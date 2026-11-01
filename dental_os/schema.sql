-- Dental OS schema. Written for SQLite now, kept simple so it ports to PostgreSQL.
-- Every business table carries clinic_id. Code always filters by it.
-- Times are clinic-local wall clock text ("YYYY-MM-DD HH:MM"), in the clinic's own timezone.
-- Deliberately absent: national ID, SSN, insurance numbers. Add them only if a clinic needs them.

CREATE TABLE IF NOT EXISTS clinics (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  phone TEXT,
  whatsapp TEXT,                -- number patients message, E.164
  country TEXT NOT NULL,        -- ISO 3166 two-letter code, for example GH, NG, GB, US
  currency TEXT NOT NULL,       -- ISO 4217 code, for example GHS, NGN, GBP, USD
  timezone TEXT NOT NULL,       -- IANA name, for example Africa/Accra
  payment_methods TEXT NOT NULL DEFAULT 'cash,card,bank_transfer,insurance',  -- ways of paying this clinic accepts
  open_time TEXT NOT NULL DEFAULT '08:00',
  close_time TEXT NOT NULL DEFAULT '17:00',
  slot_minutes INTEGER NOT NULL DEFAULT 30 CHECK (slot_minutes IN (10,15,20,30,60)),
  working_days TEXT NOT NULL DEFAULT '1,2,3,4,5,6',   -- ISO weekday numbers, Monday = 1
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('manager','front_desk','dentist','assistant')),
  password_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS providers (            -- people who treat patients
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS chairs (               -- surgeries / operatories
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS patients (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  first_name TEXT NOT NULL,
  last_name TEXT NOT NULL,
  dob TEXT,                                       -- YYYY-MM-DD
  gender TEXT NOT NULL DEFAULT '' CHECK (gender IN ('','Female','Male')),
  phone TEXT NOT NULL,                            -- E.164
  email TEXT,
  address TEXT,
  guarantor_id INTEGER REFERENCES patients(id),   -- family head, who pays
  preferred_contact TEXT NOT NULL DEFAULT 'whatsapp' CHECK (preferred_contact IN ('whatsapp','call','email')),
  reminders_ok INTEGER NOT NULL DEFAULT 0,        -- patient agreed to appointment messages
  offers_ok INTEGER NOT NULL DEFAULT 0,           -- patient agreed to birthday and seasonal offers
  notes TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_patients_clinic_name ON patients(clinic_id, last_name, first_name);
CREATE INDEX IF NOT EXISTS idx_patients_clinic_phone ON patients(clinic_id, phone);

CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  provider_id INTEGER REFERENCES providers(id),
  chair_id INTEGER REFERENCES chairs(id),
  start_at TEXT,                                  -- NULL while on the waiting list
  duration_min INTEGER NOT NULL DEFAULT 30 CHECK (duration_min BETWEEN 5 AND 480),
  status TEXT NOT NULL DEFAULT 'scheduled'
    CHECK (status IN ('planned','unscheduled','asap','scheduled','complete','broken')),
  confirmation TEXT NOT NULL DEFAULT 'none'
    CHECK (confirmation IN ('none','sent','confirmed','declined','failed')),
  category TEXT NOT NULL DEFAULT 'Consultation',
  notes TEXT,
  break_reason TEXT CHECK (break_reason IN ('missed','cancelled') OR break_reason IS NULL),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (status NOT IN ('scheduled','complete') OR (start_at IS NOT NULL AND provider_id IS NOT NULL AND chair_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_appt_clinic_start ON appointments(clinic_id, start_at);
CREATE INDEX IF NOT EXISTS idx_appt_patient ON appointments(clinic_id, patient_id);

-- Every message we send or prepare. Reports on confirmations and no-shows are measured from this.
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  appointment_id INTEGER REFERENCES appointments(id),
  channel TEXT NOT NULL DEFAULT 'whatsapp',
  direction TEXT NOT NULL DEFAULT 'out' CHECK (direction IN ('out','in')),
  kind TEXT NOT NULL,                             -- confirmation, reminder
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'opened',          -- opened = staff opened WhatsApp with the text ready
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_patient ON messages(clinic_id, patient_id);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER,
  user_id INTEGER,
  action TEXT NOT NULL,
  object_type TEXT,
  object_id INTEGER,
  detail TEXT,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_clinic_at ON audit_log(clinic_id, at);

CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL COLLATE NOCASE,
  ok INTEGER NOT NULL,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_login_username_at ON login_attempts(username, at);

-- ===================== v0.2: treatment, money, recalls =====================
-- Money is stored in minor units (whole numbers) so there are never rounding surprises: pesewas, kobo, cents, pence.
-- The clinic's currency says what the unit is. Columns ending _p hold minor units.
-- Procedure codes are our own short codes, not the American Dental Association's CDT codes (those are licensed).

CREATE TABLE IF NOT EXISTS procedure_codes (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'General',
  fee_p INTEGER NOT NULL DEFAULT 0 CHECK (fee_p >= 0),
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE (clinic_id, code)
);

CREATE TABLE IF NOT EXISTS procedures (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  appointment_id INTEGER REFERENCES appointments(id),
  code_id INTEGER NOT NULL REFERENCES procedure_codes(id),
  tooth INTEGER,                                   -- FDI number, for example 36
  surfaces TEXT,                                   -- any of M O D B L I
  status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','done','cancelled','voided')),
  fee_p INTEGER NOT NULL DEFAULT 0 CHECK (fee_p >= 0),
  provider_id INTEGER REFERENCES providers(id),
  planned_on TEXT NOT NULL,
  done_on TEXT,
  notes TEXT,
  void_reason TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_proc_patient ON procedures(clinic_id, patient_id, status);

CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  amount_p INTEGER NOT NULL CHECK (amount_p > 0),
  method TEXT NOT NULL CHECK (method IN ('cash','mobile_money','card','bank_transfer','insurance')),
  reference TEXT,
  note TEXT,
  receipt_no INTEGER NOT NULL,
  received_on TEXT NOT NULL,
  received_by INTEGER REFERENCES users(id),
  voided INTEGER NOT NULL DEFAULT 0,
  void_reason TEXT,
  voided_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (clinic_id, receipt_no)
);
CREATE INDEX IF NOT EXISTS idx_pay_clinic_day ON payments(clinic_id, received_on);

CREATE TABLE IF NOT EXISTS adjustments (            -- negative = discount, positive = extra charge
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  amount_p INTEGER NOT NULL CHECK (amount_p != 0),
  reason TEXT NOT NULL CHECK (reason IN ('birthday_offer','seasonal_offer','discount','write_off','correction')),
  note TEXT,
  adjusted_on TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_adj_patient ON adjustments(clinic_id, patient_id);

CREATE TABLE IF NOT EXISTS recall_types (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  interval_months INTEGER NOT NULL CHECK (interval_months BETWEEN 1 AND 60),
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS recalls (                 -- one per patient per type
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  type_id INTEGER NOT NULL REFERENCES recall_types(id),
  due_on TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'due' CHECK (status IN ('due','contacted','disabled')),
  contacted_on TEXT,
  snoozed_until TEXT,
  UNIQUE (clinic_id, patient_id, type_id)
);
CREATE INDEX IF NOT EXISTS idx_recalls_due ON recalls(clinic_id, due_on);

-- ===================== v0.4: the clinical record =====================
-- Nothing clinical is ever deleted. Alerts are resolved, conditions are removed with a date, notes are signed and then
-- only added to, files are archived, consents are withdrawn.

CREATE TABLE IF NOT EXISTS medical_history (          -- one current form per patient
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  conditions TEXT,
  medications TEXT,
  allergies TEXT,
  pregnant TEXT NOT NULL DEFAULT '' CHECK (pregnant IN ('','yes','no')),
  smoker TEXT NOT NULL DEFAULT '' CHECK (smoker IN ('','never','former','current')),
  blood_thinners INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  reviewed_on TEXT,
  updated_by INTEGER REFERENCES users(id),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (clinic_id, patient_id)
);

CREATE TABLE IF NOT EXISTS patient_alerts (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  kind TEXT NOT NULL CHECK (kind IN ('allergy','medical','medication','behaviour','other')),
  severity TEXT NOT NULL DEFAULT 'warning' CHECK (severity IN ('info','warning','critical')),
  text TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','history')),
  active INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_by INTEGER REFERENCES users(id),
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_patient ON patient_alerts(clinic_id, patient_id, active);

CREATE TABLE IF NOT EXISTS tooth_conditions (         -- what is in the mouth already (FDI tooth numbers)
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  tooth INTEGER NOT NULL,
  surfaces TEXT,
  condition TEXT NOT NULL,
  note TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','removed')),
  recorded_by INTEGER REFERENCES users(id),
  recorded_on TEXT NOT NULL,
  removed_by INTEGER REFERENCES users(id),
  removed_on TEXT
);
CREATE INDEX IF NOT EXISTS idx_conditions_patient ON tooth_conditions(clinic_id, patient_id, status);

CREATE TABLE IF NOT EXISTS perio_charts (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  charted_on TEXT NOT NULL,
  charted_by INTEGER REFERENCES users(id),
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_perio_patient ON perio_charts(clinic_id, patient_id, charted_on);

CREATE TABLE IF NOT EXISTS perio_readings (           -- six sites per tooth
  id INTEGER PRIMARY KEY,
  chart_id INTEGER NOT NULL REFERENCES perio_charts(id),
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  tooth INTEGER NOT NULL,
  site TEXT NOT NULL CHECK (site IN ('DB','B','MB','DL','L','ML')),
  pocket_mm INTEGER CHECK (pocket_mm BETWEEN 0 AND 15),
  recession_mm INTEGER CHECK (recession_mm BETWEEN -5 AND 15),
  bleeding INTEGER NOT NULL DEFAULT 0,
  UNIQUE (chart_id, tooth, site)
);

CREATE TABLE IF NOT EXISTS clinical_notes (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  appointment_id INTEGER REFERENCES appointments(id),
  tooth INTEGER,
  template TEXT NOT NULL DEFAULT 'free',
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','signed','discarded')),
  addendum_of INTEGER REFERENCES clinical_notes(id),
  author_id INTEGER REFERENCES users(id),
  signed_by INTEGER REFERENCES users(id),
  signed_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notes_patient ON clinical_notes(clinic_id, patient_id, created_at);

CREATE TABLE IF NOT EXISTS consent_templates (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  body TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (clinic_id, name)
);

CREATE TABLE IF NOT EXISTS consents (                  -- a snapshot of the exact wording that was agreed to
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  template_id INTEGER REFERENCES consent_templates(id),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  procedure_id INTEGER REFERENCES procedures(id),
  signer_name TEXT NOT NULL,
  signer_relation TEXT NOT NULL DEFAULT 'self' CHECK (signer_relation IN ('self','parent','guardian','other')),
  method TEXT NOT NULL DEFAULT 'typed' CHECK (method IN ('typed','paper')),
  signed_at TEXT NOT NULL,
  recorded_by INTEGER REFERENCES users(id),
  note TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','withdrawn')),
  withdrawn_on TEXT,
  withdrawn_by INTEGER REFERENCES users(id),
  withdrawn_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_consents_patient ON consents(clinic_id, patient_id);

CREATE TABLE IF NOT EXISTS patient_files (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  kind TEXT NOT NULL DEFAULT 'other' CHECK (kind IN ('xray','photo','document','referral','consent','other')),
  original_name TEXT NOT NULL,
  stored_name TEXT NOT NULL UNIQUE,
  mime TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  note TEXT,
  taken_on TEXT,
  uploaded_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_patient ON patient_files(clinic_id, patient_id, archived);

CREATE TABLE IF NOT EXISTS import_batches (            -- a preview of a patient list import waiting to be confirmed
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  created_by INTEGER REFERENCES users(id),
  kind TEXT NOT NULL DEFAULT 'patients',
  payload TEXT NOT NULL,
  summary TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'preview' CHECK (status IN ('preview','done','cancelled')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---- v0.5: treatment plans, memberships and instalment plans -------------------------------------------------
CREATE TABLE IF NOT EXISTS treatment_plans (            -- one plan = one option. A patient can have several.
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','presented','accepted','declined')),
  presented_on TEXT,
  decided_on TEXT,
  signer_name TEXT,
  signer_relation TEXT CHECK (signer_relation IN ('self','parent','guardian','other')),
  accept_method TEXT CHECK (accept_method IN ('typed','paper')),
  decline_reason TEXT,
  owner_id INTEGER REFERENCES users(id),               -- who follows this plan up
  follow_up_on TEXT,
  note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS plans_by_patient ON treatment_plans(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS plans_by_status ON treatment_plans(clinic_id, status, follow_up_on);
CREATE TABLE IF NOT EXISTS plan_items (                 -- lines on a plan. They become real planned work when the plan is accepted.
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  plan_id INTEGER NOT NULL REFERENCES treatment_plans(id),
  code_id INTEGER NOT NULL REFERENCES procedure_codes(id),
  tooth INTEGER,
  surfaces TEXT,
  fee_p INTEGER NOT NULL DEFAULT 0 CHECK (fee_p >= 0),
  phase INTEGER NOT NULL DEFAULT 1 CHECK (phase BETWEEN 1 AND 9),
  note TEXT,
  procedure_id INTEGER REFERENCES procedures(id),
  removed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS items_by_plan ON plan_items(clinic_id, plan_id);
CREATE TABLE IF NOT EXISTS plan_events (                -- the history of a plan: created, presented, chased, accepted, declined
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  plan_id INTEGER NOT NULL REFERENCES treatment_plans(id),
  kind TEXT NOT NULL,
  note TEXT,
  by_user INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS membership_plans (           -- what a clinic sells: a fee, a billing cycle, included services, a discount
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  description TEXT,
  fee_p INTEGER NOT NULL CHECK (fee_p > 0),
  cycle TEXT NOT NULL CHECK (cycle IN ('monthly','quarterly','yearly')),
  discount_pct INTEGER NOT NULL DEFAULT 0 CHECK (discount_pct BETWEEN 0 AND 100),
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE (clinic_id, name)
);
CREATE TABLE IF NOT EXISTS membership_benefits (        -- included services, a number of times in each membership year
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  plan_id INTEGER NOT NULL REFERENCES membership_plans(id),
  code_id INTEGER NOT NULL REFERENCES procedure_codes(id),
  qty_per_year INTEGER NOT NULL CHECK (qty_per_year BETWEEN 1 AND 99),
  UNIQUE (plan_id, code_id)
);
CREATE TABLE IF NOT EXISTS memberships (                -- a patient's membership. Fee, cycle and discount are copied at enrolment.
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  plan_id INTEGER NOT NULL REFERENCES membership_plans(id),
  plan_name TEXT NOT NULL,
  fee_p INTEGER NOT NULL,
  cycle TEXT NOT NULL,
  discount_pct INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','lapsed','cancelled')),
  started_on TEXT NOT NULL,
  next_due_on TEXT NOT NULL,
  cancelled_on TEXT,
  cancel_reason TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_membership ON memberships(patient_id) WHERE status IN ('active','lapsed');
CREATE TABLE IF NOT EXISTS membership_charges (         -- each fee that falls due
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  membership_id INTEGER NOT NULL REFERENCES memberships(id),
  due_on TEXT NOT NULL,
  amount_p INTEGER NOT NULL CHECK (amount_p > 0),
  status TEXT NOT NULL DEFAULT 'due' CHECK (status IN ('due','paid','failed','waived')),
  payment_id INTEGER REFERENCES payments(id),
  paid_on TEXT,
  note TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS mcharges_open ON membership_charges(clinic_id, status, due_on);
CREATE TABLE IF NOT EXISTS membership_uses (            -- when a member's benefit was applied to finished work
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  membership_id INTEGER NOT NULL REFERENCES memberships(id),
  procedure_id INTEGER NOT NULL UNIQUE REFERENCES procedures(id),
  kind TEXT NOT NULL CHECK (kind IN ('included','discount')),
  amount_p INTEGER NOT NULL,
  adjustment_id INTEGER REFERENCES adjustments(id),
  used_on TEXT NOT NULL,
  reversed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payment_plans (              -- a balance paid in instalments
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  treatment_plan_id INTEGER REFERENCES treatment_plans(id),
  total_p INTEGER NOT NULL CHECK (total_p > 0),
  instalment_count INTEGER NOT NULL CHECK (instalment_count BETWEEN 2 AND 36),
  frequency TEXT NOT NULL CHECK (frequency IN ('weekly','fortnightly','monthly')),
  start_on TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','cancelled')),
  note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS instalments (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  payment_plan_id INTEGER NOT NULL REFERENCES payment_plans(id),
  due_on TEXT NOT NULL,
  amount_p INTEGER NOT NULL CHECK (amount_p > 0),
  status TEXT NOT NULL DEFAULT 'due' CHECK (status IN ('due','paid','waived')),
  payment_id INTEGER REFERENCES payments(id),
  paid_on TEXT
);
CREATE INDEX IF NOT EXISTS instalments_open ON instalments(clinic_id, status, due_on);

-- ---- v0.6: insurance and other payers, claims, invoices -------------------------------------------------------
CREATE TABLE IF NOT EXISTS payers (                    -- insurers, employers, schemes: anyone who pays part of a bill
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'insurer' CHECK (kind IN ('insurer','employer','government','other')),
  contracted INTEGER NOT NULL DEFAULT 0,               -- 1: the clinic accepts the payer's allowed fee, and writes off the difference
  phone TEXT,
  email TEXT,
  notes TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE (clinic_id, name)
);
CREATE TABLE IF NOT EXISTS payer_fees (                -- the payer's own price for a procedure. No row means the clinic's price.
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  payer_id INTEGER NOT NULL REFERENCES payers(id),
  code_id INTEGER NOT NULL REFERENCES procedure_codes(id),
  fee_p INTEGER NOT NULL CHECK (fee_p >= 0),
  UNIQUE (payer_id, code_id)
);
CREATE TABLE IF NOT EXISTS patient_cover (             -- a patient's cover with a payer
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  payer_id INTEGER NOT NULL REFERENCES payers(id),
  member_no TEXT,
  holder_name TEXT,
  coverage_pct INTEGER NOT NULL DEFAULT 80 CHECK (coverage_pct BETWEEN 0 AND 100),
  annual_max_p INTEGER CHECK (annual_max_p IS NULL OR annual_max_p > 0),
  valid_from TEXT,
  valid_to TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS cover_by_patient ON patient_cover(clinic_id, patient_id);
CREATE TABLE IF NOT EXISTS claims (                    -- a request to a payer to pay for finished work, or to approve planned work first
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  claim_no INTEGER NOT NULL,
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  payer_id INTEGER NOT NULL REFERENCES payers(id),
  cover_id INTEGER REFERENCES patient_cover(id),
  kind TEXT NOT NULL DEFAULT 'claim' CHECK (kind IN ('claim','preauth')),
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted','partly_paid','paid','approved','rejected','cancelled')),
  submitted_on TEXT,
  total_fee_p INTEGER NOT NULL DEFAULT 0,
  estimate_p INTEGER NOT NULL DEFAULT 0,               -- what the payer is expected to pay (or approved, for a pre-authorisation)
  paid_p INTEGER NOT NULL DEFAULT 0,
  writeoff_p INTEGER NOT NULL DEFAULT 0,
  payer_ref TEXT,
  note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (clinic_id, claim_no)
);
CREATE INDEX IF NOT EXISTS claims_open ON claims(clinic_id, status, submitted_on);
CREATE TABLE IF NOT EXISTS claim_items (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  claim_id INTEGER NOT NULL REFERENCES claims(id),
  procedure_id INTEGER NOT NULL REFERENCES procedures(id),
  fee_p INTEGER NOT NULL,
  allowed_p INTEGER NOT NULL,
  estimate_p INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS claim_items_by_proc ON claim_items(clinic_id, procedure_id);
CREATE TABLE IF NOT EXISTS invoices (                  -- a numbered bill for finished work. Prices and tax are copied when it is issued.
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  invoice_no INTEGER NOT NULL,
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  issued_on TEXT NOT NULL,
  due_on TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'issued' CHECK (status IN ('issued','void')),
  subtotal_p INTEGER NOT NULL,
  tax_p INTEGER NOT NULL DEFAULT 0,
  total_p INTEGER NOT NULL,
  tax_name TEXT,
  tax_rate_bp INTEGER NOT NULL DEFAULT 0,
  tax_inclusive INTEGER NOT NULL DEFAULT 1,
  tax_number TEXT,
  note TEXT,
  void_reason TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (clinic_id, invoice_no)
);
CREATE TABLE IF NOT EXISTS invoice_lines (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  procedure_id INTEGER REFERENCES procedures(id),
  description TEXT NOT NULL,
  service_date TEXT,
  amount_p INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS invoice_lines_by_proc ON invoice_lines(clinic_id, procedure_id);

-- ===================== v0.7: messaging, campaigns, online booking, patient link =====================
-- Messages are prepared here and sent by a person pressing send in WhatsApp (no official API yet). Everything that goes out is
-- logged in `messages`. `outbox` is the list of messages waiting for a person to send.

CREATE TABLE IF NOT EXISTS message_templates (          -- one editable wording per kind of message, per clinic
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (clinic_id, kind)
);

CREATE TABLE IF NOT EXISTS reminder_rules (             -- "remind N days before the visit"
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  days_before INTEGER NOT NULL CHECK (days_before BETWEEN 0 AND 14),
  skip_confirmed INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE (clinic_id, days_before)
);

CREATE TABLE IF NOT EXISTS clinic_comms (               -- one row per clinic: which automatic messages are on, and online booking
  clinic_id INTEGER PRIMARY KEY REFERENCES clinics(id),
  auto_recall INTEGER NOT NULL DEFAULT 0,
  auto_review INTEGER NOT NULL DEFAULT 0,
  auto_birthday INTEGER NOT NULL DEFAULT 0,
  auto_plan INTEGER NOT NULL DEFAULT 0,
  review_link TEXT,
  booking_on INTEGER NOT NULL DEFAULT 0,
  booking_token TEXT UNIQUE,
  booking_days_ahead INTEGER NOT NULL DEFAULT 14 CHECK (booking_days_ahead BETWEEN 1 AND 60),
  booking_notice_hours INTEGER NOT NULL DEFAULT 12 CHECK (booking_notice_hours BETWEEN 0 AND 168),
  booking_minutes INTEGER NOT NULL DEFAULT 30,
  booking_note TEXT
);

CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('overdue_recall','lapsed','unaccepted_plans')),
  param_days INTEGER NOT NULL DEFAULT 30 CHECK (param_days BETWEEN 1 AND 1500),
  purpose TEXT NOT NULL DEFAULT 'care' CHECK (purpose IN ('care','offer')),   -- care needs "appointment messages" consent, offer needs "offers" consent
  body TEXT NOT NULL,
  min_gap_days INTEGER NOT NULL DEFAULT 14 CHECK (min_gap_days BETWEEN 0 AND 365),
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','closed')),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  built_at TEXT
);

CREATE TABLE IF NOT EXISTS outbox (                     -- messages waiting for a person to send, and what became of them
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  kind TEXT NOT NULL,                                   -- reminder, recall, review_request, birthday, plan_followup, campaign
  purpose TEXT NOT NULL DEFAULT 'care' CHECK (purpose IN ('care','offer')),
  appointment_id INTEGER REFERENCES appointments(id),
  recall_id INTEGER REFERENCES recalls(id),
  plan_id INTEGER REFERENCES treatment_plans(id),
  campaign_id INTEGER REFERENCES campaigns(id),
  dedupe_key TEXT NOT NULL,
  due_on TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sent','skipped')),
  skip_reason TEXT,
  message_id INTEGER REFERENCES messages(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  done_at TEXT,
  done_by INTEGER REFERENCES users(id),
  UNIQUE (clinic_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS outbox_by_status ON outbox(clinic_id, status, due_on);

CREATE TABLE IF NOT EXISTS booking_requests (           -- asked for on the public booking page. Not a booking until staff accept it.
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  first_name TEXT NOT NULL,
  last_name TEXT NOT NULL,
  phone TEXT NOT NULL,
  email TEXT,
  reason TEXT NOT NULL,
  wanted_start TEXT NOT NULL,                           -- YYYY-MM-DD HH:MM
  provider_id INTEGER REFERENCES providers(id),         -- NULL means first available
  note TEXT,
  agrees_messages INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','accepted','declined')),
  patient_id INTEGER REFERENCES patients(id),
  appointment_id INTEGER REFERENCES appointments(id),
  decline_reason TEXT,
  decided_by INTEGER REFERENCES users(id),
  decided_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS booking_by_status ON booking_requests(clinic_id, status);

CREATE TABLE IF NOT EXISTS patient_links (              -- a private link for one patient. Only a hash of the secret is stored.
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  token_hash TEXT NOT NULL UNIQUE,
  expires_on TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS links_by_patient ON patient_links(clinic_id, patient_id);

CREATE TABLE IF NOT EXISTS intake_submissions (         -- a health form the patient filled in. It does not touch the record until a clinician accepts it.
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  link_id INTEGER REFERENCES patient_links(id),
  answers TEXT NOT NULL,                                -- JSON
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','dismissed')),
  submitted_at TEXT NOT NULL DEFAULT (datetime('now')),
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS intake_by_status ON intake_submissions(clinic_id, status);

CREATE TABLE IF NOT EXISTS throttle (                   -- counts hits from the public pages, to slow down abuse. Keys are hashed.
  id INTEGER PRIMARY KEY,
  bucket TEXT NOT NULL,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS throttle_by_bucket ON throttle(bucket, at);

-- ===================== v0.8: organisations, labs, stock, API and webhooks =====================
CREATE TABLE IF NOT EXISTS organisations (              -- a group that owns several clinics. Each clinic stays separate.
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS org_members (                -- who may see the group view. Read only: it never opens another clinic's records.
  id INTEGER PRIMARY KEY,
  org_id INTEGER NOT NULL REFERENCES organisations(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  UNIQUE (org_id, user_id)
);

CREATE TABLE IF NOT EXISTS labs (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  phone TEXT,
  email TEXT,
  notes TEXT,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS lab_cases (                  -- one job sent to a dental laboratory
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  lab_id INTEGER NOT NULL REFERENCES labs(id),
  procedure_id INTEGER REFERENCES procedures(id),
  provider_id INTEGER REFERENCES providers(id),
  kind TEXT NOT NULL,                                   -- crown, bridge, denture, night guard ...
  tooth TEXT,
  shade TEXT,
  instructions TEXT,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent','received','fitted','cancelled')),
  due_on TEXT,
  sent_on TEXT,
  received_on TEXT,
  fitted_on TEXT,
  cost_p INTEGER NOT NULL DEFAULT 0 CHECK (cost_p >= 0),
  remake_of INTEGER REFERENCES lab_cases(id),
  fit_appointment_id INTEGER REFERENCES appointments(id),
  cancel_reason TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS lab_by_status ON lab_cases(clinic_id, status, due_on);
CREATE INDEX IF NOT EXISTS lab_by_patient ON lab_cases(clinic_id, patient_id);

CREATE TABLE IF NOT EXISTS lab_events (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  case_id INTEGER NOT NULL REFERENCES lab_cases(id),
  kind TEXT NOT NULL,
  note TEXT,
  by_user INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  phone TEXT,
  email TEXT,
  notes TEXT,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS stock_items (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  sku TEXT,
  unit TEXT NOT NULL DEFAULT 'each',
  category TEXT NOT NULL DEFAULT 'General',
  supplier_id INTEGER REFERENCES suppliers(id),
  reorder_level INTEGER NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
  reorder_qty INTEGER NOT NULL DEFAULT 0 CHECK (reorder_qty >= 0),
  cost_p INTEGER NOT NULL DEFAULT 0 CHECK (cost_p >= 0),     -- cost of one unit, last known
  location TEXT,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS stock_items_by_clinic ON stock_items(clinic_id, name);

CREATE TABLE IF NOT EXISTS stock_moves (                -- the level is the sum of these. Nothing is edited or deleted.
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  item_id INTEGER NOT NULL REFERENCES stock_items(id),
  qty INTEGER NOT NULL CHECK (qty != 0),                -- positive: stock came in. Negative: stock went out.
  kind TEXT NOT NULL CHECK (kind IN ('receive','use','waste','adjust','count')),
  unit_cost_p INTEGER NOT NULL DEFAULT 0,
  expires_on TEXT,
  note TEXT,
  moved_on TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS stock_moves_by_item ON stock_moves(clinic_id, item_id, id);

CREATE TABLE IF NOT EXISTS api_keys (                   -- a key lets another program read or write one clinic's data. Only a hash is kept.
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  key_prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  access TEXT NOT NULL DEFAULT 'read' CHECK (access IN ('read','write')),
  revoked INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS webhook_endpoints (          -- an address we tell when something happens
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  url TEXT NOT NULL,
  secret TEXT NOT NULL,
  events TEXT NOT NULL DEFAULT '*',
  active INTEGER NOT NULL DEFAULT 1,
  note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  endpoint_id INTEGER NOT NULL REFERENCES webhook_endpoints(id),
  event TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','dead')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_try_at TEXT NOT NULL DEFAULT (datetime('now')),
  response_code INTEGER,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS webhook_due ON webhook_deliveries(status, next_try_at);
CREATE INDEX IF NOT EXISTS webhook_by_endpoint ON webhook_deliveries(clinic_id, endpoint_id, id);

-- ===================== v0.9: tax rates, two-step login, data rights, incidents =====================
-- Tax: the clinic still has one default rate (clinics.tax_*). A clinic can add other named rates (reduced, zero, exempt) and give
-- each procedure code one of them. An invoice copies the rates it used, so changing settings later never changes an old invoice.

CREATE TABLE IF NOT EXISTS tax_rates (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'rate' CHECK (kind IN ('rate','zero','exempt')),
  rate_bp INTEGER NOT NULL DEFAULT 0 CHECK (rate_bp BETWEEN 0 AND 10000),
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE (clinic_id, name)
);

CREATE TABLE IF NOT EXISTS invoice_tax (                -- one row per tax rate used on an invoice
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  rate_bp INTEGER NOT NULL,
  net_p INTEGER NOT NULL,
  tax_p INTEGER NOT NULL,
  gross_p INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS invoice_tax_by_invoice ON invoice_tax(invoice_id);

CREATE TABLE IF NOT EXISTS recovery_codes (             -- one-time codes for when a phone is lost. Only hashes are kept.
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  code_hash TEXT NOT NULL,
  used_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS recovery_by_user ON recovery_codes(user_id);

-- Data rights and incidents. A request is someone asking about their data (see it, fix it, erase it). An incident is a loss, leak
-- or wrong disclosure of patient data. Both are records the clinic keeps to show it handled things properly.
CREATE TABLE IF NOT EXISTS data_requests (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER REFERENCES patients(id),
  requester TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('access','correction','erasure','restriction','objection','portability','other')),
  received_on TEXT NOT NULL,
  due_on TEXT NOT NULL,
  identity_checked INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','refused')),
  details TEXT,
  outcome TEXT,
  closed_on TEXT,
  created_by INTEGER REFERENCES users(id),
  handled_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS data_requests_by_clinic ON data_requests(clinic_id, status, due_on);

CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  title TEXT NOT NULL,
  occurred_on TEXT,
  found_at TEXT NOT NULL,                               -- UTC, when the clinic found out. Reporting clocks usually start here.
  what_happened TEXT NOT NULL,
  data_involved TEXT,
  people_affected INTEGER,
  risk TEXT NOT NULL DEFAULT 'low' CHECK (risk IN ('low','medium','high')),
  regulator_told INTEGER NOT NULL DEFAULT 0,
  regulator_on TEXT,
  patients_told INTEGER NOT NULL DEFAULT 0,
  patients_on TEXT,
  actions TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  closed_on TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS incidents_by_clinic ON incidents(clinic_id, status);
