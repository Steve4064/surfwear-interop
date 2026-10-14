"""Client ZITADEL : flux OIDC (code d'autorisation + PKCE) et API d'administration.

Deux adresses :
* ``public_url``   : celle que voit le navigateur (http://localhost:8081), utilisée
                     pour les redirections vers la page de connexion ZITADEL ;
* ``internal_url`` : celle du conteneur zitadel-api sur le réseau Docker. ZITADEL
                     identifie l'instance par l'en-tête Host : chaque appel interne
                     envoie donc ``Host: <domaine public>``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

log = logging.getLogger("identity.zitadel")

SCOPES = (
    "openid profile email "
    "urn:zitadel:iam:user:resourceowner "
    "urn:zitadel:iam:org:project:roles"
)
ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"
ORG_ID_CLAIM = "urn:zitadel:iam:user:resourceowner:id"
ORG_NAME_CLAIM = "urn:zitadel:iam:user:resourceowner:name"


class ZitadelError(Exception):
    pass


@dataclass
class ZitadelSettings:
    public_url: str          # http://localhost:8081
    internal_url: str        # http://zitadel-api:8080
    host_header: str         # localhost
    client_id: str
    client_secret: str
    project_id: str
    redirect_uri: str        # http://localhost:8088/api/auth/callback
    post_logout_uri: str     # http://localhost:8088/
    pat: str = ""            # jeton du compte technique (création des organisations)

    @classmethod
    def from_bootstrap(cls, path: Path, public_url: str, internal_url: str, host_header: str,
                       redirect_uri: str, post_logout_uri: str, pat_path: Path | None) -> "ZitadelSettings":
        data = json.loads(path.read_text(encoding="utf-8"))
        pat = pat_path.read_text(encoding="utf-8").strip() if pat_path and pat_path.exists() else ""
        return cls(public_url.rstrip("/"), internal_url.rstrip("/"), host_header,
                   data["client_id"], data["client_secret"], data["project_id"],
                   redirect_uri, post_logout_uri, pat)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class ZitadelClient:
    def __init__(self, settings: ZitadelSettings, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.http = http or httpx.AsyncClient(base_url=settings.internal_url, timeout=10)

    def _headers(self, extra: dict | None = None) -> dict:
        return {"Host": self.s.host_header, **(extra or {})}

    # ------------------------------------------------------------ OIDC
    def authorize_url(self, state: str, challenge: str, prompt: str | None = None, login_hint: str | None = None) -> str:
        params = {
            "client_id": self.s.client_id,
            "redirect_uri": self.s.redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if prompt:
            params["prompt"] = prompt
        if login_hint:
            params["login_hint"] = login_hint
        return f"{self.s.public_url}/oauth/v2/authorize?{urlencode(params)}"

    def end_session_url(self, id_token: str | None) -> str:
        params = {"post_logout_redirect_uri": self.s.post_logout_uri, "client_id": self.s.client_id}
        if id_token:
            params["id_token_hint"] = id_token
        return f"{self.s.public_url}/oidc/v1/end_session?{urlencode(params)}"

    async def exchange_code(self, code: str, verifier: str) -> dict:
        r = await self.http.post(
            "/oauth/v2/token",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": self.s.redirect_uri,
                  "code_verifier": verifier},
            auth=(self.s.client_id, self.s.client_secret),
            headers=self._headers(),
        )
        if r.status_code != 200:
            raise ZitadelError(f"Échange du code refusé ({r.status_code}) : {r.text[:200]}")
        return r.json()

    async def userinfo(self, access_token: str) -> dict:
        r = await self.http.get("/oidc/v1/userinfo",
                                headers=self._headers({"Authorization": f"Bearer {access_token}"}))
        if r.status_code != 200:
            raise ZitadelError(f"Profil utilisateur indisponible ({r.status_code})")
        return r.json()

    # ------------------------------------------------------------ administration
    async def _admin(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.s.pat:
            raise ZitadelError("Jeton d'administration ZITADEL absent")
        r = await self.http.request(method, path, json=body,
                                    headers=self._headers({"Authorization": f"Bearer {self.s.pat}"}))
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise ZitadelError(f"{r.status_code} {msg}")
        return r.json() if r.content else {}

    async def setup_org(self, org_name: str, email: str, first_name: str, last_name: str, password: str) -> dict:
        """Crée l'organisation d'une boutique et son responsable (propriétaire de l'organisation)."""
        return await self._admin("POST", "/admin/v1/orgs/_setup", {
            "org": {"name": org_name},
            "human": {
                "userName": email,
                "profile": {"firstName": first_name, "lastName": last_name, "preferredLanguage": "fr"},
                "email": {"email": email, "isEmailVerified": True},
                "password": password,
            },
            "roles": ["ORG_OWNER"],
        })


def staff_role(userinfo: dict) -> bool:
    roles = userinfo.get(ROLES_CLAIM) or {}
    return "staff" in roles
