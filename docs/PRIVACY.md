# Privacy and data protection: what the software does, and what a lawyer must decide

This document is for the person who owns the product and for the lawyer they ask to review it. It says what the software does
today with personal and health data, what it leaves to each clinic, and the questions that only a lawyer in each market can answer.
It is not legal advice and it does not say that the software meets any particular law.

## What the software stores

A clinic's data is kept in one SQLite database and one folder of files, both on the server the clinic or the host runs.
Every row belongs to one clinic, and a person from another clinic cannot see it (this is tested).

- Patients: name, phone, email, address, date of birth, gender, family link, language, notes, consent flags for messages and offers.
- Clinical record: medical history, alerts, tooth and gum charts, signed notes, consents, treatment plans, files (X-rays, photos, letters).
- Money: charges, payments, invoices, claims, memberships.
- Messages: the text of messages prepared for WhatsApp (name, dentist, day, time only), replies a person logs, booking requests.
- Activity log: who viewed or changed what. It never holds message text or note text.
- Not stored: national ID numbers, passport numbers, card numbers, bank account numbers, insurance policy documents.

## Who else receives data

- WhatsApp: the software opens a `wa.me` link on the staff member's device with the text ready, and a person presses send. The
  software does not call WhatsApp's servers. The message passes through the staff member's own WhatsApp account.
- Webhooks and the API: only if the clinic switches them on, to an address the clinic chooses. They carry no clinical data.
- Backups: the clinic's own. The software makes a copy of the database to a folder the clinic picks.
- The software sends nothing to the maker of the software. There is no analytics or error reporting service.

## Tools for data rights (managers, under Tools, Privacy)

| Tool | What it does | Starting point set in code | Question for the lawyer |
|---|---|---|---|
| Requests register | Records who asked, what they asked (access, correction, erasure, restriction, objection, portability, other), whether identity was checked, and the reply date. | 30 days to reply | What is the legal period in each market, and can it be extended? What identity check is enough? |
| Record copy | One patient's whole record as a printable page, or a zip with a data file and the patient's files (up to 150 MB of files). Every copy is logged. | none | Which parts may be withheld (for example third-party information, or harm to the patient)? Is a fee allowed? |
| Anonymise | Removes the name, contacts, address, date of birth (keeps the year) and free-text notes. Keeps treatment and money records. A reason and a typed word are needed. It cannot be undone. It is refused while the account is unsettled, a visit is booked, an insurance claim is open, or someone else has this person as their family head. | none | Is anonymising enough to answer a deletion request when the law also requires keeping health and tax records? For how long? |
| Incident register | Records what happened, what data, who is affected, risk, and whether the regulator and the patients were told, with a clock. | 72 hours from when the clinic found out | In which markets must the regulator be told, in what time, and when must patients be told? |
| Retention | Lists people whose last visit, charge, payment, note or message is older than a period the clinic chooses, and shows whether their account is settled. A manager reviews them. Nothing is removed automatically. | 10 years | What is the minimum and maximum keeping period for adult and child dental records in each market? When does a child's period start? |
| Access log | Who opened a patient's record, and when. | none | Is this log enough for the audit duty in each market, and how long must it be kept? |

## Security measures in the software

- Passwords stored with scrypt. Rules on length and common passwords. Lock-out after 5 wrong tries in 15 minutes.
- Optional two-step login. A clinic can require it.
- Sign-out after 60 minutes idle.
- Every form has a CSRF token. Pages are not cached. Cookies can be set to secure only.
- Files are stored under random names outside the web folder, and served only to signed-in clinical staff of the same clinic.
- The activity log records changes and views by user.
- The API never returns clinical data. Keys are stored hashed.

What the software does not do, and the host or clinic must: encrypt the disk and backups, use HTTPS, keep the server patched,
control who has the server, and train staff.

## Questions only a lawyer can answer, per market

1. Does the clinic or the product owner have to register with a data protection authority, and who is the controller and who the processor
   when the product is hosted for a clinic? What contract is needed between them?
2. Is patient consent needed for appointment messages and offers beyond what the consent flags record? Is WhatsApp acceptable as a channel?
3. Do health records have to stay in the country? If so, where may the host be?
4. The retention periods, reply periods and reporting periods in the table above.
5. Wording for the patient-facing pages (privacy notice, what the booking page and health form must tell the patient).
6. For French and other languages: are the translated consents and notices legally equal to the original?
7. For minors: who may ask for the record, and from what age may a young person ask for themselves?

## Before real patient data goes in

Get the review above for the first market, sign the contract between the product owner and each clinic, set `SECRET_KEY` to a long random
value and keep it safe (changing it breaks stored two-step secrets), turn on HTTPS and `COOKIE_SECURE=1`, schedule the nightly backup
and copy it off the machine, and ask every manager to turn on two-step login.
