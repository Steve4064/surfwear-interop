// Portail Surfwear Pro : boutiques (catalogue, pré-réservation, suivi) et équipe Surfwear.
// La session est un cookie HttpOnly posé par /api/auth/login ; aucun secret n'est stocké ici.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const euro = (n) => n.toLocaleString("fr-FR", { style: "currency", currency: "EUR" });
const frDate = (iso) => (iso ? new Date(iso + "T12:00:00").toLocaleDateString("fr-FR", { day: "numeric", month: "short" }) : "date à confirmer");
const frDateTime = (iso) => new Date(iso).toLocaleString("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const clock = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
const plural = (n, one, many) => `${n} ${n > 1 ? many : one}`;

const STATE = {
  pending: ["Pré-réservée", "pending"],
  confirmed: ["Finalisée, réservée", "ok"],
  backordered: ["Finalisée, avec reliquat", "warn"],
  partially_shipped: ["Partiellement expédiée", "info"],
  shipped: ["Expédiée", "ok"],
  expired: ["Expirée", "off"],
  cancelled: ["Annulée", "off"],
};
const DEMO = [
  ["biarritz", "Biarritz2026!", "Surf Shack Biarritz"],
  ["hossegor", "Hossegor2026!", "Hossegor Board Store"],
  ["lacanau", "Lacanau2026!", "Lacanau Glisse"],
  ["arcachon", "Arcachon2026!", "Arcachon Wave"],
  ["surfwear", "Surfwear2026!", "Équipe Surfwear"],
];

const app = {
  me: null,
  view: null,
  catalog: [],
  cart: new Map(), // sku -> qty
  category: "Tout",
  search: "",
  orders: [],
  orderFilter: "all",
  open: new Set(), // commandes dépliées
  deadlines: new Map(), // order_id -> timestamp de fin
  allOrders: [],
  shops: [],
  shopFilter: "",
  stateFilter: "active",
  pickings: [],
  stock: { products: [], lots: [] },
  shipments: [],
  busy: false,
};

// ------------------------------------------------------------------ outils
function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(path, { method = "GET", body, headers = {} } = {}) {
  const resp = await fetch(path, {
    method,
    credentials: "same-origin",
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
    const msg = typeof d === "string" ? d : d?.message
      ? `${d.message}${d.skus ? " : " + d.skus.join(", ") : ""}`
      : resp.status === 429 ? "Trop de tentatives, patientez une minute." : `Erreur ${resp.status}`;
    throw new Error(msg);
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

// ------------------------------------------------------------------ connexion
function showLogin(message) {
  app.me = null;
  $("#app-view").hidden = true;
  $("#login-view").hidden = false;
  const err = $("#login-error");
  err.hidden = !message;
  err.textContent = message || "";
  const local = ["localhost", "127.0.0.1"].includes(location.hostname);
  $(".demo-accounts").hidden = !local;
  $("#demo-list").innerHTML = DEMO.map(([u, p, n]) =>
    `<li><button type="button" data-user="${u}" data-pass="${esc(p)}"><span>${esc(n)}</span><span class="hint">${u}</span></button></li>`).join("");
  setTimeout(() => $('#login-form [name="username"]').focus(), 0);
}

$("#demo-list").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-user]");
  if (!b) return;
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

$("#logout").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  app.cart.clear();
  showLogin();
});

// ------------------------------------------------------------------ navigation
const NAV = {
  shop: [["catalog", "Catalogue"], ["orders", "Mes commandes"]],
  staff: [["overview", "Vue d'ensemble"], ["all-orders", "Commandes"], ["picking", "Préparation"], ["production", "Production"]],
};

function start(me) {
  app.me = me;
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  $("#who-name").textContent = me.shop ? me.shop.name : me.display_name;
  $("#hold-minutes").textContent = plural(Math.round(me.hold_minutes), "minute", "minutes");
  renderNav();
  go(me.role === "staff" ? "overview" : "catalog");
}

function renderNav() {
  const pending = app.orders.filter((o) => o.state === "pending").length;
  const picks = app.pickings.length;
  $("#nav").innerHTML = NAV[app.me.role].map(([id, label]) => {
    const badge = id === "orders" && pending ? `<span class="badge">${pending}</span>`
      : id === "picking" && picks ? `<span class="badge">${picks}</span>` : "";
    return `<button data-go="${id}" ${app.view === id ? 'aria-current="page"' : ""}>${label}${badge}</button>`;
  }).join("");
}

$("#nav").addEventListener("click", (e) => {
  const b = e.target.closest("[data-go]");
  if (b) go(b.dataset.go);
});

function go(view) {
  app.view = view;
  $$("[data-view]").forEach((s) => (s.hidden = s.dataset.view !== view));
  renderNav();
  refresh(true);
  window.scrollTo({ top: 0 });
}

// ------------------------------------------------------------------ boutique : catalogue
function planFor(p, qty) {
  let rest = qty;
  const now = Math.min(rest, Math.max(p.available_now, 0));
  rest -= now;
  const later = [];
  for (const u of p.upcoming) {
    if (!rest) break;
    const t = Math.min(rest, u.available);
    if (t) { later.push([t, u.eta]); rest -= t; }
  }
  return { now, later, none: rest };
}

function previewHtml(p, qty) {
  if (!qty) return "";
  const { now, later, none } = planFor(p, qty);
  const parts = [];
  if (now) parts.push(`<span class="now">${now} tout de suite</span>`);
  for (const [t, eta] of later) parts.push(`<span class="later">${t} le ${frDate(eta)}</span>`);
  if (none) parts.push(`<span class="none">${none} sans date de production</span>`);
  return parts.join(" · ");
}

function stockHtml(p) {
  const max = Math.max(...app.catalog.map((x) => x.available_now), 1);
  const lots = p.upcoming.length
    ? p.upcoming.map((u) => `+${u.available} le ${frDate(u.eta)}`).join(", ")
    : p.available_now ? "" : "Aucun lot prévu";
  return `<div class="stock">
    <div class="stock-line"><span class="stock-qty ${p.available_now ? "" : "zero"}">${p.available_now}</span>
      <span class="hint">${p.available_now ? "disponibles" : "en rupture"}</span></div>
    <div class="stock-bar"><i style="width:${Math.round((p.available_now / max) * 100)}%"></i></div>
    ${lots ? `<div class="lots">${lots}</div>` : ""}</div>`;
}

function renderCategories() {
  const cats = ["Tout", ...new Set(app.catalog.map((p) => p.category))];
  $("#categories").innerHTML = cats.map((c) =>
    `<button type="button" data-cat="${esc(c)}" aria-pressed="${c === app.category}">${esc(c)}</button>`).join("");
}

function visible(p) {
  const q = app.search.trim().toLowerCase();
  return (app.category === "Tout" || p.category === app.category)
    && (!q || p.name.toLowerCase().includes(q) || p.sku.toLowerCase().includes(q));
}

function renderCatalog() {
  const body = $("#catalog-body");
  const known = new Set($$("tr[data-sku]", body).map((r) => r.dataset.sku));
  const same = app.catalog.length === known.size && app.catalog.every((p) => known.has(p.sku));
  if (!same) {
    body.innerHTML = app.catalog.map((p) => `
      <tr data-sku="${esc(p.sku)}">
        <td><div class="name">${esc(p.name)}</div><div class="sku">${esc(p.sku)} · ${esc(p.category)}</div>
          <div class="preview" aria-live="polite"></div></td>
        <td class="num price">${euro(p.unit_price)}</td>
        <td class="avail"></td>
        <td class="num qty"><input class="qty-input" type="text" inputmode="numeric" autocomplete="off"
          value="${app.cart.get(p.sku) || 0}" aria-label="Quantité de ${esc(p.name)}"></td>
      </tr>`).join("");
  }
  let shown = 0;
  for (const p of app.catalog) {
    const row = $(`tr[data-sku="${CSS.escape(p.sku)}"]`, body);
    row.hidden = !visible(p);
    shown += !row.hidden;
    $(".avail", row).innerHTML = stockHtml(p);
    const qty = app.cart.get(p.sku) || 0;
    row.classList.toggle("picked", qty > 0);
    $(".preview", row).innerHTML = previewHtml(p, qty);
    const input = $(".qty-input", row);
    if (document.activeElement !== input) input.value = qty;
  }
  $("#catalog-empty").hidden = shown > 0;
}

$("#catalog-body").addEventListener("focusin", (e) => {
  if (e.target.classList.contains("qty-input")) e.target.select();
});
$("#catalog-body").addEventListener("input", (e) => {
  const input = e.target;
  if (!input.classList.contains("qty-input")) return;
  const digits = input.value.replace(/\D/g, "").replace(/^0+(?=\d)/, "").slice(0, 5);
  const qty = Math.min(parseInt(digits || "0", 10), 10000);
  if (input.value !== digits) input.value = digits;
  const sku = input.closest("tr").dataset.sku;
  if (qty) app.cart.set(sku, qty); else app.cart.delete(sku);
  const p = app.catalog.find((x) => x.sku === sku);
  const row = input.closest("tr");
  row.classList.toggle("picked", qty > 0);
  $(".preview", row).innerHTML = previewHtml(p, qty);
  renderDock();
});
$("#catalog-body").addEventListener("focusout", (e) => {
  if (e.target.classList.contains("qty-input") && e.target.value === "") e.target.value = "0";
});
$("#catalog-body").addEventListener("keydown", (e) => {
  if (!e.target.classList.contains("qty-input")) return;
  if (e.key === "ArrowUp" || e.key === "ArrowDown") e.preventDefault(); // saisie au clavier uniquement
  if (e.key === "Enter") {
    const inputs = $$(".qty-input").filter((i) => !i.closest("tr").hidden);
    inputs[inputs.indexOf(e.target) + 1]?.focus();
  }
});
$("#catalog-body").addEventListener("wheel", (e) => {
  if (e.target.classList.contains("qty-input")) e.target.blur();
}, { passive: true });

$("#search").addEventListener("input", (e) => { app.search = e.target.value; renderCatalog(); });
$("#categories").addEventListener("click", (e) => {
  const b = e.target.closest("[data-cat]");
  if (!b) return;
  app.category = b.dataset.cat;
  renderCategories();
  renderCatalog();
});

// ------------------------------------------------------------------ bandeau panier et compte à rebours
function renderDock() {
  if (!app.me || app.me.role !== "shop") { $("#dock").hidden = true; return; }
  const pending = app.orders.filter((o) => o.state === "pending" && secondsLeft(o.order_id) > 0)
    .sort((a, b) => secondsLeft(a.order_id) - secondsLeft(b.order_id));
  const lines = [...app.cart.entries()];
  $("#dock").hidden = !pending.length && !lines.length;

  const hold = $("#dock-hold");
  hold.hidden = !pending.length;
  if (pending.length) {
    const o = pending[0];
    const left = secondsLeft(o.order_id);
    const total = app.me.hold_minutes * 60;
    const late = left < 120;
    const qty = o.lines.reduce((s, l) => s + l.qty_ordered, 0);
    const key = `${o.order_id}`;
    if (hold.dataset.order !== key) {
      hold.dataset.order = key;
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
    bar.style.width = `${Math.min(100, (left / total) * 100)}%`;
    bar.classList.toggle("late", late);
    $("[data-more]", hold).textContent = pending.length > 1
      ? `${plural(pending.length - 1, "autre pré-réservation", "autres pré-réservations")} en attente : voir Mes commandes.` : "";
  } else {
    hold.dataset.order = "";
  }

  const cart = $("#dock-cart");
  cart.hidden = !lines.length;
  if (lines.length) {
    let total = 0, units = 0, late = 0;
    for (const [sku, qty] of lines) {
      const p = app.catalog.find((x) => x.sku === sku);
      if (!p) continue;
      total += p.unit_price * qty;
      units += qty;
      late += qty - planFor(p, qty).now;
    }
    $("#cart-count").textContent = `${plural(units, "article", "articles")} · ${euro(total)} HT`;
    $("#cart-detail").textContent = `${plural(lines.length, "référence", "références")}` +
      (late ? ` · ${plural(late, "pièce", "pièces")} en reliquat` : " · tout est disponible");
  }
}

$("#cart-clear").addEventListener("click", () => {
  app.cart.clear();
  renderCatalog();
  renderDock();
});

$("#cart-reserve").addEventListener("click", async () => {
  const lines = [...app.cart.entries()].map(([sku, qty]) => ({ sku, qty }));
  if (!lines.length) return;
  const btn = $("#cart-reserve");
  btn.disabled = true;
  try {
    const order = await api("/api/orders", { method: "POST", body: { lines }, headers: { "Idempotency-Key": crypto.randomUUID() } });
    app.cart.clear();
    app.orders = [order, ...app.orders.filter((o) => o.order_id !== order.order_id)];
    rememberDeadlines([order]);
    toast(`Stock bloqué ${plural(Math.round(app.me.hold_minutes), "minute", "minutes")}. Finalisez la commande pour la valider.`);
    await refresh(true);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
  }
});

async function orderAction(act, id) {
  if (act === "details") {
    app.open.add(id);
    app.orderFilter = "pending";
    go("orders");
    return;
  }
  if (act === "cancel" && !confirm(`Annuler la pré-réservation ${id} ? Le stock sera remis à disposition des autres boutiques.`)) return;
  try {
    const o = await api(`/api/orders/${encodeURIComponent(id)}/${act}`, { method: "POST" });
    if (app.orderFilter === "pending") app.orderFilter = "all";
    app.open.add(o.order_id);
    toast(act === "confirm" ? `Commande ${o.order_id} finalisée : elle part en préparation.` : `Pré-réservation ${o.order_id} annulée.`);
  } catch (err) {
    toast(err.message, "error");
  }
  await refresh(true);
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-act]");
  if (b && b.dataset.id) orderAction(b.dataset.act, b.dataset.id);
});

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
  const names = ["Pré-réservée", "Finalisée", "En livraison", "Expédiée"];
  return `<ol class="steps">${names.map((n, i) =>
    `<li class="${i < idx ? "done" : i === idx ? "current" : ""}">${n}</li>`).join("")}</ol>`;
}

