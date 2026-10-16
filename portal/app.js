// Portail Surfwear Pro : boutiques (catalogue, panier, commandes, factures) et équipe Surfwear.
// La session est un cookie HttpOnly posé par le service Identité ; aucun secret n'est stocké ici.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const euro = (n) => (n ?? 0).toLocaleString("fr-FR", { style: "currency", currency: "EUR" });
const frDate = (iso) => (iso ? new Date(iso.slice(0, 10) + "T12:00:00").toLocaleDateString("fr-FR", { day: "numeric", month: "short" }) : "date à confirmer");
const frDay = (iso) => new Date(iso.slice(0, 10) + "T12:00:00").toLocaleDateString("fr-FR");
const frDateTime = (iso) => new Date(iso).toLocaleString("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const clock = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
const plural = (n, one, many) => `${n} ${n > 1 ? many : one}`;
const digits = (v) => parseInt(String(v).replace(/\D/g, "") || "0", 10);

const STATE = {
  pending: ["Pré-réservée", "pending"],
  confirmed: ["Finalisée, réservée", "ok"],
  backordered: ["Finalisée, avec reliquat", "warn"],
  partially_shipped: ["Partiellement expédiée", "info"],
  shipped: ["Expédiée", "ok"],
  expired: ["Expirée", "off"],
  cancelled: ["Annulée", "off"],
};
const SHOP_STATE = { pending: ["En attente", "pending"], active: ["Active", "ok"], rejected: ["Refusée", "off"], suspended: ["Suspendue", "warn"] };
const CONNECTORS = { rest: "Éditeur A (webhooks)", csv: "Éditeur B (fichiers CSV)", none: "Portail seul" };
const DEMO = [
  ["biarritz@boutiques.example", "Biarritz2026!", "Surf Shack Biarritz"],
  ["hossegor@boutiques.example", "Hossegor2026!", "Hossegor Board Store"],
  ["lacanau@boutiques.example", "Lacanau2026!", "Lacanau Glisse"],
  ["arcachon@boutiques.example", "Arcachon2026!", "Arcachon Wave"],
  ["equipe@surfwear.example", "Surfwear2026!", "Équipe Surfwear"],
];

const app = {
  config: { provider: "local" }, me: null, view: null, busy: false,
  catalog: [], cart: { lines: [], count: 0, total_ht: 0, total_vat: 0, total_ttc: 0 },
  category: "Tout", search: "", orders: [], orderFilter: "all", open: new Set(), deadlines: new Map(),
  invoices: [], invoiceFilter: "all",
  allOrders: [], shops: [], shopFilter: "", stateFilter: "active",
  pickings: [], stock: { products: [], lots: [] }, shipments: [], receptions: [], adjustments: [], products: [],
  editing: null, pendingQty: new Map(),
};

// ------------------------------------------------------------------ outils
function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 4500);
}

function ask(title, text, yes = "Confirmer") {
  return new Promise((resolve) => {
    $("#dialog-title").textContent = title;
    $("#dialog-text").textContent = text;
    $("#dialog-yes").textContent = yes;
    $("#dialog").hidden = false;
    $("#dialog-yes").focus();
    const done = (v) => { $("#dialog").hidden = true; $("#dialog-yes").onclick = $("#dialog-no").onclick = null; resolve(v); };
    $("#dialog-yes").onclick = () => done(true);
    $("#dialog-no").onclick = () => done(false);
  });
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#dialog").hidden) $("#dialog-no").click(); });

async function api(path, { method = "GET", body, headers = {} } = {}) {
  const resp = await fetch(path, {
    method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 401 && !path.startsWith("/api/auth/")) {
    showLogin("Votre session a expiré. Reconnectez-vous.");
    throw new Error("Connexion requise");
  }
  const data = resp.status === 204 ? null : await resp.json().catch(() => null);
  if (!resp.ok) {
    const d = data?.detail;
    const msg = typeof d === "string" ? d
      : Array.isArray(d) ? d.map((x) => x.msg?.replace(/^Value error, /, "")).join(" · ")
      : d?.message ? `${d.message}${d.skus ? " : " + d.skus.join(", ") : ""}`
      : resp.status === 429 ? "Trop de tentatives, patientez une minute." : `Erreur ${resp.status}`;
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return data;
}

function rememberDeadlines(orders) {
  const now = Date.now();
  for (const o of orders) {
    if (o.state === "pending" && o.seconds_left != null) app.deadlines.set(o.order_id, now + o.seconds_left * 1000);
    else app.deadlines.delete(o.order_id);
  }
}
const secondsLeft = (id) => Math.max(0, Math.round(((app.deadlines.get(id) ?? 0) - Date.now()) / 1000));

function stateBadge(o) {
  const [label, tone] = STATE[o.status] || [o.status, "off"];
  const timer = o.status === "pending" ? ` <span class="timer" data-timer="${esc(o.order_id)}">${clock(secondsLeft(o.order_id))}</span>` : "";
  return `<span class="state ${tone}">${label}</span>${timer}`;
}

function lineSummary(l) {
  const parts = [];
  if (l.qty_shipped) parts.push(`${l.qty_shipped} expédié${l.qty_shipped > 1 ? "s" : ""}`);
  if (l.qty_reserved) parts.push(`${l.qty_reserved} réservé${l.qty_reserved > 1 ? "s" : ""}`);
  for (const s of l.scheduled) parts.push(`${s.qty} le ${frDate(s.eta)}`);
  if (l.qty_unscheduled) parts.push(`${l.qty_unscheduled} en attente de production`);
  return parts.join(", ");
}

// ------------------------------------------------------------------ connexion et inscription
function showPanel(id) {
  for (const p of ["signin", "signup", "signup-done"]) $(`#${p}`).hidden = p !== id;
}

function showLogin(message) {
  app.me = null;
  $("#app-view").hidden = true;
  $("#pending-view").hidden = true;
  $("#login-view").hidden = false;
  const err = $("#login-error");
  err.hidden = !message;
  err.textContent = message || "";
  const zitadel = app.config.provider === "zitadel";
  $("#signin-zitadel").hidden = !zitadel;
  $("#login-form").hidden = zitadel;
  const local = ["localhost", "127.0.0.1"].includes(location.hostname);
  $(".demo-accounts").hidden = !local;
  $("#demo-list").innerHTML = DEMO.map(([u, p, n]) => `<li><button type="button" data-user="${esc(u)}" data-pass="${esc(p)}">
      <span>${esc(n)}</span><span class="hint">${esc(u)} · ${esc(p)}</span></button></li>`).join("");
  showPanel("signin");
}

document.addEventListener("click", (e) => {
  const s = e.target.closest("[data-show]");
  if (s) { showPanel(s.dataset.show); $(`#${s.dataset.show} input`)?.focus(); }
});

$("#demo-list").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-user]");
  if (!b) return;
  if (app.config.provider === "zitadel") {
    navigator.clipboard?.writeText(b.dataset.pass).catch(() => {});
    location.href = `/api/auth/login?hint=${encodeURIComponent(b.dataset.user)}`;
    return;
  }
  $('#login-form [name="username"]').value = b.dataset.user;
  $('#login-form [name="password"]').value = b.dataset.pass;
  $("#login-form button[type=submit]").focus();
});

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const btn = $("button[type=submit]", e.target);
  btn.disabled = true;
  try {
    const me = await api("/api/auth/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
    e.target.reset();
    start(me);
  } catch (err) {
    $("#login-error").hidden = false;
    $("#login-error").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$("#signup").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  const btn = $("button[type=submit]", e.target);
  const err = $("#signup-error");
  btn.disabled = true;
  err.hidden = true;
  try {
    await api("/api/register", { method: "POST", body: f });
    e.target.reset();
    showPanel("signup-done");
  } catch (x) {
    err.hidden = false;
    err.textContent = x.message;
  } finally {
    btn.disabled = false;
  }
});

