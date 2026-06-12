/**
 * admin.js — Admin dashboard layout helpers.
 */

document.addEventListener("DOMContentLoaded", () => {
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
});
