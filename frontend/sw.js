// ESLATMA: bu ilova hozircha faol rivojlantirilmoqda va har bir funksiyasi
// baribir doimiy MoySklad ulanishini talab qiladi (haqiqiy oflayn foydalanish
// imkoni deyarli yo'q) — shuning uchun sahifani agressiv keshlash foyda
// bermaydi, aksincha eskirgan versiya (masalan tugmalar/maydonlar yo'q)
// ko'rsatilishiga olib kelishi mumkin edi. Shu sabab bu Service Worker endi
// hech narsani keshlamaydi — faqat avvalgi versiyalar qoldirgan eski keshlarni
// tozalaydi va barcha so'rovlarni to'g'ridan-to'g'ri tarmoqqa uzatadi.

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

// Fetch listener yo'q — brauzer har bir so'rovni oddiy tarmoq so'rovi sifatida
// bajaradi, hech qanday maxsus kesh aralashmaydi.
