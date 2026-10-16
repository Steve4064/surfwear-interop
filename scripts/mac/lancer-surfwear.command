#!/bin/bash
# Lance (ou relance) Surfwear Pro sur macOS : double-clic depuis le Finder.
# Crée .env au premier lancement (ports libres, secrets aléatoires), puis reconstruit et
# démarre les conteneurs SANS effacer les données. Journal : logs/lancement.log
cd "$(dirname "$0")/../.." || exit 1
mkdir -p logs
exec > >(tee logs/lancement.log) 2>&1
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
echo "== Lancement $(date)"

if ! docker info >/dev/null 2>&1; then
  echo "Ouverture de Docker Desktop…"; open -a Docker
  for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 3; done
fi
docker version --format 'Docker Engine {{.Server.Version}}' || { echo "== ECHEC : Docker ne répond pas"; exit 1; }

if [ ! -f .env ]; then
  echo "== Première configuration (.env)"
  port_free() { ! lsof -nP -iTCP:$1 -sTCP:LISTEN >/dev/null 2>&1; }
  GW=8088; for p in 8080 8088 8090 8888; do port_free $p && { GW=$p; break; }; done
  ZP=8081; for p in 8081 8089 8091 8181; do port_free $p && { ZP=$p; break; }; done
  rand() { LC_ALL=C tr -dc A-Za-z0-9 </dev/urandom | head -c "$1"; }
  cp .env.example .env
  sed -i '' -e "s/^GATEWAY_PORT=.*/GATEWAY_PORT=$GW/" -e "s/^ZITADEL_PORT=.*/ZITADEL_PORT=$ZP/" \
    -e "s/^AUTH_SECRET=.*/AUTH_SECRET=$(rand 48)/" -e "s/^ZITADEL_MASTERKEY=.*/ZITADEL_MASTERKEY=$(rand 32)/" \
    -e "s/^LOGIN_SESSION_COOKIE_SECRET=.*/LOGIN_SESSION_COOKIE_SECRET=$(rand 48)/" \
    -e "s/^STAFF_API_KEY=.*/STAFF_API_KEY=sk_staff_$(rand 32)/" .env
fi
GW=$(sed -n 's/^GATEWAY_PORT=//p' .env); GW=${GW:-8080}
ZP=$(sed -n 's/^ZITADEL_PORT=//p' .env); ZP=${ZP:-8081}
BASE="http://localhost:$GW"

echo "== Construction et démarrage (premier lancement : plusieurs minutes)"
docker compose up -d --build || { docker compose ps -a; echo "== ECHEC (démarrage)"; exit 1; }

echo "== Attente du portail"
ready=0
for i in $(seq 1 120); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/auth/config")" = "200" ] && { ready=1; echo "Portail prêt après $((i*5)) s"; break; }
  sleep 5
done
docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
if [ "$ready" != 1 ]; then
  for s in zitadel-api zitadel-login zitadel-bootstrap identity-service mcp-server portal gateway; do echo "---- $s"; docker compose logs --tail=30 "$s"; done
  echo "== ECHEC (démarrage incomplet)"; exit 1
fi
open "$BASE"
echo "== TERMINE : portail $BASE · MCP $BASE/mcp · ZITADEL http://localhost:$ZP · mails http://localhost:8025"
echo "Vous pouvez fermer cette fenêtre : les services continuent de tourner dans Docker."
