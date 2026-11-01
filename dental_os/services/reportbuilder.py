"""A report builder for managers (version 0.28): pick what to report on, the columns, filters, grouping and order, then save the
report, share it with the team, and download it as a spreadsheet.

Safety: there is no free SQL. Every data set, column and filter is from the lists below, every value is passed as a parameter, and
every query is held to the user's own clinic. Clinical data sets are only for staff who may see clinical records. No web framework here."""
import json
import re

from . import audit, permissions

MAX_ROWS = 5000
# data set: (label, FROM ... with joins, clinical only?, {column key: (label, SQL expression, type)})
#   types: text, date, money (minor units), number, flag
DATASETS = {
    "patients": ("Patients", "patients t LEFT JOIN referrers rf ON rf.id = t.referrer_id", False, {
        "id": ("Patient number", "t.id", "number"), "first_name": ("First name", "t.first_name", "text"),
        "last_name": ("Last name", "t.last_name", "text"), "dob": ("Date of birth", "t.dob", "date"),
        "birth_year": ("Year of birth", "substr(t.dob, 1, 4)", "text"), "gender": ("Sex", "t.gender", "text"),
        "phone": ("Phone", "t.phone", "text"), "email": ("Email", "t.email", "text"), "status": ("Status", "t.status", "text"),
        "joined": ("Added on", "substr(t.created_at, 1, 10)", "date"), "joined_month": ("Month added", "substr(t.created_at, 1, 7)", "text"),
        "reminders_ok": ("Agreed to reminders", "t.reminders_ok", "flag"), "offers_ok": ("Agreed to offers", "t.offers_ok", "flag"),
        "language": ("Language", "COALESCE(t.language, '')", "text"), "source": ("How they heard of us", "COALESCE(t.referral_source, '')", "text"),
        "referrer": ("Referred by", "COALESCE(rf.name, '')", "text")}),
    "appointments": ("Appointments", "appointments t JOIN patients p ON p.id = t.patient_id LEFT JOIN providers pv ON pv.id = t.provider_id "
                                     "LEFT JOIN chairs ch ON ch.id = t.chair_id", False, {
        "day": ("Day", "substr(t.start_at, 1, 10)", "date"), "month": ("Month", "substr(t.start_at, 1, 7)", "text"),
        "time": ("Time", "substr(t.start_at, 12, 5)", "text"), "patient": ("Patient", "p.first_name || ' ' || p.last_name", "text"),
        "patient_id": ("Patient number", "t.patient_id", "number"), "dentist": ("Dentist", "COALESCE(pv.name, '')", "text"),
        "chair": ("Chair", "COALESCE(ch.name, '')", "text"), "minutes": ("Length (minutes)", "t.duration_min", "number"),
        "status": ("Status", "t.status", "text"), "confirmation": ("Confirmation", "t.confirmation", "text"),
        "reason": ("Reason", "COALESCE(t.category, '')", "text"), "break_reason": ("Why missed or cancelled", "COALESCE(t.break_reason, '')", "text")}),
    "treatment": ("Treatment", "procedures t JOIN patients p ON p.id = t.patient_id JOIN procedure_codes pc ON pc.id = t.code_id "
                               "LEFT JOIN providers pv ON pv.id = t.provider_id", True, {
        "done_on": ("Done on", "t.done_on", "date"), "done_month": ("Month done", "substr(t.done_on, 1, 7)", "text"),
        "planned_on": ("Planned on", "t.planned_on", "date"), "status": ("Status", "t.status", "text"),
        "code": ("Code", "pc.code", "text"), "procedure": ("Procedure", "pc.name", "text"), "category": ("Category", "pc.category", "text"),
        "tooth": ("Tooth (FDI)", "t.tooth", "number"), "fee": ("Fee", "t.fee_p", "money"), "dentist": ("Dentist", "COALESCE(pv.name, '')", "text"),
        "patient": ("Patient", "p.first_name || ' ' || p.last_name", "text"), "patient_id": ("Patient number", "t.patient_id", "number")}),
    "payments": ("Payments", "payments t JOIN patients p ON p.id = t.patient_id", False, {
        "received_on": ("Received on", "t.received_on", "date"), "month": ("Month", "substr(t.received_on, 1, 7)", "text"),
        "receipt": ("Receipt number", "t.receipt_no", "number"), "method": ("Paid by", "t.method", "text"), "amount": ("Amount", "t.amount_p", "money"),
        "reference": ("Reference", "COALESCE(t.reference, '')", "text"), "purpose": ("For", "t.purpose", "text"), "voided": ("Voided", "t.voided", "flag"),
        "patient": ("Patient", "p.first_name || ' ' || p.last_name", "text"), "patient_id": ("Patient number", "t.patient_id", "number")}),
    "claims": ("Insurance claims", "claims t JOIN patients p ON p.id = t.patient_id JOIN payers py ON py.id = t.payer_id", False, {
        "claim_no": ("Claim number", "t.claim_no", "number"), "kind": ("Kind", "t.kind", "text"), "status": ("Status", "t.status", "text"),
        "payer": ("Payer", "py.name", "text"), "submitted_on": ("Sent on", "COALESCE(t.submitted_on, '')", "date"),
        "month": ("Month sent", "substr(COALESCE(t.submitted_on, ''), 1, 7)", "text"), "total": ("Total", "t.total_fee_p", "money"),
        "estimate": ("Payer expected to pay", "t.estimate_p", "money"), "paid": ("Paid", "t.paid_p", "money"), "writeoff": ("Written off", "t.writeoff_p", "money"),
        "patient": ("Patient", "p.first_name || ' ' || p.last_name", "text")}),
    "lab": ("Lab cases", "lab_cases t JOIN patients p ON p.id = t.patient_id JOIN labs l ON l.id = t.lab_id", True, {
        "kind": ("Work", "t.kind", "text"), "lab": ("Lab", "l.name", "text"), "status": ("Status", "t.status", "text"),
        "sent_on": ("Sent", "COALESCE(t.sent_on, '')", "date"), "due_on": ("Due back", "COALESCE(t.due_on, '')", "date"),
        "received_on": ("Back", "COALESCE(t.received_on, '')", "date"), "cost": ("Cost", "t.cost_p", "money"), "remake": ("Remake", "CASE WHEN t.remake_of IS NULL THEN 0 ELSE 1 END", "flag"),
        "patient": ("Patient", "p.first_name || ' ' || p.last_name", "text")}),
}
OPS = {"eq": "is", "ne": "is not", "contains": "contains", "gte": "is at least", "lte": "is at most", "empty": "is empty", "filled": "is not empty"}
AGGS = {"count": "Count", "sum": "Total", "avg": "Average", "min": "Smallest", "max": "Largest"}


