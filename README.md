# Surfwear Interop : prototype d'écosystème e-commerce B2B

Prototype du **projet fil rouge « Interopérabilité des SI »** (Master 2 SIGLIS, UPPA, 2026-2027), question 7.

Une entreprise de surfwear fournit une cinquantaine de boutiques en Aquitaine. Aujourd'hui, les commandes passent par téléphone puis par des fiches envoyées par mail. Le stock n'est décrémenté qu'au moment de la préparation du colis, si bien qu'une même pièce peut être promise à plusieurs boutiques.

Ce dépôt implémente la solution TO BE décrite dans l'étude technique (question 6) :

- les boutiques **commandent en ligne 24h/24**, depuis un portail ou directement depuis leur logiciel ;
- le stock est **réservé dès la confirmation**, par une mise à jour atomique : la double réservation devient impossible ;
- chaque reliquat reçoit **une date de livraison**, calculée sur les lots en production, et il est réservé automatiquement à leur arrivée ;
- l'ERP existant est **conservé** et relié par un connecteur ;
- les logiciels des boutiques, **différents d'un éditeur à l'autre**, sont intégrés par des adaptateurs : webhook JSON pour l'éditeur A, fichier CSV pour l'éditeur B ;
- tous les échanges passent par une **API REST** (OpenAPI 3) ou un **bus d'événements RabbitMQ** (AMQP), et plus jamais par mail entre humains.

---

## Sommaire

