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
        
        // Show flash message alert dynamically
        const flashContainer = document.querySelector(".flash-container");
        if (flashContainer) {
          const alertDiv = document.createElement("div");
          alertDiv.className = `alert alert-${data.success ? "success" : "danger"} alert-dismissible fade show flash-msg`;
          alertDiv.role = "alert";
          alertDiv.innerHTML = `
            ${data.message}
            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="إغلاق"></button>
          `;
          flashContainer.appendChild(alertDiv);
          
          // Auto-dismiss after 5s
          setTimeout(() => {
            const bsAlert = bootstrap.Alert.getOrCreateInstance(alertDiv);
            if (bsAlert) bsAlert.close();
          }, 5000);
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