document.addEventListener("click", async (e) => {
  if (!e.target.closest("[data-logout]")) return;
  const out = await api("/api/auth/logout", { method: "POST" }).catch(() => null);
  app.me = null;
  if (out?.logout_url) location.href = out.logout_url;
  else showLogin();
});

// ------------------------------------------------------------------ navigation
const NAV = {
  shop: [["catalog", "Catalogue"], ["cart", "Panier"], ["orders", "Commandes"], ["invoices", "Factures"], ["ai", "Assistant IA"]],
  staff: [["overview", "Vue d'ensemble"], ["shops", "Boutiques"], ["all-orders", "Commandes"], ["picking", "Préparation"],
    ["receiving", "Réceptions"], ["products", "Produits"], ["invoices", "Factures"], ["ai", "Assistant IA"]],
};

function start(me) {
  app.me = me;
  $("#login-view").hidden = true;
  if (me.role === "shop" && me.shop.status !== "active") {
    $("#app-view").hidden = true;
    $("#pending-view").hidden = false;
    const s = me.shop.status;
    $("#pending-title").textContent = s === "pending" ? "Compte en attente de validation" : s === "suspended" ? "Compte suspendu" : "Inscription refusée";
    $("#pending-text").textContent = s === "pending"
      ? `Merci ${me.display_name}. L'équipe Surfwear vérifie l'inscription de ${me.shop.name} (SIRET ${me.shop.siret}). Vous recevrez un e-mail dès la validation.`
      : "Contactez l'équipe Surfwear pour en savoir plus.";
    return;
  }
  $("#pending-view").hidden = true;
  $("#app-view").hidden = false;
  document.body.classList.toggle("is-staff", me.role === "staff");
  $("#who-name").textContent = me.shop ? `${me.display_name} · ${me.shop.name}` : me.display_name;
  $$(".hold-minutes").forEach((el) => (el.textContent = plural(Math.round(me.hold_minutes), "minute", "minutes")));
  const wanted = location.hash.slice(1);
  go(NAV[me.role].some(([id]) => id === wanted) ? wanted : me.role === "staff" ? "overview" : "catalog");
}

function renderNav() {
  const badges = {
    cart: app.cart.count, orders: app.orders.filter((o) => o.state === "pending").length,
    picking: app.pickings.length, shops: app.shops.filter((s) => s.status === "pending").length,
  };
  $("#nav").innerHTML = NAV[app.me.role].map(([id, label]) =>
    `<button data-go="${id}" ${app.view === id ? 'aria-current="page"' : ""}>${label}${badges[id] ? `<span class="badge">${badges[id]}</span>` : ""}</button>`).join("");
}

$("#nav").addEventListener("click", (e) => { const b = e.target.closest("[data-go]"); if (b) go(b.dataset.go); });

function go(view) {
  app.view = view;
  history.replaceState(null, "", `#${view}`);
  $$("[data-view]").forEach((s) => (s.hidden = s.dataset.view !== view));
  renderNav();
  refresh(true);
  window.scrollTo({ top: 0 });
}

// ------------------------------------------------------------------ assistant IA (serveur MCP derrière la gateway)
const AI_TOOLS = {
  shop: ["Chercher dans le catalogue et vérifier une disponibilité", "Remplir, vider et commander le panier",
    "Suivre, finaliser ou annuler ses pré-réservations", "Consulter ses factures"],
  staff: ["Tableau de bord : commandes, préparations, stock bas, impayés", "Lister et valider les boutiques",
    "Consulter le stock, les lots et les préparations", "Expédier et enregistrer des réceptions", "Consulter toutes les commandes et factures"],
};

function renderAi(token = "VOTRE_JETON") {
  const url = `${location.origin}/mcp`;
  $("#ai-url").textContent = url;
  $("#ai-claude-code").textContent =
    `claude mcp add --transport http surfwear-pro ${url} \\\n  --header "Authorization: Bearer ${token}"`;
  $("#ai-claude-desktop").textContent = JSON.stringify({ mcpServers: { "surfwear-pro": {
    command: "npx", args: ["-y", "mcp-remote", url, "--allow-http", "--header", "Authorization:${SURFWEAR_TOKEN}"],
    env: { SURFWEAR_TOKEN: `Bearer ${token}` } } } }, null, 2);
  $("#ai-tools").innerHTML = AI_TOOLS[app.me.role].map((t) => `<li>${esc(t)}</li>`).join("");
}

$("#ai-token").addEventListener("click", async () => {
  try {
    const out = await api("/api/auth/token", { method: "POST" });
    $("#ai-token-value").value = out.token;
    $("#ai-token-exp").textContent = new Date(out.expires_at).toLocaleDateString("fr-FR");
    $("#ai-token-out").hidden = false;
    renderAi(out.token);
    toast("Jeton créé : copiez-le maintenant, il ne sera plus affiché.");
  } catch (err) { toast(err.message, "error"); }
});

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b) return;
  const el = $(b.dataset.copy);
  try { await navigator.clipboard.writeText(el.value || el.textContent); toast("Copié"); }
  catch { el.select?.(); toast("Sélectionné : copiez avec ⌘C"); }
});

// ------------------------------------------------------------------ boutique : catalogue (la quantité = le panier)
function cartQty(sku) {
  if (app.pendingQty.has(sku)) return app.pendingQty.get(sku);
  return app.cart.lines.find((l) => l.sku === sku)?.qty || 0;
}

function planFor(p, qty) {
  let rest = qty;
  const now = Math.min(rest, Math.max(p.available_now ?? 0, 0));
  rest -= now;
  const later = [];
  for (const u of p.upcoming || []) {
    if (!rest) break;
    const t = Math.min(rest, u.available);
    if (t) { later.push([t, u.eta]); rest -= t; }
  }
  return { now, later, none: rest };
}

function previewHtml(plan) {
  const parts = [];
  if (plan.now) parts.push(`<span class="now">${plan.now} tout de suite</span>`);
  for (const [t, eta] of plan.later) parts.push(`<span class="later">${t} le ${frDate(eta)}</span>`);
  if (plan.none) parts.push(`<span class="none">${plan.none} sans date de production</span>`);
  return parts.join(" · ");
}

