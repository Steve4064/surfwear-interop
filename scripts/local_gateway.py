"""Passerelle locale (ASGI) qui applique gateway/kong.yml sans Kong.

Utilisée par les tests et par le mode démonstration sans Docker. Elle lit la
configuration déclarative de l'API Gateway et en reproduit le comportement :
* choix de la route par le préfixe le plus long (comme le routeur de Kong) ;
* effacement des en-têtes X-Auth-* envoyés par le client ;
* étiquettes « auth-any » / « auth-staff » : appel au service Identité
  (/api/auth/check) puis transmission de l'identité vérifiée ;
* étiquettes « strip-svc » et « slash-redirect », strip_path, request-termination ;
* le service « portal » est servi depuis le dossier portal/.
La limitation de débit n'est pas reproduite. Le code Lua lui-même est testé à part
(tests/test_gateway.py) avec un interpréteur Lua.
"""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

KONG_FILE = Path(__file__).resolve().parents[1] / "gateway" / "kong.yml"
AUTH_HEADERS = (b"x-auth-user", b"x-auth-role", b"x-auth-shop", b"x-auth-name")
FORWARDED_FOR_CHECK = (b"cookie", b"authorization", b"x-api-key")
HOSTS = {  # hôte docker-compose -> clé de l'application dans local_stack.build()
    "identity-service": "identity", "catalog-service": "catalog", "stock-service": "stock",
    "cart-service": "cart", "order-service": "orders", "billing-service": "billing",
    "warehouse-service": "warehouse", "notification-service": "notifications",
    "shop-connector-rest": "conn_rest", "shop-connector-csv": "conn_csv",
    "shop-simulator": "simulator", "mcp-server": "mcp", "portal": "portal",
}


def load_routes(path: Path = KONG_FILE) -> list[dict]:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    routes = []
    for service in config["services"]:
        target = HOSTS[urlparse(service["url"]).hostname]
        for route in service["routes"]:
            terminate = next((p["config"] for p in route.get("plugins", []) if p["name"] == "request-termination"), None)
            for prefix in route["paths"]:
                routes.append({"name": route["name"], "prefix": prefix, "target": target,
                               "tags": set(route.get("tags", [])), "strip": route.get("strip_path", True),
                               "terminate": terminate})
    return sorted(routes, key=lambda r: len(r["prefix"]), reverse=True)


def build_gateway(apps: dict, portal_dir: Path | None = None, kong_file: Path = KONG_FILE):
    """apps : identity, catalog, cart, orders, billing, stock, warehouse, notifications, simulator,
    conn_rest, conn_csv et, facultativement, mcp."""
    routes = load_routes(kong_file)
    identity_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=apps["identity"]), base_url="http://identity")

    async def reply(send, status: int, body: bytes, ctype: bytes = b"application/json", extra=()) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode()), *extra]})
        await send({"type": "http.response.body", "body": body})

    def detail(msg: str) -> bytes:
        return json.dumps({"detail": msg}).encode()

    async def static(scope, send) -> None:
        if portal_dir is None:
            return await reply(send, 404, detail("Not Found"))
        rel = scope["path"].lstrip("/") or "index.html"
        f = (portal_dir / rel).resolve()
        if not f.is_file() or portal_dir.resolve() not in f.parents:
            f = portal_dir / "index.html"
        ctype = (mimetypes.guess_type(f.name)[0] or "application/octet-stream").encode()
        await reply(send, 200, f.read_bytes(), ctype)

    async def gateway(scope, receive, send):
        if scope["type"] == "lifespan":
            return
        if scope["type"] != "http":
            return
        path = scope["path"]
        route = next(r for r in routes if path.startswith(r["prefix"]))   # « / » attrape le reste
        headers = [(k, v) for k, v in scope["headers"] if k.lower() not in AUTH_HEADERS]
        tags = route["tags"]

        if "slash-redirect" in tags and "/" not in path[1:]:
            return await reply(send, 301, b"", extra=[(b"location", (path + "/").encode())])
        new_path = path
        if "strip-svc" in tags:
            new_path = "/" + path.split("/", 3)[3] if path.count("/") >= 3 else "/"
        need = "staff" if "auth-staff" in tags else "any" if "auth-any" in tags else None
        if need:
            fwd = {k.decode(): v.decode() for k, v in scope["headers"] if k.lower() in FORWARDED_FOR_CHECK}
            r = await identity_client.get("/api/auth/check", params={"role": need}, headers=fwd)
            if r.status_code == 401:
                return await reply(send, 401, detail("Connexion requise"))
            if r.status_code == 403:
                return await reply(send, 403, detail("Accès refusé : compte en attente de validation ou réservé à l'équipe Surfwear"))
            for h in ("x-auth-user", "x-auth-role", "x-auth-shop", "x-auth-name"):
                if r.headers.get(h):
                    headers.append((h.encode(), r.headers[h].encode()))
        if route["terminate"]:
            t = route["terminate"]
            return await reply(send, t["status_code"], t.get("body", "").encode(), t.get("content_type", "application/json").encode())
        if route["strip"]:
            new_path = path[len(route["prefix"].rstrip("/")):] or "/"
        if route["target"] == "portal":
            return await static(scope, send)
        app = apps.get(route["target"])
        if app is None:
            return await reply(send, 503, detail(f"Service {route['target']} absent"))
        return await app({**scope, "path": new_path, "raw_path": new_path.encode(), "headers": headers},
                         receive, send)

    return gateway
