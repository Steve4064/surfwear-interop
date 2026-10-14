"""Service Identité : connexion (ZITADEL ou comptes locaux), comptes B2B des boutiques.

* Connexion OIDC avec ZITADEL (code d'autorisation + PKCE). Chaque boutique est
  une organisation ZITADEL ; ses employés sont les utilisateurs de cette organisation.
  L'équipe Surfwear porte le rôle de projet « staff ».
* Inscription d'une boutique : création de son organisation et de son responsable
  dans ZITADEL, puis attente de validation par Surfwear.
* Contrôle d'accès pour la passerelle (``/api/auth/check``) : renvoie l'identité
  vérifiée dans les en-têtes X-Auth-*, que la passerelle transmet aux services.
* Clés d'API des logiciels de boutique (éditeurs A et B).

AUTH_PROVIDER=local remplace ZITADEL par des comptes en base (tests, démonstration hors ligne).
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from uuid import uuid4

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select

from surfwear_common.bus import EventBus, bus_from_url
from surfwear_common.db import create_schema, make_engine, make_sessionmaker
from surfwear_common.events import SHOP_APPROVED, SHOP_REGISTERED, Event, ShopRegistered
from surfwear_common.service import env, setup_logging

from . import security
from .models import Base, LocalUser, Shop
from .zitadel import ORG_ID_CLAIM, ZitadelClient, ZitadelError, ZitadelSettings, pkce_pair, staff_role

SERVICE = "identity-service"
log = setup_logging(SERVICE)
OIDC_COOKIE = "sw_oidc"
CONNECTORS = ("rest", "csv", "none")


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class RegisterIn(BaseModel):
    company: str = Field(min_length=2, max_length=120, description="Raison sociale de la boutique")
    siret: str = Field(description="14 chiffres")
    address: str = Field(min_length=3, max_length=200)
    city: str = Field(min_length=2, max_length=80)
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str = Field(min_length=1, max_length=60)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)

    @field_validator("siret")
    @classmethod
    def siret_digits(cls, v: str) -> str:
        v = re.sub(r"\s", "", v)
        if not re.fullmatch(r"\d{14}", v):
            raise ValueError("Le SIRET compte 14 chiffres")
        return v

    @field_validator("password")
    @classmethod
    def strong(cls, v: str) -> str:
        checks = [r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"]
        if not all(re.search(c, v) for c in checks):
            raise ValueError("Le mot de passe doit contenir une minuscule, une majuscule, un chiffre et un symbole")
        return v


class ApproveIn(BaseModel):
    connector: str = Field("none", description="rest (éditeur A), csv (éditeur B) ou none (portail seul)")

    @field_validator("connector")
    @classmethod
    def known(cls, v: str) -> str:
        if v not in CONNECTORS:
            raise ValueError("Connecteur inconnu")
        return v


def shop_dto(s: Shop, with_key: bool = False) -> dict:
    d = {"id": s.id, "name": s.name, "city": s.city, "address": s.address, "siret": s.siret,
         "email": s.email, "contact_name": s.contact_name, "connector": s.connector, "status": s.status,
         "created_at": s.created_at.isoformat() if s.created_at else None,
         "decided_at": s.decided_at.isoformat() if s.decided_at else None}
    if with_key:
        d["api_key"] = s.api_key
    return d


def create_app(
    database_url: str | None = None,
    bus: EventBus | None = None,
    provider: str | None = None,
    zitadel: ZitadelClient | None = None,
    shops_file: str | None = None,
    users_file: str | None = None,
    auth_secret: str | None = None,
) -> FastAPI:
    engine = make_engine(database_url or env("DATABASE_URL", "sqlite+aiosqlite:///./identity.db"))
    Session = make_sessionmaker(engine)
    bus = bus or bus_from_url(env("BUS_URL", "memory://"))
    provider = provider or env("AUTH_PROVIDER", "local")
    secret = auth_secret or env("AUTH_SECRET", "dev-only-secret-change-me")
    ttl = int(float(env("SESSION_HOURS", "8")) * 3600)
    shops_path = Path(shops_file or env("SHOPS_FILE", "/config/shops.json"))
    users_path = Path(users_file or env("USERS_FILE", "/config/users.json"))
    state: dict = {"zitadel": zitadel}
    staff_api_key = env("STAFF_API_KEY", "")  # clé technique de l'équipe Surfwear (scripts) ; vide = désactivée

    # ------------------------------------------------------------ amorçage
    async def load_zitadel() -> None:
        if state["zitadel"] is not None:
            return
        path = Path(env("ZITADEL_BOOTSTRAP_FILE", "/bootstrap/zitadel.json"))
        for attempt in range(300):
            if path.exists():
                break
            if attempt % 15 == 0:
                log.info("En attente de la configuration ZITADEL (%s)…", path)
            await asyncio.sleep(2)
        else:
            raise RuntimeError("Configuration ZITADEL introuvable : le conteneur zitadel-bootstrap a-t-il réussi ?")
        public = env("PUBLIC_URL", "http://localhost:8080").rstrip("/")
        settings = ZitadelSettings.from_bootstrap(
            path,
            public_url=env("ZITADEL_PUBLIC_URL", "http://localhost:8081"),
            internal_url=env("ZITADEL_INTERNAL_URL", "http://zitadel-api:8080"),
            host_header=env("ZITADEL_DOMAIN", "localhost"),
            redirect_uri=f"{public}/api/auth/callback",
            post_logout_uri=f"{public}/",
            pat_path=Path(env("ZITADEL_ADMIN_PAT_FILE", "/bootstrap/admin.pat")),
        )
        state["zitadel"] = ZitadelClient(settings)
        state["demo_orgs"] = json.loads(path.read_text(encoding="utf-8")).get("demo_orgs", {})
        log.info("ZITADEL configuré : %s", settings.public_url)

    async def seed() -> None:
        async with Session.begin() as s:
            if not (await s.execute(select(func.count()).select_from(Shop))).scalar_one() and shops_path.exists():
                for shop in json.loads(shops_path.read_text(encoding="utf-8"))["shops"]:
                    s.add(Shop(id=shop["id"], name=shop["name"], city=shop.get("city", ""),
                               email=shop["email"], connector=shop["connector"], api_key=shop["api_key"],
                               address=shop.get("address", ""), siret=shop.get("siret", ""),
                               contact_name=shop.get("contact_name", ""),
                               status="active", decided_at=datetime.now(timezone.utc), decided_by="démonstration"))
                log.info("Boutiques de démonstration créées depuis %s", shops_path)
        if provider == "local":
            async with Session.begin() as s:
                if not (await s.execute(select(func.count()).select_from(LocalUser))).scalar_one() \
                        and users_path.exists():
                    for u in json.loads(users_path.read_text(encoding="utf-8"))["users"]:
                        s.add(LocalUser(username=u["username"].lower(),
                                        password_hash=security.hash_password(u["password"]),
                                        role=u["role"], shop_id=u.get("shop_id"),
                                        display_name=u.get("display_name", "")))
        for shop_id, org_id in (state.get("demo_orgs") or {}).items():
            async with Session.begin() as s:
                shop = await s.get(Shop, shop_id)
                if shop and not shop.zitadel_org_id:
                    shop.zitadel_org_id = org_id

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await create_schema(engine, Base)
        if provider == "zitadel":
            await load_zitadel()
        await seed()
        await bus.connect()
        yield
        await bus.close()
        await engine.dispose()

    app = FastAPI(root_path=env("ROOT_PATH", ""), title="Service Identité", version="3.0.0",
                  description="Connexion via ZITADEL, comptes B2B des boutiques, contrôle d'accès de la passerelle.",
                  lifespan=lifespan)
    app.state.session = Session

    # ------------------------------------------------------------ sessions
    def set_session(response: Response, request: Request, claims: dict) -> None:
        token = security.make_token(secret, claims, ttl)
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(security.COOKIE_NAME, token, max_age=ttl, httponly=True,
                            samesite="lax", secure=secure, path="/")

    async def principal(authorization: str | None, x_api_key: str | None, cookie: str | None) -> dict | None:
        """Renvoie {user, role, name, shop} ; shop est l'objet Shop pour une boutique."""
        if x_api_key and staff_api_key and secrets.compare_digest(x_api_key, staff_api_key):
            return {"user": "api:surfwear", "role": "staff", "name": "Automatisation Surfwear", "shop": None}
        async with Session() as s:
            if x_api_key:
                shop = (await s.execute(select(Shop).where(Shop.api_key == x_api_key))).scalar_one_or_none()
                if shop is None:
                    return None
                return {"user": f"api:{shop.id}", "role": "shop", "name": shop.name, "shop": shop}
            token = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else cookie
            claims = security.read_token(secret, token) if token else None
            if not claims:
                return None
            shop = await s.get(Shop, claims["shop"]) if claims.get("shop") else None
            if claims["role"] == "shop" and shop is None:
                return None
            return {"user": claims["sub"], "role": claims["role"], "name": claims.get("name", ""),
                    "shop": shop, "idt": claims.get("idt")}

    async def current(
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
        sw_session: str | None = Cookie(None),
    ) -> dict | None:
        return await principal(authorization, x_api_key, sw_session)

    def me_dto(p: dict) -> dict:
        shop = p["shop"]
        return {"user": p["user"], "role": p["role"], "display_name": p["name"],
                "shop": shop_dto(shop) if shop else None, "provider": provider,
                "hold_minutes": float(env("HOLD_MINUTES", "15"))}

    # ------------------------------------------------------------ connexion
    @app.get("/health", tags=["supervision"])
    async def health():
        return {"status": "ok", "service": SERVICE, "provider": provider}

    @app.get("/api/auth/config", tags=["connexion"], summary="Mode de connexion")
    async def config():
        return {"provider": provider, "registration": True}

    @app.get("/api/auth/login", tags=["connexion"], summary="Démarrer la connexion ZITADEL")
    async def login_redirect(request: Request, hint: str | None = None):
        if provider != "zitadel":
            raise HTTPException(405, "Connexion par formulaire (POST) en mode local")
        z: ZitadelClient = state["zitadel"]
        verifier, challenge = pkce_pair()
        st = secrets.token_urlsafe(24)
        resp = RedirectResponse(z.authorize_url(st, challenge, login_hint=hint), status_code=302)
        resp.set_cookie(OIDC_COOKIE, security.make_token(secret, {"st": st, "v": verifier}, 600),
                        max_age=600, httponly=True, samesite="lax", path="/api/auth")
        return resp

    @app.get("/api/auth/callback", tags=["connexion"], include_in_schema=False)
    async def callback(request: Request, code: str | None = None, state_: str | None = Query(None, alias="state"),
                       error: str | None = None, sw_oidc: str | None = Cookie(None)):
        def fail(msg: str) -> RedirectResponse:
            r = RedirectResponse(f"/?auth_error={quote(msg)}", status_code=302)
            r.delete_cookie(OIDC_COOKIE, path="/api/auth")
            return r

        if error:
            return fail("Connexion annulée")
        pending = security.read_token(secret, sw_oidc) if sw_oidc else None
        if not pending or not code or pending.get("st") != state_:
            return fail("Session de connexion expirée, recommencez")
        z: ZitadelClient = state["zitadel"]
        try:
            tokens = await z.exchange_code(code, pending["v"])
            info = await z.userinfo(tokens["access_token"])
        except (ZitadelError, KeyError) as exc:
            log.warning("Connexion ZITADEL échouée : %s", exc)
            return fail("La connexion a échoué, réessayez")
        name = info.get("name") or info.get("preferred_username") or info.get("email", "")
        claims = {"sub": info["sub"], "name": name, "idt": tokens.get("id_token")}
        if staff_role(info):
            claims.update(role="staff", shop=None)
        else:
            async with Session() as s:
                shop = (await s.execute(select(Shop).where(Shop.zitadel_org_id == info.get(ORG_ID_CLAIM)))
                        ).scalar_one_or_none()
            if shop is None:
                return fail("Ce compte n'est rattaché à aucune boutique cliente. Créez un compte boutique.")
            claims.update(role="shop", shop=shop.id)
        resp = RedirectResponse("/", status_code=302)
        resp.delete_cookie(OIDC_COOKIE, path="/api/auth")
        set_session(resp, request, claims)
        log.info("Connexion de %s (%s)", name, claims["role"])
        return resp

    @app.post("/api/auth/login", tags=["connexion"], summary="Connexion par formulaire (mode local)")
    async def login_local(body: LoginIn, request: Request, response: Response):
        if provider != "local":
            raise HTTPException(405, "Utilisez la connexion ZITADEL")
        async with Session() as s:
            user = await s.get(LocalUser, body.username.strip().lower())
            shop = await s.get(Shop, user.shop_id) if user and user.shop_id else None
        if user is None or not security.verify_password(body.password, user.password_hash):
            await asyncio.sleep(0.4)
            raise HTTPException(401, "Identifiant ou mot de passe incorrect")
        set_session(response, request, {"sub": user.username, "role": user.role, "shop": user.shop_id,
                                        "name": user.display_name})
        return me_dto({"user": user.username, "role": user.role, "name": user.display_name, "shop": shop})

    @app.post("/api/auth/logout", tags=["connexion"], summary="Se déconnecter")
    async def logout(response: Response, p: dict | None = Depends(current)):
        response.delete_cookie(security.COOKIE_NAME, path="/")
        if provider == "zitadel" and state["zitadel"]:
            return {"logout_url": state["zitadel"].end_session_url((p or {}).get("idt"))}
        return {"logout_url": None}

    @app.get("/api/auth/me", tags=["connexion"], summary="Utilisateur connecté")
    async def me(p: dict | None = Depends(current)):
        if p is None:
            raise HTTPException(401, "Connexion requise")
        return me_dto(p)

    @app.get("/api/auth/check", tags=["connexion"], include_in_schema=False)
    async def check(response: Response, role: str = Query("any"), p: dict | None = Depends(current)):
        """Appelé par la passerelle (auth_request). 204 + en-têtes X-Auth-* si l'accès est autorisé."""
        if p is None:
            raise HTTPException(401, "Connexion requise")
        if p["role"] == "shop" and p["shop"].status != "active":
            raise HTTPException(403, "Compte boutique en attente de validation par Surfwear")
        if role == "staff" and p["role"] != "staff":
            raise HTTPException(403, "Réservé à l'équipe Surfwear")
        response.status_code = 204
        response.headers["X-Auth-User"] = p["user"]
        response.headers["X-Auth-Role"] = p["role"]
        response.headers["X-Auth-Name"] = quote(p["name"] or "")
        response.headers["X-Auth-Shop"] = p["shop"].id if p["shop"] else ""
        return None

    # ------------------------------------------------------------ inscription B2B
    @app.post("/api/register", tags=["inscription"], status_code=201, summary="Créer un compte boutique")
    async def register(body: RegisterIn):
        async with Session() as s:
            dup = (await s.execute(select(Shop).where(Shop.siret == body.siret))).scalar_one_or_none()
        if dup:
            raise HTTPException(409, "Une boutique avec ce SIRET est déjà inscrite")
        shop_id = f"SHOP-{uuid4().hex[:6].upper()}"
        org_id = None
        if provider == "zitadel":
            try:
                created = await state["zitadel"].setup_org(body.company, body.email, body.first_name,
                                                           body.last_name, body.password)
                org_id = created.get("orgId")
            except ZitadelError as exc:
                msg = str(exc)
                log.warning("Création d'organisation refusée : %s", msg)
                if "already" in msg.lower() or "409" in msg or "existe" in msg.lower():
                    raise HTTPException(409, "Un compte existe déjà avec cette adresse e-mail ou ce nom")
                raise HTTPException(502, "Le service d'identité n'a pas pu créer le compte, réessayez")
        async with Session.begin() as s:
            s.add(Shop(id=shop_id, name=body.company, city=body.city, address=body.address, siret=body.siret,
                       email=str(body.email), contact_name=f"{body.first_name} {body.last_name}",
                       status="pending", zitadel_org_id=org_id))
            if provider == "local":
                if await s.get(LocalUser, str(body.email).lower()):
                    raise HTTPException(409, "Un compte existe déjà avec cette adresse e-mail")
                s.add(LocalUser(username=str(body.email).lower(), password_hash=security.hash_password(body.password),
                                role="shop", shop_id=shop_id, display_name=f"{body.first_name} {body.last_name}"))
        await bus.publish(Event.build(SHOP_REGISTERED, SERVICE, ShopRegistered(
            shop_id=shop_id, name=body.company, city=body.city, email=str(body.email), status="pending")))
        log.info("Inscription de %s (%s), en attente de validation", body.company, shop_id)
        return {"shop_id": shop_id, "status": "pending",
                "message": "Compte créé. Surfwear doit le valider avant votre première commande."}

    # ------------------------------------------------------------ Surfwear : boutiques
    async def staff(p: dict | None = Depends(current)) -> dict:
        if p is None:
            raise HTTPException(401, "Connexion requise")
        if p["role"] != "staff":
            raise HTTPException(403, "Réservé à l'équipe Surfwear")
        return p

    @app.get("/api/admin/shops", tags=["Surfwear"], summary="Boutiques clientes")
    async def list_shops(_: dict = Depends(staff)):
        async with Session() as s:
            rows = (await s.execute(select(Shop).order_by(Shop.status != "pending", Shop.name))).scalars().all()
        return [shop_dto(x, with_key=True) for x in rows]

    async def decide(shop_id: str, status: str, who: str, connector: str | None = None) -> Shop:
        async with Session.begin() as s:
            shop = await s.get(Shop, shop_id)
            if shop is None:
                raise HTTPException(404, "Boutique introuvable")
            shop.status = status
            shop.decided_at = datetime.now(timezone.utc)
            shop.decided_by = who
            if connector:
                shop.connector = connector
            if status == "active" and not shop.api_key:
                shop.api_key = f"sk_{secrets.token_urlsafe(24)}"
            return shop

    @app.post("/api/admin/shops/{shop_id}/approve", tags=["Surfwear"], summary="Valider une boutique")
    async def approve(shop_id: str, body: ApproveIn, p: dict = Depends(staff)):
        shop = await decide(shop_id, "active", p["name"], body.connector)
        await bus.publish(Event.build(SHOP_APPROVED, SERVICE, {
            "shop": {"id": shop.id, "name": shop.name, "connector": shop.connector, "email": shop.email},
            "status": "active"}))
        return shop_dto(shop, with_key=True)

    @app.post("/api/admin/shops/{shop_id}/reject", tags=["Surfwear"], summary="Refuser une inscription")
    async def reject(shop_id: str, p: dict = Depends(staff)):
        return shop_dto(await decide(shop_id, "rejected", p["name"]))

    @app.post("/api/admin/shops/{shop_id}/suspend", tags=["Surfwear"], summary="Suspendre une boutique")
    async def suspend(shop_id: str, p: dict = Depends(staff)):
        return shop_dto(await decide(shop_id, "suspended", p["name"]))

    # ------------------------------------------------------------ interne
    @app.get("/internal/shops/{shop_id}", tags=["interne"])
    async def internal_shop(shop_id: str):
        async with Session() as s:
            shop = await s.get(Shop, shop_id)
        if shop is None:
            raise HTTPException(404, "Boutique introuvable")
        return {"id": shop.id, "name": shop.name, "connector": shop.connector, "email": shop.email,
                "city": shop.city, "address": shop.address, "siret": shop.siret, "status": shop.status}

    return app
