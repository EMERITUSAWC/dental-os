# Changes in 0.71 and 0.72: Dental OS HQ

The company's own side, apart from the clinics. Everyone has their own login; departments decide what each person reaches.

## 0.71

- `services/opteam.py`: `DEPTS` and `DEPT_ABOUT` (support, finance, sales, onboarding, engineering, compliance); `CAN` now maps each
  action to departments, owners reach everything. `memberships`, `heads_of`, `set_departments`, `set_dates`, `in_dates`. A 0.69
  login with a role and no departments counts as being in that role's department.
- `operators`: `starts_on`, `ends_on`, `email`. Sign-in outside the days is refused and an open session stops.
- New tables: `hq_members`, `hq_tickets`, `hq_ticket_notes`, `hq_articles`, `hq_leads`, `hq_lead_notes`, `hq_projects`,
  `hq_project_steps`, `hq_trainings`. None has a patient column.
- `services/hq.py`: support (tickets, answer-by and solve-by times by priority, bringing in shared problem reports once, phone
  numbers refused), help library, sales (stages, pipeline, salesperson figures, handover), onboarding (eleven steps, training),
  finance (subscriptions, monthly income, money owed, overdue, VAT report and CSV), leadership (`company`), and `my_work` for
  the HQ home.
- `hqviews.py`: `/hq`, `/hq/leadership`, `/hq/departments/<dept>`, `/hq/support`, `/hq/help`, `/hq/finance`, `/hq/finance/vat`,
  `/hq/sales`, `/hq/onboarding` and their item pages; `/help` and `/help/<id>` for clinic staff.
- `HQ_HOST`: HQ only on its own address. Sign-in now lands on `/hq`.
- Team page: departments with heads, start and end days, work email.
- French: `i18n_fr_staff35.py`. Tests: `tests/test_v71.py` (28).

## 0.72

- New tables: `hq_requests`, `hq_request_clinics`, `hq_releases`, `hq_installs`, `hq_incidents`, `hq_data_requests`,
  `hq_subprocessors`, `hq_registrations`.
- `services/hqtech.py`: server health, install keys (hash only), `checkin`, `send_report`, flags for clinic servers, feature
  requests ranked by clinics, release notes, incidents with a 72-hour clock, data requests with a 30-day date, outside companies,
  registrations, and `access_check` for HQ itself.
- `/hq/engineering`, `/hq/requests`, `/hq/compliance` and item pages; `POST /hq/checkin` (bearer install key, JSON);
  `/help/whats-new` for clinics; `flask hq-report` (hourly in the cron file, silent without `HQ_URL`).
- French: `i18n_fr_staff36.py`. Tests: `tests/test_v72.py` (14).
