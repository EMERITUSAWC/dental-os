# Dental OS, version 0.63

A web-first practice management system for dental clinics. It is built to be sold to clinics in many countries,
starting with Ghana and West Africa, then the rest of Africa, then Europe and the US. It is not built for one clinic.
It was designed from a study of Open Dental's public documentation and then widened with a feature map of the leading UK
and US systems (`docs/FEATURES.md`, which also says what is built, what is next and what is left out on purpose). Nothing
here copies anyone's code, screens, wording, images or licensed code sets.

## Running through internet outages

Put Dental OS on a small computer at the clinic (a mini PC with a UPS is enough), reached by every
screen over the clinic's network, so the internet is not needed for daily work. Schedule these jobs (cron on Linux, Task Scheduler
on Windows):

```
*/5 * * * *  flask net-check
*/5 * * * *  flask send-sms ; flask send-email
30 1 * * *   flask offsite-copy
0 3 1 * *    flask offsite-drill
```

Set `OFFSITE_TARGET` (a folder, or an https WebDAV address with `OFFSITE_USER` and `OFFSITE_PASSWORD`) and `OFFSITE_PASSPHRASE`.
For a viewing copy elsewhere, run a second server with `READ_ONLY=1`, the same `OFFSITE_*` settings, and `flask offsite-load` hourly.

## What is per clinic, and what is still fixed

Each clinic has its own country, currency, timezone, phone-number rules, ways of paying, opening hours, dentists,
chairs, prices and staff. Nothing is shared between clinics, and the tests prove clinic B cannot see or change clinic A.

Not yet per country (known gaps, in the order they matter for selling abroad):

1. Language. English and French are built for every staff screen, the sign-in page, the online booking page, a patient's private
   page, the printed documents (invoice, receipt, statement, quote, consent) and the WhatsApp message wording. A clinic, a member of
   staff and each patient can have their own language. What stays English on purpose: the developer pages (API how-to, integrations,
   audit trail, message setup), the headings of clinical note types (they are read back from saved notes), and anything the clinic
   typed or stored (procedure and category names, consent form wording, recall types, stock categories). A few messages that are built
   from a name or a number in the code also stay English. More languages are a new catalogue file
   (`services/i18n_fr.py` is the model) and a line in `services/i18n.py`. The French has been written carefully but not checked
   by a native speaker or a dental professional. Get that check before a French-speaking clinic relies on it.
2. Tax. A clinic sets a default tax (name, rate, inclusive or on top) and can add other named rates (reduced, zero rated,
   exempt) and give each procedure its own. An invoice with mixed rates shows a line per rate. What tax applies in any country,
   e-invoicing and tax authority reporting are not built and not decided here: that is for the clinic's accountant.
3. Insurance. Payers, cover, estimates and claim tracking are built. US electronic claims (837D, 835, 270/271, 999) are built from
   version 0.23 and move by file download and upload; a direct link to a named clearinghouse is not built yet. Other countries'
   electronic claim formats are not built.
4. Procedure codes. They are our own short codes, not the licensed CDT codes. Ready-made packs (everyday dentistry, gum care,
   children, root canals, oral surgery, crowns and dentures, implants, braces, cosmetic, extras) add codes switched off with no
   price, and the whole price list can be downloaded, edited in a spreadsheet and uploaded again. US clinics type in the CDT code for
   each procedure from their own CDT licence (version 0.23). NHS England bands are set per procedure (version 0.24). Other local
   code sets (for example BEMA in Germany) would be added the same way.
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
  that fall due, review requests after a visit, birthday greetings and quote follow-ups. By default nothing sends by itself: a person opens
  WhatsApp from the list and presses send (the API in version 0.11 can change that, if the clinic chooses). Consent is checked when a message is queued and again when it is opened. Cancelled
  visits and withdrawn consent drop messages off the list.
- Replies: staff paste a patient's reply on their Messages tab. It waits in the Replies list until read, and a plain YES can confirm
  the next visit. (Replies are read automatically only if the official WhatsApp Business API is connected, see version 0.11.)
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

### Added in version 0.63

- A live clearinghouse connection for US clinics (Electronic claims, Set up the connection). The first is Stedi, built from its public
  API reference. Claims go one at a time: if the clearinghouse finds a problem, that claim is not marked as sent, and its reason is
  written in the connection log on the Electronic claims screen. Stedi's first acknowledgement comes straight back and is shown on
  the claim. A cover check asks the payer straight away and saves the answer on the patient's cover.
- Later acknowledgements (277CA) and remittances (835) are fetched every half hour by `flask fetch-claims` (in `deploy/dental-os.cron`),
  or with the Fetch button. A claim the payer returns before processing is marked rejected and listed again, to fix and send again.
  A remittance waits on the screen for a person to check it and post it, exactly as an uploaded one does. Nothing is read twice.
- Test and live are kept apart: the receiver ID must be STEDITEST for test files and STEDI for live ones, and the clinic uses
  Stedi's test key with test files. Not yet tried against Stedi itself: the first run should be with a test key and Stedi's test payer.

### Added in version 0.62

- A pharmacy check on every prescription. Each one printed now carries a QR code and a short link. A pharmacist opens it on any
  phone, with no account, and sees whether it is genuine, still valid, cancelled by the dentist or already dispensed, then records the
  pharmacy, the pharmacist and (if they wish) their licence number. Once dispensed it cannot be dispensed again, so a forged or
  photocopied paper is caught. The page shows only the medicines, the prescriber and the patient's initials and year of birth. The
  link is made from the server's secret key, so it can be printed again, and a stolen copy of the database cannot make links.
  Prescriptions stay valid for 30 days, 28 with a controlled drug (the UK rule for Schedule 2 to 4); change these where the law differs.
- An electronic copy of each prescription in FHIR R4 (a Bundle of Patient, Practitioner, Organization and one MedicationRequest per
  medicine), the format national health systems and e-prescribing networks read.
- Sending to an e-prescribing network (Settings, E-prescribing): a dentist sends the FHIR copy to the network's https address with
  the clinic's key, and the network's answer is kept. Networks have their own rules and certification: DoseSpot and Surescripts in
  the US (including the extra rules for controlled drugs), the NHS Electronic Prescription Service in England (Spine, smartcards and
  dm+d codes). A short adapter is written for each once there is an agreement. Ghana's national e-pharmacy platform publishes no way
  for clinics to send prescriptions yet; the pharmacy check works with any pharmacy there today.

### Added in version 0.61

- Dictation in clinical notes, off until a manager switches it on (Settings, voice). Each note box and the AI shorthand box get a
  Dictate button. Speech is turned into words on the device only: notes can hold names and health details, so the online speech
  service a clinic may allow for gum charting is never used for notes. Where the browser cannot listen on the device, there is no
  button, and the keyboard's own microphone key still works. Say "full stop", "comma", "new line" or "new paragraph" for punctuation.
  Dictated shorthand can go straight into Draft with AI.
- Chairside mode for a tablet at the chair (Chairside in the menu, for dentists and nurses). Pick the chair once; the screen shows
  that chair's day: who is in the chair, waiting, still to come and finished. For the patient in the chair it shows the medical
  alerts first, then big buttons for the medical history, tooth chart, gum chart (with voice), treatment, a new note, X-rays and
  prescriptions, and Seat and Finished, which move the patient on the check-in board as the front desk sees it.
