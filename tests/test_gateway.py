"""API Gateway : routes de gateway/kong.yml et code Lua du contrôle d'accès.

* Les routes sont vérifiées à travers la passerelle locale, qui applique kong.yml.
* Le code Lua du plugin pre-function est exécuté tel quel dans un interpréteur Lua
  (lupa), avec un faux PDK Kong et un faux client HTTP : on vérifie ce qu'il transmet
  aux services et quand il interrompt la requête.
"""
from __future__ import annotations

import pytest
import yaml

from conftest import key
from local_gateway import KONG_FILE

lupa = pytest.importorskip("lupa")

HARNESS = """
local state = { cleared = {}, set = {}, path = nil, exit = nil, calls = {} }
function make_kong(route_tags, req_path, req_headers, check_status, check_headers)
  package.loaded["resty.http"] = {
    new = function()
      return {
        set_timeout = function() end,
        request_uri = function(_, url, opts)
          table.insert(state.calls, url)
          state.forwarded = opts.headers
          return { status = check_status, headers = check_headers }
        end,
      }
    end,
  }
  kong = {
    router = { get_route = function() return { tags = route_tags } end },
    request = {
      get_path = function() return req_path end,
      get_header = function(h) return req_headers[h] end,
    },
    service = { request = {
      clear_header = function(h) table.insert(state.cleared, h) end,
      set_header = function(h, v) state.set[h] = v end,
      set_path = function(p) state.path = p end,
    } },
    response = { exit = function(status, body, headers) state.exit = { status = status, body = body, headers = headers } end },
    log = { err = function() end },
  }
  return state
end
"""


def run_lua(path: str, tags: list[str], headers: dict | None = None, status: int = 204,
            reply_headers: dict | None = None):
    code = yaml.safe_load(KONG_FILE.read_text())["plugins"][0]["config"]["access"][0]
    L = lupa.LuaRuntime(unpack_returned_tuples=True)
    L.execute(HARNESS)
    state = L.globals().make_kong(L.table_from(tags), path, L.table_from(headers or {}), status,
                                  L.table_from(reply_headers or {}))
    L.execute(code)
    exit_ = state.exit
    return {
        "cleared": list(state.cleared.values()),
        "set": dict(state.set.items()),
        "path": state.path,
        "calls": list(state.calls.values()),
        "forwarded": dict(state.forwarded.items()) if state.forwarded else {},
        "exit": None if exit_ is None else {"status": exit_.status,
                                            "body": dict(exit_.body.items()) if lupa.lua_type(exit_.body) == "table" else exit_.body,
                                            "headers": dict(exit_.headers.items()) if exit_.headers else {}},
    }


def test_lua_public_route_only_clears_identity_headers():
    out = run_lua("/api/auth/me", [], {"x-auth-role": "staff"})
    assert set(out["cleared"]) == {"X-Auth-User", "X-Auth-Role", "X-Auth-Shop", "X-Auth-Name"}
    assert not out["calls"] and not out["set"] and out["exit"] is None


def test_lua_protected_route_forwards_verified_identity():
    out = run_lua("/api/catalog", ["auth-any"], {"authorization": "Bearer abc", "x-forwarded-for": "1.2.3.4"},
                  reply_headers={"X-Auth-User": "u1", "X-Auth-Role": "shop", "X-Auth-Shop": "SHOP-BIA",
                                 "X-Auth-Name": "L%C3%A9a"})
    assert out["calls"] == ["http://identity-service:8000/api/auth/check?role=any"]
    assert out["forwarded"] == {"authorization": "Bearer abc"}
    assert out["set"] == {"X-Auth-User": "u1", "X-Auth-Role": "shop", "X-Auth-Shop": "SHOP-BIA", "X-Auth-Name": "L%C3%A9a"}
    assert out["exit"] is None


@pytest.mark.parametrize("status,expected", [(401, 401), (403, 403), (500, 502)])
def test_lua_denies(status, expected):
    out = run_lua("/api/warehouse/stock", ["auth-staff"], status=status)
    assert out["calls"][0].endswith("role=staff")
    assert out["exit"]["status"] == expected and out["set"] == {}


def test_lua_rewrites_and_redirects():
    assert run_lua("/svc/catalog/openapi.json", ["strip-svc"])["path"] == "/openapi.json"
    out = run_lua("/boutiques", ["auth-staff", "slash-redirect"])
    assert out["exit"] == {"status": 301, "body": "", "headers": {"Location": "/boutiques/"}} and not out["calls"]


@pytest.mark.asyncio
async def test_routes_from_kong_config(eco):
    c = eco.client()
    assert (await c.get("/internal/stock")).status_code == 404
    assert (await c.get("/svc/catalog/internal/products")).status_code == 404
    assert (await c.get("/api/inconnue")).status_code == 404
    assert (await c.get("/svc/catalog/health")).json()["status"] == "ok"
    assert (await c.get("/svc/mcp/health")).json()["service"] == "mcp-server"
    assert (await c.get("/boutiques")).headers["location"] == "/boutiques/"
    assert (await c.get("/boutiques/", headers=key("biarritz"))).status_code == 403   # équipe Surfwear
    assert "<html" in (await c.get("/commandes")).text.lower()   # le reste : le portail


@pytest.mark.asyncio
async def test_spoofed_identity_headers_are_ignored(eco):
    c = eco.client()
    spoof = {"X-Auth-User": "x", "X-Auth-Role": "staff", "X-Auth-Shop": ""}
    assert (await c.get("/api/warehouse/stock", headers=spoof)).status_code == 401
    assert (await c.get("/api/warehouse/stock", headers={**spoof, **key("biarritz")})).status_code == 403


@pytest.mark.asyncio
async def test_personal_token(eco):
    shop = await eco.login("lacanau@boutiques.example")
    token = (await shop.post("/api/auth/token")).json()["token"]
    c = eco.client()
    me = await c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["shop"]["id"] == "SHOP-LAC"
    # un jeton ne se crée pas avec une clé d'API
    assert (await c.post("/api/auth/token", headers=key("lacanau"))).status_code == 403