function renderOrders() {
  $("#order-filter").innerHTML = FILTERS.map(([id, label, f]) => {
    const n = app.orders.filter(f).length;
    return `<button type="button" data-filter="${id}" aria-pressed="${app.orderFilter === id}">${label}<span class="count">${n}</span></button>`;
  }).join("");
  const f = FILTERS.find(([id]) => id === app.orderFilter)[2];
  const list = app.orders.filter(f);
  $("#orders-list").innerHTML = list.length ? list.map((o) => `
    <details class="order" data-id="${esc(o.order_id)}" ${app.open.has(o.order_id) ? "open" : ""}>
      <summary>
        <div><div class="order-id">${esc(o.order_id)}</div>
          <div class="order-meta">${frDateTime(o.created_at)} · ${plural(o.lines.length, "référence", "références")}</div></div>
        <div>${stateBadge(o)}</div>
        <div class="num amount">${euro(o.total_amount)} HT</div>
      </summary>
      <div class="order-body">
        ${steps(o)}
        ${o.state === "expired" ? `<p class="hint">Non finalisée à temps : le stock a été remis à disposition des autres boutiques.</p>` : ""}
        <table class="lines"><tbody>${o.lines.map((l) => `
          <tr><td><span class="name">${esc(l.name)}</span><div class="sku">${esc(l.sku)}</div></td>
            <td class="num">${l.qty_ordered} × ${euro(l.unit_price)}</td>
            <td>${lineSummary(l)}</td></tr>`).join("")}</tbody></table>
        ${o.state === "pending" ? `<div class="order-actions">
          <button class="btn btn-primary" data-act="confirm" data-id="${esc(o.order_id)}">Finaliser la commande</button>
          <button class="btn btn-danger" data-act="cancel" data-id="${esc(o.order_id)}">Annuler</button></div>` : ""}
      </div>
    </details>`).join("")
    : `<p class="empty">${app.orders.length ? "Aucune commande dans cette catégorie." : "Aucune commande pour l'instant. Commencez par le catalogue."}</p>`;
}

