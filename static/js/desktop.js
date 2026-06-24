/**
 * desktop.js — Cart-based POS kiosk.
 *
 * Workflow:
 *   1. Tap product → adds to cart (badge shows qty on card)
 *   2. Adjust quantities in cart panel with +/- buttons
 *   3. Choose print mode:
 *      - "تذاكر البارستا" → POST /api/orders → prints individual barista slip per item unit
 *      - "فاتورة الطاولة"  → POST /api/orders → prints single consolidated invoice (outdoor tables)
 *
 * Order history slide panel shows today's confirmed orders via GET /api/orders/history
 */

document.addEventListener("DOMContentLoaded", () => {
  // ── State ─────────────────────────────────────────────────────────────────
  let menuData = [];
  let currentCategory = "all";
  let printInProgress = false;

  // cart: { [productId]: { product: {...}, qty: N } }
  const cart = {};

  // ── DOM refs ──────────────────────────────────────────────────────────────
  const productGrid      = document.getElementById("product-grid");
  const categoryNav      = document.getElementById("category-nav");
  const syncStatusBadge  = document.getElementById("sync-status");
  const syncLabel        = document.getElementById("sync-label");
  const cartItemsEl      = document.getElementById("cart-items");
  const cartEmptyEl      = document.getElementById("cart-empty");
  const cartTotalAmount  = document.getElementById("cart-total-amount");
  const btnClearCart     = document.getElementById("btn-clear-cart");
  const btnPrintSlips    = document.getElementById("btn-print-slips");
  const btnPrintInvoice  = document.getElementById("btn-print-invoice");
  const printIframe      = document.getElementById("print-iframe");

  // ── Helpers ───────────────────────────────────────────────────────────────
  function uuid() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const r = Math.random() * 16 | 0;
      return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
  }

  function fmt(cents) {
    return (cents / 100).toFixed(2) + " DA";
  }

  function cartTotal() {
    return Object.values(cart).reduce((sum, e) => sum + e.product.price_cents * e.qty, 0);
  }

  function cartIsEmpty() {
    return Object.keys(cart).length === 0;
  }

  // ── Product catalog ───────────────────────────────────────────────────────
  async function fetchProducts() {
    showLoading(true);
    try {
      const resp = await fetch(`/api/products?t=${Date.now()}`);
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

      const imgSrc = product.image
        ? (product.image.startsWith("http") ? product.image : `/static/uploads/products/${product.image}`)
        : null;

      const imgHTML = imgSrc
        ? `<img class="product-card__img" src="${imgSrc}" alt="${product.name}" loading="lazy">`
        : `<div class="product-card__img-placeholder"><i class="bi bi-fire"></i></div>`;

      const qtyInCart = cart[product.id] ? cart[product.id].qty : 0;

      card.innerHTML = `
        ${imgHTML}
        <div class="product-card__body">
          <div class="product-card__name">${product.name}</div>
          <div class="product-card__price">${product.price_display}</div>
          <div class="product-card__tap-hint"><i class="bi bi-plus-circle"></i> أضف للسلة</div>
        </div>
        <div class="product-card__flash" id="flash-${product.id}"></div>
        ${qtyInCart > 0 ? `<div class="cart-badge" id="badge-${product.id}">${qtyInCart}</div>` : `<div class="cart-badge" id="badge-${product.id}" style="display:none;">${qtyInCart}</div>`}
      `;

      card.addEventListener("click", () => addToCart(product, card));
      productGrid.appendChild(card);
    });
  }

  // ── Cart logic ────────────────────────────────────────────────────────────
  function addToCart(product, cardEl) {
    if (cart[product.id]) {
      cart[product.id].qty += 1;
    } else {
      cart[product.id] = { product, qty: 1 };
    }

    // Flash card
    cardEl.classList.add("card-printing");
    setTimeout(() => cardEl.classList.remove("card-printing"), 400);

    // Update badge on card
    updateCardBadge(product.id);

    // Re-render cart panel
    renderCart();
  }

  function removeFromCart(productId, delta = null) {
    if (!cart[productId]) return;
    if (delta !== null) {
      cart[productId].qty += delta;
      if (cart[productId].qty <= 0) {
        delete cart[productId];
      }
    } else {
      delete cart[productId];
    }
    updateCardBadge(productId);
    renderCart();
  }

  function updateCardBadge(productId) {
    const badge = document.getElementById(`badge-${productId}`);
    if (!badge) return;
    const qty = cart[productId] ? cart[productId].qty : 0;
    if (qty > 0) {
      badge.textContent = qty;
      badge.style.display = "flex";
    } else {
      badge.style.display = "none";
    }
  }

  function renderCart() {
    // Remove existing cart item rows (keep the empty state div)
    cartItemsEl.querySelectorAll(".cart-item-row").forEach(el => el.remove());

    const isEmpty = cartIsEmpty();
    cartEmptyEl.style.display = isEmpty ? "flex" : "none";

    if (!isEmpty) {
      Object.values(cart).forEach(({ product, qty }) => {
        const row = document.createElement("div");
        row.className = "cart-item-row";
        row.setAttribute("data-pid", product.id);
        const subtotal = fmt(product.price_cents * qty);

        row.innerHTML = `
          <div class="cart-item-name">${product.name}</div>
          <div class="cart-item-controls">
            <button class="qty-btn qty-btn--minus" data-pid="${product.id}" aria-label="تقليل">
              <i class="bi bi-dash"></i>
            </button>
            <span class="qty-display">${qty}</span>
            <button class="qty-btn qty-btn--plus" data-pid="${product.id}" aria-label="زيادة">
              <i class="bi bi-plus"></i>
            </button>
          </div>
          <div class="cart-item-price">${subtotal}</div>
          <button class="cart-item-delete" data-pid="${product.id}" aria-label="حذف">
            <i class="bi bi-x"></i>
          </button>
        `;
        // Insert before the cart-empty div to maintain order
        cartItemsEl.insertBefore(row, cartEmptyEl);
      });

      // Wire up quantity buttons
      cartItemsEl.querySelectorAll(".qty-btn--minus").forEach(btn => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          removeFromCart(btn.dataset.pid, -1);
        });
      });
      cartItemsEl.querySelectorAll(".qty-btn--plus").forEach(btn => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          const pid = btn.dataset.pid;
          const entry = Object.values(cart).find(e => e.product.id.toString() === pid);
          if (entry) addToCart(entry.product, document.querySelector(`[data-id="${pid}"]`) || btn);
        });
      });
      cartItemsEl.querySelectorAll(".cart-item-delete").forEach(btn => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          removeFromCart(btn.dataset.pid);
        });
      });
    }

    // Update total and button states
    const total = cartTotal();
    cartTotalAmount.textContent = fmt(total);
    const hasItems = !isEmpty;
    btnPrintSlips.disabled = !hasItems;
    btnPrintInvoice.disabled = !hasItems;
  }

  // ── Clear cart ────────────────────────────────────────────────────────────
  btnClearCart.addEventListener("click", () => {
    if (cartIsEmpty()) return;
    // Clear all badges
    Object.keys(cart).forEach(pid => {
      cart[pid] = null;
      const badge = document.getElementById(`badge-${pid}`);
      if (badge) badge.style.display = "none";
    });
    // Wipe cart object
    for (const k in cart) delete cart[k];
    renderCart();
  });

  // ── Place order and print ─────────────────────────────────────────────────
  async function placeOrder(printMode) {
    if (cartIsEmpty() || printInProgress) return;
    printInProgress = true;
    btnPrintSlips.disabled = true;
    btnPrintInvoice.disabled = true;

    const localId = uuid();
    const items = Object.values(cart).map(({ product, qty }) => ({
      product_id: product.id,
      quantity: qty,
    }));

    try {
      const resp = await fetch("/api/orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          local_id: localId,
          device_id: "Kiosk-1",
          items,
        }),
      });

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || "فشل إنشاء الطلب");
      }

      const data = await resp.json();
      const orderId = data.order_id;

      // Trigger the correct print mode
      if (printMode === "invoice") {
        printIframe.src = `/api/print-invoice/${orderId}`;
        showToast("جاري طباعة فاتورة الطاولة…", "success");
      } else {
        printIframe.src = `/api/print-receipt/${orderId}`;
        showToast("جاري طباعة التذاكر…", "success");
      }

      // Confirm order after a short delay (receipt iframe triggers print)
      setTimeout(() => {
        fetch(`/api/orders/${orderId}/confirm`, { method: "POST" }).catch(() => {});
        fetch("/api/sync", { method: "POST" }).catch(() => {});
      }, 500);

      // Clear cart after successful print trigger
      for (const k in cart) {
        const badge = document.getElementById(`badge-${k}`);
        if (badge) badge.style.display = "none";
        delete cart[k];
      }
      renderCart();

    } catch (err) {
      console.error("Order error:", err);
      showToast(err.message || "خطأ في الطلب", "error");
    } finally {
      printInProgress = false;
      renderCart(); // Re-enable buttons based on cart state
    }
  }

  btnPrintSlips.addEventListener("click", () => placeOrder("slips"));
  btnPrintInvoice.addEventListener("click", () => placeOrder("invoice"));

  // ── Category filtering ────────────────────────────────────────────────────
  categoryNav.addEventListener("click", (e) => {
    const btn = e.target.closest(".cat-btn");
    if (!btn) return;
    document.querySelectorAll(".cat-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    currentCategory = btn.dataset.cat;
    renderProducts();
  });

  // ── Toast notification ────────────────────────────────────────────────────
  function showToast(msg, type = "info") {
    const toast = document.createElement("div");
    toast.style.cssText = `
      position:fixed; bottom:24px; left:50%; transform:translateX(-50%);
      background:${type === "error" ? "#ef4444" : "#22c55e"};
      color:#fff; padding:12px 24px; border-radius:999px;
      font-weight:700; font-size:.95rem; z-index:9999;
      box-shadow: 0 4px 20px rgba(0,0,0,0.3);
      animation: toastFadeOut 3s ease forwards;
    `;
    toast.textContent = msg;
    if (!document.getElementById("toast-keyframes")) {
      const style = document.createElement("style");
      style.id = "toast-keyframes";
      style.textContent = `@keyframes toastFadeOut { 0%,70%{opacity:1;} 100%{opacity:0;} }`;
      document.head.appendChild(style);
    }
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
  }

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

  // ── Manual Sync Now button ────────────────────────────────────────────────
  const btnManualSync = document.getElementById("btn-manual-sync");
  const btnManualSyncIcon = document.getElementById("btn-manual-sync-icon");

  if (btnManualSync) {
    btnManualSync.addEventListener("click", async () => {
      btnManualSync.disabled = true;
      if (btnManualSyncIcon) btnManualSyncIcon.classList.add("bi-spin");
      syncLabel.textContent = "يتزامن…";
      syncStatusBadge.classList.add("syncing");

      try {
        const resp = await fetch("/api/sync-all", { method: "POST" });
        if (!resp.ok) {
          const errData = await resp.json();
          throw new Error(errData.error || "خطأ في المزامنة");
        }
        const data = await resp.json();
        syncLabel.textContent = "تم التزامن";
        showToast(data.message || "تمت المزامنة بنجاح", "success");
        await fetchProducts();
        setTimeout(() => {
          syncLabel.textContent = "جاهز";
          syncStatusBadge.classList.remove("syncing");
        }, 3000);
      } catch (err) {
        console.error("Manual sync failed:", err);
        syncLabel.textContent = "خطأ";
        syncStatusBadge.classList.remove("syncing");
        syncStatusBadge.classList.add("error");
        showToast(err.message || "فشلت المزامنة", "error");
        setTimeout(() => {
          syncLabel.textContent = "جاهز";
          syncStatusBadge.classList.remove("error");
        }, 5000);
      } finally {
        btnManualSync.disabled = false;
        if (btnManualSyncIcon) btnManualSyncIcon.classList.remove("bi-spin");
      }
    });
  }

  // ── Revenue feature ───────────────────────────────────────────────────────
  const btnShowRevenue = document.getElementById("btn-show-revenue");
  const revenueModal = new bootstrap.Modal(document.getElementById("revenueModal"));
  const btnFetchRevenue = document.getElementById("btn-fetch-revenue");
  const inputStartDate = document.getElementById("revenue-start-date");
  const inputEndDate = document.getElementById("revenue-end-date");
  const txtRevenueResult = document.getElementById("revenue-result");

  if (btnShowRevenue) {
    btnShowRevenue.addEventListener("click", () => {
      inputStartDate.value = "";
      inputEndDate.value = "";
      txtRevenueResult.textContent = "0.00 DA";
      revenueModal.show();
    });
  }

  if (btnFetchRevenue) {
    btnFetchRevenue.addEventListener("click", async () => {
      const startVal = inputStartDate.value;
      const endVal = inputEndDate.value;
      txtRevenueResult.textContent = "جاري الحساب...";
      try {
        let url = "/api/revenue";
        if (startVal || endVal) {
          const params = new URLSearchParams();
          if (startVal) params.append("start_date", startVal);
          if (endVal) params.append("end_date", endVal);
          url += "?" + params.toString();
        }
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

  // ── Orders History Slide Panel ────────────────────────────────────────────
  const btnShowHistory  = document.getElementById("btn-show-history");
  const btnCloseHistory = document.getElementById("btn-close-history");
  const historyPanel    = document.getElementById("history-panel");
  const historyOverlay  = document.getElementById("history-overlay");
  const historyList     = document.getElementById("history-list");
  const historyLoading  = document.getElementById("history-loading");
  const historyEmpty    = document.getElementById("history-empty");
  const inputHistoryDate = document.getElementById("history-date");

  if (inputHistoryDate) {
    const today = new Date();
    const yyyy = today.getFullYear();
    const mm = String(today.getMonth() + 1).padStart(2, '0');
    const dd = String(today.getDate()).padStart(2, '0');
    inputHistoryDate.value = `${yyyy}-${mm}-${dd}`;

    inputHistoryDate.addEventListener("change", () => {
      loadOrderHistory();
    });
  }

  function openHistoryPanel() {
    historyPanel.classList.add("open");
    historyOverlay.classList.add("open");
    document.body.style.overflow = "hidden";
    loadOrderHistory();
  }

  function closeHistoryPanel() {
    historyPanel.classList.remove("open");
    historyOverlay.classList.remove("open");
    document.body.style.overflow = "";
  }

  btnShowHistory.addEventListener("click", openHistoryPanel);
  btnCloseHistory.addEventListener("click", closeHistoryPanel);
  historyOverlay.addEventListener("click", closeHistoryPanel);

  async function loadOrderHistory() {
    historyList.innerHTML = "";
    historyLoading.style.display = "flex";
    historyEmpty.style.display = "none";

    try {
      const selectedDate = inputHistoryDate ? inputHistoryDate.value : "";
      let url = "/api/orders/history";
      if (selectedDate) {
        url += `?date=${selectedDate}`;
      }
      const resp = await fetch(url);
      if (!resp.ok) throw new Error("فشل تحميل السجل");
      const orders = await resp.json();

      historyLoading.style.display = "none";

      if (!orders.length) {
        historyEmpty.style.display = "flex";
        return;
      }

      orders.forEach(order => {
        const card = document.createElement("div");
        card.className = "history-order-card";

        const statusClass = {
          pending: "status-pending",
          synced: "status-synced",
          failed: "status-failed",
        }[order.status] || "status-pending";

        const statusLabel = {
          pending: "معلق",
          synced: "مُزامن",
          failed: "فشل",
        }[order.status] || order.status;

        const itemsHtml = order.items.map(item => `
          <div class="history-item-row">
            <span class="history-item-name">${item.name}</span>
            <span class="history-item-qty">× ${item.qty}</span>
            <span class="history-item-sub">${item.subtotal}</span>
          </div>
        `).join("");

        card.innerHTML = `
          <div class="history-order-meta">
            <span class="history-order-time"><i class="bi bi-clock"></i> ${order.created_at}</span>
            <span class="history-order-id">#${order.id}</span>
            <span class="history-status-badge ${statusClass}">${statusLabel}</span>
          </div>
          <div class="history-order-items">${itemsHtml}</div>
          <div class="history-order-total">
            <span>الإجمالي:</span>
            <strong>${order.total_display}</strong>
          </div>
        `;
        historyList.appendChild(card);
      });
    } catch (err) {
      historyLoading.style.display = "none";
      historyList.innerHTML = `<div class="text-center text-danger py-3"><i class="bi bi-exclamation-circle"></i> ${err.message}</div>`;
    }
  }

  // ── Boot ──────────────────────────────────────────────────────────────────
  fetchProducts();
  renderCart();
});