function stockHtml(p) {
  const max = Math.max(...app.catalog.map((x) => x.available_now || 0), 1);
  const lots = (p.upcoming || []).length ? p.upcoming.map((u) => `+${u.available} le ${frDate(u.eta)}`).join(", ")
    : p.available_now ? "" : "Aucun lot prévu";
  return `<div class="stock">
    <div class="stock-line"><span class="stock-qty ${p.available_now ? "" : "zero"}">${p.available_now ?? "?"}</span>
      <span class="hint">${p.available_now ? "disponibles" : "en rupture"}</span></div>
    <div class="stock-bar"><i style="width:${Math.round(((p.available_now || 0) / max) * 100)}%"></i></div>
    ${lots ? `<div class="lots">${lots}</div>` : ""}</div>`;
}

function renderCategories() {
  const cats = ["Tout", ...new Set(app.catalog.map((p) => p.category))];
  $("#categories").innerHTML = cats.map((c) => `<button type="button" data-cat="${esc(c)}" aria-pressed="${c === app.category}">${esc(c)}</button>`).join("");
}

function visible(p) {
  const q = app.search.trim().toLowerCase();
  return (app.category === "Tout" || p.category === app.category)
    && (!q || `${p.name} ${p.sku} ${p.description}`.toLowerCase().includes(q));
}

function renderCatalog() {
  const body = $("#catalog-body");
  const known = $$("tr[data-sku]", body).map((r) => r.dataset.sku).join();
  if (known !== app.catalog.map((p) => p.sku).join()) {
    body.innerHTML = app.catalog.map((p) => `
      <tr data-sku="${esc(p.sku)}">
        <td><div class="name">${esc(p.name)}</div><div class="sku">${esc(p.sku)} · ${esc(p.category)}</div>
          <div class="desc">${esc(p.description)}</div><div class="preview" aria-live="polite"></div></td>
        <td class="num price">${euro(p.unit_price)}</td>
        <td class="avail"></td>
        <td class="num qty"><div class="qty-cell"><span class="saving" aria-hidden="true"></span>
          <input class="qty-input" type="text" inputmode="numeric" autocomplete="off" value="0" aria-label="Quantité de ${esc(p.name)}"></div></td>
      </tr>`).join("");
  }
  let shown = 0;
  for (const p of app.catalog) {
    const row = $(`tr[data-sku="${CSS.escape(p.sku)}"]`, body);
    row.hidden = !visible(p);
    shown += !row.hidden;
    $(".avail", row).innerHTML = stockHtml(p);
    const qty = cartQty(p.sku);
    row.classList.toggle("picked", qty > 0);
    $(".preview", row).innerHTML = qty ? previewHtml(planFor(p, qty)) : "";
    const input = $(".qty-input", row);
    if (document.activeElement !== input) input.value = qty;
  }
  $("#catalog-empty").hidden = shown > 0;
}

const timers = new Map();
async function setCartQty(sku, qty) {
  app.pendingQty.set(sku, qty);
  clearTimeout(timers.get(sku));
  const mark = $(`tr[data-sku="${CSS.escape(sku)}"] .saving`);
  if (mark) mark.textContent = "…";
  timers.set(sku, setTimeout(async () => {
    try {
      app.cart = await api(`/api/cart/items/${encodeURIComponent(sku)}`, { method: "PUT", body: { qty } });
      if (app.pendingQty.get(sku) === qty) app.pendingQty.delete(sku);
      if (mark) mark.textContent = "";
    } catch (err) {
      app.pendingQty.delete(sku);
      if (mark) mark.textContent = "!";
      toast(err.message, "error");
    }
    renderDock();
    renderNav();
    if (app.view === "cart") renderCart();
  }, 450));
}

$("#catalog-body").addEventListener("focusin", (e) => { if (e.target.classList.contains("qty-input")) e.target.select(); });
$("#catalog-body").addEventListener("input", (e) => {
  const input = e.target;
  if (!input.classList.contains("qty-input")) return;
  const clean = input.value.replace(/\D/g, "").replace(/^0+(?=\d)/, "").slice(0, 5);
  if (input.value !== clean) input.value = clean;
  const qty = Math.min(parseInt(clean || "0", 10), 10000);
  const row = input.closest("tr");
  const p = app.catalog.find((x) => x.sku === row.dataset.sku);
  row.classList.toggle("picked", qty > 0);
  $(".preview", row).innerHTML = qty ? previewHtml(planFor(p, qty)) : "";
  setCartQty(p.sku, qty);
  renderDock();
});
$("#catalog-body").addEventListener("focusout", (e) => { if (e.target.classList?.contains("qty-input") && e.target.value === "") e.target.value = "0"; });
$("#catalog-body").addEventListener("keydown", (e) => {
  if (!e.target.classList.contains("qty-input")) return;
  if (e.key === "ArrowUp" || e.key === "ArrowDown") e.preventDefault();
  if (e.key === "Enter") {
    const inputs = $$(".qty-input", $("#catalog-body")).filter((i) => !i.closest("tr").hidden);
    inputs[inputs.indexOf(e.target) + 1]?.focus();
  }
});
$("#search").addEventListener("input", (e) => { app.search = e.target.value; renderCatalog(); });
$("#categories").addEventListener("click", (e) => {
  const b = e.target.closest("[data-cat]");
  if (!b) return;
  app.category = b.dataset.cat;
  renderCategories();
  renderCatalog();
});

// ------------------------------------------------------------------ boutique : panier
function renderCart() {
  const c = app.cart;
  $("#cart-lines").innerHTML = c.lines.length ? c.lines.map((l) => l.available === false ? `
    <div class="cart-line"><div><div class="name">${esc(l.sku)}</div><div class="hint">Cet article n'est plus au catalogue.</div></div>
      <span></span><button class="btn btn-ghost btn-small" data-remove="${esc(l.sku)}">Retirer</button></div>` : `
    <div class="cart-line" data-sku="${esc(l.sku)}">
      <div><div class="name">${esc(l.name)}</div><div class="sku">${esc(l.sku)} · ${euro(l.unit_price)} HT l'unité</div></div>
      <div class="qty-cell"><input class="qty-input cart-qty" type="text" inputmode="numeric" value="${l.qty}" aria-label="Quantité de ${esc(l.name)}">
        <button class="btn btn-ghost btn-small" data-remove="${esc(l.sku)}">Retirer</button></div>
      <div class="line-total">${euro(l.total_ht)}</div>
      <div class="preview">${previewHtml({ now: l.plan.now, later: l.plan.later.map((x) => [x.qty, x.eta]), none: l.plan.unscheduled })}</div>
    </div>`).join("")
    : `<p class="empty">Votre panier est vide. Saisissez des quantités dans le <button class="link" data-go-view="catalog">catalogue</button>.</p>`;
  const late = c.lines.reduce((s, l) => s + (l.plan ? l.qty - l.plan.now : 0), 0);
  $("#cart-totals").innerHTML = `
    <h2>Récapitulatif</h2>
    <dl><dt>${plural(c.count, "article", "articles")}</dt><dd></dd>
      <dt>Total HT</dt><dd>${euro(c.total_ht)}</dd><dt>TVA</dt><dd>${euro(c.total_vat)}</dd>
      <dt>Total TTC</dt><dd class="grand">${euro(c.total_ttc)}</dd></dl>
    <p class="hint">${late ? `${plural(late, "pièce sera livrée", "pièces seront livrées")} plus tard (reliquat).` : c.count ? "Tout est disponible immédiatement." : ""}</p>
    <button class="btn btn-primary btn-block" data-checkout ${c.count ? "" : "disabled"}>Pré-réserver</button>
    <button class="btn btn-ghost btn-block" id="cart-empty" ${c.count ? "" : "disabled"}>Vider le panier</button>`;
}