$("#order-filter").addEventListener("click", (e) => {
  const b = e.target.closest("[data-filter]");
  if (!b) return;
  app.orderFilter = b.dataset.filter;
  renderOrders();
});
$("#orders-list").addEventListener("toggle", (e) => {
  const id = e.target.dataset.id;
  if (id) e.target.open ? app.open.add(id) : app.open.delete(id);
}, true);

// ------------------------------------------------------------------ Surfwear
function renderOverview() {
  const pending = app.allOrders.filter((o) => o.state === "pending");
  const backlog = app.allOrders.filter((o) => o.status === "backordered" || o.status === "partially_shipped")
    .reduce((s, o) => s + o.lines.reduce((t, l) => t + l.qty_unscheduled + l.scheduled.reduce((u, x) => u + x.qty, 0), 0), 0);
  const shipped = app.allOrders.filter((o) => o.status === "shipped").length;
  const kpis = [
    [pending.length, "pré-réservations en cours", "all-orders", "pending", pending.length > 0],
    [app.pickings.length, "commandes à préparer", "picking", null, false],
    [backlog, "pièces en reliquat", "all-orders", "active", false],
    [shipped, "commandes expédiées", "all-orders", "shipped", false],
  ];
  $("#kpis").innerHTML = kpis.map(([n, label, view, filter, hot]) =>
    `<button class="kpi ${hot ? "hot" : ""}" data-kpi="${view}" data-filter="${filter ?? ""}"><b>${n}</b><span>${label}</span></button>`).join("");
  $("#overview-pending").innerHTML = pending.length ? `<div class="table-wrap"><table class="table"><thead>
      <tr><th>Commande</th><th>Boutique</th><th>Temps restant</th><th class="num">Montant HT</th></tr></thead><tbody>
      ${pending.map((o) => `<tr><td>${esc(o.order_id)}</td><td>${esc(o.shop.name)}</td><td>${stateBadge(o)}</td>
        <td class="num">${euro(o.total_amount)}</td></tr>`).join("")}</tbody></table></div>`
    : `<p class="empty">Aucune pré-réservation en attente de finalisation.</p>`;
  const recent = app.allOrders.filter((o) => o.state !== "pending").slice(0, 8);
  $("#overview-recent").innerHTML = recent.length ? `<div class="table-wrap"><table class="table"><thead>
      <tr><th>Commande</th><th>Boutique</th><th>État</th><th class="num">Montant HT</th></tr></thead><tbody>
      ${recent.map((o) => `<tr><td>${esc(o.order_id)}<div class="sku">${frDateTime(o.created_at)}</div></td>
        <td>${esc(o.shop.name)}</td><td>${stateBadge(o)}</td><td class="num">${euro(o.total_amount)}</td></tr>`).join("")}
      </tbody></table></div>` : `<p class="empty">Aucune commande pour l'instant.</p>`;
}
$("#kpis").addEventListener("click", (e) => {
  const b = e.target.closest("[data-kpi]");
  if (!b) return;
  if (b.dataset.filter) app.stateFilter = b.dataset.filter;
  go(b.dataset.kpi);
});

