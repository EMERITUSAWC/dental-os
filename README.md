# Dental OS, version 0.9

A web-first practice management system for dental clinics. It is built to be sold to clinics in many countries,
starting with Ghana and West Africa, then the rest of Africa, then Europe and the US. It is not built for one clinic.
It was designed from a study of Open Dental's public documentation and then widened with a feature map of the leading UK
and US systems (`docs/FEATURES.md`, which also says what is built, what is next and what is left out on purpose). Nothing
here copies anyone's code, screens, wording, images or licensed code sets.

## What is per clinic, and what is still fixed

Each clinic has its own country, currency, timezone, phone-number rules, ways of paying, opening hours, dentists,
chairs, prices and staff. Nothing is shared between clinics, and the tests prove clinic B cannot see or change clinic A.

Not yet per country (known gaps, in the order they matter for selling abroad):

1. Language. English and French are built for the menus, the sign-in page, the online booking page, a patient's private page,
   the printed documents (invoice, receipt, statement, quote, consent) and the WhatsApp message wording. A clinic, a member of
   staff and each patient can have their own language. The other staff screens (clinical record, reports, settings and so on)
   are still English only, and a French speaker would meet them in English. More languages are a new catalogue file
   (`services/i18n_fr.py` is the model) and a line in `services/i18n.py`. The French has been written carefully but not checked
   by a native speaker or a dental professional. Get that check before a French-speaking clinic relies on it.
2. Tax. A clinic sets a default tax (name, rate, inclusive or on top) and can add other named rates (reduced, zero rated,
   exempt) and give each procedure its own. An invoice with mixed rates shows a line per rate. What tax applies in any country,
   e-invoicing and tax authority reporting are not built and not decided here: that is for the clinic's accountant.
3. Insurance. Payers, cover, estimates and claim tracking are built. Sending claims electronically to a payer or clearing house is not.
4. Procedure codes. They are our own short codes, not the licensed CDT codes. Ready-made packs (everyday dentistry, gum care,
   children, root canals, oral surgery, crowns and dentures, implants, braces, cosmetic, extras) add codes switched off with no
   price, and the whole price list can be downloaded, edited in a spreadsheet and uploaded again. Local code sets (for example NHS
   or BEMA) would be added as options.
5. Phone rules. Each country is a calling code, a trunk prefix and an allowed length. Replace this with a full
   phone-number library before launch in a market.
6. Number formats. A comma is read as a decimal mark only in the form "12,50". Full local formats are not handled.
7. Data-protection and health-record law. Tools are built (see below and `docs/PRIVACY.md`), but the periods and wording in them
   are starting points. A lawyer in each market must confirm them.

Countries in the table now: Ghana, Nigeria, Kenya, Uganda, Tanzania, South Africa, Senegal, Côte d'Ivoire, United Kingdom,
Ireland, Germany, France, United States, Canada. Adding one is a line in `dental_os/services/regions.py`.

## What works now

- Staff logins with four roles (manager, front desk, dentist, assistant). Managers add staff and change settings.
- Patients: add, edit, search by name or phone, family head link, archive (never delete), duplicate warning.
- Consent flags per patient: appointment messages, and offers.
- Diary: day view by dentist and chair, free-slot finder, clash checks (dentist, chair, patient), opening hours.
- Appointment life: booked, confirmed or declined, done, missed or cancelled (kept, with a reason). Waiting list with ASAP.
- WhatsApp: opens WhatsApp with the confirmation or reminder text ready, and a person presses send. Every message is logged.
  Blocked when the patient has not agreed to messages.
- Today screen: who is booked, who to chase for tomorrow, waiting list, birthdays in the next 7 days (offers consent only).
- Treatment: plan work by tooth and surface (FDI numbers), mark it done, drop it, or void it with a reason (manager).
  Finished work is locked. A manager sets the prices under Procedures and prices.
