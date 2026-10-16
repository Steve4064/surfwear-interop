# Surfwear Pro : écosystème B2B en microservices

Prototype du **projet fil rouge « Interopérabilité des SI »** (UPPA, 2026-2027), question 7.

Une entreprise de surfwear fournit une cinquantaine de boutiques en Aquitaine. Aujourd'hui, les commandes passent par téléphone puis par des fiches envoyées par mail, et le stock n'est décrémenté qu'à la préparation du colis : une même pièce peut être promise à plusieurs boutiques.

Surfwear Pro remplace ce circuit :

- **comptes B2B gérés par ZITADEL** : chaque boutique est une organisation ZITADEL, inscrite en ligne puis validée par Surfwear ;
- **catalogue, panier, commandes et factures en libre-service** pour les boutiques, 24h/24 ;
- **pré-réservation** : valider le panier bloque le stock 15 minutes ; la boutique finalise, sinon le stock retourne aux autres boutiques ;
- **réservation atomique** : la double réservation est impossible ;
- **facture PDF émise automatiquement à chaque expédition** ;
- **espace entrepôt** : préparation, expédition, réceptions, lots, inventaire, ajout de produits ;
- **intégration des logiciels de boutique existants** : webhook JSON (éditeur A) ou fichier CSV (éditeur B) ;
- tous les échanges passent par une **API REST** (OpenAPI 3) ou un **bus d'événements RabbitMQ**.

---

## 1. Architecture

```
 Navigateur (portail)     Logiciels des boutiques (clé d'API)     Assistant IA (client MCP)
        │                          │                                   │ jeton personnel
        ▼                          ▼                                   ▼
 ┌──────────────────────────── API Gateway Kong :8080 ─────────────────────────────┐
 │ routes déclaratives (gateway/kong.yml) · contrôle d'accès via Identité → X-Auth-* │
 │ limitation de débit · X-Request-Id · en-têtes de sécurité · /internal jamais exposé│
 └──┬──────────┬─────────┬─────────┬──────────┬──────────┬─────────┬──────────┬─────┘
    ▼          ▼         ▼         ▼          ▼          ▼         ▼          ▼
 Portail   Identité  Catalogue  Panier  Commandes  Facturation  Entrepôt  Serveur MCP
 (statique)  │  ▲        │ REST    │ REST    │ REST     ▲  PDF      │        │ outils
             │  │        └─► Stock └─► Cmd   └─► Stock  │           │        └─► Catalogue, Stock,
             │  │ OIDC                                  │           │            Panier, Commandes,
             ▼  │                                       │           │            Facturation,
 ZITADEL :8081 (API + page de connexion)                │           │            Entrepôt, Identité
 ══════════════════════════ Bus RabbitMQ « surfwear.events » ══════════════════════════
     │ order.* · allocation.updated · shipment.dispatched · invoice.issued
     │ stock.* · product.* · shop.registered / shop.approved
     ▼                  ▼                       ▼
 Notifications    Connecteur A (JSON)     Connecteur B (CSV)
   → Mailpit        → logiciel éditeur A    → logiciel éditeur B
```