1. [Architecture](#1-architecture)
2. [Démarrage rapide](#2-démarrage-rapide)
3. [Démonstration guidée](#3-démonstration-guidée)
4. [Interfaces exposées](#4-interfaces-exposées)
5. [Contrats d'échange](#5-contrats-déchange)
6. [Règles métier du stock](#6-règles-métier-du-stock)
7. [Intégration des deux éditeurs de logiciels boutique](#7-intégration-des-deux-éditeurs-de-logiciels-boutique)
8. [Disponibilité, scalabilité, résilience](#8-disponibilité-scalabilité-résilience)
9. [Tests et mode sans Docker](#9-tests-et-mode-sans-docker)
10. [Structure du monorepo](#10-structure-du-monorepo)
11. [Configuration](#11-configuration)
12. [Limites du prototype et passage en production](#12-limites-du-prototype-et-passage-en-production)
13. [Dépannage](#13-dépannage)

---

## 1. Architecture

```
   Portail B2B          Logiciel éditeur A           Logiciel éditeur B
   (navigateur)         (API REST + webhooks)        (import de fichiers)
        │                  │            ▲                     ▲
        │ REST             │ REST       │ webhook JSON signé  │ fichier CSV
        ▼                  ▼            │                     │
  ┌──────────────────────────────┐  ┌───┴────────────┐  ┌─────┴──────────┐
  │  Passerelle API (Nginx :8080) │  │ Connecteur A   │  │ Connecteur B   │
  └──────┬───────────────┬────────┘  │ ADAPTER=rest   │  │ ADAPTER=csv    │
         ▼               ▼           └───────▲────────┘  └───────▲────────┘
  ┌──────────────┐ REST ┌──────────────┐     │                   │
  │   Service    │─────▶│   Service    │     │                   │
  │  Commandes   │      │    Stock     │     │                   │
  └──────┬───────┘      └──────┬───────┘     │                   │
         │ order.*             │ allocation.updated              │
  ═══════▼═════════════════════▼═════════════╧═══════════════════╧═══════
                 Bus d'événements RabbitMQ · échange « surfwear.events »
  ═══════▲═════════════════════════════════════════╤═════════════════════
         │ stock.*, shipment.dispatched            │ order.*, shipment.*
  ┌──────┴───────┐      ┌──────────────┐    ┌──────▼─────────┐
  │ Connecteur   │◀────▶│ ERP existant │    │ Notifications  │──▶ SMTP (Mailpit)
  │    ERP       │      │  (simulé)    │    └────────────────┘
  └──────────────┘      └──────────────┘
```

| Service | Rôle | Technologie | Données |
| --- | --- | --- | --- |
| `gateway` | Point d'entrée unique, routage, en-têtes de sécurité, portail statique | Nginx 1.27 | — |
| `order-service` | Commandes, statuts, registre des boutiques et clés d'API | FastAPI | PostgreSQL `orders` |
| `stock-service` | Disponibilité, allocations (stock réel, lots, reliquats), dates de livraison | FastAPI | PostgreSQL `stock` |
| `erp-connector` | Pont ERP ↔ bus ; simule l'ERP : stock physique, lots, préparation, bons de livraison | FastAPI | SQLite (volume `erp-data`) |
| `shop-connector-rest` | Adaptateur éditeur A : événements → webhooks JSON signés HMAC-SHA256 | FastAPI | — |
| `shop-connector-csv` | Adaptateur éditeur B : événements → fichiers CSV (RFC 4180) | même image | volume `exchange` |
| `notification-service` | Mails de confirmation, de reliquat et d'expédition | FastAPI + SMTP | — |
| `shop-simulator` | Simule les logiciels des boutiques des deux éditeurs (démonstration uniquement) | FastAPI | mémoire + volume `exchange` |
| `rabbitmq` | Bus d'événements, files durables, file de lettres mortes | RabbitMQ 3.13 | volume `rabbit-data` |
| `postgres` | Bases des services Commandes et Stock | PostgreSQL 16 | volume `pg-data` |
| `mailpit` | Boîte mail de démonstration | Mailpit | — |

---

## 2. Démarrage rapide

### Prérequis

- Docker Engine 24 ou plus récent, avec Docker Compose v2 (`docker compose version`)
- Ports libres : **8080** (portail et API), **8025** (mails), **15672** (console RabbitMQ)
- Environ 1,5 Go de RAM disponible

### Lancer l'écosystème

```bash
git clone <url-du-depot> surfwear-interop
cd surfwear-interop
cp .env.example .env        # facultatif : les valeurs par défaut fonctionnent
docker compose up -d --build
docker compose ps           # attendre que tous les services soient « healthy » (≈ 30 à 60 s)
```

Ou, avec `make` : `make up`, puis `make ps`.

### Ouvrir

| Quoi | URL |
| --- | --- |
| Portail B2B (boutiques et entrepôt) | <http://localhost:8080> |
| Documentation API Commandes | <http://localhost:8080/svc/orders/docs> |
| Documentation API Stock | <http://localhost:8080/svc/stock/docs> |
| Documentation API ERP | <http://localhost:8080/svc/erp/docs> |
| Logiciels des boutiques simulés | <http://localhost:8080/boutiques/> |
| Mails envoyés aux boutiques | <http://localhost:8025> |
| Console RabbitMQ (`surfwear` / `surfwear`) | <http://localhost:15672> |

### Arrêter

```bash
docker compose down        # conserve les données
docker compose down -v     # efface tout, pour repartir du jeu de données initial
```

---

## 3. Démonstration guidée

### Par le portail

1. Sur <http://localhost:8080>, choisissez la boutique **Surf Shack Biarritz**. Commandez **12 lycras** (5 seulement en stock) et **3 t-shirts**.
   La commande est confirmée immédiatement : 5 lycras réservés, 7 livrables le 02/11/2026.
2. Passez sur **Hossegor Board Store** : le lycra est affiché en rupture, et le lot du 02/11 n'a plus que 23 pièces libres. Les 5 pièces de Biarritz ne peuvent plus être promises à une autre boutique.
3. Passez sur **Lacanau Glisse** (éditeur B) et commandez 2 ponchos. Le fichier CSV apparaît dans <http://localhost:8080/boutiques/>.
4. Dans l'onglet **Entrepôt**, la liste de préparation ne contient que le stock réservé. Cliquez sur **Réceptionner** pour le lot `LOT-0001` : le reliquat de Biarritz passe en « réservé ».
5. Cliquez sur **Expédier tous les colis**. Les bons de livraison partent sur le bus : le stock de la boutique de Biarritz est mis à jour dans son logiciel (éditeur A), un fichier `LIVRAISON` est déposé pour l'éditeur B, et les mails arrivent dans <http://localhost:8025>.

### Par script

```bash
./scripts/demo.sh            # ou : make demo
```

Le script rejoue le même scénario en `curl`. Il vérifie aussi qu'une requête rejouée avec la même `Idempotency-Key` ne crée pas de doublon.

### Clés d'API de démonstration

| Boutique | Éditeur | Clé (`X-API-Key`) |
| --- | --- | --- |
| Surf Shack Biarritz (`SHOP-BIA`) | A, webhooks | `demo-key-biarritz` |
| Hossegor Board Store (`SHOP-HOS`) | A, webhooks | `demo-key-hossegor` |
| Lacanau Glisse (`SHOP-LAC`) | B, fichiers CSV | `demo-key-lacanau` |
| Arcachon Wave (`SHOP-ARC`) | B, fichiers CSV | `demo-key-arcachon` |

---

## 4. Interfaces exposées

Toutes les interfaces passent par la passerelle (`http://localhost:8080`).

| Méthode et chemin | Service | Description |
| --- | --- | --- |
| `GET /api/catalog` | Stock | Catalogue, disponibilité immédiate, lots à venir |
| `GET /api/availability/{sku}?qty=n` | Stock | Plan de livraison simulé pour une quantité (rien n'est réservé) |
| `POST /api/orders` | Commandes | Passer une commande. En-têtes : `X-API-Key`, et `Idempotency-Key` (facultatif) |
| `GET /api/orders` · `GET /api/orders/{id}` | Commandes | Suivi des commandes de la boutique authentifiée |
| `GET /api/me` | Commandes | Boutique associée à la clé |
| `GET /erp/stock` | ERP | Stock physique et lots en production |
| `GET /erp/pickings` | ERP | Liste de préparation |
| `POST /erp/pickings/dispatch` | ERP | Expédier (corps `{"order_ids": [...]}`, ou `{}` pour tout expédier) |
| `POST /erp/lots` | ERP | Annoncer un lot `{"sku","qty","eta"}` |
| `POST /erp/receptions` | ERP | Réceptionner `{"lot_id"}`, ou `{"sku","qty"}` sans annonce préalable |
| `GET /svc/<service>/docs` | tous | Documentation OpenAPI interactive (`stock`, `orders`, `erp`, `notifications`, `connector-rest`, `connector-csv`) |

Exemple de commande :

```bash
curl -X POST http://localhost:8080/api/orders \
  -H 'Content-Type: application/json' -H 'X-API-Key: demo-key-biarritz' \
  -H 'Idempotency-Key: 6f1c2a' \
  -d '{"lines":[{"sku":"SW-LYC-004","qty":12}]}'
```

---

## 5. Contrats d'échange

Les événements sont publiés sur l'échange **`surfwear.events`** (type topic). La clé de routage est le type de l'événement. Ils sont décrits en AsyncAPI 3 dans [`docs/asyncapi.yaml`](docs/asyncapi.yaml) et en Pydantic dans [`libs/common/surfwear_common/events.py`](libs/common/surfwear_common/events.py).

| Événement | Producteur | Consommateurs |
| --- | --- | --- |
| `order.confirmed` | Commandes | Connecteur ERP, Notifications, connecteurs A et B |
| `allocation.updated` | Stock | Commandes, Connecteur ERP |
| `order.updated` | Commandes | Notifications, connecteurs A et B |
| `shipment.dispatched` | Connecteur ERP | Stock, Commandes, Notifications, connecteurs A et B |
| `stock.incoming` | Connecteur ERP | Stock |
| `stock.received` | Connecteur ERP | Stock |

Chaque message porte une enveloppe commune :

```json
{ "id": "3d0c…", "type": "order.confirmed", "occurred_at": "2026-10-14T13:07:03Z",
  "version": 1, "source": "order-service", "data": { … } }
```

L'identifiant `id` sert au dédoublonnage : un consommateur ignore un message déjà traité.

---

## 6. Règles métier du stock

Le Service Stock tient, pour chaque référence, le **stock réel** (copie de l'ERP) et la quantité **déjà réservée**. Il tient aussi les **lots en production** et leur date prévue. Chaque ligne de commande est découpée en allocations :

| Allocation | Signification | Ce que voit la boutique |
| --- | --- | --- |
| `stock` | Réservée sur le stock réel | Partira à la prochaine expédition |
| `lot` | Planifiée sur un lot en production | Livrable à la date du lot |
| `unscheduled` | Aucun lot identifié pour l'instant | En attente de production |

- **Ordre d'allocation** : stock réel d'abord, puis lots par date croissante, puis reliquat non planifié.
- **Anti double réservation** : chaque prise est une mise à jour conditionnelle atomique, `UPDATE products SET reserved = reserved + q WHERE on_hand - reserved >= q`. Deux commandes simultanées ne peuvent pas obtenir la même pièce ; le test `test_concurrent_orders_never_oversell` le vérifie.
- **Annonce d'un lot** (`stock.incoming`) : le lot sert d'abord les reliquats non planifiés, dans l'ordre d'arrivée des commandes.
- **Réception d'un lot** (`stock.received`) : les allocations du lot deviennent des réservations fermes, puis le stock libre restant sert les reliquats non planifiés.
- **Expédition** (`shipment.dispatched`) : les quantités sortent du stock réel et des réservations.

---

## 7. Intégration des deux éditeurs de logiciels boutique

Une seule image `shop-connector` est déployée deux fois avec un adaptateur différent (`ADAPTER=rest` ou `ADAPTER=csv`). Chaque connecteur ne traite que les boutiques de son éditeur. Ajouter un troisième éditeur revient à écrire un adaptateur dans [`services/shop-connector/shop_connector/adapters.py`](services/shop-connector/shop_connector/adapters.py).

**Éditeur A, webhook JSON** (`POST /vendor-a/webhooks/{shop_id}`, signé dans l'en-tête `X-Surfwear-Signature: sha256=<hmac>`) :

```json
{ "messageType": "ORDER_ACK", "messageId": "…", "storeCode": "SHOP-BIA",
  "purchaseOrder": "CMD-20261014-F524DA", "status": "backordered",
  "items": [{ "itemCode": "SW-LYC-004", "label": "Lycra anti-UV manches longues", "unitPrice": 24.0,
              "ordered": 12, "confirmed": 5, "unscheduled": 0,
              "backorder": [{ "quantity": 7, "expectedDate": "2026-11-02" }] }] }
```

Les types de messages sont `ORDER_ACK`, `ORDER_CHANGE` et `DELIVERY_NOTE`. Un webhook en échec est retenté 3 fois avec un délai croissant, puis le message part en file de lettres mortes.

**Éditeur B, fichier CSV** déposé dans `/exchange/{shop_id}/inbox/`. L'écriture est atomique (fichier temporaire puis renommage), au format RFC 4180 :

```csv
record_type,order_id,sku,label,qty_ordered,qty_confirmed,qty_planned,expected_date,qty_unscheduled,qty_shipped,delivery_note
COMMANDE,CMD-20261014-BEC0E7,SW-PON-005,Poncho éponge,2,0,2,2026-11-09,0,0,
LIVRAISON,CMD-20261014-BEC0E7,SW-PON-005,Poncho éponge,,,,,,2,BL-20261109-1A2B3C
```

En production, ce répertoire serait un dépôt SFTP propre à chaque boutique.

---

## 8. Disponibilité, scalabilité, résilience

**Mise à l'échelle horizontale.** Les services Commandes et Stock sont sans état ; Nginx réinterroge le DNS de Docker toutes les 10 s.

```bash
docker compose up -d --scale order-service=3 --scale stock-service=2     # ou : make scale
```

**Panne d'un consommateur.** Les messages attendent dans sa file durable :

```bash
docker compose stop shop-connector-rest
./scripts/demo.sh                       # commandes passées pendant la panne
docker compose start shop-connector-rest  # les webhooks en attente sont livrés
```

Pendant la panne, la file `shop-connector.rest` se remplit, comme le montre la console RabbitMQ.

**Messages en échec.** Ils sont routés vers la file `surfwear.dead-letters`, consultable dans la console RabbitMQ, au lieu d'être perdus ou de bloquer la file.

**Santé.** Chaque service expose `GET /health`, utilisé par les `HEALTHCHECK` Docker et par l'ordre de démarrage (`depends_on: condition: service_healthy`).

**Démarrage tolérant.** Les services réessaient leur connexion à PostgreSQL, à RabbitMQ et à l'ERP au lieu d'échouer.

---

## 9. Tests et mode sans Docker

Les tests démarrent **tous les services dans un même processus** : vrais appels REST entre services, vrais événements, mais avec un bus en mémoire et SQLite. Docker et RabbitMQ ne sont donc pas nécessaires.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r scripts/requirements-dev.txt
python -m pytest -q            # ou : make test
```

| Test | Ce qui est vérifié |
| --- | --- |
| `test_full_flow` | Scénario complet : commande avec reliquat, idempotence, liste de préparation, webhook A, fichier CSV B, annonce de lot, réception, expédition, stock des boutiques, mails |
| `test_concurrent_orders_never_oversell` | Deux commandes simultanées sur les 12 derniers articles : 12 réservés au total, jamais plus |
| `test_catalog_and_availability` | Calcul de disponibilité et de date de livraison |
| `test_security_and_validation` | Clé d'API invalide, référence inconnue, quantité nulle |
| `test_units.py` | Routage AMQP topic, dédoublonnage, formats JSON et CSV des éditeurs |

**Mode démonstration sans Docker**, utile en secours :

```bash
python scripts/dev_server.py   # ou : make dev
# Portail sur http://localhost:8080, même scénario possible avec ./scripts/demo.sh
```

---

## 10. Structure du monorepo

```
surfwear-interop/
├── docker-compose.yml          orchestration de tout l'écosystème
├── .env.example                variables personnalisables
├── Makefile                    raccourcis (up, down, demo, test, scale…)
├── config/
│   ├── catalog.json            catalogue et lots initiaux de l'ERP
│   └── shops.json              boutiques, éditeur, clé d'API
├── libs/common/                bibliothèque partagée (installée dans chaque image)
│   └── surfwear_common/
│       ├── events.py           enveloppe et schémas des événements
│       ├── bus.py              RabbitBus (AMQP) et MemoryBus (tests)
│       ├── db.py               SQLAlchemy asynchrone
│       └── service.py          configuration et journalisation
├── services/
│   ├── order-service/          Dockerfile, requirements.txt, order_service/
│   ├── stock-service/          … stock_service/ (models.py, logic.py, main.py)
│   ├── erp-connector/
│   ├── shop-connector/         adaptateurs des éditeurs A et B
│   ├── notification-service/
│   └── shop-simulator/         logiciels boutiques simulés (démonstration)
├── gateway/nginx.conf          passerelle API
├── portal/                     portail B2B (HTML/CSS/JS sans dépendance)
├── infra/postgres/init.sql     création des bases
├── docs/asyncapi.yaml          contrats des événements
├── scripts/
│   ├── demo.sh                 scénario de démonstration
│   ├── dev_server.py           mode sans Docker
│   └── requirements-dev.txt
└── tests/                      tests de bout en bout et unitaires
```

Chaque image est construite depuis la racine du dépôt, afin d'embarquer la bibliothèque commune (`docker compose build` s'en charge).

---

## 11. Configuration

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `GATEWAY_PORT` | `8080` | Port publié du portail et de l'API |
| `POSTGRES_PASSWORD` | `surfwear` | Mot de passe PostgreSQL |
| `RABBITMQ_USER` / `RABBITMQ_PASSWORD` | `surfwear` | Identifiants RabbitMQ |
| `WEBHOOK_SECRET` | `change-me` | Secret HMAC partagé avec l'éditeur A |
| `LOG_LEVEL` | `INFO` | Niveau de journalisation |

Ajouter une boutique : ajouter une ligne dans `config/shops.json`, puis `docker compose down -v && docker compose up -d`. Le registre est initialisé au premier démarrage.

---

## 12. Limites du prototype et passage en production

| Limite du prototype | Cible en production |
| --- | --- |
| ERP simulé par le connecteur | Connecteur branché sur l'API ou la base de l'ERP réel (lecture des réceptions, écriture des commandes) |
| Publication d'événement après la transaction | Patron *transactional outbox* pour garantir la publication |
| Dédoublonnage en mémoire (par instance) | Table des messages traités en base |
| Clés d'API statiques | OAuth 2.0 *client credentials*, rotation des secrets |
| Une seule instance PostgreSQL pour deux bases | Une base par service, répliquée |
| RabbitMQ seul | Cluster RabbitMQ (files *quorum*) |
| Dépôt de fichiers sur volume partagé | SFTP par boutique, ou EDIFACT (ORDERS, ORDRSP, DESADV) pour les éditeurs qui le proposent |
| Docker Compose | Kubernetes ou Docker Swarm, avec réplicas et déploiement progressif |
| Écran entrepôt sans authentification | Réseau interne et authentification des logisticiens |

---

## 13. Dépannage

| Symptôme | Piste |
| --- | --- |
| `port is already allocated` | Changer `GATEWAY_PORT` dans `.env`, ou libérer 8080, 8025 et 15672 |
| Un service reste `starting` | `docker compose logs <service>` : il attend généralement PostgreSQL ou RabbitMQ, et réessaie |
| Le portail affiche « Erreur 502 » | Les services démarrent encore ; patienter puis rafraîchir |
| Repartir des données initiales | `docker compose down -v && docker compose up -d --build` |
| Pas de mails dans Mailpit | `docker compose logs notification-service` |