- Billing: work done becomes a charge. Payments by the methods the clinic switches on (cash, mobile money, card, bank
  transfer, insurance), with receipt numbers that run per clinic. Non-cash payments need a reference number. Managers can
  discount, write off or correct, and void a payment with a reason. Nothing is deleted. Family balance, statement with a
  running balance, daily takings.
- Recalls: marking a consultation or a clean as done sets the next check-up (6 months by default). The Recalls screen lists who
  is due with nothing booked, with a WhatsApp come-back message (consent needed), "Not now" and "Stop".
- Reports (manager): no-show rate for confirmed against not confirmed visits, money in and out, who owes most.
- Activity log (managers): who viewed or changed what.
- Medical history per patient, and alerts (allergy, medical, medicine, behaviour) with a level. Allergies and blood thinners
  in the history make alerts by themselves. Alerts show on the patient, the appointment, today's list and the diary.
- Tooth chart (FDI, adult and child teeth): conditions by surface or whole tooth, with planned and finished work shown from the
  treatment list. Removing a condition keeps its history.
- Gum charts: six points per tooth for pocket depth, recession and bleeding, a simplified score for each sixth of the mouth, and
  a comparison with the last chart. Teeth marked missing are skipped.
- Clinical notes from templates. A note is a draft until a dentist signs it, then it is locked. Corrections are dated addenda.
- Consents: forms each clinic edits, recorded with who signed (patient, parent or guardian), how, and when. The wording is
  frozen at signing. Under-18s cannot sign for themselves. Withdrawing keeps the record.
- Files: X-rays, photos, letters. Type is checked by content, not name. They are stored under random names outside the web
  folder and are shown only to signed-in clinical staff of that clinic. Hiding a file keeps it.
- Tools (manager): export all of the clinic's data as spreadsheet files, import a patient list from CSV with a preview, edit
  consent forms. Imported patients never start with permission to message them.
- Manager password reset (a temporary password, shown once, that must be changed at the next login), change-my-own-password, and
  a `backup` command.
- Treatment plans: several options per patient, split into phases, with an editable fee on each line and a printable quote.
  Nothing is charged or booked until the patient accepts. Acceptance records who agreed (a parent for under-18s), turns the
  lines into planned work, and can take a deposit that sits as credit on the account. Declined plans are kept and can be reopened.
  The Plans screen lists plans waiting for a decision with their worth, owner and next follow-up date.
- Memberships: the clinic sets plans (fee, cycle, included services a year, discount on the rest). Members join with a fee that
  is recorded when received. Fees fall due automatically (`flask run-renewals` daily, and whenever the screens are opened),
  failed or late fees show on a list, and a membership lapses 14 days after a missed fee until it is paid. When treatment is marked
  done, an included service is waived or the discount is taken off, and both show on the account. Voiding the work gives it back.
  Membership fees do not count as credit for treatment.
- Instalment plans: split a balance into equal payments, weekly, fortnightly or monthly. Each payment is an ordinary receipted payment.
  Overdue ones show on the Plans screen.
- Payers (insurers, employers, schemes) with their own price lists, and a patient's cover: percentage, yearly maximum, dates.
  An estimate for planned work shows what the payer and the patient should each pay. A contracted payer's price gap is written off.
- Claims and pre-authorisations: made from finished (or planned) work, never twice for the same work. Mark as sent, record what the
  payer pays (part payments, a write-off, close the claim) or reject it and send again. A payer's payment is an ordinary insured
  payment with a receipt, so the balance, reports and statements stay right. The Claims screen shows what payers still owe.
- Numbered invoices for finished work with a tax line from the clinic's settings (tax is copied onto the invoice, so later changes
  never alter it). A manager can void one with a reason. Printable statements. An ageing report (0-30, 31-60, 61-90, 90+ days)
  that takes payments off the oldest charges first and shows how much is with insurers.