- Voice gum charting itself was built in an earlier version and is unchanged.

### Added in version 0.60

- A picture check on every X-ray as it comes in, on the clinic's own computer with no internet: blurred, too dark, burnt out, flat or
  thumbnail-sized pictures are flagged on the record and in the viewer, so they are retaken while the patient is still in the chair.
  It looks only at the picture, never at the teeth. The limits are set from test pictures and will be checked against real sensors
  in the pilot.
- A second look from an X-ray AI company the clinic chooses (Settings, X-ray AI). Dental OS does not read X-rays itself: software that
  finds disease on X-rays is a regulated medical device in many countries. Only the picture is sent, as a fresh PNG without the DICOM
  tags or photo details that hold names and dates. What comes back are suggestions by tooth, drawn over the X-ray; a dentist agrees
  or rejects each one, and nothing goes on the chart or a plan by itself. Off until a manager switches it on, with the clinic's
  confirmation that it may send X-rays to that company. The key is stored encrypted. 120 reads an hour at most, as a cost guard.
- Each company has its own way of being asked. Dental OS has one plain format of its own (in `services/xrayai.py`); a short adapter
  per company is written once the clinic has an account with it.

### Added in version 0.59

- Ready to go live (Settings): what Dental OS checks by itself (dentists, chairs, prices, patients moved in, two-step login for
  every manager, a recent off-site copy, a passed restore drill, holidays, https) and what only a person can confirm (hours,
  prices checked by a dentist, message wording, staff trained on a test patient, the old system kept running, who to call). A
  manager ticks those, and their name and the time are kept. `flask doctor` prints the same list from the command line.
- Side by side with the old system: each evening the manager types in the old system's takings, appointments and new patients for
  the day, and Dental OS shows its own figures next to them. Ten matching working days in a row is the sign the old system can go.
- "Report a problem" at the bottom of every page. Staff say what went wrong and how much it gets in the way; the page it came from
  is kept without any search words. Staff see their own reports, managers see all of them and mark each fixed, not a fault or later.
  A clinic can choose to let Dental OS support read a report.
- A page that breaks shows a reference such as `E-7K2Q9X`. The error list keeps only the screen, the kind of error, the line of
  code and the time, never what was typed or searched; the full details go to the server log under the same reference. Managers
  see their clinic's list; the operator console has Errors and problems for every clinic.
- `deploy/`: an install script for a clinic computer on Ubuntu Server 24.04 LTS, the service file, the scheduled jobs, a Caddy
  file for https on the clinic network, and a short guide (`deploy/README.md`).

### Added in version 0.58

- Accessibility: every screen is checked by the tests (`tests/a11y.py`) for form fields without a label, images without a
  description, buttons with nothing to read out, a missing title or language, more than one main heading and repeated ids. A test
  opens every screen as a manager and as the front desk, plus the login, sign-up and online booking pages.
- A "Skip to the main content" link for keyboard users, a clear ring on whatever the keyboard is on, and no animation for people who
  have asked their device for less movement. Text colours meet the WCAG AA contrast level (4.5 to 1) in light and dark; the amber
  used for warnings was too light and is darker now.
- Restore drills are written down. Settings, Copies shows the last good drill and says when one is due again (after 35 days). A copy
  that opens but holds far fewer patients than the live system does not pass, so copies made from the wrong database are caught.
- Opening the two-step code page while already signed in no longer signs you out.

### Added in version 0.57

- Speed with a large clinic. `tools/bigclinic.py` fills a scratch database with 20,000 made-up patients, 100,000 appointments,
  60,000 treatments and 30,000 payments, then times every screen. Before this version the recall list took 53 seconds, the ageing
  report 75 seconds, likely duplicates 87 seconds, records kept too long over four minutes, and the reports page did not finish.
  Every screen now opens in under a second at that size.
- What changed: indexes on every table that holds a patient's records (a test now fails if a new one is added without), an index for
  "has this patient got a booking coming up", all balances worked out in one pass instead of one patient at a time, likely duplicates
  grouped by phone and by name and birthday instead of comparing every pair, and the recall list in pages of 200.
- The tests compare each faster way with the old one on the same records, so the answers have not changed.

### Added in version 0.56

- A security review. Every query that is built from pieces was checked: the pieces are table and column names from the code, never
  from a form. Addresses to go back to after a form are now checked in one place, which also refuses a backslash (browsers read
  `/\evil.example` as another site). Spreadsheet exports all guard against cells that start a formula, including the Ghana claim batch.
- Wrong logins are limited per network address as well as per username (30 in 15 minutes), so one address cannot try a common
  password on many usernames. Behind a proxy, set `TRUST_PROXY=1` (or the number of proxies) so each visitor's own address is used;
  before this the app saw every visitor as the proxy.
- With `COOKIE_SECURE=1` the server tells browsers to use only https for a year (HSTS). Pages also refuse the camera, microphone and
  location by default.
- Tests that cover the whole app: every page needs a login unless it is one of the public ones; a manager at one clinic opens every
  page with another clinic's record numbers and never sees its patients; the same manager sends every form at the other clinic's
  records and nothing there changes. Each has a check that the same steps, done by the right clinic, do reach the records, so the
  test can see a leak if one appears.

### Added in version 0.55

- Works on a phone: the menu folds behind a Menu button, tables scroll sideways inside their card, forms stack, and buttons are big
  enough to press. Checked at 390 pixels wide on the main screens. It can be added to a phone's home screen.

### Added in version 0.54

- Plans and bills (the operator console, Plans): each plan has a monthly price in each currency it is sold in, and an optional limit on
  dentists. A clinic is put on a plan; `flask bill-clinics` (once a day) makes each month's bill when the paid-up period runs out, due
  in 7 days, and marks late clinics overdue.
- A clinic's manager sees its state and bills under Settings, Billing, and pays by card or mobile money through the service's own
  Paystack (Ghana cedis) or Stripe account (`PLATFORM_PAYSTACK_SECRET`, `PLATFORM_STRIPE_SECRET`); the payment company is asked to
  confirm the amount and currency before the bill is marked paid. A bank transfer is marked paid by the operator with its reference.
- Overdue for more than 14 days: the clinic can look at everything and take a copy of its data, but cannot change anything until it
  pays. Tax on the service is left to the service company's accountant.

### Added in version 0.53

- Sign-up (`ALLOW_SIGNUP=1`): a clinic signs itself up for a 30-day free trial with its first manager, with the same password rules as
  everywhere, five tries an hour from one address. `flask create-clinic` uses the same setup.
- Clinic states: trial, active, overdue, paused and closed. A paused or closed clinic's staff can still sign in, read why, and download a
  full copy of the clinic's data: the records belong to the clinic, never to us.
- The operator console (`/operator`, people added with `flask create-operator`): each clinic's country, state, plan, and counts of
  patients, staff and open bills. Patient names and records are never shown there. Every operator action is logged.
- Support access (Settings, Support access): the clinic's manager lets support look for 1, 4, 24 or 72 hours. Only then can an operator
  open the clinic, and only to look: changes are refused, every page they open is in the clinic's Activity log as Dental OS support, and
  the manager can end it at any time.
