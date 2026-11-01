/* Image viewer for X-rays and photos: zoom, move, turn, brightness, contrast, invert and measure.
   Nothing is saved: the stored picture never changes. The millimetre scale is kept for this page only. */
(function () {
  "use strict";
  var box = document.getElementById("viewer");
  if (!box) { return; }
  var canvas = document.getElementById("vcanvas"), ctx = canvas.getContext("2d"), status = document.getElementById("vstatus");
  var img = new Image();
  var st = { scale: 1, x: 0, y: 0, rot: 0, inv: false, bright: 100, contrast: 100 };
  var measuring = false, points = [], mmPerPx = parseFloat(box.getAttribute("data-mmpx")) || null, lastLine = null;
  var drag = null;
  function T(key, fallback) { return box.getAttribute("data-t-" + key) || fallback; }

  function fit() {
    var w = (st.rot % 180 === 0) ? img.width : img.height, h = (st.rot % 180 === 0) ? img.height : img.width;
    st.scale = Math.min(canvas.width / w, canvas.height / h) * 0.95;
    st.x = canvas.width / 2; st.y = canvas.height / 2;
  }
  function draw() {
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.filter = "brightness(" + st.bright + "%) contrast(" + st.contrast + "%)" + (st.inv ? " invert(100%)" : "");
    ctx.translate(st.x, st.y); ctx.rotate(st.rot * Math.PI / 180); ctx.scale(st.scale, st.scale);
    ctx.drawImage(img, -img.width / 2, -img.height / 2);
    ctx.restore();
    ctx.filter = "none";
    if (points.length) {
      ctx.strokeStyle = "#ffcc00"; ctx.fillStyle = "#ffcc00"; ctx.lineWidth = 2;
      var a = toScreen(points[0]);
      ctx.beginPath(); ctx.arc(a.x, a.y, 4, 0, 6.3); ctx.fill();
      if (points.length === 2) {
        var b = toScreen(points[1]);
        ctx.beginPath(); ctx.arc(b.x, b.y, 4, 0, 6.3); ctx.fill();
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      }
    }
  }
  /* picture pixels <-> screen pixels */
  function toImage(sx, sy) {
    var dx = (sx - st.x) / st.scale, dy = (sy - st.y) / st.scale, r = -st.rot * Math.PI / 180;
    return { x: dx * Math.cos(r) - dy * Math.sin(r) + img.width / 2, y: dx * Math.sin(r) + dy * Math.cos(r) + img.height / 2 };
  }
  function toScreen(p) {
    var dx = p.x - img.width / 2, dy = p.y - img.height / 2, r = st.rot * Math.PI / 180;
    return { x: (dx * Math.cos(r) - dy * Math.sin(r)) * st.scale + st.x, y: (dx * Math.sin(r) + dy * Math.cos(r)) * st.scale + st.y };
  }
  function pos(ev) {
    var r = canvas.getBoundingClientRect();
    return { x: (ev.clientX - r.left) * canvas.width / r.width, y: (ev.clientY - r.top) * canvas.height / r.height };
  }
  function say(t) { status.textContent = t; }

  var scaleForm = document.createElement("div");
  scaleForm.className = "row";
  scaleForm.hidden = true;
  var lab = document.createElement("label"), inp = document.createElement("input"), btn = document.createElement("button");
  lab.className = "check"; lab.style.margin = "0"; lab.textContent = T("reallen", "Real length in mm") + " ";
  inp.type = "number"; inp.step = "0.1"; inp.min = "0.1"; inp.style.width = "90px"; lab.appendChild(inp);
  btn.type = "button"; btn.textContent = T("setscale", "Set the scale");
  scaleForm.appendChild(lab); scaleForm.appendChild(document.createTextNode(" ")); scaleForm.appendChild(btn);
  box.appendChild(scaleForm);
  scaleForm.querySelector("button").addEventListener("click", function () {
    var v = parseFloat(scaleForm.querySelector("input").value);
    if (lastLine && v > 0) { mmPerPx = v / lastLine; say(T("scaleset", "Scale set. Measurements are now in millimetres.")); scaleForm.hidden = true; }
  });

  canvas.addEventListener("mousedown", function (ev) {
    var p = pos(ev);
    if (measuring) {
      if (points.length === 2) { points = []; }
      points.push(toImage(p.x, p.y));
      if (points.length === 2) {
        var dx = points[1].x - points[0].x, dy = points[1].y - points[0].y, px = Math.sqrt(dx * dx + dy * dy);
        lastLine = px;
        if (mmPerPx) { say(T("length", "Length:") + " " + (px * mmPerPx).toFixed(1) + " mm"); }
        else { say(T("length", "Length:") + " " + px.toFixed(0) + " px. " + T("givescale", "Give the real length to set a scale.")); scaleForm.hidden = false; }
      }
      draw();
      return;
    }
    drag = { x: p.x, y: p.y, ox: st.x, oy: st.y };
  });
  window.addEventListener("mousemove", function (ev) {
    if (!drag) { return; }
    var p = pos(ev); st.x = drag.ox + p.x - drag.x; st.y = drag.oy + p.y - drag.y; draw();
  });
  window.addEventListener("mouseup", function () { drag = null; });
  canvas.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    zoomAt(pos(ev), ev.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });
  /* touch: one finger moves, two fingers zoom */
  var touch = null;
  canvas.addEventListener("touchstart", function (ev) {
    if (ev.touches.length === 1) {
      var t = ev.touches[0];
      if (measuring) { canvas.dispatchEvent(new MouseEvent("mousedown", { clientX: t.clientX, clientY: t.clientY })); return; }
      touch = { one: pos(t), ox: st.x, oy: st.y };
    } else if (ev.touches.length === 2) {
      touch = { dist: Math.hypot(ev.touches[0].clientX - ev.touches[1].clientX, ev.touches[0].clientY - ev.touches[1].clientY), scale: st.scale };
    }
  }, { passive: true });
  canvas.addEventListener("touchmove", function (ev) {
    if (!touch) { return; }
    ev.preventDefault();
    if (ev.touches.length === 1 && touch.one) {
      var p = pos(ev.touches[0]); st.x = touch.ox + p.x - touch.one.x; st.y = touch.oy + p.y - touch.one.y;
    } else if (ev.touches.length === 2 && touch.dist) {
      var d = Math.hypot(ev.touches[0].clientX - ev.touches[1].clientX, ev.touches[0].clientY - ev.touches[1].clientY);
      st.scale = Math.max(0.05, Math.min(40, touch.scale * d / touch.dist));
    }
    draw();
  }, { passive: false });
  canvas.addEventListener("touchend", function () { touch = null; });

  function zoomAt(p, f) {
    var before = toImage(p.x, p.y);
    st.scale = Math.max(0.05, Math.min(40, st.scale * f));
    var after = toScreen(before);
    st.x += p.x - after.x; st.y += p.y - after.y;
    draw();
  }
  box.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-act]");
    if (!b) { return; }
    var act = b.getAttribute("data-act"), centre = { x: canvas.width / 2, y: canvas.height / 2 };
    if (act === "zoomin") { zoomAt(centre, 1.25); }
    if (act === "zoomout") { zoomAt(centre, 0.8); }
    if (act === "fit") { fit(); draw(); }
    if (act === "rotate") { st.rot = (st.rot + 90) % 360; fit(); draw(); }
    if (act === "invert") { st.inv = !st.inv; draw(); }
    if (act === "measure") { measuring = !measuring; points = []; b.classList.toggle("primary", measuring); say(measuring ? T("taptwo", "Tap two points.") : ""); draw(); }
    if (act === "reset") {
      st.rot = 0; st.inv = false; st.bright = 100; st.contrast = 100; measuring = false; points = [];
      Array.prototype.forEach.call(box.querySelectorAll("input[type=range]"), function (r) { r.value = 100; });
      fit(); draw();
    }
  });
  box.addEventListener("input", function (ev) {
    var c = ev.target.getAttribute("data-ctl");
    if (c === "brightness") { st.bright = ev.target.value; draw(); }
    if (c === "contrast") { st.contrast = ev.target.value; draw(); }
  });
  function size() {
    var w = Math.min(box.clientWidth || 1000, 1400);
    canvas.width = w; canvas.height = Math.round(Math.max(360, Math.min(window.innerHeight * 0.7, w * 0.66)));
  }
  img.onload = function () { size(); fit(); draw(); };
  img.onerror = function () { say(T("error", "The picture could not be opened.")); };
  window.addEventListener("resize", function () { if (img.width) { size(); fit(); draw(); } });
  img.src = box.getAttribute("data-src");
}());
