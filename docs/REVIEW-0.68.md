# Review of every version, 0.1 to 0.67 (done for version 0.68)

Written 1 November 2026. The goal: find what would let a real clinic down, or what an auditor or a competitor would point at, and
fix it. Every change has a test in `tests/test_v68.py`, and the whole suite (1,016 tests) was run on SQLite and PostgreSQL.

## How the review was done

1. Each area was read again in the order it was built: logins, patients, diary and money (the first versions, up to 0.9), the clinical record, claims and messages
   (0.10 to 0.30), forms, governance, copies and moving in (0.31 to 0.50), and the platform, security, AI, prescribing, claims,
   FHIR and languages (0.51 to 0.67).
2. Every form in the app was attacked. A test opens every page, reads every form with its real fields, and sends each one three
   times: impossible dates (30 February, 9999-99-99), huge and negative numbers, 20,000 characters of text, an invisible NUL
   character, quotes and script tags. 262 forms were reached. Before the review, five forms crashed on SQLite and ten more on
   PostgreSQL. Now none do.
3. The money and clinical rules were checked for ways wrong figures could get through without a crash, which is worse than a
   crash because nobody notices.

## What was found and fixed

| Area (version) | What was wrong | What it does now |
|---|---|---|
| Payments (first versions) | A double click, or a form resent on a slow connection, recorded the payment twice | Buttons wait while a form is sent, on every screen. At the desk, the same payment within a minute is refused, and one mobile money or card reference can be on only one payment per patient |
| Amounts, everywhere (first versions) | "1e5" was read as 100,000. A 40-digit number crashed the page | Plain numbers only; anything else gets a plain message |
| Refunds (0.42) | A discount on an account owing nothing made a "credit" that the refund screen would pay out as cash | A refund can never be more than the patient actually paid in |
| Insurance (first versions) | A write-off typed by hand could be larger than what was left on the claim, making credit from nothing | Refused. A payer's own remittance file is still posted exactly as sent |
| Messages (first versions, 0.11, 0.33) | The fields kept amounts out, but a clinic could type "your balance is GH₵500" into its own wording or a WhatsApp reply | Message wording, campaigns and replies refuse currencies, amounts, balances and invoices, in English and French |
| Clinical notes (first versions) | A signed note was locked by the app only; anyone with database access could change it | The database itself refuses to change a signed note's words, author or signature, or to delete it |
| Payments (first versions) | The same: locked by the app only | The database refuses to change a payment's amount, date, method or receipt number, or to delete it; it can be voided once |
| Operators (0.53) | The people who run the service for every clinic signed in with a password alone | A code from an authenticator app as well (`flask operator-2fa`) |
| Off-site copies (0.50) | Could go to a folder or WebDAV, not to Amazon's storage | Also to Amazon S3 or Lightsail object storage, with AWS request signing checked against AWS's own published example |
| Cloud setup (0.59) | The install kit assumed a clinic network | A web server file for a real domain and an AWS section in `deploy/README.md` |
| Patient form (first versions) | A huge family-head number crashed it | A plain message |
| Statements (0.27) | An empty patient choice crashed the statement run | Ignored |
| Duplicates (0.40) | "Not a duplicate" with no patients chosen crashed | A plain message |
| PostgreSQL (0.14) | Text with the invisible NUL character, from a copy and paste, crashed ten forms | The character is dropped, from forms and from anything sent to PostgreSQL |
| Every form | A number too large for the database crashed the page | Treated as "not a number", and anywhere it slips through, the person goes back to the form with a plain message |

## What was checked and found sound

- Booking takes a lock on both databases, so two receptionists cannot double-book one chair at the same moment.
- Closed claims cannot be paid twice.
- Uploaded files are checked by their first bytes, not their name, and are shown in a sandbox, so a disguised web page cannot run.
- Every page that needs no login is limited per visitor, protected by a long secret link, or checks the signature of the company
  calling it.
- The code checker (0.67) still finds nothing, and the hostile-text crawl of every page (0.67) still passes.

## Left for you to decide

1. Two-step login is still optional for clinic managers. Requiring it for every manager by default would close the most common way
   in for an attacker. My advice is to require it, with a clinic able to turn it off only in writing.
2. A finished procedure's fee is locked by the app but not yet by the database, as notes and payments now are. Some older tests change
   fees directly, so this needs a careful pass of its own.
3. The money words in messages are English and French. When native speakers check the Spanish, Portuguese and Arabic, add their
   words for balance, invoice and owe.
