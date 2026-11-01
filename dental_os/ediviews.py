"""US electronic claims screens (version 0.23): set-up, making claim files, reading remittances and answers."""
from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import edi as E, insurance as INS

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
    return render_template("edi_home.html", ready=E.ready_claims(db, g.clinic), files=E.files(db, g.clinic["id"], 30), kinds=E.FILE_KINDS,
                           s=E.settings(db, g.clinic["id"]))


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
