"use strict";

/**
 * Mobil POS frontend — MoySklad'ga to'g'ridan-to'g'ri emas, faqat o'z backend
 * proksisi (/api/...) orqali murojaat qiladi. Token brauzerga hech qachon kelmaydi.
 */

const API = {
  login: "/api/login",
  logout: "/api/logout",
  me: "/api/me",
  products: (q) => `/api/products?q=${encodeURIComponent(q)}`,
  productScan: (code) => `/api/products/scan?code=${encodeURIComponent(code)}`,
  counterparties: (q) => `/api/counterparties?q=${encodeURIComponent(q)}`,
  counterpartiesCreate: "/api/counterparties",
  accounts: "/api/accounts",
  context: "/api/context",
  checkout: "/api/checkout",
};

const state = {
  // Har bir cart elementi: { id, meta, name, quantity, price /* har doim so'mda,
  // hisoblangan */, priceCurrency: 'som'|'foreign', rawPrice /* kassir kiritgan
  // asl qiymat, priceCurrency birligida */ }
  cart: [],
  accounts: [], // to'liq hisob obyektlari (id -> meta/currency qidirish uchun)
  organizations: [],
  stores: [],
  selectedOrg: null,
  selectedStore: null,
  selectedAgent: null, // { meta, name }
  selectedAccount: null, // accounts ro'yxatidagi element
  // Savat va to'lov ekranlaridagi "Kurs" maydonlari shu bittagina qiymatga bog'langan —
  // biri o'zgarsa ikkinchisi ham yangilanadi, ikkalasi ham bitta real kursni anglatadi.
  exchangeRate: 1,
};

// ---------------- Yordamchi funksiyalar ----------------

function formatSom(value) {
  const rounded = Math.round(value * 100) / 100;
  return rounded.toLocaleString("ru-RU").replace(/,/g, " ") + " so'm";
}

function debounce(fn, delay) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

function showLoading(show) {
  document.getElementById("loadingOverlay").classList.toggle("hidden", !show);
}

let toastTimer;
function showToast(message, isError = false) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 3500);
}

function extractErrorMessage(err) {
  // Backend orqali kelgan MoySklad xatosi odatda { errors: [{ error: "..." }] } shaklida bo'ladi
  const detail = err && err.detail;
  if (detail && Array.isArray(detail.errors) && detail.errors.length) {
    return detail.errors.map((e) => e.error).join("; ");
  }
  if (typeof detail === "string") return detail;
  if (detail) return JSON.stringify(detail);
  return err && err.message ? err.message : "Noma'lum xatolik yuz berdi";
}

// Sessiya cookie'si httpOnly bo'lgani uchun JS uni o'qiy olmaydi — shuning uchun
// har bir so'rovga credentials: "include" qo'shamiz, brauzer cookie'ni o'zi ilova qiladi.

async function apiGet(url) {
  const res = await fetch(url, { credentials: "include" });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) showLoginScreen();
  if (!res.ok) throw { status: res.status, detail: data.detail ?? data };
  return data;
}

async function apiPost(url, body) {
  const res = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) showLoginScreen();
  if (!res.ok) throw { status: res.status, detail: data.detail ?? data };
  return data;
}

// ---------------- Navigatsiya ----------------

function switchView(name) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  document.getElementById(`view-${name}`).classList.add("active");
  document.querySelectorAll(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
}

document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

document.getElementById("cartBtn").addEventListener("click", () => switchView("cart"));

// ---------------- Mahsulotlarni qidirish ----------------

const productList = document.getElementById("productList");
const productHint = document.getElementById("productHint");
const searchInput = document.getElementById("searchInput");

async function runProductSearch(query) {
  if (!query.trim()) {
    productList.innerHTML = "";
    productHint.style.display = "block";
    productHint.textContent = "Qidirishni boshlash uchun nom yoki artikul kiriting";
    return;
  }
  try {
    showLoading(true);
    const data = await apiGet(API.products(query.trim()));
    renderProducts(data.items || []);
  } catch (err) {
    showToast("Mahsulotlarni yuklashda xatolik: " + extractErrorMessage(err), true);
  } finally {
    showLoading(false);
  }
}

