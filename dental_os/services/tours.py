"""Version 0.74: a short guided tour the first time someone opens a main screen.

Each tour is a few steps; each step points at part of the page (a CSS selector) with one or two sentences. A step whose part is
not on the page (a role that does not see it) is skipped. Finishing or skipping a tour marks it done for that person; Help has a
button to see the tours again. The steps are plain wording, translated like the rest of the screen. No web framework here."""

TOURS = {
    "today": ("views.dashboard", [
        ("#mainnav", "The menu. Inbox, Clinic and Money open a short list. You see what your role needs."),
        (".stats", "Today's visits at a glance: booked, confirmed and done so far."),
        ("a[href$='/huddle']", "The morning huddle: how yesterday went and what to do for each patient today."),
        (".foot", "Help and Report a problem are at the foot of every page."),
    ]),
    "diary": ("views.diary", [
        (".diary", "The diary: one column for each dentist and chair. Click an empty time to book."),
        ("#mainnav", "Patients who want an earlier time are on the waiting list, under Diary."),
    ]),
    "patient": ("views.patient_view", [
        ("h1", "The patient. Medical alerts show in red on every screen for this patient."),
        ("nav.tabs", "Every part of the record is a tab: chart, notes, X-rays, treatment, account and more."),
    ]),
    "chart": ("record.chart_view", [
        (".arch-wrap", "Each tooth from the side and from above. Upper roots point up, lower roots down. Tap a tooth to see or add what is there."),
        (".legend", "Red is already in the mouth, yellow is planned, green is done. The other marks are explained here."),
    ]),
}
BY_ENDPOINT = {endpoint: key for key, (endpoint, _steps) in TOURS.items()}
STEP_TEXT = {text: text for _e, steps in TOURS.values() for _sel, text in steps}       # for the translation checks


def pending(db, user, endpoint):
    """The tour to show on this screen for this person, or None."""
    key = BY_ENDPOINT.get(endpoint or "")
    if not key or not user:
        return None
    if db.execute("SELECT 1 FROM user_tours WHERE user_id = ? AND tour = ?", (user["id"], key)).fetchone():
        return None
    return key


def done(db, user, key):
    if key not in TOURS:
        raise LookupError("tour")
    if not db.execute("SELECT 1 FROM user_tours WHERE user_id = ? AND tour = ?", (user["id"], key)).fetchone():
        db.execute("INSERT INTO user_tours(user_id, tour) VALUES (?, ?)", (user["id"], key))


def reset(db, user):
    db.execute("DELETE FROM user_tours WHERE user_id = ?", (user["id"],))