- Needed before selling: the terms of service and the data processing terms that sign-up asks clinics to agree to. They are legal
  documents for a lawyer in each country sold in, and are listed under Waiting on an outside party.

### Added in version 0.52

- Diagnoses on claims: each finished treatment can carry an ICD-10 code, picked on the treatment list. The starter list is the dental
  block of the WHO's ICD-10 (5th edition), K00 to K14, as published by NHS England's classification browser; clinics add other codes.
  The titles stay in English: the official French edition should come from its publisher.
- What a payer covers: tick the treatments a payer pays for, and set it to need a diagnosis on every line. Treatment not on the list is
  estimated at nothing, with a note, everywhere estimates appear; a claim missing a diagnosis is refused before it goes.
- The NHIS (Ghanaian clinics, Payers): adds the payer as a contracted government scheme that needs diagnoses. The clinic ticks its own
  treatments against the NHIA's benefits list, which for oral health covers pain relief (incision and drainage, tooth extraction and
  temporary relief) and dental restoration (simple amalgam fillings and temporary dressing), and excludes dentures.
- Monthly claim batches (Claims, Monthly batches): one payer's draft claims for one month, with a printed summary to sign, a claim form
  for each claim, and a CSV for the payer's spreadsheet; marking the batch sent submits every claim in it.
- Not built: the NHIA's own e-claims XML file. Its published standard could not be read from here, and a guessed government format
  would be rejected. It is listed under Waiting on an outside party.

### Added in version 0.51

- A read-only viewing copy: a second server set to `READ_ONLY=1` loads the newest off-site copy (`flask offsite-load`, say every hour)
  so an owner abroad can look at the diary, records and numbers. It refuses every change and says how old the copy is. Two copies that
  can both be changed are not offered on purpose: merging them is how systems lose data.
- Every screen says when the internet is down (`flask net-check` every few minutes), and that everything keeps working while
  WhatsApp, SMS and email messages and online payments wait.

### Added in version 0.50

- Working through internet outages: run Dental OS on the clinic's own computer, reached over the clinic's network. Only messages,
  online payments and the off-site copy need the internet; messages wait on the send list and go out when it is back.
- A locked off-site copy (`flask offsite-copy`, every night): the whole database and every patient file in one file, locked with a
  passphrase (PBKDF2, 390,000 rounds, then Fernet), sent to a folder (a USB disk, or a Google Drive, OneDrive or Dropbox folder) or a
  WebDAV address over https, then read back to check it. The last 14 are kept in a folder. Managers see a warning when no good copy has
  been made in a day. Settings, Copies and the internet shows every try.
- Restore drills: `flask offsite-drill` fetches the newest copy, unlocks it and checks the database (SQLite's integrity check, and counts
  of patients, appointments and payments) without touching anything live; `--to` unpacks it into another folder to look at.
- If the passphrase is lost, the copies cannot be opened by anyone, including us. Keep it written down, away from the computer.

### Added in version 0.49

- Dentrix, other systems and spreadsheets: files named for what they hold (patients, providers, treatment, appointments, payments,
  adjustments, balances, recalls, notes), with plain column names and their common other names. On the preview each file's kind and
  every column can be matched by hand. Opening balances set each patient's balance to the old system's figure.
- `flask move-in --clinic 1 --user manager --file export.zip [--teeth fdi] [--import]` for clinics too large for the screen.
- I could not find a published description of Dentrix's export files, so Dentrix is read through this matching screen rather than a
  ready-made layout. The first real Dentrix export will show whether a ready-made layout is worth adding.

### Added in version 0.48

- Moving in from Open Dental (Tools, Move in from another system): its tables exported as CSV files in one ZIP (patient, provider,
  procedurecode, procedurelog, appointment, operatory, payment, adjustment, recall, commlog), recognised from their columns. Status
  codes follow Open Dental's documentation. Universal tooth numbers become FDI.
- The preview runs the whole import and undoes it, so its numbers are exactly what will happen: new records, records already here,
  what is left out on purpose (deleted records, existing work, conditions, referred-out work) and lines that cannot be read.
- Each record keeps a link to its old number, so the same files, or newer ones the night before going live, can be brought in again
  without doubling anything. Patients with no readable phone number come in with the phone empty and are counted. Consent to messages
  is never brought across. Payments in the old system that were refunds become corrections. The upload is deleted once done.

### Added in version 0.47

- Closed days (Settings): public holidays and the clinic's own closures, each with a name. Nobody can be booked on them, the online
  booking page offers no times, the diary says why, and working-day counts (the complaint acknowledgement clock) skip them. Days that
  fall on the same date every year can be copied to the next year. The list is not filled in for the clinic: public holidays differ by
  country and some are declared only weeks ahead, so take them from the official notice (in Ghana, the Ministry of the Interior).

### Added in version 0.46

- Auto codes (Settings): staff pick a treatment by type, such as Composite filling, with the tooth and surfaces, and the right code is
  chosen from the number of surfaces and whether it is a front or back tooth. The most exact rule wins; if none fits, the screen says
  what is missing.

### Added in version 0.45

- Treatment kits (Stock, Kits): what each treatment uses from stock. Marking the treatment done takes those amounts off the shelf;
  voiding it puts them back. Clinical work is never held up: if the shelf shows too little, what is there is taken and the shortfall is
  listed for the manager. A report shows what each treatment cost in materials against its fees.

### Added in version 0.44

- Aftercare sheets (Tools, and an Aftercare tab on each patient): what to do after each kind of treatment, in English and optionally
  French, linked to treatments on the clinic's list. A dentist must approve each sheet, and again after any change to the wording.
  Staff print the sheets that fit the patient's recent or planned treatment, in the patient's language, and the system notes it.
- One starter sheet, for tooth extractions, drawn from a published NHS hospital leaflet (UCLH). It comes unapproved: the clinic's
  dentist reads it, changes what their practice does differently, and approves it. Sheets are printed only; WhatsApp, SMS and email
  messages still never carry treatment details.

### Added in version 0.43

- Close the books (Reports): a manager sets the date the books are closed up to. Payments, refunds, voids and finished or voided
  treatment dated on or before it are refused. It can be moved forward freely; opening closed days again needs a reason. Every change is
  kept.

### Added in version 0.42

- Refunds: a manager can pay back credit a patient holds (by cash, mobile money, card or bank transfer, with a reason and reference),
  never more than the credit, so a refund never undoes a payment that paid for work. Each refund is numbered, shows on the account and
  statement, comes off the day's takings and the cash-up's expected till, comes off collections in reports and associate pay (the most
  recent money first), and is in the accountant's export. A wrong refund is voided with a reason.
- A list of patients in credit, with the total.

### Added in version 0.41

- Not seen for a while (from Recalls): patients seen before but not for 6, 12, 18 (the default), 24 or 36 months, with nothing booked
  and nothing on the waiting list. It shows the last visit, the last contact and any treatment planned but never done.
- One click adds a "we have not seen you for a while" message to the send list, for one patient or everyone who agreed to messages,
  at most once a month each. The wording is editable like every other message and has a French starter. It never mentions treatment
  or money. Patients can be put aside for a while with a reason (moved away, sees another dentist, asked not to be contacted).

