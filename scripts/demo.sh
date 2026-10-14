#!/usr/bin/env bash
# Scénario de démonstration, à lancer une fois l'écosystème démarré
# (docker compose up, ou python scripts/dev_server.py).
#   ./scripts/demo.sh [url]          url par défaut : http://localhost:8080
set -euo pipefail
BASE="${1:-http://localhost:8080}"
J='Content-Type: application/json'
step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
pp()   { python3 -m json.tool --no-ensure-ascii; }

step "1. Disponibilité de 12 lycras (stock réel 5, lot de 30 attendu)"
curl -sf "$BASE/api/availability/SW-LYC-004?qty=12" | pp

step "2. Biarritz (éditeur A) commande 12 lycras + 3 t-shirts"
ORDER1=$(curl -sf -X POST "$BASE/api/orders" -H "$J" -H 'X-API-Key: demo-key-biarritz' \
  -H 'Idempotency-Key: demo-1' -d '{"lines":[{"sku":"SW-LYC-004","qty":12},{"sku":"SW-TSH-001","qty":3}]}')
echo "$ORDER1" | pp
ID1=$(echo "$ORDER1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["order_id"])')

step "3. La même requête rejouée ne crée pas de doublon (même numéro attendu : $ID1)"
curl -sf -X POST "$BASE/api/orders" -H "$J" -H 'X-API-Key: demo-key-biarritz' \
  -H 'Idempotency-Key: demo-1' -d '{"lines":[{"sku":"SW-LYC-004","qty":12},{"sku":"SW-TSH-001","qty":3}]}' \
  | python3 -c 'import sys,json;print("Numéro renvoyé :", json.load(sys.stdin)["order_id"])'

step "4. Lacanau (éditeur B, fichiers CSV) commande 2 ponchos en rupture"
curl -sf -X POST "$BASE/api/orders" -H "$J" -H 'X-API-Key: demo-key-lacanau' \
  -d '{"lines":[{"sku":"SW-PON-005","qty":2}]}' | python3 -c \
  'import sys,json;o=json.load(sys.stdin);print(o["order_id"], o["status_label"], o["lines"][0]["scheduled"])'
sleep 1

step "5. Liste de préparation de l'entrepôt (uniquement le stock réservé)"
curl -sf "$BASE/erp/pickings" | pp

step "6. Réception du lot LOT-0001 : le reliquat de Biarritz devient réservé"
curl -sf -X POST "$BASE/erp/receptions" -H "$J" -d '{"lot_id":"LOT-0001"}' | pp
sleep 1

step "7. Expédition de tous les colis : bons de livraison publiés sur le bus"
curl -sf -X POST "$BASE/erp/pickings/dispatch" -H "$J" -d '{}' | pp
sleep 1

step "8. Ce que les boutiques ont reçu"
echo "-- Logiciel de l'éditeur A (Biarritz), stock mis à jour par le bon de livraison :"
curl -sf "$BASE/boutiques/vendor-a/SHOP-BIA/stock" | pp
echo "-- Logiciel de l'éditeur B (Lacanau), fichiers CSV importés :"
curl -sf "$BASE/boutiques/vendor-b/SHOP-LAC/files" | python3 -c \
  'import sys,json;[print(" ",f["file"]) for f in json.load(sys.stdin)]'
echo "-- Notifications envoyées :"
curl -sf "$BASE/svc/notifications/notifications" | python3 -c \
  'import sys,json;[print(" ",n["to"],"|",n["subject"]) for n in json.load(sys.stdin)]'

step "Terminé. Portail : $BASE  ·  Mails : http://localhost:8025  ·  RabbitMQ : http://localhost:15672"
