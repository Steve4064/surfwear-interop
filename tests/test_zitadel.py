"""Intégration ZITADEL du service Identité, contre un faux ZITADEL qui vérifie le protocole.

Le faux fournisseur contrôle ce que ZITADEL contrôlerait : en-tête Host de l'instance,
authentification du client, code à usage unique, preuve PKCE (S256), jeton
d'administration pour la création d'organisation.
"""
from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI, Form, Header, HTTPException, Request

from identity_service.main import create_app as create_identity
from identity_service.zitadel import ORG_ID_CLAIM, ROLES_CLAIM, ZitadelClient, ZitadelSettings
from surfwear_common.bus import MemoryBus

pytestmark = pytest.mark.asyncio
STAFF_ORG, BIA_ORG = "org-surfwear", "org-biarritz"


def fake_zitadel():
    app = FastAPI()
    st = {"codes": {}, "tokens": {}, "orgs": []}

    def instance(host: str | None):
        if host != "localhost":
            raise HTTPException(404, "Instance not found")

    @app.post("/oauth/v2/token")
    async def token(request: Request, code: str = Form(), code_verifier: str = Form(), redirect_uri: str = Form(),
                    grant_type: str = Form(), host: str = Header(None), authorization: str = Header("")):
        instance(host)
        assert grant_type == "authorization_code" and redirect_uri == "http://localhost:8080/api/auth/callback"
        assert base64.b64decode(authorization.split()[1]).decode() == "client-123:secret-xyz"
        entry = st["codes"].pop(code, None)  # usage unique
        if entry is None:
            raise HTTPException(400, "invalid_grant")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
        if challenge != entry["challenge"]:
            raise HTTPException(400, "invalid PKCE")
        st["tokens"]["at-" + code] = entry["user"]
        return {"access_token": "at-" + code, "id_token": "idt-" + code, "token_type": "Bearer"}

    @app.get("/oidc/v1/userinfo")
    async def userinfo(authorization: str = Header(""), host: str = Header(None)):
        instance(host)
        user = st["tokens"].get(authorization.removeprefix("Bearer "))
        if user is None:
            raise HTTPException(401)
        return user

    @app.post("/admin/v1/orgs/_setup")
    async def setup(body: dict, authorization: str = Header(""), host: str = Header(None)):
        instance(host)
        assert authorization == "Bearer admin-pat"
        if any(o["org"]["name"] == body["org"]["name"] for o in st["orgs"]):
            raise HTTPException(409, "Organisation already exists")
        st["orgs"].append(body)
        return {"orgId": f"org-{len(st['orgs'])}", "userId": "u-new"}

    return app, st


@pytest.fixture
async def ident(tmp_path):
    fake, state = fake_zitadel()
    settings = ZitadelSettings(public_url="http://localhost:8081", internal_url="http://zitadel-api:8080",
                               host_header="localhost", client_id="client-123", client_secret="secret-xyz",
                               project_id="p1", redirect_uri="http://localhost:8080/api/auth/callback",
                               post_logout_uri="http://localhost:8080/", pat="admin-pat")
    z = ZitadelClient(settings, httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), base_url="http://zitadel-api"))
    from conftest import CONFIG
    app = create_identity(f"sqlite+aiosqlite:///{tmp_path}/id.db", MemoryBus(), provider="zitadel", zitadel=z,
                          shops_file=str(CONFIG / "shops.json"), auth_secret="t")
    async with app.router.lifespan_context(app):
        async with app.state.session.begin() as s:
            from identity_service.models import Shop
            (await s.get(Shop, "SHOP-BIA")).zitadel_org_id = BIA_ORG
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080")
        yield client, state
        await client.aclose()


async def sign_in(client: httpx.AsyncClient, state: dict, user: dict, code: str = "c1") -> httpx.Response:
    r = await client.get("/api/auth/login")
    assert r.status_code == 302
    url = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "localhost:8081" and url.path == "/oauth/v2/authorize"
    assert q["client_id"] == "client-123" and q["code_challenge_method"] == "S256"
    assert "urn:zitadel:iam:org:project:roles" in q["scope"]
    state["codes"][code] = {"challenge": q["code_challenge"], "user": user}
    return await client.get("/api/auth/callback", params={"code": code, "state": q["state"]})


async def test_staff_login_through_zitadel(ident):
    client, state = ident
    r = await sign_in(client, state, {"sub": "u-staff", "name": "Équipe Surfwear", ORG_ID_CLAIM: STAFF_ORG,
                                      ROLES_CLAIM: {"staff": {STAFF_ORG: "surfwear.localhost"}}})
    assert r.status_code == 302 and r.headers["location"] == "/"
    me = (await client.get("/api/auth/me")).json()
    assert me["role"] == "staff" and me["provider"] == "zitadel"
    check = await client.get("/api/auth/check", params={"role": "staff"})
    assert check.status_code == 204 and check.headers["x-auth-role"] == "staff"
    out = (await client.post("/api/auth/logout")).json()
    assert out["logout_url"].startswith("http://localhost:8081/oidc/v1/end_session?") and "id_token_hint=idt-c1" in out["logout_url"]


async def test_shop_user_mapped_by_organisation(ident):
    client, state = ident
    await sign_in(client, state, {"sub": "u-lea", "name": "Léa", ORG_ID_CLAIM: BIA_ORG})
    check = await client.get("/api/auth/check")
    assert check.status_code == 204 and check.headers["x-auth-shop"] == "SHOP-BIA"
    assert (await client.get("/api/auth/check", params={"role": "staff"})).status_code == 403


async def test_user_without_shop_or_role_is_refused(ident):
    client, state = ident
    # Un utilisateur de l'organisation Surfwear sans le rôle « staff » n'a pas accès
    r = await sign_in(client, state, {"sub": "u-x", "name": "X", ORG_ID_CLAIM: STAFF_ORG})
    assert "auth_error" in r.headers["location"]
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_replayed_code_and_bad_state_are_refused(ident):
    client, state = ident
    await sign_in(client, state, {"sub": "u-lea", "name": "Léa", ORG_ID_CLAIM: BIA_ORG}, code="c9")
    client.cookies.clear()
    r = await client.get("/api/auth/callback", params={"code": "c9", "state": "forged"})
    assert "auth_error" in r.headers["location"]


async def test_registration_creates_zitadel_organisation(ident):
    client, state = ident
    body = {"company": "Capbreton Surf Co", "siret": "12345678900012", "address": "2 quai", "city": "Capbreton",
            "first_name": "Jon", "last_name": "Darrigade", "email": "jon@capbreton.example", "password": "Capbreton2026!"}
    r = await client.post("/api/register", json=body)
    assert r.status_code == 201
    sent = state["orgs"][0]
    assert sent["org"]["name"] == "Capbreton Surf Co" and sent["human"]["userName"] == "jon@capbreton.example"
    assert sent["human"]["email"]["isEmailVerified"] is True and sent["roles"] == ["ORG_OWNER"]
    # Le responsable se connecte : compte rattaché à sa boutique, en attente de validation
    await sign_in(client, state, {"sub": "u-new", "name": "Jon", ORG_ID_CLAIM: "org-1"})
    assert (await client.get("/api/auth/me")).json()["shop"]["status"] == "pending"
    assert (await client.get("/api/auth/check")).status_code == 403
    dup = await client.post("/api/register", json={**body, "siret": "99999999900099"})
    assert dup.status_code == 409