### Added in version 0.40

- Duplicate records (Patients, managers only): pairs with the same phone, or the same name and date of birth, side by side with what is
  on each. A pair can be marked as different people so it is not shown again.
- Merging moves everything from the duplicate to the record kept, from every table that holds a patient (read from the schema, so a
  new table is never missed), repoints family links, and fills empty details. A second medical history and a recall of a kind the kept
  record already has stay on the old record and are listed to look at. The duplicate is archived and marked as merged. A merge cannot be
  undone, so each is kept in full: what moved, how many rows, what stayed.

### Added in version 0.39

- Fee schedules (Settings): price lists besides the standard one, such as staff and family, a company's workers or students. Each has
  its own prices where written, and otherwise the standard price less a set percentage. A manager puts a patient on one from their page.
- Treatment and plan lines for that patient take the schedule's price when they are added. Prices already planned or done never change.
  Insurers' agreed fees and membership discounts work as before.
- The patient page now shows SMS as the way to reach a patient who chose it (it showed WhatsApp before).

### Added in version 0.38

- Registration, indemnity and CPD for every staff member (Staff, Registration and CPD). The manager records who each person is
  registered with, the number, their title, and when registration and indemnity cover run out; both are flagged 60 days ahead.
- Everyone keeps their own CPD under My CPD: title, date, hours, who gave it, a certificate reference, whether it is verifiable, and the
  GDC development outcomes (A to D). Wrong entries are struck out with a reason, never deleted.
- Two ways to measure it: the UK GDC scheme (verifiable hours over a five-year cycle: 100 for dentists; 75 for dental therapists,
  hygienists, orthodontic therapists and clinical dental technicians; 50 for dental nurses and dental technicians; and at least 10 hours
  in any two years), or a yearly target the clinic sets. I could not confirm the Ghana Medical and Dental Council's yearly figure from
  its public pages, so for Ghana the clinic types the target from the council's own notice.

### Added in version 0.37

- A complaints register (Tools, Complaints and incidents). Anyone can write one down, from the patient's page or the Today page. The
  manager acknowledges it (due three working days after it came in, Monday to Friday; public holidays are not taken off), agrees a date
  for the full answer, adds notes, records the answer and outcome, and closes it with what the clinic learned. Late ones are flagged.
  Complaints stay out of the clinical record.
- A patient safety incident register: sharps injuries, wrong tooth, medical emergencies, reactions, falls, equipment and sterilisation
  failures, near misses and more, with the harm done, what was done at once, the review (why it happened, what will stop it), and who
  it was reported to. For moderate harm or worse to a patient, the screen sets out the duty of candour as it applies in England and
  will not close the incident until telling the patient in person, the apology and the written follow-up are recorded.
- Twelve-month counts by kind, outcome and harm, and how many complaints were acknowledged and answered on time.

### Added in version 0.36

- Associate pay (Reports, Associate pay; managers only). Each provider's terms: a percentage of gross production, net production
  (less discounts and write-offs) or collections, the share of lab bills they bear, and an optional amount per day worked. Terms have a
  start date, so changing them never rewrites past pay.
- Collections follow one stated rule: each patient's payments and discounts are matched to their oldest completed work first, and
  count on the later of the day paid and the day the work was done, so a deposit counts once the work is done. Lines with no provider
  (late fees, for example) stay with the clinic.
- A pay run keeps a copy of every figure, takes extra lines (a bonus, an advance), is approved (then it cannot change, and no second
  approved run may cover the same days), and is marked paid with the date and reference. It prints as a pay statement.

### Added in version 0.35

- An implant and materials register on each patient's Implants tab: implants, abutments, implant crowns, bone grafts, membranes,
  screws and anything else that must be traceable, with the maker, product, catalogue code, lot number, size, expiry, tooth, date and
  who placed it. Expired items are refused. Removing one keeps the line with the date and reason.
- A printable implant card for the patient, in the patient's language.
- Under Tools, the register finds every patient with a maker, product or lot (and a date range). From a search, staff open a recall:
  the patients found are kept as a list to work through, marking each as told, then the recall is closed.

### Added in version 0.34

- A form builder under Tools, Patient forms: a manager makes the clinic's own forms (short and long answers, yes or no, one or several
  choices, dates, numbers, a typed-name signature and headings, up to 40 questions). Changing the questions makes a new version.
- From a patient's Forms tab staff make a one-time private link (1 to 60 days), sent by WhatsApp, shown as a QR code, or opened on the
  desk tablet. Only a hash of the link is kept. The page needs no login, is in the patient's language, and checks every answer.
- Filled-in forms wait in a review list until a staff member files them on the record, with a note. Each keeps the questions exactly
  as the patient saw them. Nothing is deleted.

### Added in version 0.33

- SMS as a third channel, through the clinic's own Hubtel, Africa's Talking or Twilio account (Settings, SMS, with a test SMS).
  Patients can choose SMS as their preferred contact. The send list has a button to text everyone who prefers SMS, statements can be
  texted (the private link only, never amounts), and `flask send-sms` sends the list on a timer when a manager allows it.
- The key is stored encrypted and never shown again. Offers carry a STOP line. Long texts are counted in segments before sending.
- How it was tested: against a stand-in that answers the way each company's sending API is documented. **Not yet tried against the
  live companies.** Hubtel's reply fields could not be fully confirmed, so send a test SMS first. Many countries
  need the sender name registered with the SMS company before it works.

### Added in version 0.32

- Saved cards, through the clinic's own Paystack or Stripe account, off until a manager switches them on under Settings, Online
  payments. On the pay page the patient can tick to keep the card, and separately to have monthly ortho or membership fees charged
  to it. The words they agreed to, in the language they saw, and the time are kept with the card. Only the payment company's token is
  kept here, encrypted, with the brand, last four digits and expiry; the card itself never touches this system.
- Staff can charge a saved card for an amount the patient owes, after ticking that the patient was told. Every attempt is kept,
  paid or failed, with the payment company's reason. Staff can stop automatic charges or remove a card (removing it stops all use at
  once); only the patient can agree to automatic charges.
- `flask run-autopay` once a day (after `post-ortho-fees` and `run-renewals`) charges due membership fees and new ortho monthly
  amounts on cards the patient agreed to use that way. Each amount is tried once; a failure is left for staff, never retried.
- How it was tested: against a stand-in that answers the way Paystack's charge_authorization and Stripe's off-session payment
  intents are documented, including a decline and a bank asking the patient to approve. **Not yet tried against the live companies.**
  Do a full test with test keys and test cards first, and check the clinic's card terms and local law before switching it on.

### Added in version 0.31

- Email as a second channel, through the clinic's own mail account (SMTP, set up under Settings, Email, with a test message). Patients
  whose preferred contact is email get their send-list messages by email, by hand or by the scheduled `flask send-email`. Statements
  can be emailed, one at a time or a whole run: the email carries the private link, never the amount. The same consent and wording
  rules as WhatsApp apply, the password is stored encrypted, and addresses and subjects are checked so no extra mail headers can be added.

### Added in version 0.30

