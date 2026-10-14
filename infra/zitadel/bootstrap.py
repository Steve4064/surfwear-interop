"""Configure ZITADEL pour Surfwear Pro (s'exécute une fois, dans le conteneur zitadel-bootstrap).

Étapes (chacune mémorisée dans /bootstrap/progress.json : une relance reprend où elle s'est arrêtée) :
1. connexion au compte technique créé au premier démarrage (jeton /bootstrap/admin.pat) ;
2. connexion par simple identifiant ou e-mail (sans suffixe de domaine d'organisation) ;
3. projet « Surfwear Pro » avec le rôle « staff » ;
4. application OIDC du portail (code d'autorisation + PKCE, secret client) ;
5. compte de l'équipe Surfwear, avec le rôle « staff » ;
6. une organisation par boutique de démonstration, avec son responsable ;
7. écriture de /bootstrap/zitadel.json, lu par le service Identité.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

OUT = Path(os.environ.get("BOOTSTRAP_DIR", "/bootstrap"))
API = os.environ.get("ZITADEL_INTERNAL_URL", "http://zitadel-api:8080")
HOST = os.environ.get("ZITADEL_DOMAIN", "localhost")
PUBLIC_URLS = [u.strip().rstrip("/") for u in os.environ.get("PORTAL_PUBLIC_URLS", "http://localhost:8080").split(",") if u.strip()]
STAFF_EMAIL = os.environ.get("STAFF_EMAIL", "equipe@surfwear.example")
STAFF_PASSWORD = os.environ.get("STAFF_PASSWORD", "Surfwear2026!")
DEMO_FILE = Path(os.environ.get("DEMO_ACCOUNTS_FILE", "/config/zitadel-demo.json"))

progress_file = OUT / "progress.json"
progress = json.loads(progress_file.read_text()) if progress_file.exists() else {}


def say(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def save() -> None:
    progress_file.write_text(json.dumps(progress, indent=2))


def wait_for_pat() -> str:
    pat_file = OUT / "admin.pat"
    for _ in range(180):
        if pat_file.exists() and pat_file.read_text().strip():
            return pat_file.read_text().strip()
        time.sleep(2)
    sys.exit("Jeton d'administration introuvable : ZITADEL a-t-il démarré ?")


def client(pat: str) -> httpx.Client:
    return httpx.Client(base_url=API, timeout=30,
                        headers={"Host": HOST, "Authorization": f"Bearer {pat}", "Content-Type": "application/json"})


def call(c: httpx.Client, method: str, path: str, body: dict | None = None, org: str | None = None,
         ok_conflict: bool = False, optional: bool = False) -> dict:
    headers = {"x-zitadel-orgid": org} if org else {}
    for attempt in range(30):
        try:
            r = c.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            say(f"API injoignable ({exc}), nouvel essai…")
            time.sleep(3)
            continue
        if r.status_code in (502, 503, 504) or (r.status_code == 401 and attempt < 10):
            time.sleep(3)
            continue
        if r.status_code == 409 and ok_conflict:
            return {"conflict": True}
        if r.status_code >= 400:
            if optional:
                say(f"Étape facultative ignorée : {method} {path} -> {r.status_code} {r.text[:200]}")
                return {}
            sys.exit(f"{method} {path} -> {r.status_code} {r.text[:400]}")
        return r.json() if r.content else {}
    sys.exit(f"{method} {path} : ZITADEL ne répond pas")


def main() -> None:
    final = OUT / "zitadel.json"
    if final.exists():
        say("Déjà configuré, rien à faire.")
        return
    pat = wait_for_pat()
    c = client(pat)

    if "domain_policy" not in progress:
        say("Connexion par identifiant ou e-mail, sans suffixe de domaine")
        call(c, "PUT", "/admin/v1/policies/domain",
             {"userLoginMustBeDomain": False, "validateOrgDomains": False,
              "smtpSenderAddressMatchesInstanceDomain": False}, ok_conflict=True, optional=True)
        progress["domain_policy"] = True
        save()

    if "org_id" not in progress:
        progress["org_id"] = call(c, "GET", "/management/v1/orgs/me")["org"]["id"]
        save()

    if "project_id" not in progress:
        say("Projet Surfwear Pro")
        progress["project_id"] = call(c, "POST", "/management/v1/projects",
                                      {"name": "Surfwear Pro", "projectRoleAssertion": True})["id"]
        save()
    pid = progress["project_id"]

    if "role" not in progress:
        call(c, "POST", f"/management/v1/projects/{pid}/roles",
             {"roleKey": "staff", "displayName": "Équipe Surfwear"}, ok_conflict=True)
        progress["role"] = True
        save()

    if "client_id" not in progress:
        say("Application OIDC du portail")
        app = call(c, "POST", f"/management/v1/projects/{pid}/apps/oidc", {
            "name": "Portail Surfwear Pro",
            "redirectUris": [f"{u}/api/auth/callback" for u in PUBLIC_URLS],
            "postLogoutRedirectUris": [f"{u}/" for u in PUBLIC_URLS],
            "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
            "grantTypes": ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"],
            "appType": "OIDC_APP_TYPE_WEB",
            "authMethodType": "OIDC_AUTH_METHOD_TYPE_BASIC",
            "accessTokenType": "OIDC_TOKEN_TYPE_BEARER",
            "idTokenRoleAssertion": True,
            "idTokenUserinfoAssertion": True,
            "devMode": True,
        })
        progress.update(client_id=app["clientId"], client_secret=app["clientSecret"])
        save()

    if "staff_user" not in progress:
        say(f"Compte de l'équipe Surfwear : {STAFF_EMAIL}")
        user = call(c, "POST", "/management/v1/users/human/_import", {
            "userName": STAFF_EMAIL,
            "profile": {"firstName": "Équipe", "lastName": "Surfwear", "preferredLanguage": "fr"},
            "email": {"email": STAFF_EMAIL, "isEmailVerified": True},
            "password": STAFF_PASSWORD,
            "passwordChangeRequired": False,
        }, org=progress["org_id"])
        progress["staff_user"] = user["userId"]
        save()

    if "staff_grant" not in progress:
        call(c, "POST", f"/management/v1/users/{progress['staff_user']}/grants",
             {"projectId": pid, "roleKeys": ["staff"]}, org=progress["org_id"], ok_conflict=True)
        progress["staff_grant"] = True
        save()

    demo = json.loads(DEMO_FILE.read_text(encoding="utf-8"))["shops"] if DEMO_FILE.exists() else []
    orgs = progress.setdefault("demo_orgs", {})
    for shop in demo:
        if shop["shop_id"] in orgs:
            continue
        say(f"Organisation de démonstration : {shop['name']}")
        res = call(c, "POST", "/admin/v1/orgs/_setup", {
            "org": {"name": shop["name"]},
            "human": {"userName": shop["email"],
                      "profile": {"firstName": shop["first_name"], "lastName": shop["last_name"],
                                  "preferredLanguage": "fr"},
                      "email": {"email": shop["email"], "isEmailVerified": True},
                      "password": shop["password"]},
            "roles": ["ORG_OWNER"],
        })
        orgs[shop["shop_id"]] = res["orgId"]
        save()

    final.write_text(json.dumps({
        "client_id": progress["client_id"], "client_secret": progress["client_secret"],
        "project_id": pid, "staff_org_id": progress["org_id"], "demo_orgs": orgs,
    }, indent=2))
    say("Configuration terminée.")


if __name__ == "__main__":
    main()
