/**
 * desktop.js — Touch-friendly kiosk POS client code.
 */

document.addEventListener("DOMContentLoaded", () => {
  let cart = [];
  let menuData = []; // Array of categories, each containing products
  let currentCategory = "all";

  // Elements
  const productGrid = document.getElementById("product-grid");
  const categoryNav = document.getElementById("category-nav");
  const cartItemsContainer = document.getElementById("cart-items");
  const cartEmptyState = document.getElementById("cart-empty");
  const cartTotalVal = document.getElementById("cart-total");
  const btnPlaceOrder = document.getElementById("btn-place-order");
  const btnClearCart = document.getElementById("btn-clear-cart");
  const syncStatusBadge = document.getElementById("sync-status");
  const syncLabel = document.getElementById("sync-label");

  // Modal elements
  const orderModalEl = document.getElementById("orderModal");
  const orderModal = new bootstrap.Modal(orderModalEl);
  const modalOrderId = document.getElementById("modal-order-id");
  const btnNewOrder = document.getElementById("btn-new-order");
  const btnPrintReceipt = document.getElementById("btn-print-receipt");

  // UUID generator for order local_id
  function generateUUID() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
      const r = Math.random() * 16 | 0;
      const v = c === 'x' ? r : (r & 0x3 | 0x8);
      return v.toString(16);
    });
  }

  // Format cents to $XX.XX string
  function formatPrice(cents) {
    return "$" + (cents / 100).toFixed(2);
  }

  // Fetch product catalog
  async function fetchProducts() {
    try {
      showLoading(true);
      const resp = await fetch("/api/products");
      if (!resp.ok) throw new Error("Failed to fetch product catalog");
      menuData = await resp.json();
      renderProducts();
    } catch (err) {
      console.error(err);
      productGrid.innerHTML = `
        <div class="col-12 text-center py-5 text-danger">
          <i class="bi bi-exclamation-octagon fs-1"></i>
          <p class="mt-2">Error loading menu. Please try pulling products manually or check network.</p>
          <button class="btn btn-outline-warning btn-sm mt-2" id="btn-retry-load">Retry</button>
        </div>
      `;
      document.getElementById("btn-retry-load")?.addEventListener("click", fetchProducts);
    } finally {
      showLoading(false);
    }
  }

  function showLoading(show) {
    if (show) {
      productGrid.innerHTML = `
        <div class="loading-spinner">
          <div class="spinner-border text-warning" role="status">
            <span class="visually-hidden">Loading…</span>
          </div>
          <p>Loading menu…</p>
        </div>
      `;
    }
  }

  // Render products in grid based on category selection
  function renderProducts() {
    productGrid.innerHTML = "";
    let productsToRender = [];

    if (currentCategory === "all") {
      menuData.forEach(cat => {
        productsToRender = productsToRender.concat(cat.products);
      });
    } else {
      const selectedCat = menuData.find(cat => cat.id.toString() === currentCategory.toString());
      if (selectedCat) {
        productsToRender = selectedCat.products;
      }
    }

    if (productsToRender.length === 0) {
      productGrid.innerHTML = `
        <div class="col-12 text-center py-5 text-muted">
          <i class="bi bi-inbox fs-1"></i>
          <p class="mt-2">No products available in this category.</p>
        </div>
      `;
      return;
    }

    productsToRender.forEach(product => {
      const card = document.createElement("div");
      card.className = "product-card";
      
      let imgHTML = `<div class="product-card__img-placeholder"><i class="bi bi-cup-hot"></i></div>`;
      if (product.image) {
        imgHTML = `<img class="product-card__img" src="/static/uploads/products/${product.image}" alt="${product.name}">`;
      }

      card.innerHTML = `
        ${imgHTML}
        <div class="product-card__body">
          <div class="product-card__name">${product.name}</div>
          <div class="product-card__price">${product.price_display}</div>
        </div>
      `;

      card.addEventListener("click", () => addToCart(product));
      productGrid.appendChild(card);
    });
  }

  // Cart operations
  function addToCart(product) {
    const existing = cart.find(item => item.id === product.id);
    if (existing) {
      if (existing.quantity < 99) {
        existing.quantity += 1;
      }
    } else {
      cart.push({
        id: product.id,
        name: product.name,
        price_cents: product.price_cents,
        quantity: 1
      });
    }
    renderCart();
  }

  function changeQty(productId, delta) {
    const existing = cart.find(item => item.id === productId);
    if (!existing) return;

    existing.quantity += delta;
    if (existing.quantity <= 0) {
      cart = cart.filter(item => item.id !== productId);
    }
    renderCart();
  }

  function clearCart() {
    cart = [];
    renderCart();
  }

  function renderCart() {
    cartItemsContainer.innerHTML = "";

    if (cart.length === 0) {
      cartEmptyState.style.display = "flex";
      cartTotalVal.textContent = "$0.00";
      btnPlaceOrder.disabled = true;
      return;
    }

    cartEmptyState.style.display = "none";
    btnPlaceOrder.disabled = false;

    let totalCents = 0;

    cart.forEach(item => {
      const itemSubtotal = item.price_cents * item.quantity;
      totalCents += itemSubtotal;

      const itemEl = document.createElement("div");
      itemEl.className = "cart-item";
      itemEl.innerHTML = `
        <div class="cart-item__name">${item.name}</div>
        <div class="cart-item__price">${formatPrice(itemSubtotal)}</div>
        <div class="cart-item__controls">
          <button class="qty-btn btn-qty-minus" data-id="${item.id}">-</button>
          <span class="qty-value">${item.quantity}</span>
          <button class="qty-btn btn-qty-plus" data-id="${item.id}">+</button>
        </div>
      `;

      // Event listeners for qty controls
      itemEl.querySelector(".btn-qty-minus").addEventListener("click", () => changeQty(item.id, -1));
      itemEl.querySelector(".btn-qty-plus").addEventListener("click", () => changeQty(item.id, 1));

      cartItemsContainer.appendChild(itemEl);
    });

    cartTotalVal.textContent = formatPrice(totalCents);
  }

  // Category filtering
  categoryNav.addEventListener("click", (e) => {
    const btn = e.target.closest(".cat-btn");
    if (!btn) return;

    // Toggle active state
    document.querySelectorAll(".cat-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");

    currentCategory = btn.dataset.cat;
    renderProducts();
  });

  // Clear cart action
  btnClearCart.addEventListener("click", clearCart);

  // Submit Order
  btnPlaceOrder.addEventListener("click", async () => {
    if (cart.length === 0) return;

    btnPlaceOrder.disabled = true;
    const prevText = btnPlaceOrder.innerHTML;
    btnPlaceOrder.innerHTML = `<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Submitting...`;

    const localId = generateUUID();
    const payload = {
      local_id: localId,
      device_id: "Kiosk-Desktop",
      items: cart.map(item => ({
        product_id: item.id,
        quantity: item.quantity
      }))
    };

    try {
      const resp = await fetch("/api/orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      if (!resp.ok) {
        const errData = await resp.json();
        throw new Error(errData.error || "Failed to place order");
      }

      const data = await resp.json();
      
      // Order placed successfully
      modalOrderId.textContent = data.order_id || localId;
      btnPrintReceipt.href = `/api/print-receipt/${data.order_id}`;
      
      // Clear local cart and show modal
      clearCart();
      orderModal.show();

      // Trigger automatic manual sync in the background
      triggerSync(false);

    } catch (err) {
      alert("Order submission failed: " + err.message);
    } finally {
      btnPlaceOrder.innerHTML = prevText;
      btnPlaceOrder.disabled = cart.length === 0;
    }
  });

  // Modal reset flow
  btnNewOrder.addEventListener("click", () => {
    orderModal.hide();
  });

  // Manual trigger sync
  async function triggerSync(userInitiated = true) {
    if (userInitiated) {
      syncStatusBadge.classList.add("syncing");
      syncLabel.textContent = "Syncing...";
    }

    try {
      const ordersResp = await fetch("/api/sync", { method: "POST" });
      const productsResp = await fetch("/api/pull-products", { method: "POST" });

      if (ordersResp.ok && productsResp.ok) {
        if (userInitiated) {
          syncStatusBadge.classList.remove("syncing");
          syncLabel.textContent = "Synced";
          // Refetch UI menu catalog in case it changed
          await fetchProducts();
          setTimeout(() => {
            syncLabel.textContent = "Ready";
          }, 3000);
        }
      } else {
        throw new Error("Failed background sync sync/pull APIs");
      }
    } catch (err) {
      console.warn("Manual sync error: ", err);
      if (userInitiated) {
        syncStatusBadge.classList.remove("syncing");
        syncStatusBadge.classList.add("error");
        syncLabel.textContent = "Sync Fail";
        setTimeout(() => {
          syncStatusBadge.classList.remove("error");
          syncLabel.textContent = "Ready";
        }, 5000);
      }
    }
  }

  // Click badge to sync manually
  syncStatusBadge.addEventListener("click", () => triggerSync(true));

  // Initialize
  fetchProducts();
});
