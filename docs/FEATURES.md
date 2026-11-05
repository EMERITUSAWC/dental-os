# Feature map: what the leading systems do, and our version

How this was made: from public product pages, vendor help pages and review articles (listed at the bottom). Each feature is
described here in our own words, then built from that note. Ideas and features are free to build. Nothing was copied: no code,
no screens, no wording, no images, no icons, no procedure-code tables that are licensed (for example CDT).
"Seen in" says where the idea is common, not that we copied that product.

Stages are build order. A stage ships as its own version. Stage 1 (clinical record) is built in version 0.4. Stage 2 (plans, quotes, memberships, instalments) is built in version 0.5. Stage 3 (payers, claims, invoices, ageing) is built in version 0.6. Stage 4 (messages, campaigns, online booking, patient link) is built in version 0.7. Stage 5 (key numbers, groups of clinics, lab cases, stock, API and webhooks) is built in version 0.8. Stage 6 (two-step login, data-rights tools, tax by treatment, procedure packs, French) is built in version 0.9. French for the daily staff screens is built in version 0.10. The official WhatsApp Business API is built in version 0.11. Tax starting points for five countries are built in version 0.12. Optional AI drafting of clinical notes is built in version 0.13. A PostgreSQL option and voice entry for gum charting are built in version 0.14. French for the remaining staff screens and messages is built in version 0.15. Diary depth (dentists' hours, time off, blocked time, appointment types, week view, check-in board, family booking, gap filling, tooth numbering) is built in version 0.16. Prescriptions, referrals, vital signs, letters, a contact log, X-ray layouts and an image viewer are built in version 0.17. Insurance depth (shares by kind of treatment, excess, waiting periods, how-often limits, a second payer, cover checks, one payer payment across many claims, payer ageing) is built in version 0.18. Staff tasks, a time clock, end-of-day cash-up, accountant exports, a check-in kiosk and more reports are built in version 0.19. Online payments with Paystack and Stripe are built in version 0.20. Phone calls (0.21), sterilisation tracking (0.22), US electronic claims (0.23), NHS England (0.24), and DICOM viewing with an imaging bridge (0.25) follow. Orthodontics (0.26), statement runs and late fees (0.27), a report builder (0.28), team messages and an equipment register (0.29), and permission groups (0.30) come after, then email (0.31), saved cards with automatic charges (0.32), SMS (0.33), a form builder (0.34), an implant and materials register (0.35), associate pay (0.36), complaints and safety incidents (0.37), staff registration and CPD (0.38), fee schedules (0.39), merging duplicate records (0.40), a list of patients not seen for a while (0.41), refunds (0.42), closing the books (0.43), aftercare sheets (0.44), treatment kits (0.45), auto codes (0.46), closed days (0.47), moving in from Open Dental, Dentrix and spreadsheets (0.48 and 0.49), and working through internet outages with locked off-site copies and a read-only viewing copy (0.50 and 0.51).

## Clinical record

| Feature | Seen in | Our version | Stage |
|---|---|---|---|
| Medical history and allergy alerts that show wherever the patient appears | all major systems | A history form per patient, plus alerts (allergy, medical, medication, behaviour) with a severity. Critical alerts show in red on the patient, the diary and the appointment. | 1 |
| Tooth chart (odontogram) | Dentrix, Eaglesoft, R4, CareStack | FDI chart for adult and child teeth, drawn as real tooth shapes (crown, roots, biting surface) since 0.70. Each tooth shows existing conditions, planned work and finished work. | 1 |
| Periodontal charting with six probing points | Eaglesoft, Dentrix, Curve, voice-charting tools | Full-mouth grid for pocket depth, recession and bleeding. Summary of bleeding, deep pockets and a simple score per sextant. Compare with the last chart. | 1 |
| Clinical notes from templates, locked once signed | Kiroku, Dentally, Dentrix | Templates by treatment type. Draft, sign, lock. Corrections are added as dated addenda, never by editing. | 1 |
| Digital consent forms and signatures | Denticon, Curve, Practice-Web | Consent templates the clinic edits. Recorded with typed name, time and who recorded it. Paper consents can be marked as scanned. | 1 |
| Images and documents on the record | all | Upload X-rays, photos, referral letters. Type-checked, stored outside the web folder, served only to signed-in staff of that clinic. | 1 |
| Data export and import | all | Clinic data export as CSV files. Patient list import from CSV with a preview. | 1 |

## Treatment plans and money

| Feature | Seen in | Our version | Stage |
|---|---|---|---|
| Plans with options and phases, estimates that update | CareStack, Dentrix, Denticon | Several plan options per patient, split into phases, with cost per phase. | 2 |
| Present, accept, sign, pay in one flow | Denticon, Curve | A printable patient quote, an accept or decline record with a signature, and a deposit taken against it. | 2 |
| Stalled-plan follow-up, treatment coordinator | CareStack | A list of plans offered but not accepted, with an owner and a follow-up date. | 2 |
| In-house membership plans with recurring billing, benefits used, renewals | Practice Plan and Denplan style plans, dental membership tools | Plans with a fee, a billing cycle, included services and a discount on extras. Enrol a patient, track what was used, track renewals and failed payments. | 2 |
| Payment plans and deposits | Denticon, Pabau | Instalment schedules against a balance. Deposits held as credit. | 2 |
| Insurance and other payers, fee schedules per payer | Dentrix, Eaglesoft, Denticon | Payers, a fee schedule per payer, a patient's cover, and an estimate of what the payer and the patient each pay. | 3 |
| Claims and pre-authorisation tracking, payments matched to claims | Dentrix, Eaglesoft, Denticon | Claim record with status, amounts, response and write-off. Electronic submission is later and country by country. | 3 |
| Invoices and statements, ageing | most | Numbered invoices with tax lines set per clinic, statements, and a 30/60/90 day ageing report. | 3 |

## Talking to patients

| Feature | Seen in | Our version | Stage |
|---|---|---|---|
| Message templates and automatic reminders | Dentally, Weave, Lighthouse 360 | Editable templates. A reminder schedule per clinic (for example 3 days and 1 day before). | 4 |
| Two-way inbox linked to the patient record | Weave, Solutionreach, Curve | Every message in and out is on the patient. An inbox for unread replies. Replies are read automatically when the official WhatsApp Business API is connected (version 0.11). | 4 |
| Recall and reactivation campaigns | RevenueWell, Solutionreach | Campaigns by rule (overdue recall, lapsed patients, unaccepted plans) with consent checks. | 4 |
| Review requests sent after a visit | Solutionreach, RevenueWell | A review request after a completed visit, only for patients who agreed to messages. | 4 |
| Online booking | Dentally, NexHealth, Curve | A public booking page per clinic that offers real free slots and lands in the diary as a request to confirm. | 4 |
| Digital intake forms | Curve, Practice-Web, Adit | A link to a patient's own medical history form, filled on their phone before the visit. | 4 |
| Patient portal | Open Dental, Dentally | A private link per patient to see appointments, quotes and balance, and to sign. | 4 |

## Running the practice

| Feature | Seen in | Our version | Stage |
|---|---|---|---|
| Production, collections and acceptance reports, KPI dashboard | Dentrix, Denticon, CareStack | Dashboard by day, month and dentist. Production, collections, acceptance rate, new patients, no-shows, hygiene recall rate, ageing. | 5 |
| Multi-site view | Denticon, R4, CareStack | One organisation with several clinics and a manager who sees all of them. Each clinic is still separate. | 5 |
| Lab case tracking | lab tools, R4 | A case per lab job with the lab, due date, status, shade and cost. | 5 |
| Stock control | Pabau, Dentally | Items, levels, reorder points, usage and suppliers. | 5 |
| Open API, webhooks, integrations | Dentally, Open Dental, Ascend | A keyed REST API for patients and appointments, and webhooks for events. | 5 |
| Roles and permissions, audit | all | Role rules per area. Activity log already built. | 1 onward |
| AI help: notes, voice perio charting, radiograph analysis | Curve, CareStack, Ascend, Pabau, note tools | Planned. These need a speech and an imaging model, patient-data agreements and clinical safety review, so they come after the core is proven. | later |

## Safety, tax and reach (stage 6)

| Feature | Seen in | Our version | Stage |
|---|---|---|---|
| Two-step login | CareStack, Denticon, Curve | An authenticator app code (RFC 6238), ten recovery codes, a clinic setting to require it, a manager reset, lock-out after 5 wrong codes. | 6 |
| Password rules, idle sign-out | all hosted systems | Length and common-password checks, sign-out after 60 idle minutes. | 6 |
| Patient data rights | UK and EU practice tools | A register of requests with a reply date, a full record copy (page or zip), anonymising with a reason, and who has looked at a record. | 6 |
| Data incident log | UK and EU practice tools | A register with a 72-hour reporting clock, what was told to the regulator and to patients, and closing with actions. | 6 |
| Retention review | UK and EU practice tools | A clinic-chosen period and a list of records older than it, for a manager to review. Nothing is deleted by itself. | 6 |
| Several tax rates, tax by treatment | Xero-style invoicing, Dentally | Named rates (taxed, zero rated, exempt), one per procedure, a default for the rest, tax worked out once per rate on each invoice. | 6 |
| Procedure packs, price list in a spreadsheet | Dentrix, Open Dental fee schedules | Ready-made groups of our own codes, and a CSV that is checked in full before anything changes. | 6 |
| AI help for notes | Newer dental systems (not checked one by one) | Optional and off by default. A dentist types shorthand and gets a draft for the note boxes. Nothing is saved or signed by the AI. Name, phone and email are removed first. Not yet tried against the live service (version 0.13). | 7 |
| Voice gum charting | Several desktop systems offer voice perio (not checked one by one) | Optional and off by default. Say a tooth and its numbers, the chart fills in and moves on. Listens on the computer itself unless the clinic allows the online service. Typed box as a fallback. Not tried with a microphone (version 0.14). | 7 |
| PostgreSQL | any hosted system | Same product on PostgreSQL by setting one address, with a copy tool from SQLite. Tested on a real server with a test client; real drivers not yet tried (version 0.14). | 7 |
| Languages | CareStack, Dentally | English and French for every staff screen, sign-in, booking, a patient's page, printed documents and message wording (version 0.15). Developer pages and note-type headings stay English. | 6 |

## Diary and clinical depth (versions 0.16 and 0.17)

| Feature | Seen in | Our version | Version |
|---|---|---|---|
| Provider schedules and time off | Open Dental, Dentrix | Hours per dentist per weekday (split days), time off with a list of clashing visits | 0.16 |
| Blockouts | Open Dental, Dentrix, Eaglesoft | Hard blocks and held-back time, by dentist or chair, once, daily or weekly | 0.16 |
| Appointment types with colours and procedures | Open Dental, Dentrix | Length, colour, reason, held-back time it may use, treatments planned on booking | 0.16 |
| Week view, check-in board | Open Dental, Dentrix, Curve | One dentist's week; arrived, waiting, in chair, left, with times | 0.16 |
| Family scheduling, ASAP list matching | Open Dental | Back-to-back family booking; waiting-list people who fit a freed time | 0.16 |
| Tooth numbering systems | Dentrix (US), UK systems | FDI, Universal, Palmer per clinic; stored as FDI | 0.16 |
| Prescriptions with allergy and interaction checks | Open Dental, Dentrix, Dentally | Clinic medicines list, plain-word checks, printable prescription; no e-prescribing | 0.17 |
| Referrals in and out, letters | Open Dental, Dentrix, R4 | Referrers, referral tracking and letters, where new patients came from | 0.17 |
| Vital signs | Dentrix, Curve | BP graded against clinic-set levels, shown on the record | 0.17 |
| Letters and merge fields | Open Dental, Dentrix | Clinic templates, fields from the record, printed words kept | 0.17 |
| Communication log | Open Dental (commlog) | Calls, desk visits, letters, with follow-ups on the Today screen | 0.17 |
| Image mounts and viewer | Open Dental, Dentrix | FMX and bitewing layouts, viewer with adjustments and measuring | 0.17 |
| Insurance benefits: coverage by category, deductibles, frequency limits, waiting periods, benefit year | Open Dental, Dentrix, Eaglesoft | Per cover, all optional; estimates and claims use them | 0.18 |
| Secondary insurance (coordination of benefits) | Open Dental, Dentrix | Second payer pays the lesser of its share and what is left | 0.18 |
| Eligibility verification log | Dentrix, Denticon | Who checked the cover, when, and what the payer said | 0.18 |
| Batch insurance payments (one cheque or transfer, many claims) | Open Dental, Dentrix | Shares must add up; receipts per patient; write-offs | 0.18 |
| Insurance ageing by payer | Open Dental, Dentrix | 0-30, 31-60, 61-90, over 90 days | 0.18 |
| Tasks and task lists | Open Dental, Dentrix | Assigned, due, priority, repeating, linked to a patient | 0.19 |
| Time clock and time cards | Open Dental, Dentrix | Clock in and out, manager corrections with reasons, weekly cards | 0.19 |
| Daily close / deposit reconciliation | Dentrix, Eaglesoft | Expected against counted by way of paying, notes, reopen kept | 0.19 |
| Accounting export | Dentrix (QuickBooks link), Dentally (Xero) | Journal CSV and Xero-style invoice CSV | 0.19 |
| Patient self check-in | Open Dental (kiosk), Curve, NexHealth | Locked kiosk mode, phone digits and date of birth | 0.19 |
| Day sheet, production by code, unscheduled treatment, broken appointments | Open Dental, Dentrix | Printable, with spreadsheet downloads | 0.19 |
| Online payments and text-to-pay | Weave, Curve, NexHealth, Dentally | Pay links by WhatsApp or QR, Paystack (mobile money) and Stripe, portal Pay now | 0.20 |
| Phone system integration: caller pop-up, click to call, missed-call follow-up | Weave, RevenueWell, Dentrix (with partners) | Through the clinic's Twilio account; call log on the record; calls not recorded | 0.21 |
| Sterilisation and instrument tracking | standalone tracking tools, larger UK and US systems | Steriliser tests, cycles, pack labels with QR codes, use per patient, holds, recalls after a failed spore test | 0.22 |
| Electronic dental claims (837D), remittances (835), eligibility (270/271), receipts (999) | Dentrix, Eaglesoft, Open Dental, Denticon | X12 5010 files, checked before they are made; remittances posted as one payer payment; file transfer by download and upload | 0.23 |
| NHS England: courses, bands, exemptions, UDAs, FP17 | R4, Dentally, SOE (UK systems) | Courses with charges and exemptions, UDA tracking against the contract, 62-day FP17 clock; sending FP17s direct needs NHSBSA accreditation | 0.24 |
| Imaging bridge and DICOM viewing | Dentrix, Open Dental bridges, imaging software | A folder-watching agent on the X-ray computer, patient matching by session or DICOM Patient ID, DICOM shown in the viewer with measuring | 0.25 |
| Orthodontics: ortho chart, ortho billing contracts | Open Dental (ortho chart, auto ortho billing), Dentrix (ortho) | Cases with diagnosis, visit chart, deposit and monthly amounts posted on their dates | 0.26 |
| Billing cycles: mass statements, finance charges | Open Dental, Dentrix, Eaglesoft | Statement runs printed or sent by private link; late fees by the clinic's own rule, off by default | 0.27 |
| Custom reports | Open Dental (user queries), Dentrix (report builder) | A safe builder with fixed data sets, filters and grouping; saved, shared, exported | 0.28 |
| Internal messaging, equipment log | Open Dental (messaging, equipment) | Team messages linked to patients; equipment register with service due dates | 0.29 |
| Security groups and permissions | Open Dental, Dentrix | Groups that narrow a role by area, checked before every screen | 0.30 |
| Email reminders and statements | Dentally, Weave, Open Dental (email statements) | Through the clinic's own mail account, for patients who prefer email; statements carry the private link only | 0.31 |
| Cards on file, recurring card payments | Curve, Dentally, Weave | Paystack or Stripe token only, the patient's own consent, staff charges and a daily job for agreed monthly fees | 0.32 |
| SMS reminders and statements | Dentrix (eCentral), Open Dental (eServices), Dentally | Through the clinic's own Hubtel, Africa's Talking or Twilio account, for patients who prefer SMS; offers carry a STOP line | 0.33 |
| Clinic-made patient forms | Open Dental (web forms), Dentrix (Patient Forms), Dentally (online forms) | Any questions, one-time private link or the desk tablet, versions kept as the patient saw them, review and file | 0.34 |
| Implant and materials traceability, recalls | Few general systems; often a paper log | Maker, product, lot, size, expiry per tooth; find every patient by lot; a recall worklist; a patient implant card | 0.35 |
| Associate pay by production or collections, lab fee split | Dentrix (provider pay reports), Software of Excellence, Dentally | Dated terms per provider, first-in-first-out collections rule stated on the statement, locked approved runs | 0.36 |
| Complaints log and patient safety incidents | Usually a separate compliance tool (iComply, Agilio) | Three-working-day acknowledgement clock, outcome and learning, duty of candour steps that block closing | 0.37 |
| Staff registration, indemnity and CPD | Usually a separate compliance tool | GDC five-year cycle with the two-year rule and outcomes A to D, or a yearly target; expiry warnings | 0.38 |
| Several fee schedules | Open Dental, Dentrix | Own prices or a percentage off, per patient; past prices never change | 0.39 |
| Merge duplicate patients | Open Dental (Merge Patients), Dentrix | Every patient table found from the schema, what cannot be doubled is listed, full record of each merge | 0.40 |
| Reactivation (lapsed patients) | Dentrix (Continuing Care), Open Dental (Reactivation list) | Months chosen, waiting list respected, consent checked, put aside with a reason | 0.41 |
| Refunds and credit balances | Open Dental, Dentrix | Only out of credit; flows into takings, cash-up, reports, associate pay and the accountant's export | 0.42 |
| Lock date for finished periods | Open Dental (security lock date), Dentrix (month-end close) | Refuses backdated money and treatment changes; reopening needs a reason | 0.43 |
| Post-operative instructions | Dentrix (patient education add-ons), Dentally | Dentist approval that lapses on any change; printed in the patient's language | 0.44 |
| Stock used per treatment | Dentally (stock), add-on inventory tools | Taken off on completion, put back on void, never blocks care, material cost per treatment | 0.45 |
| Auto codes by surfaces and tooth | Open Dental (Auto Codes), Dentrix | Most exact rule wins; front or back from the FDI number | 0.46 |
| Holidays and closed days | Open Dental (holidays), Dentrix | Blocks booking and online slots, used by working-day clocks, copy fixed dates forward | 0.47 |
| Conversion from another system | Done by the vendor's conversion team, usually for a fee | Self-service preview that is the real run undone; can be repeated without doubling; Open Dental layout read directly | 0.48–0.49 |
| Working without the internet | Dentrix and Open Dental run on a clinic server; most cloud systems stop | Clinic server first, messages wait, a locked off-site copy checked after writing, restore drills, read-only viewing copy | 0.50–0.51 |
| Ghana claims: ICD-10 diagnoses, covered lists, monthly batches | Local Ghanaian systems; not in Open Dental or Dentrix | WHO ICD-10 dental block built in, NHIS benefits stated, batch summary, claim form and CSV | 0.52 |
| Self sign-up, trials and subscription billing | Cloud systems (Dentally, Curve); Open Dental and Dentrix sell by licence | Clinic keeps access to its own data when paused; overdue means look-only, never locked out | 0.53–0.54 |
| Support access granted by the clinic | Usually remote-desktop on request | Time-limited, look-only, every page in the clinic's own activity log, ended by the clinic | 0.53 |
| Works on a phone | Cloud systems; Open Dental and Dentrix are desktop programs | Every main screen at phone width, can sit on the home screen | 0.55 |
| Whole-app security tests | Not published by the leading systems | Every page needs a login; one clinic never sees or changes another's records, checked on every page and form | 0.56 |
| Speed at 20,000 patients | Not published | Every screen under a second at that size, timed by a tool that ships with the code | 0.57 |
| Accessibility | Varies; rarely stated | Every screen checked for labels, headings and readable colours by the tests; skip link and keyboard ring | 0.58 |
| Restore drills on record | Usually left to the IT company | Last good drill and when the next is due, shown to the manager; a copy with far fewer patients fails | 0.58 |
| Go-live checklist and side-by-side run | Done by the vendor's trainers, on paper | Checks itself where it can; the old system's daily figures compared until ten days match | 0.59 |
| Problem reports and error references | Phone the support line | From any page, without patient details; a broken page gives a reference the support side can find | 0.59 |
| Install kit for a clinic server | Dentrix and Open Dental: installer for Windows; cloud systems: none | One script for Ubuntu, https on the clinic network, every scheduled job set up | 0.59 |
| X-ray quality check as pictures come in | Dentrix Image Verify (AI, 2026) | Runs on the clinic's computer with no internet or extra fee; flags blur and exposure | 0.60 |
| AI second look at X-rays | Dentrix Detect AI (with VideaHealth); Pearl and Overjet hold FDA clearances | Any company the clinic chooses; only the pixels leave; every finding agreed or rejected by a dentist | 0.60 |
| Dictation in clinical notes | Dentrix Voice Notes | On the device only, never an online service; works with the AI note draft | 0.61 |
| Chairside tablet mode | Open Dental (ODTouch) | Any tablet browser; alerts first; moves the patient on the shared check-in board | 0.61 |
| Prescriptions a pharmacy can check | Not in Open Dental or Dentrix | QR on every prescription; genuine, valid or already dispensed; works with any pharmacy, no account | 0.62 |
| Electronic prescribing | Open Dental through DoseSpot (US, including controlled drugs) | FHIR R4 copy of every prescription; network connection built, an adapter per network once agreed | 0.62 |
| Live clearinghouse connection | Both, through their own clearinghouse services | Claims, cover checks, acknowledgements and remittances with no file handling; a refused claim is never marked sent | 0.63 |
| FHIR R4 interface | Open Dental's API is its own format; Dentrix through partner programs | The international health format, read-only; a separate key for clinical records; every read in the patient's access log | 0.64 |
| AI assistant inside the app | Dentrix Ascend: AI agents through its MCP layer | Read-only, the person's own permissions, no patient names ever sent, every read in the patient's access log | 0.65 |
| Patient side in Spanish, Portuguese and Arabic | Open Dental and Dentrix: English (Open Dental has community translations) | Booking, private page, forms, paying, check-in, documents and messages; Arabic right to left | 0.66 |
| Evidence pack for an outside security audit | Dentrix and Open Dental: audit trail reports; the rest is the vendor's own paperwork | 19 controls checked live, mapped to ISO 27001 and HIPAA; one zip with the evidence and a software parts list | 0.67 |
| Tamper-evident activity log | Open Dental: audit trail that shows in red any entry failing its check that it is unchanged; Dentrix: audit trail | The database refuses changes and deletions; nightly sealed chain, checkable later against a value the auditor keeps | 0.67 |
| Access reviews | Not built in | Quarterly, every login decided and kept; remove switches it off | 0.67 |
| Records the database itself protects | No published database-level lock found for either; both keep audit trails | Signed notes and payments cannot be altered even by direct database access; payments only voided | 0.68 |
| Company back office: departments with their own workspaces | Not something clinics see in either; vendors use separate CRM, help desk and billing tools | Dental OS HQ: leadership dashboard, support inbox with answer-by times and a help library, finance and VAT, sales pipeline, onboarding checklists, engineering health of clinic servers, feature requests ranked by clinics, compliance registers | 0.71, 0.72 |
| Roles for the vendor's own staff | Not something clinics see in either | Owner, customer service, finance, sales and engineering, each limited to its part; first sign-in sets password and two-step login | 0.69 |
| Protection from double charges | No published feature found for either | Buttons wait while sending; a repeated desk payment or reused mobile money reference is refused | 0.68 |

## Waiting on an outside party (the code is built)

- Canadian claims (CDAnet): CDA vendor certification and the CDAnet specification, which is not published. Nothing is built until the CDA provides it.
- A native speaker's check of the Spanish, Portuguese and Arabic.
- An independent penetration test (OWASP ASVS 5.0) and Bandit and pip-audit runs on a machine that can reach PyPI. Then the host's own
  evidence (disk encryption, updates, access to the server) and the clinic's written policies and training records for the audit.

- Clearinghouse: a first run with Stedi's test key and test payer, then a clinic's live account. Other clearinghouses (DentalXChange, Availity, Claim.MD) as adapters when a clinic uses them.

- E-prescribing: an adapter and certification for each network (DoseSpot or Surescripts in the US, NHS EPS in England), once there is a partner agreement. The pharmacy check and the FHIR copy need nothing outside.

- X-ray AI: an adapter for the clinic's chosen company (Pearl, Overjet, VideaHealth or another), once the clinic has an account and the company's API terms. The picture check needs nothing outside.

- Sending FP17s straight to the NHS Business Services Authority: needs NHSBSA accreditation of this software. Until then FP17s are prepared and checked here.
- A direct link to a named US clearinghouse: needs the clinic's clearinghouse account and its API or SFTP details. Download and upload works with any clearinghouse now.
- Live use of Twilio, Paystack, Stripe, WhatsApp Business, Hubtel and Africa's Talking: each clinic's own account, then one real test each. Hubtel's reply fields could not be fully confirmed from its public pages, so send a test SMS before switching SMS on.
- Real X-rays from each sensor make: a few files from each, to check the DICOM reader and the agent's folder settings.
- Licensed code sets: CDT (US) is typed in by the clinic from its own licence; other countries' licensed sets the same way.
- The NHIA's e-claims XML file for NHIS claims: needs the NHIA's current e-claims standard and the clinic's NHIS accreditation and credentials. Until then NHIS claims go as printed forms and a CSV.
- Terms of service and data processing terms for clinics that sign up: legal documents, written by a lawyer for each country sold in (in Ghana, with the Data Protection Act 2012 in mind).
- A native French speaker with dental knowledge to check the French.

## Left out on purpose, for now

- Voice for anything but gum charting (notes and the tooth chart). The gum chart is the one place hands are busy and the words are only numbers. Free-text notes would put patient details into the sound.
- Scotland, Wales and Northern Ireland NHS rules. Each has its own system; England is built first.
- Being a phone company or a card processor ourselves. Calls and payments run through the clinic's own Twilio, Paystack or Stripe account, so we never carry calls or hold money.
- Tax presets for every country. Version 0.12 has a checked starting point for Ghana, the UK, South Africa, Nigeria and Kenya, each with its sources and date. The rest need a checked source each.

## Sources

- [NHS England: preparing for NHS dental quality and payment contract reforms](https://www.england.nhs.uk/long-read/preparing-for-nhs-dental-quality-and-payment-contract-reforms/)
- [NHS dental charges from 1 April 2026 (poster)](https://ldc.org.uk/wp-content/uploads/2026/03/NHS-England-dental-charges-poster-April-2026.pdf)
- [NHSBSA: free NHS dental treatment](https://nhsbsa.nhs.uk/help-nhs-dental-costs/free-nhs-dental-treatment)
- [NHS dental contract guide 2026/27, UDA values](https://udapulse.co.uk/nhs-contract-guide/)
- [CDC: best practices for sterilization monitoring in dental settings](https://cdc.gov/dental-infection-control/hcp/dental-ipc-faqs/sterilization-monitoring.html)
- [HTM 01-05 testing requirements (The Dentist)](https://www.the-dentist.co.uk/content/news/testing-requirements)
- [Twilio: webhooks security](https://www.twilio.com/docs/usage/security)
- [837D to ADA claim form mapping (Redix)](https://redix.com/blogs/technical/837d-to-ada-j400)
- [Best Dental Practice Software in the UK (2026), Updent](https://updent.co.uk/blog/dental-practice-management-software-uk-comparison/)
- [UK Dental Practice Management Software Compared (2026)](https://www.dental-practice-software.com/uk/)
- [Best Dental Practice Management Software 2026: 12 Compared](https://dentalpracticeinsider.org/dental-practice-management-software/)
- [7 best dental practice management software in the US (2026), Pabau](https://pabau.com/blog/top-7-dental-practice-software-in-the-us-2026-guide/)
- [Introduction to Dental Patient Communication Software, Curve Dental](https://www.curvedental.com/dental-blog/intro-to-dental-patient-communication-software)
- [Best AI Dental Notes UK 2026, Dentistry Dashboard](https://dentistrydashboard.com/best-ai-dental-notes-uk)
- [Dental membership plan software features, Clerri](https://clerri.com/blog/dental-membership-plan-software-features/)
- [Dental treatment planning, CareStack](https://carestack.com/en-GB/dental-software/features/treatment-planning)
- [Treatment presentation, accept, sign, pay, Planet DDS help](https://support.planetdds.com/hc/en-us/articles/51549621423131-Denticon-Treatment-Presentation-Accept-Sign-Pay)
- [Best dental lab software, SoftSmile](https://softsmile.com/blog/best-dental-lab-software/)

Review articles are written by vendors and resellers, so they are good for what exists and unreliable for how well it works.
Claims about any one product should be checked against that product's own pages before we rely on them.
