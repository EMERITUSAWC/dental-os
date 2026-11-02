"""v0.69: the company's own staff. Each operator has a role and reaches only that part of the operator area; the owner runs the
team; a new person chooses their own password and sets up two-step login before anything else."""
import re
import time

from tests.test_v04 import Base
from dental_os.services import opteam as OT, platform as PF, twofactor

PW = "a-long-operator-password"


class Ops(Base):
    def setUp(self):
        super().setUp()
        self.app.config["OPERATOR_REQUIRE_2FA"] = False
        conn = self.db()
        PF.add_operator(conn, "owner", "The Owner", PW)
        conn.commit()
        conn.close()

    def op_login(self, username, password=PW, code=None):
        c = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/operator/login").get_data(as_text=True)).group(1)
        r = c.post("/operator/login", data={"_csrf": token, "username": username, "password": password, "code": code or ""})
        return c, r

    def op_post(self, c, url, data):
        page = c.get("/operator/security").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        return c.post(url, data={"_csrf": token, **data})

    def add(self, c, username, role):
        r = self.op_post(c, "/operator/team", {"what": "add", "username": username, "display_name": username.title(), "role": role})
        return re.search(r"<code>([^<]+)</code>", r.get_data(as_text=True)).group(1)

    def first_sign_in(self, username, temp, new="a-brand-new-password-1"):
        c, r = self.op_login(username, temp)
        self.assertIn("/operator/security", r.headers["Location"])
        self.assertIn("/operator/security", c.get("/operator").headers["Location"])          # nothing else until it is done
        self.op_post(c, "/operator/security", {"what": "password", "current": temp, "new": new})
        return c


class TeamTests(Ops):
    def test_the_owner_adds_people_and_each_sees_only_their_part(self):
        owner, _ = self.op_login("owner")
        self.assertEqual(owner.get("/operator/team").status_code, 200)
        temps = {role: self.add(owner, f"{role}1", role) for role in ("support", "finance", "sales", "engineering")}
        pages = {"/operator": "clinics", "/operator/plans": "plans.view", "/operator/health": "health", "/operator/team": "team"}
        for role, temp in temps.items():
            c = self.first_sign_in(f"{role}1", temp)
            for url, action in pages.items():
                code = c.get(url).status_code
                self.assertEqual(code == 200, role in OT.CAN[action], f"{role} {url} {code}")
        fin = self.first_sign_in("finance2", self.add(owner, "finance2", "finance"))
        sales = self.first_sign_in("sales2", self.add(owner, "sales2", "sales"))
        self.assertEqual(self.op_post(sales, "/operator/clinics/1", {"what": "state", "state": "suspended", "reason": "x"}).status_code, 403)
        self.assertEqual(self.op_post(sales, "/operator/clinics/1", {"what": "state", "state": "trial", "trial_days": "14"}).status_code, 302)
        self.assertEqual(self.op_post(fin, "/operator/clinics/1", {"what": "support"}).status_code, 403)       # finance never opens a clinic
        self.assertEqual(self.op_post(sales, "/operator/plans", {"name": "X", "price_GHS": "100"}).status_code, 403)
        self.assertNotIn("Bills", sales.get("/operator/clinics/1").get_data(as_text=True))
        self.assertIn("Bills", fin.get("/operator/clinics/1").get_data(as_text=True))
        conn = self.db()
        log = [r[0] for r in conn.execute("SELECT action FROM operator_log ORDER BY id")]
        conn.close()
        self.assertIn("team.add", log)
        self.assertIn("password.change", log)

    def test_a_one_time_password_works_once(self):
        owner, _ = self.op_login("owner")
        temp = self.add(owner, "helper", "support")
        self.first_sign_in("helper", temp)
        _, r = self.op_login("helper", temp)
        self.assertNotIn("Location", r.headers)                                            # the old one-time password is refused

    def test_there_is_always_an_owner(self):
        conn = self.db()
        me = conn.execute("SELECT * FROM operators WHERE username = 'owner'").fetchone()
        with self.assertRaisesRegex(OT.TeamError, "Ask another owner"):
            OT.set_role(conn, me, me["id"], "sales")                                       # nobody changes their own login
        oid, _t = OT.add(conn, me, "second", "Second", "owner")
        second = conn.execute("SELECT * FROM operators WHERE id = ?", (oid,)).fetchone()
        OT.set_role(conn, second, me["id"], "sales")                                       # fine: "second" is still an owner
        me = conn.execute("SELECT * FROM operators WHERE id = ?", (me["id"],)).fetchone()
        conn.execute("UPDATE operators SET role = 'owner' WHERE id = ?", (me["id"],))
        conn.execute("UPDATE operators SET active = 0 WHERE id = ?", (oid,))                # leave just one owner switched on
        with self.assertRaisesRegex(OT.TeamError, "always be one owner"):
            OT.set_role(conn, second, me["id"], "sales")
        conn.close()

    def test_two_step_login_is_set_up_on_the_web(self):
        self.app.config["OPERATOR_REQUIRE_2FA"] = True
        conn = self.db()
        me = conn.execute("SELECT * FROM operators WHERE username = 'owner'").fetchone()
        _oid, temp = OT.add(conn, me, "newbie", "Newbie", "engineering")
        conn.commit()
        conn.close()
        c = self.first_sign_in("newbie", temp)
        page = c.get("/operator/security").get_data(as_text=True)
        self.assertIn("must use two-step login", page)
        secret = re.search(r"<code>([A-Z2-7]{16,})</code>", page).group(1)
        self.assertEqual(c.get("/operator/health").status_code, 302)                         # still waiting for two-step login
        code = twofactor.code_at(secret, int(time.time() // 30))
        r = self.op_post(c, "/operator/security", {"what": "2fa", "code": code})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get("/operator/health").status_code, 200)
        self.assertEqual(c.get("/operator/team").status_code, 403)
        _, r = self.op_login("newbie", "a-brand-new-password-1", "000000")
        self.assertNotIn("Location", r.headers)                                             # a wrong code is refused