$("#cart-lines").addEventListener("input", (e) => {
  if (!e.target.classList.contains("cart-qty")) return;
  const clean = e.target.value.replace(/\D/g, "").replace(/^0+(?=\d)/, "").slice(0, 5);
  if (e.target.value !== clean) e.target.value = clean;
  if (clean !== "") setCartQty(e.target.closest("[data-sku]").dataset.sku, Math.min(parseInt(clean, 10), 10000));
});
$("#cart-lines").addEventListener("keydown", (e) => { if (e.key === "ArrowUp" || e.key === "ArrowDown") e.preventDefault(); });
document.addEventListener("click", async (e) => {
  const r = e.target.closest("[data-remove]");
  if (r) {
    app.cart = await api(`/api/cart/items/${encodeURIComponent(r.dataset.remove)}`, { method: "DELETE" }).catch((x) => { toast(x.message, "error"); return app.cart; });
    renderCart(); renderDock(); renderNav();
  }
  if (e.target.id === "cart-empty" && await ask("Vider le panier ?", "Tous les articles seront retirés.", "Vider")) {
    app.cart = await api("/api/cart", { method: "DELETE" });
    renderCart(); renderDock(); renderNav();
  }
  const g = e.target.closest("[data-go-view]");
  if (g) go(g.dataset.goView);
  if (e.target.closest("[data-checkout]")) checkout(e.target.closest("[data-checkout]"));
});
$("#cart-open").addEventListener("click", () => go("cart"));

async function checkout(btn) {
  for (const t of timers.values()) clearTimeout(t);
  if (app.pendingQty.size) {  // enregistre d'abord les saisies en cours
    for (const [sku, qty] of [...app.pendingQty]) app.cart = await api(`/api/cart/items/${encodeURIComponent(sku)}`, { method: "PUT", body: { qty } });
    app.pendingQty.clear();
  }
  btn.disabled = true;
  try {
    const order = await api("/api/cart/checkout", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() } });
    app.orders = [order, ...app.orders.filter((o) => o.order_id !== order.order_id)];
    rememberDeadlines([order]);
    toast(`Stock bloqué ${plural(Math.round(app.me.hold_minutes), "minute", "minutes")}. Finalisez la commande pour la valider.`);
    await refresh(true);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------------ bandeau : pré-réservation et panier
function renderDock() {
  if (!app.me || app.me.role !== "shop") { $("#dock").hidden = true; return; }
  const pending = app.orders.filter((o) => o.state === "pending" && secondsLeft(o.order_id) > 0)
    .sort((a, b) => secondsLeft(a.order_id) - secondsLeft(b.order_id));
  const count = app.cart.count;
  const showCart = count > 0 && app.view !== "cart";
  $("#dock").hidden = !pending.length && !showCart;

  const hold = $("#dock-hold");
  hold.hidden = !pending.length;
  if (pending.length) {
    const o = pending[0];
    const left = secondsLeft(o.order_id);
    const late = left < 120;
    const qty = o.lines.reduce((s, l) => s + l.qty_ordered, 0);
    if (hold.dataset.order !== o.order_id) {
      hold.dataset.order = o.order_id;
      hold.innerHTML = `
        <div class="hold">
          <div class="hold-clock" data-clock></div>
          <div class="hold-text"><strong>${plural(qty, "article bloqué", "articles bloqués")} pour vous · ${euro(o.total_amount)} HT</strong>
            <span>Finalisez avant la fin du compte à rebours, sinon le stock retourne aux autres boutiques.</span></div>
          <div class="cart-actions">
            <button class="btn btn-ghost" data-act="cancel" data-id="${esc(o.order_id)}">Annuler</button>
            <button class="btn btn-ghost" data-act="details" data-id="${esc(o.order_id)}">Voir le détail</button>
            <button class="btn btn-primary" data-act="confirm" data-id="${esc(o.order_id)}">Finaliser la commande</button>
          </div>
        </div>
        <div class="tide" aria-hidden="true"><i></i></div>
        <div class="hold-more" data-more></div>`;
    }
    const c = $("[data-clock]", hold);
    c.textContent = clock(left);
    c.classList.toggle("late", late);
    const bar = $(".tide i", hold);
    bar.style.width = `${Math.min(100, (left / (app.me.hold_minutes * 60)) * 100)}%`;
    bar.classList.toggle("late", late);
    $("[data-more]", hold).textContent = pending.length > 1
      ? `${plural(pending.length - 1, "autre pré-réservation", "autres pré-réservations")} en attente : voir Commandes.` : "";
  } else hold.dataset.order = "";

  $("#dock-cart").hidden = !showCart;
  if (showCart) {
    $("#cart-count").textContent = `Panier : ${plural(count, "article", "articles")} · ${euro(app.cart.total_ht)} HT`;
    const late = app.cart.lines.reduce((s, l) => s + (l.plan ? l.qty - l.plan.now : 0), 0);
    $("#cart-detail").textContent = `${plural(app.cart.lines.length, "référence", "références")}` + (late ? ` · ${plural(late, "pièce", "pièces")} en reliquat` : " · tout est disponible");
  }
}

async function orderAction(act, id) {
  if (act === "details") { app.open.add(id); app.orderFilter = "pending"; go("orders"); return; }
  if (act === "cancel" && !(await ask("Annuler la pré-réservation ?", `Le stock de ${id} sera remis à disposition des autres boutiques.`, "Annuler la commande"))) return;
  try {
    const o = await api(`/api/orders/${encodeURIComponent(id)}/${act}`, { method: "POST" });
    if (app.orderFilter === "pending") app.orderFilter = "all";
    app.open.add(o.order_id);
    toast(act === "confirm" ? `Commande ${o.order_id} finalisée : elle part en préparation.` : `Pré-réservation ${o.order_id} annulée.`);
  } catch (err) { toast(err.message, "error"); }
  await refresh(true);
}
document.addEventListener("click", (e) => { const b = e.target.closest("[data-act]"); if (b && b.dataset.id) orderAction(b.dataset.act, b.dataset.id); });

