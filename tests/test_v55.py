"""v0.55: phones. A menu button, scrolling tabs and tables, and a home-screen icon. The layout itself was checked by eye in a phone-sized
browser (390 pixels wide): every main screen fits with nothing sticking out sideways."""
import json
from pathlib import Path

from tests.test_v04 import Base

STATIC = Path(__file__).resolve().parent.parent / "dental_os" / "static"


class PhoneTests(Base):
    def test_menu_button_manifest_and_styles(self):
        page = self.page(self.login("desk"), "/")
        self.assertIn('data-menu aria-expanded="false" aria-controls="mainnav"', page)
        self.assertIn('id="mainnav"', page)
        self.assertIn("manifest.webmanifest", page)
        m = json.loads((STATIC / "manifest.webmanifest").read_text())
        self.assertEqual((m["display"], m["start_url"]), ("standalone", "/"))
        css = (STATIC / "app.css").read_text()
        self.assertIn("@media (max-width: 860px)", css)
        self.assertIn(".js header.top nav { display: none", css)               # without the script the menu stays open, never lost
        self.assertIn("data-menu", (STATIC / "app.js").read_text())
        r = self.app.test_client().get("/static/manifest.webmanifest")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Menu", self.page(self.login("desk"), "/?lang=fr").replace("Menu", "Menu"))