class ReportError(Exception):
    pass


def allowed(user, ds):
    return ds in DATASETS and (not DATASETS[ds][2] or permissions.can_clinical(user))


def clean_spec(user, spec):
    """Check a report definition and return a tidy copy. Anything not in the lists is refused."""
    ds = spec.get("dataset")
    if not allowed(user, ds):
        raise ReportError("Pick what to report on.")
    cols = DATASETS[ds][3]
    columns = [c for c in spec.get("columns") or [] if c in cols]
    group = spec.get("group_by") or ""
    if group and group not in cols:
        raise ReportError("Pick a column to group by from the list.")
    agg = spec.get("agg") or "count"
    if agg not in AGGS:
        raise ReportError("Pick how to sum up each group.")
    measure = spec.get("measure") or ""
    if agg != "count" and (measure not in cols or cols[measure][2] not in ("money", "number")):
        raise ReportError("Pick a money or number column to add up.")
    if not group and not columns:
        raise ReportError("Pick at least one column.")
    filters = []
    for f in spec.get("filters") or []:
        col, op, val = f.get("col"), f.get("op"), (f.get("value") or "").strip()
        if not col:
            continue
        if col not in cols or op not in OPS:
            raise ReportError("One of the filters is not right.")
        if op not in ("empty", "filled") and val == "":
            raise ReportError("Give each filter a value.")
        if cols[col][2] == "money" and op not in ("empty", "filled"):
            try:
                val = str(round(float(val.replace(",", "")) * 100))
            except ValueError:
                raise ReportError("A money filter needs a number, for example 100.")
        if cols[col][2] == "date" and op in ("gte", "lte", "eq", "ne") and not re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", val):
            raise ReportError("Write dates like 2026-10-01, or a month like 2026-10.")
        filters.append({"col": col, "op": op, "value": val[:100]})
    if len(filters) > 8:
        raise ReportError("Use at most 8 filters.")
    sort = spec.get("sort") or ""
    if sort and sort not in cols and sort != "_value":
        raise ReportError("Pick a column to sort by from the list.")
    return {"dataset": ds, "columns": columns, "group_by": group, "agg": agg, "measure": measure if agg != "count" else "",
            "filters": filters, "sort": sort, "desc": bool(spec.get("desc"))}