// ------------------------------------------------------------------ boutique : commandes
const FILTERS = [
  ["all", "Toutes", () => true],
  ["pending", "À finaliser", (o) => o.state === "pending"],
  ["active", "En cours", (o) => ["confirmed", "backordered", "partially_shipped"].includes(o.status)],
  ["shipped", "Expédiées", (o) => o.status === "shipped"],
  ["closed", "Expirées ou annulées", (o) => ["expired", "cancelled"].includes(o.state)],
];

function steps(o) {
  if (o.state === "expired" || o.state === "cancelled") return "";
  const idx = { pending: 0, confirmed: 1, backordered: 1, partially_shipped: 2, shipped: 3 }[o.status];
  return `<ol class="steps">${["Pré-réservée", "Finalisée", "En livraison", "Expédiée"].map((n, i) =>
    `<li class="${i < idx ? "done" : i === idx ? "current" : ""}">${n}</li>`).join("")}</ol>`;
}

function renderOrders() {
  $("#order-filter").innerHTML = FILTERS.map(([id, label, f]) =>
    `<button type="button" data-filter="${id}" aria-pressed="${app.orderFilter === id}">${label}<span class="count">${app.orders.filter(f).length}</span></button>`).join("");
  const list = app.orders.filter(FILTERS.find(([id]) => id === app.orderFilter)[2]);
  const invByOrder = {};
  for (const i of app.invoices) (invByOrder[i.order_id] ||= []).push(i);
  $("#orders-list").innerHTML = list.length ? list.map((o) => `
    <details class="order" data-id="${esc(o.order_id)}" ${app.open.has(o.order_id) ? "open" : ""}>
      <summary>
        <div><div class="order-id">${esc(o.order_id)}</div>
          <div class="order-meta">${frDateTime(o.created_at)} · ${esc(o.placed_by || "")} · ${plural(o.lines.length, "référence", "références")}</div></div>
        <div>${stateBadge(o)}</div>
        <div class="num amount">${euro(o.total_amount)} HT</div>
      </summary>
      <div class="order-body">
        ${steps(o)}
        ${o.state === "expired" ? `<p class="hint">Non finalisée à temps : le stock a été remis à disposition des autres boutiques.</p>` : ""}
        <table class="lines"><tbody>${o.lines.map((l) => `
          <tr><td><span class="name">${esc(l.name)}</span><div class="sku">${esc(l.sku)}</div></td>
            <td class="num">${l.qty_ordered} × ${euro(l.unit_price)}</td><td>${lineSummary(l)}</td></tr>`).join("")}</tbody></table>
        <div class="order-actions">
          ${o.state === "pending" ? `<button class="btn btn-primary" data-act="confirm" data-id="${esc(o.order_id)}">Finaliser la commande</button>
            <button class="btn btn-danger" data-act="cancel" data-id="${esc(o.order_id)}">Annuler</button>` : ""}
          ${(invByOrder[o.order_id] || []).map((i) => `<a class="btn btn-ghost" href="/api/invoices/${encodeURIComponent(i.id)}/pdf">Facture ${esc(i.id)} (PDF)</a>`).join("")}
        </div>
      </div>
    </details>`).join("")
    : `<p class="empty">${app.orders.length ? "Aucune commande dans cette catégorie." : "Aucune commande pour l'instant. Commencez par le catalogue."}</p>`;
}
$("#order-filter").addEventListener("click", (e) => { const b = e.target.closest("[data-filter]"); if (b) { app.orderFilter = b.dataset.filter; renderOrders(); } });
$("#orders-list").addEventListener("toggle", (e) => { const id = e.target.dataset.id; if (id) e.target.open ? app.open.add(id) : app.open.delete(id); }, true);

// ------------------------------------------------------------------ factures
const INV_FILTERS = [
  ["all", "Toutes", () => true],
  ["due", "À régler", (i) => i.status !== "paid"],
  ["overdue", "En retard", (i) => i.overdue],
  ["paid", "Réglées", (i) => i.status === "paid"],
];
function renderInvoices() {
  $("#invoice-filter").innerHTML = INV_FILTERS.map(([id, label, f]) =>
    `<button type="button" data-ifilter="${id}" aria-pressed="${app.invoiceFilter === id}">${label}<span class="count">${app.invoices.filter(f).length}</span></button>`).join("");
  const rows = app.invoices.filter(INV_FILTERS.find(([id]) => id === app.invoiceFilter)[2]);
  const staff = app.me.role === "staff";
  $("#invoices-body").innerHTML = rows.length ? rows.map((i) => `
    <tr><td><div class="order-id">${esc(i.id)}</div><div class="sku">${frDay(i.issued_at)}</div></td>
      <td class="staff-only">${esc(i.shop.name)}</td>
      <td>${esc(i.order_id)}<div class="sku">BL ${esc(i.shipment_id)}</div></td>
      <td class="num">${euro(i.total_ttc)}<div class="sku">${euro(i.total_ht)} HT</div></td>
      <td>${frDay(i.due_date)}</td>
      <td>${i.status === "paid" ? `<span class="state ok">Réglée</span>` : i.overdue ? `<span class="state warn">En retard</span>` : `<span class="state info">À régler</span>`}</td>
      <td class="num"><a class="btn btn-ghost btn-small" href="/api/invoices/${encodeURIComponent(i.id)}/pdf">PDF</a>
        ${staff && i.status !== "paid" ? `<button class="btn btn-ghost btn-small" data-paid="${esc(i.id)}">Marquer réglée</button>` : ""}</td></tr>`).join("")
    : `<tr><td colspan="7" class="empty">${app.invoices.length ? "Aucune facture dans cette catégorie." : "Aucune facture : elles sont émises à l'expédition des colis."}</td></tr>`;
}
$("#invoice-filter").addEventListener("click", (e) => { const b = e.target.closest("[data-ifilter]"); if (b) { app.invoiceFilter = b.dataset.ifilter; renderInvoices(); } });
$("#invoices-body").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-paid]");
  if (!b) return;
  try { await api(`/api/invoices/${encodeURIComponent(b.dataset.paid)}/paid`, { method: "POST" }); toast(`Facture ${b.dataset.paid} marquée réglée.`); }
  catch (err) { toast(err.message, "error"); }
  refresh(true);
});