- Permission groups. The four roles still set the most a person can do; a manager can now make groups that narrow it, area by area
  (diary, registering patients, clinical record, plans, taking payments, voids and discounts, insurance and statements, memberships,
  patient messages, reports, lab, stock, sterilisation, NHS, exports, time cards). For example "Front desk, no money". Every screen
  belongs to one area and the check runs before it opens; menu items for closed areas are hidden. A group never widens a role, and
  managers are never put in one, so a clinic cannot lock itself out. Under Staff, Permission groups.

### Added in version 0.29

- Team messages: to one person or to everyone, optionally about a patient (sent from the patient's page, where they also show).
  Unread messages show as a number in the menu. Messages cannot be changed once sent.
- An equipment register: each item with maker, model, serial number, where it is, warranty and how often it is serviced. Services,
  repairs, checks and faults are logged with who did the work and the cost. The Today screen shows managers what is due or faulty.
  Under Tools, Equipment.

### Added in version 0.28

- A report builder for managers: pick what to report on (patients, appointments, treatment, payments, claims, lab cases), the
  columns, up to four filters, an optional grouping with a count, total, average, smallest or largest, and the order. Save it, let the
  team run it, print it, or download it as a spreadsheet. There is no free SQL: every data set, column and filter is from a fixed
  list, values are passed as parameters, every query is held to the clinic, and clinical data sets are only for clinical staff.

### Added in version 0.27

- Statement runs: everyone who owes at least a set amount, optionally only where some is older than a set number of days, leaving
  out anyone sent a statement recently. Print them all at once (one per page) or send each by WhatsApp. The message never says the
  amount; it carries the patient's private link, where the balance and Pay button are.
- Late fees, off until a manager switches them on: a fixed amount or a percentage of what is overdue, after a set number of days,
  above a set amount, at most once in a set number of days, with an optional cap. Never on what insurers are expected to pay, never
  for a patient on an instalment plan, and only added when a manager makes a run and ticks the box. Whether a clinic may charge them
  depends on local law and its terms with patients: check both first.

### Added in version 0.26

- Orthodontics. A case per patient with the appliance, arches, molar class, overjet, overbite, crowding and expected length, from
  planned to in treatment, retention and finished (or stopped). An ortho chart line at each visit: wires, elastics, aligner number,
  breakages, notes and when to come back. The ortho board shows every open case, months in treatment against the estimate, and late
  visits.
- The ortho contract: total fee, deposit charged today, then equal monthly amounts (the last one takes any rounding). Each amount goes
  on the account on its due date as one line, so statements, receipts and reports work as before. Run `flask post-ortho-fees` once a
  day (amounts also post when the ortho screens are opened). Stopping a contract or a case stops the amounts not yet due.

### Added in version 0.25

- DICOM X-rays are shown in the browser. A DICOM file on a patient's record opens in the image viewer and in X-ray layouts like any
  photo. The picture is made from the file's own settings (rescale, window, MONOCHROME1 the right way round, 8, 12 or 16 bit), once,
  and kept beside the original, which never changes. The viewer shows the name, ID, date and maker inside the file, measures in
  millimetres straight away when the file gives its pixel size, and warns when the name inside the file is not this patient's.
  Uncompressed DICOM (implicit or explicit VR little endian, and deflated) is read by our own code. JPEG and JPEG 2000 compressed
  DICOM needs Pillow on the server (`pip install Pillow`). JPEG lossless, RLE and big-endian files are not shown; the screen says so.
- The imaging bridge. `tools/imaging_agent.py` runs on the computer the X-ray sensor is plugged into and needs only Python 3. Staff
  press Take X-rays now on the patient and pick that computer; every new image the sensor software saves into the watched folder is
  filed on that patient as an X-ray, for the next 30 minutes (each image adds 10 more). A DICOM file carrying the patient's number
  (DOS-123) in its Patient ID is filed on that patient at any time. Sent images move into a "sent" folder by date; images that cannot
  be filed move into "failed" with the reason. Nothing is deleted. It can open the sensor software for the waiting patient when that
  software takes a patient on its command line. Set-up is under Tools, Imaging bridge, which also shows whether each agent is running.
- How it was tested: the agent runs in the tests against the app itself, with real folders and files. The DICOM reader is tested
  with files built to the DICOM standard's layout, including a nested sequence and compressed frames. **Not yet tried with files from
  a real sensor**, and an independent check with another DICOM library could not be run where this was built (package downloads were
  blocked). Try a few real X-rays from each sensor before relying on it.

### Added in version 0.24

- NHS dentistry in England. Courses of treatment with the lead dentist and their performer number, why the patient pays or is free
  (under 18, under 19 in full-time education, pregnant or a baby in the last 12 months, the benefits on the NHS list, HC2, HC3 with its
  limit) and whether evidence was seen. The course suggests its band from the NHS work done (each procedure is marked with its band in
  NHS set-up); the dentist picks 2a, 2b or 2c. Finishing a course puts the patient's charge on their account as one line, works out
  the UDAs, and starts the 62-day clock for the FP17.
- The year's contract: contracted UDAs and UDA value, UDAs done against what an even pace would give by today, by month and by
  dentist, and urgent care courses against the 2026/27 requirement (11 for each £10,000 of contract value, for contracts of 100 UDAs
  or more). Urgent care UDAs come from the £75 credit at the contract's UDA value.
- Figures used, from NHS England for 2026/27: Band 1 £27.90, Band 2 £76.60, Band 3 £332.10; UDAs 1, 3, 5, 7 and 12; fluoride varnish
  sub-band 0.5; denture repair 2. They change each April and are kept as data the clinic updates, with the date and source.
- The FP17: a check list of what is missing (address, sex, declaration, evidence), a printable FP17 sheet, a spreadsheet of all of
  them, and the sent, accepted and rejected record with the NHSBSA reference. **Sending FP17s straight to the NHSBSA needs this
  software to be accredited by them.** Until it is, the FP17 is prepared here and sent through the clinic's own NHSBSA route.
  Scotland, Wales and Northern Ireland have their own rules and are not covered.

### Added in version 0.23

- US electronic claims in the HIPAA X12 5010 formats. Claim files (837D) for claims and predeterminations, built from the claims
  already in the system: billing and rendering NPIs, subscriber and patient loops, CDT codes, teeth in US numbering with surfaces,
  dates of service. Before a file is made, each claim is checked and anything missing is listed in plain words.
- Remittances (835) are read, matched to our claims, shown for checking, then posted as one payer payment: contract adjustments (CO)
  written off, what the patient owes (PR) left on their account, denials marked rejected with the payer's codes. The same remittance
  cannot be posted twice. Cover checks (270) are made from the cover, and the payer's answer (271) is saved on the cover in plain words.
  The clearinghouse's receipt (999) marks the claim file accepted or rejected.
- What the clinic brings: its clearinghouse submitter and receiver IDs, practice and dentist NPIs (the check digit is verified), tax ID,
  taxonomy code, payer IDs, and a CDT code for each procedure from its own CDT licence (CDT is licensed by the ADA, so none are shipped).
- Files go to and from the clearinghouse by download and upload, which works with any clearinghouse. Files are marked as tests
  until a manager switches them to live. **Not yet tried against a real clearinghouse.** Send test files first; the clearinghouse's
  999 and 277 replies show what it accepts.

### Added in version 0.22

- Sterilisation and instrument tracking. Sterilisers by type; their daily, weekly and yearly tests (automatic control test, steam
  penetration, air leakage, spore test, yearly validation), with a spore test able to wait for its result. A clinic setting stops a
  cycle being recorded until that machine's daily tests have passed.
- Each cycle with what the machine showed, whether it reached its numbers and whether the indicators changed. Each pack from the load
  gets its own code and use-by date, and labels with a QR code print from the cycle. Scanning or typing a pack code on the patient
  records which instruments were used on whom, and refuses any pack that is used, out of date, recalled, from a failed cycle, or held.
- Loads with implants are held until their spore test passes. A failed test takes the steriliser out of service. A failed spore test
  also recalls every pack made on it since its last passing spore test, and lists the patients those packs were already used on.
  Back in service after a failed spore test needs one passing spore test, or three in a row after two failures (the CDC's advice).
- The Today screen warns about missing checks, machines out of service, open recalls and out-of-date packs.

### Added in version 0.21

- Phone calls through the clinic's own Twilio account. Calls to the clinic ring the desk phone and every signed-in screen shows who is
  calling, with a link to their record. A missed call becomes a call-back task. Click to call rings the desk first, then the patient,
  who sees the clinic's number. Every call is in the call log and on the patient's contact log. Messages from Twilio must carry
  Twilio's signature (checked against Twilio's own published example). Callers hear the clinic's language. Calls are not recorded.
  **Not yet tried against the live Twilio service.**

### Added in version 0.20

- Online payments through the clinic's own account with Paystack (cards and mobile money in Ghana, Nigeria, Kenya and South Africa)
  or Stripe (cards in the UK, Europe, the US and many other countries). The money goes straight to the clinic; this system never holds it.
- Set-up under Settings, Online payments: paste the company's secret key (stored encrypted, never shown again, never logged) and, for
  Stripe, the webhook signing secret; copy the clinic's webhook address into the company's dashboard. A test key works against the
  company's test system, so the whole flow can be tried with test cards first. Check the key works with one click.
- Pay links: from a patient's account, ask for any amount (the balance by default). The link page has the link, a WhatsApp message ready
  to send, and a QR code to scan at the desk. The patient's page shows the clinic, their first name, the amount and a Pay now button,
  in their language. Links last seven days and can be cancelled.
- A payment counts only when the company confirms it: by its signed message to the webhook (Paystack: SHA-512 signature with the secret
  key; Stripe: timed SHA-256 signature, refused after five minutes), or by our own look-up when the patient comes back. The amount and
  currency must match exactly, or the link is marked failed and nothing is recorded. A confirmed payment becomes one normal payment and
  receipt on the patient's account (card or mobile money, as the company reports it), however many times it is confirmed.
- The patient's private link gets a Pay now button for the balance when online payments are on.
- Set `PUBLIC_URL` to the address patients reach (for example https://clinic.example.com) so pay links point there.
- How it was tested: against a stand-in that answers the way Paystack's and Stripe's published APIs do. **Not yet tried against the
  live companies.** Do one real test payment with a test key before using it with patients.

### Added in version 0.19

- Staff tasks: what needs doing, for whom, by when, how urgent, optionally for a patient, repeating daily, weekly or monthly (doing one
  makes the next). My tasks, everyone's, and done. Late tasks are marked, and the menu shows how many of yours are due. A patient's page
  lists their tasks and can add one.
- A time clock: Clock in and Clock out at the top of every screen. A manager sees time cards by week (hours per person per day, after
  breaks), corrects an entry or adds a forgotten shift with a reason that is kept, and downloads the week as a spreadsheet. It records
  time worked; it does not work out pay.
- End-of-day cash-up: what should have come in by each way of paying, what was counted, and the difference. A difference needs a note.
  Payments entered for a day after it was closed are shown. A manager can reopen a day; the earlier close is kept.
- Exports for the accountant: a money journal (payments, voided payments, adjustments) and issued invoices laid out like Xero's sales
  invoice import, with the clinic's own sales account code and tax type. Spreadsheet cells cannot run as formulas. Check the invoice file
  against your package's template before the first import.
- A self check-in kiosk: front desk turns a tablet into a kiosk; a patient enters the last four digits of their phone and their date of
  birth, and is checked in (only their first name is shown). The device shows nothing else until a manager or front desk login takes it
  out of kiosk mode. Tries are limited.
- Reports: the day sheet (visits, work done by dentist, money taken, new patients, printable), work done by treatment (with a
  spreadsheet), treatment planned but not booked (the oldest first, with a Book button), and missed and cancelled visits with whether
  they booked again.
- Fixed: lists of visits on the prescription and vital signs forms would have failed for a patient with past visits. Dates with a time
  now always show correctly.

### Added in version 0.18

- Cover terms per patient cover: a share for each kind of treatment (for example check-ups 100%, fillings 80%, crowns 50%), a yearly
  excess the patient pays first (skipped for check-ups, cleaning and X-rays unless the cover says otherwise), waiting periods by kind of
  treatment, how-often limits per treatment (for example 2 scale and polish in 12 months, counting work already done), a benefit year
  that can start in any month, and a lifetime orthodontic limit. With no terms set, estimates work exactly as before.
- A second payer. Covers are ordered (pays first, pays second, swap with one click). The second payer pays the lesser of its own normal
  share and what the patient still owes after the first payer, in estimates and in claims. The same treatment can be claimed from each
  payer once.
- Claim lines keep the excess and a note of why the payer is expected to pay less (waiting period, limit reached, maximum reached).
- Cover checks: record when the cover was confirmed with the payer and what they said. The patient's insurance page shows which covers
  have not been checked.
- One payment from a payer split across many claims (a bank transfer and the payer's list of what it is for). The shares must add up to
  the amount received; each share becomes a normal insurance payment and receipt on that patient's account; write-offs and closing a
  claim with nothing paid are allowed; the same reference cannot be entered twice.
- What each payer owes on sent claims, by 0 to 30, 31 to 60, 61 to 90 and over 90 days, on the claims screen.
- Still not built: sending claims electronically and reading payers' electronic remittance files (US claims are in version 0.23).

### Added in version 0.17

- Prescriptions. Each clinic keeps its own medicines list. A dentist picks medicines, the system checks them against the patient's
  allergy alerts and medical history (penicillin allergy and amoxicillin, warfarin and metronidazole, warfarin or statins and the azole
  antifungals, blood thinners and anti-inflammatory painkillers, pregnancy, controlled drugs), and a printable prescription is made with
  the clinic's address, the date, the patient's name, address and age, each medicine and the dentist's registration number. A "stop"
  warning needs a written reason before it can go ahead, and the reason is kept. What was written is copied onto the prescription,
  so editing the list later changes nothing already written. Prescriptions are cancelled with a reason, never deleted. The starter
  antibiotic doses come from SDCEP and the College of General Dentistry (UK); painkillers and the rest come without a dose, for the
  clinic's dentists to fill in. The checks only find plain words on the record: they are a safety net, not a formulary. Electronic
  prescribing to pharmacies is not built.
- Referrals both ways. A list of referrers (specialists, hospitals, doctors, other dentists). A referral out goes from draft to sent,
  booked, seen, report received and closed, with a printed letter that carries the reason, findings, the patient's alerts and medicines,
  and the referring dentist's registration. An open referrals list puts urgent ones first. Each patient can record who sent them or how
  they heard of the clinic, and a report shows where new patients came from.
- Vital signs: blood pressure, pulse, temperature, oxygen and blood sugar, checked for impossible values. Blood pressure is graded against
  two levels the clinic sets (starting at 160/100 to put off planned treatment and refer, and 180/110 for no treatment without a doctor,
  from the University of Oklahoma College of Dentistry clinic manual). A high reading taken today shows in red on every patient screen.
- Letters from templates the clinic edits, with fields filled from the record ({patient_name}, {last_visit}, {balance} and so on).
  Starters: attendance letter, excuse from duty, recall reminder, account reminder. The words can be changed before saving; what was
  printed is kept on the patient.
- A contact log for calls, desk visits, emails and letters, with follow-up dates that show on the Today screen until done.
- X-ray layouts (full mouth series of 18, four or two bitewings, panoramic with bitewings, clinical photos) built from pictures already
  on the record, and an image viewer with zoom, move, turn, brightness, contrast, invert and measuring in millimetres after setting a
  scale from a known length. The viewer never changes the stored picture. DICOM files are kept and downloaded but not yet shown.
- The clinic's address, phone and email are now set under Settings, and each dentist's qualifications and registration.
- All of these are in the full copy of a patient's record, and anonymising a patient clears the words of their letters and contact log.

### Added in version 0.16

- Each dentist's own working hours (up to two spans a day), or the clinic's hours when none are set. Time off, whole days or part of one
  day, with a list of visits already booked in it so they can be moved.
- Blocked time in the diary, once, every working day or every week: lunch, meetings, training and closed time cannot be booked; time kept
  for emergencies, new patients or held back stays out of the free times unless the appointment type may use it or someone ticks to use it.
  A block can be for every dentist, one dentist or one chair.
- Appointment types: a name, length, colour, reason, the held-back time it may use, and treatments to plan when it is booked (linked to
  treatment already planned, so nothing is doubled). Eight starter types. The diary shows each visit in its type's colour.
- A week view for one dentist, with hours worked, minutes booked, blocks and time off.
- A check-in board for the day: expected (with how late), waiting (with how long), in the chair, and left. Finished marks the visit as done,
  so recalls start as before.
- Booking a family back to back with the same dentist in one step: all of them or none.
- Filling a gap: from a missed or cancelled visit, the people on the waiting list who fit the time, earliest-slot people first.
- Tooth numbering per clinic: FDI, Universal (US) or Palmer (UK). Teeth are stored in FDI and shown and typed in the clinic's own system on
  every screen, document and in voice gum charting. The API stays FDI.
- The zip no longer carries the developer's database and secret key from the instance folder. If you unpacked version 0.15, delete
  instance/secret_key and instance/dental.sqlite from that copy; a new key is made on first start.

### Added in version 0.15

- French for the rest of the staff screens: clinical record (chart, gum charts including the voice help, notes, medical history,
  consents, files, health forms), money screens (invoices, claims, payers, plans, memberships, instalments, takings, ageing, account
  statements), reports and key numbers, lab cases, stock, campaigns, catalogue, groups of clinics, import, settings, staff, privacy
  and security pages. Tests fail if a screen's wording has no French, or if French wording is left that nothing uses.
- French for the plain messages shown after an action and for the plain errors the services raise ("Password changed.", "Enter the
  full name of the person who signed."). They are translated when shown, so the services stay free of any language.
- A crawl test opens about 100 screens and 30 detail pages in both languages for a clinic with a full set of records, so a screen
  that breaks in one language is caught.
- Bug found by that crawl and fixed: copying a patient's record (data-rights request) failed with a database error for any patient
  with an instalment plan. A new test now checks every table the copy reads from against the schema.
- Honest limits: the French is mine, not a native speaker's. A message built in code from a name or number stays in English.

### Added in version 0.14

- A PostgreSQL option. Set `DATABASE_URL=postgresql://user:password@host:5432/dental_os` (instead of `DATABASE_PATH`) and the same
  product runs on PostgreSQL, with no change to any screen or rule. `dental_os/dbpg.py` translates the few SQLite-only bits of SQL
  as each statement is sent, gives back rows that behave like SQLite's, and builds the tables from `schema.sql`. It keeps SQLite's
  habits where they matter: dates stay as text, empty values sort first going up and last going down, `LIKE` and user names ignore
  capitals, a failed statement does not spoil the rest of the transaction, and the lock that stops two people booking the last slot
  still works (an advisory lock). `flask copy-to-postgres --to postgresql://...` copies an existing SQLite database across, keeps
  every id, carries the id counters on, and counts the rows on both sides. `flask backup` uses `pg_dump` on PostgreSQL.
- How it was tested, and what was not: all 600+ tests run unchanged against a real PostgreSQL 16 server (set `DENTAL_OS_TEST_PG`
  and `DENTAL_OS_PG_DRIVER=tools.pgwire`, see `tools/pgwire.py`). Where this was built no PostgreSQL driver could be installed, so the
  tests use a small client written for testing only, which also makes the server check parameter types the strict way a real driver
  does. **The real drivers (psycopg 3, or pg8000) have not been run against it.** Install one with `pip install -r requirements-postgres.txt`
  and run the suite once the same way, using no `DENTAL_OS_PG_DRIVER`, before trusting it with real patients. psycopg 3 is the one
  to use. pg8000 should work but has had even less attention.
- Create the database with plain binary sorting, as SQLite sorts: `createdb -T template0 -E UTF8 --locale=C dental_os`. It also
  needs ICU support (standard in packaged PostgreSQL) for case-blind user names. Other sort orders work but may order names
  differently.
- Voice entry for gum charting, off by default. A manager switches it on under Settings. On the new gum chart screen a dentist or
  hygienist presses Speak (or types into a box) and says, for example, "tooth sixteen, three two three, four three three, next tooth".
  The chart fills in in the order shown on screen, moves to the next tooth by itself, and understands recession ("minus two"), bleeding
  by point number, skip, back and undo. It reads digits the way speech recognisers often write them ("323"). It shows what it heard
  and what it ignored, and nothing is saved until the form is saved.
- Privacy of voice: the browser does the listening. By default the page asks the browser to recognise speech on the computer itself
  (`processLocally`, which MDN describes as keeping the audio and text from being sent to a third party), and if the browser cannot, the
  Speak button switches off and the typed box still works. A second switch lets the clinic allow the browser maker's online
  service instead (for Chrome, Google), which sends the sound away: leave it off unless the clinic's lawyer agrees and patients are told.
  Only numbers and tooth numbers are spoken, never names.
- Honest limits: the parsing is tested (in node, with real cases), but the listening itself depends on the browser and was not tried
  with a microphone. Recognition mishears, so the screen shows the chart filling in and the clinician must check it before saving.
  English only for now.

### Added in version 0.13

- Optional AI help for clinical notes. It is off by default. A manager switches it on under Settings, AI help for clinical notes,
  with the clinic's own API key for Anthropic's Claude service (the key is stored encrypted with a key made from `SECRET_KEY`, never
  shown again, never logged). On the note screen a dentist or other clinical user then sees "Draft with AI": they type rough
  shorthand ("46 mod caries, comp, rv 6/12"), and the boxes of the note come back filled in as a draft.
- What stays in the dentist's hands: nothing is saved by the AI. The boxes are filled in on the screen, the dentist reads and fixes
  them, presses Save draft, and a dentist still signs the note as before. The screen labels the result "AI draft".
- What leaves the server: only the shorthand just typed, and the names of the boxes in that type of note. No patient number, no
  history, no chart. The patient's name, phone numbers and email addresses are removed first (names become "the patient"). It cannot
  remove a name or detail it does not know about, and the screen says so. The audit log records that a draft was asked for and the
  model name, never the text. A clinic can ask for 60 drafts an hour (a guard against runaway cost).
- Honest limits: the request follows Anthropic's published Messages API shape, but it has not been tried against the live service
  (no key was available when it was built). Try it first with a made-up patient. The AI can be wrong or leave things out, which is
  why a person reads and signs. Sending a patient's health details to a service in another country needs a legal basis and a line in
  the clinic's privacy notice: see `docs/PRIVACY.md`. This screen is in English only for now.

### Added in version 0.12

- A starting point for tax, by country, under Settings. It exists for Ghana, the United Kingdom, South Africa, Nigeria and Kenya.
  It adds the named rates a clinic there usually needs (for example Exempt, and VAT 15% + levies 5% for Ghana), and shows the facts
  we could check, how well each is supported (read on an official site, in an official guide, or only in a news report), the pages
  we read, the date we checked (27 October 2026) and the questions to put to an accountant. It never changes the clinic's default
  rate or any treatment, and a clinic can ignore it. It is not tax advice. Other countries show "no starting point yet" until
  someone checks the facts for them.
- Honest limits: for Ghana we confirmed the 2026 rates on the GRA's own site (VAT 15%, NHIL 2.5%, GETFund 2.5%, COVID levy gone),
  but the GRA's exemption list we could read still refers to the old Act 870 and says "medical services" without naming dental work.
  Whether dental work is exempt under the VAT Act 2025 is a question for the clinic's accountant, and the page says so. For South
  Africa and Nigeria the exemption facts come from advisers' summaries, not the law itself, and are marked "check it".
  France, Germany, Canada, the United States and the rest of Africa have not been researched yet. Local procedure-code sets are not
  built: the main ones (for example the American CDT codes) are licensed, so each needs a licence before it can be shipped.

### Added in version 0.11

- The official WhatsApp Business API (Meta's Cloud API) as a second way to send. It is optional and off by default. The manual way
  (staff open WhatsApp and press send) is unchanged and needs nothing from Meta. A manager turns the API on under Messages, Set-up,
  WhatsApp Business API, with the phone number ID, a permanent access token and the app secret from the clinic's own Meta app.
  The token and secret are stored encrypted with a key made from `SECRET_KEY` (change that key and the API switches itself off
  until they are entered again), and are never shown or logged.
- What it does: a Send button on each message in the send list and on the visit page, "Send everything that can go", and
  `flask send-whatsapp` for clinics that choose automatic sending (run it every few minutes from the scheduler). Consent is checked
  again at the moment of sending. A message that cannot go (no approved template, no consent) stays in the list for a person.
- Business-started WhatsApp messages need a template that Meta has approved. The clinic writes each template at Meta, then enters
  its name and field order here, per message and per language. The page shows the clinic's own wording with the fields turned
  into {{1}}, {{2}} so it can be pasted into Meta. During the 24 hours after a patient writes in, plain text can be sent without a template.
- Meta calls the address `/hooks/whatsapp/<secret key>` (each clinic has its own). It is answered only if the request is signed with the
  clinic's app secret (HMAC-SHA256, checked in constant time), so a forged message does nothing. It reports sent, delivered, read and
  failed on each message, files a patient's reply under that patient as an unread reply, and treats a reply of exactly STOP as the
  patient withdrawing consent. A reply from a number that matches no patient is kept aside for 30 days and not put on any record.
  Repeats from Meta are ignored, so it is safe when Meta retries.
- **Not tested against Meta's live service.** The workspace this was built in has no access to Meta, so every test uses a fake that
  answers the way Meta's documentation says it does (endpoint, token header, template body, `messages[0].id`, signature header,
  verification handshake, status and reply payloads). The first real test must be done by a person with a Meta developer account
  and a test number: expect small differences (an error code, a field name). Meta changes its API version and its prices;
  check the current version number and the price list for Ghana before a clinic relies on it. Marketing messages to some countries
  (the US from April 2025) are restricted by Meta.

### Added in version 0.10

- French for the daily staff screens: the dashboard, diary, patient list, patient page and edit form, appointments (view, book, move),
  treatment plan, account and payments, patient messages and private link, the send list and recalls. The short messages shown after
  an action (Patient saved, Payment recorded and so on) are in French too. Messages that carry a name, a number or an error from the
  system (for example a rule that failed) stay in English for now. Every other staff screen is still English: the clinical record
  (medical, tooth chart, gums, notes, consents, files), plans and quotes, insurance and invoices, reports, lab and stock, privacy
  tools, settings, team and the catalogue. A French-speaking team would meet those in English, so tell clinics this honestly.
- The French is new and has not been read by a native speaker or a dental professional. Get that check before a French-speaking
  clinic relies on it.

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
still have a version 0.2 database, start from a fresh file. Versions 0.4 to 0.14 only add tables and columns, and do it by themselves
when the app starts, so a 0.3 database carries on.

Backups: `python -m flask backup --to /path/to/backups --keep 14` makes a consistent copy of the database. Run it every night
from the server's scheduler, and copy the folder off the machine. X-rays and documents live in `FILES_PATH` (default
`instance/files`). Back that folder up too, or the images are lost even though the database is safe.

Production: `gunicorn wsgi:app`, behind HTTPS (if a proxy sits in front, set `TRUST_PROXY=1` and make the proxy pass the visitor's
address, https and the host name, so the login and booking limits count per visitor and the private links point at your address), with `COOKIE_SECURE=1`, `SECRET_KEY` set to a long random value, and
`DATABASE_PATH` on a disk that is backed up (or `DATABASE_URL` for PostgreSQL, see above). Do not run the Flask dev server in production.

## Not built yet (in this order)

What needs an outside party before it can go live, with the code already built: sending FP17s straight to the NHSBSA (accreditation),
a direct link to a named US clearinghouse (its account and API), the live Twilio, Paystack and Stripe services (the clinic's own
accounts), real X-rays from each sensor, and a native French speaker's check of the French. The full map is in `docs/FEATURES.md`.

## Stack note

The teardown proposed Django and PostgreSQL. This build uses Flask and SQLite because package installs were blocked
where it was built. The business rules (`services/`) and the SQL schema carry over unchanged. Moving to Django is mostly
rewriting `views.py`, templates and the login code. SQLite is fine for a first few clinics. A hosted product with many
clinics should move to PostgreSQL, and since version 0.14 it can (set `DATABASE_URL`).

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
