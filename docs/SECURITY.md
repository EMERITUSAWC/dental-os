# Security and compliance: what Dental OS does, how to check it, and what is left to the clinic and the host

This is for the outside auditor, the person who owns the product, and the clinic's manager. It says how the software protects
patient data, which control does what, and how to prove each claim yourself rather than take it on trust. It is not a certificate.
Nothing here says that a clinic, a host or the product meets ISO/IEC 27001, HIPAA, the GDPR or Ghana's Data Protection Act. Those
depend on the clinic and the host as much as on the software, and only an auditor or a lawyer can say so.

Version 0.73. The privacy side (what is stored, who receives it, data rights) is in `docs/PRIVACY.md`.

## 1. How it is built

- One web application (Python 3, Flask) behind HTTPS, with SQLite or PostgreSQL and one folder of patient files.
- Many clinics can share one server. Every row carries its clinic's number, and every query is limited to the signed-in person's
  clinic. Tests try every page and every form as another clinic's manager and fail if anything leaks or changes
  (`tests/test_v56.py`).
- Pages are built on the server. The browser is told to run only the app's own scripts (`Content-Security-Policy: default-src 'self'`),
  never to show the app inside another site (`X-Frame-Options: DENY`), and not to keep signed-in pages in its cache.
- Outside services (WhatsApp, SMS, email, payments, AI, X-ray AI, prescribing, claims) are all off until a manager switches each one
  on. Each one's key is stored encrypted. The evidence pack lists which are on and what goes to each.

Trust boundaries: the visitor's browser; the public pages that need no login (online booking, a patient's private link, forms,
paying, the pharmacy check, `/.well-known/security.txt`); the signed-in staff pages; the API (a key per program); the server and
its database; each outside service.

## 2. Threats, and what stops each

| Threat | What stops it | Where it is tested |
|---|---|---|
| Someone guesses a password | 5 wrong tries per login and 30 per network address in 15 minutes; scrypt hashes; password rules; two-step login, which a clinic can require for everyone | `test_v56` LoginLimitTests, `test_v09` |
| A stolen session on a shared reception computer | Signed out after 60 idle minutes; cookies are HttpOnly, SameSite and, on https, Secure; HSTS | `test_v56` |
| One clinic sees or changes another's records | Clinic number on every query; crawl of every page and form as the wrong clinic | `test_v56` CrossClinicCrawl, CrossClinicWrites |
| A page opens without a login | Every page needs one except a short, named list | `test_v56` LoginNeededTests |
| Hostile text in a form or address (SQL injection, cross-site scripting) | Values always passed to the database separately from the SQL; templates escape by default; the code checker refuses SQL built from the request and unescaped output | `test_v67` HostileInputCrawl and CodeCheckTests |
| A forged form sent from another site (CSRF) | A token on every form | `test_v04` and on |
| A "go back to" link that sends the user to another site | Only paths on this site are followed | `test_v56` SafeUrlTests |
| A spreadsheet export that runs a formula | Cells starting with `=`, `+`, `-` or `@` are made plain text | `test_v56` SpreadsheetTests |
| A signed note or a payment is altered later | The database refuses to change a signed note's words, author or signature, or a payment's amount, date, method or receipt number; a payment is only voided, once | `test_v68` RecordLockTests |
| A form sent twice takes money twice | Buttons wait while a form is sent; the desk refuses the same payment within a minute and a reused mobile money or card reference | `test_v68` DeskPaymentTests |
| A discount paid out as cash | A refund cannot be more than the patient paid in | `test_v68` CreditFromNothingTests |
| A stolen operator password | Operators need a code from an authenticator app as well | `test_v68` OperatorTwoStepTests |
| The service's own staff see more than their job needs | Each person in HQ belongs to departments (support, finance, sales, onboarding, engineering, compliance) that reach only their part; owners run the team, with a 90-day review in the log | `test_v69` TeamTests, `test_v71` Departments |
| A leaver or contractor keeps access | Logins have start and end days; outside them sign-in is refused and an open session stops | `test_v71` LoginDates |
| HQ staff type a patient's details into a ticket or register | Phone numbers are refused in ticket, request and incident text; HQ tables have no patient columns | `test_v71` Support, Checks; `test_v72` Compliance |
| A fake clinic server reports to HQ | Each server has its own key, shown once, kept only as a hash, switchable off; only version, copy and disk fields are kept | `test_v72` Engineering |
| Someone covers their tracks in the activity log | The database refuses to change or delete log rows; daily seals show a change made around the database | `test_v67` LockTests and SealTests |
| A login nobody needs any more | Quarterly access review; remove switches the login off at once | `test_v67` ReviewTests |
| A disk, laptop or backup is lost | Off-site copies are locked with a passphrase before they leave; monthly restore drill. Disk encryption is the host's job | `test_v50`, `test_v58` |
| A hole in a library the app uses | The list of parts (SBOM) in each evidence pack; `tools/security-scan.sh` runs pip-audit against the public list of known holes | by hand, see section 5 |

