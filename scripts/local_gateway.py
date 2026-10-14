"""Passerelle locale (ASGI) : mêmes règles que gateway/nginx.conf, sans Nginx.

Utilisée par les tests et par le mode démonstration sans Docker. Elle :
* efface les en-têtes X-Auth-* envoyés par le client ;
* pour une route protégée, interroge le service Identité (/api/auth/check) et
  transmet l'identité vérifiée au service ;
* sert le portail statique pour tout le reste.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

import httpx

AUTH_HEADERS = (b"x-auth-user", b"x-auth-role", b"x-auth-shop", b"x-auth-name")
FORWARDED_FOR_CHECK = (b"cookie", b"authorization", b"x-api-key")


def build_gateway(apps: dict, portal_dir: Path | None = None):
    """apps : identity, catalog, cart, orders, billing, stock, warehouse, notifications, simulator, conn_rest, conn_csv."""
    identity_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=apps["identity"]), base_url="http://identity")

    routes = [  # (préfixe, service, contrôle, réécriture)
        ("/api/auth/", "identity", None, None),
        ("/api/register", "identity", None, None),
        ("/api/admin/shops", "identity", None, None),
        ("/api/catalog", "catalog", "any", None),
        ("/api/availability", "stock", "any", None),
        ("/api/cart", "cart", "any", None),
        ("/api/orders", "orders", "any", None),
        ("/api/admin/orders", "orders", "any", None),
        ("/api/invoices", "billing", "any", None),
        ("/api/products", "catalog", "staff", None),
        ("/api/warehouse/", "warehouse", "staff", None),
        ("/svc/notifications/notifications", "notifications", "staff", "/notifications"),
        ("/svc/connector-rest/stats", "conn_rest", "staff", "/stats"),
        ("/svc/connector-csv/stats", "conn_csv", "staff", "/stats"),
        ("/boutiques", "simulator", "staff", "strip"),
    ]
    docs = {"identity", "catalog", "cart", "orders", "billing", "stock", "warehouse", "notifications"}

    async def reply(send, status: int, body: bytes, ctype: bytes = b"application/json") -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def static(scope, send) -> None:
        if portal_dir is None:
            return await reply(send, 404, b'{"detail":"Not Found"}')
        rel = scope["path"].lstrip("/") or "index.html"
        f = (portal_dir / rel).resolve()
        if not f.is_file() or portal_dir.resolve() not in f.parents:
            f = portal_dir / "index.html"
        ctype = (mimetypes.guess_type(f.name)[0] or "application/octet-stream").encode()
        await reply(send, 200, f.read_bytes(), ctype)

    async def gateway(scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"]
        headers = [(k, v) for k, v in scope["headers"] if k.lower() not in AUTH_HEADERS]

        for prefix, target, need, rewrite in routes:
            p = prefix.rstrip("/")
            if path == p or path.startswith(p + "/"):
                break
        else:
            parts = path.split("/")
            if len(parts) == 4 and parts[1] == "svc" and parts[2] in docs and parts[3] in ("docs", "openapi.json", "health"):
                app = apps[parts[2]]
                return await app({**scope, "path": "/" + parts[3], "root_path": f"/svc/{parts[2]}",
                                  "headers": headers}, receive, send)
            if path.startswith(("/svc/", "/internal/", "/api/")):
                return await reply(send, 404, b'{"detail":"Not Found"}')
            return await static(scope, send)

        if need:
            fwd = {k.decode(): v.decode() for k, v in scope["headers"] if k.lower() in FORWARDED_FOR_CHECK}
            r = await identity_client.get("/api/auth/check", params={"role": need}, headers=fwd)
            if r.status_code != 204:
                msg = b'{"detail":"Connexion requise"}' if r.status_code == 401 else \
                    b'{"detail":"Acc\\u00e8s refus\\u00e9"}'
                return await reply(send, r.status_code, msg)
            for h in ("x-auth-user", "x-auth-role", "x-auth-shop", "x-auth-name"):
                if r.headers.get(h):
                    headers.append((h.encode(), r.headers[h].encode()))

        new_path = path
        if rewrite == "strip":
            new_path = path[len(prefix.rstrip("/")):] or "/"
        elif rewrite:
            new_path = rewrite
        return await apps[target]({**scope, "path": new_path, "raw_path": new_path.encode(),
                                   "headers": headers}, receive, send)

    return gateway
