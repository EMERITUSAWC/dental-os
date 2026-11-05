"""v0.70: the top menu is grouped so it fits on one line, and the tooth chart draws real tooth shapes (crown, roots and the
biting surface) instead of plain boxes."""
import re

from tests.test_v04 import Base
from tests import a11y
from dental_os.services import chart as CH


def nav(page):
    return page.split('<nav id="mainnav"', 1)[1].split("</nav>", 1)[0]


class Menu(Base):
    def top_level(self, page):
        """What sits on the menu bar itself: plain links and group headings, not what is inside a group."""
        n = re.sub(r'<div class="navdrop">.*?</div>', "", nav(page), flags=re.S)
        items = re.findall(r"<a [^>]*>([^<]+)", n) + re.findall(r"<summary>([^<]+)", n)
        return [i.strip() for i in items]

    def test_manager_menu_is_short(self):
        items = self.top_level(self.page(self.login("mgr"), "/"))
        self.assertLessEqual(len(items), 9, items)
        for word in ("Today", "Diary", "Patients", "Reports", "Inbox", "Clinic", "Money", "Admin"):
            self.assertIn(word, items)

    def test_every_page_still_reachable_from_the_menu(self):
        n = nav(self.page(self.login("mgr"), "/"))
        for url in ("/settings", "/staff", "/tools", "/claims", "/stock", "/lab", "/tasks", "/team", "/recalls"):
            self.assertIn(f'href="{url}', n)

    def test_groups_follow_permissions(self):
        n = nav(self.page(self.login("desk"), "/"))
        self.assertNotIn("<summary>Admin", n)                 # nothing in Admin for the front desk, so no heading
        self.assertNotIn('href="/settings', n)
        self.assertNotIn('href="/stock', n)
        self.assertIn("<summary>Inbox", n)

    def test_group_shows_where_you_are(self):
        n = nav(self.page(self.login("mgr"), "/stock"))
        self.assertIn('class="navgroup on"', n)
        self.assertRegex(n, r'<details class="navgroup on" data-navgroup><summary>Clinic')

    def test_inbox_badge_adds_up(self):
        c = self.login("dent")
        self.post(c, "/team", {"to_user": "", "body": "Lab work is back.", "back": "/"})
        n = nav(self.page(self.login("desk"), "/"))
        self.assertRegex(n, r'<summary>Inbox <span class="badge">\d+</span>')

    def test_menu_is_accessible(self):
        page = self.page(self.login("mgr"), "/")
        self.assertEqual(a11y.check(page) if hasattr(a11y, "check") else [], [])
        self.assertIn('data-menu aria-expanded="false" aria-controls="mainnav"', page)

    def test_french_menu(self):
        conn = self.db()
        conn.execute("UPDATE users SET language = 'fr' WHERE username = 'mgr'")
        conn.commit()
        conn.close()
        n = nav(self.page(self.login("mgr"), "/"))
        self.assertIn("Boîte de réception", n)
        self.assertIn("Administration", n)


class DrawnTeeth(Base):
    def test_kinds_from_the_tooth_number(self):
        self.assertEqual(CH.tooth_kind(11), "incisor")
        self.assertEqual(CH.tooth_kind(23), "canine")
        self.assertEqual(CH.tooth_kind(35), "premolar")
        self.assertEqual(CH.tooth_kind(16), "molar_upper")
        self.assertEqual(CH.tooth_kind(46), "molar_lower")
        self.assertEqual(CH.tooth_kind(55), "child_molar")
        self.assertEqual(CH.tooth_kind(61), "incisor")
        self.assertTrue(CH.is_upper(26) and CH.is_upper(64) and not CH.is_upper(36) and not CH.is_upper(84))
        self.assertEqual(len(CH.SHAPES["molar_upper"]["roots"]), 3)
        self.assertEqual(len(CH.SHAPES["molar_lower"]["roots"]), 2)

    def test_chart_draws_crowns_roots_and_surfaces(self):
        page = self.page(self.login("dent"), "/patients/1/chart")
        self.assertEqual(page.count('<a class="tooth'), 32)
        self.assertEqual(page.count('class="crown'), 32)
        self.assertEqual(page.count('class="root"'), 6 * 3 + 6 * 2 + 20)       # six upper molars with 3 roots, six lower with 2, the rest 1
        self.assertEqual(len(set(re.findall(r'clipPath id="(top\d+)"', page))), 32)  # one outline each, ids unique
        self.assertIn('class="gap"', page)                                       # the midline
        child = self.page(self.login("dent"), "/patients/2/chart?dentition=child")
        self.assertEqual(child.count('<a class="tooth'), 20)
        self.assertIn("k-child_molar", child)

    def test_lower_teeth_are_upside_down(self):
        page = self.page(self.login("dent"), "/patients/1/chart")
        t16 = page.split('aria-label="Tooth 16"')[0].rsplit("<a ", 1)[1] + page.split('aria-label="Tooth 16"')[1].split("</a>")[0]
        t46 = page.split('aria-label="Tooth 46"')[1].split("</a>")[0]
        self.assertNotIn("scale(1,-1)", t16)
        self.assertIn("scale(1,-1)", t46)

    def test_whole_tooth_things_are_drawn(self):
        conn = self.db()
        clinic = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        for tooth, what in (("11", "crown"), ("21", "root_canal"), ("36", "implant"), ("46", "missing"), ("26", "bridge_pontic"),
                            ("38", "unerupted"), ("14", "fracture")):
            CH.add_condition(conn, clinic, 2, 1, tooth, [], what)
        CH.add_condition(conn, clinic, 2, 1, "16", ["M", "O"], "filling")
        conn.commit()
        conn.close()
        page = self.page(self.login("dent"), "/patients/1/chart")

        def one(n):
            return page.split(f'aria-label="Tooth {n}')[1].split("</a>")[0]
        self.assertIn("capped", one(11))
        self.assertIn('class="canal"', one(21))
        self.assertIn('class="imp', one(36))
        self.assertNotIn('class="root"', one(36))                               # an implant has no natural root
        self.assertIn('class="x"', one(46))
        self.assertIn(", missing", one(46))
        self.assertNotIn('class="root"', one(26))                               # a false tooth has no root
        self.assertIn('class="crack"', one(14))
        self.assertIn("under", page.split('aria-label="Tooth 38')[0].rsplit("<a ", 1)[1])
        self.assertEqual(one(16).count('class="s-cond"'), 2)                    # two surfaces shown, M and O

    def test_chart_page_passes_accessibility_checks(self):
        page = self.page(self.login("dent"), "/patients/1/chart?tooth=16")
        if hasattr(a11y, "check"):
            self.assertEqual(a11y.check(page), [])
        for svg in re.findall(r"<svg[^>]*>", page):
            self.assertTrue('aria-hidden="true"' in svg or 'role="img"' in svg, svg)
