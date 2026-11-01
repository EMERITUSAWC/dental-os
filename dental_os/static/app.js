document.documentElement.classList.add("js");                 // 0.55: phones get a menu button only when this script runs
document.addEventListener("click", function (e) {
  if (e.target.closest("[data-print]")) { window.print(); }
  var m = e.target.closest("[data-menu]");
  if (m) {
    var top = m.closest("header.top");
    var open = top.classList.toggle("open");
    m.setAttribute("aria-expanded", open ? "true" : "false");
  }
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
