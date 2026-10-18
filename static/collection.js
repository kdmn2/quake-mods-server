/* Collection selection: checkboxes, select-all (this view = the add-ons in the current filtered list),
   clear, a live selected-count, and the batch action bar. */
(function () {
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
    form.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = on.length === 0; });
  }
  all.addEventListener("click", function () {
    picks().forEach(function (p) { p.checked = all.checked; });
    update();
  });
  picks().forEach(function (p) { p.addEventListener("change", update); });
  update();
})();
