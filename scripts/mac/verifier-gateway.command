#!/bin/bash
# Vérifie l'API Gateway Kong et le serveur MCP sur l'installation qui tourne.
# Double-clic depuis le Finder. Résultat : logs/verification.log
cd "$(dirname "$0")/../.." || exit 1
mkdir -p logs
exec > >(tee logs/verification.log) 2>&1
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
export no_proxy="localhost,127.0.0.1" NO_PROXY="localhost,127.0.0.1"
GW=$(sed -n 's/^GATEWAY_PORT=//p' .env); BASE="http://localhost:${GW:-8080}"
STAFF=$(sed -n 's/^STAFF_API_KEY=//p' .env)
SHOP="demo-key-biarritz"
ok=0; ko=0
check() {  # libellé, code attendu, code obtenu
  if [ "$2" = "$3" ]; then ok=$((ok+1)); echo "OK    $1 ($3)"; else ko=$((ko+1)); echo "ECHEC $1 : attendu $2, obtenu $3"; fi
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
echo "== Vérification de l'API Gateway $(date) sur $BASE"

check "route publique /api/auth/config"            200 "$(code "$BASE/api/auth/config")"
check "portail"                                    200 "$(code "$BASE/")"
check "catalogue sans identifiant"                 401 "$(code "$BASE/api/catalog")"
check "catalogue avec clé de boutique"             200 "$(code -H "X-API-Key: $SHOP" "$BASE/api/catalog")"
check "panier avec clé de boutique"                200 "$(code -H "X-API-Key: $SHOP" "$BASE/api/cart")"
check "en-têtes d'identité forgés"                 401 "$(code -H 'X-Auth-User: x' -H 'X-Auth-Role: staff' "$BASE/api/warehouse/stock")"
check "entrepôt refusé à une boutique"             403 "$(code -H "X-API-Key: $SHOP" "$BASE/api/warehouse/stock")"
[ -n "$STAFF" ] && check "entrepôt pour Surfwear (clé STAFF_API_KEY)" 200 "$(code -H "X-API-Key: $STAFF" "$BASE/api/warehouse/stock")"
check "route interne jamais exposée"               404 "$(code "$BASE/internal/stock")"
check "documentation OpenAPI"                      200 "$(code "$BASE/svc/orders/openapi.json")"
check "santé du serveur MCP"                       200 "$(code "$BASE/svc/mcp/health")"
check "simulateur : redirection"                   301 "$(code "$BASE/boutiques")"
H=$(curl -s -D - -o /dev/null "$BASE/api/auth/config")
echo "$H" | grep -qi '^x-request-id:' && check "en-tête X-Request-Id" 1 1 || check "en-tête X-Request-Id" 1 0
echo "$H" | grep -qi '^x-content-type-options: nosniff' && check "en-têtes de sécurité" 1 1 || check "en-têtes de sécurité" 1 0

MCP=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"verification","version":"1"}}}'
check "MCP sans identifiant"                       401 "$(code "${MCP[@]}" -X POST -d "$INIT" "$BASE/mcp")"
check "MCP initialize (boutique)"                  200 "$(code "${MCP[@]}" -H "X-API-Key: $SHOP" -X POST -d "$INIT" "$BASE/mcp")"
TOOLS=$(curl -s "${MCP[@]}" -H "X-API-Key: $SHOP" -X POST -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' "$BASE/mcp")
N=$(echo "$TOOLS" | grep -o '"name":"[a-z_]*"' | wc -l | tr -d ' ')
check "MCP : outils listés" 21 "$N"
WHO=$(curl -s "${MCP[@]}" -H "X-API-Key: $SHOP" -X POST \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"qui_suis_je","arguments":{}}}' "$BASE/mcp")
echo "$WHO" | grep -q 'SHOP-BIA' && check "MCP : identité relayée par Kong" 1 1 || { check "MCP : identité relayée par Kong" 1 0; echo "$WHO" | head -c 400; echo; }
if [ -n "$STAFF" ]; then
  BOARD=$(curl -s "${MCP[@]}" -H "X-API-Key: $STAFF" -X POST \
    -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"tableau_de_bord","arguments":{}}}' "$BASE/mcp")
  echo "$BOARD" | grep -q 'commandes_par_etat' && check "MCP : tableau de bord Surfwear" 1 1 || { check "MCP : tableau de bord Surfwear" 1 0; echo "$BOARD" | head -c 400; echo; }
fi

echo "---- erreurs récentes de Kong"
docker compose logs --since 10m gateway 2>&1 | grep -i "\[error\]\|\[crit\]\|\[alert\]" | tail -10 || true
echo "== $ok vérifications réussies, $ko en échec"