- Messages: each kind of message has wording the clinic edits (only safe fields such as name, dentist, day and time can be used,
  never money or treatment). A send list fills by itself: visit reminders at the days you choose, and, if switched on, check-ups
  that fall due, review requests after a visit, birthday greetings and quote follow-ups. Nothing sends by itself: a person opens
  WhatsApp from the list and presses send. Consent is checked when a message is queued and again when it is opened. Cancelled
  visits and withdrawn consent drop messages off the list.
- Replies: staff paste a patient's reply on their Messages tab. It waits in the Replies list until read, and a plain YES can confirm
  the next visit. (Reading replies automatically needs the official WhatsApp Business connection, which is not built.)
- Campaigns: overdue check-ups, patients not seen for a while, quotes not accepted. The rule finds patients, leaves out anyone who
  has not agreed (care messages need appointment-message consent, offers need offers consent) or was messaged recently, and adds
  messages to the send list. The campaign page shows how many wrote back and booked within 30 days.
- Online booking: a public page per clinic (a long random address the manager can change or switch off) that shows real free
  times. What comes out is a request, never a booking. Staff accept it (matching an existing patient or adding a new one) or decline
  with a reason. Hidden-field and rate-limit checks slow down spam.
- A private link per patient (30 days, replaced when a new one is made, cancelled by staff, only a hash stored). It shows the
  patient's own visits, quotes and balance, lets them confirm or decline a visit, accept a quote by typing their name (a parent or
  guardian for under-18s) and fill in a health form. A health form never changes the record until a clinician accepts it.
- Front desk sees alerts but not the clinical record. Assistants write drafts. Dentists and managers sign.
- Key numbers (manager): gross and net production, collections, collection rate, visits, no-show rate, new patients, plan
  acceptance by number and by money, chair time booked, recall health, who owes, production by dentist and by treatment, a
  chart over time, and a comparison with the period before. A CSV download. The meaning of each figure is on the page.
- Groups of clinics: a manager whose clinic is in a group sees every clinic's headline numbers side by side. It is read only,
  never opens another clinic's patients, and never adds money across currencies. Set up with `create-org`, `org-add-clinic`
  and `org-add-user`.
- Lab cases: a job per patient with the lab, tooth, shade, instructions, due date and price. Not sent, at the lab, back, fitted
  (or cancelled with a reason). Late and ready-to-fit lists, a printable job sheet, a remake that links to the old case, the
  fitting visit, and how each lab is doing (turnaround, on time, remakes, spend).
- Stock: items with a supplier, unit, reorder level and cost. Stock in, used, wasted, corrected or counted, always as a trail of
  moves. Soonest expiry is used first, so the screen shows what is left of each batch and what has expired. A to-order list
  grouped by supplier, and an expiry list.
- REST API (version 1) with keys per clinic, read only or read and write: patients, appointments, free slots, balances.
  Keys are shown once and stored hashed. 300 requests a minute per key. No medical or clinical data is ever returned.
- Webhooks: signed messages to an https address when a patient, appointment, payment, invoice, plan or online-booking event
  happens. Private networks are refused (checked again when sending). Failures retry five times, then can be retried by hand.
  Run `flask deliver-webhooks` every minute or two. The how-to page is under Tools, API and webhooks.

### Added in version 0.9

- Two-step login with an authenticator app (the standard 6-digit code, 30 seconds), ten one-time recovery codes, and a QR code made
  by the app itself, so no secret leaves the server. A clinic can require it for everyone. A manager can reset it for a person.
  The secret is stored encrypted with a key made from `SECRET_KEY`: **if you change `SECRET_KEY`, every person's two-step login
  stops working and a manager must reset it** (`flask reset-2fa` does it from the command line).
- Password rules (10 to 128 characters, no common passwords, no runs of keys, not the username or the person's name) and signing out
  after 60 minutes with no activity (`SESSION_IDLE_MINUTES`, 0 turns it off).
- Privacy tools (managers, under Tools): a register of requests from people about their data (30 days to reply by default), a
  copy of one patient's whole record as a printable page or a zip with their files, anonymising a patient (name, contacts and
  free-text notes are removed, treatment and money records are kept, with a reason and a typed confirmation), a register of data
  incidents with the 72-hour reporting clock, records kept longer than the clinic's chosen period, and who has looked at a patient's
  record. The Today screen warns a manager of requests and incidents that are late.
