// Portail B2B : consomme uniquement l'API publique exposée par la passerelle.
const SHOPS = [
  { id: "SHOP-BIA", name: "Surf Shack Biarritz (éditeur A)", key: "demo-key-biarritz" },
  { id: "SHOP-HOS", name: "Hossegor Board Store (éditeur A)", key: "demo-key-hossegor" },
  { id: "SHOP-LAC", name: "Lacanau Glisse (éditeur B)", key: "demo-key-lacanau" },
  { id: "SHOP-ARC", name: "Arcachon Wave (éditeur B)", key: "demo-key-arcachon" },
];
const STATUS = {
  confirmed: ["Entièrement réservée", "ok"],
  backordered: ["Avec reliquat", "warn"],
  partially_shipped: ["Partiellement expédiée", "info"],
  shipped: ["Expédiée", "ok"],
};

const $ = (sel) => document.querySelector(sel);
const euro = (n) => n.toLocaleString("fr-FR", { style: "currency", currency: "EUR" });
const frDate = (iso) => (iso ? new Date(iso + "T12:00:00").toLocaleDateString("fr-FR") : "date à confirmer");
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

let shopKey = SHOPS[0].key;
let view = "shop";
let catalog = [];

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 3500);
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-API-Key": shopKey, ...(options.headers || {}) },
  });
  const body = resp.headers.get("content-type")?.includes("json") ? await resp.json() : null;
  if (!resp.ok) {
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail : detail?.message || `Erreur ${resp.status}`);
  }
  return body;
}

// ------------------------------------------------------------------ boutique
function readCart() {
  return [...document.querySelectorAll("#catalog input")]
    .map((i) => ({ sku: i.dataset.sku, qty: parseInt(i.value, 10) || 0 }))
    .filter((l) => l.qty > 0);
}

function updateCart() {
  const cart = readCart();
  const total = cart.reduce((s, l) => s + l.qty * catalog.find((p) => p.sku === l.sku).unit_price, 0);
  $("#cart-summary").textContent = cart.length
    ? `${cart.length} référence(s) · ${euro(total)} HT`
    : "Panier vide";
  $("#order-btn").disabled = !cart.length;
}

async function loadCatalog() {
  const previous = Object.fromEntries(readCart().map((l) => [l.sku, l.qty]));
  catalog = await api("/api/catalog");
  $("#catalog tbody").innerHTML = catalog
    .map((p) => {
      const avail = p.available_now > 0
        ? `<span class="pill ok">${p.available_now}</span>`
        : `<span class="pill bad">Rupture</span>`;
      const lots = p.upcoming.length
        ? p.upcoming.map((u) => `${u.available} le ${frDate(u.eta)}`).join("<br>")
        : "—";
      return `<tr>
        <td class="sku">${esc(p.sku)}</td><td>${esc(p.name)}</td>
        <td class="num">${euro(p.unit_price)}</td><td class="num">${avail}</td>
        <td class="hint">${lots}</td>
        <td class="num"><input type="number" min="0" max="10000" data-sku="${esc(p.sku)}"
            value="${previous[p.sku] || ""}" aria-label="Quantité ${esc(p.name)}"></td></tr>`;
    })
    .join("");
  document.querySelectorAll("#catalog input").forEach((i) => i.addEventListener("input", updateCart));
  $("#lot-sku").innerHTML = catalog.map((p) => `<option value="${esc(p.sku)}">${esc(p.sku)} · ${esc(p.name)}</option>`).join("");
  updateCart();
}

function describeLine(l) {
  const parts = [];
  if (l.qty_shipped) parts.push(`${l.qty_shipped} expédié(s)`);
  if (l.qty_reserved) parts.push(`${l.qty_reserved} réservé(s)`);
  l.scheduled.forEach((s) => parts.push(`${s.qty} livrable(s) le ${frDate(s.eta)}`));
  if (l.qty_unscheduled) parts.push(`${l.qty_unscheduled} en attente de production`);
  return `<li><span>${esc(l.name)} × ${l.qty_ordered}</span> : ${parts.join(", ")}</li>`;
}

function renderOrder(o) {
  const [label, tone] = STATUS[o.status];
  return `<div class="order">
    <div class="order-head"><strong>${esc(o.order_id)}</strong>
      <span class="pill ${tone}">${label}</span>
      <span class="hint">${new Date(o.created_at).toLocaleString("fr-FR")}</span>
      <span class="amount">${euro(o.total_amount)} HT</span></div>
    <ul>${o.lines.map(describeLine).join("")}</ul></div>`;
}

async function loadOrders() {
  const orders = await api("/api/orders");
  $("#orders").innerHTML = orders.length ? orders.map(renderOrder).join("") : `<p class="empty">Aucune commande pour l'instant.</p>`;
}