## 3. The controls

Each row is a line of `controls.csv` in the evidence pack, with its state worked out on the day the pack was made. The ISO and
HIPAA columns say where the control sits, to help an auditor find their way. ISO/IEC 27001:2022 Annex A names: A.5.16 Identity
management, A.5.18 Access rights, A.5.19 and A.5.20 supplier relationships and agreements, A.5.24 and A.5.26 incident planning
and response, A.5.28 Collection of evidence, A.5.33 Protection of records, A.5.34 Privacy and protection of PII, A.6.3 awareness and training, A.8.2 Privileged
access rights, A.8.5 Secure authentication, A.8.7 Protection against malware, A.8.8 Management of technical vulnerabilities,
A.8.13 Information backup, A.8.15 Logging, A.8.24 Use of cryptography, A.8.28 Secure coding, A.8.29 Security testing in
development and acceptance. HIPAA references are to the Security Rule, 45 CFR Part 164 Subpart C.

| Control | ISO 27001:2022 | HIPAA | How to check it |
|---|---|---|---|
| Every person has their own login | A.5.16 | 164.312(a)(2)(i) | `users.csv`; the clinic shows no login is shared |
| Two-step login | A.8.5 | 164.312(d) | Settings, Staff; `users.csv` |
| Automatic sign-out when idle | A.8.5 | 164.312(a)(2)(iii) | `settings.txt` |
| Limits on wrong passwords | A.8.5 | 164.308(a)(5)(ii)(C) | `settings.txt`; LoginLimitTests |
| Access review in the last 90 days | A.5.18 | 164.308(a)(4) | `access_reviews.csv` and the newest review's decisions |
| Managers kept to a few | A.8.2 | 164.308(a)(4) | `users.csv` |
| Activity log cannot be changed or deleted | A.8.15 | 164.312(b) | `flask audit-verify` reports the database locks |
| Signed notes and recorded payments cannot be changed | A.5.33 | 164.312(c)(1) | `controls.csv`; `tests/test_v68.py` RecordLockTests |
| Activity log sealed and intact | A.8.15, A.5.28 | 164.312(b), 164.312(c)(1) | `audit_chain.txt`; `flask audit-verify --anchor` |
| Locked off-site copies every night | A.8.13 | 164.308(a)(7)(ii)(A) | `backups.csv` |
| Restore drill in the last 35 days | A.8.13 | 164.308(a)(7)(ii)(D) | `backups.csv` |
| HTTPS and secure cookies | A.8.24 | 164.312(e)(1) | `settings.txt`; the response headers |
| Outside services' keys stored encrypted | A.8.24 | 164.312(a)(2)(iv) | the code (`services/*` "encrypted"); the database shows no key in plain text |
| Data breaches written down and reported in time | A.5.24, A.5.26 | 164.308(a)(6) | `incidents.csv` |
| Patients' data requests answered in time | A.5.34 | | `data_requests.csv` |
| Outside services listed, each with a contract | A.5.19, A.5.20 | 164.308(b)(1) | `outside_services.csv`, then the clinic's contracts |
| List of the software's parts | A.8.8 | 164.308(a)(1)(ii)(A) | `sbom.cdx.json` |
| Automatic code checks | A.8.28, A.8.29 | | `code_checks.txt`; the test suite |
| A way to report a security problem | A.8.8 | | `/.well-known/security.txt` |
| Disk encryption, server updates, staff training | A.8.7, A.8.8, A.6.3 | 164.308(a)(5)(i), 164.310(d)(1) | the host's and the clinic's own evidence |

## 4. The activity log

Every sign-in, every record opened, every change, every export and every outside read (FHIR, the assistant) is written to the
activity log, with who, when and what, but never the text of a note or a message.

Locked: since 0.67 the database itself refuses to change or delete a row of the log or of its seals, whatever program asks
(SQLite triggers; PostgreSQL row and TRUNCATE triggers). The app puts the locks back each time it starts.

Sealed: each night (`flask audit-seal`, in the cron file) every new row is added to a chain: a keyed hash (HMAC-SHA256) of each
row in turn, with the result written as a seal. `flask audit-verify` walks every row again and compares. A row changed, removed or
slipped in by someone who got around the locks breaks the chain from that seal on, and the check says which seal.

What the seal cannot do, said plainly: the key comes from the server's `SECRET_KEY`. Someone with that key **and** write access to
the database could rebuild every seal after a change. That is why each evidence pack and the compliance page print the newest
seal value (`UPTO:CHAIN`). An auditor who keeps it can later run `flask audit-verify --anchor UPTO:CHAIN`: if the history up to
that point was rewritten, the value no longer matches. Keep it somewhere the server's people cannot change, such as the audit file.