- Tax by treatment (see Tax above).
- Procedure packs and the price list as a CSV (see Procedure codes above).
- French (see Language above).

## Run it

```
pip install -r requirements.txt
export FLASK_APP=dental_os
python -m flask create-clinic           # asks for the name and country; sets currency, timezone and payment methods
python -m flask list-clinics            # shows each clinic's id
python -m flask create-user             # asks for the clinic id, a username, name and a password of 10+ characters
python -m flask deliver-webhooks        # from the scheduler, every minute or two, only if webhooks are used
python -m flask run-renewals            # from the scheduler, once a day: membership renewals
python -m flask build-queue             # from the scheduler, every 15 minutes or so: reminder and recall messages to send
python -m flask reset-2fa               # clears a person's two-step login if they are locked out and no manager can reach them
python -m flask seed-demo               # optional: made-up patients in one clinic so the screens are not empty
python -m flask run                     # http://127.0.0.1:5000
python -m unittest discover -s tests -t .
```

After the first login, a manager sets opening hours, adds dentists and chairs under Settings, and enters prices under
Procedures and prices. Opening hours start as Monday to Saturday, 8 am to 5 pm, which is only a starting point.

Version 0.3 changed the clinics table (country, currency and payment methods are new, and the VIP fields are gone). If you
still have a version 0.2 database, start from a fresh file. Versions 0.4 to 0.9 only add tables and columns, and do it by themselves
when the app starts, so a 0.3 database carries on.

Backups: `python -m flask backup --to /path/to/backups --keep 14` makes a consistent copy of the database. Run it every night
from the server's scheduler, and copy the folder off the machine. X-rays and documents live in `FILES_PATH` (default
`instance/files`). Back that folder up too, or the images are lost even though the database is safe.

Production: `gunicorn wsgi:app`, behind HTTPS (if a proxy sits in front, make it pass the visitor's address so the online booking
limits count per visitor, and make it pass the host name so the private links point at your address), with `COOKIE_SECURE=1`, `SECRET_KEY` set to a long random value, and
`DATABASE_PATH` on a disk that is backed up. Do not run the Flask dev server in production.

## Not built yet (in this order)

Stages 1 to 6 of the feature map are built. Later: official WhatsApp Business API, AI notes and voice charting, a PostgreSQL option,
French (and other languages) for the rest of the staff screens, tax presets for each country (they need checked legal facts),
local procedure-code sets. The full map is in `docs/FEATURES.md`.

## Stack note

The teardown proposed Django and PostgreSQL. This build uses Flask and SQLite because package installs were blocked
where it was built. The business rules (`services/`) and the SQL schema carry over unchanged. Moving to Django is mostly
rewriting `views.py`, templates and the login code. SQLite is fine for a first few clinics. A hosted product with many
clinics should move to PostgreSQL.

## Privacy choices

- No national ID, SSN or insurance numbers are stored. Add them only if a clinic needs them.
- WhatsApp text has name, dentist, day and time only. Never treatment details.
- The activity log records who and what, never message text or notes.
- Every country has its own health-data and privacy law, for example Ghana's Data Protection Act, Nigeria's Data Protection
  Act, Kenya's Data Protection Act, South Africa's POPIA, the UK and EU GDPR, and HIPAA in the US. Each market needs its own
  check, registration where required, and legal advice before real patient data goes in. This README is not legal advice.
- Login: scrypt password hashes, lockout after 5 failed tries in 15 minutes, optional two-step login, CSRF tokens on every form,
  no-store caching, automatic sign-out when idle.
- Data rights, incidents and retention: see `docs/PRIVACY.md`, which is also the brief to give a lawyer.
