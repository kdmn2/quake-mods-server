/* Collection selection: checkboxes, select-all (current page only — the add-ons in the current view),
   clear, a live selected-count, and the batch action bar. Only the batch-action buttons in the toolbar
   depend on the selection; per-row Unpublish/Publish/Delete forms are independent of the selection.
   The same script also wires the per-row forms' ``data-confirm`` attribute to ``window.confirm`` so
   destructive actions still ask for confirmation without relying on inline ``onsubmit`` handlers. */
(function () {
  function wireConfirmation() {
    document.querySelectorAll("form[data-confirm]").forEach(function (f) {
      if (f.__confirmWired) return;
      f.__confirmWired = true;
      f.addEventListener("submit", function (ev) {
        var msg = f.getAttribute("data-confirm");
        if (msg && !window.confirm(msg)) { ev.preventDefault(); }
      });
    });
  }
  wireConfirmation();

  var form = document.getElementById("collection-form");
  if (!form) return;
  var bar = document.getElementById("batch-bar");
  var count = document.getElementById("sel-count");
  var all = document.getElementById("sel-all");

  function picks() {
    return Array.prototype.slice.call(form.querySelectorAll("input.pick"));
  }
  function update() {
    var on = picks().filter(function (p) { return p.checked; });
    bar.style.display = on.length ? "flex" : "none";
    count.textContent = on.length + " selected";
    var boxes = picks();
    all.checked = boxes.length > 0 && on.length === boxes.length;
    all.indeterminate = on.length > 0 && on.length < boxes.length;
    if (bar) {
      bar.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = on.length === 0; });
    }
  }
  if (all) {
    all.addEventListener("click", function () {
      picks().forEach(function (p) { p.checked = all.checked; });
      update();
    });
  }
  picks().forEach(function (p) { p.addEventListener("change", update); });
  update();
})();