function renderProducts(items) {
  if (!items.length) {
    productList.innerHTML = "";
    productHint.style.display = "block";
    productHint.textContent = "Hech narsa topilmadi";
    return;
  }
  productHint.style.display = "none";
  productList.innerHTML = items
    .map(
      (p, idx) => `
      <div class="product-card">
        <div class="product-info">
          <div class="product-name">${escapeHtml(p.name)}</div>
          <div class="product-meta">${escapeHtml(p.code || p.article || "")}</div>
          <div class="product-price">${formatSom(p.price)}</div>
        </div>
        <button class="add-btn" data-idx="${idx}" type="button">+</button>
      </div>`
    )
    .join("");

  productList.querySelectorAll(".add-btn").forEach((btn) => {
    btn.addEventListener("click", () => addToCart(items[Number(btn.dataset.idx)]));
  });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

searchInput.addEventListener("input", debounce((e) => runProductSearch(e.target.value), 350));

// ---------------- Savat ----------------

function addToCart(product) {
  const existing = state.cart.find((i) => i.id === product.id);
  if (existing) {
    existing.quantity += 1;
  } else {
    state.cart.push({
      id: product.id,
      meta: product.meta,
      name: product.name,
      price: product.price,
      rawPrice: product.price,
      priceCurrency: "som",
      quantity: 1,
    });
  }
  renderCart();
  showToast(`"${product.name}" savatga qo'shildi`);
}

function changeQty(id, delta) {
  const item = state.cart.find((i) => i.id === id);
  if (!item) return;
  item.quantity += delta;
  if (item.quantity <= 0) {
    state.cart = state.cart.filter((i) => i.id !== id);
  }
  renderCart();
}

function removeFromCart(id) {
  state.cart = state.cart.filter((i) => i.id !== id);
  renderCart();
}

function cartTotal() {
  return state.cart.reduce((sum, i) => sum + i.price * i.quantity, 0);
}

// Kassir narxni to'g'ridan-to'g'ri boshqa valyutada kiritishi mumkin (masalan USD) —
// bunday holda "price" (so'm ekvivalenti) joriy kursga ko'ra hisoblanadi.
function setItemRawPrice(id, rawValue) {
  const item = state.cart.find((i) => i.id === id);
  if (!item) return;
  const raw = Math.max(0, Number(rawValue) || 0);
  item.rawPrice = raw;
  item.price = item.priceCurrency === "foreign" ? raw * state.exchangeRate : raw;
  renderCart();
}

function setItemCurrency(id, currency) {
  const item = state.cart.find((i) => i.id === id);
  if (!item || item.priceCurrency === currency) return;
  // Valyuta turi almashtirilganda joriy so'm summasi o'zgarmasligi uchun
  // rawPrice'ni yangi birlikka moslab qayta hisoblaymiz.
  item.rawPrice = currency === "foreign" && state.exchangeRate > 0 ? item.price / state.exchangeRate : item.price;
  item.priceCurrency = currency;
  renderCart();
}

function setExchangeRate(value) {
  const rate = Math.max(0, Number(value) || 0);
  state.exchangeRate = rate;
  // Kurs o'zgarsa, "valyuta"da narxlangan tovarlarning so'm ekvivalenti qayta hisoblanadi
  state.cart.forEach((item) => {
    if (item.priceCurrency === "foreign") {
      item.price = item.rawPrice * rate;
    }
  });
  document.getElementById("cartExchangeRate").value = String(rate);
  document.getElementById("exchangeRate").value = String(rate);
  renderCart();
}

function renderCart() {
  const cartList = document.getElementById("cartList");
  const cartEmpty = document.getElementById("cartEmpty");
  const count = state.cart.reduce((s, i) => s + i.quantity, 0);
  document.getElementById("cartCount").textContent = String(count);

  if (!state.cart.length) {
    cartList.innerHTML = "";
    cartEmpty.style.display = "block";
    document.getElementById("toCheckoutBtn").disabled = true;
  } else {
    cartEmpty.style.display = "none";
    document.getElementById("toCheckoutBtn").disabled = false;
    cartList.innerHTML = state.cart
      .map(
        (item) => `
        <div class="cart-item">
          <div class="cart-item-top">
            <div class="cart-item-name">${escapeHtml(item.name)}</div>
            <button class="remove-btn" data-id="${item.id}" type="button">O'chirish</button>
          </div>
          <div class="cart-item-bottom">
            <div class="qty-control">
              <button class="qty-btn" data-id="${item.id}" data-delta="-1" type="button">−</button>
              <span class="qty-value">${item.quantity}</span>
              <button class="qty-btn" data-id="${item.id}" data-delta="1" type="button">+</button>
            </div>
            <div class="line-total">${formatSom(item.price * item.quantity)}</div>
          </div>
          <div class="cart-item-price-row">
            <input
              type="number"
              class="price-input"
              data-id="${item.id}"
              inputmode="decimal"
              step="0.01"
              min="0"
              value="${item.rawPrice}"
            />
            <div class="currency-toggle">
              <button type="button" class="curr-btn ${item.priceCurrency === "som" ? "active" : ""}" data-id="${item.id}" data-currency="som">so'm</button>
              <button type="button" class="curr-btn ${item.priceCurrency === "foreign" ? "active" : ""}" data-id="${item.id}" data-currency="foreign">valyuta</button>
            </div>
          </div>
        </div>`
      )
      .join("");

    cartList.querySelectorAll(".qty-btn").forEach((btn) => {
      btn.addEventListener("click", () => changeQty(btn.dataset.id, Number(btn.dataset.delta)));
    });
    cartList.querySelectorAll(".remove-btn").forEach((btn) => {
      btn.addEventListener("click", () => removeFromCart(btn.dataset.id));
    });
    // "change" (input emas) — aks holda har bosilgan raqamda qayta render bo'lib,
    // maydon fokusdan chiqib ketardi
    cartList.querySelectorAll(".price-input").forEach((input) => {
      input.addEventListener("change", () => setItemRawPrice(input.dataset.id, input.value));
    });
    cartList.querySelectorAll(".curr-btn").forEach((btn) => {
      btn.addEventListener("click", () => setItemCurrency(btn.dataset.id, btn.dataset.currency));
    });
  }

  const total = cartTotal();
  document.getElementById("cartTotal").textContent = formatSom(total);
  document.getElementById("checkoutTotal").textContent = formatSom(total);
}

document.getElementById("toCheckoutBtn").addEventListener("click", () => {
  switchView("checkout");
  loadCheckoutData();
});

// ---------------- To'lov ekrani: tashkilot / ombor ----------------

let checkoutDataLoaded = false;

async function loadCheckoutData() {
  if (checkoutDataLoaded) return;
  try {
    showLoading(true);
    const [context, accountsData] = await Promise.all([apiGet(API.context), apiGet(API.accounts)]);

    state.organizations = context.organizations || [];
    state.stores = context.stores || [];
    state.accounts = accountsData.items || [];

    fillSelect("orgSelect", "orgGroup", state.organizations, (o) => {
      state.selectedOrg = o;
      fillAccountSelect(); // hisoblar tashkilotga bog'liq — tashkilot almashsa qayta filtrlanadi
    });
    fillSelect("storeSelect", "storeGroup", state.stores, (s) => {
      state.selectedStore = s;
    });
    fillAccountSelect();

    checkoutDataLoaded = true;
  } catch (err) {
    showToast("Sozlamalarni yuklashda xatolik: " + extractErrorMessage(err), true);
  } finally {
    showLoading(false);
  }
}

function fillSelect(selectId, groupId, list, onSelect) {
  const select = document.getElementById(selectId);
  const group = document.getElementById(groupId);

  if (!list.length) {
    group.classList.add("hidden");
    return;
  }

  select.innerHTML = list.map((item, idx) => `<option value="${idx}">${escapeHtml(item.name)}</option>`).join("");
  onSelect(list[0]);

  if (list.length === 1) {
    group.classList.add("hidden");
  } else {
    group.classList.remove("hidden");
  }

  select.onchange = () => onSelect(list[Number(select.value)]);
}

function fillAccountSelect() {
  const select = document.getElementById("accountSelect");
  const docTypeSelect = document.getElementById("docTypeSelect");

  // Hisoblar tashkilot bo'yicha alohida (entity/organization/{id}/accounts), shuning
  // uchun faqat tanlangan tashkilotga tegishlilari ko'rsatiladi.
  const accountsForOrg = state.selectedOrg
    ? state.accounts.filter((a) => a.organization_id === state.selectedOrg.id)
    : state.accounts;

  if (!accountsForOrg.length) {
    select.innerHTML = `<option value="">Hisoblar topilmadi</option>`;
    state.selectedAccount = null;
    return;
  }

  select.innerHTML = accountsForOrg
    .map((acc, idx) => {
      const icon = acc.guessed_type === "cash" ? "💵" : "🏦";
      return `<option value="${idx}">${icon} ${escapeHtml(acc.name)}</option>`;
    })
    .join("");

  const rateInput = document.getElementById("exchangeRate");
  const rateHint = document.getElementById("rateHint");

  const applySelection = (acc) => {
    state.selectedAccount = acc;
    // Hisob nomiga qarab hujjat turini taxminiy tanlaymiz, kassir kerak bo'lsa o'zi o'zgartiradi
    docTypeSelect.value = acc.guessed_type === "cash" ? "cashin" : "paymentin";

    // MoySklad tashkilotning bazaviy valyutasi uchun kursni 1'dan boshqa qiymatga
    // o'zgartirishga ruxsat bermaydi (xato 3007) — shu hisoblarda maydonni bloklaymiz.
    // Diqqat: bu faqat TO'LOV hujjatiga jo'natiladigan qiymatni ko'rsatadi — savatdagi
    // tovarlarni valyutada narxlash uchun ishlatilgan umumiy kurs (state.exchangeRate)
    // bilan aralashtirilmaydi, aks holda hisob almashtirilganda savat summalari
    // kutilmaganda o'zgarib qolardi.
    rateInput.disabled = Boolean(acc.is_base_currency);
    rateHint.classList.toggle("hidden", !acc.is_base_currency);
    rateInput.value = acc.is_base_currency ? "1" : String(state.exchangeRate);
  };

  applySelection(accountsForOrg[0]);
  select.onchange = () => applySelection(accountsForOrg[Number(select.value)]);
}

// ---------------- To'lov ekrani: mijoz qidirish ----------------

const agentSearch = document.getElementById("agentSearch");
const agentResults = document.getElementById("agentResults");
const agentSelected = document.getElementById("agentSelected");

async function runAgentSearch(query) {
  if (!query.trim()) {
    agentResults.classList.add("hidden");
    return;
  }
  try {
    const data = await apiGet(API.counterparties(query.trim()));
    renderAgentResults(data.items || []);
  } catch (err) {
    showToast("Mijozlarni qidirishda xatolik: " + extractErrorMessage(err), true);
  }
}

function renderAgentResults(items) {
  if (!items.length) {
    agentResults.innerHTML = `<div class="dropdown-item">Hech narsa topilmadi</div>`;
    agentResults.classList.remove("hidden");
    return;
  }
  agentResults.innerHTML = items
    .map((a, idx) => `<div class="dropdown-item" data-idx="${idx}">${escapeHtml(a.name)}</div>`)
    .join("");
  agentResults.classList.remove("hidden");

  agentResults.querySelectorAll(".dropdown-item[data-idx]").forEach((el) => {
    el.addEventListener("click", () => {
      const agent = items[Number(el.dataset.idx)];
      selectAgent(agent);
    });
  });
}

function selectAgent(agent) {
  state.selectedAgent = agent;
  agentSearch.value = "";
  agentResults.classList.add("hidden");
  newAgentForm.classList.add("hidden");
  agentSelected.classList.remove("hidden");
  agentSelected.innerHTML = `<span>${escapeHtml(agent.name)}</span> <button type="button" id="clearAgentBtn">✕</button>`;
  document.getElementById("clearAgentBtn").addEventListener("click", () => {
    state.selectedAgent = null;
    agentSelected.classList.add("hidden");
  });
}

agentSearch.addEventListener("input", debounce((e) => runAgentSearch(e.target.value), 350));

// ---------------- To'lov ekrani: yangi mijoz qo'shish ----------------

const newAgentForm = document.getElementById("newAgentForm");
const newAgentName = document.getElementById("newAgentName");

document.getElementById("showNewAgentBtn").addEventListener("click", () => {
  agentResults.classList.add("hidden");
  newAgentForm.classList.toggle("hidden");
  if (!newAgentForm.classList.contains("hidden")) newAgentName.focus();
});

document.getElementById("saveNewAgentBtn").addEventListener("click", async () => {
  const name = newAgentName.value.trim();
  if (!name) {
    showToast("Mijoz ismini kiriting", true);
    return;
  }
  try {
    showLoading(true);
    const agent = await apiPost(API.counterpartiesCreate, { name });
    newAgentName.value = "";
    selectAgent(agent);
    showToast(`"${agent.name}" mijoz sifatida saqlandi va tanlandi`);
  } catch (err) {
    showToast("Mijoz saqlashda xatolik: " + extractErrorMessage(err), true);
  } finally {
    showLoading(false);
  }
});

// ---------------- To'lash ----------------

document.getElementById("payBtn").addEventListener("click", handlePay);

async function handlePay() {
  if (!state.cart.length) {
    showToast("Savat bo'sh", true);
    return;
  }
  if (!state.selectedOrg) {
    showToast("Tashkilot tanlanmagan", true);
    return;
  }
  if (!state.selectedStore) {
    showToast("Ombor tanlanmagan", true);
    return;
  }
  if (!state.selectedAgent) {
    showToast("Mijozni tanlang yoki qidiring", true);
    return;
  }
  if (!state.selectedAccount) {
    showToast("To'lov hisobini tanlang", true);
    return;
  }

  const exchangeRate = Number(document.getElementById("exchangeRate").value) || 1;

  const payload = {
    organization_meta: state.selectedOrg.meta,
    store_meta: state.selectedStore.meta,
    agent_meta: state.selectedAgent.meta,
    items: state.cart.map((i) => ({
      assortment_meta: i.meta,
      quantity: i.quantity,
      price: i.price,
    })),
    account_meta: state.selectedAccount.meta,
    document_type: document.getElementById("docTypeSelect").value,
    currency_meta: state.selectedAccount.currency ? state.selectedAccount.currency.meta : null,
    exchange_rate: exchangeRate,
    comment: document.getElementById("orderComment").value.trim() || null,
  };

  try {
    showLoading(true);
    document.getElementById("payBtn").disabled = true;
    const result = await apiPost(API.checkout, payload);
    showToast(`To'lov muvaffaqiyatli amalga oshirildi: Buyurtma #${result.order.name}`);
    resetAfterPayment();
  } catch (err) {
    showToast("To'lovda xatolik: " + extractErrorMessage(err), true);
  } finally {
    showLoading(false);
    document.getElementById("payBtn").disabled = false;
  }
}

function resetAfterPayment() {
  state.cart = [];
  state.selectedAgent = null;
  agentSelected.classList.add("hidden");
  document.getElementById("orderComment").value = "";
  setExchangeRate(1);
  renderCart();
  switchView("products");
}

// ---------------- Kurs maydonlarini sinxronlash (savat <-> to'lov) ----------------

document.getElementById("cartExchangeRate").addEventListener("input", (e) => setExchangeRate(e.target.value));
document.getElementById("exchangeRate").addEventListener("input", (e) => {
  if (e.target.disabled) return; // bazaviy valyutali hisobda maydon bloklangan, o'zgartirilmaydi
  setExchangeRate(e.target.value);
});

// ---------------- Kamera bilan barkod skanerlash ----------------

let scannerInstance = null;

async function openScanner() {
  const overlay = document.getElementById("scannerOverlay");
  overlay.classList.remove("hidden");
  try {
    scannerInstance = new Html5Qrcode("scannerViewport");
    await scannerInstance.start(
      { facingMode: "environment" },
      { fps: 10, qrbox: { width: 250, height: 150 } },
      onScanSuccess,
      () => {} // har bir muvaffaqiyatsiz freym uchun chaqiriladi — jim o'tkaziladi
    );
  } catch (err) {
    showToast("Kameraga kirish imkoni bo'lmadi: " + (err && err.message ? err.message : err), true);
    await closeScanner();
  }
}

async function closeScanner() {
  const overlay = document.getElementById("scannerOverlay");
  overlay.classList.add("hidden");
  if (scannerInstance) {
    try {
      await scannerInstance.stop();
      scannerInstance.clear();
    } catch (err) {
      /* skaner allaqachon to'xtagan bo'lishi mumkin */
    }
    scannerInstance = null;
  }
}

async function onScanSuccess(decodedText) {
  await closeScanner();
  try {
    showLoading(true);
    const data = await apiGet(API.productScan(decodedText));
    if (!data.item) {
      showToast(`Barkod topilmadi: ${decodedText}`, true);
      return;
    }
    addToCart(data.item);
  } catch (err) {
    showToast("Skanerlashda xatolik: " + extractErrorMessage(err), true);
  } finally {
    showLoading(false);
  }
}

document.getElementById("scanBtn").addEventListener("click", openScanner);
document.getElementById("scannerCloseBtn").addEventListener("click", closeScanner);

// ---------------- Autentifikatsiya ----------------
//
// Har bir kassir o'zining shaxsiy MoySklad login/paroli bilan kiradi. Backend buni
// MoySklad'ning o'z tokeniga almashtirib, server-side sessiya sifatida saqlaydi —
// brauzerda faqat httpOnly sessiya cookie'si turadi, haqiqiy MoySklad tokeni
// hech qachon JavaScript'ga yoki tarmoqqa (bizning backendimizdan tashqarida)
// ko'rinmaydi.

function showLoginScreen() {
  document.getElementById("app").classList.add("hidden");
  document.getElementById("loginScreen").classList.remove("hidden");
  checkoutDataLoaded = false;
  state.accounts = [];
  state.organizations = [];
  state.stores = [];
}

function showApp(employeeName) {
  document.getElementById("loginScreen").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  document.getElementById("employeeName").textContent = employeeName || "";
}

async function bootstrapSession() {
  try {
    const me = await apiGet(API.me);
    showApp(me.employee_name);
  } catch (err) {
    showLoginScreen();
  }
}

document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const login = document.getElementById("loginInput").value.trim();
  const password = document.getElementById("passwordInput").value;
  if (!login || !password) return;

  const loginBtn = document.getElementById("loginBtn");
  try {
    showLoading(true);
    loginBtn.disabled = true;
    const res = await fetch(API.login, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw { status: res.status, detail: data.detail ?? data };
    document.getElementById("passwordInput").value = "";
    showApp(data.employee_name);
  } catch (err) {
    showToast("Kirishda xatolik: " + extractErrorMessage(err), true);
  } finally {
    showLoading(false);
    loginBtn.disabled = false;
  }
});