async function placeOrder() {
  const lines = readCart();
  $("#order-btn").disabled = true;
  try {
    const order = await api("/api/orders", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ lines }),
    });
    $("#order-result").hidden = false;
    $("#order-result").innerHTML = `<strong>Commande ${esc(order.order_id)} enregistrée.</strong>${renderOrder(order)}`;
    document.querySelectorAll("#catalog input").forEach((i) => (i.value = ""));
    toast("Commande confirmée, stock réservé");
    await Promise.all([loadCatalog(), loadOrders()]);
  } catch (e) {
    toast(e.message);
  } finally {
    updateCart();
  }
}

// ------------------------------------------------------------------ entrepôt
async function loadWarehouse() {
  const [pickings, stock, shipments] = await Promise.all([
    api("/erp/pickings"), api("/erp/stock"), api("/erp/shipments"),
  ]);
  $("#pickings").innerHTML = pickings.length
    ? `<table class="table"><thead><tr><th>Commande</th><th>Boutique</th><th>À préparer</th></tr></thead><tbody>${pickings
        .map((p) => `<tr><td class="sku">${esc(p.order_id)}</td><td>${esc(p.shop?.name || "?")}</td>
          <td>${p.lines.map((l) => `${l.qty} × ${esc(l.name)}`).join("<br>")}</td></tr>`).join("")}</tbody></table>`
    : `<p class="empty">Rien à préparer.</p>`;
  $("#dispatch-btn").disabled = !pickings.length;

  const names = Object.fromEntries(stock.products.map((p) => [p.sku, p.name]));
  $("#lots").innerHTML = stock.lots.length
    ? `<table class="table"><tbody>${stock.lots
        .map((l) => `<tr><td>${esc(names[l.sku] || l.sku)}<br><span class="sku">${esc(l.id)}</span></td>
          <td class="num">${l.qty}</td><td>${frDate(l.eta)}</td>
          <td class="num"><button class="ghost" data-lot="${esc(l.id)}">Réceptionner</button></td></tr>`).join("")}</tbody></table>`
    : `<p class="empty">Aucun lot attendu.</p>`;
  document.querySelectorAll("[data-lot]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api("/erp/receptions", { method: "POST", body: JSON.stringify({ lot_id: b.dataset.lot }) });
      toast(`Lot ${b.dataset.lot} réceptionné : reliquats réservés`);
      setTimeout(refresh, 400);
    }));

  $("#shipments").innerHTML = shipments.length
    ? `<table class="table"><thead><tr><th>Bon de livraison</th><th>Commande</th><th>Contenu</th></tr></thead><tbody>${shipments
        .map((s) => `<tr><td class="sku">${esc(s.id)}</td><td class="sku">${esc(s.order_id)}</td>
          <td>${s.lines.map((l) => `${l.qty} × ${esc(l.name)}`).join("<br>")}</td></tr>`).join("")}</tbody></table>`
    : `<p class="empty">Aucune expédition.</p>`;
}

// ------------------------------------------------------------------ général
async function refresh() {
  try {
    if (view === "shop") await Promise.all([loadCatalog(), loadOrders()]);
    else await Promise.all([loadWarehouse(), catalog.length ? null : loadCatalog()]);
  } catch (e) {
    toast(e.message);
  }
}

function init() {
  $("#shop").innerHTML = SHOPS.map((s) => `<option value="${s.key}">${esc(s.name)}</option>`).join("");
  $("#shop").addEventListener("change", (e) => {
    shopKey = e.target.value;
    $("#order-result").hidden = true;
    refresh();
  });
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => {
      view = t.dataset.view;
      document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
      $("#view-shop").hidden = view !== "shop";
      $("#view-warehouse").hidden = view !== "warehouse";
      refresh();
    }));
  $("#order-btn").addEventListener("click", placeOrder);
  $("#dispatch-btn").addEventListener("click", async () => {
    const shipped = await api("/erp/pickings/dispatch", { method: "POST", body: "{}" });
    toast(`${shipped.length} colis expédié(s), bons de livraison transmis aux boutiques`);
    setTimeout(refresh, 400);
  });
  $("#lot-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const lot = await api("/erp/lots", {
        method: "POST",
        body: JSON.stringify({ sku: f.get("sku"), qty: parseInt(f.get("qty"), 10), eta: f.get("eta") }),
      });
      toast(`Lot ${lot.id} annoncé : les reliquats en attente sont planifiés`);
      setTimeout(refresh, 400);
    } catch (err) {
      toast(err.message);
    }
  });
  const eta = new Date(Date.now() + 21 * 864e5).toISOString().slice(0, 10);
  document.querySelector('#lot-form [name="eta"]').value = eta;
  refresh();
  setInterval(() => document.activeElement?.tagName !== "INPUT" && refresh(), 5000);
}

init();
