// Paste guard for the answer and explain-back textareas
// (templates/answer.html, templates/tutorial.html).
//
// Friction, not enforcement: anyone can retype text, turn JavaScript off,
// or POST the form directly, and grading is still what decides. Only
// pasting *in* is blocked — copying out of the diff is fine.
//
// The hidden `js_active` field is how the server tells a submission made
// with this script running from one made without it; see
// migrations/V6__paste_guard.sql and app/web.py:_log_if_unguarded.
//
// Served same-origin under `script-src 'self'` (app/main.py). No inline
// script anywhere, so keep it that way: behaviour lives in this file only.
(function () {
  "use strict";

  var BLOCKED_INPUT_TYPES = {
    insertFromPaste: true,
    insertFromPasteAsQuotation: true,
    insertFromDrop: true,
  };

  function guard(textarea) {
    var note = document.createElement("p");
    note.className = "paste-note";
    note.textContent = "Pasting is disabled — write this in your own words.";
    note.hidden = true;
    textarea.insertAdjacentElement("afterend", note);

    function block(event) {
      event.preventDefault();
      note.hidden = false;
    }

    textarea.addEventListener("paste", block);
    textarea.addEventListener("drop", block);
    // Catches paste routes that don't raise a `paste` event on every
    // browser, e.g. mobile keyboards' clipboard suggestions.
    textarea.addEventListener("beforeinput", function (event) {
      if (BLOCKED_INPUT_TYPES[event.inputType]) block(event);
    });

    var form = textarea.form;
    if (form && !form.querySelector('input[name="js_active"]')) {
      var flag = document.createElement("input");
      flag.type = "hidden";
      flag.name = "js_active";
      flag.value = "1";
      form.appendChild(flag);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("textarea").forEach(guard);
  });
})();
