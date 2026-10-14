#!/usr/bin/env bash
# Scénario de démonstration, à lancer une fois l'écosystème démarré
# (docker compose up, ou python scripts/dev_server.py).
#   ./scripts/demo.sh [url]          url par défaut : http://localhost:8080
set -euo pipefail
BASE="${1:-http://localhost:8080}"
J='Content-Type: application/json'
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT
step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
pp()   { python3 -m json.tool --no-ensure-ascii; }
field(){ python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }
BIA=(-H 'X-API-Key: demo-key-biarritz')   # logiciel de la boutique (éditeur A)
HOS=(-H 'X-API-Key: demo-key-hossegor')
LAC=(-H 'X-API-Key: demo-key-lacanau')

step "0. Connexion de l'équipe Surfwear (accès entrepôt)"
curl -sf -c "$JAR" -X POST "$BASE/api/auth/login" -H "$J" \
  -d '{"username":"surfwear","password":"Surfwear2026!"}' | field '["display_name"]'
echo "Accès à l'entrepôt sans connexion : HTTP $(curl -s -o /dev/null -w '%{http_code}' "$BASE/erp/pickings")"

step "1. Disponibilité de 12 lycras (stock réel 5, lot de 30 attendu)"
curl -sf "${BIA[@]}" "$BASE/api/availability/SW-LYC-004?qty=12" | pp

step "2. Biarritz pré-réserve 12 lycras + 3 t-shirts : le stock lui est bloqué"
ORDER1=$(curl -sf -X POST "$BASE/api/orders" -H "$J" "${BIA[@]}" -H 'Idempotency-Key: demo-1' \
  -d '{"lines":[{"sku":"SW-LYC-004","qty":12},{"sku":"SW-TSH-001","qty":3}]}')
ID1=$(echo "$ORDER1" | field '["order_id"]')
echo "$ORDER1" | python3 -c 'import sys,json;o=json.load(sys.stdin);print(o["order_id"],"|",o["status_label"],"| expire dans",o["seconds_left"],"s")'
echo "Lycras encore disponibles pour les autres boutiques : $(curl -sf "${HOS[@]}" "$BASE/api/availability/SW-LYC-004?qty=1" | field '["available_now"]')"

step "3. Hossegor pré-réserve 2 lycras puis annule : le stock revient"
ID2=$(curl -sf -X POST "$BASE/api/orders" -H "$J" "${HOS[@]}" -d '{"lines":[{"sku":"SW-LYC-004","qty":2}]}' | field '["order_id"]')
curl -sf -X POST "$BASE/api/orders/$ID2/cancel" "${HOS[@]}" | field '["status_label"]'

step "4. Biarritz finalise : la commande part à l'entrepôt"
curl -sf -X POST "$BASE/api/orders/$ID1/confirm" "${BIA[@]}" | field '["status_label"]'

step "5. Lacanau (éditeur B, fichiers CSV) commande et finalise 2 ponchos en rupture"
curl -sf -X POST "$BASE/api/orders" -H "$J" "${LAC[@]}" \
  -d '{"lines":[{"sku":"SW-PON-005","qty":2}],"confirm":true}' | python3 -c \
  'import sys,json;o=json.load(sys.stdin);print(o["order_id"], o["status_label"], o["lines"][0]["scheduled"])'
sleep 1

step "6. Liste de préparation de l'entrepôt (commandes finalisées, stock réservé)"
curl -sf -b "$JAR" "$BASE/erp/pickings" | pp

step "7. Réception du lot LOT-0001 : le reliquat de Biarritz devient réservé"
curl -sf -b "$JAR" -X POST "$BASE/erp/receptions" -H "$J" -d '{"lot_id":"LOT-0001"}' | pp
sleep 1

step "8. Expédition de tous les colis : bons de livraison publiés sur le bus"
curl -sf -b "$JAR" -X POST "$BASE/erp/pickings/dispatch" -H "$J" -d '{}' | python3 -c \
  'import sys,json;[print(" ",s["shipment_id"],"->",s["shop"]["name"]) for s in json.load(sys.stdin)]'
sleep 1

step "9. Ce que les boutiques ont reçu"
echo "-- Logiciel de l'éditeur A (Biarritz), stock mis à jour par le bon de livraison :"
curl -sf -b "$JAR" "$BASE/boutiques/vendor-a/SHOP-BIA/stock" | pp
echo "-- Logiciel de l'éditeur B (Lacanau), fichiers CSV importés :"
curl -sf -b "$JAR" "$BASE/boutiques/vendor-b/SHOP-LAC/files" | python3 -c \
  'import sys,json;[print(" ",f["file"]) for f in json.load(sys.stdin)]'
echo "-- Notifications envoyées :"
curl -sf -b "$JAR" "$BASE/svc/notifications/notifications" | python3 -c \
  'import sys,json;[print(" ",n["to"],"|",n["subject"]) for n in json.load(sys.stdin)]'

step "Terminé. Portail : $BASE  ·  Mails : http://localhost:8025  ·  RabbitMQ : http://localhost:15672"
