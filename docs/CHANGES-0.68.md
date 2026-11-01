# Files changed from 0.67 to 0.68

Every zip holds the whole project, from version 0.1 onwards. This list shows only what the 0.68 review changed: 25 files edited,
5 files added, out of 574. The exact lines are in `dental-os-0.67-to-0.68.diff` (sent with this version; open it in any
text editor or in VS Code, where added lines show in green and removed lines in red).

## Added

| File | What it is |
|---|---|
| `tests/test_v68.py` | 13 tests for every change below, including the crawl that sends every form hostile values |
| `docs/REVIEW-0.68.md` | The review report: what was checked, found and fixed, and the decisions left for you |
| `docs/CHANGES-0.68.md` | This list |
| `dental_os/services/i18n_fr_staff32.py` | French for the new messages |
| `deploy/Caddyfile.cloud.example` | Web server settings for a cloud server with a real domain (AWS and others) |

## Changed: money

| File | Lines (added, removed) | What changed |
|---|---|---|
| `dental_os/services/billing.py` | +32 −1 | New check at the desk: the same payment within a minute, or a mobile money or card reference already on one of the patient's payments, is refused |
| `dental_os/clinical.py` | +1 −1 | The desk's "Record payment" form switches that check on |
| `dental_os/services/money.py` | +5 | Amounts must be plain numbers. "1e5" and 40-digit numbers now get a message instead of being read wrongly or crashing |
| `dental_os/services/refunds.py` | +7 | A refund cannot be more than the patient actually paid in |
| `dental_os/services/insurance.py` | +7 −1 | A write-off typed by hand cannot be more than is left on the claim |
| `dental_os/services/payerpay.py` | +2 −1 | Passes on whether a write-off came from the payer's own remittance file |
| `dental_os/services/edi.py` | +1 −1 | Marks remittance-file write-offs, so they are posted exactly as the payer sent them |
| `dental_os/static/app.js` | +14 | On every screen, a pressed button waits while its form is sent, so a double click cannot send it twice |

## Changed: records the database protects

| File | Lines | What changed |
|---|---|---|
| `dental_os/db.py` | +42 −1 | Database locks: a signed clinical note and a recorded payment cannot be changed or deleted, on SQLite and PostgreSQL. Also two new columns for operators' two-step login |
| `dental_os/services/compliance.py` | +5 | A new control on the compliance page and in the evidence pack reports those locks (20 controls now) |
| `dental_os/dbpg.py` | +2 | Drops the invisible NUL character from text going to PostgreSQL, which refuses it |

## Changed: messages

| File | Lines | What changed |
|---|---|---|
| `dental_os/services/msgtemplates.py` | +9 | Message wording and campaigns cannot mention money: currencies, amounts, balances, invoices |
| `dental_os/services/inbox.py` | +3 | The same rule for staff replies to a patient's WhatsApp |

## Changed: logins and the app as a whole

| File | Lines | What changed |
|---|---|---|
| `dental_os/__init__.py` | +70 −1 | Every form: a number too large for the database counts as "not a number", the NUL character is dropped, and anything that still slips through sends the person back to the form with a message. Also the `flask operator-2fa` command and the setting that requires it |
| `dental_os/services/platform.py` | +26 −2 | Operators sign in with a code from an authenticator app as well as the password |
| `dental_os/platformviews.py` | +2 −1 | The operator sign-in page passes the code on |
| `dental_os/templates/op_login.html` | +1 | The box for the code |
| `dental_os/version.py` | +1 −1 | 0.68 |

## Changed: crashes found by the form crawl

| File | Lines | What changed |
|---|---|---|
| `dental_os/services/patients.py` | +2 | A huge family-head number gets a message instead of a crash |
| `dental_os/services/statements.py` | +1 −1 | An empty patient choice in a statement run is ignored |
| `dental_os/services/merge.py` | +2 | "Not a duplicate" with no patients chosen gets a message |

## Changed: copies, French and documents

| File | Lines | What changed |
|---|---|---|
| `dental_os/services/offsite.py` | +69 −2 | Off-site copies to Amazon S3 and Lightsail object storage, with AWS request signing |
| `dental_os/services/i18n_fr.py` | +3 −2 | Loads the new French file |
| `deploy/README.md` | +24 | How to run on AWS or any cloud server |
| `README.md` | +30 −2 | The 0.68 section |
| `docs/SECURITY.md` | +7 −2 | The new protections, for the auditor |
| `docs/FEATURES.md` | +2 | Two new rows in the comparison with Dentrix and Open Dental |
