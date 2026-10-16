"""Serveur MCP de Surfwear Pro : les microservices exposés à des assistants IA.

Place dans l'architecture :  assistant IA → API Gateway (Kong) → serveur MCP → microservices.

* La passerelle authentifie la requête (jeton personnel, clé d'API de boutique ou session)
  et transmet l'identité vérifiée dans les en-têtes X-Auth-* ; le serveur MCP n'est jamais
  joignable directement depuis l'extérieur.
* Chaque outil appelle le microservice concerné sur le réseau interne en relayant cette
  identité : les règles d'accès restent celles des services (une boutique ne voit que ses
  commandes, l'entrepôt est réservé à l'équipe Surfwear).
* Transport « Streamable HTTP » sans état, réponses JSON : chaque appel est une requête
  HTTP indépendante, ce qui passe sans difficulté par une API Gateway.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from urllib.parse import unquote

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import JSONResponse

log = logging.getLogger("mcp-server")

SERVICES = {
    "catalog": ("CATALOG_URL", "http://catalog-service:8000"),
    "stock": ("STOCK_URL", "http://stock-service:8000"),
    "cart": ("CART_URL", "http://cart-service:8000"),
    "orders": ("ORDERS_URL", "http://order-service:8000"),
    "billing": ("BILLING_URL", "http://billing-service:8000"),
    "warehouse": ("WAREHOUSE_URL", "http://warehouse-service:8000"),
    "identity": ("IDENTITY_URL", "http://identity-service:8000"),
}
AUTH = ("x-auth-user", "x-auth-role", "x-auth-shop", "x-auth-name")
# Les routes d'administration du service Identité vérifient elles-mêmes les identifiants
CREDENTIALS = ("authorization", "x-api-key", "cookie")

INSTRUCTIONS = """\
Surfwear Pro : plateforme B2B d'un fabricant de surfwear et de ses boutiques revendeuses.
Une commande passe par une pré-réservation (statut « pending ») qui bloque le stock pendant
un délai limité : elle doit être finalisée (finaliser_commande) avant expiration, sinon le
stock est libéré pour les autres boutiques. Les prix sont hors taxes, en euros.
Outils boutique : catalogue, disponibilité, panier, commandes, factures.
Outils équipe Surfwear : tableau de bord, toutes les commandes, boutiques, entrepôt.
"""


def create_app(clients: dict[str, httpx.AsyncClient] | None = None) -> Starlette:
    http = clients or {name: httpx.AsyncClient(base_url=os.environ.get(var, default), timeout=10)
                       for name, (var, default) in SERVICES.items()}
    mcp = FastMCP("Surfwear Pro", instructions=INSTRUCTIONS, host="0.0.0.0",
                  stateless_http=True, json_response=True, streamable_http_path="/mcp")

    # ------------------------------------------------------------ identité et appels
    def identity(ctx: Context) -> dict:
        request = ctx.request_context.request
        h = request.headers if request is not None else {}
        role = h.get("x-auth-role")
        if not h.get("x-auth-user") or role not in ("shop", "staff"):
            raise ToolError("Accès non authentifié : le serveur MCP s'utilise via l'API Gateway.")
        return {"user": h["x-auth-user"], "role": role, "shop": h.get("x-auth-shop") or None,
                "name": unquote(h.get("x-auth-name", "")),
                "headers": {k: v for k, v in h.items() if k in AUTH or k in CREDENTIALS}}

    def need(ctx: Context, role: str) -> dict:
        me = identity(ctx)
        if me["role"] != role:
            raise ToolError("Outil réservé aux boutiques." if role == "shop"
                            else "Outil réservé à l'équipe Surfwear.")
        return me

    async def call(me: dict, service: str, method: str, path: str, **kw):
        try:
            r = await http[service].request(method, path, headers=me["headers"], **kw)
        except httpx.HTTPError as exc:
            log.warning("%s injoignable : %s", service, exc)
            raise ToolError(f"Service {service} indisponible, réessayez dans un instant.")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            if isinstance(detail, list):  # erreurs de validation FastAPI
                detail = "; ".join(d.get("msg", str(d)) for d in detail)
            raise ToolError(f"{detail} (HTTP {r.status_code})")
        return r.json() if r.content else {}

    def order_summary(o: dict) -> dict:
        keep = ("order_id", "state", "status", "status_label", "created_at", "expires_at", "seconds_left",
                "total_amount", "lines", "shop", "placed_by", "finalized_at")
        return {k: o[k] for k in keep if k in o}

    # ------------------------------------------------------------ commun
    @mcp.tool(title="Qui suis-je ?")
    async def qui_suis_je(ctx: Context) -> dict:
        """Identité vue par la plateforme : rôle (boutique ou équipe Surfwear) et boutique rattachée."""
        me = identity(ctx)
        return {k: me[k] for k in ("user", "role", "shop", "name")}

    @mcp.tool(title="Rechercher dans le catalogue")
    async def rechercher_produits(ctx: Context, recherche: str | None = None,
                                  categorie: str | None = None) -> list[dict]:
        """Produits actifs avec prix HT et disponibilité (stock immédiat et lots à venir).
        `recherche` filtre sur le nom, la référence (SKU) ou la description ; `categorie` sur la catégorie exacte."""
        me = identity(ctx)
        params = {k: v for k, v in (("q", recherche), ("category", categorie)) if v}
        return await call(me, "catalog", "GET", "/api/catalog", params=params)

    @mcp.tool(title="Fiche produit")
    async def fiche_produit(ctx: Context, sku: str) -> dict:
        """Fiche complète d'un produit à partir de sa référence (SKU)."""
        return await call(identity(ctx), "catalog", "GET", f"/api/catalog/{sku}")

    @mcp.tool(title="Vérifier une disponibilité")
    async def verifier_disponibilite(ctx: Context, sku: str, quantite: int = 1) -> dict:
        """Indique si une quantité est disponible tout de suite, et sinon à quelle date (lots en production)."""
        return await call(identity(ctx), "stock", "GET", f"/api/availability/{sku}", params={"qty": quantite})

    @mcp.tool(title="Factures")
    async def factures(ctx: Context, impayees_seulement: bool = False) -> list[dict]:
        """Factures émises : celles de la boutique connectée, ou toutes pour l'équipe Surfwear."""
        rows = await call(identity(ctx), "billing", "GET", "/api/invoices")
        if impayees_seulement:
            rows = [i for i in rows if i.get("status") != "paid"]
        return [{k: i.get(k) for k in ("id", "order_id", "shop", "total_ht", "total_ttc", "issued_at",
                                        "due_date", "status", "overdue")} for i in rows]

    # ------------------------------------------------------------ boutiques
    @mcp.tool(title="Voir le panier")
    async def voir_panier(ctx: Context) -> dict:
        """Contenu du panier de la boutique, avec prix et disponibilité à jour."""
        return await call(need(ctx, "shop"), "cart", "GET", "/api/cart")

    @mcp.tool(title="Modifier le panier")
    async def modifier_panier(ctx: Context, sku: str, quantite: int) -> dict:
        """Fixe la quantité d'une référence dans le panier (0 la retire)."""
        me = need(ctx, "shop")
        await call(me, "cart", "PUT", f"/api/cart/items/{sku}", json={"qty": quantite})
        return await call(me, "cart", "GET", "/api/cart")

    @mcp.tool(title="Vider le panier")
    async def vider_panier(ctx: Context) -> dict:
        """Retire tous les articles du panier."""
        await call(need(ctx, "shop"), "cart", "DELETE", "/api/cart")
        return {"panier": "vide"}

    @mcp.tool(title="Commander le panier")
    async def commander_panier(ctx: Context) -> dict:
        """Transforme le panier en commande PRÉ-RÉSERVÉE : le stock est bloqué pendant un délai limité.
        Il faut ensuite appeler finaliser_commande avant l'expiration (voir `expires_at`)."""
        order = await call(need(ctx, "shop"), "cart", "POST", "/api/cart/checkout")
        return order_summary(order)

    @mcp.tool(title="Mes commandes")
    async def mes_commandes(ctx: Context, etat: str | None = None) -> list[dict]:
        """Commandes de la boutique, les plus récentes d'abord. `etat` : pending (pré-réservée),
        confirmed (finalisée), cancelled ou expired. Montants HT en euros (`total_amount`)."""
        rows = await call(need(ctx, "shop"), "orders", "GET", "/api/orders")
        return [order_summary(o) for o in rows if not etat or o.get("state") == etat]

    @mcp.tool(title="Détail d'une commande")
    async def detail_commande(ctx: Context, commande_id: str) -> dict:
        """Détail d'une commande : lignes, quantités réservées, expédiées, reliquats et dates prévues."""
        return await call(need(ctx, "shop"), "orders", "GET", f"/api/orders/{commande_id}")

    @mcp.tool(title="Finaliser une commande")
    async def finaliser_commande(ctx: Context, commande_id: str) -> dict:
        """Confirme une commande pré-réservée : la réservation devient définitive et part en préparation."""
        return order_summary(await call(need(ctx, "shop"), "orders", "POST", f"/api/orders/{commande_id}/confirm"))

    @mcp.tool(title="Annuler une pré-réservation")
    async def annuler_commande(ctx: Context, commande_id: str) -> dict:
        """Annule une commande encore pré-réservée et libère le stock pour les autres boutiques."""
        return order_summary(await call(need(ctx, "shop"), "orders", "POST", f"/api/orders/{commande_id}/cancel"))

    # ------------------------------------------------------------ équipe Surfwear
    @mcp.tool(title="Tableau de bord Surfwear")
    async def tableau_de_bord(ctx: Context, seuil_stock_bas: int = 10) -> dict:
        """Synthèse pour le pilotage : commandes par état, préparations en attente, boutiques à valider,
        références sous le seuil de stock et factures impayées."""
        me = need(ctx, "staff")
        orders = await call(me, "orders", "GET", "/api/admin/orders", params={"limit": 1000})
        shops = await call(me, "identity", "GET", "/api/admin/shops")
        stock = await call(me, "warehouse", "GET", "/api/warehouse/stock")
        pickings = await call(me, "warehouse", "GET", "/api/warehouse/pickings")
        invoices = await call(me, "billing", "GET", "/api/invoices")
        unpaid = [i for i in invoices if i.get("status") != "paid"]
        return {
            "commandes_par_etat": dict(Counter(o.get("status") for o in orders)),
            "preparations_en_attente": len(pickings) if isinstance(pickings, list) else pickings,
            "boutiques_a_valider": [s["name"] for s in shops if s.get("status") == "pending"],
            "stock_bas": [{"sku": p["sku"], "nom": p["name"], "en_stock": p["on_hand"]}
                          for p in stock.get("products", []) if p["on_hand"] < seuil_stock_bas],
            "lots_en_production": len(stock.get("lots", [])),
            "factures_impayees": {"nombre": len(unpaid),
                                  "montant_ttc": round(sum(i.get("total_ttc", 0) for i in unpaid), 2),
                                  "en_retard": sum(1 for i in unpaid if i.get("overdue"))},
        }

    @mcp.tool(title="Toutes les commandes")
    async def toutes_les_commandes(ctx: Context, etat: str | None = None, boutique: str | None = None,
                                   limite: int = 100) -> list[dict]:
        """Commandes de toutes les boutiques. Filtres : `etat` (pending, confirmed, cancelled, expired)
        et `boutique` (identifiant ou partie du nom)."""
        rows = await call(need(ctx, "staff"), "orders", "GET", "/api/admin/orders", params={"limit": min(limite, 1000)})
        out = []
        for o in rows:
            shop = o.get("shop") or {}
            if etat and o.get("state") != etat:
                continue
            if boutique and boutique.lower() not in f"{shop.get('id', '')} {shop.get('name', '')}".lower():
                continue
            out.append(order_summary(o))
        return out

    @mcp.tool(title="Boutiques clientes")
    async def boutiques(ctx: Context, statut: str | None = None) -> list[dict]:
        """Boutiques inscrites. `statut` : pending (en attente de validation), active, rejected ou suspended."""
        rows = await call(need(ctx, "staff"), "identity", "GET", "/api/admin/shops")
        return [s for s in rows if not statut or s.get("status") == statut]

    @mcp.tool(title="Valider une boutique")
    async def valider_boutique(ctx: Context, boutique_id: str, connecteur: str = "none") -> dict:
        """Active le compte d'une boutique en attente. `connecteur` : logiciel de caisse relié
        (rest = éditeur A, csv = éditeur B, none = portail uniquement)."""
        return await call(need(ctx, "staff"), "identity", "POST", f"/api/admin/shops/{boutique_id}/approve",
                          json={"connector": connecteur})

    @mcp.tool(title="Stock de l'entrepôt")
    async def stock_entrepot(ctx: Context, recherche: str | None = None) -> dict:
        """Stock physique par référence et lots en production (quantité, date prévue)."""
        data = await call(need(ctx, "staff"), "warehouse", "GET", "/api/warehouse/stock")
        if recherche:
            r = recherche.lower()
            data["products"] = [p for p in data["products"] if r in f"{p['sku']} {p['name']} {p['category']}".lower()]
            skus = {p["sku"] for p in data["products"]}
            data["lots"] = [l for l in data["lots"] if l["sku"] in skus]
        return data

    @mcp.tool(title="Préparations en attente")
    async def preparations(ctx: Context) -> list[dict]:
        """Colis à préparer, regroupés par commande."""
        return await call(need(ctx, "staff"), "warehouse", "GET", "/api/warehouse/pickings")

    @mcp.tool(title="Expédier les préparations")
    async def expedier(ctx: Context, commandes: list[str] | None = None) -> dict:
        """Expédie les colis préparés (toutes les commandes, ou seulement celles listées) ;
        les bons de livraison sont émis et les factures suivent automatiquement."""
        body = {"order_ids": commandes} if commandes else None
        return await call(need(ctx, "staff"), "warehouse", "POST", "/api/warehouse/pickings/dispatch", json=body)

    @mcp.tool(title="Enregistrer une réception")
    async def enregistrer_reception(ctx: Context, lot_id: str | None = None, sku: str | None = None,
                                    quantite: int | None = None, bon_livraison: str = "") -> dict:
        """Entre de la marchandise en stock : soit un lot annoncé (`lot_id`, quantité facultative),
        soit une réception libre (`sku` + `quantite`). Les reliquats des boutiques sont servis automatiquement."""
        body = {"lot_id": lot_id, "sku": sku, "qty": quantite}
        body = {k: v for k, v in body.items() if v is not None} | {"reference": bon_livraison}
        return await call(need(ctx, "staff"), "warehouse", "POST", "/api/warehouse/receptions", json=body)

    # ------------------------------------------------------------ application ASGI
    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_request):
        return JSONResponse({"status": "ok", "service": "mcp-server", "tools": len(mcp._tool_manager.list_tools())})

    app = mcp.streamable_http_app()   # route /mcp ; son lifespan démarre le gestionnaire de sessions
    app.state.mcp = mcp
    return app