// ------------------------------------------------------------------ Surfwear : vue d'ensemble
function renderOverview() {
  const pending = app.allOrders.filter((o) => o.state === "pending");
  const backlog = app.allOrders.filter((o) => o.status === "backordered" || o.status === "partially_shipped")
    .reduce((s, o) => s + o.lines.reduce((t, l) => t + l.qty_unscheduled + l.scheduled.reduce((u, x) => u + x.qty, 0), 0), 0);
  const requests = app.shops.filter((s) => s.status === "pending").length;
  const due = app.invoices.filter((i) => i.status !== "paid").reduce((s, i) => s + i.total_ttc, 0);
  const kpis = [
    [requests, "inscriptions à valider", "shops", null, requests > 0],
    [pending.length, "pré-réservations en cours", "all-orders", "pending", false],
    [app.pickings.length, "commandes à préparer", "picking", null, false],
    [backlog, "pièces en reliquat", "all-orders", "active", false],
  ];
  $("#kpis").innerHTML = kpis.map(([n, label, view, filter, hot]) =>
    `<button class="kpi ${hot ? "hot" : ""}" data-kpi="${view}" data-filter="${filter ?? ""}"><b>${n}</b><span>${label}</span></button>`).join("")
    + "";
  $("#overview-pending").innerHTML = pending.length ? `<div class="table-wrap"><table class="table"><thead>
      <tr><th>Commande</th><th>Boutique</th><th>Temps restant</th><th class="num">Montant HT</th></tr></thead><tbody>
      ${pending.map((o) => `<tr><td>${esc(o.order_id)}</td><td>${esc(o.shop.name)}</td><td>${stateBadge(o)}</td><td class="num">${euro(o.total_amount)}</td></tr>`).join("")}</tbody></table></div>`
    : `<p class="empty">Aucune pré-réservation en attente de finalisation.</p>`;
  const recent = app.allOrders.filter((o) => o.state !== "pending").slice(0, 8);
  $("#overview-recent").innerHTML = (recent.length ? `<div class="table-wrap"><table class="table"><thead>
      <tr><th>Commande</th><th>Boutique</th><th>État</th><th class="num">Montant HT</th></tr></thead><tbody>
      ${recent.map((o) => `<tr><td>${esc(o.order_id)}<div class="sku">${frDateTime(o.created_at)}</div></td><td>${esc(o.shop.name)}</td>
        <td>${stateBadge(o)}</td><td class="num">${euro(o.total_amount)}</td></tr>`).join("")}</tbody></table></div>`
    : `<p class="empty">Aucune commande pour l'instant.</p>`) + (due ? `<p class="hint">Factures à encaisser : ${euro(due)} TTC.</p>` : "");
}
$("#kpis").addEventListener("click", (e) => { const b = e.target.closest("[data-kpi]"); if (!b) return; if (b.dataset.filter) app.stateFilter = b.dataset.filter; go(b.dataset.kpi); });

// ------------------------------------------------------------------ Surfwear : boutiques
function renderShops() {
  const requests = app.shops.filter((s) => s.status === "pending");
  $("#shop-requests").innerHTML = requests.length ? `<h2>Inscriptions à valider</h2>` + requests.map((s) => `
    <div class="request" data-shop="${esc(s.id)}">
      <div><div class="name">${esc(s.name)}</div>
        <dl><dt>SIRET</dt><dd>${esc(s.siret)}</dd><dt>Adresse</dt><dd>${esc(s.address)}, ${esc(s.city)}</dd>
          <dt>Contact</dt><dd>${esc(s.contact_name)} · ${esc(s.email)}</dd><dt>Inscrite le</dt><dd>${frDateTime(s.created_at)}</dd></dl></div>
      <div class="decide">
        <label class="hint">Logiciel de la boutique
          <select data-connector>${Object.entries(CONNECTORS).map(([k, v]) => `<option value="${k}" ${k === "none" ? "selected" : ""}>${v}</option>`).join("")}</select></label>
        <button class="btn btn-primary" data-approve="${esc(s.id)}">Valider</button>
        <button class="btn btn-danger" data-reject="${esc(s.id)}">Refuser</button>
      </div>
    </div>`).join("") : "";
  const rows = app.shops.filter((s) => s.status !== "pending");
  $("#shops-body").innerHTML = rows.map((s) => {
    const [label, tone] = SHOP_STATE[s.status];
    return `<tr><td><div class="name">${esc(s.name)}</div><div class="sku">${esc(s.id)} · ${esc(s.city)}</div></td>
      <td>${esc(s.contact_name || "")}<div class="sku">${esc(s.email)}</div></td>
      <td>${CONNECTORS[s.connector] || s.connector}</td>
      <td>${s.api_key ? `<span class="key" data-key="${esc(s.api_key)}">${esc(s.api_key.slice(0, 6))}••••</span>
        <button class="btn btn-ghost btn-small" data-copy="${esc(s.api_key)}">Copier</button>` : "—"}</td>
      <td><span class="state ${tone}">${label}</span></td>
      <td class="num">${s.status === "active" ? `<button class="btn btn-ghost btn-small" data-suspend="${esc(s.id)}">Suspendre</button>`
        : s.status !== "pending" ? `<button class="btn btn-ghost btn-small" data-reactivate="${esc(s.id)}" data-connector-now="${esc(s.connector)}">Réactiver</button>` : ""}</td></tr>`;
  }).join("");
}
$("#main").addEventListener("click", async (e) => {
  const t = e.target;
  try {
    if (t.dataset.approve) {
      const connector = $("[data-connector]", t.closest(".request")).value;
      await api(`/api/admin/shops/${t.dataset.approve}/approve`, { method: "POST", body: { connector } });
      toast("Boutique validée : elle peut commander. Un e-mail lui a été envoyé.");
    } else if (t.dataset.reject && await ask("Refuser l'inscription ?", "La boutique ne pourra pas commander.", "Refuser")) {
      await api(`/api/admin/shops/${t.dataset.reject}/reject`, { method: "POST" });
      toast("Inscription refusée.");
    } else if (t.dataset.suspend && await ask("Suspendre la boutique ?", "Ses utilisateurs et son logiciel n'auront plus accès au portail.", "Suspendre")) {
      await api(`/api/admin/shops/${t.dataset.suspend}/suspend`, { method: "POST" });
      toast("Boutique suspendue.");
    } else if (t.dataset.reactivate) {
      await api(`/api/admin/shops/${t.dataset.reactivate}/approve`, { method: "POST", body: { connector: t.dataset.connectorNow } });
      toast("Boutique réactivée.");
    } else if (t.dataset.copy) {
      await navigator.clipboard.writeText(t.dataset.copy).then(() => toast("Clé d'API copiée."), () => toast(t.dataset.copy));
      return;
    } else return;
  } catch (err) { toast(err.message, "error"); }
  refresh(true);
});

