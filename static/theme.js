/* Theme switch: an explicit light/dark toggle in the header. The saved preference is applied pre-paint by an
   inline head script; storage failures (private mode, blocked storage) fall back to the system look silently. */
(function () {
  var btn = document.getElementById("theme-switch");
  if (!btn) return;

  function current() {
    return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }
  function apply(value, save) {
    try { if (save) window.localStorage.setItem("theme", value); } catch (e) { /* storage unavailable; page keeps working */ }
    document.documentElement.setAttribute("data-theme", value);
    update();
  }
  function update() {
    var t = current();
    var other = t === "dark" ? "light" : "dark";
    btn.textContent = (t === "dark" ? "☾ / ☀︎" : "☀︎ / ☾") + " Theme: " + t;
    btn.setAttribute("aria-label", "Switch the color theme; the page currently uses the " + t + " theme");
    btn.dataset.next = other;
  }
  btn.addEventListener("click", function () {
    apply(btn.dataset.next === "dark" ? "dark" : "light", true);
  });
  update();
})();
