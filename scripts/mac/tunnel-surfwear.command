#!/bin/bash
# Tunnel public vers le portail (double-clic depuis le Finder). Cloudflare est essayé
# d'abord ; si son port 7844 est bloqué (réseaux d'école ou d'entreprise), bascule sur
# Pinggy, qui passe par le port 443 (sans compte, 60 min par session, relance automatique).
# Fermer cette fenêtre coupe le tunnel. Journal : logs/tunnel.log
cd "$(dirname "$0")/../.." || exit 1
mkdir -p logs
exec > >(tee logs/tunnel.log) 2>&1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
echo "== Tunnel $(date)"
PORT=$(sed -n 's/^GATEWAY_PORT=//p' .env 2>/dev/null); PORT=${PORT:-8080}
curl -sf "http://localhost:$PORT/api/auth/config" >/dev/null || { echo "ERREUR : portail absent sur le port $PORT (lancez d'abord lancer-surfwear.command)"; echo "== ECHEC"; exit 1; }
echo "Portail local OK sur le port $PORT"

if command -v cloudflared >/dev/null && nc -z -G 4 region1.v2.argotunnel.com 7844 2>/dev/null; then
  echo "== Tunnel Cloudflare"
  exec cloudflared tunnel --no-autoupdate --protocol http2 --url "http://localhost:$PORT"
fi

echo "== Port 7844 bloqué ou cloudflared absent : tunnel Pinggy par le port 443"
while true; do
  ssh -p 443 -T -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes -R0:localhost:$PORT a.pinggy.io
  echo "== Tunnel interrompu, nouvelle tentative dans 5 s (l'adresse va changer)"
  sleep 5
done
