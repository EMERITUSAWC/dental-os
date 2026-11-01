// Run with node. Prints one JSON line per case: {name, ok, got}. Used by tests/test_v14.py.
const V = require("../../dental_os/static/voice.js");
function site(t) { const right = [1, 4].includes(Math.floor(t / 10)); return right ? ["DB", "B", "MB", "DL", "L", "ML"] : ["MB", "B", "DB", "ML", "L", "DL"]; }
const teeth = [18, 17, 16, 15, 14, 11, 21, 22, 26];
const sites = {}; teeth.forEach(t => sites[t] = site(t));
const chart = { teeth, sites, skip: [17] };
const fresh = () => ({ tooth: null, row: 0, pos: 0, mode: "pk" });
const pairs = r => r.ops.map(o => o.undo ? "undo" : o.name + "=" + o.value);
const out = [];
function check(name, text, want, state, extra) {
  const r = V.parse(text, state || fresh(), chart);
  const got = pairs(r);
  out.push({ name, ok: JSON.stringify(got) === JSON.stringify(want), got, want, state: r.state, ignored: r.ignored, heard: r.heard, extra });
  return r;
}
check("six pockets by words", "tooth sixteen three two three four three three",
  ["pk_16_DB=3", "pk_16_B=2", "pk_16_MB=3", "pk_16_DL=4", "pk_16_L=3", "pk_16_ML=3"]);
check("tooth by two digits spoken separately", "tooth one six 2 2 2", ["pk_16_DB=2", "pk_16_B=2", "pk_16_MB=2"]);
check("left side teeth run mesial first", "tooth 21 3 2 4", ["pk_21_MB=3", "pk_21_B=2", "pk_21_DB=4"]);
check("heard as one number", "tooth 16 323 433", ["pk_16_DB=3", "pk_16_B=2", "pk_16_MB=3", "pk_16_DL=4", "pk_16_L=3", "pk_16_ML=3"]);
check("mishearings: to, for, free", "tooth 16 to for free", ["pk_16_DB=2", "pk_16_B=4", "pk_16_MB=3"]);
let r = check("moves on to the next tooth by itself, skipping a missing one", "tooth 18 1 1 1 1 1 1 2", ["pk_18_DB=1", "pk_18_B=1", "pk_18_MB=1", "pk_18_DL=1", "pk_18_L=1", "pk_18_ML=1", "pk_16_DB=2"]);
check("a missing tooth is refused", "tooth 17 3", [], null);
check("recession can be negative", "tooth 16 recession minus 1 0 2", ["rc_16_DB=-1", "rc_16_B=0", "rc_16_MB=2"]);
check("bleeding by point number", "tooth 16 bleeding 1 4", ["bl_16_DB=true", "bl_16_DL=true"]);
check("bleeding all", "tooth 11 bleeding all", ["bl_11_DB=true", "bl_11_B=true", "bl_11_MB=true", "bl_11_DL=true", "bl_11_L=true", "bl_11_ML=true"]);
check("skip leaves a point empty", "tooth 16 3 skip 2", ["pk_16_DB=3", "pk_16_MB=2"]);
check("buccal and lingual words choose the row", "tooth 16 lingual 5 5 5", ["pk_16_DL=5", "pk_16_L=5", "pk_16_ML=5"]);
check("next tooth and back", "tooth 16 next tooth 1 back 2", ["pk_15_DB=1", "pk_16_DB=2"]);
check("undo is passed on", "tooth 16 3 undo", ["pk_16_DB=3", "undo"]);
check("numbers before any tooth are not guessed", "3 2 3", []);
check("a number out of range is ignored", "tooth 16 recession minus 9", [], null);
check("the word sixteen is not a depth", "tooth 16 sixteen", [], null);
check("unknown words are reported, not used", "tooth 16 banana 3", ["pk_16_DB=3"]);
const uni = {}; [18, 17, 16, 15, 14, 13, 12, 11, 21, 22, 23, 24, 25, 26, 27, 28, 38, 37, 36, 35, 34, 33, 32, 31, 41, 42, 43, 44, 45, 46, 47, 48].forEach((f, i) => uni[i + 1] = f);
const ru = V.parse("tooth three 2 3 2", fresh(), Object.assign({}, chart, { uni }));
out.push({ name: "Universal numbering: tooth 3 is FDI 16", ok: JSON.stringify(pairs(ru)) === JSON.stringify(["pk_16_DB=2", "pk_16_B=3", "pk_16_MB=2"]), got: pairs(ru) });
const ru2 = V.parse("tooth 16 2", fresh(), Object.assign({}, chart, { uni }));
out.push({ name: "Universal numbering: 16 is the upper left wisdom tooth, not charted here", ok: pairs(ru2).length === 0, got: pairs(ru2) });
const r2 = V.parse("tooth 16 banana", fresh(), chart);
out.push({ name: "unknown words listed", ok: r2.heard.join() === "banana", got: r2.heard });
const r3 = V.parse("Tooth 16, 3, 2, 3.", fresh(), chart);
out.push({ name: "capitals and punctuation", ok: r3.ops.length === 3, got: pairs(r3) });
const r4 = V.parse("tooth 16 3 2 3 4 3 3", fresh(), chart);
out.push({ name: "state after a full tooth", ok: r4.state.tooth === 15 && r4.state.row === 0 && r4.state.pos === 0, got: r4.state });
out.forEach(o => console.log(JSON.stringify(o)));
