/* File editor bundle entry. Built with esbuild into
   overstate_ui/static/editor.bundle.js (see `npm run build:editor`).
   Progressive enhancement only: the form posts the plain textarea, so
   saving works when this bundle is missing or JS is disabled. */

import { EditorView, basicSetup } from "codemirror";
import { yaml } from "@codemirror/lang-yaml";

(function () {
  var mount = document.getElementById("editor-mount");
  var field = document.getElementById("editor-textarea");
  if (!mount || !field || typeof EditorView === "undefined") return;

  var extensions = [
    basicSetup,
    EditorView.updateListener.of(function (update) {
      if (update.docChanged) field.value = update.state.doc.toString();
    }),
  ];
  if (mount.getAttribute("data-lang") === "yaml") extensions.push(yaml());

  new EditorView({ doc: field.value, extensions: extensions, parent: mount });
  field.hidden = true;
})();