def build_sql(cid, spec):
    label, src, _clin, cols = DATASETS[spec["dataset"]]
    where, args = ["t.clinic_id = ?"], [cid]
    for f in spec["filters"]:
        expr, typ = cols[f["col"]][1], cols[f["col"]][2]
        op, val = f["op"], f["value"]
        if op == "empty":
            where.append(f"({expr} IS NULL OR {expr} = '')" if typ in ("text", "date") else f"{expr} IS NULL")
        elif op == "filled":
            where.append(f"({expr} IS NOT NULL AND {expr} != '')" if typ in ("text", "date") else f"{expr} IS NOT NULL")
        elif op == "contains":
            where.append(f"LOWER(CAST({expr} AS TEXT)) LIKE ?")
            args.append("%" + val.lower().replace("%", "").replace("_", "") + "%")
        else:
            sym = {"eq": "=", "ne": "!=", "gte": ">=", "lte": "<="}[op]
            if typ in ("money", "number", "flag"):
                try:
                    num = int(float(val)) if typ != "flag" else (1 if val.lower() in ("1", "yes", "true", "y") else 0)
                except ValueError:
                    raise ReportError("A number filter needs a number.")
                where.append(f"{expr} {sym} ?")
                args.append(num)
            elif typ == "date" and op in ("lte",) and len(val) == 7:
                where.append(f"substr({expr}, 1, 7) <= ?")
                args.append(val)
            elif typ == "date" and len(val) == 7 and op in ("eq", "ne"):
                where.append(f"substr({expr}, 1, 7) {sym} ?")
                args.append(val)
            else:
                where.append(f"{expr} {sym} ?")
                args.append(val)
    w = " AND ".join(where)
    if spec["group_by"]:
        g = cols[spec["group_by"]][1]
        agg = "COUNT(*)" if spec["agg"] == "count" else f"{spec['agg'].upper()}({cols[spec['measure']][1]})"
        order = "2" if spec["sort"] == "_value" else "1"
        sql = f"SELECT {g} AS k, {agg} AS v FROM {src} WHERE {w} GROUP BY {g} ORDER BY {order} {'DESC' if spec['desc'] else 'ASC'} LIMIT {MAX_ROWS}"
    else:
        sel = ", ".join(f"{cols[c][1]} AS c{i}" for i, c in enumerate(spec["columns"]))
        order = cols[spec["sort"]][1] if spec["sort"] in cols else "t.id"
        sql = f"SELECT {sel} FROM {src} WHERE {w} ORDER BY {order} {'DESC' if spec['desc'] else 'ASC'}, t.id LIMIT {MAX_ROWS + 1}"
    return sql, args


def run(db, user, spec):
    spec = clean_spec(user, spec)
    sql, args = build_sql(user["clinic_id"], spec)
    rows = db.execute(sql, args).fetchall()
    cols = DATASETS[spec["dataset"]][3]
    if spec["group_by"]:
        head = [(cols[spec["group_by"]][0], cols[spec["group_by"]][2]),
                (AGGS[spec["agg"]] + ("" if spec["agg"] == "count" else " of " + cols[spec["measure"]][0].lower()),
                 "number" if spec["agg"] == "count" else cols[spec["measure"]][2])]
        data = [[r[0], r[1]] for r in rows]
        more = False
    else:
        head = [(cols[c][0], cols[c][2]) for c in spec["columns"]]
        more = len(rows) > MAX_ROWS
        data = [list(r) for r in rows[:MAX_ROWS]]
    return {"spec": spec, "head": head, "rows": data, "more": more}


# --------------------------------------------------------------------------------------------------------- saved reports
def save(db, clinic, user, name, spec, shared, report_id=None):
    name = (name or "").strip()[:80]
    if not name:
        raise ReportError("Give the report a name.")
    spec = clean_spec(user, spec)
    body = json.dumps(spec)
    if report_id:
        r = get(db, clinic["id"], report_id)
        if not r or (r["created_by"] != user["id"] and user["role"] != "manager"):
            raise ReportError("That report was not found.")
        db.execute("UPDATE saved_reports SET name = ?, spec = ?, shared = ?, updated_at = datetime('now') WHERE id = ?",
                   (name, body, 1 if shared else 0, report_id))
    else:
        report_id = db.execute("INSERT INTO saved_reports(clinic_id, name, spec, shared, created_by) VALUES (?,?,?,?,?)",
                               (clinic["id"], name, body, 1 if shared else 0, user["id"])).lastrowid
    audit.log(db, clinic["id"], user["id"], "report.save", None, None, name)
    return report_id


def get(db, cid, report_id):
    return db.execute("SELECT * FROM saved_reports WHERE id = ? AND clinic_id = ? AND archived = 0", (report_id, cid)).fetchone()


def visible(db, user):
    rows = db.execute("""SELECT r.*, u.display_name AS by_name FROM saved_reports r LEFT JOIN users u ON u.id = r.created_by
                         WHERE r.clinic_id = ? AND r.archived = 0 AND (r.shared = 1 OR r.created_by = ?) ORDER BY r.name""",
                      (user["clinic_id"], user["id"])).fetchall()
    return [r for r in rows if allowed(user, json.loads(r["spec"])["dataset"])]


def archive(db, clinic, user, report_id):
    r = get(db, clinic["id"], report_id)
    if not r or (r["created_by"] != user["id"] and user["role"] != "manager"):
        raise ReportError("That report was not found.")
    db.execute("UPDATE saved_reports SET archived = 1 WHERE id = ?", (report_id,))
    audit.log(db, clinic["id"], user["id"], "report.archive", None, None, r["name"])


# Every label the builder shows, so the French catalogue can be checked against them.
LABELS = {v[0]: v[0] for v in DATASETS.values()}
LABELS.update({c[0]: c[0] for v in DATASETS.values() for c in v[3].values()})
