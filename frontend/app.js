"use strict";

/**
 * Mobil POS frontend — MoySklad'ga to'g'ridan-to'g'ri emas, faqat o'z backend
 * proksisi (/api/...) orqali murojaat qiladi. Token brauzerga hech qachon kelmaydi.
 */

const API = {
  login: "/api/login",
  logout: "/api/logout",
  me: "/api/me",
  products: (q, storeId) =>
    `/api/products?q=${encodeURIComponent(q)}${storeId ? `&store_id=${encodeURIComponent(storeId)}` : ""}`,
  productScan: (code, storeId) =>
    `/api/products/scan?code=${encodeURIComponent(code)}${
      storeId ? `&store_id=${encodeURIComponent(storeId)}` : ""
    }`,
  counterparties: (q) => `/api/counterparties?q=${encodeURIComponent(q)}`,
  counterpartiesCreate: "/api/counterparties",
  accounts: "/api/accounts",
  context: "/api/context",
  currencies: "/api/currencies",
  projects: "/api/projects",
  checkout: "/api/checkout",
  orderHistory: "/api/orders/history",
  pendingOrder: (orderId) => `/api/orders/pending/${encodeURIComponent(orderId)}`,
};

const state = {
  // Har bir cart elementi: { id, meta, name, quantity, price /* JORIY SAVAT
  // VALYUTASIDA (state.cartCurrencyId), hech qanday konvertatsiyasiz to'g'ridan-
  // to'g'ri MoySklad'ga yuboriladigan raqam */ }
  //
  // MUHIM: narx har bir tovar uchun ALOHIDA valyutada bo'lishi chalkashlikka
  // olib kelgani uchun (haqiqiy foydalanishda tasdiqlangan) butun savat uchun
  // BITTA umumiy valyuta va BITTA kurs ishlatiladi — soddaroq va tushunarli.
  cart: [],
  accounts: [], // to'liq hisob obyektlari (id -> meta/currency qidirish uchun)
  organizations: [],
  stores: [],
  currencies: [], // [{id, meta, name, is_default}] — tashkilotda sozlangan barcha valyutalar
  cartCurrencyId: null, // hozir tanlangan savat valyutasi (state.currencies dagi id)
  cartRate: 0, // "1 bazaviy valyuta = ? cartCurrencyId" (faqat cartCurrencyId bazaviy bo'lmasa kerak)
  selectedOrg: null,
  selectedStore: null,
  selectedAgent: null, // { meta, name }
  selectedAccount: null, // accounts ro'yxatidagi element
  projects: [], // [{id, meta, name}] — entity/project ro'yxati
  selectedProject: null,
  historyItems: [], // /api/orders/history'dan kelgan xom ro'yxat
};

// Hali sinxronlanmagan (pending/failed) buyurtmalar uchun mahalliy tahrirlash
// nusxasi: order_id -> items[] (server'dan kelgan items'ning ishchi nusxasi,
// +/- va o'chirish tugmalari shuni o'zgartiradi, keyin debounce bilan
// PATCH orqali serverga yuboriladi).
const pendingEditState = {};
const _pendingPatchTimers = {};

// ---------------- Yordamchi funksiyalar ----------------

// Tashkilotning HAQIQIY bazaviy (учетная) valyutasi har doim ham "so'm" bo'lavermaydi —
// ba'zi hisoblarda bu doller yoki boshqa valyuta bo'lishi mumkin (real hisobda
// tekshirilgan). Shu sabab summalar hech qachon "so'm" deb qattiq yozilmaydi.
function formatWithLabel(value, label) {
  const rounded = Math.round(value * 100) / 100;
  return rounded.toLocaleString("ru-RU").replace(/,/g, " ") + (label ? " " + label : "");
}

// Tashkilotning bazaviy valyutasida (masalan mahsulotlar ro'yxatida, MoySklad'ning
// o'z narxi ko'rsatilganda).
function formatMoney(value) {
  const def = defaultCurrency();
  return formatWithLabel(value, def ? def.name : "");
}

