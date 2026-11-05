"""v0.74: comparing X-rays and photos over time, one place for notifications, an optional credit card fee that follows each
country's rules, and first-time guided tours of the main screens."""
import re

from tests.test_v04 import Base, PNG
from tests import a11y
from dental_os.services import cardfee as CF, files as F, notify, tours as TO


class XBase(Base):
    def picture(self, conn, taken_on, teeth=None, kind="xray", mime="image/png", pid=1, name=None):
        return conn.execute("""INSERT INTO patient_files(clinic_id, patient_id, kind, original_name, stored_name, mime, size_bytes, taken_on, teeth)
                               VALUES (1, ?, ?, 'x.png', ?, ?, 10, ?, ?)""", (pid, kind, name or f"s{taken_on}{teeth}{pid}{kind}.png", mime, taken_on, teeth)).lastrowid

    def clinic(self, conn):
        return conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()


class Compare(XBase):
    def test_teeth_are_read_in_the_clinics_numbering(self):
        self.assertEqual(F.parse_teeth("36, 37 36"), "36,37")
        self.assertIsNone(F.parse_teeth("  "))
        with self.assertRaises(F.FileError):
            F.parse_teeth("36, 99")

    def test_every_picture_of_a_tooth_oldest_first(self):
        conn = self.db()
        a = self.picture(conn, "2024-01-10", "36")
        b = self.picture(conn, "2026-01-10", "36,37")
        self.picture(conn, "2025-06-01", "11")
        self.picture(conn, "2025-03-01", "36", kind="document", mime="application/pdf")     # not a picture
        self.picture(conn, "2025-03-01", "36", pid=2)                                       # someone else
        rows = F.for_tooth(conn, 1, 1, 36)
        self.assertEqual([r["id"] for r in rows], [a, b])
        self.assertEqual(F.gaps(rows), [0, 731])
        conn.close()

    def test_chosen_pictures_must_be_this_patients(self):
        conn = self.db()
        mine = [self.picture(conn, f"2025-0{i}-01", "36") for i in range(1, 9)]
        other = self.picture(conn, "2025-01-01", "36", pid=2)
        rows = F.pick(conn, 1, 1, [str(other)] + [str(i) for i in mine] + ["x", "1e9"])
        self.assertEqual(len(rows), F.COMPARE_MAX)
        self.assertNotIn(other, [r["id"] for r in rows])
        conn.close()

    def test_compare_page_and_teeth_form(self):
        conn = self.db()
        a = self.picture(conn, "2024-01-10")
        self.picture(conn, "2026-01-10", "36")
        conn.commit()
        conn.close()
        c = self.login("dent")
        self.post(c, f"/files/{a}/details", {"teeth": "36", "taken_on": "2024-01-10"})
        page = self.page(c, "/patients/1/compare?tooth=36")
        self.assertEqual(page.count("<figure>"), 2)
        self.assertIn("days after picture 1", page)
        self.assertEqual(a11y.check(page), [])
        self.assertEqual(self.login("desk").get("/patients/1/compare").status_code, 403)
        conn = self.db()
        self.assertIn("file.compare", [r[0] for r in conn.execute("SELECT action FROM audit_log")])
        conn.close()


class Notifications(XBase):
    def test_items_follow_the_role(self):
        conn = self.db()
        conn.execute("INSERT INTO booking_requests(clinic_id, first_name, last_name, phone, reason, wanted_start, status) VALUES (1, 'New', 'Person', '+233201112299', 'Check-up', '2026-12-01 09:00', 'new')")
        conn.execute("""INSERT INTO clinical_notes(clinic_id, patient_id, template, title, body, status, author_id)
                        VALUES (1, 1, 'free', 'Note', 'x', 'draft', (SELECT id FROM users WHERE username = 'dent'))""")
        conn.commit()
        dent = conn.execute("SELECT * FROM users WHERE username = 'dent'").fetchone()
        desk = conn.execute("SELECT * FROM users WHERE username = 'desk'").fetchone()
        keys_dent = {r["key"] for r in notify.items(conn, self.clinic(conn), dent)}
        keys_desk = {r["key"] for r in notify.items(conn, self.clinic(conn), desk)}
        self.assertIn("unsigned", keys_dent)
        self.assertNotIn("unsigned", keys_desk)
        self.assertIn("requests", keys_desk)
        rows = notify.items(conn, self.clinic(conn), dent)
        self.assertEqual(rows[0]["level"], "urgent")                                         # most urgent first
        conn.close()
        page = self.page(self.login("desk"), "/notifications")
        self.assertIn("Online booking requests", page)
        self.assertEqual(a11y.check(page), [])

    def test_every_link_exists(self):
        names = set(self.app.view_functions)
        import inspect
        src = inspect.getsource(notify)
        for endpoint in re.findall(r'"([a-z_]+\.[a-z_]+)"', src):
            self.assertIn(endpoint, names, endpoint)


