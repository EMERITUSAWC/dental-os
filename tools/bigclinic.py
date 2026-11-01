"""Fill one clinic with a lot of made-up records and time the main pages. For checking speed, never for a real database.

    python tools/bigclinic.py /path/to/scratch.sqlite            # makes the database, fills it, times the pages
    python tools/bigclinic.py /path/to/scratch.sqlite --time      # only times the pages again

None of these are real people. Names are built from a short list, phones are in the +233 50 range used in the demo.
"""
import random
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.security import generate_password_hash  # noqa: E402

from dental_os import create_app, db as dbmod  # noqa: E402
from dental_os.services import recalls, treatment  # noqa: E402

PW = "plantain kente harmattan lagoon"
FIRST = ["Ama", "Kofi", "Akua", "Kwame", "Efua", "Yaw", "Abena", "Kojo", "Esi", "Kwesi", "Adwoa", "Fiifi", "Afia", "Kwabena", "Yaa"]
LAST = ["Mensah", "Owusu", "Boateng", "Asante", "Osei", "Addo", "Quaye", "Tetteh", "Ankrah", "Lamptey", "Darko", "Appiah",
        "Nkrumah", "Sarpong", "Agyeman", "Ofori", "Bonsu", "Amoah", "Frimpong", "Acheampong"]

SIZES = {"patients": 20000, "appointments": 100000, "procedures": 60000, "payments": 30000, "notes": 20000, "audit": 200000}

SKIP = ("/kiosk", "/operator", "/signup", "/login", "/billing/paused", "/api/", "/health", "/static")
EXTRA = ["/diary?day={today}", "/patients?q=Mensah", "/patients?q=0501234", "/patients?q=Ama%20Owusu"]


def pages(app):
    """Every page with no record number, every page about one patient, and a few searches."""
    out = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if "GET" not in rule.methods or rule.rule.startswith(SKIP) or rule.rule.endswith("/exit"):
            continue
        if not rule.arguments:
            out.append(rule.rule)
        elif rule.arguments == {"pid"} and rule.rule.startswith("/patients/<int:pid>"):
            out.append(rule.rule.replace("<int:pid>", "{pid}"))
    return out + EXTRA