const STAFF_FILTERS = [
  ["active", "En cours", (o) => !["shipped", "expired", "cancelled"].includes(o.status)],
  ["pending", "Pré-réservées", (o) => o.state === "pending"],
  ["shipped", "Expédiées", (o) => o.status === "shipped"],
  ["closed", "Expirées ou annulées", (o) => ["expired", "cancelled"].includes(o.state)],
  ["all", "Toutes", () => true],
];

function renderAllOrders() {
  const sel = $("#shop-filter");
  if (sel.options.length !== app.shops.length + 1) {
    sel.innerHTML = `<option value="">Toutes les boutiques</option>` +
      app.shops.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
    sel.value = app.shopFilter;
  }
  const byShop = app.allOrders.filter((o) => !app.shopFilter || o.shop.id === app.shopFilter);
  $("#state-filter").innerHTML = STAFF_FILTERS.map(([id, label, f]) =>
    `<button type="button" data-state="${id}" aria-pressed="${app.stateFilter === id}">${label}<span class="count">${byShop.filter(f).length}</span></button>`).join("");
  const f = STAFF_FILTERS.find(([id]) => id === app.stateFilter)[2];
  const rows = byShop.filter(f);
  $("#all-orders-body").innerHTML = rows.length ? rows.map((o) => `
    <tr><td><div class="order-id">${esc(o.order_id)}</div><div class="sku">${frDateTime(o.created_at)}</div></td>
      <td>${esc(o.shop.name)}</td><td>${stateBadge(o)}</td>
      <td>${o.lines.map((l) => `<div>${l.qty_ordered} × ${esc(l.name)} <span class="hint">${lineSummary(l)}</span></div>`).join("")}</td>
      <td class="num">${euro(o.total_amount)}</td></tr>`).join("")
    : `<tr><td colspan="5" class="empty">Aucune commande.</td></tr>`;
}
$("#shop-filter").addEventListener("change", (e) => { app.shopFilter = e.target.value; renderAllOrders(); });
$("#state-filter").addEventListener("click", (e) => {
  const b = e.target.closest("[data-state]");
  if (!b) return;
  app.stateFilter = b.dataset.state;
  renderAllOrders();
});