// ------------------------------------------------------------------ Surfwear : commandes
const STAFF_FILTERS = [
  ["active", "En cours", (o) => !["shipped", "expired", "cancelled"].includes(o.status)],
  ["pending", "Pré-réservées", (o) => o.state === "pending"],
  ["shipped", "Expédiées", (o) => o.status === "shipped"],
  ["closed", "Expirées ou annulées", (o) => ["expired", "cancelled"].includes(o.state)],
  ["all", "Toutes", () => true],
];
function renderAllOrders() {
  const shops = [...new Map(app.allOrders.map((o) => [o.shop.id, o.shop.name])).entries()];
  const sel = $("#shop-filter");
  sel.innerHTML = `<option value="">Toutes les boutiques</option>` + shops.map(([id, n]) => `<option value="${esc(id)}">${esc(n)}</option>`).join("");
  sel.value = app.shopFilter;
  const byShop = app.allOrders.filter((o) => !app.shopFilter || o.shop.id === app.shopFilter);
  $("#state-filter").innerHTML = STAFF_FILTERS.map(([id, label, f]) =>
    `<button type="button" data-state="${id}" aria-pressed="${app.stateFilter === id}">${label}<span class="count">${byShop.filter(f).length}</span></button>`).join("");
  const rows = byShop.filter(STAFF_FILTERS.find(([id]) => id === app.stateFilter)[2]);
  $("#all-orders-body").innerHTML = rows.length ? rows.map((o) => `
    <tr><td><div class="order-id">${esc(o.order_id)}</div><div class="sku">${frDateTime(o.created_at)}</div></td>
      <td>${esc(o.shop.name)}<div class="sku">${esc(o.placed_by || "")}</div></td><td>${stateBadge(o)}</td>
      <td>${o.lines.map((l) => `<div>${l.qty_ordered} × ${esc(l.name)} <span class="hint">${lineSummary(l)}</span></div>`).join("")}</td>
      <td class="num">${euro(o.total_amount)}</td></tr>`).join("") : `<tr><td colspan="5" class="empty">Aucune commande.</td></tr>`;
}
$("#shop-filter").addEventListener("change", (e) => { app.shopFilter = e.target.value; renderAllOrders(); });
$("#state-filter").addEventListener("click", (e) => { const b = e.target.closest("[data-state]"); if (b) { app.stateFilter = b.dataset.state; renderAllOrders(); } });

// ------------------------------------------------------------------ Surfwear : préparation
function renderPicking() {
  $("#dispatch-all").disabled = !app.pickings.length;
  $("#pickings").innerHTML = app.pickings.length ? app.pickings.map((p) => `
    <div class="pick"><div><div class="order-id">${esc(p.order_id)} · ${esc(p.shop?.name || "")}</div>
      <ul>${p.lines.map((l) => `<li>${l.qty} × ${esc(l.name)} <span class="sku">${esc(l.sku)}</span></li>`).join("")}</ul></div>
      <button class="btn btn-ghost btn-small" data-ship="${esc(p.order_id)}">Expédier ce colis</button></div>`).join("")
    : `<p class="empty">Rien à préparer pour l'instant.</p>`;
  $("#shipments").innerHTML = app.shipments.length ? `<div class="table-wrap"><table class="table"><thead>
      <tr><th>Bon de livraison</th><th>Boutique</th><th>Contenu</th><th>Expédié</th></tr></thead><tbody>
      ${app.shipments.slice(0, 30).map((s) => `<tr><td>${esc(s.id)}<div class="sku">${esc(s.order_id)}</div></td><td>${esc(s.shop_name)}</td>
        <td>${s.lines.map((l) => `${l.qty} × ${esc(l.name)}`).join("<br>")}</td><td>${frDateTime(s.dispatched_at)}<div class="sku">${esc(s.dispatched_by)}</div></td></tr>`).join("")}
      </tbody></table></div>` : `<p class="empty">Aucune expédition.</p>`;
}
async function dispatch(orderIds) {
  try {
    const sent = await api("/api/warehouse/pickings/dispatch", { method: "POST", body: orderIds ? { order_ids: orderIds } : {} });
    toast(`${plural(sent.length, "colis expédié", "colis expédiés")} : bons de livraison et factures transmis aux boutiques.`);
  } catch (err) { toast(err.message, "error"); }
  setTimeout(() => refresh(true), 500);
}
$("#dispatch-all").addEventListener("click", () => dispatch(null));
$("#pickings").addEventListener("click", (e) => { const b = e.target.closest("[data-ship]"); if (b) dispatch([b.dataset.ship]); });

// ------------------------------------------------------------------ Surfwear : réceptions et inventaire
function renderReceiving() {
  const names = Object.fromEntries(app.stock.products.map((p) => [p.sku, p.name]));
  const options = app.stock.products.map((p) => `<option value="${esc(p.sku)}">${esc(p.name)} (${esc(p.sku)})</option>`).join("");
  for (const sel of $$(".sku-select")) {
    if (sel.options.length !== app.stock.products.length) { const v = sel.value; sel.innerHTML = options; if (v) sel.value = v; }
  }
  updateAdjustHint();
  $("#lots").innerHTML = app.stock.lots.length ? `<table class="lines"><tbody>${app.stock.lots.map((l) => `
      <tr><td><span class="name">${esc(names[l.sku] || l.sku)}</span><div class="sku">${esc(l.id)} · prévu le ${frDate(l.eta)}</div></td>
        <td class="num">${l.qty}</td>
        <td class="num"><button class="btn btn-ghost btn-small" data-receive="${esc(l.id)}">Réceptionner</button></td></tr>`).join("")}</tbody></table>`
    : `<p class="empty">Aucun lot attendu.</p>`;
  $("#receptions").innerHTML = app.receptions.length ? `<table class="lines"><tbody>${app.receptions.slice(0, 8).map((r) => `
      <tr><td><span class="name">${esc(r.name)}</span><div class="sku">${esc(r.lot_id)}${r.reference ? " · BL " + esc(r.reference) : ""}</div></td>
        <td class="num">+${r.qty}</td><td><span class="sku">${frDateTime(r.received_at)}<br>${esc(r.received_by)}</span></td></tr>`).join("")}</tbody></table>`
    : `<p class="empty">Aucune réception.</p>`;
  $("#adjustments").innerHTML = app.adjustments.length ? `<table class="lines"><tbody>${app.adjustments.slice(0, 8).map((a) => `
      <tr><td><span class="name">${esc(names[a.sku] || a.sku)}</span><div class="sku">${esc(a.reason)}</div></td>
        <td class="num">${a.before} → ${a.after}</td><td><span class="sku">${frDateTime(a.at)}<br>${esc(a.by)}</span></td></tr>`).join("")}</tbody></table>`
    : `<p class="empty">Aucun inventaire.</p>`;
}
function updateAdjustHint() {
  const sku = $("#adjust-form [name=sku]").value;
  const p = app.stock.products.find((x) => x.sku === sku);
  $("#adjust-hint").textContent = p ? `Stock physique actuel : ${p.on_hand}.` : "";
}
$("#adjust-form [name=sku]").addEventListener("change", updateAdjustHint);
$("#lots").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-receive]");
  if (!b) return;
  b.disabled = true;
  try { await api("/api/warehouse/receptions", { method: "POST", body: { lot_id: b.dataset.receive } }); toast(`Lot ${b.dataset.receive} réceptionné : les reliquats correspondants sont réservés.`); }
  catch (err) { toast(err.message, "error"); }
  setTimeout(() => refresh(true), 500);
});
function formSubmit(id, handler) {
  $(id).addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("button[type=submit]", e.target);
    btn.disabled = true;
    try { await handler(new FormData(e.target), e.target); } catch (err) { toast(err.message, "error"); }
    btn.disabled = false;
    setTimeout(() => refresh(true), 500);
  });
}
formSubmit("#lot-form", async (f) => {
  const lot = await api("/api/warehouse/lots", { method: "POST", body: { sku: f.get("sku"), qty: digits(f.get("qty")), eta: f.get("eta") } });
  toast(`Lot ${lot.id} annoncé : les reliquats en attente reçoivent sa date.`);
});
formSubmit("#free-reception-form", async (f, form) => {
  await api("/api/warehouse/receptions", { method: "POST", body: { sku: f.get("sku"), qty: digits(f.get("qty")), reference: f.get("reference") } });
  form.reset();
  toast("Marchandise réceptionnée : elle sert d'abord les reliquats en attente.");
});
formSubmit("#adjust-form", async (f) => {
  const r = await api("/api/warehouse/adjustments", { method: "POST", body: { sku: f.get("sku"), on_hand: digits(f.get("on_hand")), reason: f.get("reason") } });
  toast(`Stock corrigé : ${r.before} → ${r.after}.`);
});
$('#lot-form [name="eta"]').value = new Date(Date.now() + 21 * 864e5).toISOString().slice(0, 10);

