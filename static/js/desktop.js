/**
 * desktop.js — 1-Click instant-print kiosk.
 *
 * Flow: tap product → POST /api/orders (1 item, qty=1) → open print window → done.
 * No cart, no confirmation, no modal. Pure speed.
 */

document.addEventListener("DOMContentLoaded", () => {
  let menuData = [];
  let currentCategory = "all";
  let printInProgress = false; // prevent double-tap spam
  let runningTotalCents = 0;

  // Elements
  const productGrid     = document.getElementById("product-grid");
  const categoryNav     = document.getElementById("category-nav");
  const syncStatusBadge = document.getElementById("sync-status");
  const syncLabel       = document.getElementById("sync-label");
  const ticketFeedItems = document.getElementById("ticket-feed-items");
  const feedEmpty       = document.getElementById("feed-empty");
  const btnClearFeed    = document.getElementById("btn-clear-feed");
  const printIframe     = document.getElementById("print-iframe");

  // ── UUID generator ────────────────────────────────────────────────────────
  function uuid() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const r = Math.random() * 16 | 0;
      return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
  }

  // ── Format price ──────────────────────────────────────────────────────────
  function fmt(cents) {
    return (cents / 100).toFixed(2) + " DA";
  }

  // ── Fetch product catalog ─────────────────────────────────────────────────
  async function fetchProducts() {
    try {
      showLoading(true);
      const resp = await fetch("/api/products");
      if (!resp.ok) throw new Error("فشل تحميل المنتجات");
      menuData = await resp.json();
      renderProducts();
    } catch (err) {
      productGrid.innerHTML = `
        <div class="col-12 text-center py-5 text-danger">
          <i class="bi bi-exclamation-octagon fs-1"></i>
          <p class="mt-2">خطأ في تحميل المنتجات</p>
          <button class="btn btn-outline-warning btn-sm mt-2" id="btn-retry-load">إعادة المحاولة</button>
        </div>`;
      document.getElementById("btn-retry-load")?.addEventListener("click", fetchProducts);
    } finally {
      showLoading(false);
    }
  }

  function showLoading(show) {
    if (show) {
      productGrid.innerHTML = `
        <div class="loading-spinner">
          <div class="spinner-border text-warning" role="status"></div>
          <p>جاري التحميل…</p>
        </div>`;
    }
  }

  // ── Render products ───────────────────────────────────────────────────────
  function renderProducts() {
    productGrid.innerHTML = "";
    let items = [];
    if (currentCategory === "all") {
      menuData.forEach(cat => { items = items.concat(cat.products); });
    } else {
      const cat = menuData.find(c => c.id.toString() === currentCategory.toString());
      if (cat) items = cat.products;
    }

    if (!items.length) {
      productGrid.innerHTML = `<div class="col-12 text-center py-5 text-muted"><p>لا توجد منتجات</p></div>`;
      return;
    }

    items.forEach(product => {
      const card = document.createElement("div");
      card.className = "product-card";
      card.setAttribute("data-id", product.id);

      // Support external URLs (Unsplash) as well as local uploads
      const imgSrc = product.image
        ? (product.image.startsWith("http") ? product.image : `/static/uploads/products/${product.image}`)
        : null;

      const imgHTML = imgSrc
        ? `<img class="product-card__img" src="${imgSrc}" alt="${product.name}" loading="lazy">`
        : `<div class="product-card__img-placeholder"><i class="bi bi-cup-hot"></i></div>`;

      card.innerHTML = `
        ${imgHTML}
        <div class="product-card__body">
          <div class="product-card__name">${product.name}</div>
          <div class="product-card__price">${product.price_display}</div>
          <div class="product-card__tap-hint"><i class="bi bi-printer"></i> اطبع</div>
        </div>
        <div class="product-card__flash" id="flash-${product.id}"></div>
      `;

      card.addEventListener("click", () => instantOrder(product, card));
      productGrid.appendChild(card);
    });
  }

  // ── 1-Click instant order & print ────────────────────────────────────────
  async function instantOrder(product, cardEl) {
    if (printInProgress) return;
    printInProgress = true;

    // Visual feedback — flash the card green
    cardEl.classList.add("card-printing");

    const localId = uuid();
    try {
      const resp = await fetch("/api/orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          local_id: localId,
          device_id: "Kiosk-1",
          items: [{ product_id: product.id, quantity: 1 }]
        })
      });

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || "Order failed");
      }

      const data = await resp.json();

      // ── Print ticket silently via iframe ──────────────────────────────────
      printTicket(data.order_id, product);

      // ── Add to feed ───────────────────────────────────────────────────────
      addToFeed(product, data.order_id);

      // ── Update running total ──────────────────────────────────────────────
      runningTotalCents += product.price_cents;
      document.getElementById("total-amount").textContent = fmt(runningTotalCents);

      // Trigger background sync (don't await — keep it fast)
      fetch("/api/sync", { method: "POST" }).catch(() => {});

    } catch (err) {
      console.error("Order error:", err);
      showFlash(product.id, false);
    } finally {
      setTimeout(() => {
        cardEl.classList.remove("card-printing");
        printInProgress = false;
      }, 600);
    }
  }

  // ── Silent browser print (perfectly compatible with all printers) ─────────
  function printTicket(orderId, product) {
    browserFallbackPrint(orderId);
  }

  // ── Browser fallback (only if printer is disconnected) ────────────────────
  function browserFallbackPrint(orderId) {
    const iframe = document.getElementById("print-iframe");
    iframe.src = `/api/print-receipt/${orderId}`;
  }

  // ── Toast notification ────────────────────────────────────────────────────
  function showToast(msg, type = "info") {
    const toast = document.createElement("div");
    toast.style.cssText = `
      position:fixed; bottom:24px; left:50%; transform:translateX(-50%);
      background:${type === "error" ? "#ef4444" : "#22c55e"};
      color:#fff; padding:12px 24px; border-radius:999px;
      font-weight:700; font-size:.95rem; z-index:9999;
      animation: fadeOut 3s ease forwards;
    `;
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
  }

  // ── Live ticket feed ──────────────────────────────────────────────────────
  function addToFeed(product, orderId) {
    feedEmpty.style.display = "none";
    const now = new Date();
    const timeStr = now.toLocaleTimeString("ar-DZ", { hour: "2-digit", minute: "2-digit" });

    const item = document.createElement("div");
    item.className = "feed-item";
    item.innerHTML = `
      <div class="feed-item__icon"><i class="bi bi-receipt-cutoff"></i></div>
      <div class="feed-item__info">
        <div class="feed-item__name">${product.name}</div>
        <div class="feed-item__meta">طلب #${orderId} · ${timeStr}</div>
      </div>
      <div class="feed-item__badge">✓</div>
    `;
    ticketFeedItems.insertBefore(item, ticketFeedItems.firstChild);
  }

  // ── Category filtering ────────────────────────────────────────────────────
  categoryNav.addEventListener("click", (e) => {
    const btn = e.target.closest(".cat-btn");
    if (!btn) return;
    document.querySelectorAll(".cat-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    currentCategory = btn.dataset.cat;
    renderProducts();
  });

  // ── Clear feed ────────────────────────────────────────────────────────────
  btnClearFeed.addEventListener("click", () => {
    ticketFeedItems.querySelectorAll(".feed-item").forEach(el => el.remove());
    feedEmpty.style.display = "flex";
    
    // Reset running total
    runningTotalCents = 0;
    document.getElementById("total-amount").textContent = fmt(runningTotalCents);
  });

  // ── Sync status badge ─────────────────────────────────────────────────────
  syncStatusBadge.addEventListener("click", async () => {
    syncLabel.textContent = "يتزامن…";
    syncStatusBadge.classList.add("syncing");
    try {
      await fetch("/api/sync", { method: "POST" });
      await fetch("/api/pull-products", { method: "POST" });
      syncLabel.textContent = "تم التزامن";
      await fetchProducts();
      setTimeout(() => { syncLabel.textContent = "جاهز"; syncStatusBadge.classList.remove("syncing"); }, 3000);
    } catch {
      syncLabel.textContent = "خطأ";
      syncStatusBadge.classList.remove("syncing");
      syncStatusBadge.classList.add("error");
      setTimeout(() => { syncLabel.textContent = "جاهز"; syncStatusBadge.classList.remove("error"); }, 5000);
    }
  });

  // ── Revenue feature ───────────────────────────────────────────────────────
  const btnShowRevenue = document.getElementById("btn-show-revenue");
  const revenueModal = new bootstrap.Modal(document.getElementById('revenueModal'));
  const btnFetchRevenue = document.getElementById("btn-fetch-revenue");
  const inputRevenueDate = document.getElementById("revenue-date");
  const txtRevenueResult = document.getElementById("revenue-result");

  if (btnShowRevenue) {
    btnShowRevenue.addEventListener("click", () => {
      inputRevenueDate.value = ""; // Default empty to show today
      txtRevenueResult.textContent = "0.00 DA";
      revenueModal.show();
    });
  }

  if (btnFetchRevenue) {
    btnFetchRevenue.addEventListener("click", async () => {
      const dateVal = inputRevenueDate.value;
      txtRevenueResult.textContent = "جاري الحساب...";
      
      try {
        const url = dateVal ? `/api/revenue?date=${dateVal}` : "/api/revenue";
        const resp = await fetch(url);
        if (!resp.ok) {
          const errData = await resp.json();
          throw new Error(errData.error || "خطأ في جلب الإيرادات");
        }
        const data = await resp.json();
        txtRevenueResult.textContent = data.total_display;
      } catch (err) {
        txtRevenueResult.textContent = "خطأ";
        showToast(err.message, "error");
      }
    });
  }

  // ── Boot ──────────────────────────────────────────────────────────────────
  fetchProducts();
});
