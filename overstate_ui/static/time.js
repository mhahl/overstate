(function () {
  function labelFor(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return "just now";
    var m = Math.floor(s / 60);
    if (m < 60) return m + "m ago";
    var h = Math.floor(m / 60);
    if (h < 24) return h + "h ago";
    var d = Math.floor(h / 24);
    if (d < 7) return d + "d ago";
    if (d < 30) return Math.floor(d / 7) + "w ago";
    return null; // >= 30d: leave server DD/MM/YY alone
  }
  function refresh(root) {
    (root || document).querySelectorAll("time[data-rel][datetime]").forEach(function (el) {
      var t = Date.parse(el.getAttribute("datetime"));
      if (isNaN(t)) return;
      var label = labelFor(Date.now() - t);
      if (label) el.textContent = label;
    });
  }
  window.OverstateTime = { refresh: refresh };
  document.addEventListener("DOMContentLoaded", function () { refresh(document); });
  document.addEventListener("htmx:afterSwap", function (ev) { refresh(ev.target); });
  setInterval(function () { refresh(document); }, 60000);
})();
