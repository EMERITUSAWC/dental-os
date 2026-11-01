"""v0.9: tax rates per treatment, two-step login, patient data rights, incidents, procedure packs, French patient pages."""
import io
import json
import re
import sqlite3
import time
import unittest
import zipfile
from datetime import timedelta
from pathlib import Path

from tests import dbmeta
from dental_os.services import (audit, catalogue as CAT, dataio, links, outbox, patients as P, plans as PL, msgtemplates as MT, invoices as INV, passwords, privacy as PV, qr, serialize, taxes as TX, twofactor as TF,
                                webhooks as WH)
from dental_os.services.timeutil import now_local
from tests.test_v04 import PW
from tests.test_v07 import Public
from tests.test_v08 import Ops, today


class Tax(Ops):
    """Clinic one: FILL 300.00, CONS 100.00, CROWN 1500.00, SCALE 200.00 (in cedis, stored as pesewas)."""

    def set_default(self, name="VAT", pct=1500, inclusive=1):
        self.sql("UPDATE clinics SET tax_name = ?, tax_rate_bp = ?, tax_inclusive = ? WHERE id = 1", name, pct, inclusive)

    def rate(self, name, kind="rate", pct="5", clinic=1):
        c = self.clinic(clinic)
        conn = self.db()
        rid = TX.add_rate(conn, c, None, name, kind, pct)
        conn.commit()
        conn.close()
        return rid

    def assign(self, code, rate_id, clinic=1):
        conn = self.db()
        TX.set_code_rate(conn, clinic, None, self.cid(code, clinic), rate_id)
        conn.commit()
        conn.close()

    def work(self, *codes):
        ids = []
        for code in codes:
            self.done(code)
            ids.append(self.one("SELECT MAX(id) FROM procedures")[0])
        return ids

    def invoice(self, ids, clinic=1):
        conn = self.db()
        iid = INV.create(conn, dict(conn.execute("SELECT * FROM clinics WHERE id = ?", (clinic,)).fetchone()), None, 1, ids)
        conn.commit()
        conn.close()
        return iid

    def inv(self, iid):
        return self.one("SELECT * FROM invoices WHERE id = ?", iid)

    def rows(self, iid):
        return self.many("SELECT * FROM invoice_tax WHERE invoice_id = ? ORDER BY id", iid)


