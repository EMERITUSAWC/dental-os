// Run with node. Prints one JSON line per case: {name, ok, got, want}. Used by tests/test_v61.py.
const D = require("../../dental_os/static/dictate.js");
const out = [];
function check(name, got, want) { out.push({ name, ok: got === want, got, want }); }
check("spoken full stop and capital", D.tidy("patient attended full stop no pain"), "Patient attended. No pain");
check("comma and question mark", D.tidy("pain on biting comma worse at night question mark"), "Pain on biting, worse at night?");
check("new line and paragraph", D.tidy("first point new line second point new paragraph plan"), "First point\nSecond point\n\nPlan");
check("brackets", D.tidy("composite open bracket shade A2 close bracket placed"), "Composite (shade A2) placed");
check("spaces tidied", D.tidy("  lots   of    space  "), "Lots of space");
check("tooth numbers kept", D.tidy("36 mod caries"), "36 mod caries");
check("join adds a space", D.join("Pain on biting", "lower left"), "Pain on biting lower left");
check("join after a full stop capitalises", D.join("No pain.", "review in six months"), "No pain. Review in six months");
check("join into an empty box", D.join("", "local anaesthetic given"), "Local anaesthetic given");
check("join after a new line", D.join("Plan\n", "fill 36"), "Plan\nFill 36");
check("nothing said", D.join("Kept", ""), "Kept");
out.forEach(o => console.log(JSON.stringify(o)));
