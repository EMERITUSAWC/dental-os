/* Hands-free gum charting. Speak (or type) tooth numbers and measurements, and the chart fills in.
   Two parts: parse() is plain logic with no browser in it (tested with node), and the bottom half connects it to the page.
   Only numbers and a few words are understood. Nothing is saved until the form is saved. */
(function (root) {
  "use strict";

  var WORDS = {
    zero: 0, oh: 0, o: 0, one: 1, won: 1, two: 2, to: 2, too: 2, three: 3, free: 3, four: 4, for: 4, fore: 4, five: 5, six: 6, sicks: 6,
    seven: 7, eight: 8, ate: 8, nine: 9, ten: 10, eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15,
    sixteen: 16, seventeen: 17, eighteen: 18, twenty: 20, "twenty-one": 21
  };
  var ROW_WORDS = { buccal: 0, facial: 0, buckle: 0, lingual: 1, palatal: 1, palatine: 1, palette: 1 };
  var MODE_WORDS = {
    pocket: "pk", pockets: "pk", depth: "pk", depths: "pk", probing: "pk",
    recession: "rc", recessions: "rc", recede: "rc",
    bleeding: "bl", bleed: "bl", bleeds: "bl", blood: "bl"
  };

  function toNumber(tok) {
    if (/^-?\d+$/.test(tok)) { return parseInt(tok, 10); }
    if (Object.prototype.hasOwnProperty.call(WORDS, tok)) { return WORDS[tok]; }
    return null;
  }

  function tokenize(text) {
    return String(text || "").toLowerCase().replace(/[,.;:!?]/g, " ").replace(/\b(\d+)\s*-\s*(\d+)\b/g, "$1 $2")
      .replace(/(\d)(?=[a-z])|([a-z])(?=\d)/g, "$1$2 ").split(/\s+/).filter(Boolean);
  }

  function copy(state) { return { tooth: state.tooth, row: state.row, pos: state.pos, mode: state.mode }; }

  /* state: {tooth, row (0 buccal, 1 lingual), pos (0-2), mode ("pk", "rc", "bl")}
     chart: {teeth: [numbers in chart order], sites: {tooth: [six site codes in chart order]}, skip: [teeth not to be charted]}
     Returns {ops, state, heard, ignored}. An op is {name, value} to set a field ("" empties it, true or false for a tick box). */
  function parse(text, stateIn, chart) {
    var st = copy(stateIn), ops = [], heard = [], ignored = [];
    var teeth = chart.teeth.filter(function (t) { return (chart.skip || []).indexOf(t) < 0; });
    var tokens = tokenize(text), i = 0;

    function goto(toothNo) { st.tooth = toothNo; st.row = 0; st.pos = 0; }
    function step(delta) {
      var at = teeth.indexOf(st.tooth), to = at + delta;
      if (at < 0) { to = delta > 0 ? 0 : teeth.length - 1; }
      if (to < 0 || to >= teeth.length) { ignored.push(delta > 0 ? "end of chart" : "start of chart"); return false; }
      goto(teeth[to]);
      return true;
    }
    function prefix() { return st.mode; }
    function siteName(toothNo, row, pos) { return chart.sites[toothNo][row * 3 + pos]; }
    function setValue(v) {
      var lo = st.mode === "rc" ? -5 : 0, hi = 15;
      if (v < lo || v > hi) { ignored.push(String(v)); return; }
      ops.push({ name: prefix() + "_" + st.tooth + "_" + siteName(st.tooth, st.row, st.pos), value: String(v) });
      advance();
    }
    function advance() {
      st.pos += 1;
      if (st.pos > 2) {
        st.pos = 0; st.row += 1;
        if (st.row > 1) { st.row = 0; if (!step(1)) { st.row = 1; st.pos = 2; } }
      }
    }
    function readTooth(from) {
      var a = tokens[from], b = tokens[from + 1], n = toNumber(a), m = b === undefined ? null : toNumber(b);
      if (n === null) { return null; }
      if (chart.uni) {          /* Universal numbering: 1 to 32 */
        if (n >= 1 && n <= 32 && chart.sites[chart.uni[n]]) { return { tooth: chart.uni[n], used: 1 }; }
        return { tooth: n, used: 1, bad: true };
      }
      if (n >= 11 && chart.sites[n]) { return { tooth: n, used: 1 }; }
      if (n >= 1 && n <= 4 && m !== null && m >= 1 && m <= 8 && chart.sites[n * 10 + m]) { return { tooth: n * 10 + m, used: 2 }; }
      return { tooth: n, used: 1, bad: true };
    }

    while (i < tokens.length) {
      var tok = tokens[i];
      if (tok === "tooth" || tok === "teeth") {
        var t = readTooth(i + 1);
        if (!t) { heard.push(tok); i += 1; continue; }
        if (t.bad || !chart.sites[t.tooth] || teeth.indexOf(t.tooth) < 0) {
          ignored.push("tooth " + t.tooth + (chart.sites[t.tooth] ? " (marked missing)" : ""));
        } else { goto(t.tooth); }
        i += 1 + t.used; continue;
      }
      if (tok === "next") { step(1); i += (tokens[i + 1] === "tooth" ? 2 : 1); continue; }
      if (tok === "previous" || tok === "back" || tok === "last") { step(-1); i += (tokens[i + 1] === "tooth" ? 2 : 1); continue; }
      if (Object.prototype.hasOwnProperty.call(ROW_WORDS, tok)) { st.row = ROW_WORDS[tok]; st.pos = 0; i += 1; continue; }
      if (Object.prototype.hasOwnProperty.call(MODE_WORDS, tok)) { st.mode = MODE_WORDS[tok]; st.row = 0; st.pos = 0; i += 1; continue; }
      if (tok === "skip" || tok === "pass") { if (st.mode !== "bl") { advance(); } i += 1; continue; }
      if (tok === "undo" || tok === "scratch") { ops.push({ undo: true }); i += 1; continue; }
      if (tok === "all" && st.mode === "bl") {
        for (var k = 0; k < 6; k++) { ops.push({ name: "bl_" + st.tooth + "_" + chart.sites[st.tooth][k], value: true }); }
        i += 1; continue;
      }
      var neg = false;
      if (tok === "minus" || tok === "negative") { neg = true; i += 1; tok = tokens[i]; if (tok === undefined) { break; } }
      var n = toNumber(tok);
      if (n === null) { heard.push(tok); i += 1; continue; }
      if (st.tooth === null || st.tooth === undefined) { ignored.push(tok + " (say the tooth first)"); i += 1; continue; }
      if (st.mode === "bl") {
        if (n >= 1 && n <= 6) { ops.push({ name: "bl_" + st.tooth + "_" + chart.sites[st.tooth][n - 1], value: true }); }
        else if (n > 6 && /^\d+$/.test(tok)) {
          tok.split("").forEach(function (d) { var x = parseInt(d, 10); if (x >= 1 && x <= 6) { ops.push({ name: "bl_" + st.tooth + "_" + chart.sites[st.tooth][x - 1], value: true }); } });
        } else { ignored.push(tok); }
        i += 1; continue;
      }
      if (neg) { n = -n; }
      if (!neg && /^\d+$/.test(tok) && n > 15) {
        tok.split("").forEach(function (d) { setValue(parseInt(d, 10)); });      // "323" heard as one number: read it digit by digit
      } else { setValue(n); }
      i += 1;
    }
    return { ops: ops, state: st, heard: heard, ignored: ignored };
  }

  /* ------------------------------------------------------------------------------------------ the page */
  function attach(doc) {
    var panel = doc.getElementById("voice-panel");
    if (!panel) { return; }
    var form = panel.closest("form") || doc.querySelector("form.perio-form") || doc.forms[0];
    var inputs = Array.prototype.slice.call(doc.querySelectorAll("input[name^='pk_']"));
    var chart = { teeth: [], sites: {}, skip: [], uni: null };
    if (panel.getAttribute("data-notation") === "universal") {
      chart.uni = {};
      [18, 17, 16, 15, 14, 13, 12, 11, 21, 22, 23, 24, 25, 26, 27, 28, 38, 37, 36, 35, 34, 33, 32, 31, 41, 42, 43, 44, 45, 46, 47, 48]
        .forEach(function (f, i) { chart.uni[i + 1] = f; });
    }
    inputs.forEach(function (el) {
      var m = /^pk_(\d+)_([A-Z]+)$/.exec(el.name);
      if (!m) { return; }
      var t = parseInt(m[1], 10);
      if (!chart.sites[t]) { chart.sites[t] = []; chart.teeth.push(t); }
      chart.sites[t].push(m[2]);
      if (el.disabled && chart.skip.indexOf(t) < 0) { chart.skip.push(t); }
    });
    var state = { tooth: null, row: 0, pos: 0, mode: "pk" };
    var undo = [];
    var log = doc.getElementById("voice-log"), status = doc.getElementById("voice-status"), text = doc.getElementById("voice-text");
    var now = doc.getElementById("voice-now");

    function field(name) { return form.querySelector("[name='" + name + "']"); }
    function show() {
      var modes = { pk: "pocket depth", rc: "recession", bl: "bleeding" };
      now.textContent = state.tooth === null ? "Say a tooth number to begin, for example: tooth sixteen"
        : "Tooth " + state.tooth + ", " + (state.row ? "lingual" : "buccal") + ", point " + (state.pos + 1) + " of 3, " + modes[state.mode];
      Array.prototype.forEach.call(doc.querySelectorAll(".voice-now"), function (el) { el.classList.remove("voice-now"); });
      if (state.tooth !== null) {
        Array.prototype.forEach.call(doc.querySelectorAll("[name^='pk_" + state.tooth + "_']"), function (el) { el.classList.add("voice-now"); });
        var first = doc.querySelector("[name^='pk_" + state.tooth + "_']");
        if (first && first.scrollIntoView) { first.scrollIntoView({ block: "nearest", inline: "center" }); }
      }
    }
    function say(line) {
      var li = doc.createElement("li");
      li.textContent = line;
      log.insertBefore(li, log.firstChild);
      while (log.children.length > 8) { log.removeChild(log.lastChild); }
    }
    function apply(heardText) {
      var res = parse(heardText, state, chart);
      res.ops.forEach(function (op) {
        if (op.undo) {
          var last = undo.pop();
          if (last) { var f = field(last.name); if (f) { if (f.type === "checkbox") { f.checked = last.old; } else { f.value = last.old; } } }
          return;
        }
        var el = field(op.name);
        if (!el || el.disabled) { return; }
        if (el.type === "checkbox") { undo.push({ name: op.name, old: el.checked }); el.checked = !!op.value; }
        else { undo.push({ name: op.name, old: el.value }); el.value = op.value; }
      });
      state = res.state;
      var said = "Heard: " + heardText;
      if (res.ignored.length) { said += "   (ignored: " + res.ignored.join(", ") + ")"; }
      if (res.heard.length) { said += "   (not understood: " + res.heard.join(", ") + ")"; }
      say(said);
      show();
    }

    var typed = doc.getElementById("voice-apply");
    typed.addEventListener("click", function () { if (text.value.trim()) { apply(text.value); text.value = ""; } });
    text.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); typed.click(); } });

    var Rec = root.SpeechRecognition || root.webkitSpeechRecognition;
    var speak = doc.getElementById("voice-speak"), stop = doc.getElementById("voice-stop");
    var rec = null, wanted = false;
    var cloudOk = panel.getAttribute("data-cloud") === "1";
    var onDevice = !!Rec && ("processLocally" in Rec.prototype);
    if (!Rec) {
      speak.disabled = true; stop.disabled = true;
      status.textContent = "This browser cannot listen. You can still type what you would say, in the box below.";
    } else if (!onDevice && !cloudOk) {
      speak.disabled = true; stop.disabled = true;
      status.textContent = "This browser cannot turn speech into words on this computer, and your clinic has not allowed an online service, so speaking is off. You can still type what you would say, in the box below.";
    } else {
      speak.addEventListener("click", function () {
        if (rec) { return; }
        rec = new Rec(); rec.continuous = true; rec.interimResults = false; rec.lang = panel.getAttribute("data-lang") || "en-GB";
        if (onDevice) { rec.processLocally = !cloudOk; }                       // on the computer itself unless the clinic allowed the online service
        rec.onresult = function (e) {
          for (var k = e.resultIndex; k < e.results.length; k++) { if (e.results[k].isFinal) { apply(e.results[k][0].transcript); } }
        };
        rec.onerror = function (e) {
          status.textContent = e.error === "language-not-supported"
            ? "This browser has no listening language installed for this computer yet. Install the English speech pack in the browser's settings, or type instead."
            : "The microphone stopped: " + (e.error || "unknown reason") + ".";
          wanted = false;
        };
        rec.onend = function () { rec = null; if (wanted) { speak.click(); } else { status.textContent = "Not listening."; } };
        wanted = true; status.textContent = "Listening. Say a tooth, then the numbers."; rec.start();
      });
      stop.addEventListener("click", function () { wanted = false; if (rec) { rec.stop(); } });
    }
    show();
  }

  var api = { parse: parse, tokenize: tokenize, attach: attach };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; }
  else {
    root.DentalVoice = api;
    if (root.document) {
      if (root.document.readyState === "loading") { root.document.addEventListener("DOMContentLoaded", function () { attach(root.document); }); }
      else { attach(root.document); }
    }
  }
})(typeof window !== "undefined" ? window : this);
