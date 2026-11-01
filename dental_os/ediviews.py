"""US electronic claims screens (version 0.23): set-up, making claim files, reading remittances and answers."""
from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import clearinghouse as CH, edi as E, insurance as INS

bp = Blueprint("edi", __name__)
MAX_FILE = 5 * 1024 * 1024


def _fail(exc):
    get_db().rollback()
    flash(str(exc), "error")


@bp.route("/claims/electronic/setup", methods=["GET", "POST"])
@manager_required
def setup():
    db = get_db()
    cid = g.clinic["id"]
    if request.method == "POST":
        try:
            if request.form.get("what") == "codes":
                f = request.form
                E.save_codes(db, g.clinic, g.user["id"],
                             {int(k[4:]): v for k, v in f.items() if k.startswith("cdt_") and k[4:].isdigit()},
                             {int(k[4:]): v for k, v in f.items() if k.startswith("npi_") and k[4:].isdigit()},
                             {int(k[6:]): v for k, v in f.items() if k.startswith("payer_") and k[6:].isdigit()})
            else:
                E.save_settings(db, g.clinic, g.user["id"], request.form)
            flash("Saved.", "ok")
        except E.EdiError as exc:
            _fail(exc)
        return redirect(url_for("edi.setup"))
    codes = db.execute("SELECT id, code, name, cdt_code FROM procedure_codes WHERE clinic_id = ? AND active = 1 ORDER BY category, code", (cid,)).fetchall()
    providers = db.execute("SELECT id, name, npi FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY name", (cid,)).fetchall()
    payers = db.execute("SELECT id, name, edi_payer_id FROM payers WHERE clinic_id = ? AND active = 1 ORDER BY name", (cid,)).fetchall()
    return render_template("edi_setup.html", s=E.settings(db, cid), codes=codes, providers=providers, payers=payers)


@bp.route("/claims/electronic", methods=["GET", "POST"])
def home():
    db = get_db()
    if request.method == "POST":
        ids = [int(x) for x in request.form.getlist("claim_id") if x.isdigit()]
        try:
            fid = E.build_837(db, g.clinic, g.user["id"], ids)
            flash("Claim file made. Download it and upload it to your clearinghouse.", "ok")
            return redirect(url_for("edi.file_view", fid=fid))
        except E.EdiError as exc:
            _fail(exc)
        return redirect(url_for("edi.home"))
    cid = g.clinic["id"]
    return render_template("edi_home.html", ready=E.ready_claims(db, g.clinic), files=E.files(db, cid, 30), kinds=E.FILE_KINDS,
                           s=E.settings(db, cid), live=CH.is_on(db, cid), waiting=CH.waiting_remits(db, cid), chlog=CH.recent_log(db, cid, 10),
                           ch=CH.account(db, cid), ch_problems=CH.setup_problems(db, g.clinic) if CH.is_on(db, cid) else [])


# ------------------------------------------------------------------------------------------------ 0.63: the live connection
def _transport():
    return current_app.config.get("CLEARINGHOUSE_TRANSPORT")