function renderPicking() {
  $("#dispatch-all").disabled = !app.pickings.length;
  $("#pickings").innerHTML = app.pickings.length ? app.pickings.map((p) => `
    <div class="pick"><div><div class="order-id">${esc(p.order_id)} · ${esc(p.shop?.name || "")}</div>
      <ul>${p.lines.map((l) => `<li>${l.qty} × ${esc(l.name)} <span class="sku">${esc(l.sku)}</span></li>`).join("")}</ul></div>
      <button class="btn btn-ghost btn-small" data-ship="${esc(p.order_id)}">Expédier ce colis</button></div>`).join("")
    : `<p class="empty">Rien à préparer pour l'instant.</p>`;
  $("#shipments").innerHTML = app.shipments.length ? `<div class="table-wrap"><table class="table"><thead>
      <tr><th>Bon de livraison</th><th>Commande</th><th>Contenu</th><th>Expédié le</th></tr></thead><tbody>
      ${app.shipments.slice(0, 30).map((s) => `<tr><td>${esc(s.id)}</td><td>${esc(s.order_id)}</td>
        <td>${s.lines.map((l) => `${l.qty} × ${esc(l.name)}`).join("<br>")}</td><td>${frDateTime(s.dispatched_at)}</td></tr>`).join("")}
      </tbody></table></div>` : `<p class="empty">Aucune expédition.</p>`;
}

