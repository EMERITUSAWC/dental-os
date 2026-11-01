# Feature map: what the leading systems do, and our version

How this was made: from public product pages, vendor help pages and review articles (listed at the bottom). Each feature is
described here in our own words, then built from that note. Ideas and features are free to build. Nothing was copied: no code,
no screens, no wording, no images, no icons, no procedure-code tables that are licensed (for example CDT).
"Seen in" says where the idea is common, not that we copied that product.

Stages are build order. A stage ships as its own version. Stage 1 (clinical record) is built in version 0.4. Stage 2 (plans, quotes, memberships, instalments) is built in version 0.5. Stage 3 (payers, claims, invoices, ageing) is built in version 0.6. Stage 4 (messages, campaigns, online booking, patient link) is built in version 0.7. Stage 5 (key numbers, groups of clinics, lab cases, stock, API and webhooks) is built in version 0.8. Stage 6 (two-step login, data-rights tools, tax by treatment, procedure packs, French) is built in version 0.9.

## Clinical record

| Feature | Seen in | Our version | Stage |
|---|---|---|---|
| Medical history and allergy alerts that show wherever the patient appears | all major systems | A history form per patient, plus alerts (allergy, medical, medication, behaviour) with a severity. Critical alerts show in red on the patient, the diary and the appointment. | 1 |
| Tooth chart (odontogram) | Dentrix, Eaglesoft, R4, CareStack | FDI chart for adult and child teeth. Each tooth shows existing conditions, planned work and finished work. | 1 |
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
| Two-way inbox linked to the patient record | Weave, Solutionreach, Curve | Every message in and out is on the patient. An inbox for unread replies. Reading replies automatically needs the official WhatsApp business connection. | 4 |
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
| Languages | CareStack, Dentally | English and French for menus, sign-in, booking, a patient's page, printed documents and message wording. | 6 |

## Left out on purpose, for now

- NHS UDA tracking and FP17 claims (UK only). A country module, built when a UK clinic is a real customer.
- Electronic claims to US clearinghouses (835/837). Needs a clearinghouse contract and the licensed CDT code set.
- Imaging sensor bridges (Dexis, Schick, Carestream). Each needs the maker's agreement.
- Built-in phone system (VoIP). A different business.
- Card payment processing. We record payments. Processing needs a licensed provider per country.
- Tax presets for each country. What rate applies to dental work differs by country and changes, and needs a checked source for each.
- French for every staff screen. Version 0.9 covers the menus and everything a patient sees. The clinical and reporting screens are next.

## Sources

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
