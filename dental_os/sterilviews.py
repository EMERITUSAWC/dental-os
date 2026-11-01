"""Sterilisation and instrument tracking screens (version 0.22)."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .safeurl import local_path
from .auth import clinical_required, manager_required
from .db import get_db
from .services import qr, steri as ST
from .services.timeutil import now_local

bp = Blueprint("steri", __name__)


def _today():
    return now_local(g.clinic["timezone"]).date()


def _fail(exc):
    get_db().rollback()
    flash(str(exc), "error")


@bp.get("/steri")
@clinical_required
def home():
    db = get_db()
    ov = ST.overview(db, g.clinic)
    return render_template("steri_home.html", ov=ov, tests=ST.TESTS, kinds=ST.KINDS, results=ST.RESULTS,
                           cycles=ST.cycles(db, g.clinic["id"], limit=15), cycle_results=ST.CYCLE_RESULTS)


@bp.route("/steri/setup", methods=["GET", "POST"])
@manager_required
def setup():
    db = get_db()
    if request.method == "POST":
        try:
            if request.form.get("what") == "settings":
                ST.save_settings(db, g.clinic, g.user["id"], request.form.get("require_daily") == "1", request.form.get("shelf_days"))
            else:
                ST.save_steriliser(db, g.clinic, g.user["id"], request.form.get("name"), request.form.get("kind"), request.form.get("serial"),
                                   request.form.get("validated_on"), request.form.get("sid", type=int), request.form.get("retired") == "1")
            flash("Saved.", "ok")
        except ST.SteriError as exc:
            _fail(exc)
        return redirect(url_for("steri.setup"))
    return render_template("steri_setup.html", rows=ST.sterilisers(db, g.clinic["id"], all_=True), kinds=ST.KINDS,
                           settings=ST.settings(db, g.clinic["id"]))


@bp.get("/steri/sterilisers/<int:sid>")
@clinical_required
def steriliser(sid):
    db = get_db()
    s = ST.get_steriliser(db, g.clinic["id"], sid)
    if not s:
        abort(404)
    log = db.execute("SELECT l.*, u.display_name AS by_name FROM steri_service_log l LEFT JOIN users u ON u.id = l.by_user "
                     "WHERE l.steriliser_id = ? ORDER BY l.id DESC", (sid,)).fetchall()
    return render_template("steri_steriliser.html", s=s, due=ST.checks_due(db, g.clinic, s), tests=ST.tests_for(db, g.clinic["id"], sid),
                           test_names=ST.TESTS, kinds=ST.KINDS, results=ST.RESULTS, cycles=ST.cycles(db, g.clinic["id"], sid, 30),
                           cycle_results=ST.CYCLE_RESULTS, need=ST.passes_needed(s), can_return=ST.can_return(s), log=log)


@bp.post("/steri/sterilisers/<int:sid>/return")
@manager_required
def back_in_service(sid):
    try:
        ST.return_to_service(get_db(), g.clinic, g.user["id"], sid, request.form.get("note"))
        flash("Back in service.", "ok")
    except ST.SteriError as exc:
        _fail(exc)
    return redirect(url_for("steri.steriliser", sid=sid))


@bp.post("/steri/tests")
@clinical_required
def test_add():
    sid = request.form.get("sid", type=int)
    try:
        ST.record_test(get_db(), g.clinic, g.user["id"], sid, request.form.get("kind"), request.form.get("result"),
                       request.form.get("lot"), request.form.get("notes"), request.form.get("cycle_id", type=int))
        flash("Test recorded.", "ok")
    except ST.SteriError as exc:
        _fail(exc)
    back = request.form.get("back") or ""
    return redirect(back if local_path(back) else url_for("steri.home"))


@bp.post("/steri/tests/<int:tid>/result")
@clinical_required
def test_result(tid):
    try:
        ST.read_result(get_db(), g.clinic, g.user["id"], tid, request.form.get("result"))
        flash("Result recorded.", "ok")
    except ST.SteriError as exc:
        _fail(exc)
    return redirect(url_for("steri.home"))


@bp.route("/steri/cycles/new", methods=["GET", "POST"])
@clinical_required
def cycle_new():
    db = get_db()
    rows = ST.sterilisers(db, g.clinic["id"])
    if request.method == "POST":
        f = request.form
        packs = [(f.get(f"what{i}"), f.get(f"count{i}") or "1") for i in range(8)]
        try:
            cid = ST.record_cycle(db, g.clinic, g.user["id"], f.get("sid", type=int), packs, f.get("program"), f.get("temp_c"),
                                  f.get("pressure_kpa"), f.get("hold_min"), f.get("mechanical_ok") == "1", f.get("indicator_ok") == "1",
                                  f.get("implants") == "1", f.get("notes"), f.get("machine_cycle_no"))
            c = ST.get_cycle(db, g.clinic["id"], cid)
            flash({"pass": "Cycle recorded. Print the labels and put the packs on the shelf.",
                   "held": "Cycle recorded. The packs are held until the spore test run with this load passes.",
                   "fail": "Cycle recorded as failed. Do not use these instruments: clean and sterilise them again."}[c["result"]],
                  "ok" if c["result"] != "fail" else "error")
            return redirect(url_for("steri.cycle_view", cycle_id=cid))
        except ST.SteriError as exc:
            _fail(exc)
    sets = [r[0] for r in db.execute("SELECT DISTINCT contents FROM steri_packs WHERE clinic_id = ? ORDER BY contents LIMIT 50",
                                      (g.clinic["id"],)).fetchall()]
    return render_template("steri_cycle_new.html", rows=rows, f=request.form, sets=sets, kinds=ST.KINDS)


@bp.get("/steri/cycles")
@clinical_required
def cycles():
    return render_template("steri_cycles.html", rows=ST.cycles(get_db(), g.clinic["id"], limit=300), cycle_results=ST.CYCLE_RESULTS)


@bp.get("/steri/cycles/<int:cycle_id>")
@clinical_required
def cycle_view(cycle_id):
    db = get_db()
    c = ST.get_cycle(db, g.clinic["id"], cycle_id)
    if not c:
        abort(404)
    spore = db.execute("SELECT * FROM steri_tests WHERE cycle_id = ? ORDER BY id", (cycle_id,)).fetchall()
    return render_template("steri_cycle.html", c=c, packs=ST.packs_of(db, g.clinic["id"], cycle_id), cycle_results=ST.CYCLE_RESULTS,
                           pack_status=ST.PACK_STATUS, spore=spore, results=ST.RESULTS, tests=ST.TESTS)


@bp.get("/steri/cycles/<int:cycle_id>/labels")
@clinical_required
def labels(cycle_id):
    db = get_db()
    c = ST.get_cycle(db, g.clinic["id"], cycle_id)
    if not c:
        abort(404)
    packs = [k for k in ST.packs_of(db, g.clinic["id"], cycle_id) if k["status"] in ("stored", "held")]
    return render_template("steri_labels.html", c=c, packs=[(k, qr.svg(k["code"], 96, k["code"])) for k in packs])


@bp.get("/steri/packs")
@clinical_required
def pack_find():
    db = get_db()
    code = (request.args.get("code") or "").strip()
    k = ST.find_pack(db, g.clinic["id"], code) if code else None
    if code and not k:
        flash("No pack has that code in this clinic.", "error")
    return render_template("steri_pack.html", k=k, code=code, pack_status=ST.PACK_STATUS, today=_today().isoformat())


@bp.post("/steri/packs/back")
@clinical_required
def pack_back():
    code = request.form.get("code")
    try:
        ST.send_back(get_db(), g.clinic, g.user["id"], code, request.form.get("reason"))
        flash("Pack marked as sent back for cleaning.", "ok")
    except ST.SteriError as exc:
        _fail(exc)
    return redirect(url_for("steri.pack_find", code=code))


@bp.get("/steri/shelf")
@clinical_required
def shelf():
    status = request.args.get("status") if request.args.get("status") in ST.PACK_STATUS else "stored"
    return render_template("steri_shelf.html", rows=ST.shelf(get_db(), g.clinic["id"], _today(), status), status=status,
                           pack_status=ST.PACK_STATUS)


@bp.get("/steri/recalls/<int:rid>")
@clinical_required
def recall_view(rid):
    data = ST.recall(get_db(), g.clinic["id"], rid)
    if not data:
        abort(404)
    return render_template("steri_recall.html", d=data, pack_status=ST.PACK_STATUS)


@bp.post("/steri/recalls/<int:rid>/close")
@manager_required
def recall_close(rid):
    try:
        ST.close_recall(get_db(), g.clinic, g.user["id"], rid, request.form.get("note"))
        flash("Recall closed.", "ok")
    except ST.SteriError as exc:
        _fail(exc)
    return redirect(url_for("steri.recall_view", rid=rid))


@bp.route("/patients/<int:pid>/instruments", methods=["GET", "POST"])
@clinical_required
def patient_instruments(pid):
    db = get_db()
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (pid, g.clinic["id"])).fetchone()
    if not p:
        abort(404)
    if request.method == "POST":
        try:
            ST.use_pack(db, g.clinic, g.user["id"], request.form.get("code"), pid, request.form.get("appointment_id", type=int))
            flash("Pack recorded on this patient.", "ok")
        except ST.SteriError as exc:
            _fail(exc)
        return redirect(url_for("steri.patient_instruments", pid=pid))
    visits = db.execute("SELECT id, start_at FROM appointments WHERE clinic_id = ? AND patient_id = ? AND substr(start_at, 1, 10) = ? "
                        "ORDER BY start_at", (g.clinic["id"], pid, _today().isoformat())).fetchall()
    return render_template("steri_patient.html", p=p, rows=ST.for_patient(db, g.clinic["id"], pid), visits=visits)