| Microservice | Responsabilité | Données |
| --- | --- | --- |
| `identity-service` | Connexion OIDC avec ZITADEL, inscription B2B, validation des boutiques, clés d'API, contrôle d'accès pour l'API Gateway, jetons personnels | PostgreSQL `identity` |
| `catalog-service` | Référentiel produits (ajout, modification, masquage), catalogue des boutiques enrichi de la disponibilité | PostgreSQL `catalog` |
| `cart-service` | Panier de chaque utilisateur, conservé côté serveur ; validation = pré-réservation | PostgreSQL `cart` |
| `order-service` | Pré-réservation, finalisation, expiration, annulation, suivi | PostgreSQL `orders` |
| `billing-service` | Facture à chaque expédition (numérotation sans trou, TVA, échéance 30 jours), PDF, règlement | PostgreSQL `billing` |
| `stock-service` | Disponibilité, allocations (stock, lots, reliquats), dates de livraison | PostgreSQL `stock` |
| `warehouse-service` | ERP simulé : préparation, expédition, bons de livraison, réceptions, lots, inventaire | SQLite (volume) |
| `notification-service` | Mails : commande, reliquat, expédition, facture, inscription, validation | — |
| `shop-connector-rest` / `-csv` | Adaptateurs des éditeurs A (webhooks signés) et B (CSV) | volume `exchange` |
| `shop-simulator` | Logiciels des boutiques simulés (démonstration) | mémoire |
| `mcp-server` | Serveur MCP : expose les microservices à des assistants IA sous forme d'outils (21 outils boutique et Surfwear) | — |
| `gateway` | API Gateway Kong 3.9 en mode déclaratif : routage, contrôle d'accès, limitation de débit | `gateway/kong.yml` |
| `portal` | Portail web (fichiers statiques) | — |
| ZITADEL v4.16 | Fournisseur d'identité : `zitadel-api`, `zitadel-login`, `zitadel-proxy` (Traefik), `zitadel-bootstrap` | PostgreSQL `zitadel` |
| Infrastructure | RabbitMQ, PostgreSQL 16, Mailpit | volumes |

**Une base par service.** Les services ne lisent jamais la base d'un autre service : ils passent par une API interne (`/internal/...`, jamais exposée par l'API Gateway) ou par un événement.

**API Gateway.** Kong est le seul point d'entrée : sa configuration (`gateway/kong.yml`, sans base de données) déclare un service par microservice et les routes publiques de chacun. Un plugin `pre-function` (quelques lignes de Lua) vérifie chaque route étiquetée `auth-any` ou `auth-staff` auprès du service Identité (`/api/auth/check`). Celui-ci renvoie l'identité vérifiée (utilisateur, rôle, boutique), que Kong transmet au service dans les en-têtes `X-Auth-*`. Ces en-têtes sont toujours effacés s'ils viennent du client, et les services ne sont pas joignables hors du réseau Docker. Kong limite aussi le débit (connexion, inscription, MCP), ajoute un identifiant de requête `X-Request-Id` et des en-têtes de sécurité. La configuration est validée à la construction de l'image (`kong config parse`).

**Serveur MCP.** Le *Model Context Protocol* permet à un assistant IA (Claude, par exemple) d'utiliser des outils. Le serveur MCP est placé **derrière** l'API Gateway : assistant → Kong (`/mcp`, authentification) → serveur MCP → microservices. Il ne détient aucun droit propre : chaque outil appelle le microservice concerné en relayant l'identité vérifiée par Kong, donc les règles d'accès restent celles des services. Transport *Streamable HTTP* sans état, réponses JSON.

---

## 2. Démarrage

Prérequis : Docker Desktop (4 Go de RAM alloués conseillés) et les ports **8080** (API Gateway : portail, API, MCP), **8081** (ZITADEL), **8025** (mails) et **15672** (RabbitMQ) libres.

```bash
cp .env.example .env            # puis changez les secrets
docker compose up -d --build    # premier démarrage : 3 à 5 minutes (ZITADEL s'initialise)
docker compose ps               # tout doit être « healthy », zitadel-bootstrap « exited (0) »
```

| Quoi | Adresse |
| --- | --- |
| Portail Surfwear Pro | <http://localhost:8080> |
| Connexion ZITADEL (redirection automatique) | <http://localhost:8081> |
| Console d'administration ZITADEL | <http://localhost:8081/ui/console> (`zitadel-admin` / `Password1!`) |
| Documentation des API | `http://localhost:8080/svc/<service>/docs` (`identity`, `catalog`, `cart`, `orders`, `billing`, `stock`, `warehouse`) |
| Serveur MCP (assistants IA) | `http://localhost:8080/mcp` (jeton créé dans le portail, menu « Assistant IA ») |
| Mails envoyés | <http://localhost:8025> |
| Console RabbitMQ | <http://localhost:15672> (`surfwear` / `surfwear`) |

Repartir de zéro : `docker compose down -v && docker compose up -d --build`.