def seed(conn, cid, scale=1.0, rng=None):
    rng = rng or random.Random(56)
    n = {k: int(v * scale) for k, v in SIZES.items()}
    today = date.today()
    providers = [r[0] for r in conn.execute("SELECT id FROM providers WHERE clinic_id = ?", (cid,))]
    while len(providers) < 4:
        providers.append(conn.execute("INSERT INTO providers(clinic_id, name) VALUES (?,?)", (cid, f"Dr Test {len(providers) + 1}")).lastrowid)
    chairs = [r[0] for r in conn.execute("SELECT id FROM chairs WHERE clinic_id = ?", (cid,))]
    while len(chairs) < 4:
        chairs.append(conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (?,?)", (cid, f"Chair {len(chairs) + 1}")).lastrowid)
    treatment.ensure_catalogue(conn, cid)
    rtype = recalls.ensure_default_type(conn, cid)
    codes = [(r[0], r[1]) for r in conn.execute("SELECT id, fee_p FROM procedure_codes WHERE clinic_id = ?", (cid,))]
    start = conn.execute("SELECT COALESCE(MAX(id), 0) FROM patients").fetchone()[0] + 1
    conn.executemany(
        "INSERT INTO patients(clinic_id, first_name, last_name, dob, phone, reminders_ok, created_at) VALUES (?,?,?,?,?,?,?)",
        [(cid, rng.choice(FIRST), rng.choice(LAST), (today - timedelta(days=rng.randint(2000, 30000))).isoformat(),
          f"+23350{1000000 + i:07d}", rng.randint(0, 1), (today - timedelta(days=rng.randint(0, 3000))).isoformat() + " 09:00:00")
         for i in range(n["patients"])])
    pids = list(range(start, start + n["patients"]))
    rows = []
    for i in range(n["appointments"]):
        day = today + timedelta(days=rng.randint(-1500, 60))
        status = "scheduled" if day >= today else rng.choice(["complete", "complete", "complete", "complete", "broken"])
        rows.append((cid, rng.choice(pids), rng.choice(providers), rng.choice(chairs),
                     f"{day} {rng.randint(8, 16):02d}:{rng.choice(['00', '15', '30', '45'])}", 30, status))
    conn.executemany("INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status) VALUES (?,?,?,?,?,?,?)", rows)
    rows = []
    for i in range(n["procedures"]):
        code, fee = rng.choice(codes)
        done = today - timedelta(days=rng.randint(0, 1500))
        status = rng.choice(["done", "done", "done", "planned"])
        rows.append((cid, rng.choice(pids), code, status, fee or 10000, rng.choice(providers), done.isoformat(),
                     done.isoformat() if status == "done" else None))
    conn.executemany("INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, provider_id, planned_on, done_on) VALUES (?,?,?,?,?,?,?,?)", rows)
    receipt = conn.execute("SELECT COALESCE(MAX(receipt_no), 0) FROM payments WHERE clinic_id = ?", (cid,)).fetchone()[0]
    conn.executemany("INSERT INTO payments(clinic_id, patient_id, amount_p, method, receipt_no, received_on) VALUES (?,?,?,?,?,?)",
                     [(cid, rng.choice(pids), rng.randint(5, 200) * 1000, rng.choice(["cash", "mobile_money", "card"]), receipt + 1 + i,
                       (today - timedelta(days=rng.randint(0, 1500))).isoformat()) for i in range(n["payments"])])
    conn.executemany("INSERT INTO recalls(clinic_id, patient_id, type_id, due_on) VALUES (?,?,?,?)",
                     [(cid, p, rtype, (today + timedelta(days=rng.randint(-400, 200))).isoformat()) for p in pids[: n["patients"] * 3 // 4]])
    author = conn.execute("SELECT id FROM users WHERE clinic_id = ? ORDER BY id", (cid,)).fetchone()[0]
    conn.executemany("INSERT INTO clinical_notes(clinic_id, patient_id, author_id, title, body, status) VALUES (?,?,?,'Check-up',?,'draft')",
                     [(cid, rng.choice(pids), author, "Routine check. Made-up note for speed testing.") for _ in range(n["notes"])])
    conn.executemany("INSERT INTO audit_log(clinic_id, user_id, action, object_type, object_id) VALUES (?,?,?,?,?)",
                     [(cid, author, "patient.view", "patient", rng.choice(pids)) for _ in range(n["audit"])])
    conn.commit()
    return pids


def make(path, scale=1.0):
    app = create_app({"DATABASE": str(path), "SECRET_KEY": "speed-test"})
    r = app.test_cli_runner()
    r.invoke(args=["create-clinic", "--name", "Speed Test Dental", "--country", "GH"])
    conn = dbmod.connect(str(path))
    conn.execute("INSERT INTO users(clinic_id, username, display_name, role, password_hash) VALUES (1,'speed','Speed','manager',?)",
                 (generate_password_hash(PW),))
    conn.commit()
    t = time.perf_counter()
    seed(conn, 1, scale)
    conn.close()
    return app, time.perf_counter() - t


_TRACE = []


def _traced_connect(real):
    """Notes when each statement starts. The gap to the next one is roughly how long it took, Python work included."""
    def connect(path):
        conn = real(path)
        if hasattr(conn, "set_trace_callback"):
            conn.set_trace_callback(lambda sql: _TRACE.append((time.perf_counter(), sql)))
        return conn
    return connect


def _worst(end):
    gaps = [(_TRACE[i + 1][0] if i + 1 < len(_TRACE) else end) - t for i, (t, _sql) in enumerate(_TRACE)]
    if not gaps:
        return ""
    i = max(range(len(gaps)), key=gaps.__getitem__)
    return f"   slowest step {gaps[i] * 1000:.0f} ms after: {' '.join(_TRACE[i][1].split())[:160]}"


def time_pages(app, repeat=3):
    dbmod.connect = _traced_connect(dbmod.connect)
    c = app.test_client()
    token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
    c.post("/login", data={"_csrf": token, "username": "speed", "password": PW})
    conn = dbmod.connect(app.config["DATABASE"])
    pid = conn.execute("""SELECT patient_id FROM appointments WHERE clinic_id = 1 GROUP BY patient_id ORDER BY COUNT(*) DESC LIMIT 1""").fetchone()[0]
    conn.close()
    out = []
    for page in pages(app):
        url = page.format(pid=pid, today=date.today().isoformat())
        best, code = None, None
        for _ in range(repeat):
            _TRACE.clear()
            t = time.perf_counter()
            r = c.get(url)
            took = time.perf_counter() - t
            worst = _worst(time.perf_counter())
            code = r.status_code
            best = took if best is None else min(best, took)
        out.append((url, code, best))
        print(f"{best * 1000:8.0f} ms  {code}  {url}" + (worst if best > 0.3 else ""), flush=True) if best > 0.25 or code != 200 or "-v" in sys.argv else None
    return out


if __name__ == "__main__":
    path = Path(sys.argv[1])
    if "--time" in sys.argv:
        app = create_app({"DATABASE": str(path), "SECRET_KEY": "speed-test"})
    else:
        if path.exists():
            sys.exit("That file is already there. Give a new path, or add --time to time it again.")
        app, took = make(path)
        print(f"Filled in {took:.0f} s", flush=True)
    time_pages(app, 1 if "--once" in sys.argv else 3)
