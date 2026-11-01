"""Starting tax set-ups by country.

This is NOT tax advice and it never decides what a clinic charges. It does two small things: it adds the named rates that a country
commonly needs (so the manager does not have to type them), and it puts the facts we could check, with where they came from and
the date we checked them, in front of the manager and their accountant. It never touches a procedure code, never changes a rate
that is already set, and never switches tax on: the clinic's default rate stays what the manager made it.

Every entry says how well each fact is supported:
  official   read on the tax authority's own site, or in the law's text
  guidance   read in an official guide or manual that explains the law
  secondary  read in a news report or an adviser's summary of the law. Check it before relying on it.
A fact we could not check is not written down. A country with no entry here simply has no starting point yet.

Rates in this product are one number per rate. Where a country charges several taxes on the same price (Ghana's VAT and its two
levies), they are shown as one combined rate in the name, so an invoice shows one tax line. Where the tax authority needs the
parts listed separately, the clinic's accountant must say so.

Checked on 27 October 2026. Tax law changes often (Ghana and Nigeria both changed on 1 January 2026). Re-check before each launch."""
from . import audit, taxes

CHECKED = "27 October 2026"


def _src(title, url, strength):
    return {"title": title, "url": url, "strength": strength}


PRESETS = {
    "GH": {
        "country": "Ghana",
        "tax_name": "VAT",
        "inclusive": True,
        "rates": [("Exempt", "exempt", "0"), ("VAT 15% + levies 5%", "rate", "20")],
        "facts": [
            ("From 1 January 2026 the VAT rate is 15%, with the NHIL at 2.5% and the GETFund levy at 2.5% on top, which is 20% of the price before tax. "
             "The COVID-19 levy no longer exists.", "official"),
            ("Supplies that are exempt from VAT are also exempt from the NHIL and GETFund levy.", "official"),
            ("The GRA's list of exempt supplies has an entry for medical services. It does not name dental work.", "official"),
            ("A business that supplies services must register for VAT whatever its turnover.", "guidance"),
        ],
        "ask": ["Is dental treatment a medical service under the VAT Act 2025 (Act 1151), so that it is exempt? The GRA's list we read still refers to Act 870.",
                "Are cosmetic treatments (whitening, veneers) and goods you sell (toothbrushes, mouthwash) taxable?",
                "Does the GRA need VAT, NHIL and GETFund shown on separate lines on your invoice or e-VAT receipt?"],
        "sources": [_src("GRA, VAT rates", "https://gra.gov.gh/domestic-tax/tax-types/vat/", "official"),
                    _src("GRA, VAT exemptions", "https://gra.gov.gh/domestic-tax/tax-types/vat-exemptions/", "official"),
                    _src("EY, Ghana's Parliament enacts several indirect tax-related laws", "https://globaltaxnews.ey.com/news/2026-0163-ghanas-parliament-enacts-several-indirect-tax-related-laws-effective-1-january-2026", "guidance"),
                    _src("Crowe Ghana, VAT reform 2026", "https://www.crowe.com/gh/news/ghana-vat-reform-2026", "guidance")],
    },
    "GB": {
        "country": "United Kingdom",
        "tax_name": "VAT",
        "inclusive": True,
        "rates": [("Exempt", "exempt", "0"), ("VAT 20%", "rate", "20")],
        "facts": [
            ("The standard VAT rate is 20%.", "official"),
            ("Health services by registered dentists are exempt from VAT where the main purpose is to protect, maintain or restore the person's health. "
             "Dental prostheses supplied by those professionals are exempt too.", "official"),
            ("The exemption covers the professional services of someone on the dentists' register or the roll of dental auxiliaries, "
             "or work directly supervised by them in a company.", "official"),
        ],
        "ask": ["Which of your treatments have a cosmetic main purpose (whitening, some veneers)? The HMRC page we read does not list them. Our reading is that "
                "they are not exempt, but that is our inference, not HMRC's words.",
                "Do you sell goods (for example over-the-counter products)? The page we read does not cover them.",
                "If you have both exempt and taxable income, how does partial exemption affect the VAT you can claim back?"],
        "sources": [_src("GOV.UK, VAT rates", "https://www.gov.uk/vat-rates", "official"),
                    _src("GOV.UK, VAT rates on different goods and services", "https://www.gov.uk/guidance/vat-rates-on-different-goods-and-services", "official"),
                    _src("HMRC VAT health manual, Dentists: introduction (VATHLT2450)", "https://www.gov.uk/hmrc-internal-manuals/vat-health/vathlt2450", "official")],
    },
    "ZA": {
        "country": "South Africa",
        "tax_name": "VAT",
        "inclusive": True,
        "rates": [("Exempt", "exempt", "0"), ("VAT 15%", "rate", "15")],
        "facts": [
            ("The standard VAT rate is 15%.", "secondary"),
            ("Medical services provided by registered practitioners are exempt from VAT.", "secondary"),
        ],
        "ask": ["Confirm both points against the VAT Act (section 12 covers exempt services) and SARS guidance. We could only read an adviser's summary.",
                "Are cosmetic dental treatments exempt, and what about goods you sell?"],
        "sources": [_src("Accounter, VAT for healthcare (says it uses SARS figures)", "https://accounter.co.za/calculators/for/vat/healthcare", "secondary")],
    },
    "NG": {
        "country": "Nigeria",
        "tax_name": "VAT",
        "inclusive": True,
        "rates": [("Zero rated", "zero", "0"), ("VAT 7.5%", "rate", "7.5")],
        "facts": [
            ("The VAT rate stays at 7.5% under the Nigeria Tax Act 2025, which takes effect on 1 January 2026.", "guidance"),
            ("A published list of items under the new law shows 'medical services' and 'all medical and pharmaceutical products' as zero rated, "
             "which differs from the old exempt treatment (zero rated means you can claim back the VAT you pay on your costs).", "secondary"),
        ],
        "ask": ["Is dental treatment a 'medical service' for this zero rating? The list we read does not name dentists.",
                "Do you have to register for VAT and file returns even when your treatment is zero rated?"],
        "sources": [_src("Forvis Mazars, 2025 Nigeria VAT key business changes", "https://www.forvismazars.com/ng/en/services/tax/nigeria-s-tax-reform-in-focus/2025-nta-vat-key-business-changes", "guidance"),
                    _src("TheCable, list of items exempted or zero rated in the new tax law", "https://www.thecable.ng/full-list-sanitary-pads-products-energy-exports-land-items-exempted-from-vat-in-new-tax-law", "secondary")],
    },
    "KE": {
        "country": "Kenya",
        "tax_name": "VAT",
        "inclusive": True,
        "rates": [("Exempt", "exempt", "0"), ("VAT 16%", "rate", "16")],
        "facts": [
            ("The standard VAT rate is 16%.", "guidance"),
            ("Medical, veterinary, dental and nursing services are listed as exempt, not zero rated.", "guidance"),
            ("A news report on the KRA's updated list says healthcare and medical services stay exempt.", "secondary"),
        ],
        "ask": ["Confirm against the VAT Act's First Schedule and the latest Finance Act, because Kenya changes this list often.",
                "Are cosmetic dental treatments and goods you sell exempt?"],
        "sources": [_src("Grant Thornton, Kenya indirect tax guide", "https://www.grantthornton.global/en/insights/indirect-tax-guide/indirect-tax---Kenya/", "guidance"),
                    _src("Kenyans.co.ke, KRA updated list of VAT-exempt and zero-rated goods", "https://www.kenyans.co.ke/news/125329-kra-releases-updated-list-vat-exempt-zero-rated-goods", "secondary")],
    },
}

STRENGTHS = {"official": "Read on an official site", "guidance": "Read in an official guide or an adviser's guide", "secondary": "Read in a news report or summary: check it"}


def get(country):
    return PRESETS.get((country or "").upper())


def apply(db, clinic, user_id):
    """Add the preset's named rates that the clinic does not have yet. Changes nothing else: not the clinic's default rate, not any
    procedure code. Returns {'added': [...], 'kept': [...]}."""
    p = get(clinic["country"])
    if not p:
        raise taxes.TaxError("There is no starting point for this country yet. Ask your accountant which rates to add.")
    have = {r["name"].strip().lower() for r in taxes.list_rates(db, clinic["id"])}
    added, kept = [], []
    for name, kind, pct in p["rates"]:
        if name.strip().lower() in have:
            kept.append(name)
            continue
        taxes.add_rate(db, clinic, user_id, name, kind, pct)
        added.append(name)
    audit.log(db, clinic["id"], user_id, "tax.preset", None, None, clinic["country"])
    return {"added": added, "kept": kept}