**Sur macOS, sans terminal** : double-cliquer dans le Finder sur
`scripts/mac/lancer-surfwear.command` (crée `.env` avec des ports libres et des secrets
aléatoires, ouvre Docker Desktop si besoin, démarre sans effacer les données), puis, pour
une adresse publique, sur `scripts/mac/tunnel-surfwear.command` (Cloudflare, ou Pinggy par
le port 443 si le réseau bloque Cloudflare). Journaux dans `logs/`.

### Comptes de démonstration

| E-mail | Mot de passe | Rôle |
| --- | --- | --- |
| `equipe@surfwear.example` | `Surfwear2026!` | Équipe Surfwear (rôle ZITADEL `staff`) |
| `biarritz@boutiques.example` | `Biarritz2026!` | Surf Shack Biarritz (éditeur A) |
| `hossegor@boutiques.example` | `Hossegor2026!` | Hossegor Board Store (éditeur A) |
| `lacanau@boutiques.example` | `Lacanau2026!` | Lacanau Glisse (éditeur B) |
| `arcachon@boutiques.example` | `Arcachon2026!` | Arcachon Wave (éditeur B) |

Ils sont créés dans ZITADEL au premier démarrage par `zitadel-bootstrap` (voir `config/zitadel-demo.json`). Changez-les avant toute mise en ligne.

---

## 3. Parcours

**Une nouvelle boutique**
1. Sur le portail, cliquez sur **Créer un compte boutique** et renseignez la raison sociale, le SIRET, l'adresse, le responsable et un mot de passe.
2. Le service Identité crée l'organisation de la boutique et son responsable dans ZITADEL (`/admin/v1/orgs/_setup`). La boutique est enregistrée « en attente », et l'équipe Surfwear reçoit un mail.
3. La boutique peut se connecter, mais ne voit qu'un écran « en attente de validation ».
4. Surfwear ouvre **Boutiques**, choisit le logiciel de la boutique (éditeur A, éditeur B ou portail seul) et clique sur **Valider**. La boutique reçoit un mail et une clé d'API pour son logiciel.
5. Le responsable peut ajouter ses employés à son organisation depuis la console ZITADEL. Ils sont rattachés automatiquement à la boutique.

**Une commande**
1. **Catalogue** : la quantité saisie (au clavier) va directement dans le panier. L'aperçu indique ce qui part tout de suite et ce qui arrivera plus tard.
2. **Panier** : il montre le total HT, la TVA et le TTC. **Pré-réserver** bloque le stock 15 minutes, avec un compte à rebours.
3. **Finaliser la commande** : elle part à l'entrepôt et le logiciel de la boutique la reçoit.
4. L'entrepôt expédie : le bon de livraison est émis, le stock de la boutique est mis à jour et la **facture PDF** apparaît dans **Factures**.

**L'entrepôt (équipe Surfwear)**
- **Préparation** : expédier un colis ou tous les colis.
- **Réceptions** :
  - réceptionner un lot annoncé, ou une marchandise reçue sans lot annoncé, avec le n° de BL du fournisseur ;
  - annoncer un lot en production ;
  - faire un inventaire.
- **Produits** : ajouter, modifier ou masquer un produit. Un nouveau produit est créé à stock zéro dans le Stock et l'Entrepôt, via un événement.
- **Factures** : suivre les échéances et marquer une facture comme réglée.

Le scénario complet se rejoue aussi en ligne de commande : `./scripts/demo.sh`.

---

## 4. Principales routes (via l'API Gateway)

