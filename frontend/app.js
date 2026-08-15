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
  currencies: "/api/currencies",
  checkout: "/api/checkout",
};

const state = {
  // Har bir cart elementi: { id, meta, name, quantity, price /* har doim
  // tashkilotning bazaviy valyutasida, hisoblangan */, currencyId /* qaysi
  // valyutada narxlangani, state.currencies dagi id */, rawPrice /* kassir
  // kiritgan asl qiymat, currencyId birligida */ }
  cart: [],
  accounts: [], // to'liq hisob obyektlari (id -> meta/currency qidirish uchun)
  organizations: [],
  stores: [],
  currencies: [], // [{id, meta, name, is_default}] — tashkilotda sozlangan barcha valyutalar
  // Har bir CHET EL valyutasi uchun "1 shu valyuta = necha bazaviy valyuta" kursi.
  // Kalit — currencyId, qiymat — son. Savat va to'lov ekranlari shu bitta obyektdan foydalanadi.
  exchangeRates: {},
  selectedOrg: null,
  selectedStore: null,
  selectedAgent: null, // { meta, name }
  selectedAccount: null, // accounts ro'yxatidagi element
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

// ---------------- Savat / valyutalar ----------------

function defaultCurrency() {
  return state.currencies.find((c) => c.is_default) || null;
}

function currencyById(id) {
  return state.currencies.find((c) => c.id === id) || null;
}

// Bazaviy valyuta uchun kurs har doim 1; chet el valyutalari uchun kassir
// kiritgan qiymat (yoki hali kiritilmagan bo'lsa — standart 1) ishlatiladi.
function rateForCurrency(currencyId) {
  const def = defaultCurrency();
  if (!def || currencyId === def.id) return 1;
  return state.exchangeRates[currencyId] || 1;
}

async function loadCurrencies() {
  try {
    const data = await apiGet(API.currencies);
    state.currencies = data.items || [];
  } catch (err) {
    showToast("Valyutalarni yuklashda xatolik: " + extractErrorMessage(err), true);
  }
}

function addToCart(product) {
  const existing = state.cart.find((i) => i.id === product.id);
  if (existing) {
    existing.quantity += 1;
  } else {
    // Tovarning MoySklad'dagi o'z narxi qaysi valyutada bo'lsa, savatda ham
    // shu valyuta bilan boshlanadi — kassir kerak bo'lsa keyin o'zgartiradi.
    const def = defaultCurrency();
    const currencyId = product.price_currency_id || (def && def.id) || null;
    state.cart.push({
      id: product.id,
      meta: product.meta,
      name: product.name,
      price: product.price,
      rawPrice: product.price,
      currencyId,
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
// bunday holda "price" (bazaviy valyutadagi ekvivalent) joriy kursga ko'ra hisoblanadi.
function setItemRawPrice(id, rawValue) {
  const item = state.cart.find((i) => i.id === id);
  if (!item) return;
  const raw = Math.max(0, Number(rawValue) || 0);
  item.rawPrice = raw;
  item.price = raw * rateForCurrency(item.currencyId);
  renderCart();
}

function setItemCurrency(id, currencyId) {
  const item = state.cart.find((i) => i.id === id);
  if (!item || item.currencyId === currencyId) return;
  // Valyuta almashtirilganda joriy bazaviy summa o'zgarmasligi uchun
  // rawPrice'ni yangi valyuta birligiga moslab qayta hisoblaymiz.
  const newRate = rateForCurrency(currencyId);
  item.rawPrice = newRate > 0 ? item.price / newRate : item.price;
  item.currencyId = currencyId;
  renderCart();
}

// Bitta chet el valyutasining kursini o'zgartiradi — shu valyutada narxlangan
// barcha savat elementlarining bazaviy summasi qayta hisoblanadi.
function setExchangeRateForCurrency(currencyId, value) {
  const rate = Math.max(0, Number(value) || 0);
  state.exchangeRates[currencyId] = rate;
  state.cart.forEach((item) => {
    if (item.currencyId === currencyId) {
      item.price = item.rawPrice * rate;
    }
  });
  renderCart();
}

// Savat va to'lov ekranlarida bir xil panel — tashkilotning bazaviy valyutasidan
// FARQLI har bir valyuta uchun "1 X = ? bazaviy" maydoni.
function renderCurrencyRatesPanel() {
  const panel = document.getElementById("currencyRatesPanel");
  if (!panel) return;
  const def = defaultCurrency();
  const foreignCurrencies = state.currencies.filter((c) => !c.is_default);

  if (!def || !foreignCurrencies.length) {
    panel.innerHTML = "";
    return;
  }

  panel.innerHTML = foreignCurrencies
    .map(
      (c) => `
      <div class="form-group">
        <label for="rate-${c.id}">1 ${escapeHtml(c.name)} = ? ${escapeHtml(def.name)}</label>
        <input
          type="number"
          id="rate-${c.id}"
          class="rate-input"
          data-currency-id="${c.id}"
          inputmode="decimal"
          step="0.01"
          min="0"
          value="${state.exchangeRates[c.id] || ""}"
          placeholder="Masalan: 12700"
        />
      </div>`
    )
    .join("");

  panel.querySelectorAll(".rate-input").forEach((input) => {
    input.addEventListener("change", () => setExchangeRateForCurrency(input.dataset.currencyId, input.value));
  });
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
              ${state.currencies
                .map(
                  (c) => `
                <button type="button" class="curr-btn ${item.currencyId === c.id ? "active" : ""}" data-id="${item.id}" data-currency-id="${c.id}">${escapeHtml(c.name)}</button>`
                )
                .join("")}
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
      btn.addEventListener("click", () => setItemCurrency(btn.dataset.id, btn.dataset.currencyId));
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

  const applySelection = (acc) => {
    state.selectedAccount = acc;
    // Hisob nomiga qarab hujjat turini taxminiy tanlaymiz, kassir kerak bo'lsa o'zi o'zgartiradi
    docTypeSelect.value = acc.guessed_type === "cash" ? "cashin" : "paymentin";
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

function idFromHref(href) {
  if (!href) return null;
  return href.replace(/\/+$/, "").split("/").pop();
}

// "2026-08-01T09:30" (datetime-local) -> "2026-08-01 09:30:00" (MoySklad "moment" formati)
function formatMomentForApi(datetimeLocalValue) {
  if (!datetimeLocalValue) return null;
  const withSpace = datetimeLocalValue.replace("T", " ");
  return withSpace.length === 16 ? `${withSpace}:00` : withSpace;
}

async function handlePay() {
  const isDebt = document.getElementById("debtCheckbox").checked;

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
  if (!isDebt && !state.selectedAccount) {
    showToast("To'lov hisobini tanlang", true);
    return;
  }

  const payload = {
    organization_meta: state.selectedOrg.meta,
    store_meta: state.selectedStore.meta,
    agent_meta: state.selectedAgent.meta,
    items: state.cart.map((i) => ({
      assortment_meta: i.meta,
      quantity: i.quantity,
      price: i.price,
    })),
    is_debt: isDebt,
    comment: document.getElementById("orderComment").value.trim() || null,
  };

  if (!isDebt) {
    // To'lov kursi endi qo'lda emas — savat ekranida shu hisob valyutasi uchun
    // kiritilgan kursdan (yoki bazaviy valyuta bo'lsa, 1'dan) avtomatik olinadi.
    const accountCurrencyId = state.selectedAccount.currency
      ? idFromHref(state.selectedAccount.currency.meta.href)
      : null;
    payload.account_meta = state.selectedAccount.meta;
    payload.document_type = document.getElementById("docTypeSelect").value;
    payload.currency_meta = state.selectedAccount.currency ? state.selectedAccount.currency.meta : null;
    payload.exchange_rate = accountCurrencyId ? rateForCurrency(accountCurrencyId) : 1;
    payload.payment_moment = formatMomentForApi(document.getElementById("paymentMoment").value);
  }

  try {
    showLoading(true);
    document.getElementById("payBtn").disabled = true;
    const result = await apiPost(API.checkout, payload);
    const message = result.payment
      ? `To'lov muvaffaqiyatli amalga oshirildi: Buyurtma #${result.order.name}`
      : `Qarzga sotildi: Buyurtma #${result.order.name} (to'lov kiritilmadi)`;
    showToast(message);
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
  document.getElementById("paymentMoment").value = "";
  document.getElementById("debtCheckbox").checked = false;
  document.getElementById("paymentFieldsGroup").classList.remove("hidden");
  // Diqqat: kurslarni (state.exchangeRates) tozalamaymiz — bir xil kunda ketma-ket
  // sotuvlar uchun kassir har safar qayta kiritmasin.
  renderCart();
  switchView("products");
}

// ---------------- Qarzga sotish ----------------

document.getElementById("debtCheckbox").addEventListener("change", (e) => {
  document.getElementById("paymentFieldsGroup").classList.toggle("hidden", e.target.checked);
});

// ---------------- Kamera bilan barkod skanerlash ----------------

// Telegram'ning ichki WebView'ida jonli video oqimi (getUserMedia) ba'zi
// qurilmalarda ishlamaydi (tasdiqlangan platforma xatolari — qora ekran yoki
// kamera umuman ochilmaydi). Shu sabab "capture" atributli fayl input orqali
// qurilmaning tub kamera ilovasi ochiladi, bitta surat olinadi va shu surat
// ichidan barkod statik tasvir sifatida o'qiladi — bu usul Telegram ichida ham,
// oddiy brauzerda ham bab-baravar ishonchli ishlaydi.
const scanFileInput = document.getElementById("scanFileInput");

document.getElementById("scanBtn").addEventListener("click", () => {
  scanFileInput.value = ""; // bir xil suratni qayta tanlaganda ham "change" ishga tushishi uchun
  scanFileInput.click();
});

scanFileInput.addEventListener("change", async () => {
  const file = scanFileInput.files && scanFileInput.files[0];
  if (!file) return;

  try {
    showLoading(true);
    const scanner = new Html5Qrcode("scanDecodeTarget");
    let decodedText;
    try {
      decodedText = await scanner.scanFile(file, false);
    } finally {
      scanner.clear();
    }

    const data = await apiGet(API.productScan(decodedText));
    if (!data.item) {
      showToast(`Barkod topilmadi: ${decodedText}`, true);
      return;
    }
    addToCart(data.item);
  } catch (err) {
    showToast("Suratda barkod topilmadi, qaytadan urinib ko'ring", true);
  } finally {
    showLoading(false);
  }
});

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
  state.currencies = [];
  state.exchangeRates = {};
}

async function showApp(employeeName) {
  document.getElementById("loginScreen").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  document.getElementById("employeeName").textContent = employeeName || "";
  // Valyutalar savat ekranida (to'lovga yetmasdan oldin) kerak bo'lgani uchun
  // kirishdan so'ng darhol yuklab, kurs panelini tayyorlab qo'yamiz.
  await loadCurrencies();
  renderCurrencyRatesPanel();
}

async function bootstrapSession() {
  try {
    const me = await apiGet(API.me);
    await showApp(me.employee_name);
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
    await showApp(data.employee_name);
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

// ---------------- Eski Service Worker'ni tozalash ----------------
//
// Ilova avval agressiv keshlash bilan Service Worker ishlatgan edi — bu haqiqiy
// bug'ga olib keldi: kod yangilansa ham, oldin ilovani ochgan foydalanuvchilar
// eskirgan versiyani ko'raverishardi (masalan yangi tugma/maydonlar "yo'q"
// bo'lib ko'rinardi). Bu ilovaning har bir funksiyasi baribir doimiy MoySklad
// ulanishini talab qilgani uchun oflayn kesh foyda bermaydi — shuning uchun
// endi Service Worker umuman ro'yxatdan o'tkazilmaydi, va agar foydalanuvchi
// qurilmasida avvalgi versiyadan qolgan Service Worker bo'lsa, shu yerda
// avtomatik o'chirib tashlanadi.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations().then((registrations) => {
    registrations.forEach((registration) => registration.unregister());
  });
  if (window.caches) {
    caches.keys().then((keys) => keys.forEach((key) => caches.delete(key)));
  }
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