document.getElementById("logoutBtn").addEventListener("click", async () => {
  try {
    await fetch(API.logout, { method: "POST", credentials: "include" });
  } catch (err) {
    /* tarmoq xatosida ham lokal holatni tozalab, kirish ekraniga qaytamiz */
  }
  state.cart = [];
  renderCart();
  showLoginScreen();
});

// ---------------- PWA: service worker ----------------

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* offline-rejim ixtiyoriy, xatolik kritik emas */
    });
  });
}

// ---------------- Telegram Mini App integratsiyasi ----------------
//
// Bu ilova Telegram bot orqali Web App sifatida ham ochilishi mumkin. Agar shunday
// bo'lsa, Telegram o'zining JS SDK'sini (index.html'dagi <script>) window.Telegram
// obyekti sifatida taqdim etadi. Oddiy brauzerda bu obyekt mavjud bo'lmaydi —
// shuning uchun quyidagi kod ixtiyoriy va shartli.

if (window.Telegram && window.Telegram.WebApp) {
  Telegram.WebApp.ready();
  Telegram.WebApp.expand(); // to'liq balandlikda ochish (Telegram'ning kichik boshlang'ich balandligi o'rniga)
}

// ---------------- Boshlang'ich holat ----------------

renderCart();
bootstrapSession();