class TaxTests(Tax):
    def test_tax_number_line_names_the_clinics_tax_not_several_rates(self):
        self.set_default("VAT", 1500, 0)
        self.sql("UPDATE clinics SET tax_number = 'C123' WHERE id = 1")
        self.assign("CONS", self.rate("Reduced", "rate", "5"))
        iid = self.invoice(self.work("FILL", "CONS"))
        self.assertEqual(self.inv(iid)["tax_name"], "Several rates")
        page = self.login("mgr").get(f"/invoices/{iid}").get_data(as_text=True)
        self.assertIn("VAT number C123", page)
        self.assertNotIn("Several rates number", page)

    def test_single_default_rate_works_as_before(self):
        self.set_default()
        iid = self.invoice(self.work("FILL"))
        inv = self.inv(iid)
        want = INV.tax_split(30000, 1500, True)
        self.assertEqual((inv["subtotal_p"], inv["tax_p"], inv["total_p"]), want)
        self.assertEqual((inv["tax_name"], inv["tax_rate_bp"]), ("VAT", 1500))
        rows = self.rows(iid)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["name"], rows[0]["net_p"], rows[0]["tax_p"], rows[0]["gross_p"]), ("VAT", want[0], want[1], 30000))

    def test_no_tax_at_all_writes_no_breakdown(self):
        iid = self.invoice(self.work("FILL"))
        inv = self.inv(iid)
        self.assertEqual((inv["tax_p"], inv["total_p"], inv["tax_name"]), (0, 30000, None))
        self.assertEqual(self.rows(iid), [])

    def test_mixed_rates_on_one_invoice(self):
        self.set_default()
        exempt = self.rate("Exempt", "exempt")
        self.assign("CONS", exempt)
        iid = self.invoice(self.work("FILL", "CONS"))
        inv = self.inv(iid)
        vat = INV.tax_split(30000, 1500, True)
        self.assertEqual(inv["total_p"], 40000)
        self.assertEqual(inv["tax_p"], vat[1])
        self.assertEqual(inv["subtotal_p"], vat[0] + 10000)
        self.assertEqual(inv["tax_name"], "Several rates")
        rows = {r["name"]: r for r in self.rows(iid)}
        self.assertEqual(set(rows), {"VAT", "Exempt"})
        self.assertEqual((rows["Exempt"]["net_p"], rows["Exempt"]["tax_p"], rows["Exempt"]["kind"]), (10000, 0, "exempt"))
        # the money always adds up: net + tax = gross, and the groups add to the invoice
        self.assertEqual(sum(r["gross_p"] for r in rows.values()), inv["total_p"])
        self.assertEqual(sum(r["tax_p"] for r in rows.values()), inv["tax_p"])
        for r in rows.values():
            self.assertEqual(r["net_p"] + r["tax_p"], r["gross_p"])

    def test_tax_added_on_top_per_group(self):
        self.set_default("VAT", 1000, 0)
        reduced = self.rate("Reduced", "rate", "5")
        self.assign("CROWN", reduced)
        iid = self.invoice(self.work("FILL", "CROWN"))
        inv = self.inv(iid)
        self.assertEqual(inv["subtotal_p"], 180000)
        self.assertEqual(inv["tax_p"], 3000 + 7500)               # 10% of 300.00 and 5% of 1500.00
        self.assertEqual(inv["total_p"], 180000 + 10500)

    def test_code_rate_with_no_clinic_default(self):
        reduced = self.rate("Reduced", "rate", "5")
        self.assign("FILL", reduced)
        iid = self.invoice(self.work("FILL", "CONS"))
        inv = self.inv(iid)
        self.assertEqual(inv["tax_name"], "Reduced")
        self.assertEqual(inv["tax_rate_bp"], 500)
        rows = self.rows(iid)
        self.assertEqual([r["name"] for r in rows], ["Reduced"])          # the untaxed consultation has no row
        self.assertEqual(inv["total_p"], 40000)

    def test_zero_rated_is_shown_but_adds_no_tax(self):
        self.set_default()
        zero = self.rate("Zero rated", "zero")
        self.assign("SCALE", zero)
        iid = self.invoice(self.work("SCALE"))
        inv = self.inv(iid)
        self.assertEqual((inv["tax_p"], inv["total_p"]), (0, 20000))
        self.assertEqual(self.rows(iid)[0]["kind"], "zero")

    def test_rounding_is_done_once_per_group(self):
        self.set_default("VAT", 1250, 0)
        a, b = self.work("FILL", "FILL")
        self.sql("UPDATE procedures SET fee_p = 10001 WHERE id IN (?, ?)", a, b)
        iid = self.invoice([a, b])
        # 12.5% of 20002 = 2500.25, rounded once to 2500. Rounding each line would give 1251 + 1251 = 2502.
        self.assertEqual(self.inv(iid)["tax_p"], 2500)

    def test_invoice_keeps_the_rates_it_was_made_with(self):
        self.set_default()
        iid = self.invoice(self.work("FILL"))
        before = dict(self.inv(iid))
        self.set_default("GST", 500, 0)
        self.assertEqual(dict(self.inv(iid)), before)
        self.assertEqual(self.rows(iid)[0]["name"], "VAT")

    def test_a_clinic_cannot_use_or_change_another_clinics_rates(self):
        other = self.rate("Theirs", "rate", "7", clinic=2)
        conn = self.db()
        with self.assertRaises(TX.TaxError):
            TX.set_code_rate(conn, 1, None, self.cid("FILL"), other)
        with self.assertRaises(LookupError):
            TX.toggle_rate(conn, 1, None, other)
        with self.assertRaises(LookupError):
            TX.set_code_rate(conn, 2, None, self.cid("FILL", 1), other)
        conn.close()

    def test_rate_rules(self):
        c = self.clinic()
        conn = self.db()
        for args in (("", "rate", "5"), ("X", "rate", "0"), ("X", "rate", "abc"), ("X", "rate", "101"), ("X", "weird", "5")):
            with self.assertRaises(TX.TaxError, msg=str(args)):
                TX.add_rate(conn, c, None, *args)
        TX.add_rate(conn, c, None, "Reduced", "rate", "5")
        with self.assertRaises(TX.TaxError):
            TX.add_rate(conn, c, None, "Reduced", "rate", "6")
        TX.add_rate(conn, c, None, "Exempt", "exempt", "99")           # a rate typed against an exempt kind is ignored
        self.assertEqual(conn.execute("SELECT rate_bp FROM tax_rates WHERE name = 'Exempt'").fetchone()[0], 0)
        conn.close()

    def test_switched_off_rate_stays_on_old_codes_and_cannot_be_newly_picked(self):
        rid = self.rate("Reduced", "rate", "5")
        self.assign("FILL", rid)
        conn = self.db()
        TX.toggle_rate(conn, 1, None, rid)
        conn.commit()
        with self.assertRaises(TX.TaxError):
            TX.set_code_rate(conn, 1, None, self.cid("CONS"), rid)
        TX.set_code_rate(conn, 1, None, self.cid("FILL"), rid)       # unchanged, so allowed
        conn.close()
        iid = self.invoice(self.work("FILL"))
        self.assertEqual(self.inv(iid)["tax_name"], "Reduced")

    def test_screens(self):
        c = self.login("mgr")
        self.assertNotIn("<th>Tax</th>", self.page(c, "/catalogue"))
        r = self.post(c, "/settings", {"action": "taxrate_add", "rate_name": "Reduced", "rate_kind": "rate", "rate_pct": "5"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("Reduced", self.page(c, "/settings"))
        cat = self.page(c, "/catalogue")
        self.assertIn("<th>Tax</th>", cat)
        rid = self.one("SELECT id FROM tax_rates WHERE name = 'Reduced'")[0]
        code = self.cid("FILL")
        r = self.post(c, "/catalogue", {"action": "update", "id": str(code), "fee": "300", "active": "1", "tax_rate_id": str(rid)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.one("SELECT tax_rate_id FROM procedure_codes WHERE id = ?", code)[0], rid)
        # switching the rate off keeps it selected on the code, and saving the row again does not lose it
        self.post(c, "/settings", {"action": "taxrate_toggle", "id": str(rid)})
        self.assertIn("Reduced (off)", self.page(c, "/catalogue"))
        self.post(c, "/catalogue", {"action": "update", "id": str(code), "fee": "310", "active": "1", "tax_rate_id": str(rid)})
        row = self.one("SELECT tax_rate_id, fee_p FROM procedure_codes WHERE id = ?", code)
        self.assertEqual((row[0], row[1]), (rid, 31000))
        # others cannot reach the settings
        self.assertEqual(self.post(self.login("desk"), "/settings", {"action": "taxrate_toggle", "id": str(rid)}).status_code, 403)

    def test_invoice_page_shows_each_rate(self):
        self.set_default()
        exempt = self.rate("Exempt", "exempt")
        self.assign("CONS", exempt)
        iid = self.invoice(self.work("FILL", "CONS"))
        page = self.page(self.login("mgr"), f"/invoices/{iid}")
        self.assertIn("VAT 15%", page)
        self.assertIn("Exempt on", page)
        self.assertIn("(included in the prices)", page)

    def test_old_invoice_without_a_breakdown_still_shows_its_tax_line(self):
        self.sql("""INSERT INTO invoices(clinic_id, invoice_no, patient_id, issued_on, due_on, subtotal_p, tax_p, total_p, tax_name, tax_rate_bp, tax_inclusive)
                    VALUES (1, 1, 1, '2026-01-02', '2026-01-16', 26087, 3913, 30000, 'VAT', 1500, 1)""")
        iid = self.one("SELECT id FROM invoices")[0]
        page = self.page(self.login("mgr"), f"/invoices/{iid}")
        self.assertIn("VAT 15%", page)




# ----------------------------------------------------------------------------------------------- two-step login
SEED = "12345678901234567890"                       # the secret used in the published RFC 6238 test table, as text
SEED_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


class CodeTests(unittest.TestCase):
    def test_published_test_vectors(self):
        # RFC 6238 appendix B, SHA-1 column, last six digits of the eight-digit values
        for when, want in ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"), (1234567890, "005924"), (2000000000, "279037")):
            self.assertEqual(TF.code_at(SEED_B32, when // 30), want, when)

    def test_window_and_replay(self):
        now = 1_700_000_000
        step = now // 30
        good = TF.code_at(SEED_B32, step)
        self.assertEqual(TF.check(SEED_B32, good, 0, now), step)
        self.assertIsNone(TF.check(SEED_B32, good, step, now))                  # already used
        self.assertEqual(TF.check(SEED_B32, TF.code_at(SEED_B32, step - 1), 0, now), step - 1)        # a little behind
        self.assertEqual(TF.check(SEED_B32, TF.code_at(SEED_B32, step + 1), 0, now), step + 1)        # a little ahead
        self.assertIsNone(TF.check(SEED_B32, TF.code_at(SEED_B32, step - 2), 0, now))                 # too old
        self.assertIsNone(TF.check(SEED_B32, TF.code_at(SEED_B32, step + 2), 0, now))
        for bad in ("", "12345", "1234567", "abcdef", None):
            self.assertIsNone(TF.check(SEED_B32, bad, 0, now))
        self.assertEqual(TF.check(SEED_B32, good[:3] + " " + good[3:], 0, now), step)                # spaces are fine

    def test_setup_link(self):
        link = TF.uri("ABCD", "ama", "Dental OS")
        self.assertTrue(link.startswith("otpauth://totp/Dental%20OS%3Aama?secret=ABCD&issuer=Dental%20OS"))
        self.assertEqual(TF.spaced("ABCDEFGHIJ"), "ABCD EFGH IJ")


class TwoFactor(Ops):
    def secret_of(self, name):
        conn = self.db()
        uid = conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()[0]
        secret, _ = TF._secret_of(conn, "test", uid)
        conn.close()
        return secret

    def fresh_code(self, name, ahead=0):
        """A current code. The 'last used' marker is cleared first so tests can sign in more than once in the same half minute."""
        self.sql("UPDATE users SET totp_last_step = 0 WHERE username = ?", name)
        return TF.code_at(self.secret_of(name), int(time.time() // 30) + ahead)

    def token(self, c, url):
        return re.search(r'name="_csrf" value="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)

    def enable(self, name):
        """Turn two-step login on for a user through the screens. Returns (recovery codes, a signed-in client)."""
        c = self.login(name)
        self.assertEqual(c.get("/account/security/setup").status_code, 200)
        code = TF.code_at(self.secret_of(name), int(time.time() // 30))
        r = self.post(c, "/account/security/setup", {"code": code})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        text = r.get_data(as_text=True)
        return re.findall(r"\b[A-Z2-9]{5}-[A-Z2-9]{5}\b", text), c

    def start_login(self, name, password=PW):
        c = self.app.test_client()
        tok = self.token(c, "/login")
        r = c.post("/login", data={"_csrf": tok, "username": name, "password": password})
        return c, r

    def send_code(self, c, code):
        return c.post("/login/code", data={"_csrf": self.token(c, "/login/code"), "code": code})

    def signed_in(self, c):
        return c.get("/patients").status_code == 200


class TwoFactorTests(TwoFactor):
    def test_setup_shows_a_scannable_picture_and_a_key_and_needs_the_right_code(self):
        c = self.login("mgr")
        page = self.page(c, "/account/security/setup")
        self.assertIn("<svg", page)
        self.assertIn("Cannot scan?", page)
        secret = self.secret_of("mgr")
        self.assertIn(TF.spaced(secret), page)
        r = self.post(c, "/account/security/setup", {"code": "000000"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("That code is not right", r.get_data(as_text=True))
        self.assertEqual(self.one("SELECT totp_enabled FROM users WHERE username = 'mgr'")[0], 0)
        self.assertEqual(self.secret_of("mgr"), secret)                 # reloading does not change the secret under the person's feet

    def test_turning_on_gives_ten_recovery_codes_and_stores_things_safely(self):
        codes, c = self.enable("mgr")
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        row = self.one("SELECT totp_secret, totp_enabled FROM users WHERE username = 'mgr'")
        self.assertEqual(row[1], 1)
        self.assertNotIn(self.secret_of("mgr"), row[0])                  # encrypted at rest
        hashes = [r[0] for r in self.many("SELECT code_hash FROM recovery_codes")]
        self.assertEqual(len(hashes), 10)
        for code in codes:
            self.assertNotIn(code.replace("-", ""), " ".join(hashes))    # recovery codes are only kept as hashes
        self.assertIn("Recovery codes left: <strong>10</strong>", self.page(c, "/account/security"))

    def test_password_alone_does_not_sign_in(self):
        self.enable("mgr")
        c, r = self.start_login("mgr")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login/code"))
        self.assertFalse(self.signed_in(c))
        self.assertEqual(c.get("/patients").status_code, 302)

    def test_login_with_a_current_code(self):
        self.enable("mgr")
        c, _ = self.start_login("mgr")
        r = self.send_code(c, self.fresh_code("mgr"))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.signed_in(c))

    def test_wrong_code_is_refused_and_the_same_code_cannot_be_used_twice(self):
        self.enable("mgr")
        c, _ = self.start_login("mgr")
        r = self.send_code(c, "123456")
        self.assertEqual(r.status_code, 200)
        self.assertIn("That code is not right", r.get_data(as_text=True))
        self.assertFalse(self.signed_in(c))
        code = self.fresh_code("mgr")
        self.assertEqual(self.send_code(c, code).status_code, 302)
        c2, _ = self.start_login("mgr")
        again = self.send_code(c2, code)                                   # a thief who watched the code being typed
        self.assertEqual(again.status_code, 200)
        self.assertFalse(self.signed_in(c2))

    def test_recovery_code_works_once(self):
        codes, _ = self.enable("mgr")
        c, _ = self.start_login("mgr")
        self.assertEqual(self.send_code(c, codes[0].lower()).status_code, 302)       # case does not matter
        self.assertTrue(self.signed_in(c))
        c2, _ = self.start_login("mgr")
        self.assertEqual(self.send_code(c2, codes[0]).status_code, 200)
        self.assertFalse(self.signed_in(c2))
        c3, _ = self.start_login("mgr")
        self.assertEqual(self.send_code(c3, codes[1]).status_code, 302)
        self.assertEqual(self.one("SELECT COUNT(*) FROM recovery_codes WHERE used_at IS NOT NULL")[0], 2)

    def test_too_many_wrong_codes_lock_the_account_for_a_while(self):
        self.enable("mgr")
        c, _ = self.start_login("mgr")
        for _ in range(5):
            self.send_code(c, "000000")
        r = self.send_code(c, self.fresh_code("mgr"))                       # even the right code now waits
        self.assertEqual(r.status_code, 200)
        self.assertIn("Too many wrong codes", r.get_data(as_text=True))
        self.assertFalse(self.signed_in(c))

    def test_the_code_step_expires(self):
        self.enable("mgr")
        c, _ = self.start_login("mgr")
        with c.session_transaction() as s:
            s["pending_at"] = int(time.time()) - 600
        r = c.get("/login/code")
        self.assertEqual(r.status_code, 302)                              # too long since the password: sent back to the start
        self.assertTrue(r.headers["Location"].endswith("/login"))

    def test_login_code_page_needs_a_password_first(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/login/code").status_code, 302)

    def test_audit_trail_tells_the_two_steps_apart(self):
        self.enable("mgr")
        c, _ = self.start_login("mgr")
        self.send_code(c, "111111")
        self.send_code(c, self.fresh_code("mgr"))
        actions = [r[0] for r in self.many("SELECT action FROM audit_log ORDER BY id")]
        for want in ("2fa.enable", "login.password.ok", "login.2fa.failed", "login.ok"):
            self.assertIn(want, actions)

    def test_turning_off_needs_the_password(self):
        _, c = self.enable("mgr")
        self.assertEqual(self.post(c, "/account/security/off", {"password": "wrong"}).status_code, 400)
        self.assertEqual(self.one("SELECT totp_enabled FROM users WHERE username = 'mgr'")[0], 1)
        self.assertEqual(self.post(c, "/account/security/off", {"password": PW}).status_code, 302)
        row = self.one("SELECT totp_enabled, totp_secret FROM users WHERE username = 'mgr'")
        self.assertEqual((row[0], row[1]), (0, None))
        self.assertEqual(self.one("SELECT COUNT(*) FROM recovery_codes")[0], 0)

    def test_new_recovery_codes_need_password_and_a_code_and_replace_the_old(self):
        old, c = self.enable("mgr")
        r = self.post(c, "/account/security/codes", {"password": "wrong", "code": self.fresh_code("mgr")})
        self.assertEqual(r.status_code, 400)
        r = self.post(c, "/account/security/codes", {"password": PW, "code": "000000"})
        self.assertEqual(r.status_code, 400)
        r = self.post(c, "/account/security/codes", {"password": PW, "code": self.fresh_code("mgr")})
        self.assertEqual(r.status_code, 200)
        new = re.findall(r"\b[A-Z2-9]{5}-[A-Z2-9]{5}\b", r.get_data(as_text=True))
        self.assertEqual(len(new), 10)
        self.assertFalse(set(new) & set(old))
        c2, _ = self.start_login("mgr")
        self.assertEqual(self.send_code(c2, old[0]).status_code, 200)       # old codes no longer work
        self.assertFalse(self.signed_in(c2))

    def test_secret_unreadable_with_another_key(self):
        self.enable("mgr")
        conn = self.db()
        uid = conn.execute("SELECT id FROM users WHERE username = 'mgr'").fetchone()[0]
        self.assertIsNone(TF._secret_of(conn, "another key", uid)[0])
        conn.close()

    def test_manager_can_reset_a_colleague_but_not_across_clinics_or_as_a_non_manager(self):
        self.enable("dent")
        m = self.login("mgr")
        did = self.one("SELECT id FROM users WHERE username = 'dent'")[0]
        self.assertIn("Reset two-step", self.page(m, "/staff"))
        r = self.post(m, f"/staff/{did}/reset-2fa")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.one("SELECT totp_enabled FROM users WHERE id = ?", did)[0], 0)
        c, resp = self.start_login("dent")
        self.assertTrue(resp.headers["Location"].endswith("/"))              # no second step any more
        self.assertTrue(self.signed_in(c))
        self.assertEqual(self.post(self.login("desk"), f"/staff/{did}/reset-2fa").status_code, 403)
        other = self.login("mgr2")
        self.assertEqual(self.post(other, f"/staff/{did}/reset-2fa").status_code, 404)

    def test_clinic_can_require_two_step_login(self):
        m = self.login("mgr")
        r = self.post(m, "/staff/require-2fa", {"on": "1"})                  # the manager has not set it up yet
        self.assertEqual(self.one("SELECT require_2fa FROM clinics WHERE id = 1")[0], 0)
        _, m = self.enable("mgr")
        self.post(m, "/staff/require-2fa", {"on": "1"})
        self.assertEqual(self.one("SELECT require_2fa FROM clinics WHERE id = 1")[0], 1)
        d = self.login("dent")                                               # no second step yet, so every page sends them to set it up
        r = d.get("/patients")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/account/security/setup"))
        self.assertEqual(d.get("/account/security/setup").status_code, 200)
        self.assertEqual(self.login("mgr2").get("/patients").status_code, 200)     # another clinic is not affected
        # they cannot switch it off for themselves, and once set up they can work normally
        code = TF.code_at(self.secret_of("dent"), int(time.time() // 30))
        tok = re.search(r'name="_csrf" value="([^"]+)"', d.get("/account/security/setup").get_data(as_text=True)).group(1)
        self.assertEqual(d.post("/account/security/setup", data={"_csrf": tok, "code": code}).status_code, 200)
        self.assertEqual(d.get("/patients").status_code, 200)
        self.assertEqual(self.post(d, "/account/security/off", {"password": PW}).status_code, 400)
        self.assertEqual(self.one("SELECT totp_enabled FROM users WHERE username = 'dent'")[0], 1)

    def test_idle_sign_out(self):
        self.app.config["SESSION_IDLE_MINUTES"] = 30
        c = self.login("mgr")
        self.assertEqual(c.get("/patients").status_code, 200)
        with c.session_transaction() as s:
            s["seen"] = int(time.time()) - 31 * 60
        r = c.get("/patients")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.app.config["SESSION_IDLE_MINUTES"] = 0                          # switched off: never times out
        c = self.login("mgr")
        with c.session_transaction() as s:
            s["seen"] = int(time.time()) - 5 * 3600
        self.assertEqual(c.get("/patients").status_code, 200)

    def test_cli_reset(self):
        self.enable("mgr")
        out = self.app.test_cli_runner().invoke(args=["reset-2fa", "--username", "mgr"])
        self.assertIn("Two-step login is off", out.output)
        self.assertEqual(self.one("SELECT totp_enabled FROM users WHERE username = 'mgr'")[0], 0)
        bad = self.app.test_cli_runner().invoke(args=["reset-2fa", "--username", "nobody"])
        self.assertNotEqual(bad.exit_code, 0)

    def test_api_is_not_touched(self):
        self.enable("mgr")
        self.assertEqual(self.app.test_client().get("/api/v1/clinic").status_code, 401)


# ---------------------------------------------------------------------------------------------------- passwords
class PasswordTests(Ops):
    def test_rules(self):
        for bad in ("short", "password123", "Password1!", "1234567890", "qwertyuiop", "aaaaaaaaaaaa", "abcdefghij", "Welcome2026!",
                    "dentalclinic", "x" * 129):
            self.assertIsNotNone(passwords.check(bad), bad)
        self.assertIn("username", passwords.check("zebra-amamensah-77", "amamensah"))
        self.assertIn("name", passwords.check("kwame-coffee-pot", "kk", "Kwame Boateng"))
        for good in ("correct-horse-battery", "blue pelican drinks tea", "demo-password-123", "T9#vq-lunch-ridge"):
            self.assertIsNone(passwords.check(good, "user", "A B"), good)

    def test_screens_use_the_rules(self):
        c = self.login("mgr")
        r = self.post(c, "/staff", {"username": "newbie", "display_name": "New Person", "role": "assistant", "password": "Password123"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM users WHERE username = 'newbie'")[0], 0)
        self.assertIn("first ones anybody tries", c.get("/staff").get_data(as_text=True))
        self.post(c, "/staff", {"username": "newbie", "display_name": "New Person", "role": "assistant", "password": "tulip-garden-bridge"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM users WHERE username = 'newbie'")[0], 1)
        r = self.post(c, "/account/password", {"current": PW, "new": "qwertyuiop", "again": "qwertyuiop"})
        self.assertIn("first ones anybody tries", r.get_data(as_text=True))
        r = self.post(c, "/account/password", {"current": PW, "new": "tulip-garden-bridge", "again": "tulip-garden-bridge"})
        self.assertEqual(r.status_code, 302)

    def test_cli_create_user_uses_the_rules(self):
        out = self.app.test_cli_runner().invoke(args=["create-user", "--username", "x1", "--name", "X One", "--clinic-id", "1"],
                                                input="password123\npassword123\n")
        self.assertNotEqual(out.exit_code, 0)
        self.assertIn("first ones anybody tries", out.output)


# --------------------------------------------------------------------------------------------------------- QR
try:
    import cv2
    import numpy as np
except ImportError:                                  # the scanner is only a test aid
    cv2 = None


class QrTests(unittest.TestCase):
    def test_block_sizes_fit_every_version(self):
        # data + error-correction codewords must exactly fill the modules that are not fixed patterns
        for ver in range(1, 11):
            fixed = qr._blank_function_map(ver)
            free = sum(1 for row in fixed for v in row if not v)
            ec, blocks = qr._M[ver]
            total = sum(n * (d + ec) for n, d in blocks)
            self.assertEqual(free // 8, total, f"version {ver}")
            self.assertLess(free - total * 8, 8)

    def test_capacity(self):
        self.assertEqual([qr.byte_capacity(v) for v in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)], [14, 26, 42, 62, 84, 106, 122, 152, 180, 213])
        with self.assertRaises(qr.QRError):
            qr.make("x" * 214)

    def test_svg(self):
        out = qr.svg("hello")
        self.assertTrue(out.startswith("<svg"))
        self.assertIn('role="img"', out)

    @staticmethod
    def scan(rows):
        """Read the code back with real scanners. A scanner can fail to find a code that is fine, so two are tried at two sizes."""
        n, quiet = len(rows), 4
        detectors = [cv2.QRCodeDetector()]
        if hasattr(cv2, "QRCodeDetectorAruco"):
            detectors.append(cv2.QRCodeDetectorAruco())
        for scale in (8, 10):
            img = np.full(((n + 2 * quiet) * scale,) * 2, 255, np.uint8)
            for y, row in enumerate(rows):
                for x, v in enumerate(row):
                    if v:
                        img[(y + quiet) * scale:(y + quiet + 1) * scale, (x + quiet) * scale:(x + quiet + 1) * scale] = 0
            for det in detectors:
                got, _, _ = det.detectAndDecode(img)
                if got:
                    return got
        return ""

    @unittest.skipIf(cv2 is None, "no scanner available")
    def test_a_real_scanner_reads_every_size_back(self):
        base = "otpauth://totp/Dental%20OS:abc?secret=JBSWY3DPEHPK3PXP&issuer=Dental%20OS&x="
        for ln in (1, 14, 15, 26, 27, 42, 43, 62, 63, 84, 85, 106, 107, 122, 123, 152, 153, 180, 181, 213):
            text = (base * 4)[:ln]
            self.assertEqual(self.scan(qr.make(text)), text, f"length {ln}")

    @unittest.skipIf(cv2 is None, "no scanner available")
    def test_the_real_setup_link_scans(self):
        for _ in range(5):
            link = TF.uri(TF.new_secret(), "ama.mensah", "Dental OS")
            self.assertEqual(self.scan(qr.make(link)), link)


# ------------------------------------------------------------------------------------------------- data rights
NAME = "Ama Mensah"
PHONE = "+233201112223"


class Priv(Ops):
    """Patient 1 (Ama Mensah, clinic one) gets a little of everything. Patient 2 is a child in the same clinic, 3 is in clinic two."""

    def rich(self, pid=1, clinic=1):
        self.sql("UPDATE patients SET email = 'ama@example.com', address = '12 Oxford Street, Osu' WHERE id = ?", pid)
        self.visit(day=today() - timedelta(days=20), pid=pid, clinic=clinic)
        self.sql("UPDATE appointments SET notes = 'Ama prefers mornings' WHERE patient_id = ?", pid)
        self.done("FILL", today() - timedelta(days=20), pid=pid, clinic=clinic)
        self.sql("INSERT INTO adjustments(clinic_id, patient_id, amount_p, reason, adjusted_on) VALUES (?,?,-1000,'discount',?)", clinic, pid, str(today()))
        self.pay(29000, today() - timedelta(days=20), pid=pid, clinic=clinic)
        self.sql("INSERT INTO medical_history(clinic_id, patient_id, allergies, conditions) VALUES (?,?,'Penicillin','Asthma')", clinic, pid)
        self.sql("INSERT INTO patient_alerts(clinic_id, patient_id, kind, severity, text) VALUES (?,?,'allergy','critical','Penicillin')", clinic, pid)
        self.sql("INSERT INTO tooth_conditions(clinic_id, patient_id, tooth, condition, recorded_on) VALUES (?,?,36,'caries',?)", clinic, pid, str(today()))
        self.sql("INSERT INTO perio_charts(clinic_id, patient_id, charted_on) VALUES (?,?,?)", clinic, pid, str(today()))
        chart = self.one("SELECT MAX(id) FROM perio_charts")[0]
        for site in ("MB", "B", "DB"):
            self.sql("INSERT INTO perio_readings(chart_id, clinic_id, tooth, site, pocket_mm) VALUES (?,?,16,?,3)", chart, clinic, site)
        self.sql("INSERT INTO clinical_notes(clinic_id, patient_id, title, body, status) VALUES (?,?,'Exam','Routine exam, no concerns','signed')", clinic, pid)
        self.sql("INSERT INTO consents(clinic_id, patient_id, title, body, signer_name, signed_at) VALUES (?,?,'Extraction','Wording',?,?)",
                 clinic, pid, NAME, str(today()))
        self.sql("INSERT INTO messages(clinic_id, patient_id, kind, body) VALUES (?,?,'reminder','Hi Ama, see you Tuesday')", clinic, pid)
        self.sql("INSERT INTO treatment_plans(clinic_id, patient_id, title, signer_name) VALUES (?,?,'Option A',?)", clinic, pid, NAME)
        plan = self.one("SELECT MAX(id) FROM treatment_plans")[0]
        self.sql("INSERT INTO plan_items(clinic_id, plan_id, code_id) VALUES (?,?,?)", clinic, plan, self.cid("CROWN", clinic))
        self.sql("INSERT INTO plan_events(clinic_id, plan_id, kind) VALUES (?,?,'created')", clinic, plan)
        self.sql("INSERT INTO payers(clinic_id, name) VALUES (?, 'Insurer')", clinic)
        payer = self.one("SELECT MAX(id) FROM payers")[0]
        self.sql("INSERT INTO patient_cover(clinic_id, patient_id, payer_id, member_no, holder_name) VALUES (?,?,?,'M-123',?)", clinic, pid, payer, NAME)
        self.sql("INSERT INTO labs(clinic_id, name) VALUES (?, 'Lab')", clinic)
        lab = self.one("SELECT MAX(id) FROM labs")[0]
        self.sql("INSERT INTO lab_cases(clinic_id, patient_id, lab_id, kind) VALUES (?,?,?,'Crown')", clinic, pid, lab)
        case = self.one("SELECT MAX(id) FROM lab_cases")[0]
        self.sql("INSERT INTO lab_events(clinic_id, case_id, kind) VALUES (?,?,'created')", clinic, case)
        self.sql("""INSERT INTO booking_requests(clinic_id, first_name, last_name, phone, email, reason, wanted_start, patient_id, note)
                    VALUES (?, 'Ama', 'Mensah', ?, 'ama@example.com', 'Pain', '2026-01-01 09:00', ?, 'Ama called twice')""", clinic, PHONE, pid)
        self.sql("INSERT INTO intake_submissions(clinic_id, patient_id, answers) VALUES (?,?,?)", clinic, pid, json.dumps({"name": NAME, "pain": "yes"}))
        self.sql("INSERT INTO patient_links(clinic_id, patient_id, token_hash, expires_on) VALUES (?,?,'abc','2099-01-01')", clinic, pid)

    def put_file(self, pid=1, clinic=1, name="xray.png", data=b"\x89PNG fake bytes"):
        root = Path(self.app.config["FILES_PATH"]) / str(clinic)
        root.mkdir(parents=True, exist_ok=True)
        stored = f"stored-{pid}-{name}"
        (root / stored).write_bytes(data)
        self.sql("INSERT INTO patient_files(clinic_id, patient_id, kind, original_name, stored_name, mime, size_bytes) VALUES (?,?,'xray',?,?,'image/png',?)",
                 clinic, pid, name, stored, len(data))
        return stored

    def hook_rows(self, pid=1, clinic=1):
        """Queue the kind of messages the system really queues, so we can test they are cleaned."""
        conn = self.db()
        ep, _ = WH.add_endpoint(conn, self.clinic_row(clinic), 1, "https://hooks.example.com/x", [])
        row = conn.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
        WH.emit(conn, clinic, "patient.updated", serialize.patient(row))
        WH.emit(conn, clinic, "payment.received", {"id": 5, "patient_id": pid, "amount_p": 100})
        WH.emit(conn, clinic, "payment.received", {"id": 6, "patient_id": 2, "amount_p": 100})      # someone else
        conn.commit()
        conn.close()

    def mgr(self):
        return self.login("mgr")


class RecordCopyTests(Priv):
    def test_every_table_with_a_person_in_it_is_in_the_copy(self):
        conn = self.db()
        known = set(PV.DIRECT) | set(PV.CHILD) | {"patients"}
        for t in dbmeta.tables(conn):
            if t in known or t in PV.NOT_RECORDS:
                continue
            cols = dbmeta.columns(conn, t)
            points_at = dbmeta.foreign_targets(conn, t) & known
            self.assertNotIn("patient_id", cols, f"{t} has a patient_id but is not in the record copy list")
            self.assertFalse(points_at, f"{t} points at patient data ({points_at}) but is not in the record copy list")
        conn.close()

    def test_copy_has_everything_for_this_person_and_nobody_else(self):
        self.rich(1)
        self.sql("INSERT INTO medical_history(clinic_id, patient_id, allergies) VALUES (1, 2, 'Nuts')")
        self.visit(pid=2)
        self.visit(pid=3, clinic=2)
        self.put_file(1)
        conn = self.db()
        data = PV.record_data(conn, 1, 1)
        for table in ("patients", "appointments", "procedures", "payments", "medical_history", "patient_alerts", "tooth_conditions", "perio_charts",
                      "perio_readings", "clinical_notes", "consents", "messages", "treatment_plans", "plan_items", "plan_events", "patient_cover",
                      "lab_cases", "lab_events", "booking_requests", "intake_submissions", "patient_files", "adjustments"):
            self.assertTrue(data[table], f"{table} is empty")
        self.assertEqual(len(data["perio_readings"]), 3)
        for table, rows in data.items():
            for r in rows:
                if "patient_id" in r:
                    self.assertEqual(r["patient_id"], 1, table)
                if "clinic_id" in r:
                    self.assertEqual(r["clinic_id"], 1, table)
        self.assertEqual([r["id"] for r in data["patients"]], [1])
        self.assertNotIn("stored_name", data["patient_files"][0])           # the private disk name is not given out
        self.assertEqual(data["procedures"][0]["procedure"], "Filling, tooth-coloured")
        with self.assertRaises(LookupError):
            PV.record_data(conn, 1, 3)                                      # another clinic's patient
        conn.close()

    def test_zip_has_the_data_and_the_files(self):
        self.rich(1)
        self.put_file(1, name="xray one.png", data=b"IMAGE-BYTES-1")
        self.sql("INSERT INTO patient_files(clinic_id, patient_id, kind, original_name, stored_name, mime, size_bytes) VALUES (1,1,'xray','gone.png','missing-file','image/png',3)")
        c = self.mgr()
        r = self.post(c, "/patients/1/record-copy.zip")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/zip")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        z = zipfile.ZipFile(io.BytesIO(r.data))
        names = z.namelist()
        self.assertIn("README.txt", names)
        self.assertIn("record.json", names)
        record = json.loads(z.read("record.json"))
        self.assertEqual(record["patients"][0]["first_name"], "Ama")
        self.assertEqual(len(record["perio_readings"]), 3)
        fname = [n for n in names if n.startswith("files/")]
        self.assertEqual(len(fname), 1)
        self.assertEqual(z.read(fname[0]), b"IMAGE-BYTES-1")
        self.assertIn("gone.png", z.read("NOT_INCLUDED.txt").decode())        # a missing file is said, not hidden
        self.assertIn("record.copy.download", [a[0] for a in self.many("SELECT action FROM audit_log")])

    def test_printable_copy(self):
        self.rich(1)
        c = self.mgr()
        page = self.page(c, "/patients/1/record-copy")
        for want in ("Ama Mensah", "12 Oxford Street", "Penicillin", "Filling, tooth-coloured", "Routine exam", "Receipt"):
            self.assertIn(want, page)
        self.assertIn("record.copy.view", [a[0] for a in self.many("SELECT action FROM audit_log")])

    def test_only_a_manager_of_this_clinic(self):
        self.rich(1)
        urls = ["/privacy", "/privacy/requests", "/privacy/incidents", "/privacy/retention", "/patients/1/privacy", "/patients/1/record-copy"]
        for who in ("dent", "asst", "desk"):
            c = self.login(who)
            for u in urls:
                self.assertEqual(c.get(u).status_code, 403, f"{who} {u}")
            self.assertEqual(self.post(c, "/patients/1/record-copy.zip").status_code, 403)
            self.assertEqual(self.post(c, "/patients/1/anonymise", {"reason": "because", "confirm": "ANONYMISE"}).status_code, 403)
        m = self.mgr()
        for u in urls:
            self.assertEqual(m.get(u).status_code, 200, u)
        for u in ("/patients/3/privacy", "/patients/3/record-copy"):
            self.assertEqual(m.get(u).status_code, 404, u)
        self.assertEqual(self.post(m, "/patients/3/record-copy.zip").status_code, 404)
        self.assertEqual(self.post(m, "/patients/3/anonymise", {"reason": "because", "confirm": "ANONYMISE"}).status_code, 404)

    def test_who_has_looked(self):
        c = self.mgr()
        c.get("/patients/1")
        self.login("dent").get("/patients/1")
        self.login("dent").get("/patients/2")
        page = self.page(c, "/patients/1/privacy")
        self.assertIn("patient.view", page)
        self.assertIn("Dent", page)
        log = PV.access_log(self.db(), 1, 1)
        self.assertTrue(all(r["object_id"] == 1 for r in log))


class AnonymiseTests(Priv):
    def anon(self, pid=1, **kw):
        conn = self.db()
        try:
            PV.anonymise(conn, self.clinic_row(), 1, pid, kw.get("reason", "Asked to be erased"), kw.get("confirm", "ANONYMISE"), kw.get("request_id"))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def settle(self):
        """Patient 1 owes nothing: one filling paid in full."""
        self.assertEqual(self.balance(1), 0)

    def test_what_blocks_it(self):
        self.rich(1)
        self.settle()
        with self.assertRaises(PV.PrivacyError) as e:
            self.anon(reason="x")
        self.assertIn("why", str(e.exception).lower())
        with self.assertRaises(PV.PrivacyError) as e:
            self.anon(confirm="yes")
        self.assertIn("ANONYMISE", str(e.exception))
        self.done("CONS", today(), pid=1)                                            # now they owe 100.00
        with self.assertRaises(PV.PrivacyError) as e:
            self.anon()
        self.assertIn("not settled", str(e.exception))
        self.pay(10000, today())
        self.sql("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status) VALUES (1,1,1,1,?,30,'scheduled')""",
                 str(today() + timedelta(days=3)) + " 09:00")
        with self.assertRaises(PV.PrivacyError) as e:
            self.anon()
        self.assertIn("appointments", str(e.exception))
        self.sql("UPDATE appointments SET status = 'broken' WHERE status = 'scheduled'")
        self.sql("UPDATE patients SET guarantor_id = 1 WHERE id = 2")
        with self.assertRaises(PV.PrivacyError) as e:
            self.anon()
        self.assertIn("family head", str(e.exception))
        self.sql("UPDATE patients SET guarantor_id = NULL WHERE id = 2")
        self.sql("INSERT INTO claims(clinic_id, claim_no, patient_id, payer_id, status) VALUES (1, 1, 1, 1, 'submitted')")
        with self.assertRaises(PV.PrivacyError) as e:
            self.anon()
        self.assertIn("claim", str(e.exception))
        self.sql("UPDATE claims SET status = 'paid'")
        self.anon()                                                                  # nothing in the way now
        with self.assertRaises(PV.PrivacyError) as e:
            self.anon()
        self.assertIn("already", str(e.exception))

    def test_identity_goes_and_the_history_stays(self):
        self.rich(1)
        self.put_file(1)
        self.hook_rows(1)
        before = {t: self.one(f"SELECT COUNT(*) FROM {t} WHERE patient_id = 1")[0] for t in ("procedures", "payments", "appointments", "clinical_notes")}
        money = self.balance(1)
        self.anon()
        p = self.one("SELECT * FROM patients WHERE id = 1")
        self.assertEqual((p["first_name"], p["last_name"], p["phone"], p["email"], p["address"], p["notes"]), ("Anonymised", "#1", "", None, None, None))
        self.assertEqual((p["dob"], p["status"], p["reminders_ok"], p["offers_ok"]), ("1990-01-01", "archived", 0, 0))
        self.assertTrue(p["anonymised_on"])
        after = {t: self.one(f"SELECT COUNT(*) FROM {t} WHERE patient_id = 1")[0] for t in before}
        self.assertEqual(before, after)                                              # treatment, notes and money are kept
        self.assertEqual(self.balance(1), money)
        self.assertEqual(self.one("SELECT body FROM messages")[0], "[removed]")
        self.assertIsNone(self.one("SELECT notes FROM appointments WHERE patient_id = 1")[0])
        self.assertEqual(self.one("SELECT signer_name FROM consents")[0], "Anonymised")
        self.assertIsNone(self.one("SELECT holder_name FROM patient_cover")[0])
        self.assertEqual(self.one("SELECT archived FROM patient_files")[0], 1)
        self.assertEqual(self.one("SELECT revoked FROM patient_links")[0], 1)
        self.assertEqual(self.one("SELECT first_name FROM booking_requests")[0], "Anonymised")
        self.assertEqual(self.one("SELECT answers FROM intake_submissions")[0], "{}")
        self.assertEqual(self.one("SELECT first_name FROM patients WHERE id = 2")[0], "Kofi")      # nobody else was touched

    def scan(self):
        """Every place in the database where the person's name, phone, email or address can still be found."""
        conn = self.db()
        found = []
        for t in dbmeta.tables(conn):
            cols = [name for name, kind in dbmeta.column_types(conn, t).items() if kind in ("TEXT", "")]
            for col in cols:
                for (v,) in conn.execute(f'SELECT "{col}" FROM {t} WHERE "{col}" IS NOT NULL').fetchall():
                    for needle in ("Mensah", "ama@example.com", "Oxford Street", "233201112223", "0201112223"):
                        if needle in str(v):
                            found.append((t, col, needle))
                    if re.search(r"\bAma\b", str(v)):
                        found.append((t, col, "Ama"))
        conn.close()
        return found

    def test_no_trace_of_the_name_is_left_anywhere_in_the_database(self):
        self.rich(1)
        self.hook_rows(1)
        self.sql("INSERT INTO data_requests(clinic_id, patient_id, requester, kind, received_on, due_on, details) VALUES (1,1,?, 'erasure','2026-01-01','2026-01-31','Ama wrote in')", NAME)
        before = {t for t, _, _ in self.scan()}
        for t in ("patients", "appointments", "messages", "consents", "patient_cover", "treatment_plans", "booking_requests", "intake_submissions",
                  "data_requests", "webhook_deliveries"):
            self.assertIn(t, before, f"the test data should have put the name in {t}")       # so the scan is shown to be able to find it
        self.anon()
        # Free text that clinicians wrote (notes, treatment notes) is kept on purpose and can mention a person. We did not put the
        # name there, so nothing at all should turn up.
        self.assertEqual(self.scan(), [])

    def test_waiting_webhook_messages_for_that_person_are_cancelled_and_emptied(self):
        self.rich(1)
        self.hook_rows(1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM webhook_deliveries WHERE status = 'pending'")[0], 3)
        self.anon()
        rows = self.many("SELECT event, status, payload FROM webhook_deliveries ORDER BY id")
        mine = [r for r in rows if r["payload"] == "{}"]
        self.assertEqual(len(mine), 2)                                               # the old patient.updated and the payment for patient 1
        self.assertTrue(all(r["status"] == "dead" for r in mine))
        other = [r for r in rows if '"patient_id":2' in r["payload"]]
        self.assertEqual(len(other), 1)
        self.assertEqual(other[0]["status"], "pending")                              # someone else's message is untouched
        news = [r for r in rows if r["event"] == "patient.updated" and r["payload"] != "{}"]
        self.assertEqual(len(news), 1)                                               # the receiver is told the person is now anonymised
        self.assertIn("Anonymised", news[0]["payload"])
        self.assertNotIn("Mensah", news[0]["payload"])

    def test_audit_entry_names_nobody(self):
        self.rich(1)
        self.anon(reason="Erasure request handled")
        row = self.one("SELECT * FROM audit_log WHERE action = 'patient.anonymise'")
        self.assertEqual((row["object_type"], row["object_id"], row["detail"]), ("patient", 1, "Erasure request handled"))

    def test_closing_a_linked_request(self):
        self.rich(1)
        c = self.mgr()
        conn = self.db()
        rid = PV.add_request(conn, self.clinic_row(), 1, 1, "", "erasure")
        conn.commit()
        conn.close()
        with self.assertRaises(PV.PrivacyError) as e:
            self.anon(request_id=rid)                                                # identity not yet checked
        self.assertIn("who is asking", str(e.exception))
        self.assertEqual(self.one("SELECT first_name FROM patients WHERE id = 1")[0], "Ama")      # nothing changed
        self.sql("UPDATE data_requests SET identity_checked = 1 WHERE id = ?", rid)
        self.anon(request_id=rid)
        row = self.one("SELECT * FROM data_requests WHERE id = ?", rid)
        self.assertEqual((row["status"], row["requester"]), ("done", "Anonymised"))
        self.assertIn("anonymised", row["outcome"].lower())

    def test_the_screen(self):
        self.rich(1)
        c = self.mgr()
        page = self.page(c, "/patients/1/privacy")
        self.assertIn("Type ANONYMISE to confirm", page)
        r = self.post(c, "/patients/1/anonymise", {"reason": "Asked to be erased", "confirm": "no"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.one("SELECT first_name FROM patients WHERE id = 1")[0], "Ama")
        r = self.post(c, "/patients/1/anonymise", {"reason": "Asked to be erased", "confirm": "anonymise"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.one("SELECT first_name FROM patients WHERE id = 1")[0], "Anonymised")
        self.assertIn("Anonymised", self.page(c, "/patients/1"))
        self.assertIn("Identity details were removed", self.page(c, "/patients/1/privacy"))


class RequestRegisterTests(Priv):
    def add(self, **kw):
        conn = self.db()
        args = dict(patient_id=1, requester="", kind="access")
        args.update(kw)
        rid = PV.add_request(conn, self.clinic_row(), 1, **args)
        conn.commit()
        conn.close()
        return rid

    def test_defaults_and_validation(self):
        rid = self.add()
        r = self.one("SELECT * FROM data_requests WHERE id = ?", rid)
        self.assertEqual(r["requester"], NAME)
        self.assertEqual(r["status"], "open")
        self.assertEqual((PV.parse_date(r["due_on"]) - PV.parse_date(r["received_on"])).days, 30) if hasattr(PV, "parse_date") else None
        conn = self.db()
        c1 = self.clinic_row()
        for kw in (dict(kind="nonsense"), dict(patient_id=None, requester=""), dict(patient_id=99), dict(patient_id=3),
                   dict(received_on="2026-02-10", due_on="2026-02-01"), dict(received_on="10/02/2026")):
            args = dict(patient_id=1, requester="", kind="access")
            args.update(kw)
            with self.assertRaises(PV.PrivacyError, msg=str(kw)):
                PV.add_request(conn, c1, 1, **args)
        rid2 = PV.add_request(conn, c1, 1, None, "A relative", "correction", "2026-03-01", "2026-03-10", "Wrong date of birth")
        self.assertEqual(conn.execute("SELECT due_on FROM data_requests WHERE id = ?", (rid2,)).fetchone()[0], "2026-03-10")
        conn.close()

    def test_overdue_and_order(self):
        late = self.add(received_on=str(today() - timedelta(days=40)))
        soon = self.add(kind="correction")
        done = self.add(kind="other")
        conn = self.db()
        PV.close_request(conn, self.clinic_row(), 1, done, "done", "Nothing needed")
        conn.commit()
        rows = PV.list_requests(conn, self.clinic_row())
        conn.close()
        self.assertEqual([r["id"] for r in rows], [late, soon, done])               # open ones first, most urgent first
        self.assertTrue(rows[0]["overdue"])
        self.assertLess(rows[0]["days_left"], 0)
        self.assertFalse(rows[1]["overdue"])
        self.assertEqual(rows[1]["days_left"], 30)
        self.assertIsNone(rows[2]["days_left"])

    def test_closing_rules(self):
        rid = self.add(kind="access")
        conn = self.db()
        c = self.clinic_row()
        with self.assertRaises(PV.PrivacyError) as e:
            PV.close_request(conn, c, 1, rid, "done", "Sent the copy")
        self.assertIn("who is asking", str(e.exception))
        PV.set_identity_checked(conn, c, 1, rid)
        with self.assertRaises(PV.PrivacyError):
            PV.close_request(conn, c, 1, rid, "done", "no")                         # too short to mean anything
        with self.assertRaises(PV.PrivacyError):
            PV.close_request(conn, c, 1, rid, "maybe", "Sent the copy")
        PV.close_request(conn, c, 1, rid, "done", "Sent the copy")
        with self.assertRaises(PV.PrivacyError):
            PV.close_request(conn, c, 1, rid, "done", "Sent the copy again")
        row = conn.execute("SELECT * FROM data_requests WHERE id = ?", (rid,)).fetchone()
        self.assertEqual((row["status"], row["handled_by"], row["closed_on"]), ("done", 1, str(today())))
        PV.reopen_request(conn, c, 1, rid)
        with self.assertRaises(PV.PrivacyError):
            PV.reopen_request(conn, c, 1, rid)
        self.assertEqual(conn.execute("SELECT status FROM data_requests WHERE id = ?", (rid,)).fetchone()[0], "open")
        other = PV.add_request(conn, c, 1, None, "A visitor", "objection")
        PV.close_request(conn, c, 1, other, "refused", "No data held about this person")          # no identity step for a refusal-type request
        conn.commit()
        conn.close()

    def test_other_clinics_cannot_see_or_change_them(self):
        rid = self.add()
        c = self.login("mgr2")
        self.assertEqual(c.get(f"/privacy/requests/{rid}").status_code, 404)
        self.assertEqual(self.post(c, f"/privacy/requests/{rid}", {"action": "reopen"}).status_code, 404)
        self.assertNotIn(NAME, self.page(c, "/privacy/requests"))

    def test_screens(self):
        c = self.mgr()
        r = self.post(c, "/privacy/requests", {"requester": "A. Visitor", "kind": "access", "details": "Wants a copy", "identity_checked": "1"})
        self.assertEqual(r.status_code, 302)
        rid = int(r.headers["Location"].rsplit("/", 1)[1])
        page = self.page(c, f"/privacy/requests/{rid}")
        self.assertIn("A. Visitor", page)
        self.assertIn("Wants a copy", page)
        r = self.post(c, f"/privacy/requests/{rid}", {"action": "done", "outcome": "ok"})
        self.assertIn("Write what was done", self.page(c, f"/privacy/requests/{rid}"))
        self.post(c, f"/privacy/requests/{rid}", {"action": "done", "outcome": "Copy handed over in person"})
        self.assertIn("Copy handed over in person", self.page(c, f"/privacy/requests/{rid}"))
        self.assertEqual(self.post(c, f"/privacy/requests/{rid}", {"action": "whatever"}).status_code, 400)
        self.assertIn("A. Visitor", self.page(c, "/privacy/requests"))
        self.assertIn("Record a request from this person", self.page(c, "/patients/1/privacy"))


class IncidentTests(Priv):
    def add(self, hours_ago=1, risk="high", **kw):
        conn = self.db()
        found = (now_local("Africa/Accra") - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")
        iid = PV.add_incident(conn, self.clinic_row(), 1, kw.get("title", "Phone lost"), kw.get("what", "A phone with the daily list was lost"),
                              found_at=found, risk=risk)
        conn.commit()
        conn.close()
        return iid

    def view(self, iid):
        conn = self.db()
        row = PV.incident_view(self.clinic_row(), PV.get_incident(conn, 1, iid))
        conn.close()
        return row

    def test_validation(self):
        conn = self.db()
        c = self.clinic_row()
        for kw in (dict(title="", what_happened="Something happened here"), dict(title="T", what_happened="short"),
                   dict(title="T", what_happened="Something happened here", risk="extreme"),
                   dict(title="T", what_happened="Something happened here", found_at="tomorrow"),
                   dict(title="T", what_happened="Something happened here", found_at="2099-01-01 10:00"),
                   dict(title="T", what_happened="Something happened here", occurred_on="last week"),
                   dict(title="T", what_happened="Something happened here", people_affected="many"),
                   dict(title="T", what_happened="Something happened here", people_affected="-2")):
            with self.assertRaises(PV.PrivacyError, msg=str(kw)):
                PV.add_incident(conn, c, 1, **kw)
        conn.close()

    def test_the_reporting_clock(self):
        fresh = self.view(self.add(hours_ago=2, risk="high"))
        self.assertTrue(fresh["clock_running"])
        self.assertFalse(fresh["clock_late"])
        self.assertAlmostEqual(fresh["hours_left"], 70, delta=1)
        late = self.view(self.add(hours_ago=80, risk="medium"))
        self.assertTrue(late["clock_late"])
        low = self.view(self.add(hours_ago=80, risk="low"))
        self.assertFalse(low["clock_running"])                                       # low risk does not run the clock
        self.assertFalse(low["clock_late"])
        self.assertEqual(PV.deadline("2026-10-24 09:30"), "2026-10-27 09:30")

    def test_telling_the_regulator_stops_the_clock_and_closing_needs_actions(self):
        iid = self.add(hours_ago=80, risk="high")
        conn = self.db()
        c = self.clinic_row()
        with self.assertRaises(PV.PrivacyError) as e:
            PV.close_incident(conn, c, 1, iid)
        self.assertIn("what was done", str(e.exception))
        PV.update_incident(conn, c, 1, iid, "high", "12", True, "", True, "2026-10-26", "Changed passwords; told the regulator")
        conn.commit()
        row = PV.incident_view(c, PV.get_incident(conn, 1, iid))
        self.assertFalse(row["clock_late"])
        self.assertEqual((row["people_affected"], row["regulator_told"], row["regulator_on"], row["patients_on"]), (12, 1, str(today()), "2026-10-26"))
        PV.close_incident(conn, c, 1, iid)
        with self.assertRaises(PV.PrivacyError):
            PV.update_incident(conn, c, 1, iid, "low", "", False, "", False, "", "x")       # closed: reopen first
        PV.close_incident(conn, c, 1, iid, closing=False)
        conn.commit()
        conn.close()
        self.assertEqual(self.one("SELECT status FROM incidents WHERE id = ?", iid)[0], "open")

    def test_the_managers_today_screen_shows_open_privacy_work(self):
        c = self.mgr()
        self.assertNotIn("data request", self.page(c, "/"))
        self.add(hours_ago=90, risk="high")
        conn = self.db()
        PV.add_request(conn, self.clinic_row(), 1, 1, "", "access", received_on=str(today() - timedelta(days=40)))
        conn.commit()
        conn.close()
        page = self.page(c, "/")
        self.assertIn("1 data request open", page)
        self.assertIn("1 late", page)
        self.assertIn("past the reporting clock", page)
        self.assertNotIn("data request", self.page(self.login("desk"), "/"))           # not for other roles

    def test_counts_and_screens(self):
        iid = self.add(hours_ago=90, risk="high")
        self.assertEqual(PV.counts(self.db(), self.clinic_row()), {"requests_open": 0, "requests_late": 0, "incidents_open": 1, "incidents_late": 1})
        c = self.mgr()
        self.assertIn("Past the 72 hours", self.page(c, "/privacy/incidents"))
        page = self.page(c, f"/privacy/incidents/{iid}")
        self.assertIn("regulator is not marked as told", page)
        r = self.post(c, f"/privacy/incidents/{iid}", {"action": "update", "risk": "high", "regulator_told": "1", "actions": "Reported and phone wiped"})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("regulator is not marked as told", self.page(c, f"/privacy/incidents/{iid}"))
        self.post(c, f"/privacy/incidents/{iid}", {"action": "close"})
        self.assertEqual(self.one("SELECT status FROM incidents WHERE id = ?", iid)[0], "closed")
        r = self.post(c, "/privacy/incidents", {"title": "Wrong chart sent", "what_happened": "A statement went to the wrong number", "risk": "low"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("Wrong chart sent", self.page(c, "/privacy/incidents"))
        other = self.login("mgr2")
        self.assertEqual(other.get(f"/privacy/incidents/{iid}").status_code, 404)
        self.assertEqual(self.post(other, f"/privacy/incidents/{iid}", {"action": "reopen"}).status_code, 404)


class RetentionAndCleaningTests(Priv):
    def test_retention_review(self):
        self.sql("UPDATE patients SET created_at = ? WHERE id IN (1, 2)", str(today() - timedelta(days=4200)) + " 09:00:00")       # about 11.5 years
        self.done("FILL", today() - timedelta(days=4000), pid=1)
        self.pay(30000, today() - timedelta(days=4000), pid=1)
        self.done("FILL", today() - timedelta(days=4100), pid=2)                      # patient 2 still owes
        self.visit(day=today() - timedelta(days=5), pid=2)                            # ...and was seen recently, so is not listed
        conn = self.db()
        rows, cutoff = PV.retention_candidates(conn, self.clinic_row())
        conn.close()
        self.assertEqual([r["id"] for r in rows], [1])
        self.assertTrue(rows[0]["ready"])
        self.sql("UPDATE appointments SET status = 'broken' WHERE patient_id = 2")
        self.sql("UPDATE appointments SET start_at = ? WHERE patient_id = 2", str(today() - timedelta(days=4100)) + " 09:00")
        conn = self.db()
        rows, _ = PV.retention_candidates(conn, self.clinic_row())
        conn.close()
        by = {r["id"]: r for r in rows}
        self.assertEqual(set(by), {1, 2})
        self.assertFalse(by[2]["ready"])                                              # owes money: not ready to remove
        self.assertEqual(by[2]["balance"], 30000)

    def test_setting_and_screen(self):
        c = self.mgr()
        self.assertIn("10", self.page(c, "/privacy/retention"))
        for bad in ("0", "61", "abc", ""):
            self.post(c, "/privacy/retention", {"years": bad})
            self.assertEqual(self.one("SELECT retention_years FROM clinics WHERE id = 1")[0], 10, bad)
        self.post(c, "/privacy/retention", {"years": "7"})
        self.assertEqual(self.one("SELECT retention_years FROM clinics WHERE id = 1")[0], 7)
        self.assertEqual(self.one("SELECT retention_years FROM clinics WHERE id = 2")[0], 10)

    def test_anonymised_people_are_not_listed_again(self):
        self.sql("UPDATE patients SET created_at = '2001-01-01 00:00:00', anonymised_on = '2026-01-01' WHERE id = 1")
        conn = self.db()
        rows, _ = PV.retention_candidates(conn, self.clinic_row())
        conn.close()
        self.assertNotIn(1, [r["id"] for r in rows])

    def test_a_finished_import_keeps_no_names(self):
        c = self.mgr()
        conn = self.db()
        bid = dataio.stage(conn, self.clinic_row(), 1, "first_name,last_name,phone\nJane,Doe,0244000111\n,,\nKofi,Young,0201112224\n")
        conn.commit()
        conn.close()
        self.assertIn("Jane", self.one("SELECT payload FROM import_batches WHERE id = ?", bid)[0])
        self.assertIn("Jane", self.page(c, f"/tools/import/{bid}"))                   # the preview still shows the rows
        r = self.post(c, f"/tools/import/{bid}/confirm")
        self.assertEqual(r.status_code, 302)
        payload = self.one("SELECT payload FROM import_batches WHERE id = ?", bid)[0]
        self.assertNotIn("Jane", payload)
        self.assertNotIn("0244000111", payload)
        page = self.page(c, f"/tools/import/{bid}")
        self.assertIn("This import is done", page)
        self.assertIn("ready to add", page)
        conn = self.db()
        bid2 = dataio.stage(conn, self.clinic_row(), 1, "first_name,last_name,phone\nTina,Test,0244000999\n")
        conn.commit()
        conn.close()
        self.post(c, f"/tools/import/{bid2}/cancel")
        self.assertNotIn("Tina", self.one("SELECT payload FROM import_batches WHERE id = ?", bid2)[0])

    def test_a_preview_nobody_confirmed_is_dropped(self):
        conn = self.db()
        old = dataio.stage(conn, self.clinic_row(), 1, "first_name,last_name,phone\nOld,Preview,0244000333\n")
        conn.execute("UPDATE import_batches SET created_at = datetime('now', '-3 days') WHERE id = ?", (old,))
        conn.commit()
        dataio.stage(conn, self.clinic_row(), 1, "first_name,last_name,phone\nNew,Preview,0244000444\n")
        conn.commit()
        row = conn.execute("SELECT status, payload FROM import_batches WHERE id = ?", (old,)).fetchone()
        conn.close()
        self.assertEqual(row["status"], "cancelled")
        self.assertNotIn("Old", row["payload"])

    def test_old_dead_webhook_messages_are_cleared(self):
        self.hook_rows(1)
        self.sql("UPDATE webhook_deliveries SET status = 'dead', created_at = datetime('now', '-61 days') WHERE id = 1")
        self.sql("UPDATE webhook_deliveries SET status = 'dead', created_at = datetime('now', '-10 days') WHERE id = 2")
        conn = self.db()
        WH.deliver_due(conn, post=lambda *a, **k: 500, limit=0)
        conn.commit()
        ids = [r[0] for r in conn.execute("SELECT id FROM webhook_deliveries ORDER BY id")]
        conn.close()
        self.assertEqual(ids, [2, 3])


class UpgradeNineTests(Priv):
    def test_an_older_database_gets_the_new_pieces(self):
        import tempfile
        from dental_os import db as dbmod
        path = tempfile.mkdtemp() + "/old.sqlite"
        conn = dbmod.connect(path)
        dbmod.init_schema(conn)
        for t in ("tax_rates", "invoice_tax", "recovery_codes", "data_requests", "incidents"):
            dbmeta.drop_table(conn, t)
        for t, col in (("users", "totp_secret"), ("users", "totp_enabled"), ("users", "totp_last_step"), ("clinics", "require_2fa"),
                       ("clinics", "retention_years"), ("patients", "anonymised_on"), ("procedure_codes", "tax_rate_id"),
                       ("invoice_lines", "tax_name"), ("invoice_lines", "tax_rate_bp")):
            conn.execute(f"ALTER TABLE {t} DROP COLUMN {col}")
        conn.commit()
        dbmod.init_schema(conn)
        for t, col in (("users", "totp_enabled"), ("clinics", "require_2fa"), ("clinics", "retention_years"), ("patients", "anonymised_on"),
                       ("procedure_codes", "tax_rate_id"), ("invoice_lines", "tax_rate_bp")):
            self.assertIn(col, dbmeta.columns(conn, t), f"{t}.{col}")
        for t in ("tax_rates", "invoice_tax", "recovery_codes", "data_requests", "incidents"):
            self.assertTrue(dbmeta.has_table(conn, t), t)
        conn.close()

    def test_every_new_screen_opens(self):
        self.rich(1)
        c = self.mgr()
        for url in ("/privacy", "/privacy/requests", "/privacy/incidents", "/privacy/retention", "/patients/1/privacy", "/patients/1/record-copy",
                    "/account/security", "/account/security/setup", "/staff", "/settings", "/catalogue", "/tools", "/patients/1"):
            self.page(c, url)


# ===================================================================== procedure packs and the price list
class CatalogueTests(Tax):
    def mgr(self):
        return self.login("mgr")

    def post_file(self, c, action, text, update=False):
        data = {"_csrf": self.tok(c), "action": action, "file": (io.BytesIO(text.encode("utf-8")), "prices.csv")}
        if update:
            data["update_existing"] = "1"
        return c.post("/catalogue", data=data, content_type="multipart/form-data", follow_redirects=True)

    def codes(self, clinic=1):
        return {r["code"]: r for r in self.many("SELECT * FROM procedure_codes WHERE clinic_id = ?", clinic)}

    def test_packs_are_clean(self):
        seen = {}
        for key in CAT.PACKS:
            for code, name, group in CAT.pack_codes(key):
                self.assertRegex(code, CAT.CODE_RE.pattern, code)
                self.assertLessEqual(len(name), 80)
                self.assertLessEqual(len(group), 40)
                self.assertNotRegex(code, r"^D\d{4}$", "CDT codes are licensed")
                self.assertNotIn(code, seen, f"{code} is in {key} and {seen.get(code)}")
                seen[code] = key
                self.assertNotIn("knight", (code + name + group).lower())

    def test_adding_a_pack_adds_switched_off_codes_once(self):
        c = self.mgr()
        r = self.post(c, "/catalogue", {"action": "pack", "pack": "children"}, follow_redirects=True)
        self.assertIn("Added 7 procedures", r.get_data(as_text=True))
        have = self.codes()
        self.assertEqual(have["PAED-EXAM"]["active"], 0)
        self.assertEqual(have["PAED-EXAM"]["fee_p"], 0)
        self.assertEqual(have["FILL"]["fee_p"], 30000)                      # what the clinic had is untouched
        r = self.post(c, "/catalogue", {"action": "pack", "pack": "children"}, follow_redirects=True)
        self.assertIn("already have every code", r.get_data(as_text=True))
        self.assertEqual(self.one("SELECT COUNT(*) FROM procedure_codes WHERE code = 'PAED-EXAM'")[0], 1)

    def test_a_pack_never_overwrites_a_clinic_code(self):
        self.sql("UPDATE procedure_codes SET name = 'My own name', fee_p = 5000, active = 1 WHERE clinic_id = 1 AND code = 'FILL'")
        self.sql("DELETE FROM procedure_codes WHERE clinic_id = 1 AND code = 'EXT'")
        conn = self.db()
        self.assertEqual(CAT.add_pack(conn, 1, None, "core"), 1)
        conn.commit()
        conn.close()
        row = self.codes()["FILL"]
        self.assertEqual((row["name"], row["fee_p"], row["active"]), ("My own name", 5000, 1))

    def test_unknown_pack_is_404_and_other_clinic_unaffected(self):
        c = self.mgr()
        self.assertEqual(self.post(c, "/catalogue", {"action": "pack", "pack": "nope"}).status_code, 404)
        self.post(c, "/catalogue", {"action": "pack", "pack": "ortho"})
        self.assertNotIn("ALIGNER", self.codes(2))

    def test_staff_cannot_use_the_catalogue(self):
        d = self.login("dent")
        self.assertEqual(d.get("/catalogue").status_code, 403)
        self.assertEqual(d.get("/catalogue/price-list.csv").status_code, 403)
        self.assertEqual(self.post(d, "/catalogue", {"action": "pack", "pack": "ortho"}).status_code, 403)

    def test_export_has_every_code_and_round_trips_without_change(self):
        self.rate("Reduced", "rate", "5")
        self.assign("CROWN", self.one("SELECT id FROM tax_rates WHERE name = 'Reduced'")[0])
        c = self.mgr()
        r = c.get("/catalogue/price-list.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["Content-Type"])
        text = r.get_data(as_text=True)
        self.assertEqual(len(text.strip().splitlines()) - 1, len(self.codes()))
        self.assertIn("FILL,", text)
        self.assertIn("300.00", text)
        before = self.many("SELECT * FROM procedure_codes WHERE clinic_id = 1 ORDER BY id")
        conn = self.db()
        res = CAT.import_csv(conn, self.clinic_row(1), None, text, update_existing=True)
        conn.commit()
        conn.close()
        self.assertEqual((res["added"], res["updated"], res["errors"]), (0, 0, []))
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in self.many("SELECT * FROM procedure_codes WHERE clinic_id = 1 ORDER BY id")])

    def test_import_adds_new_and_leaves_existing_unless_asked(self):
        text = "code,name,group,price,in_use\nFILL,Changed,Restorative,999,yes\nNEWONE,A new thing,Other,45.50,yes\n"
        c = self.mgr()
        r = self.post_file(c, "pricelist_apply", text)
        self.assertIn("1 new, 0 changed, 1 left", r.get_data(as_text=True))
        have = self.codes()
        self.assertEqual((have["NEWONE"]["fee_p"], have["NEWONE"]["active"]), (4550, 1))
        self.assertEqual((have["FILL"]["name"], have["FILL"]["fee_p"]), ("Filling, tooth-coloured", 30000))
        r = self.post_file(c, "pricelist_apply", text, update=True)
        self.assertIn("0 new, 1 changed", r.get_data(as_text=True))
        have = self.codes()
        self.assertEqual((have["FILL"]["name"], have["FILL"]["fee_p"]), ("Changed", 99900))

    def test_check_only_changes_nothing(self):
        text = "code,name,price\nNEWONE,A new thing,10\n"
        r = self.post_file(self.mgr(), "pricelist_check", text)
        self.assertIn("Nothing has been changed yet", r.get_data(as_text=True))
        self.assertNotIn("NEWONE", self.codes())

    def test_one_bad_row_changes_nothing_and_says_which(self):
        text = ("code,name,price,in_use\nGOOD1,Fine,10,yes\nBAD CODE,Spaces,10,yes\nGOOD2,Fine,abc,yes\n"
                "GOOD3,Fine,5,maybe\nGOOD1,Again,5,yes\n,No code,5,yes\n")
        c = self.mgr()
        r = self.post_file(c, "pricelist_apply", text, update=True)
        page = r.get_data(as_text=True)
        self.assertIn("Nothing was changed", page)
        for n in (3, 4, 5, 6, 7):
            self.assertIn(f"Row {n}:", page)
        self.assertNotIn("GOOD1", self.codes())
        self.assertEqual(self.one("SELECT COUNT(*) FROM audit_log WHERE action = 'catalogue.import'")[0], 0)

    def test_tax_names_and_negative_prices(self):
        self.rate("Reduced", "rate", "5")
        conn = self.db()
        res = CAT.import_csv(conn, self.clinic_row(1), None, "code,name,price,tax\nA1,Thing,10,Reduced\nA2,Thing,10,Mystery\nA3,Thing,-5,\n")
        conn.close()
        self.assertEqual([n for n, _ in res["errors"]], [3, 4])
        conn = self.db()
        res = CAT.import_csv(conn, self.clinic_row(1), None, "code,name,price,tax\nA1,Thing,10,Reduced\n")
        conn.commit()
        conn.close()
        self.assertEqual(res["added"], 1)
        self.assertEqual(self.one("SELECT tax_rate_id FROM procedure_codes WHERE code = 'A1' AND clinic_id = 1")[0],
                         self.one("SELECT id FROM tax_rates WHERE name = 'Reduced'")[0])

    def test_semicolon_files_bom_and_clinic_isolation(self):
        text = "\ufeffcode;name;price\nSEMI;Semicolon file;12,50\n"
        conn = self.db()
        res = CAT.import_csv(conn, self.clinic_row(1), None, text)
        conn.commit()
        conn.close()
        self.assertEqual(res["errors"], [])
        self.assertEqual(self.codes()["SEMI"]["fee_p"], 1250)
        self.assertNotIn("SEMI", self.codes(2))

    def test_formula_cells_are_defused_on_the_way_out_and_back_in(self):
        self.sql("UPDATE procedure_codes SET name = '=HYPERLINK(\"http://evil\")' WHERE clinic_id = 1 AND code = 'FILL'")
        text = CAT.export_csv(self.db(), self.clinic_row(1))
        self.assertIn("'=HYPERLINK", text)
        self.assertNotIn(",=HYPERLINK", text)
        conn = self.db()
        CAT.import_csv(conn, self.clinic_row(1), None, text, update_existing=True)
        conn.commit()
        conn.close()
        self.assertTrue(self.codes()["FILL"]["name"].startswith("=HYPERLINK"))

    def test_empty_cells_stay_empty_in_exports(self):
        from dental_os.services.dataio import _safe_cell
        self.assertEqual(_safe_cell(""), "")
        self.assertEqual(_safe_cell("+233201234567"), "+233201234567")
        self.assertEqual(_safe_cell("-5"), "-5")
        self.assertEqual(_safe_cell("+cmd|calc"), "'+cmd|calc")
        self.assertEqual(_safe_cell("=1+1"), "'=1+1")

    def test_bad_files_are_refused_plainly(self):
        conn = self.db()
        for text, word in (("", "empty"), ("price\n10\n", "code and name"), ("code,name\n", "no rows")):
            with self.assertRaises(CAT.CatalogueError) as ctx:
                CAT.import_csv(conn, self.clinic_row(1), None, text)
            self.assertIn(word, str(ctx.exception))
        big = "code,name\n" + "".join(f"C{i},Name\n" for i in range(CAT.MAX_ROWS + 1))
        with self.assertRaises(CAT.CatalogueError):
            CAT.import_csv(conn, self.clinic_row(1), None, big)
        conn.close()
        c = self.mgr()
        r = self.post(c, "/catalogue", {"action": "pricelist_apply"}, follow_redirects=True)
        self.assertIn("Choose a CSV file", r.get_data(as_text=True))

    def test_new_pack_codes_stay_off_until_priced(self):
        c = self.mgr()
        self.post(c, "/catalogue", {"action": "pack", "pack": "cosmetic"})
        page = self.page(c, "/catalogue")
        self.assertIn("Cosmetic dentistry", page)
        self.assertIn("6 of 6 added", page)
        pid = self.one("SELECT id FROM procedure_codes WHERE code = 'VENEER' AND clinic_id = 1")[0]
        self.post(c, "/catalogue", {"action": "update", "id": str(pid), "fee": "800", "active": "1"})
        row = self.codes()["VENEER"]
        self.assertEqual((row["fee_p"], row["active"]), (80000, 1))


# ===================================================================== French
SRC = Path(__file__).resolve().parent.parent / "dental_os"
MARKED = re.compile(r"""\b_d?\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
SAY = re.compile(r"""\bsay\(\s*"((?:[^"\\]|\\.)*)\"""")
# wording that reaches a visitor from code rather than from a template. Each is also checked to still be in the code.
CODE_WORDING = {
    "booking.py": ["Too many requests from this connection. Please try again later, or contact the clinic directly.",
                   "We are getting a lot of requests right now. Please try again later, or contact the clinic directly.",
                   "We cannot take more requests online right now. Please contact the clinic directly.",
                   "Pick what the visit is for.", "Pick one of the times shown.", "Pick a dentist from the list.",
                   "Sorry, that time has just been taken or is no longer offered. Please pick another.",
                   "We already have a request from this number for that time."],
    "patients.py": ["First name is needed.", "Last name is needed.", "That email address does not look right."],
    "phone.py": ["Enter a phone number.", "Phone numbers can only have digits, spaces and a leading +.",
                 "That phone number looks the wrong length.", "Phone numbers can only have digits and spaces.",
                 "Start the number with the country code, for example +233 or +44."],
    "links.py": ["Pick one of the two answers.", "That visit was not found.", "That quote was not found, or it is not waiting for an answer.",
                 "Your last form is still waiting to be checked by the clinic. Thank you.", "One of your answers is too long. Please shorten it.",
                 "Please answer at least one question. If nothing applies, write 'none' under conditions."],
    "plans.py": ["Enter the full name of the person who agreed.", "Tick the box to confirm the patient has seen the cost and agreed.",
                 "This patient is under 18. A parent or guardian needs to agree."],
    "scheduling.py": ["Only booked appointments can be confirmed."],
    "services/privacy.py": ["This person has already been anonymised.", "There are appointments still booked. Cancel or complete them first.",
                            "Other patients list this person as their family head. Change that first.", "An insurance claim is still open. Close it first.",
                            "The account is not settled. Collect, refund or write off the balance first, because the clinic needs to know who owes or is owed."],
}
LABEL_DICTS = [("rxlink", "STATES"), ("xrayai", "KINDS"), ("xrayai", "STATES"), ("xrayqc", "ISSUES"), ("pilot", "BY_HAND"), ("pilot", "PROBLEM_STATES"), ("pilot", "URGENCY"), ("pilot", "KINDS"), ("plans", "STATUSES"), ("memberships", "CYCLES"), ("insurance", "CLAIM_STATUS"), ("insurance", "PAYER_KINDS"), ("instalments", "FREQUENCIES"),
               ("labs", "LABELS"), ("stock", "KINDS"), ("medical", "KINDS"), ("medical", "SEVERITIES"), ("medical", "SMOKER"), ("files", "KINDS"),
               ("taxes", "KINDS"), ("campaigns", "KINDS"), ("campaigns", "PURPOSES"), ("outbox", "KINDS"), ("privacy", "REQUEST_KINDS"),
               ("diaryrules", "BLOCK_KINDS"), ("diaryrules", "REPEATS"), ("diaryrules", "COLOURS"), ("diaryrules", "WEEKDAYS"),
               ("teeth", "NOTATIONS"), ("teeth", "HINT"),
               ("prescriptions", "CLASSES"), ("referrals", "KINDS"), ("referrals", "SOURCES"), ("referrals", "URGENCY"), ("referrals", "STATUSES"),
               ("vitals", "GRADES"), ("letters", "FIELDS"), ("contactlog", "KINDS"), ("mounts", "LAYOUTS"),
               ("tasks", "PRIORITIES"), ("tasks", "REPEATS"), ("onlinepay", "PROVIDERS"), ("onlinepay", "STATUSES"),
               ("voice", "STATUSES"), ("steri", "KINDS"), ("steri", "TESTS"), ("steri", "RESULTS"), ("steri", "CYCLE_RESULTS"),
               ("steri", "PACK_STATUS"), ("edi", "RELATION"), ("edi", "FILE_KINDS"), ("edi", "CAS_GROUPS"), ("edi", "CLP_STATUS"),
               ("nhs", "COURSES"), ("nhs", "EXEMPTIONS"), ("nhs", "EVIDENCE"), ("nhs", "NHS_CLASSES"), ("nhs", "STATUSES"),
               ("ortho", "APPLIANCES"), ("ortho", "ARCHES"), ("ortho", "MOLAR"), ("ortho", "CROWDING"), ("ortho", "STATUSES"),
               ("team", "EQUIPMENT_KINDS"), ("team", "EVENT_KINDS"), ("reportbuilder", "OPS"), ("reportbuilder", "AGGS"), ("reportbuilder", "LABELS"), ("sms", "PROVIDERS"), ("formbuilder", "KINDS"), ("formbuilder", "STATUSES"), ("implants", "KINDS"), ("implants", "REASONS"), ("providerpay", "BASES"), ("providerpay", "STATUSES"), ("governance", "CHANNELS"), ("governance", "CATEGORIES"), ("governance", "OUTCOMES"), ("governance", "C_STATUSES"), ("governance", "KINDS"), ("governance", "HARM"), ("governance", "I_STATUSES"), ("staffcpd", "TITLES"), ("staffcpd", "SCHEMES"), ("staffcpd", "OUTCOMES"), ("reactivation", "REASONS"), ("merge", "LABELS"), ("merge", "LEFT"), ("refunds", "REASONS"), ("autocodes", "REGIONS"), ("migrate", "KINDS"), ("ghclaims", "BATCH_STATUS"), ("platform", "STATES"),
               ("permgroups", "AREA_LABELS")]


def _service_labels():
    """Wording that lives in dictionaries inside the services and is translated where the templates show it."""
    import importlib
    out = set()
    for module, name in LABEL_DICTS:
        for v in getattr(importlib.import_module("dental_os.services." + module), name).values():
            out.update(x for x in v if isinstance(x, str)) if isinstance(v, tuple) else out.add(v)
    from dental_os.services import catalogue, perio
    for label, about, _codes in catalogue.PACKS.values():
        out.update((label, about))
    out.update(name for name, _teeth in perio.SEXTANTS)
    from dental_os.services import mounts
    out.update(label for _name, rows in mounts.LAYOUTS.values() for row in rows for label in row if label)
    return out


DYNAMIC = {"January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December",
           "Cover not in force on that day.", "Excess applied.", "Orthodontic lifetime limit reached.", "Yearly maximum reached.", "Refused", "This month", "Last month", "Last 30 days", "This quarter", "This year", "Active", "Completed", "Cancelled", "Membership fee", "Payment", "our team", "Our team", "tooth", "parent", "legal guardian", "authorised person", "the patient"}


def _marked_in_templates():
    found = set()
    for f in (SRC / "templates").glob("*.html"):
        for m in MARKED.finditer(f.read_text()):
            found.add(m.group(1) if m.group(1) is not None else m.group(2))
    return found


class FrenchCatalogueTests(unittest.TestCase):
    def setUp(self):
        from dental_os.services import billing as B, i18n_fr, msgtemplates as MT, scheduling as S
        self.fr, self.B, self.MT, self.S, self.mod = i18n_fr.FR, B, MT, S, i18n_fr

    def test_every_marked_wording_has_french(self):
        missing = sorted(k for k in _marked_in_templates() if k not in self.fr)
        self.assertEqual(missing, [])
        say_found = set()
        for m in SAY.finditer((SRC / "public.py").read_text()):
            say_found.add(m.group(1))
        self.assertEqual(sorted(k for k in say_found if k not in self.fr), [])
        self.assertGreaterEqual(len(say_found), 3)

    def test_code_wording_is_still_in_the_code_and_has_french(self):
        for filename, lines in CODE_WORDING.items():
            path = next((SRC).rglob(filename))
            text = path.read_text()
            for line in lines:
                self.assertIn(line, text, f"{line!r} is no longer in {filename}")
                self.assertIn(line, self.fr, f"no French for {line!r}")

    def test_lists_shown_to_patients_are_all_translated(self):
        for name in list(self.S.CATEGORIES) + list(self.B.METHODS.values()) + list(self.B.ADJUSTMENT_REASONS.values()):
            self.assertIn(name, self.fr, name)

    def test_service_labels_have_french(self):
        self.assertEqual(sorted(k for k in _service_labels() if k not in self.fr), [])

    def test_no_french_line_is_dead(self):
        used = _marked_in_templates() | DYNAMIC | _service_labels() | set(self.S.CATEGORIES) | set(self.B.METHODS.values()) | set(self.B.ADJUSTMENT_REASONS.values())
        used |= {m.group(1) for m in SAY.finditer((SRC / "public.py").read_text())}
        for lines in CODE_WORDING.values():
            used |= set(lines)
        from dental_os.services import i18n_fr_staff, i18n_fr_staff7
        used |= set(i18n_fr_staff7.MESSAGES) | self.mod.MESSAGE_KEYS   # plain errors and after-action messages; tests.test_v10 checks each is still in the code
        used |= set(i18n_fr_staff.FLASH)          # shown after an action; tests.test_v10 checks each is still in the code
        self.assertEqual(sorted(k for k in self.fr if k not in used), [])

    def test_fields_match_between_english_and_french(self):
        for en, fr in self.fr.items():
            self.assertEqual(sorted(re.findall(r"%\(\w+\)s", en)), sorted(re.findall(r"%\(\w+\)s", fr)), en)
            self.assertNotIn("\u2014", fr)                       # no em dashes
            self.assertEqual(fr, fr.strip(" "))
            self.assertNotEqual(fr, "", en)
        for kind, (_label, starter, required) in self.MT.KINDS.items():
            french = self.mod.STARTERS_FR[kind]
            self.assertEqual(sorted(self.MT.names_in(starter)), sorted(self.MT.names_in(french)), kind)
            self.MT.validate(french, required=required)           # passes the same rules a clinic's own wording must pass
        self.assertEqual(set(self.mod.STARTERS_FR), set(self.MT.KINDS))

    def test_french_typography(self):
        self.assertIn("\u00a0:", self.fr["Amount due:"])
        self.assertTrue(self.fr["Are you pregnant?"].endswith("\u00a0?"))
        self.assertNotIn(" ?", self.fr["Do you smoke?"])


class FrenchFormatTests(unittest.TestCase):
    def test_dates_and_times(self):
        from datetime import date, datetime
        from dental_os.services import i18n
        self.assertEqual(i18n.fmt_date(date(2026, 10, 24), "fr"), "24 octobre 2026")
        self.assertEqual(i18n.fmt_date(date(2026, 11, 1), "fr"), "1er novembre 2026")
        self.assertEqual(i18n.fmt_day_date(date(2026, 11, 3), "fr"), "mardi 3 novembre")
        self.assertEqual(i18n.fmt_day_date(date(2026, 8, 2), "fr"), "dimanche 2 août")
        self.assertEqual(i18n.fmt_time(datetime(2026, 1, 1, 16, 0), "fr"), "16 h")
        self.assertEqual(i18n.fmt_time(datetime(2026, 1, 1, 9, 5), "fr"), "9 h 05")
        self.assertEqual(i18n.fmt_date(date(2026, 10, 24), "en"), "24 October 2026")
        self.assertEqual(i18n.fmt_day_date(date(2026, 11, 3), "en"), "Tuesday 3 November")
        self.assertEqual(i18n.fmt_time(datetime(2026, 1, 1, 16, 30), "en"), "4:30 pm")
        for m in range(1, 13):
            self.assertTrue(i18n.fmt_date(date(2026, m, 15), "fr").split()[1] in i18n.MONTHS_FR)

    def test_language_choices(self):
        from dental_os.services import i18n
        self.assertEqual(i18n.pick("FR"), "fr")
        self.assertEqual(i18n.pick(" en "), "en")
        for bad in (None, "", "de", "fr-CH", "../x", "frfr"):
            self.assertIsNone(i18n.pick(bad))
        self.assertEqual(i18n.from_header("fr-CH, fr;q=0.9, en;q=0.8"), "fr")
        self.assertEqual(i18n.from_header("en-GB;q=0.5,en;q=0.4,fr;q=0.9"), "fr")
        self.assertEqual(i18n.from_header("en-GB,en;q=0.8,fr;q=0.9"), "en")
        self.assertEqual(i18n.from_header("de, it;q=0.5"), None)
        self.assertEqual(i18n.from_header("de, es;q=0.5"), "es")                        # 0.66: Spanish is offered now
        self.assertEqual(i18n.from_header("fr;q=0, en"), "en")
        self.assertIsNone(i18n.from_header(None))
        self.assertEqual(i18n.from_header("fr;q=abc,,,;;"), "fr")
        self.assertEqual(i18n.t("fr", "Today"), "Aujourd'hui")
        self.assertEqual(i18n.t("fr", "Something nobody translated"), "Something nobody translated")
        self.assertEqual(i18n.t("en", "Today"), "Today")
        self.assertEqual(i18n.t("fr", "Hello %(name)s. This page is only for you. Please do not share its address.", name="Ama")[:12], "Bonjour Ama.")
        self.assertEqual(i18n.t("xx", "Today"), "Today")


class French(Public, Tax):
    def fr_clinic(self, lang="fr", clinic=1):
        self.sql("UPDATE clinics SET language = ? WHERE id = ?", lang, clinic)

    def fr_patient(self, lang="fr", pid=1):
        self.sql("UPDATE patients SET language = ? WHERE id = ?", lang, pid)

    def link(self, pid=1):
        conn = self.db()
        token = links.create(conn, dict(conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()), 1, pid)
        conn.commit()
        conn.close()
        return token

    def text(self, c, url):
        r = c.get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.get_data(as_text=True)


class SignInAndMenuTests(French):
    def test_sign_in_follows_the_browser_and_the_visitor(self):
        c = self.app.test_client()
        page = self.text(c, "/login")
        self.assertIn("Log in", page)
        self.assertIn('lang="en-GB"', page)
        fr = self.app.test_client().get("/login", headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5"}).get_data(as_text=True)
        self.assertIn("Se connecter", fr)
        self.assertIn("Nom d&#39;utilisateur", fr)
        self.assertIn('<html lang="fr" dir="ltr">', fr)
        c = self.app.test_client()
        self.assertIn("Se connecter", self.text(c, "/login?lang=fr"))
        self.assertIn("Se connecter", self.text(c, "/login"))                  # kept for this browser
        self.assertIn("Log in", self.text(c, "/login?lang=en"))
        self.assertIn("Log in", self.text(c, "/login?lang=de"))                # not supported: ignored
        self.assertIn("Français", self.text(self.app.test_client(), "/login"))  # the way to switch is on the page

    def test_menus_use_the_clinic_language_unless_the_person_chose(self):
        self.fr_clinic()
        c = self.login("mgr")
        page = self.text(c, "/")
        for word in ("Aujourd&#39;hui", "Agenda", "Rappels", "Équipe", "Paramètres", "Se déconnecter"):
            self.assertIn(word, page)
        self.assertNotIn(">Settings<", page)
        self.post(c, "/account/language", {"language": "en"})
        page = self.text(c, "/")
        self.assertIn(">Settings<", page)
        self.assertIn(">Log out<", page)
        self.assertEqual(self.one("SELECT language FROM users WHERE username = 'mgr'")[0], "en")
        self.post(c, "/account/language", {"language": ""})
        self.assertIn("Paramètres", self.text(c, "/"))
        self.post(c, "/account/language", {"language": "klingon"})
        self.assertIsNone(self.one("SELECT language FROM users WHERE username = 'mgr'")[0])

    def test_one_persons_choice_does_not_change_another_screen(self):
        self.fr_clinic("en")
        c = self.login("mgr")
        self.post(c, "/account/language", {"language": "fr"})
        self.assertIn("Se déconnecter", self.text(c, "/"))
        self.assertIn(">Log out<", self.text(self.login("desk"), "/"))
        self.assertIn(">Log out<", self.text(self.login("mgr2"), "/"))

    def test_clinic_language_is_set_in_settings(self):
        c = self.login("mgr")
        form = {"action": "clinic", "name": "Clinic One", "open_time": "08:00", "close_time": "17:00", "days": ["1", "2", "3", "4", "5"],
                "slot_minutes": "30", "country": "GH", "currency": "GHS", "timezone": "Africa/Accra", "pay_cash": "1", "language": "fr"}
        self.post(c, "/settings", form)
        self.assertEqual(self.one("SELECT language FROM clinics WHERE id = 1")[0], "fr")
        self.assertEqual(self.one("SELECT language FROM clinics WHERE id = 2")[0], "en")
        self.post(c, "/settings", dict(form, language="xx"))
        self.assertEqual(self.one("SELECT language FROM clinics WHERE id = 1")[0], "fr")       # a bad value changes nothing
        form.pop("language")
        self.post(c, "/settings", form)
        self.assertEqual(self.one("SELECT language FROM clinics WHERE id = 1")[0], "fr")       # a form without the field changes nothing

    def test_a_bad_language_in_the_database_falls_back_to_english(self):
        self.sql("UPDATE clinics SET language = 'zz' WHERE id = 1")
        self.assertIn(">Log out<", self.text(self.login("mgr"), "/"))


class BookingPageTests(French):
    def test_booking_page_in_french_and_back(self):
        token = self.open_booking()
        self.assertIn("Ask for a visit", self.text(self.anon(), f"/book/{token}"))
        self.fr_clinic()
        c = self.anon()
        page = self.text(c, f"/book/{token}")
        for word in ("Demander un rendez-vous", "Choisissez un horaire", "Prénom", "Téléphone (WhatsApp si possible)", "Demander cet horaire"):
            self.assertIn(word, page)
        self.assertRegex(page, r"(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche) \d")
        self.assertRegex(page, r"<span>\d{1,2} h( \d\d)?</span>")
        self.assertIn("Consultation", page)
        self.assertIn('value="Cleaning &amp; preventive care"', page)                   # what is stored stays English
        self.assertIn("Détartrage et soins préventifs", page)
        self.assertIn("Ask for a visit", self.text(c, f"/book/{token}?lang=en"))
        self.assertIn("Ask for a visit", self.text(c, f"/book/{token}"))                 # the visitor's choice is kept

    def test_booking_works_and_errors_are_in_french(self):
        token = self.open_booking()
        self.fr_clinic()
        c = self.anon()
        slot = self.first_slot(token)
        r = self.anon_post(c, f"/book/{token}", f"/book/{token}", self.request_data(slot, first_name=""))
        self.assertIn("Le prénom est obligatoire.", r.get_data(as_text=True))
        r = self.anon_post(c, f"/book/{token}", f"/book/{token}", self.request_data(slot, phone="abc"))
        self.assertIn("Un numéro de téléphone ne peut contenir", r.get_data(as_text=True))
        r = self.anon_post(c, f"/book/{token}", f"/book/{token}", self.request_data("nope"))
        self.assertIn("Choisissez l&#39;un des horaires proposés.", r.get_data(as_text=True))
        r = self.anon_post(c, f"/book/{token}", f"/book/{token}", self.request_data(slot, reason="Not a reason"))
        self.assertIn("Choisissez le motif du rendez-vous.", r.get_data(as_text=True))
        r = self.anon_post(c, f"/book/{token}", f"/book/{token}", self.request_data(slot))
        self.assertIn("Nous avons bien reçu votre demande", r.get_data(as_text=True))
        row = self.one("SELECT * FROM booking_requests")
        self.assertEqual(row["reason"], "Consultation")

    def test_lang_in_the_query_survives_a_post_back_to_the_same_page(self):
        token = self.open_booking()
        c = self.anon()
        slot = self.first_slot(token)
        r = self.anon_post(c, f"/book/{token}?lang=fr", f"/book/{token}?lang=fr", self.request_data(slot, first_name=""))
        self.assertIn("Le prénom est obligatoire.", r.get_data(as_text=True))


class PortalTests(French):
    def test_patient_language_beats_clinic_language_on_their_page(self):
        token = self.link()
        c = self.anon()
        self.assertIn("Your visits", self.text(c, f"/p/{token}"))
        self.fr_patient()
        page = self.text(self.anon(), f"/p/{token}")
        for word in ("Bonjour Ama.", "Vos rendez-vous", "Votre questionnaire de santé", "Vous ne devez rien."):
            self.assertIn(word, page)
        self.fr_patient(None)
        self.fr_clinic()
        self.assertIn("Vos rendez-vous", self.text(self.anon(), f"/p/{token}"))        # falls back to the clinic's

    def test_visit_day_and_time_are_french_and_answers_are_acknowledged(self):
        self.consent()
        self.fr_patient()
        aid = self.appt(days=3, hour=14, minute=30)
        token = self.link()
        c = self.anon()
        page = self.text(c, f"/p/{token}")
        self.assertRegex(page, r"(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche) \d{1,2}[^<]* octobre|novembre|décembre|janvier")
        self.assertIn("14 h 30", page)
        self.assertIn("Notre équipe", page) if not self.one("SELECT provider_id FROM appointments WHERE id = ?", aid)[0] else None
        r = self.anon_post(c, f"/p/{token}", f"/p/{token}/visit/{aid}", {"answer": "confirmed"})
        r = c.get(f"/p/{token}")
        body = r.get_data(as_text=True)
        self.assertIn("Merci. Nous avons bien reçu votre réponse.", body)
        self.assertIn("Vous avez confirmé", body)

    def test_visitor_choice_beats_the_patient_and_sticks_across_pages(self):
        self.fr_patient()
        token = self.link()
        c = self.anon()
        self.assertIn("Vos rendez-vous", self.text(c, f"/p/{token}"))
        self.assertIn("Your visits", self.text(c, f"/p/{token}?lang=en"))
        self.assertIn("Health form", self.text(c, f"/p/{token}/health"))

    def test_health_form_in_french_with_french_errors(self):
        self.fr_patient()
        token = self.link()
        c = self.anon()
        page = self.text(c, f"/p/{token}/health")
        self.assertIn("Êtes-vous enceinte\u00a0?", page)
        self.assertIn("Médicaments que vous prenez", page)
        r = self.anon_post(c, f"/p/{token}/health", f"/p/{token}/health", {})
        self.assertIn("Merci de répondre à au moins une question", r.get_data(as_text=True))
        r = self.anon_post(c, f"/p/{token}/health", f"/p/{token}/health", {"conditions": "none"})
        self.assertEqual(r.status_code, 302)
        r = c.get(f"/p/{token}")
        self.assertIn("Merci. Le cabinet vérifiera vos réponses.", r.get_data(as_text=True))

    def test_a_name_with_markup_is_not_run_in_french(self):
        self.fr_patient()
        self.sql("UPDATE patients SET first_name = '<script>alert(1)</script>' WHERE id = 1")
        page = self.text(self.anon(), f"/p/{self.link()}")
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("&lt;script&gt;alert(1)", page)

    def test_quote_in_french_with_french_money_words(self):
        self.fr_patient()
        conn = self.db()
        pid = PL.create(conn, self.clinic(), 1, 1, "Option A")
        PL.add_item(conn, self.clinic(), 1, pid, self.cid("FILL"))
        PL.present(conn, self.clinic(), {"id": 1}, pid, 7)
        conn.commit()
        conn.close()
        page = self.text(self.anon(), f"/p/{self.link()}")
        self.assertIn("Votre devis\u00a0: Option A", page)
        self.assertIn("Accepter ce devis", page)
        self.assertIn("le parent du patient", page)


class DocumentTests(French):
    """A document is in the patient's language whoever prints it. The Print and Back buttons are the staff member's."""

    def test_invoice_receipt_statement_quote_and_consent(self):
        self.set_default("TVA", 1800, 0)
        ids = self.work("FILL")
        iid = self.invoice(ids)
        self.pay(10000, today())
        pay = self.one("SELECT id FROM payments ORDER BY id DESC")[0]
        self.sql("INSERT INTO consents(clinic_id, patient_id, title, body, signer_name, signed_at) VALUES (1,1,'Extraction','Wording','Ama Mensah',?)",
                 f"{today()} 14:30")
        consent = self.one("SELECT MAX(id) FROM consents")[0]
        conn = self.db()
        plan = PL.create(conn, self.clinic(), 1, 1, "Option A")
        PL.add_item(conn, self.clinic(), 1, plan, self.cid("CROWN"))
        conn.commit()
        conn.close()
        c = self.login("mgr")
        urls = {"invoice": f"/invoices/{iid}", "receipt": f"/payments/{pay}/receipt", "statement": "/patients/1/statement",
                "quote": f"/plans/{plan}/quote", "consent": f"/consents/{consent}"}
        english = {k: self.text(c, u) for k, u in urls.items()}
        self.assertIn("Invoice", english["invoice"])
        self.assertIn("Billed to", english["invoice"])
        self.assertIn("Receipt", english["receipt"])
        self.assertIn("Treatment quote", english["quote"])
        self.fr_patient()
        french = {k: self.text(c, u) for k, u in urls.items()}
        for k, words in {"invoice": ("Facture", "Facturé à", "Émise le", "À payer avant le", "Sous-total", "TVA"),
                         "receipt": ("Reçu R-", "Reçu de", "Payé par", "Espèces", "Merci. Ceci est un reçu de paiement."),
                         "statement": ("Relevé de compte", "Montant dû\u00a0:", "Paiement, Espèces"),
                         "quote": ("Devis de soins", "Phase 1", "Total de la phase 1", "Total\u00a0:", "Ceci est une estimation"),
                         "consent": ("Signé par", "le patient", "Saisi à l&#39;écran", " à 14:30")}.items():
            for w in words:
                self.assertIn(w, french[k], f"{k}: {w}")
        self.assertNotIn("Billed to", french["invoice"])
        from dental_os.services import i18n as _i18n
        self.assertIn(_i18n.MONTHS_FR[today().month - 1], french["invoice"])
        self.assertIn("Print", french["invoice"])                       # the staff member's own button is not translated
        self.assertIn(">Settings<", french["invoice"])                  # and the menus stay in the staff member's language
        other = self.login("mgr2")
        self.assertEqual(other.get(urls["invoice"]).status_code, 404)

    def test_statement_labels_and_tooth_numbers(self):
        self.fr_patient()
        self.sql("INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, provider_id, planned_on, done_on, tooth) VALUES (1,1,?, 'done', 30000, 1, ?, ?, 36)",
                 self.cid("FILL"), str(today()), str(today()))
        self.sql("INSERT INTO adjustments(clinic_id, patient_id, amount_p, reason, adjusted_on) VALUES (1,1,-1000,'discount',?)", str(today()))
        page = self.text(self.login("mgr"), "/patients/1/statement")
        self.assertIn(", dent 36", page)
        self.assertIn("Autre remise", page)

    def test_documents_follow_the_clinic_when_the_patient_has_no_language(self):
        self.fr_clinic()
        ids = self.work("FILL")
        iid = self.invoice(ids)
        self.assertIn("Facture", self.text(self.login("mgr"), f"/invoices/{iid}"))
        self.fr_patient("en")
        self.assertIn("Invoice", self.text(self.login("mgr"), f"/invoices/{iid}"))


class FrenchMessageTests(French):
    def reminder(self, pid=1):
        conn = self.db()
        row = conn.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
        a = conn.execute("""SELECT a.*, pr.name AS provider_name FROM appointments a LEFT JOIN providers pr ON pr.id = a.provider_id
                            ORDER BY a.id DESC""").fetchone()
        from dental_os.services import messaging
        return messaging.appointment_text(self.clinic_row(1), row, a, "reminder", conn)

    def test_french_patient_gets_the_french_starter_with_french_day_and_time(self):
        self.appt(days=3, hour=16, minute=30)
        self.assertIn("a reminder from", self.reminder())
        self.fr_patient()
        text = self.reminder()
        self.assertTrue(text.startswith("Bonjour Ama,"), text)
        self.assertIn("16 h 30", text)
        self.assertRegex(text, r"le (lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche) \d")
        self.assertNotIn("{", text)

    def test_a_clinic_that_wrote_its_own_wording_keeps_it_for_everyone(self):
        self.appt(days=3)
        self.fr_patient()
        self.sql("UPDATE message_templates SET body = 'Hi {first_name}, see you {day}.' WHERE clinic_id = 1 AND kind = 'reminder'")
        text = self.reminder()
        self.assertTrue(text.startswith("Hi Ama, see you "), text)
        self.assertRegex(text, r"(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)")     # the day is still in the patient's language

    def test_the_send_list_uses_the_patients_language(self):
        self.consent()
        self.fr_patient()
        self.appt(days=2, hour=10)
        self.build()
        q = self.queued("reminder")
        self.assertEqual(len(q), 1)
        conn = self.db()
        _, text = outbox.text_for(conn, self.clinic_row(1), q[0])
        self.assertTrue(text.startswith("Bonjour Ama,"), text)
        self.assertIn("10 h", text)

    def test_recall_and_portal_link_wording(self):
        from dental_os.services import messaging
        row = self.one("SELECT * FROM patients WHERE id = 1")
        self.fr_patient()
        row = self.one("SELECT * FROM patients WHERE id = 1")
        self.assertIn("prochain contrôle", messaging.recall_text(self.clinic_row(1), row, self.db()))
        self.assertIn("prochain contrôle" if False else "{link}", MT.KINDS["portal_link"][1])
        self.assertIn("lien privé", MT.body_for(self.db(), 1, "portal_link", "fr"))
        self.assertIn("private link", MT.body_for(self.db(), 1, "portal_link", "en"))
        self.assertEqual(MT.body_for(self.db(), 1, "portal_link"), MT.KINDS["portal_link"][1])

    def test_patient_language_is_saved_from_the_patient_form_and_not_wiped_by_other_edits(self):
        c = self.login("desk")
        form = {"first_name": "Ama", "last_name": "Mensah", "phone": "0201112223", "language": "fr"}
        self.post(c, "/patients/1/edit", form)
        self.assertEqual(self.one("SELECT language FROM patients WHERE id = 1")[0], "fr")
        self.post(c, "/patients/1/edit", dict(form, language="xx"))
        self.assertIsNone(self.one("SELECT language FROM patients WHERE id = 1")[0])
        self.post(c, "/patients/1/edit", dict(form, language="fr"))
        data = P.clean({"first_name": "Ama", "last_name": "Mensah", "phone": "0201112223"}, None, None, country="GH")     # the API path: no field
        self.assertNotIn("language", data)
        conn = self.db()
        P.update(conn, 1, None, 1, data)
        conn.commit()
        conn.close()
        self.assertEqual(self.one("SELECT language FROM patients WHERE id = 1")[0], "fr")
        page = self.text(c, "/patients/1/edit")
        self.assertIn('<option value="fr" selected>', page)


if __name__ == "__main__":
    unittest.main()