class CardFee(XBase):
    def test_the_rules(self):
        self.assertEqual(CF.parse_pct("2.5"), 250)
        self.assertEqual(CF.parse_pct(""), 0)
        for bad in ("3.5", "-1", "two"):
            with self.assertRaises(CF.CardFeeError):
                CF.parse_pct(bad)
        conn = self.db()
        c = self.clinic(conn)                                   # Ghana: allowed
        CF.set_fee(conn, c, 1, 250)
        self.assertEqual(CF.fee_for(self.clinic(conn), 100000), 2500)
        uk = conn.execute("SELECT * FROM clinics WHERE id = 2").fetchone()
        conn.execute("UPDATE clinics SET country = 'GB' WHERE id = 2")
        uk = conn.execute("SELECT * FROM clinics WHERE id = 2").fetchone()
        with self.assertRaises(CF.CardFeeError):
            CF.set_fee(conn, uk, 1, 200)
        conn.close()

    def test_desk_fee_is_its_own_line_and_not_treatment_money(self):
        conn = self.db()
        conn.execute("UPDATE clinics SET card_fee_bp = 200, payment_methods = 'cash,mobile_money,card' WHERE id = 1")
        conn.commit()
        conn.close()
        c = self.login("mgr")
        r = self.post(c, "/patients/1/payments", {"amount": "500", "method": "card", "reference": "AUTH123", "credit_card_fee": "1"})
        conn = self.db()
        rows = conn.execute("SELECT amount_p, purpose, reference FROM payments ORDER BY id").fetchall()
        self.assertEqual([tuple(x) for x in rows], [(50000, "treatment", "AUTH123"), (1000, "card_fee", "AUTH123 (fee)")])
        from dental_os.services import billing
        self.assertEqual(billing.totals(conn, 1, 1)["paid"], 50000)                          # the fee never pays for treatment
        conn.close()
        self.assertIn("Card fee", c.get(r.headers["Location"]).get_data(as_text=True))
        self.post(c, "/patients/1/payments", {"amount": "100", "method": "card", "reference": "AUTH124"})   # box not ticked: no fee
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM payments WHERE purpose = 'card_fee'").fetchone()[0], 1)
        conn.close()

    def test_setting_page(self):
        c = self.login("mgr")
        self.post(c, "/settings/online-payments", {"action": "card_fee", "card_fee": "2"})
        conn = self.db()
        self.assertEqual(conn.execute("SELECT card_fee_bp FROM clinics WHERE id = 1").fetchone()[0], 200)
        conn.close()
        self.assertIn("credit card fee of 2.00%", self.page(c, "/patients/1/account"))


class Tours(XBase):
    def test_a_tour_shows_once_and_can_come_back(self):
        c = self.login("desk")
        self.assertIn('id="tour"', self.page(c, "/"))
        token = re.search(r'data-csrf="([^"]+)"', self.page(c, "/")).group(1)
        self.assertEqual(c.post("/tours/today/done", data={"_csrf": token}).status_code, 200)
        self.assertNotIn('id="tour"', self.page(c, "/"))
        self.assertIn('id="tour"', self.page(c, "/diary"))                                    # each screen has its own
        self.assertEqual(c.post("/tours/nonsense/done", data={"_csrf": token}).status_code, 404)
        self.post(c, "/tours/again")
        self.assertIn('id="tour"', self.page(c, "/"))

    def test_steps_point_at_parts_that_exist(self):
        c = self.login("dent")
        pages = {"today": "/", "diary": "/diary", "patient": "/patients/1", "chart": "/patients/1/chart"}
        for key, url in pages.items():
            html = self.page(c, url)
            found = sum(1 for sel, _t in TO.TOURS[key][1] if _present(html, sel))
            self.assertGreaterEqual(found, 1, key)


def _present(html, sel):
    if sel.startswith("#"):
        return f'id="{sel[1:]}"' in html
    if sel.startswith("."):
        return re.search(r'class="[^"]*\b' + re.escape(sel[1:]) + r'\b', html) is not None
    if sel.startswith("a[href$='"):
        return sel.split("'")[1] in html
    if sel == "h1":
        return "<h1" in html
    if sel == "nav.tabs":
        return 'class="tabs"' in html
    return False