async function dispatch(orderIds) {
  try {
    const sent = await api("/erp/pickings/dispatch", { method: "POST", body: orderIds ? { order_ids: orderIds } : {} });
    toast(`${plural(sent.length, "colis expédié", "colis expédiés")}, bons de livraison transmis aux boutiques.`);
  } catch (err) {
    toast(err.message, "error");
  }
  setTimeout(() => refresh(true), 400);
}
$("#dispatch-all").addEventListener("click", () => dispatch(null));
$("#pickings").addEventListener("click", (e) => {
  const b = e.target.closest("[data-ship]");
  if (b) dispatch([b.dataset.ship]);
});

function renderProduction() {
  const names = Object.fromEntries(app.stock.products.map((p) => [p.sku, p.name]));
  const sel = $("#lot-sku");
  if (sel.options.length !== app.stock.products.length) {
    sel.innerHTML = app.stock.products.map((p) => `<option value="${esc(p.sku)}">${esc(p.name)}</option>`).join("");
  }
  $("#lots").innerHTML = app.stock.lots.length ? `<table class="lines"><tbody>${app.stock.lots.map((l) => `
      <tr><td><span class="name">${esc(names[l.sku] || l.sku)}</span><div class="sku">${esc(l.id)}</div></td>
        <td class="num">${l.qty} pièces</td><td>${frDate(l.eta)}</td>
        <td class="num"><button class="btn btn-ghost btn-small" data-receive="${esc(l.id)}">Réceptionner</button></td></tr>`).join("")}
      </tbody></table>` : `<p class="empty">Aucun lot attendu.</p>`;
}
$("#lots").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-receive]");
  if (!b) return;
  b.disabled = true;
  try {
    await api("/erp/receptions", { method: "POST", body: { lot_id: b.dataset.receive } });
    toast(`Lot ${b.dataset.receive} réceptionné : les reliquats correspondants sont réservés.`);
  } catch (err) { toast(err.message, "error"); }
  setTimeout(() => refresh(true), 400);
});
$("#lot-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const qty = parseInt(String(f.get("qty")).replace(/\D/g, ""), 10);
  if (!qty) { toast("Indiquez une quantité.", "error"); return; }
  try {
    const lot = await api("/erp/lots", { method: "POST", body: { sku: f.get("sku"), qty, eta: f.get("eta") } });
    toast(`Lot ${lot.id} annoncé : les reliquats en attente reçoivent sa date.`);
  } catch (err) { toast(err.message, "error"); }
  setTimeout(() => refresh(true), 400);
});
$('#lot-form [name="eta"]').value = new Date(Date.now() + 21 * 864e5).toISOString().slice(0, 10);