If `SECRET_KEY` is ever changed, seals made with the old key cannot be checked any more (the check counts them separately rather
than calling them broken). Run `flask audit-verify` and keep an evidence pack before changing it. A seal from an old key that
appears after one from the current key is treated as forged.

A database owner can still switch the locks off. The check reports locks that are missing, and the seals still catch what was done
while they were off.

## 5. How an auditor checks the claims

On a copy of the clinic's server, or on the software alone:

```
python -m unittest discover -s tests -t .           # about 1,000 tests; every claim in section 2 has one
DENTAL_OS_TEST_PG=postgresql://... python -m unittest discover -s tests -t .   # the same against PostgreSQL
sh tools/security-scan.sh                            # the app's own code checks, then Bandit and pip-audit if installed
flask audit-verify                                   # the log against its seals, and the database locks
flask audit-verify --anchor 1234:ab12...             # with a seal value kept from the last audit
flask audit-pack --clinic-id 1 --out pack.zip        # the evidence pack; a manager can also download it
```

Bandit and pip-audit need the internet. They were not run where version 0.67 was built, because that machine could not reach PyPI.
Run them before the audit and keep the output with the pack.

For the penetration test, use an independent firm and the OWASP Application Security Verification Standard (ASVS) version 5.0.
Agree the level with the firm; Level 2 is a common choice for apps that hold sensitive data. Give them a test server with made-up patients
(`flask seed-demo`), a login for each role, and an API key.

## 6. The access review

Settings, Security and compliance, Access review. Every 90 days, and whenever someone leaves, a manager looks at every login that is
switched on: role, permission group, two-step login and last sign-in, with flags for a login with no two-step login, one unused for
60 days or more, one never used, and every manager. For each they choose keep, change (with a note) or remove (with a note). Remove
switches the login off at once. A manager cannot remove their own login. The review and every decision are kept and go in the
evidence pack.

## 7. Reporting a security problem

Set `SECURITY_CONTACT` (for example `mailto:security@yourclinic.com`) and the app publishes `/.well-known/security.txt` as RFC 9116
describes, with the contact, an expiry date six months ahead and the languages. `SECURITY_POLICY_URL` adds a link to a written
policy. Until a contact is set the address answers "not found" rather than a promise nobody keeps.

## 8. What the software cannot do

The host and the clinic must do these, and show the auditor their own evidence:

- Encrypt the server's disk and every backup disk. Keep the server updated. Protect it from malware.
- Control who can get to the server, its console and its database, and keep that list short.
- Keep `SECRET_KEY`, the off-site passphrase and the database password out of email and chat, and change them when someone with
  access leaves (see section 4 about `SECRET_KEY`).
- Written policies: access control, passwords and two-step login, incidents and breach reporting, backups and restore, acceptable use,
  and a risk assessment. Train staff on them and keep the records.
- A contract with each outside service that is switched on, covering patient data. Where data leaves the country, a legal basis.
- Register with the data protection authority where the law requires it.

## 9. The laws, in short

These are starting points for the clinic's lawyer, not legal advice.

- Ghana, Data Protection Act, 2012 (Act 843): those who process personal data must apply security safeguards, and a data controller
  must make sure its processors do too. Where there are reasonable grounds to think someone unauthorised has reached personal data,
  the controller must tell the Data Protection Commission and the person affected as soon as reasonably practicable after finding out.
  Data controllers must register with the Commission, and the registration lasts two years (DLA Piper's summary of the Act). I could
  not confirm the section numbers from an official copy of the Act, so take them from the Act itself.
- United States, HIPAA Security Rule: the current rule is in force. In January 2025 HHS proposed a large update (for example
  encryption and multi-factor authentication would no longer be "addressable"). Its final rule was most recently scheduled for July
  2027 on OCR's regulatory agenda (Alston & Bird, July 2026), so it is not law yet. Dental OS already does much of what was proposed:
  two-step login, encryption of keys, an asset list (the SBOM) and restore drills.
- UK and EU, GDPR: Article 32 asks for security fit for the risk, including the ability to restore data and regular testing; Article 33
  asks for a breach to be reported to the regulator within 72 hours where feasible. The incident register's clock is set to 72 hours.

## Sources

- [DLA Piper, Data protection laws of the world: Ghana](https://www.dlapiperdataprotection.com/index.html?c=GH&t=law)
- [Alston & Bird, HHS delays HIPAA Security Rule to 2027 (July 2026)](https://www.alston.com/en/insights/publications/2026/07/hhs-delays-hipaa-security-rule-2027)
- [Microlise, ISO/IEC 27001:2022 Statement of Applicability (control names)](https://www.microlise.com/microlise-group-plc-statement-of-applicability/)
- [OWASP ASVS repository (5.0.0 is the stable release)](https://github.com/rfricz/ASVS/releases/tag/latest)
- RFC 9116, A File Format to Aid in Security Vulnerability Disclosure (IETF, 2022)
