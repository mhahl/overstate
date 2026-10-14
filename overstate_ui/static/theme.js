// Keep the choice across full-page navigation, including the login screen.
try {
  if (localStorage.getItem('overstate-theme') === 'dark') {
    document.documentElement.dataset.theme = 'dark';
  }
} catch (_) { /* Private browsing may block storage. */ }

document.addEventListener('DOMContentLoaded', function () {
  var control = document.getElementById('theme-toggle');
  if (!control) return;
  control.checked = document.documentElement.dataset.theme === 'dark';
  control.addEventListener('change', function () {
    var theme = control.checked ? 'dark' : 'cmyk';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('overstate-theme', theme); } catch (_) { /* Keep this page's choice. */ }
  });
});
