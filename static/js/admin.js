/**
 * admin.js — Admin dashboard layout helpers.
 */

document.addEventListener("DOMContentLoaded", () => {
  // Theme Toggle Logic
  const themeToggle = document.getElementById("theme-toggle");
  const themeToggleIcon = document.getElementById("theme-toggle-icon");

  function updateThemeUI(theme) {
    if (!themeToggleIcon) return;
    if (theme === "light") {
      themeToggleIcon.className = "bi bi-sun-fill";
    } else {
      themeToggleIcon.className = "bi bi-moon-stars-fill";
    }
  }

  // Sync visual icon with current theme
  const initialTheme = document.documentElement.getAttribute("data-theme") || "dark";
  updateThemeUI(initialTheme);

  if (themeToggle) {
    themeToggle.addEventListener("click", () => {
      const currentTheme = document.documentElement.getAttribute("data-theme") || "dark";
      const nextTheme = currentTheme === "light" ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", nextTheme);
      localStorage.setItem("theme", nextTheme);
      updateThemeUI(nextTheme);
    });
  }

  // Mobile sidebar toggle
  const sidebarToggle = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("admin-sidebar");

  if (sidebarToggle && sidebar) {
    sidebarToggle.addEventListener("click", (e) => {
      e.stopPropagation();
      sidebar.classList.toggle("show");
    });

    // Close sidebar when clicking outside on mobile
    document.addEventListener("click", (e) => {
      if (sidebar.classList.contains("show") && !sidebar.contains(e.target) && e.target !== sidebarToggle) {
        sidebar.classList.remove("show");
      }
    });
  }

  // Auto-dismiss alert notifications after 5 seconds
  const flashAlerts = document.querySelectorAll(".flash-msg");
  flashAlerts.forEach((alert) => {
    setTimeout(() => {
      // Use Bootstrap 5 Alert API to close
      const bsAlert = bootstrap.Alert.getOrCreateInstance(alert);
      if (bsAlert) {
        bsAlert.close();
      }
    }, 5000);
  });

  // Sync databases manually
  const btnSyncDatabases = document.getElementById("btn-sync-databases");
  const syncDatabasesIcon = document.getElementById("sync-databases-icon");

  if (btnSyncDatabases) {
    btnSyncDatabases.addEventListener("click", async () => {
      btnSyncDatabases.disabled = true;
      if (syncDatabasesIcon) syncDatabasesIcon.classList.add("bi-spin");
      
      try {
        const resp = await fetch("/admin/sync-databases", {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          }
        });
        const data = await resp.json();
        
        let alertClass = data.success ? "success" : "danger";
        if (data.details && !data.success && data.details.reachable_count > 0) {
          // Semi-successful or skip status
          alertClass = "warning";
        }
        
        // Show flash message alert dynamically
        const flashContainer = document.querySelector(".flash-container");
        if (flashContainer) {
          const alertDiv = document.createElement("div");
          alertDiv.className = `alert alert-${alertClass} alert-dismissible fade show flash-msg`;
          alertDiv.role = "alert";
          
          let msgHtml = `<strong>${data.message}</strong>`;
          if (data.details) {
            const d = data.details;
            msgHtml += `<hr class="my-2" style="border-color: rgba(0,0,0,0.15)">`;
            msgHtml += `<div class="small" style="line-height: 1.5; font-size: 0.85rem;">`;
            msgHtml += `<div><strong>قواعد البيانات الناجحة (${d.synced_databases ? d.synced_databases.length : 0}/${d.total_count || 0}):</strong></div>`;
            if (d.synced_databases && d.synced_databases.length > 0) {
              d.synced_databases.forEach(db => {
                msgHtml += `<div class="text-success" style="direction: ltr; text-align: right;">&bull; ${db}</div>`;
              });
            } else {
              msgHtml += `<div class="text-muted">&bull; لا يوجد</div>`;
            }
            if (d.failed_databases && d.failed_databases.length > 0) {
              msgHtml += `<div class="mt-1"><strong>قواعد البيانات غير المتصلة:</strong></div>`;
              d.failed_databases.forEach(db => {
                msgHtml += `<div class="text-danger" style="direction: ltr; text-align: right;">&bull; ${db}</div>`;
              });
            }
            if (d.merged_counts && Object.keys(d.merged_counts).length > 0) {
              msgHtml += `<div class="mt-1"><strong>إجمالي السجلات المدمجة:</strong></div>`;
              msgHtml += `<div>المنتجات: ${d.merged_counts.products || 0} | الفئات: ${d.merged_counts.categories || 0} | الطلبات: ${d.merged_counts.orders || 0} | مفاتيح الترخيص: ${d.merged_counts.serial_keys || 0}</div>`;
            }
            msgHtml += `</div>`;
          }
          
          alertDiv.innerHTML = `
            ${msgHtml}
            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="إغلاق"></button>
          `;
          flashContainer.appendChild(alertDiv);
          
          // Auto-dismiss after 15s if it has details, otherwise 5s
          const dismissTime = data.details ? 15000 : 5000;
          setTimeout(() => {
            const bsAlert = bootstrap.Alert.getOrCreateInstance(alertDiv);
            if (bsAlert) bsAlert.close();
          }, dismissTime);
        }
      } catch (err) {
        console.error("Database sync failed:", err);
      } finally {
        btnSyncDatabases.disabled = false;
        if (syncDatabasesIcon) syncDatabasesIcon.classList.remove("bi-spin");
      }
    });
  }
});
