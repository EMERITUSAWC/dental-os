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

(function () {
  // 0.74: a first-time guided tour. Steps whose part of the page is missing are skipped. Finishing or skipping marks it done.
  var holder = document.getElementById("tour");
  if (!holder || !document.querySelector) { return; }
  var steps = [];
  holder.querySelectorAll("p[data-sel]").forEach(function (p) {
    var el = document.querySelector(p.getAttribute("data-sel"));
    if (el) { steps.push({ el: el, text: p.textContent }); }
  });
  if (!steps.length) { return; }
  var i = 0, lit = null;
  var box = document.createElement("div");
  box.className = "tourbox";
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-live", "polite");
  box.setAttribute("tabindex", "-1");
  var text = document.createElement("p");
  var count = document.createElement("p");
  count.className = "small muted";
  var row = document.createElement("div");
  row.className = "row";
  var next = document.createElement("button");
  next.type = "button";
  next.className = "primary small";
  var skip = document.createElement("button");
  skip.type = "button";
  skip.className = "small";
  skip.textContent = holder.getAttribute("data-skip");
  row.appendChild(next);
  row.appendChild(skip);
  box.appendChild(text);
  box.appendChild(count);
  box.appendChild(row);
  document.body.appendChild(box);
  function finish() {
    if (lit) { lit.classList.remove("tourlit"); }
    box.remove();
    var body = new FormData();
    body.append("_csrf", holder.getAttribute("data-csrf"));
    if (window.fetch) { fetch(holder.getAttribute("data-url"), { method: "POST", body: body, credentials: "same-origin" }); }
  }
  function show() {
    if (lit) { lit.classList.remove("tourlit"); }
    var s = steps[i];
    lit = s.el;
    lit.classList.add("tourlit");
    lit.scrollIntoView({ block: "center", behavior: "smooth" });
    text.textContent = s.text;
    count.textContent = (i + 1) + " " + holder.getAttribute("data-of") + " " + steps.length;
    next.textContent = i === steps.length - 1 ? holder.getAttribute("data-done") : holder.getAttribute("data-next");
    var r = lit.getBoundingClientRect();
    var top = window.scrollY + r.bottom + 10;
    if (r.bottom + 180 > window.innerHeight) { top = Math.max(window.scrollY + r.top - 170, window.scrollY + 10); }
    box.style.top = top + "px";
    box.style.left = Math.max(10, Math.min(window.scrollX + r.left, window.scrollX + document.documentElement.clientWidth - 340)) + "px";
    box.focus();
  }
  next.addEventListener("click", function () { if (i < steps.length - 1) { i += 1; show(); } else { finish(); } });
  skip.addEventListener("click", finish);
  box.addEventListener("keydown", function (e) { if (e.key === "Escape") { finish(); } });
  setTimeout(show, 400);
})();

(function () {
  // 0.74: the same brightness, contrast and invert for every picture on the compare page.
  var box = document.querySelector("[data-compare]");
  if (!box) { return; }
  function apply() {
    var b = box.querySelector("[data-cmp=brightness]").value, c = box.querySelector("[data-cmp=contrast]").value;
    var inv = box.querySelector("[data-cmp=invert]").checked ? " invert(1)" : "";
    box.querySelectorAll(".compare img").forEach(function (img) { img.style.filter = "brightness(" + b + "%) contrast(" + c + "%)" + inv; });
  }
  box.addEventListener("input", apply);
  box.addEventListener("change", apply);
})();