// Kassir hozir tanlagan SAVAT valyutasida (savat/to'lov ekranlarida).
function formatCartMoney(value) {
  const cur = currencyById(state.cartCurrencyId) || defaultCurrency();
  return formatWithLabel(value, cur ? cur.name : "");
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

async function apiPatch(url, body) {
  const res = await fetch(url, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) showLoginScreen();
  if (!res.ok) throw { status: res.status, detail: data.detail ?? data };
  return data;
}

async function apiDelete(url) {
  const res = await fetch(url, { method: "DELETE", credentials: "include" });
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
  btn.addEventListener("click", () => {
    switchView(btn.dataset.view);
    if (btn.dataset.view === "history") loadHistory();
  });
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
    const data = await apiGet(API.products(query.trim(), state.selectedStore ? state.selectedStore.id : null));
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
        <div class="product-thumb">
          ${
            p.image_url
              ? `<img class="product-img" data-idx="${idx}" src="${escapeHtml(p.image_url)}" alt="" loading="lazy" />`
              : `<span class="product-thumb-placeholder">📦</span>`
          }
        </div>
        <div class="product-info">
          <div class="product-name">${escapeHtml(p.name)}</div>
          <div class="product-meta">${escapeHtml(p.code || p.article || "")}</div>
          <div class="product-price">${formatMoney(p.price)}</div>
          ${renderStockBadge(p.stock)}
        </div>
        <button class="add-btn" data-idx="${idx}" type="button" ${isOutOfStock(p.stock) ? "disabled" : ""}>+</button>
      </div>`
    )
    .join("");

  productList.querySelectorAll(".add-btn").forEach((btn) => {
    btn.addEventListener("click", () => addToCart(items[Number(btn.dataset.idx)]));
  });

  // Rasm yuklanmasa (masalan o'chirilgan/buzilgan havola), standart belgiga almashtiramiz —
  // ilova hech qachon "buzilgan rasm" belgisini ko'rsatmaydi.
  productList.querySelectorAll(".product-img").forEach((img) => {
    img.addEventListener(
      "error",
      () => {
        const placeholder = document.createElement("span");
        placeholder.className = "product-thumb-placeholder";
        placeholder.textContent = "📦";
        img.replaceWith(placeholder);
      },
      { once: true }
    );
  });
}

// Tanlangan OMBOR bo'yicha aniq qoldiq — backend faqat "store_id" berilganda
// shu maydonni to'ldiradi (aks holda "stock" undefined/null bo'ladi va
// hech qanday belgi ko'rsatilmaydi, chunki qaysi ombor ekani noma'lum).
function renderStockBadge(stock) {
  if (stock === null || stock === undefined) return "";
  const rounded = Math.round(stock * 100) / 100;
  if (rounded <= 0) {
    return `<div class="stock-badge stock-out">Omborda yo'q</div>`;
  }
  return `<div class="stock-badge stock-in">Omborda: ${rounded.toLocaleString("ru-RU")} dona</div>`;
}

