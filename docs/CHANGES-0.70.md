# Changes in 0.70

Two visual fixes found while taking screenshots of 0.69: the top menu ran onto two lines for dentists and managers, and the
tooth chart was plain squares.

## Top menu (`templates/base.html`, `static/app.css`, `static/app.js`)

- Links are grouped under Inbox, Clinic, Money and (managers) Admin, using `<details>`/`<summary>`, so they open without
  scripts and with the keyboard. Every link keeps its permission check and its "you are here" marking; a group heading is
  marked when one of its pages is open, and is left out when nothing in it is allowed.
- The Inbox heading carries one number: tasks due plus unread team messages plus messages needing attention.
- `app.js`: clicking elsewhere or pressing Escape closes an open group; opening one closes the others.
- Phones: groups open inside the existing Menu button list.

## Tooth chart (`services/chart.py`, `record.py`, `templates/_tooth.html`, `templates/chart.html`, `static/app.css`)

- `chart.tooth_kind(n)` and `chart.is_upper(n)` work out the kind of tooth from its FDI number; `chart.SHAPES` holds the
  outlines for incisor, canine, premolar, upper and lower molar and baby molar; `chart.whole_marks(entry)` lists whole-tooth
  records (crown, implant, root canal, bridge, fracture, not yet through) so they can be drawn.
- Each tooth is drawn twice in one picture: from the side and from above. The five surfaces are cut to the tooth's outline.
  Lower teeth are drawn upside down, and buccal is on the outside of each arch (it was always at the top before).
- A gap marks the midline. The legend explains the new marks.
- Tooth colours have light and dark-mode values (`--tooth`, `--tooth-root`).

## Other

- French for the new words (`services/i18n_fr_staff34.py`).
- `tests/test_v70.py`: 12 tests covering the menu length, permissions, active group, the Inbox number, accessibility, French,
  tooth kinds, root counts, lower teeth turned over, and each whole-tooth mark.
