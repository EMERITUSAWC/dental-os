/* 0.61: dictation into clinical note boxes. Any textarea marked data-dictate gets a Dictate button.
   Speech is turned into words ON THIS DEVICE ONLY (SpeechRecognition.processLocally = true). Notes can hold names and health details,
   so unlike gum charting there is no online fallback, whatever the clinic allows there. Where the browser cannot listen on the
   device, the button is not shown and the keyboard's own microphone key still works.
   tidy() is plain logic with no browser in it (tested with node). */
(function (root) {
  "use strict";

  var SPOKEN = [
    [/\s*\b(full stop|period)\b/gi, "."], [/\s*\bcomma\b/gi, ","], [/\s*\bquestion mark\b/gi, "?"], [/\s*\bcolon\b/gi, ":"],
    [/\s*\b(new line|next line)\b\s*/gi, "\n"], [/\s*\bnew paragraph\b\s*/gi, "\n\n"], [/\bopen bracket\s*/gi, "("], [/\s*\bclose bracket\b/gi, ")"]
  ];

  /* Spoken punctuation to marks, spaces tidied, a capital after a full stop or a new line. */
  function tidy(text, keepFirst) {
    var s = String(text || "");
    SPOKEN.forEach(function (p) { s = s.replace(p[0], p[1]); });
    s = s.replace(/[ \t]+/g, " ").replace(/ *([.,?:)]) */g, "$1 ").replace(/\( +/g, "(").replace(/ *\n */g, "\n").replace(/[ \t]+$/gm, "").trim();
    s = s.replace(/([.?]\s+|\n)([a-z])/g, function (_m, a, b) { return a + b.toUpperCase(); });
    return keepFirst ? s : s.charAt(0).toUpperCase() + s.slice(1);
  }

  /* Joins what was said onto what is already in the box, with one space or a capital where needed. */
  function join(existing, said) {
    var a = String(existing || ""), b = tidy(said, true);
    if (!b) { return a; }
    if (!a || /\n$/.test(a)) { return a + b.charAt(0).toUpperCase() + b.slice(1); }
    var sep = /\s$/.test(a) ? "" : " ";
    if (/[.?]\s*$/.test(a)) { b = b.charAt(0).toUpperCase() + b.slice(1); }
    return a + sep + b;
  }

  function attach(doc) {
    var Rec = root.SpeechRecognition || root.webkitSpeechRecognition;
    var boxes = doc.querySelectorAll("textarea[data-dictate]");
    if (!boxes.length) { return; }
    var hint = doc.getElementById("dictate-hint");
    var conf = doc.getElementById("dictate-conf") || doc.body;          // the page's own words, so French screens get French
    if (!Rec || !("processLocally" in Rec.prototype)) {
      if (hint) { hint.hidden = false; }
      return;                                                          // no on-device listening here: offer nothing that would send sound away
    }
    var lang = (doc.documentElement.getAttribute("lang") || "en-GB");
    var label = conf.getAttribute("data-t-dictate") || "Dictate", stopLabel = conf.getAttribute("data-t-stop") || "Stop";
    Array.prototype.forEach.call(boxes, function (box) {
      var btn = doc.createElement("button");
      btn.type = "button"; btn.className = "small dictate"; btn.textContent = label;
      btn.setAttribute("aria-label", label + ": " + ((doc.querySelector('label[for="' + box.id + '"]') || {}).textContent || ""));
      var status = doc.createElement("span");
      status.className = "muted small"; status.setAttribute("aria-live", "polite");
      box.insertAdjacentElement("afterend", status);
      box.insertAdjacentElement("afterend", btn);
      var rec = null;
      btn.addEventListener("click", function () {
        if (rec) { rec.stop(); return; }
        rec = new Rec(); rec.processLocally = true; rec.continuous = true; rec.interimResults = false; rec.lang = lang;
        rec.onresult = function (e) {
          for (var k = e.resultIndex; k < e.results.length; k++) { if (e.results[k].isFinal) { box.value = join(box.value, e.results[k][0].transcript); } }
        };
        rec.onerror = function (e) {
          status.textContent = e.error === "language-not-supported"
            ? (conf.getAttribute("data-t-nolang") || "Install the speech language for this device in the browser's settings, or use the keyboard's microphone key.")
            : (conf.getAttribute("data-t-stopped") || "The microphone stopped.");
        };
        rec.onend = function () { rec = null; btn.textContent = label; status.textContent = ""; };
        btn.textContent = stopLabel; status.textContent = conf.getAttribute("data-t-listening") || "Listening on this device.";
        rec.start();
      });
    });
  }

  var api = { tidy: tidy, join: join, attach: attach };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; }
  else if (root.document) {
    root.DentalDictate = api;
    if (root.document.readyState === "loading") { root.document.addEventListener("DOMContentLoaded", function () { attach(root.document); }); }
    else { attach(root.document); }
  }
})(typeof window !== "undefined" ? window : this);