// ------------------------------------------------------------------ Surfwear : produits
function renderProducts() {
  $("#category-list").innerHTML = [...new Set(app.products.map((p) => p.category))].map((c) => `<option value="${esc(c)}">`).join("");
  $("#products-body").innerHTML = app.products.map((p) => `
    <tr><td><div class="name">${esc(p.name)}</div><div class="sku">${esc(p.sku)} · ${esc(p.category)}</div></td>
      <td class="num">${euro(p.unit_price)}</td><td class="num">${Math.round(p.vat_rate * 1000) / 10} %</td>
      <td class="num">${p.on_hand ?? "—"}</td><td class="num">${p.reserved ?? "—"}</td><td class="num">${p.available_now ?? "—"}</td>
      <td>${p.active ? `<span class="state ok">Visible</span>` : `<span class="state off">Masqué</span>`}</td>
      <td class="num"><button class="btn btn-ghost btn-small" data-edit="${esc(p.sku)}">Modifier</button></td></tr>`).join("");
}
function openProductForm(p) {
  const f = $("#product-form");
  app.editing = p ? p.sku : null;
  f.hidden = false;
  f.reset();
  $("#product-form-title").textContent = p ? `Modifier ${p.sku}` : "Nouveau produit";
  f.sku.disabled = !!p;
  if (p) {
    f.sku.value = p.sku; f.name.value = p.name; f.category.value = p.category; f.description.value = p.description;
    f.unit_price.value = String(p.unit_price).replace(".", ","); f.vat_rate.value = String(p.vat_rate); f.active.checked = p.active;
  }
  (p ? f.name : f.sku).focus();
}
$("#new-product").addEventListener("click", () => openProductForm(null));
$("#product-cancel").addEventListener("click", () => { $("#product-form").hidden = true; app.editing = null; });
$("#products-body").addEventListener("click", (e) => {
  const b = e.target.closest("[data-edit]");
  if (b) openProductForm(app.products.find((p) => p.sku === b.dataset.edit));
});
formSubmit("#product-form", async (f, form) => {
  const body = { name: f.get("name"), category: f.get("category"), description: f.get("description"),
    unit_price: parseFloat(String(f.get("unit_price")).replace(",", ".")), vat_rate: parseFloat(f.get("vat_rate")), active: form.active.checked };
  if (!(body.unit_price > 0)) throw new Error("Indiquez un prix de vente HT.");
  if (app.editing) {
    await api(`/api/products/${encodeURIComponent(app.editing)}`, { method: "PATCH", body });
    toast("Produit mis à jour.");
  } else {
    const p = await api("/api/products", { method: "POST", body: { sku: f.get("sku"), ...body } });
    toast(`${p.name} ajouté au catalogue. Stock à zéro jusqu'à sa première réception.`);
  }
  form.hidden = true;
  app.editing = null;
});

// ------------------------------------------------------------------ rafraîchissement
async function refresh(force = false) {
  if (!app.me || (app.busy && !force)) return;
  app.busy = true;
  try {
    if (app.me.role === "shop") {
      const [catalog, cart, orders, invoices] = await Promise.all([api("/api/catalog"), api("/api/cart"), api("/api/orders"), api("/api/invoices")]);
      const first = !app.catalog.length;
      Object.assign(app, { catalog, orders, invoices });
      if (!app.pendingQty.size) app.cart = cart;
      rememberDeadlines(orders);
      if (first) renderCategories();
      ({ catalog: renderCatalog, cart: renderCart, orders: renderOrders, invoices: renderInvoices })[app.view]?.();
      if (app.view === "ai" && $("#ai-token-out").hidden) renderAi();
      renderDock();
    } else {
      const v = app.view;
      const [orders, shops, pickings, invoices] = await Promise.all([
        api("/api/admin/orders"), api("/api/admin/shops"), api("/api/warehouse/pickings"), api("/api/invoices")]);
      Object.assign(app, { allOrders: orders, shops, pickings, invoices });
      if (v === "picking") app.shipments = await api("/api/warehouse/shipments");
      if (v === "receiving") {
        [app.stock, app.receptions, app.adjustments] = await Promise.all([
          api("/api/warehouse/stock"), api("/api/warehouse/receptions"), api("/api/warehouse/adjustments")]);
      }
      if (v === "products") app.products = await api("/api/products");
      rememberDeadlines(orders);
      ({ overview: renderOverview, shops: renderShops, "all-orders": renderAllOrders, picking: renderPicking,
        receiving: renderReceiving, products: renderProducts, invoices: renderInvoices })[v]?.();
      if (v === "ai" && $("#ai-token-out").hidden) renderAi();
      $("#dock").hidden = true;
    }
    renderNav();
  } catch (err) {
    if (err.message !== "Connexion requise") console.warn(err);
  } finally {
    app.busy = false;
  }
}

setInterval(() => {
  if (!app.me) return;
  for (const el of $$("[data-timer]")) el.textContent = clock(secondsLeft(el.dataset.timer));
  if (app.me.role === "shop") renderDock();
  if ([...app.deadlines.keys()].some((id) => secondsLeft(id) === 0)) refresh();
}, 1000);
setInterval(() => document.visibilityState === "visible" && !$("#product-form").contains(document.activeElement) && refresh(), 5000);

// ------------------------------------------------------------------ démarrage
(async () => {
  app.config = await api("/api/auth/config").catch(() => ({ provider: "local" }));
  const err = new URLSearchParams(location.search).get("auth_error");
  if (err) history.replaceState(null, "", location.pathname + location.hash);
  try { start(await api("/api/auth/me")); } catch { showLogin(err); }
})();
