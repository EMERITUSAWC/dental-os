document.documentElement.classList.add("js");                 // 0.55: phones get a menu button only when this script runs
document.addEventListener("click", function (e) {
  if (e.target.closest("[data-print]")) { window.print(); }
  var m = e.target.closest("[data-menu]");
  if (m) {
    var top = m.closest("header.top");
    var open = top.classList.toggle("open");
    m.setAttribute("aria-expanded", open ? "true" : "false");
  }
  // Grouped top menu (v0.70): a click outside an open group closes it.
  document.querySelectorAll("details[data-navgroup][open]").forEach(function (d) {
    if (!d.contains(e.target)) { d.open = false; }
  });
});
document.addEventListener("toggle", function (e) {
  // Only one group open at a time.
  var d = e.target;
  if (!d.matches || !d.matches("details[data-navgroup]") || !d.open) { return; }
  document.querySelectorAll("details[data-navgroup][open]").forEach(function (o) { if (o !== d) { o.open = false; } });
}, true);
document.addEventListener("keydown", function (e) {
  if (e.key !== "Escape") { return; }
  document.querySelectorAll("details[data-navgroup][open]").forEach(function (d) {
    d.open = false;
    var s = d.querySelector("summary"); if (s && d.contains(document.activeElement)) { s.focus(); }
  });
});

(function () {
  // Who is ringing the clinic's phone (version 0.21). Asks the server every five seconds; shows nothing when no call is ringing.
  var pop = document.getElementById("callpop");
  if (!pop || !window.fetch) { return; }
  function show(calls) {
    pop.textContent = "";
    if (!calls.length) { pop.hidden = true; return; }
    calls.forEach(function (c) {
      var line = document.createElement("p");
      line.appendChild(document.createTextNode(pop.getAttribute("data-label") + " "));
      var a = document.createElement("a");
      a.href = c.url;
      a.textContent = c.name ? c.name + " (" + c.from + ")" : c.from + ": " + pop.getAttribute("data-unknown");
      line.appendChild(a);
      pop.appendChild(line);
    });
    pop.hidden = false;
  }
  function ask() {
    fetch(pop.getAttribute("data-live"), { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : { calls: [] }; })
      .then(function (d) { show(d.calls || []); })
      .catch(function () {});
  }
  ask();
  setInterval(ask, 5000);
})();

// 0.68: a form sent twice on a slow connection books, charges or sends twice. Once a form is sent, its buttons wait.
document.addEventListener("submit", function (e) {
  var form = e.target;
  if (!form || (form.method || "").toLowerCase() !== "post" || form.hasAttribute("data-again")) { return; }
  if (form.getAttribute("data-sent") === "1") { e.preventDefault(); return; }
  form.setAttribute("data-sent", "1");
  var buttons = form.querySelectorAll("button[type=submit], button:not([type]), input[type=submit]");
  setTimeout(function () { buttons.forEach(function (b) { b.disabled = true; b.setAttribute("aria-busy", "true"); }); }, 0);
  setTimeout(function () {                                   // a download or an error that stays on the page: allow again
    form.removeAttribute("data-sent");
    buttons.forEach(function (b) { b.disabled = false; b.removeAttribute("aria-busy"); });
  }, 8000);
});