@bp.route("/claims/electronic/connection", methods=["GET", "POST"])
@manager_required
def connection():
    db = get_db()
    if request.method == "POST":
        try:
            CH.save_account(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"], request.form)
            flash("Saved.", "ok")
            return redirect(url_for("edi.connection"))
        except CH.ClearinghouseError as exc:
            _fail(exc)
    a = CH.account(db, g.clinic["id"])
    return render_template("edi_connection.html", a=a, on=CH.is_on(db, g.clinic["id"]), has_key=bool(a and a["api_key"]),
                           providers=CH.PROVIDERS, problems=CH.setup_problems(db, g.clinic), s=E.settings(db, g.clinic["id"]))


@bp.post("/claims/electronic/send")
def send_live():
    """Each ticked claim on its own: one that fails is undone and logged, and the others still go."""
    db = get_db()
    ids = [int(x) for x in request.form.getlist("claim_id") if x.isdigit()]
    if not ids:
        flash("Tick at least one claim.", "error")
        return redirect(url_for("edi.home"))
    sent = failed = 0
    for claim_id in ids:
        db.execute("SAVEPOINT send_claim")
        try:
            CH.send_claim(db, g.clinic, g.user, current_app.config["SECRET_KEY"], claim_id, transport=_transport())
            db.execute("RELEASE SAVEPOINT send_claim")
            sent += 1
        except (CH.ClearinghouseError, E.EdiError) as exc:
            db.execute("ROLLBACK TO SAVEPOINT send_claim")
            db.execute("RELEASE SAVEPOINT send_claim")
            c = INS.get_claim(db, g.clinic["id"], claim_id)
            CH.log(db, g.clinic["id"], g.user["id"], "claim", False,
                   f"Claim {c['claim_no'] if c else claim_id}: {exc}" + (f" {exc.detail}" if getattr(exc, "detail", None) else ""))
            failed += 1
    db.commit()
    if failed and not sent:
        flash("No claim was sent. The reasons are in the connection log.", "error")
    elif failed:
        flash("Some claims were sent and some were not. The reasons are in the connection log.", "error")
    else:
        flash("Sent to the clearinghouse. Acknowledgements and remittances come back here.", "ok")
    return redirect(url_for("edi.home"))


@bp.post("/claims/electronic/fetch")
def fetch_now():
    db = get_db()
    try:
        got = CH.fetch(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"], transport=_transport())
        flash("Fetched. Remittances to check are listed below." if got["remits"] else "Fetched. Nothing new to check.", "ok")
    except CH.ClearinghouseError as exc:
        db.rollback()
        CH.log(db, g.clinic["id"], g.user["id"], "fetch", False, str(exc) + (f" {exc.detail}" if exc.detail else ""))
        db.commit()
        flash(str(exc), "error")
    return redirect(url_for("edi.home"))


@bp.route("/claims/electronic/files/<int:fid>/check", methods=["GET", "POST"])
def remit_check(fid):
    """A remittance fetched from the clearinghouse: shown for checking, then posted, exactly as an uploaded one."""
    db = get_db()
    f = E.get_file(db, g.clinic["id"], fid)
    if not f or f["kind"] != "835" or f["direction"] != "in":
        abort(404)
    if request.method == "POST" and request.form.get("confirm") == "1":
        try:
            r = E.post_835(db, g.clinic, g.user["id"], f["body"], file_id=fid)
            flash("Remittance posted.", "ok")
            if r["payer_payment_id"]:
                return redirect(url_for("finance.payer_payment_show", pp_id=r["payer_payment_id"]))
            return redirect(url_for("finance.claims_list"))
        except E.EdiError as exc:
            _fail(exc)
    try:
        r = E.read_835(db, g.clinic, f["body"])
    except E.EdiError as exc:
        _fail(exc)
        return redirect(url_for("edi.home"))
    return render_template("edi_835.html", r=r, text=f["body"], groups=E.CAS_GROUPS, clp=E.CLP_STATUS)


@bp.get("/claims/electronic/files/<int:fid>")
def file_view(fid):
    f = E.get_file(get_db(), g.clinic["id"], fid)
    if not f:
        abort(404)
    return render_template("edi_file.html", f=f, kinds=E.FILE_KINDS)


@bp.get("/claims/electronic/files/<int:fid>/download")
def file_download(fid):
    f = E.get_file(get_db(), g.clinic["id"], fid)
    if not f:
        abort(404)
    name = f"{f['kind']}-{f['control_no'] or f['id']}.x12"
    return Response(f["body"], mimetype="text/plain", headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _upload_text():
    up = request.files.get("file")
    text = request.form.get("text") or ""
    if up and up.filename:
        data = up.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise E.EdiError("That file is bigger than 5 MB.")
        text = data.decode("utf-8", "replace")
    if not text.strip():
        raise E.EdiError("Choose the file your clearinghouse gave you, or paste its text.")
    return text


@bp.route("/claims/electronic/import", methods=["GET", "POST"])
def import_file():
    db = get_db()
    if request.method == "POST":
        try:
            text = _upload_text()
            segs, _comp = E.parse(text)
            kind = E.kind_of(segs)
            if kind == "835":
                if request.form.get("confirm") == "1":
                    r = E.post_835(db, g.clinic, g.user["id"], text)
                    flash("Remittance posted.", "ok")
                    if r["payer_payment_id"]:
                        return redirect(url_for("finance.payer_payment_show", pp_id=r["payer_payment_id"]))
                    return redirect(url_for("finance.claims_list"))
                return render_template("edi_835.html", r=E.read_835(db, g.clinic, text), text=text, groups=E.CAS_GROUPS, clp=E.CLP_STATUS)
            if kind == "271":
                cover = E.post_271(db, g.clinic, g.user["id"], text)
                flash("Cover check saved on the patient's cover.", "ok")
                return redirect(url_for("finance.patient_insurance", pid=cover["patient_id"]))
            if kind == "999":
                fid, ok = E.post_999(db, g.clinic, g.user["id"], text)
                flash("The clearinghouse accepted the file." if ok else "The clearinghouse rejected the file. See why on the file.", "ok" if ok else "error")
                return redirect(url_for("edi.file_view", fid=fid))
            raise E.EdiError("This file type cannot be read here. Upload a remittance (835), a cover check answer (271) or a receipt (999).")
        except E.EdiError as exc:
            _fail(exc)
    return render_template("edi_import.html")


@bp.route("/cover/<int:cover_id>/us-details", methods=["GET", "POST"])
def cover_details(cover_id):
    db = get_db()
    c = INS.get_cover(db, g.clinic["id"], cover_id)
    if not c:
        abort(404)
    p = db.execute("SELECT * FROM patients WHERE id = ?", (c["patient_id"],)).fetchone()
    if request.method == "POST":
        try:
            if request.form.get("what") == "check" and CH.is_on(db, g.clinic["id"]):
                try:
                    CH.check_cover(db, g.clinic, g.user, current_app.config["SECRET_KEY"], cover_id, transport=_transport())
                    flash("The payer answered. The answer is saved on the patient's cover.", "ok")
                    return redirect(url_for("finance.patient_insurance", pid=c["patient_id"]))
                except CH.ClearinghouseError as exc:
                    db.rollback()
                    CH.log(db, g.clinic["id"], g.user["id"], "cover", False, str(exc) + (f" {exc.detail}" if exc.detail else ""))
                    db.commit()
                    flash(str(exc), "error")
                    return redirect(url_for("edi.cover_details", cover_id=cover_id))
            if request.form.get("what") == "check":
                fid = E.build_270(db, g.clinic, g.user["id"], cover_id)
                flash("Cover check file made. Download it and upload it to your clearinghouse.", "ok")
                return redirect(url_for("edi.file_view", fid=fid))
            E.save_cover_details(db, g.clinic, g.user["id"], cover_id, request.form)
            flash("Saved.", "ok")
        except E.EdiError as exc:
            _fail(exc)
        return redirect(url_for("edi.cover_details", cover_id=cover_id))
    return render_template("edi_cover.html", c=c, p=p, relations=E.RELATION)