// "stock" faqat ombor tanlanganda keladi (aks holda null/undefined — bu holda
// "yo'q" deb hisoblanmaydi, chunki qaysi ombor ekani noma'lum). MUHIM: tovar
// BOSHQA omborlarda ko'p bo'lishi mumkin, lekin AYNAN shu tanlangan omborda
// yo'q bo'lsa, MoySklad otgruzka yaratishni rad etadi (real hisobda
// tasdiqlangan) — shuning uchun bu holatda savatga qo'shish qat'iy bloklanadi.
function isOutOfStock(stock) {
  return stock !== null && stock !== undefined && Math.round(stock * 100) / 100 <= 0;
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

// Kassir bilan kelishilgan holda rubl bu ilovada butunlay ko'rsatilmaydi —
// faqat tashkilotning bazaviy valyutasi va dollar ishlatiladi (soddalik uchun).
function availableCurrencies() {
  return state.currencies.filter((c) => c.iso_code !== "RUB");
}

// "1 BAZAVIY valyuta = ? savat valyutasi" (masalan "1 dollar = 12000 so'm") —
// bu yo'nalish kassir bilan aniq kelishilgan: chet el valyutasi (dollar) odatda
// "qimmat" bo'lgani uchun "1 so'm = ? dollar" tarzida o'ylash tabiiy emas.
// 0 bo'lsa — kassir hali kiritmagan, bu holda checkout bloklanadi.
function currentCartRate() {
  const def = defaultCurrency();
  if (!def || state.cartCurrencyId === def.id) return 1;
  return state.cartRate;
}

async function loadCurrencies() {
  try {
    const data = await apiGet(API.currencies);
    state.currencies = data.items || [];
    const def = defaultCurrency();
    if (def) state.cartCurrencyId = def.id;
  } catch (err) {
    showToast("Valyutalarni yuklashda xatolik: " + extractErrorMessage(err), true);
  }
}

// MUHIM (real MoySklad API'da to'g'ridan-to'g'ri tekshirilgan): hujjatga
// yuboriladigan narx hujjatning O'Z valyutasida bo'lishi kerak — MoySklad uni
// bazaviy valyutaga hech qanday avtomatik konvertatsiya qilmaydi. Shu sabab
// "item.price" endi doim JORIY SAVAT VALYUTASIDA saqlanadi (masalan so'mda),
// alohida "bazaviy ekvivalent" hisoblashning hojati yo'q — checkout paytida
// backend shu raqamni to'g'ridan-to'g'ri, shu valyuta bilan birga yuboradi.
function addToCart(product) {
  // Tugma "Omborda yo'q" bo'lganda o'zi o'chirilgan (renderProducts), lekin
  // barkod-skaner to'g'ridan-to'g'ri shu funksiyani chaqiradi — shu sabab
  // bu yerda ham qat'iy tekshiriladi (aks holda MoySklad keyinroq, sinxronlash
  // paytida rad etadi, kassir esa buni darhol bilmay qolardi).
  if (isOutOfStock(product.stock)) {
    showToast(`"${product.name}" tanlangan omborda yo'q — savatga qo'shib bo'lmaydi`, true);
    return;
  }
  const existing = state.cart.find((i) => i.id === product.id);
  if (existing) {
    existing.quantity += 1;
  } else {
    // Tovarning MoySklad'dagi o'z narxi ("Цена продажи") har doim bazaviy
    // valyutada — savat valyutasi bazaviydan farq qilsa, joriy kursga ko'ra
    // darhol o'giriladi (faqat qo'shilayotgan paytdagi qulaylik uchun).
    const rate = currentCartRate();
    state.cart.push({
      id: product.id,
      meta: product.meta,
      name: product.name,
      price: rate !== 1 ? product.price * rate : product.price,
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

// Savat/checkout summasi kassir TANLAGAN valyutada ko'rsatiladi — bu aynan
// kassir kiritgan/kutgan raqam, konvertatsiya haqida o'ylash shart emas.
function cartTotal() {
  return state.cart.reduce((sum, i) => sum + i.price * i.quantity, 0);
}

// Kassir yozgan narx to'g'ridan-to'g'ri JORIY SAVAT VALYUTASIDA saqlanadi —
// hech qanday konvertatsiya kerak emas (MoySklad'ga aynan shu holida yuboriladi).
function setItemPrice(id, value) {
  const item = state.cart.find((i) => i.id === id);
  if (!item) return;
  item.price = Math.max(0, Number(value) || 0);
  renderCart();
}

// Savat valyutasi almashtirilganda (masalan so'm -> dollar) tovarlarning
// HAQIQIY qiymati saqlanib qolishi uchun narxlari joriy kursga ko'ra qayta
// hisoblanadi — aks holda xuddi shu raqam boshqa valyutada qolib, summasi
// butunlay noto'g'ri chiqib qolardi.
function setCartCurrency(currencyId) {
  if (state.cartCurrencyId === currencyId) return;
  const def = defaultCurrency();
  const wasForeign = Boolean(def) && state.cartCurrencyId !== def.id;
  state.cartCurrencyId = currencyId;
  const isForeign = Boolean(def) && currencyId !== def.id;
  const rate = state.cartRate;

  if (rate > 0 && wasForeign !== isForeign) {
    state.cart.forEach((item) => {
      item.price = wasForeign ? item.price / rate : item.price * rate;
    });
  }
  renderCartCurrencySelector();
  renderCart();
}

// Diqqat: kurs o'zgarganda savatdagi tovarlarning narxi qayta hisoblanmaydi —
// ular allaqachon to'g'ri (o'sha) valyutada kiritilgan. Kurs faqat YANGI
// qo'shiladigan tovarlar va yakuniy hujjatlar uchun ishlatiladi.
function setCartRate(value) {
  state.cartRate = Math.max(0, Number(value) || 0);
}

// Savat ekranidagi valyuta tanlovi + (agar bazaviy bo'lmasa) kurs maydoni.
function renderCartCurrencySelector() {
  const select = document.getElementById("cartCurrencySelect");
  const rateGroup = document.getElementById("cartRateGroup");
  const rateLabel = document.getElementById("cartRateLabel");
  const rateInput = document.getElementById("cartRateInput");
  if (!select) return;

  const currencies = availableCurrencies();
  if (!currencies.length) return;

  select.innerHTML = currencies.map((c) => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("");
  const activeId = state.cartCurrencyId && currencies.some((c) => c.id === state.cartCurrencyId)
    ? state.cartCurrencyId
    : currencies[0].id;
  state.cartCurrencyId = activeId;
  select.value = activeId;

  const def = defaultCurrency();
  const isForeign = Boolean(def) && activeId !== def.id;
  rateGroup.classList.toggle("hidden", !isForeign);
  if (isForeign) {
    const cur = currencyById(activeId);
    // "1 dollar = ? so'm" — bazaviy (kuchli) valyutadan tanlangan (kassir bilan
    // aniq kelishilgan yo'nalish, aksincha emas).
    rateLabel.textContent = `1 ${def.name} = ? ${cur ? cur.name : ""}`;
    rateInput.value = state.cartRate || "";
  }

  select.onchange = () => setCartCurrency(select.value);
  rateInput.onchange = () => setCartRate(rateInput.value);
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
            <div class="line-total">${formatCartMoney(item.price * item.quantity)}</div>
          </div>
          <div class="cart-item-price-row">
            <input
              type="number"
              class="price-input"
              data-id="${item.id}"
              inputmode="decimal"
              step="0.01"
              min="0"
              value="${item.price}"
            />
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
      input.addEventListener("change", () => setItemPrice(input.dataset.id, input.value));
    });
  }

  const total = cartTotal();
  document.getElementById("cartTotal").textContent = formatCartMoney(total);
  document.getElementById("checkoutTotal").textContent = formatCartMoney(total);
}

function renderCheckoutStoreInfo() {
  const el = document.getElementById("checkoutStoreInfo");
  if (el) el.textContent = state.selectedStore ? state.selectedStore.name : "Tanlanmagan";
}

document.getElementById("toCheckoutBtn").addEventListener("click", () => {
  switchView("checkout");
  loadCheckoutData();
  renderCheckoutStoreInfo();
});

// ---------------- To'lov ekrani: tashkilot / ombor ----------------

let checkoutDataLoaded = false;

async function loadCheckoutData() {
  if (checkoutDataLoaded) return;
  showLoading(true);

  // Har biri ALOHIDA (bittalab) yuklanadi — birontasi xato bersa (masalan
  // loyihalar ba'zi MoySklad tariflarida cheklangan bo'lishi mumkin), qolgan
  // qismlar baribir to'g'ri ishlashda davom etishi kerak.
  try {
    const context = await apiGet(API.context);
    state.organizations = context.organizations || [];
    state.stores = context.stores || [];
    fillSelect("orgSelect", "orgGroup", state.organizations, (o) => {
      state.selectedOrg = o;
      fillAccountSelect(); // hisoblar tashkilotga bog'liq — tashkilot almashsa qayta filtrlanadi
    });
    fillSelect("storeSelect", "storeGroup", state.stores, (s) => {
      state.selectedStore = s;
      renderCheckoutStoreInfo();
      // Ombor almashtirilganda joriy qidiruv natijasidagi qoldiqlar ("Omborda: N
      // dona") ham YANGI omborga mos ravishda yangilanishi kerak — aks holda
      // eski omborning qoldig'i ko'rsatilib qolib, kassirni chalg'itadi.
      if (searchInput.value.trim()) runProductSearch(searchInput.value);
    });
  } catch (err) {
    showToast("Tashkilot/ombor yuklashda xatolik: " + extractErrorMessage(err), true);
  }

  try {
    const accountsData = await apiGet(API.accounts);
    state.accounts = accountsData.items || [];
    fillAccountSelect();
  } catch (err) {
    showToast("Hisoblarni yuklashda xatolik: " + extractErrorMessage(err), true);
  }

  try {
    const projectsData = await apiGet(API.projects);
    state.projects = projectsData.items || [];
    fillProjectSelect();
  } catch (err) {
    /* Loyihalar ixtiyoriy — xato chiqsa jim o'tkaziladi, checkout to'xtamaydi */
  }

  checkoutDataLoaded = true;
  showLoading(false);
}

function fillSelect(selectId, groupId, list, onSelect) {
  const select = document.getElementById(selectId);
  const group = document.getElementById(groupId);

  if (!list.length) {
    group.classList.add("hidden");
    return;
  }

  // Diqqat: variant bitta bo'lsa ham maydon YASHIRILMAYDI — kassir nima
  // tanlanganini har doim aniq ko'rib turishi kerak (kassir bilan kelishilgan).
  group.classList.remove("hidden");
  select.innerHTML = list.map((item, idx) => `<option value="${idx}">${escapeHtml(item.name)}</option>`).join("");
  onSelect(list[0]);

  select.onchange = () => onSelect(list[Number(select.value)]);
}

// Loyiha ixtiyoriy — org/store/account'dan farqli, standart tanlov "belgilanmagan"
// bo'lishi kerak (birinchi loyihani avtomatik tanlamaymiz), shuning uchun fillSelect
// o'rniga alohida funksiya.
function fillProjectSelect() {
  const select = document.getElementById("projectSelect");
  const group = select.closest(".form-group");

  if (!state.projects.length) {
    group.classList.add("hidden");
    return;
  }

  group.classList.remove("hidden");
  select.innerHTML =
    `<option value="">— Belgilanmagan —</option>` +
    state.projects.map((p, idx) => `<option value="${idx}">${escapeHtml(p.name)}</option>`).join("");
  state.selectedProject = null;
  select.onchange = () => {
    state.selectedProject = select.value === "" ? null : state.projects[Number(select.value)];
  };
}

function fillAccountSelect() {
  const select = document.getElementById("accountSelect");

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

  state.selectedAccount = accountsForOrg[0];
  select.onchange = () => {
    state.selectedAccount = accountsForOrg[Number(select.value)];
  };
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

  // MUHIM: savat valyutasi bazaviydan farq qilsa, kurs ALBATTA kiritilgan bo'lishi
  // shart — aks holda jim ravishda 1:1 deb hisoblanib, butunlay noto'g'ri summa
  // (masalan 90 000 so'm o'rniga 90 000 dollar) yuborilib qolardi. Shu sabab bu
  // holatda checkout to'liq bloklanadi, hech qachon standart qiymatga tushmaydi.
  const def = defaultCurrency();
  const cartCurrencyIsForeign = Boolean(def) && state.cartCurrencyId !== def.id;
  if (cartCurrencyIsForeign && !(state.cartRate > 0)) {
    showToast("Savat ekranida kursni kiriting", true);
    return;
  }

  const cartCurrency = currencyById(state.cartCurrencyId);

  const payload = {
    organization_meta: state.selectedOrg.meta,
    store_meta: state.selectedStore.meta,
    agent_meta: state.selectedAgent.meta,
    items: state.cart.map((i) => ({
      assortment_meta: i.meta,
      quantity: i.quantity,
      price: i.price,
      id: i.id,
      name: i.name,
    })),
    is_debt: isDebt,
    comment: document.getElementById("orderComment").value.trim() || null,
    project_meta: state.selectedProject ? state.selectedProject.meta : null,
    // Buyurtma/otgruzka HAM shu valyutada bo'lishi kerak (qarzga sotuvda ham) —
    // shuning uchun bu maydonlar to'lov mavjud/yo'qligidan qat'i nazar yuboriladi.
    currency_meta: cartCurrencyIsForeign && cartCurrency ? cartCurrency.meta : null,
    exchange_rate: cartCurrencyIsForeign ? state.cartRate : 1,
    // Faqat ko'rsatish uchun (flat) — agar backend buyurtmani Google Sheets
    // navbatiga yozsa, MoySklad'ga qo'shimcha so'rov yubormasdan qatorni
    // odam o'qiy oladigan qilish uchun ishlatiladi.
    store_name: state.selectedStore.name,
    agent_name: state.selectedAgent.name,
    currency_name: (cartCurrencyIsForeign && cartCurrency ? cartCurrency : defaultCurrency())?.name ?? null,
  };

  if (!isDebt) {
    payload.account_meta = state.selectedAccount.meta;
    // Kassir bilan kelishilgan holda hujjat turi tanlanmaydi — har doim
    // "Входящий платёж" ishlatiladi (shu bilan birga "cashin" MoySklad'da hisobga
    // umuman bog'lanmasligi ham aniqlangan edi — shuning uchun bu ham to'g'riroq).
    payload.document_type = "paymentin";
    payload.payment_moment = formatMomentForApi(document.getElementById("paymentMoment").value);
  }

  try {
    showLoading(true);
    document.getElementById("payBtn").disabled = true;
    const result = await apiPost(API.checkout, payload);
    let message;
    if (result.mode === "queue") {
      // Hech narsa hali MoySklad'ga yozilmagan — buyurtma Google Sheets
      // navbatiga qo'yildi, davriy sync uni keyinroq ko'chiradi. Shu sabab
      // "to'lov muvaffaqiyatli" kabi so'z ishlatilmaydi — bu hali noto'g'ri
      // bo'lardi.
      message = "Buyurtma navbatga qo'yildi — keyingi sinxronizatsiyada MoySklad'ga yuboriladi";
    } else {
      message = result.payment
        ? `To'lov muvaffaqiyatli amalga oshirildi: Buyurtma #${result.order.name}`
        : `Qarzga sotildi: Buyurtma #${result.order.name} (to'lov kiritilmadi)`;
    }
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
  state.selectedProject = null;
  const projectSelect = document.getElementById("projectSelect");
  if (projectSelect) projectSelect.value = "";
  // Diqqat: savat valyutasi/kursini (state.cartCurrencyId/cartRate) tozalamaymiz —
  // bir xil kunda ketma-ket sotuvlar uchun kassir har safar qayta kiritmasin.
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

    const data = await apiGet(
      API.productScan(decodedText, state.selectedStore ? state.selectedStore.id : null)
    );
    if (!data.item) {
      showToast(`Barkod topilmadi: ${decodedText}`, true);
      return;
    }
    // addToCart o'zi "Omborda yo'q" bo'lsa qo'shmaydi va tegishli xabarni ko'rsatadi.
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
  state.cartCurrencyId = null;
  state.cartRate = 0;
}

async function showApp(employeeName) {
  document.getElementById("loginScreen").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  document.getElementById("employeeName").textContent = employeeName || "";
  // Valyutalar savat ekranida (to'lovga yetmasdan oldin) kerak bo'lgani uchun
  // kirishdan so'ng darhol yuklab, valyuta tanlovini tayyorlab qo'yamiz.
  await loadCurrencies();
  renderCartCurrencySelector();
  // Ombor (va tashkilot/hisoblar) endi mahsulot qidiruv ekranida ham kerak —
  // qoldiq ("Omborda: N dona") aynan TANLANGAN ombor bo'yicha ko'rsatiladi,
  // shu sabab bu ilgari faqat to'lov ekranida yuklanadigan ma'lumot endi
  // kirishdan so'ng darhol tayyorlanadi.
  await loadCheckoutData();
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

// ---------------- Buyurtmalar tarixi ----------------
//
// Faqat shu ilova orqali kiritilgan buyurtmalar (backend ularni maxsus
// "POS Mini App" sotuv kanali bo'yicha filtrlab beradi — boshqa botlar yoki
// qo'lda kiritilgan buyurtmalar bu ro'yxatga tushmaydi).

const historyList = document.getElementById("historyList");
const historyHint = document.getElementById("historyHint");

function formatHistoryMoment(moment) {
  if (!moment) return "";
  // MoySklad "YYYY-MM-DD HH:MM:SS" formatida qaytaradi
  const parsed = new Date(moment.replace(" ", "T"));
  if (Number.isNaN(parsed.getTime())) return moment;
  return parsed.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const PENDING_STATUS_LABELS = {
  pending: "Kutilmoqda",
  failed: "Xato",
  needs_manual_check: "Tekshirish kerak",
};

async function loadHistory() {
  historyHint.textContent = "Yuklanmoqda...";
  historyHint.classList.remove("hidden");
  historyList.innerHTML = "";
  try {
    const data = await apiGet(API.orderHistory);
    state.historyItems = data.items || [];
    // Har bir hali tahrirlanadigan (pending/failed) buyurtma uchun mahalliy
    // ishchi nusxa — serverdan qayta kelgan har safar qayta boshlanadi.
    for (const key of Object.keys(pendingEditState)) delete pendingEditState[key];
    state.historyItems.forEach((item) => {
      if ((item.status === "pending" || item.status === "failed") && Array.isArray(item.items)) {
        pendingEditState[item.id] = item.items.map((i) => ({ ...i }));
      }
    });
    renderHistoryList();
  } catch (err) {
    historyHint.textContent = "Yuklashda xatolik: " + extractErrorMessage(err);
    historyHint.classList.remove("hidden");
  }
}

function renderHistoryList() {
  const items = state.historyItems;
  if (!items.length) {
    historyHint.textContent = "Hozircha buyurtmalar yo'q";
    historyHint.classList.remove("hidden");
    historyList.innerHTML = "";
    return;
  }
  historyHint.classList.add("hidden");
  historyList.innerHTML = "";
  items.forEach((item) => {
    historyList.appendChild(
      item.status && item.status !== "synced" ? renderPendingHistoryCard(item) : renderSyncedHistoryCard(item)
    );
  });
  wireHistoryCardEvents();
}

function renderSyncedHistoryCard(item) {
  const cur = currencyById(item.currency_id) || defaultCurrency();
  const card = document.createElement("div");
  card.className = "history-card";
  card.innerHTML = `
    <div class="history-card-top">
      <span class="history-name">${escapeHtml(item.name || "")}</span>
      <span class="history-status ${item.is_paid ? "paid" : "unpaid"}">${
    item.is_paid ? "To'langan" : "To'lanmagan"
  }</span>
    </div>
    <div class="history-agent">${escapeHtml(item.agent_name || "")}</div>
    <div class="history-card-bottom">
      <span class="history-moment">${escapeHtml(formatHistoryMoment(item.moment))}</span>
      <span class="history-sum">${escapeHtml(formatWithLabel(item.sum, cur ? cur.name : ""))}</span>
    </div>
    ${item.comment ? `<div class="history-comment">${escapeHtml(item.comment)}</div>` : ""}
  `;
  return card;
}

// Hali MoySklad'ga sinxronlanmagan buyurtma — status "pending"/"failed"/
// "needs_manual_check". "pending"/"failed" uchun tovar miqdorini +/- bilan
// tahrirlash va butun buyurtmani bekor qilish mumkin (savat ekranidagi
// bilan bir xil qty-btn/remove-btn naqshi ishlatiladi) — "needs_manual_check"
// faqat o'qish uchun (egasi qo'lda tekshirishi kerak).
function renderPendingHistoryCard(item) {
  const editable = item.status === "pending" || item.status === "failed";
  const workingItems = editable ? pendingEditState[item.id] || [] : item.items || [];
  const total = workingItems.reduce((s, i) => s + i.price * i.quantity, 0) || item.sum || 0;
  const currencyLabel = item.currency_name || "";

  const card = document.createElement("div");
  card.className = "history-card pending-order";
  card.dataset.orderId = item.id;
  card.innerHTML = `
    <div class="history-card-top">
      <span class="history-name">${escapeHtml(item.items_summary || "Buyurtma")}</span>
      <span class="history-status ${escapeHtml(item.status)}">${escapeHtml(
    PENDING_STATUS_LABELS[item.status] || item.status
  )}</span>
    </div>
    <div class="history-agent">${escapeHtml(item.agent_name || "")}</div>
    <div class="history-card-bottom">
      <span class="history-moment">${escapeHtml(formatHistoryMoment(item.moment))}</span>
      <span class="history-sum">${escapeHtml(formatWithLabel(total, currencyLabel))}</span>
    </div>
    ${
      item.status === "failed" && item.last_error
        ? `<div class="history-error">${escapeHtml(item.last_error)}</div>`
        : ""
    }
    ${
      editable
        ? `<div class="pending-items">${workingItems
            .map(
              (i, idx) => `
          <div class="cart-item">
            <div class="cart-item-top">
              <div class="cart-item-name">${escapeHtml(i.name || "Tovar")}</div>
            </div>
            <div class="cart-item-bottom">
              <div class="qty-control">
                <button class="qty-btn pending-qty-btn" data-order="${item.id}" data-idx="${idx}" data-delta="-1" type="button">−</button>
                <span class="qty-value">${i.quantity}</span>
                <button class="qty-btn pending-qty-btn" data-order="${item.id}" data-idx="${idx}" data-delta="1" type="button">+</button>
              </div>
              <div class="line-total">${formatWithLabel(i.price * i.quantity, currencyLabel)}</div>
            </div>
          </div>`
            )
            .join("")}</div>
        <button class="remove-btn pending-cancel-btn" data-order="${item.id}" type="button">Buyurtmani bekor qilish</button>`
        : `<div class="history-comment">${escapeHtml(item.items_summary || "")}</div>`
    }
  `;
  return card;
}

function wireHistoryCardEvents() {
  historyList.querySelectorAll(".pending-qty-btn").forEach((btn) => {
    btn.addEventListener("click", () =>
      changePendingItemQty(btn.dataset.order, Number(btn.dataset.idx), Number(btn.dataset.delta))
    );
  });
  historyList.querySelectorAll(".pending-cancel-btn").forEach((btn) => {
    btn.addEventListener("click", () => cancelPendingOrder(btn.dataset.order));
  });
}

function changePendingItemQty(orderId, itemIndex, delta) {
  const items = pendingEditState[orderId];
  if (!items) return;
  const item = items[itemIndex];
  if (!item) return;
  item.quantity += delta;
  if (item.quantity <= 0) {
    if (items.length <= 1) {
      // Oxirgi qator — butun buyurtmani bekor qilishni so'raymiz, "bo'sh"
      // buyurtma qoldirmaymiz (backend ham buni rad etadi).
      cancelPendingOrder(orderId);
      return;
    }
    items.splice(itemIndex, 1);
  }
  renderHistoryList();
  schedulePendingPatch(orderId);
}

function schedulePendingPatch(orderId) {
  clearTimeout(_pendingPatchTimers[orderId]);
  _pendingPatchTimers[orderId] = setTimeout(() => sendPendingPatch(orderId), 500);
}

async function sendPendingPatch(orderId) {
  const items = pendingEditState[orderId];
  if (!items || !items.length) return;
  try {
    await apiPatch(API.pendingOrder(orderId), {
      items: items.map((i) => ({
        assortment_meta: i.assortment_meta,
        quantity: i.quantity,
        price: i.price,
        id: i.id,
        name: i.name,
      })),
    });
  } catch (err) {
    showToast("Buyurtmani tahrirlashda xatolik: " + extractErrorMessage(err), true);
    loadHistory(); // server bilan qayta sinxronlash — mahalliy holat eskirgan bo'lishi mumkin
  }
}

async function cancelPendingOrder(orderId) {
  if (!window.confirm("Bu buyurtmani bekor qilishni tasdiqlaysizmi?")) return;
  try {
    showLoading(true);
    await apiDelete(API.pendingOrder(orderId));
    showToast("Buyurtma bekor qilindi");
    await loadHistory();
  } catch (err) {
    showToast("Bekor qilishda xatolik: " + extractErrorMessage(err), true);
  } finally {
    showLoading(false);
  }
}

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
