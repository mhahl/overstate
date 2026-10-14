document.addEventListener('DOMContentLoaded', function () {
  var toggle = document.getElementById('nav');
  var opener = document.getElementById('nav-open');
  var closer = document.getElementById('nav-close');
  var panel = document.getElementById('nav-panel');
  if (!toggle || !opener || !closer || !panel) return;

  function sync() {
    opener.setAttribute('aria-expanded', String(toggle.checked));
    if (!toggle.checked && panel.contains(document.activeElement)) opener.focus();
  }
  opener.addEventListener('click', function () {
    toggle.checked = true;
    sync();
    closer.focus();
  });
  closer.addEventListener('click', function () {
    toggle.checked = false;
    sync();
  });
  toggle.addEventListener('change', sync); // Also handles the drawer overlay.
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && toggle.checked) {
      toggle.checked = false;
      sync();
    }
  });
  sync();
});
