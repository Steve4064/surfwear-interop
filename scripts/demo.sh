#!/usr/bin/env bash
# Scénario de démonstration par l'API, à lancer une fois l'écosystème démarré.
#   ./scripts/demo.sh [url]          url par défaut : http://localhost:8080
# Les boutiques utilisent leur clé d'API (logiciel de caisse) ; l'entrepôt utilise
# la clé technique STAFF_API_KEY (lue dans .env), ou à défaut une connexion locale.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${1:-http://localhost:8080}"
J='Content-Type: application/json'
STAFF_KEY="${STAFF_API_KEY:-$(sed -n 's/^STAFF_API_KEY=//p' .env 2>/dev/null || true)}"
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT
step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
field(){ python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }
BIA=(-H 'X-API-Key: demo-key-biarritz')
HOS=(-H 'X-API-Key: demo-key-hossegor')
LAC=(-H 'X-API-Key: demo-key-lacanau')
if [ -n "$STAFF_KEY" ]; then
  STAFF=(-H "X-API-Key: $STAFF_KEY")
else  # mode sans ZITADEL : connexion par formulaire
  curl -sf -c "$JAR" -X POST "$BASE/api/auth/login" -H "$J" \
    -d '{"username":"equipe@surfwear.example","password":"Surfwear2026!"}' >/dev/null
  STAFF=(-b "$JAR")
fi

step "0. Sans session, l'entrepôt est fermé"
echo "HTTP $(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/warehouse/pickings")"

step "1. Catalogue (fiches produits + disponibilité) vu par Biarritz"
curl -sf "${BIA[@]}" "$BASE/api/catalog?category=Bain" | python3 -c \
  'import sys,json;[print("  %s  %-32s %6.2f €  dispo %s" % (p["sku"], p["name"], p["unit_price"], p["available_now"])) for p in json.load(sys.stdin)]'

step "2. Biarritz remplit son panier : 12 lycras + 3 t-shirts"
curl -sf -X PUT "${BIA[@]}" -H "$J" "$BASE/api/cart/items/SW-LYC-004" -d '{"qty":12}' >/dev/null
curl -sf -X PUT "${BIA[@]}" -H "$J" "$BASE/api/cart/items/SW-TSH-001" -d '{"qty":3}' | python3 -c \
  'import sys,json;c=json.load(sys.stdin);print("  %s articles · %s € HT · %s € TTC" % (c["count"], c["total_ht"], c["total_ttc"]))'

step "3. Validation du panier : pré-réservation (stock bloqué), puis finalisation"
ID1=$(curl -sf -X POST "${BIA[@]}" -H 'Idempotency-Key: demo-1' "$BASE/api/cart/checkout" | field '["order_id"]')
echo "  $ID1 pré-réservée ; lycras encore disponibles pour les autres : $(curl -sf "${HOS[@]}" "$BASE/api/availability/SW-LYC-004?qty=1" | field '["available_now"]')"
curl -sf -X POST "${BIA[@]}" "$BASE/api/orders/$ID1/confirm" | field '["status_label"]'

step "4. Lacanau (éditeur B, fichiers CSV) commande 2 ponchos en rupture et finalise"
curl -sf -X POST "$BASE/api/orders" -H "$J" "${LAC[@]}" -d '{"lines":[{"sku":"SW-PON-005","qty":2}],"confirm":true}' \
  | python3 -c 'import sys,json;o=json.load(sys.stdin);print(" ",o["order_id"], o["status_label"], o["lines"][0]["scheduled"])'
sleep 1

step "5. Entrepôt : réception du lot LOT-0001 (bon fournisseur BL-77), puis expédition"
curl -sf "${STAFF[@]}" -X POST "$BASE/api/warehouse/receptions" -H "$J" -d '{"lot_id":"LOT-0001","reference":"BL-77"}' >/dev/null
sleep 1
curl -sf "${STAFF[@]}" -X POST "$BASE/api/warehouse/pickings/dispatch" -H "$J" -d '{}' | python3 -c \
  'import sys,json;[print(" ",s["shipment_id"],"->",s["shop"]["name"]) for s in json.load(sys.stdin)]'
sleep 2

step "6. Facture émise automatiquement à l'expédition"
INV=$(curl -sf "${BIA[@]}" "$BASE/api/invoices" | python3 -c \
  'import sys,json;i=json.load(sys.stdin)[0];print(i["id"]);print("  %s : %s € TTC, échéance %s" % (i["id"], i["total_ttc"], i["due_date"]),file=sys.stderr)')
curl -sf "${BIA[@]}" -o "/tmp/$INV.pdf" "$BASE/api/invoices/$INV/pdf" && echo "  PDF téléchargé : /tmp/$INV.pdf ($(wc -c <"/tmp/$INV.pdf") octets)"

step "7. Surfwear ajoute un produit : il apparaît aussitôt au catalogue"
curl -s "${STAFF[@]}" -X POST "$BASE/api/products" -H "$J" \
  -d '{"sku":"SW-PAR-009","name":"Parka coupe-vent Seignosse","category":"Textile","unit_price":48}' >/dev/null || true
sleep 1
curl -sf "${BIA[@]}" "$BASE/api/catalog/SW-PAR-009" | python3 -c 'import sys,json;p=json.load(sys.stdin);print(" ",p["name"],"· dispo",p["available_now"])'

step "8. Ce que les boutiques ont reçu"
curl -sf "${STAFF[@]}" "$BASE/svc/notifications/notifications" | python3 -c \
  'import sys,json;[print(" ",n["to"],"|",n["subject"]) for n in json.load(sys.stdin)][:8]'

step "Terminé. Portail : $BASE  ·  Mails : http://localhost:8025"