| Route | Service | Accès |
| --- | --- | --- |
| `GET /api/auth/login` · `/api/auth/callback` · `POST /api/auth/logout` · `GET /api/auth/me` | Identité | public |
| `POST /api/register` | Identité | public (limité à 10 essais par minute) |
| `GET /api/admin/shops` · `POST /api/admin/shops/{id}/approve` · `reject` · `suspend` | Identité | Surfwear |
| `GET /api/catalog` · `GET /api/catalog/{sku}` | Catalogue | boutique active, Surfwear |
| `GET/POST /api/products` · `PATCH /api/products/{sku}` | Catalogue | Surfwear |
| `GET /api/cart` · `PUT /api/cart/items/{sku}` · `DELETE /api/cart` · `POST /api/cart/checkout` | Panier | boutique active |
| `POST /api/orders` · `POST /api/orders/{id}/confirm` · `cancel` · `GET /api/orders` | Commandes | boutique active |
| `GET /api/admin/orders` | Commandes | Surfwear |
| `GET /api/invoices` · `GET /api/invoices/{id}/pdf` · `POST /api/invoices/{id}/paid` | Facturation | boutique (les siennes) ; Surfwear (toutes, règlement) |
| `GET /api/warehouse/pickings` · `POST …/pickings/dispatch` · `…/receptions` · `…/lots` · `…/adjustments` · `GET …/shipments` | Entrepôt | Surfwear |
| `POST /api/auth/token` | Identité | utilisateur connecté (crée un jeton personnel de 30 jours) |
| `POST /mcp` | Serveur MCP | boutique active, Surfwear (jeton personnel ou clé d'API) |

Les logiciels des boutiques s'authentifient par l'en-tête `X-API-Key` (clé délivrée à la validation). Les scripts de Surfwear peuvent utiliser `STAFF_API_KEY`, désactivée si elle est vide.

---

## 5. Assistant IA (serveur MCP)

1. Dans le portail, ouvrir **Assistant IA** et cliquer sur **Créer un jeton** (valable 30 jours, avec les droits de l'utilisateur).
2. Ajouter le serveur à l'assistant. Le portail affiche les commandes prêtes à copier, par exemple pour Claude Code :

```bash
claude mcp add --transport http surfwear-pro http://localhost:8080/mcp --header "Authorization: Bearer <jeton>"
```

Un logiciel de boutique peut aussi utiliser sa clé d'API (`X-API-Key`). Sans identifiant, Kong répond 401 avant d'atteindre le serveur MCP.

| Pour | Outils |
| --- | --- |
| Tous | `qui_suis_je`, `rechercher_produits`, `fiche_produit`, `verifier_disponibilite`, `factures` |
| Boutiques | `voir_panier`, `modifier_panier`, `vider_panier`, `commander_panier` (pré-réservation), `mes_commandes`, `detail_commande`, `finaliser_commande`, `annuler_commande` |
| Équipe Surfwear | `tableau_de_bord`, `toutes_les_commandes`, `boutiques`, `valider_boutique`, `stock_entrepot`, `preparations`, `expedier`, `enregistrer_reception` |

Exemples de demandes : « Qu'est-ce qui est disponible en combinaisons 4/3 ? Mets-en 3 dans le panier et commande », « Quelles pré-réservations expirent bientôt ? », « Fais-moi le point de la journée : préparations, stock bas, impayés ».

---

## 6. Événements

| Événement | Producteur | Consommateurs |
| --- | --- | --- |
| `order.confirmed` / `order.updated` | Commandes | Entrepôt, Notifications, connecteurs |
| `allocation.updated` | Stock | Commandes, Entrepôt |
| `shipment.dispatched` | Entrepôt | Stock, Commandes, **Facturation**, Notifications, connecteurs |
| `invoice.issued` | Facturation | Notifications |
| `stock.incoming` / `stock.received` / `stock.adjusted` | Entrepôt | Stock |
| `product.created` / `product.updated` | Catalogue | Stock, Entrepôt |
| `shop.registered` / `shop.approved` | Identité | Notifications |

Les messages sont décrits dans `libs/common/surfwear_common/events.py` (et en AsyncAPI dans `docs/asyncapi.yaml` pour le cœur du flux de commande).

---

## 7. Tests et mode sans Docker

```bash
pip install -r scripts/requirements-dev.txt
python -m pytest -q             # 34 tests
python scripts/dev_server.py    # tout l'écosystème dans un processus (portail et /mcp), sur http://localhost:8080
```

Les tests lancent tous les microservices dans un même processus, derrière une passerelle locale qui lit `gateway/kong.yml` et en applique les routes et les règles d'accès (`scripts/local_gateway.py`). Le mode sans Docker remplace ZITADEL par des comptes locaux (`AUTH_PROVIDER=local`), avec les mêmes identifiants.

| Fichier | Ce qui est vérifié |
| --- | --- |
| `test_flow.py` | Catalogue, panier serveur, pré-réservation, finalisation, logiciel de boutique par clé d'API, réception, expédition, facture et PDF (accès réservé à la boutique), notifications, éditeurs A et B |
| `test_hold.py` | Blocage du stock, expiration qui sert le reliquat d'une autre boutique, annulation, commandes simultanées sans survente |
| `test_accounts_and_warehouse.py` | Inscription, attente, validation avec clé d'API, suspension ; refus des en-têtes d'identité forgés ; routes internes non exposées ; ajout de produit, réception libre, inventaire |
| `test_zitadel.py` | Flux OIDC contre un faux ZITADEL qui contrôle PKCE, code à usage unique, client, Host de l'instance ; rôle `staff`, rattachement par organisation, utilisateur sans boutique refusé, création d'organisation |
| `test_units.py` | Routage du bus, dédoublonnage, formats JSON et CSV des éditeurs |
| `test_gateway.py` | Code Lua du contrôle d'accès de Kong exécuté dans un interpréteur Lua (en-têtes effacés puis transmis, refus 401/403/502, réécriture, redirection) ; routes de `kong.yml` ; jeton personnel |
| `test_mcp.py` | Client MCP réel → passerelle → serveur MCP → services : catalogue, panier, pré-réservation et finalisation par une boutique ; tableau de bord et réception par Surfwear ; outils refusés selon le rôle ; 401 sans identifiant |

---

## 8. Configuration (`.env`)

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `GATEWAY_PORT` | `8080` | Port du portail. **À fixer avant le premier démarrage** : il est enregistré dans ZITADEL comme adresse de retour |
| `ZITADEL_PORT` | `8081` | Port de ZITADEL |
| `AUTH_PROVIDER` | `zitadel` | `local` pour se passer de ZITADEL |
| `AUTH_SECRET` | à changer | Signature des sessions du portail |
| `ZITADEL_MASTERKEY` | à changer (32 caractères) | Chiffrement des données ZITADEL, **non modifiable** après l'initialisation |
| `LOGIN_SESSION_COOKIE_SECRET` | à changer | Session de la page de connexion ZITADEL |
| `STAFF_API_KEY` | vide | Clé technique de l'équipe Surfwear (scripts) |
| `HOLD_MINUTES` | `15` | Durée de la pré-réservation |
| `API_TOKEN_DAYS` | `30` | Durée de validité des jetons personnels (API, assistant IA) |
| `POSTGRES_PASSWORD`, `RABBITMQ_*`, `WEBHOOK_SECRET` | démo | Secrets d'infrastructure |

---

## 9. Limites du prototype

| Prototype | Cible |
| --- | --- |
| ZITADEL et le portail sur `localhost` (HTTP) | Domaine public, TLS ; l'URL publique de ZITADEL doit être fixe |
| Accès par tunnel Cloudflare rapide | Impossible avec ZITADEL : son adresse publique change à chaque tunnel. Il faut un tunnel nommé sur un domaine fixe |
| Profil utilisateur lu par `userinfo` sur le réseau interne | Vérification de la signature de l'ID token (JWKS) en plus |
| ERP simulé | Connecteur vers l'ERP réel |
| Événement publié après la transaction | Patron *transactional outbox* |
| Une instance PostgreSQL pour toutes les bases | Une instance par service, répliquée |
| Paiement des factures saisi à la main | Rapprochement bancaire ou prélèvement |
| Jeton personnel signé par le service Identité, non révocable avant expiration | Autorisation OAuth du serveur MCP auprès de ZITADEL, jetons révocables |
| Kong en mode déclaratif (rechargé au redémarrage) | Kong avec base ou piloté par decK depuis la CI, plusieurs instances derrière un équilibreur |
