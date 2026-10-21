/* File browser tree pane. Built with esbuild into
   overstate_ui/static/browser.bundle.js (see `npm run build:browser`).

   Progressive enhancement only: the tree navigates (folders) and opens
   files, but the server-rendered #file-tree-fallback link list stays in
   place when this bundle is missing, JSON parsing fails, or the mount
   point is absent — every destination is a plain GET the server
   already serves. */

import { Wunderbaum } from "wunderbaum";
import "wunderbaum/dist/wunderbaum.css";

/* Full literal icon class: Tailwind scans this file, and app.css
   force-generates it via `@source inline(...)` regardless. The tree
   carries folders only — files live in the list pane, which the server
   renders with per-type icons. */
var DIR_ICON = "icon-[lucide--folder]";

(function () {
  var mount = document.getElementById("file-tree");
  var raw = document.getElementById("file-tree-data");
  var fallback = document.getElementById("file-tree-fallback");
  if (!mount || !raw || typeof Wunderbaum === "undefined") return;

  var source;
  try {
    source = JSON.parse(raw.textContent || "[]");
  } catch (err) {
    return;
  }
  if (!Array.isArray(source) || source.length === 0) return;

  var indexUrl = mount.getAttribute("data-index-url") || "/files/";

  function targetFor(node) {
    return indexUrl + "?dir=" + encodeURIComponent(node.key || "");
  }

  /* Persist manual folder expansion across navigations. Keys are plain
     dir paths; the store is per-checkout so unrelated trees never leak
     into each other. Never throws: a blocked storage or a renamed tree
     just means the browser starts collapsed. */
  var STORE_KEY = "overstate:files-tree:expanded";
  function readExpanded() {
    try {
      var text = sessionStorage.getItem(STORE_KEY);
      if (!text) return {};
      var parsed = JSON.parse(text);
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (err) {
      return {};
    }
  }
  function writeExpanded() {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify(expanded));
    } catch (err) {
      /* ignore */
    }
  }
  var expanded = readExpanded();
  function markExpanded(key, value) {
    if (value) expanded[key] = 1;
    else delete expanded[key];
    writeExpanded();
  }

  try {
    var tree = new Wunderbaum({
      element: mount,
      source: source,
      checkbox: false,
      minExpandLevel: 1,
      activate: function (e) {
        if (e.node) window.location.assign(targetFor(e.node));
      },
      expand: function (e) {
        if (e.node && e.type === "expand") markExpanded(e.node.key, true);
      },
      collapse: function (e) {
        if (e.node && e.type === "collapse") markExpanded(e.node.key, false);
      },
      /* Titles render as plain text; hang our lucide icon next to the
         (hidden) stock icon so status re-renders cannot wipe it. */
      render: function (e) {
        var row = e.nodeElem;
        if (!row || !e.node) return;
        row.setAttribute("role", "treeitem");
        row.setAttribute("aria-level", String(e.node.getLevel()));
        if (e.node.isExpandable()) {
          row.setAttribute("aria-expanded", e.node.isExpanded() ? "true" : "false");
        } else {
          row.removeAttribute("aria-expanded");
        }
        row.setAttribute("aria-selected", e.node.isActive() ? "true" : "false");
        var stock = row.querySelector("i.wb-icon");
        if (stock) stock.style.display = "none";
        var old = row.querySelector(":scope > span.fb-icon");
        if (old) old.remove();
        var title = row.querySelector("span.wb-title");
        if (!title) return;
        var s = document.createElement("span");
        s.className = "fb-icon size-4 shrink-0 opacity-70 " + DIR_ICON;
        s.setAttribute("aria-hidden", "true");
        title.before(s);
      },
      init: function (e) {
        /* Restore the user's previous expansion after the first render. */
        var tree2 = e.tree;
        if (!tree2) return;
        Object.keys(expanded).forEach(function (key) {
          try {
            var node = tree2.getNodeByKey(key);
            if (node && !node.expanded) node.setExpanded(true);
          } catch (err) {
            /* ignore */
          }
        });
      },
    });
  } catch (err) {
    return;
  }
  if (fallback) fallback.hidden = true;
})();