// ------------------------------------------------------------------ rafraîchissement
async function refresh(force = false) {
  if (!app.me || (app.busy && !force)) return;
  app.busy = true;
  try {
    if (app.me.role === "shop") {
      const [catalog, orders] = await Promise.all([api("/api/catalog"), api("/api/orders")]);
      const first = !app.catalog.length;
      app.catalog = catalog;
      app.orders = orders;
      rememberDeadlines(orders);
      if (first) renderCategories();
      if (app.view === "catalog") renderCatalog();
      if (app.view === "orders") renderOrders();
      renderDock();
    } else {
      const [orders, shops, pickings, stock, shipments] = await Promise.all([
        api("/api/admin/orders"), app.shops.length ? app.shops : api("/api/admin/shops"),
        api("/erp/pickings"), api("/erp/stock"), api("/erp/shipments"),
      ]);
      Object.assign(app, { allOrders: orders, shops, pickings, stock, shipments });
      rememberDeadlines(orders);
      ({ overview: renderOverview, "all-orders": renderAllOrders, picking: renderPicking, production: renderProduction })[app.view]?.();
      $("#dock").hidden = true;
    }
    renderNav();
  } catch (err) {
    if (err.message !== "Connexion requise") console.warn(err);
  } finally {
    app.busy = false;
  }
}

// Compte à rebours : chaque seconde, sans recharger les données
setInterval(() => {
  if (!app.me) return;
  for (const el of $$("[data-timer]")) el.textContent = clock(secondsLeft(el.dataset.timer));
  if (app.me.role === "shop") renderDock();
  const expired = [...app.deadlines.keys()].some((id) => secondsLeft(id) === 0);
  if (expired) refresh();
}, 1000);
setInterval(() => document.visibilityState === "visible" && refresh(), 4000);

// ------------------------------------------------------------------ démarrage
api("/api/auth/me").then(start).catch(() => showLogin());
