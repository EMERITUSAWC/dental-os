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

-- The official WhatsApp Business API (version 0.11). One row per clinic. The token and the secret are stored encrypted.
CREATE TABLE IF NOT EXISTS wa_account (
  clinic_id INTEGER PRIMARY KEY REFERENCES clinics(id),
  enabled INTEGER NOT NULL DEFAULT 0,
  phone_number_id TEXT UNIQUE,
  access_token TEXT,
  app_secret TEXT,
  api_version TEXT NOT NULL DEFAULT 'v22.0',
  auto_send INTEGER NOT NULL DEFAULT 0,                 -- 1: the scheduled command sends the send list by itself
  hook_key TEXT UNIQUE,                                 -- part of the address Meta calls
  verify_token TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS wa_templates (               -- which template approved at Meta goes with which message, per language
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  kind TEXT NOT NULL,
  lang TEXT NOT NULL,
  template_name TEXT NOT NULL,
  language_code TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '',                      -- the fields, in the order the template's {{1}}, {{2}} expect them
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE (clinic_id, kind, lang)
);

CREATE TABLE IF NOT EXISTS wa_events (                  -- what Meta has told us, so a repeat is ignored, and messages from numbers we do not know
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  event_key TEXT NOT NULL,
  kind TEXT NOT NULL,                                   -- status, message, unmatched
  detail TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (clinic_id, event_key)
);

-- Optional AI help for drafting clinical notes (version 0.13). Off by default. One row per clinic. The key is stored encrypted.
CREATE TABLE IF NOT EXISTS ai_account (
  clinic_id INTEGER PRIMARY KEY REFERENCES clinics(id),
  enabled INTEGER NOT NULL DEFAULT 0,
  api_key TEXT,
  model TEXT NOT NULL DEFAULT 'claude-sonnet-5-5',
  updated_at TEXT
);

-- Version 0.16: diary depth. Each dentist's own hours, time off, blocked time and appointment types.
CREATE TABLE IF NOT EXISTS provider_hours (            -- a dentist's working hours. No rows: the dentist works the clinic's opening hours
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  provider_id INTEGER NOT NULL REFERENCES providers(id),
  weekday INTEGER NOT NULL CHECK (weekday BETWEEN 1 AND 7),   -- ISO weekday, Monday = 1
  start_time TEXT NOT NULL,                             -- HH:MM
  end_time TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_hours ON provider_hours(clinic_id, provider_id, weekday);

CREATE TABLE IF NOT EXISTS provider_time_off (         -- holidays, courses, sick days. A whole day unless times are given
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  provider_id INTEGER NOT NULL REFERENCES providers(id),
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  start_time TEXT,                                      -- empty: from the start of the day
  end_time TEXT,                                        -- empty: to the end of the day
  reason TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  removed_at TEXT,
  removed_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_time_off ON provider_time_off(clinic_id, provider_id, start_date);

CREATE TABLE IF NOT EXISTS blockouts (                 -- time held in the diary: lunch, meetings, time kept for emergencies
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  provider_id INTEGER REFERENCES providers(id),         -- empty: every dentist
  chair_id INTEGER REFERENCES chairs(id),               -- empty: no particular chair
  kind TEXT NOT NULL,                                   -- see services/scheduling.py BLOCK_KINDS
  label TEXT,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,                               -- the same as start_date for a one-off
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  repeat TEXT NOT NULL DEFAULT 'once' CHECK (repeat IN ('once','daily','weekly')),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  removed_at TEXT,
  removed_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_blockouts ON blockouts(clinic_id, start_date, end_date);

CREATE TABLE IF NOT EXISTS appointment_types (         -- for example New patient exam, 60 minutes, blue, with these treatments
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  duration_min INTEGER NOT NULL,
  colour TEXT NOT NULL DEFAULT 'blue',
  category TEXT NOT NULL DEFAULT 'Consultation',
  block_kind TEXT,                                      -- held-back time of this kind may be used by this type, for example emergency
  procedure_ids TEXT NOT NULL DEFAULT '',               -- comma-separated procedure_codes ids, planned when booked
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE (clinic_id, name)
);

-- Version 0.17: clinical depth. Prescriptions, referrals, vital signs, letters, a contact log and X-ray layouts.
CREATE TABLE IF NOT EXISTS drugs (                       -- the clinic's own list of medicines it prescribes
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,                                    -- generic name, for example Amoxicillin
  strength TEXT NOT NULL DEFAULT '',                     -- for example 500 mg
  form TEXT NOT NULL DEFAULT '',                         -- capsules, tablets, mouthwash
  dose TEXT NOT NULL DEFAULT '',                         -- the label wording, for example 1 capsule three times daily
  days INTEGER,                                          -- usual length of the course
  quantity TEXT NOT NULL DEFAULT '',                     -- how much to supply, for example 15 capsules
  drug_class TEXT NOT NULL DEFAULT 'other',              -- used for allergy checks, see services/prescriptions.py
  controlled INTEGER NOT NULL DEFAULT 0,                 -- 1: a controlled drug with extra legal rules
  warnings TEXT NOT NULL DEFAULT '',
  source TEXT,                                           -- where a starter regimen came from
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS prescriptions (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  provider_id INTEGER NOT NULL REFERENCES providers(id),
  appointment_id INTEGER REFERENCES appointments(id),
  issued_on TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'issued' CHECK (status IN ('issued','cancelled')),
  notes TEXT,
  override_reason TEXT,                                  -- why it was written despite a warning
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  cancelled_by INTEGER REFERENCES users(id),
  cancelled_at TEXT,
  cancel_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_rx_patient ON prescriptions(clinic_id, patient_id);

CREATE TABLE IF NOT EXISTS prescription_items (           -- copied from the drug list when written, so later edits never change a prescription
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  prescription_id INTEGER NOT NULL REFERENCES prescriptions(id),
  drug_id INTEGER REFERENCES drugs(id),
  name TEXT NOT NULL,
  strength TEXT NOT NULL DEFAULT '',
  form TEXT NOT NULL DEFAULT '',
  dose TEXT NOT NULL,
  days INTEGER,
  quantity TEXT NOT NULL DEFAULT '',
  controlled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS referrers (                    -- people and places that send patients to us or that we send patients to
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'dentist',                   -- see services/referrals.py KINDS
  specialty TEXT,
  practice TEXT,
  phone TEXT,
  email TEXT,
  address TEXT,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS referrals_out (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  referrer_id INTEGER NOT NULL REFERENCES referrers(id),
  provider_id INTEGER REFERENCES providers(id),           -- our dentist who is referring
  urgency TEXT NOT NULL DEFAULT 'routine' CHECK (urgency IN ('routine','soon','urgent')),
  reason TEXT NOT NULL,
  teeth TEXT,
  history TEXT,                                           -- relevant history and findings for the letter
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent','booked','seen','report','closed','cancelled')),
  sent_on TEXT,
  report_note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_refout_clinic ON referrals_out(clinic_id, status);

CREATE TABLE IF NOT EXISTS vitals (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  appointment_id INTEGER REFERENCES appointments(id),
  taken_at TEXT NOT NULL,
  bp_systolic INTEGER,
  bp_diastolic INTEGER,
  pulse INTEGER,
  temperature_c REAL,
  spo2 INTEGER,
  glucose_mmol REAL,
  note TEXT,
  recorded_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vitals_patient ON vitals(clinic_id, patient_id, taken_at);

CREATE TABLE IF NOT EXISTS letter_templates (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  body TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE (clinic_id, name)
);

CREATE TABLE IF NOT EXISTS letters (                      -- every letter printed for a patient, with the words as printed
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  template_id INTEGER REFERENCES letter_templates(id),
  title TEXT NOT NULL,
  addressee TEXT,
  body TEXT NOT NULL,
  provider_id INTEGER REFERENCES providers(id),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_letters_patient ON letters(clinic_id, patient_id);

CREATE TABLE IF NOT EXISTS contact_log (                  -- calls, visits to the desk, letters and emails that happen outside the system
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  kind TEXT NOT NULL,                                     -- see services/contactlog.py KINDS
  summary TEXT NOT NULL,
  outcome TEXT,
  follow_up_on TEXT,
  follow_up_done INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_contact_patient ON contact_log(clinic_id, patient_id);

CREATE TABLE IF NOT EXISTS image_mounts (                 -- X-rays arranged in a standard layout, for example a full mouth series
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  layout TEXT NOT NULL,
  title TEXT,
  taken_on TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS image_mount_slots (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  mount_id INTEGER NOT NULL REFERENCES image_mounts(id),
  slot INTEGER NOT NULL,
  file_id INTEGER NOT NULL REFERENCES patient_files(id),
  UNIQUE (mount_id, slot)
);

-- Version 0.18: insurance depth. Cover by category, waiting periods, how-often limits, a second insurer, and one payment from a
-- payer split across many claims.
CREATE TABLE IF NOT EXISTS cover_benefits (              -- what share the payer pays for one category of treatment. No row: the cover's own share
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  cover_id INTEGER NOT NULL REFERENCES patient_cover(id),
  category TEXT NOT NULL,                                -- a procedure category, for example Preventive or Prosthetics
  pct INTEGER NOT NULL CHECK (pct BETWEEN 0 AND 100),
  waiting_months INTEGER NOT NULL DEFAULT 0 CHECK (waiting_months BETWEEN 0 AND 60),
  UNIQUE (cover_id, category)
);

CREATE TABLE IF NOT EXISTS cover_limits (                -- how often the payer pays for one treatment, for example 2 scale and polish in 12 months
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  cover_id INTEGER NOT NULL REFERENCES patient_cover(id),
  code_id INTEGER NOT NULL REFERENCES procedure_codes(id),
  times INTEGER NOT NULL CHECK (times BETWEEN 1 AND 50),
  months INTEGER NOT NULL CHECK (months BETWEEN 1 AND 120),
  UNIQUE (cover_id, code_id)
);

CREATE TABLE IF NOT EXISTS payer_payments (              -- one payment from a payer, for example a bank transfer, that pays several claims
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  payer_id INTEGER NOT NULL REFERENCES payers(id),
  received_on TEXT NOT NULL,
  amount_p INTEGER NOT NULL CHECK (amount_p >= 0),
  reference TEXT NOT NULL,
  note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Version 0.19: running the practice. Staff tasks, the time clock, end-of-day cash-up.
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  title TEXT NOT NULL,
  detail TEXT,
  patient_id INTEGER REFERENCES patients(id),
  assigned_to INTEGER REFERENCES users(id),
  due_on TEXT,
  priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('normal','high','urgent')),
  repeat TEXT NOT NULL DEFAULT 'none' CHECK (repeat IN ('none','daily','weekly','monthly')),
  repeat_of INTEGER REFERENCES tasks(id),               -- the first task in a repeating series
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  closed_by INTEGER REFERENCES users(id),
  closed_at TEXT,
  close_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(clinic_id, status, assigned_to);

CREATE TABLE IF NOT EXISTS time_entries (                -- one shift: clocked in, clocked out, minutes of break
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  clock_in TEXT NOT NULL,                                -- clinic time, YYYY-MM-DD HH:MM
  clock_out TEXT,
  break_minutes INTEGER NOT NULL DEFAULT 0,
  edited_by INTEGER REFERENCES users(id),               -- a manager who corrected it, with the reason
  edited_at TEXT,
  edit_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_time_user ON time_entries(clinic_id, user_id, clock_in);

CREATE TABLE IF NOT EXISTS day_closes (                  -- an end-of-day cash-up. A reopened day keeps its earlier close
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  day TEXT NOT NULL,
  lines TEXT NOT NULL,                                   -- JSON: {method: {expected, counted, difference}} in minor units
  note TEXT,
  status TEXT NOT NULL DEFAULT 'closed' CHECK (status IN ('closed','reopened')),
  closed_by INTEGER REFERENCES users(id),
  closed_at TEXT NOT NULL,
  reopened_by INTEGER REFERENCES users(id),
  reopened_at TEXT,
  reopen_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_day_closes ON day_closes(clinic_id, day);

-- Version 0.20: online payments through the clinic's own account with a payment company (Paystack or Stripe).
CREATE TABLE IF NOT EXISTS pay_accounts (
  clinic_id INTEGER PRIMARY KEY REFERENCES clinics(id),
  provider TEXT NOT NULL DEFAULT 'paystack',
  secret_key TEXT,                                       -- encrypted. Never shown again
  key_hint TEXT,                                         -- the first and last few characters, so staff can tell which key is saved
  webhook_secret TEXT,                                   -- encrypted. Stripe's signing secret
  enabled INTEGER NOT NULL DEFAULT 0,
  hook_key TEXT NOT NULL UNIQUE,                         -- the secret part of the address the company calls
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS pay_links (                   -- a link a patient opens to pay
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  ref TEXT NOT NULL UNIQUE,                              -- our reference, sent to the company. Also the secret part of the link
  provider TEXT NOT NULL,
  amount_p INTEGER NOT NULL CHECK (amount_p > 0),
  currency TEXT NOT NULL,
  description TEXT,
  purpose TEXT NOT NULL DEFAULT 'treatment',
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','paid','cancelled','expired','failed')),
  checkout_url TEXT,
  provider_ref TEXT,
  channel TEXT,                                          -- card or mobile_money, as the company reports it
  error TEXT,
  payment_id INTEGER REFERENCES payments(id),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT NOT NULL,
  paid_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pay_links ON pay_links(clinic_id, patient_id, status);

-- Version 0.21: phone calls through the clinic's own Twilio account.
CREATE TABLE IF NOT EXISTS voice_accounts (
  clinic_id INTEGER PRIMARY KEY REFERENCES clinics(id),
  account_sid TEXT,
  auth_token TEXT,                                       -- encrypted. Never shown again
  twilio_number TEXT,
  desk_number TEXT,
  missed_task_to INTEGER REFERENCES users(id),
  enabled INTEGER NOT NULL DEFAULT 0,
  hook_key TEXT NOT NULL UNIQUE,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  call_sid TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('in','out')),
  from_number TEXT,
  to_number TEXT,
  patient_id INTEGER REFERENCES patients(id),
  user_id INTEGER REFERENCES users(id),
  status TEXT NOT NULL,
  ref TEXT,
  duration_s INTEGER,
  task_id INTEGER REFERENCES tasks(id),
  started_at TEXT NOT NULL,
  ended_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_calls_sid ON calls(call_sid);
CREATE INDEX IF NOT EXISTS idx_calls ON calls(clinic_id, id);
CREATE INDEX IF NOT EXISTS idx_calls_patient ON calls(clinic_id, patient_id);

-- Version 0.22: sterilisation and instrument tracking.
CREATE TABLE IF NOT EXISTS steri_settings (
  clinic_id INTEGER PRIMARY KEY REFERENCES clinics(id),
  require_daily INTEGER NOT NULL DEFAULT 1,              -- no cycle until today's tests have passed
  shelf_days INTEGER NOT NULL DEFAULT 90                 -- how long a wrapped pack may be stored. The clinic sets it from its own guidance
);

CREATE TABLE IF NOT EXISTS sterilisers (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  serial TEXT,
  validated_on TEXT,                                     -- last yearly validation
  status TEXT NOT NULL DEFAULT 'in' CHECK (status IN ('in','out')),
  out_since TEXT,
  out_test TEXT,                                         -- the kind of test that failed
  out_reason TEXT,
  spore_fails INTEGER NOT NULL DEFAULT 0,                -- failed spore tests in a row
  passes_since INTEGER NOT NULL DEFAULT 0,               -- passes of the failed test since it went out of service
  retired INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS steri_cycles (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  steriliser_id INTEGER NOT NULL REFERENCES sterilisers(id),
  cycle_no INTEGER NOT NULL,
  machine_cycle_no TEXT,                                 -- the number the machine prints, if it has one
  program TEXT,
  temp_tenths INTEGER,                                   -- 1345 = 134.5 °C
  pressure_kpa INTEGER,
  hold_seconds INTEGER,
  mechanical_ok INTEGER NOT NULL DEFAULT 0,              -- the machine reached its time, temperature and pressure
  indicator_ok INTEGER NOT NULL DEFAULT 0,               -- the chemical indicator changed as it should
  implants INTEGER NOT NULL DEFAULT 0,
  result TEXT NOT NULL CHECK (result IN ('pass','fail','held')),
  notes TEXT,
  started_at TEXT NOT NULL,
  done_by INTEGER REFERENCES users(id),
  UNIQUE (steriliser_id, cycle_no)
);
CREATE INDEX IF NOT EXISTS idx_steri_cycles ON steri_cycles(clinic_id, steriliser_id, started_at);

CREATE TABLE IF NOT EXISTS steri_tests (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  steriliser_id INTEGER NOT NULL REFERENCES sterilisers(id),
  kind TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('pass','fail','pending')),
  lot TEXT,
  notes TEXT,
  cycle_id INTEGER REFERENCES steri_cycles(id),
  done_at TEXT NOT NULL,
  done_by INTEGER REFERENCES users(id),
  read_at TEXT,
  read_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_steri_tests ON steri_tests(steriliser_id, kind, done_at);

CREATE TABLE IF NOT EXISTS steri_recalls (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  steriliser_id INTEGER NOT NULL REFERENCES sterilisers(id),
  test_id INTEGER NOT NULL REFERENCES steri_tests(id),
  since TEXT,                                            -- the last passing spore test before the failure. Empty: all packs
  opened_at TEXT NOT NULL,
  opened_by INTEGER REFERENCES users(id),
  closed_at TEXT,
  closed_by INTEGER REFERENCES users(id),
  note TEXT
);

CREATE TABLE IF NOT EXISTS steri_packs (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  code TEXT NOT NULL,
  cycle_id INTEGER NOT NULL REFERENCES steri_cycles(id),
  contents TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('stored','held','used','recalled','failed','reprocessed')),
  packed_by INTEGER REFERENCES users(id),
  packed_at TEXT NOT NULL,
  expires_on TEXT NOT NULL,
  used_on INTEGER REFERENCES patients(id),
  used_appointment INTEGER REFERENCES appointments(id),
  used_at TEXT,
  used_by INTEGER REFERENCES users(id),
  recall_id INTEGER REFERENCES steri_recalls(id),
  back_reason TEXT,
  back_at TEXT,
  UNIQUE (clinic_id, code)
);
CREATE INDEX IF NOT EXISTS idx_steri_packs ON steri_packs(clinic_id, status, expires_on);
CREATE INDEX IF NOT EXISTS idx_steri_packs_patient ON steri_packs(clinic_id, used_on);

CREATE TABLE IF NOT EXISTS steri_service_log (           -- each time a steriliser goes back into service, and why
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  steriliser_id INTEGER NOT NULL REFERENCES sterilisers(id),
  note TEXT NOT NULL,
  by_user INTEGER REFERENCES users(id),
  at TEXT NOT NULL
);

-- Version 0.23: US electronic claims (X12 5010).
CREATE TABLE IF NOT EXISTS edi_settings (
  clinic_id INTEGER PRIMARY KEY REFERENCES clinics(id),
  submitter_id TEXT,                                     -- the ID the clearinghouse gave the clinic
  submitter_name TEXT,
  contact_name TEXT,
  contact_phone TEXT,
  receiver_id TEXT,                                      -- the clearinghouse's own ID
  receiver_name TEXT,
  billing_npi TEXT,
  tax_id TEXT,                                           -- EIN, 9 digits
  taxonomy TEXT,
  billing_address TEXT,
  billing_city TEXT,
  billing_state TEXT,
  billing_zip TEXT,                                      -- ZIP+4, 9 digits
  usage TEXT NOT NULL DEFAULT 'T' CHECK (usage IN ('T','P')),   -- T: test files, P: live
  next_control INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS edi_files (                   -- every claim file made and every remittance, answer or receipt read
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  kind TEXT NOT NULL,                                    -- 837D, 270, 835, 271, 999
  direction TEXT NOT NULL CHECK (direction IN ('out','in')),
  control_no INTEGER,
  body TEXT NOT NULL,
  claim_ids TEXT NOT NULL DEFAULT '[]',
  cover_id INTEGER REFERENCES patient_cover(id),
  status TEXT NOT NULL DEFAULT 'made',                   -- made, accepted, rejected, posted
  summary TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edi_files ON edi_files(clinic_id, id);

-- Version 0.24: NHS dentistry in England.
CREATE TABLE IF NOT EXISTS nhs_settings (
  clinic_id INTEGER PRIMARY KEY REFERENCES clinics(id),
  enabled INTEGER NOT NULL DEFAULT 0,
  contract_no TEXT,
  charges TEXT,                                          -- JSON: band charges in pence, the date they apply from, and the source
  udas TEXT                                              -- JSON: UDAs per kind of course, when the clinic changes them
);

CREATE TABLE IF NOT EXISTS nhs_years (                   -- the contract for one NHS year (1 April to 31 March)
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  year INTEGER NOT NULL,
  udas INTEGER NOT NULL,
  uda_value_p INTEGER NOT NULL,
  PRIMARY KEY (clinic_id, year)
);

CREATE TABLE IF NOT EXISTS nhs_courses (                 -- one NHS course of treatment, and its FP17
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  provider_id INTEGER NOT NULL REFERENCES providers(id),
  opened_on TEXT NOT NULL,
  closed_on TEXT,
  course_type TEXT,
  exemption TEXT NOT NULL DEFAULT 'none',
  evidence TEXT NOT NULL DEFAULT 'na',
  hc3_amount_p INTEGER,
  charge_p INTEGER,
  charge_proc_id INTEGER REFERENCES procedures(id),      -- the account line for the patient's charge
  uda REAL,
  declaration_on TEXT,                                   -- the patient signed the NHS declaration
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','submitted','accepted','rejected','withdrawn')),
  fp17_due TEXT,
  fp17_ref TEXT,
  fp17_sent_on TEXT,
  outcome_note TEXT,
  notes TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_nhs_courses ON nhs_courses(clinic_id, status, fp17_due);
CREATE INDEX IF NOT EXISTS idx_nhs_courses_patient ON nhs_courses(clinic_id, patient_id);

-- Version 0.25: the imaging bridge.
CREATE TABLE IF NOT EXISTS imaging_stations (            -- a computer with an X-ray sensor and the imaging agent on it
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  last_seen TEXT,                                        -- UTC. When the agent last asked for work
  PRIMARY KEY (clinic_id, name)
);

CREATE TABLE IF NOT EXISTS imaging_requests (            -- "the next X-rays on this computer are for this patient"
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  station TEXT NOT NULL,
  requested_by INTEGER REFERENCES users(id),
  requested_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  done_at TEXT,
  files INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_imaging_requests ON imaging_requests(clinic_id, station, done_at);

-- Version 0.26: orthodontics.
CREATE TABLE IF NOT EXISTS ortho_cases (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  provider_id INTEGER NOT NULL REFERENCES providers(id),
  appliance TEXT NOT NULL,
  arches TEXT NOT NULL DEFAULT 'both',
  molar_class TEXT,
  overjet_tenths INTEGER,                                -- 45 = 4.5 mm
  overbite_tenths INTEGER,
  crowding TEXT,
  est_months INTEGER NOT NULL,
  notes TEXT,
  status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','active','retention','finished','stopped')),
  started_on TEXT,
  debonded_on TEXT,
  retention TEXT,
  closed_on TEXT,
  close_reason TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ortho_cases ON ortho_cases(clinic_id, patient_id, status);

CREATE TABLE IF NOT EXISTS ortho_visits (                -- one line of the ortho chart
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  case_id INTEGER NOT NULL REFERENCES ortho_cases(id),
  visit_on TEXT NOT NULL,
  appointment_id INTEGER REFERENCES appointments(id),
  upper_wire TEXT,
  lower_wire TEXT,
  elastics TEXT,
  aligner_no TEXT,
  repairs TEXT,
  notes TEXT,
  next_weeks INTEGER,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ortho_visits ON ortho_visits(case_id, visit_on);

CREATE TABLE IF NOT EXISTS ortho_contracts (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  case_id INTEGER NOT NULL REFERENCES ortho_cases(id),
  total_p INTEGER NOT NULL,
  deposit_p INTEGER NOT NULL DEFAULT 0,
  months INTEGER NOT NULL,
  monthly_p INTEGER NOT NULL,
  first_due TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','finished','cancelled','replaced')),
  stop_reason TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ortho_charges (               -- the contract's schedule. A posted amount points at its line on the account
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  contract_id INTEGER NOT NULL REFERENCES ortho_contracts(id),
  kind TEXT NOT NULL CHECK (kind IN ('deposit','monthly')),
  due_on TEXT NOT NULL,
  amount_p INTEGER NOT NULL,
  procedure_id INTEGER REFERENCES procedures(id),
  posted_on TEXT,
  cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ortho_charges ON ortho_charges(clinic_id, due_on, procedure_id);

-- Version 0.27: statement runs and late fees.
CREATE TABLE IF NOT EXISTS statement_settings (
  clinic_id INTEGER PRIMARY KEY REFERENCES clinics(id),
  late_on INTEGER NOT NULL DEFAULT 0,
  late_kind TEXT NOT NULL DEFAULT 'fixed' CHECK (late_kind IN ('fixed','percent')),
  late_value INTEGER NOT NULL DEFAULT 0,                 -- minor units, or basis points for a percentage (150 = 1.5%)
  late_after_days INTEGER NOT NULL DEFAULT 60,
  late_min_p INTEGER NOT NULL DEFAULT 0,
  late_gap_days INTEGER NOT NULL DEFAULT 30,
  late_cap_p INTEGER
);

CREATE TABLE IF NOT EXISTS statement_runs (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  made_on TEXT NOT NULL,
  min_balance_p INTEGER NOT NULL,
  older_than_days INTEGER NOT NULL DEFAULT 0,
  fees_added INTEGER NOT NULL DEFAULT 0,
  made_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS statement_items (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  run_id INTEGER NOT NULL REFERENCES statement_runs(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  balance_p INTEGER NOT NULL,
  fee_p INTEGER NOT NULL DEFAULT 0,
  fee_proc_id INTEGER REFERENCES procedures(id),
  sent_at TEXT,
  sent_by INTEGER REFERENCES users(id),
  sent_how TEXT
);
CREATE INDEX IF NOT EXISTS idx_statement_items ON statement_items(clinic_id, patient_id);

-- Version 0.28: saved reports from the report builder.
CREATE TABLE IF NOT EXISTS saved_reports (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  spec TEXT NOT NULL,                                    -- JSON: data set, columns, filters, grouping. Never SQL
  shared INTEGER NOT NULL DEFAULT 1,
  archived INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT
);

-- Version 0.29: messages between staff, and the equipment register.
CREATE TABLE IF NOT EXISTS staff_messages (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  from_user INTEGER NOT NULL REFERENCES users(id),
  to_user INTEGER REFERENCES users(id),                  -- empty: to everyone
  patient_id INTEGER REFERENCES patients(id),
  body TEXT NOT NULL,
  reply_to INTEGER REFERENCES staff_messages(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_staff_messages ON staff_messages(clinic_id, to_user, id);

CREATE TABLE IF NOT EXISTS staff_message_reads (
  message_id INTEGER NOT NULL REFERENCES staff_messages(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  read_at TEXT NOT NULL,
  PRIMARY KEY (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS equipment (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  maker TEXT,
  model TEXT,
  serial TEXT,
  location TEXT,
  bought_on TEXT,
  warranty_until TEXT,
  service_months INTEGER,
  last_service_on TEXT,
  supplier TEXT,
  notes TEXT,
  fault_open INTEGER NOT NULL DEFAULT 0,
  retired INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS equipment_events (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  equipment_id INTEGER NOT NULL REFERENCES equipment(id),
  kind TEXT NOT NULL,
  done_on TEXT NOT NULL,
  done_by TEXT,
  cost_p INTEGER,
  notes TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Version 0.30: permission groups that narrow what a role allows.
CREATE TABLE IF NOT EXISTS perm_groups (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  areas TEXT NOT NULL,                                   -- JSON list of the areas people in the group may use
  UNIQUE (clinic_id, name)
);

-- Version 0.31: email through the clinic's own mail account.
CREATE TABLE IF NOT EXISTS mail_accounts (
  clinic_id INTEGER PRIMARY KEY REFERENCES clinics(id),
  host TEXT,
  port INTEGER NOT NULL DEFAULT 587,
  security TEXT NOT NULL DEFAULT 'starttls' CHECK (security IN ('starttls','ssl','none')),
  username TEXT,
  password TEXT,                                         -- encrypted. Never shown again
  from_addr TEXT,
  from_name TEXT,
  reply_to TEXT,
  enabled INTEGER NOT NULL DEFAULT 0,
  auto_send INTEGER NOT NULL DEFAULT 0                   -- 1: the scheduled job emails the send list for patients who prefer email
);

-- Version 0.32: saved cards (a token from the payment company, never the card) and every charge tried on them.
CREATE TABLE IF NOT EXISTS saved_cards (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  provider TEXT NOT NULL,
  token TEXT NOT NULL,                                   -- encrypted: Paystack authorization code, or Stripe customer|payment method
  brand TEXT,
  last4 TEXT,
  exp_month INTEGER,
  exp_year INTEGER,
  email TEXT,                                            -- Paystack charges need the email the card was saved with
  auto_ok INTEGER NOT NULL DEFAULT 0,
  consent_text TEXT NOT NULL,
  consent_at TEXT,
  link_id INTEGER REFERENCES pay_links(id),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','removed')),
  removed_at TEXT,
  removed_reason TEXT,
  last_used_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_saved_cards ON saved_cards(clinic_id, patient_id, status);

CREATE TABLE IF NOT EXISTS card_charges (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  card_id INTEGER NOT NULL REFERENCES saved_cards(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  ref TEXT NOT NULL UNIQUE,
  amount_p INTEGER NOT NULL,
  currency TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT 'treatment',
  what TEXT,
  status TEXT NOT NULL CHECK (status IN ('trying','paid','failed')),
  error TEXT,
  provider_ref TEXT,
  payment_id INTEGER REFERENCES payments(id),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Version 0.33: SMS through the clinic's own SMS account.
CREATE TABLE IF NOT EXISTS sms_accounts (
  clinic_id INTEGER PRIMARY KEY REFERENCES clinics(id),
  provider TEXT NOT NULL DEFAULT 'hubtel',
  sender TEXT,                                           -- the sender name (up to 11 characters), number or short code
  api_user TEXT,                                         -- Hubtel client ID, Africa's Talking user name, or Twilio Account SID
  api_secret TEXT,                                       -- encrypted. Never shown again
  sandbox INTEGER NOT NULL DEFAULT 0,                    -- Africa's Talking test system
  enabled INTEGER NOT NULL DEFAULT 0,
  auto_send INTEGER NOT NULL DEFAULT 0
);

-- Version 0.34: the form builder.
CREATE TABLE IF NOT EXISTS custom_forms (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  title TEXT NOT NULL,
  intro TEXT,
  fields TEXT NOT NULL,                                  -- JSON: the questions
  version INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS form_responses (              -- one form sent to one patient, and their answers
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  form_id INTEGER NOT NULL REFERENCES custom_forms(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  version INTEGER NOT NULL,
  fields TEXT NOT NULL,                                  -- the questions exactly as the patient saw them
  answers TEXT,
  token_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent','submitted','filed','expired','cancelled')),
  expires_at TEXT NOT NULL,                              -- UTC
  sent_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  submitted_at TEXT,
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT,
  review_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_form_responses ON form_responses(clinic_id, patient_id, status);

-- 0.35: the implant and materials register, and makers' recalls
CREATE TABLE IF NOT EXISTS implant_items (               -- one traceable item put in a patient: an implant, abutment, graft, membrane ...
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  tooth INTEGER,                                         -- FDI number; empty for an item not tied to one tooth
  kind TEXT NOT NULL CHECK (kind IN ('implant','abutment','crown','bone_graft','membrane','screw','other')),
  maker TEXT NOT NULL,
  product TEXT,
  ref_code TEXT,                                         -- the maker's catalogue or reference code
  lot TEXT NOT NULL,                                     -- the lot or batch number on the label
  size TEXT,
  expires_on TEXT,
  placed_on TEXT NOT NULL,
  provider_id INTEGER REFERENCES providers(id),
  notes TEXT,
  removed_on TEXT,
  removed_reason TEXT,
  removed_note TEXT,
  removed_by INTEGER REFERENCES users(id),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_implant_items_patient ON implant_items(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_implant_items_lot ON implant_items(clinic_id, lot);

CREATE TABLE IF NOT EXISTS material_recalls (            -- a maker's or regulator's recall the clinic is working through
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  maker TEXT,
  product TEXT,
  lot TEXT,
  reason TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  closed_at TEXT,
  closed_by INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS material_recall_patients (    -- each patient found when the recall was opened, and whether they were told
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  recall_id INTEGER NOT NULL REFERENCES material_recalls(id),
  item_id INTEGER NOT NULL REFERENCES implant_items(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  contacted_at TEXT,
  contacted_by INTEGER REFERENCES users(id),
  note TEXT
);

-- 0.36: paying dentists and hygienists their share
CREATE TABLE IF NOT EXISTS provider_pay_terms (          -- how one provider is paid, from a date; a change adds a row
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  provider_id INTEGER NOT NULL REFERENCES providers(id),
  basis TEXT NOT NULL CHECK (basis IN ('gross_production','net_production','collections')),
  percent_bp INTEGER NOT NULL DEFAULT 0,                 -- 4500 is 45.00 per cent
  lab_bp INTEGER NOT NULL DEFAULT 0,                     -- the share of lab bills the provider bears
  daily_p INTEGER NOT NULL DEFAULT 0,                    -- a fixed amount per day worked, in minor units
  effective_from TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pay_runs (                    -- one provider's pay for one period, with the figures as worked out
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  provider_id INTEGER NOT NULL REFERENCES providers(id),
  period_from TEXT NOT NULL,
  period_to TEXT NOT NULL,
  basis TEXT NOT NULL,
  percent_bp INTEGER NOT NULL,
  lab_bp INTEGER NOT NULL,
  base_p INTEGER NOT NULL,
  share_p INTEGER NOT NULL,
  lab_fees_p INTEGER NOT NULL,
  lab_deduction_p INTEGER NOT NULL,
  daily_p INTEGER NOT NULL,
  extras TEXT NOT NULL DEFAULT '[]',                     -- JSON: lines added by hand
  total_p INTEGER NOT NULL,
  figures TEXT NOT NULL,                                 -- JSON: every figure the pay was worked out from
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','paid','cancelled')),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  approved_by INTEGER REFERENCES users(id),
  approved_at TEXT,
  paid_on TEXT,
  paid_ref TEXT,
  paid_by INTEGER REFERENCES users(id),
  cancel_reason TEXT
);

-- 0.37: complaints and patient safety incidents
CREATE TABLE IF NOT EXISTS complaints (                  -- kept apart from the clinical record
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER REFERENCES patients(id),            -- empty when the person is not a patient
  complainant TEXT,                                      -- who complained, if not the patient (a parent, for example)
  contact TEXT,
  channel TEXT NOT NULL,
  category TEXT NOT NULL,
  about_staff TEXT,
  summary TEXT NOT NULL,
  wanted TEXT,                                           -- what the person would like to happen
  received_on TEXT NOT NULL,
  ack_due TEXT NOT NULL,                                 -- three working days after it came in
  acknowledged_on TEXT,
  response_due TEXT,                                     -- agreed with the person
  responded_on TEXT,
  outcome TEXT,
  learning TEXT,
  escalated_to TEXT,                                     -- an ombudsman or council, if the person took it further
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','responded','closed')),
  owner_id INTEGER REFERENCES users(id),
  closed_on TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS complaint_steps (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  complaint_id INTEGER NOT NULL REFERENCES complaints(id),
  what TEXT NOT NULL,
  note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS safety_incidents (            -- anything that went wrong or nearly did
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER REFERENCES patients(id),
  kind TEXT NOT NULL,
  harm TEXT NOT NULL,
  occurred_on TEXT NOT NULL,
  place TEXT,
  people TEXT,                                           -- staff or others involved, as written
  what_happened TEXT NOT NULL,
  immediate_action TEXT,
  cause TEXT,
  actions TEXT,
  reported_to TEXT,
  report_ref TEXT,
  candour_told_on TEXT,
  candour_told_by TEXT,
  candour_apology INTEGER NOT NULL DEFAULT 0,
  candour_written_on TEXT,
  candour_note TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','reviewed','closed')),
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT,
  closed_at TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 0.38: staff registration, indemnity and CPD
CREATE TABLE IF NOT EXISTS staff_registrations (         -- each save adds a row; the newest is the one in force
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  body TEXT,
  reg_number TEXT,
  title TEXT NOT NULL,
  reg_expires_on TEXT,
  indemnity_by TEXT,
  indemnity_ref TEXT,
  indemnity_expires_on TEXT,
  scheme TEXT NOT NULL CHECK (scheme IN ('gdc','yearly','none')),
  cycle_start TEXT,
  yearly_target_tenths INTEGER,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cpd_records (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  done_on TEXT NOT NULL,
  title TEXT NOT NULL,
  provider TEXT,
  hours_tenths INTEGER NOT NULL CHECK (hours_tenths > 0),
  verifiable INTEGER NOT NULL DEFAULT 0,
  outcomes TEXT,                                         -- GDC development outcomes, for example 'AC'
  certificate_ref TEXT,
  notes TEXT,
  struck INTEGER NOT NULL DEFAULT 0,
  struck_reason TEXT,
  struck_by INTEGER REFERENCES users(id),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 0.39: fee schedules (price lists other than the standard one)
CREATE TABLE IF NOT EXISTS fee_schedules (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  discount_bp INTEGER NOT NULL DEFAULT 0,                -- off the standard price where the schedule names no price; 1000 is 10 per cent
  notes TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fee_schedule_items (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  schedule_id INTEGER NOT NULL REFERENCES fee_schedules(id),
  code_id INTEGER NOT NULL REFERENCES procedure_codes(id),
  fee_p INTEGER NOT NULL CHECK (fee_p >= 0),
  UNIQUE (schedule_id, code_id)
);

-- 0.40: merging duplicate patient records
CREATE TABLE IF NOT EXISTS patient_merges (              -- one duplicate merged into the record kept, and exactly what moved
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  keep_id INTEGER NOT NULL REFERENCES patients(id),
  dup_id INTEGER NOT NULL REFERENCES patients(id),
  moved TEXT NOT NULL,                                   -- JSON: table and number of rows
  left_behind TEXT NOT NULL,                             -- JSON: what stayed on the old record, for a person to look at
  filled TEXT NOT NULL,                                  -- JSON: details copied to the kept record because it had none
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS patient_not_duplicates (      -- two records that look alike but are different people
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  a_id INTEGER NOT NULL REFERENCES patients(id),
  b_id INTEGER NOT NULL REFERENCES patients(id),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 0.41: patients not seen for a while
CREATE TABLE IF NOT EXISTS reactivation_holds (          -- a patient left off the list until a date, and why
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  reason TEXT NOT NULL,
  note TEXT,
  until_on TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 0.42: refunds of credit a patient holds
CREATE TABLE IF NOT EXISTS refunds (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  refund_no INTEGER NOT NULL,
  amount_p INTEGER NOT NULL CHECK (amount_p > 0),
  method TEXT NOT NULL CHECK (method IN ('cash','mobile_money','card','bank_transfer')),
  reason TEXT NOT NULL,
  reference TEXT,
  note TEXT,
  refunded_on TEXT NOT NULL,
  voided INTEGER NOT NULL DEFAULT 0,
  void_reason TEXT,
  voided_by INTEGER REFERENCES users(id),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (clinic_id, refund_no)
);

-- 0.43: the date the books are closed up to; each change adds a row
CREATE TABLE IF NOT EXISTS period_locks (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  locked_to TEXT,                                        -- empty: nothing is closed
  reason TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 0.44: aftercare sheets, approved by a dentist, printed for patients
CREATE TABLE IF NOT EXISTS aftercare_sheets (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  title_fr TEXT,
  body_fr TEXT,
  codes TEXT NOT NULL DEFAULT '[]',                      -- JSON: the treatments on the clinic's list this sheet goes with
  source TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  approved_by INTEGER REFERENCES users(id),              -- cleared whenever the wording changes
  approved_at TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS aftercare_given (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  sheet_id INTEGER NOT NULL REFERENCES aftercare_sheets(id),
  given_by INTEGER REFERENCES users(id),
  given_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 0.45: treatment kits, the stock each treatment uses
CREATE TABLE IF NOT EXISTS kit_lines (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  code_id INTEGER NOT NULL REFERENCES procedure_codes(id),
  item_id INTEGER NOT NULL REFERENCES stock_items(id),
  qty INTEGER NOT NULL CHECK (qty > 0),
  UNIQUE (code_id, item_id)
);

CREATE TABLE IF NOT EXISTS kit_uses (                    -- what one finished treatment took off the shelf
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  procedure_id INTEGER NOT NULL REFERENCES procedures(id),
  item_id INTEGER NOT NULL REFERENCES stock_items(id),
  wanted INTEGER NOT NULL,
  taken INTEGER NOT NULL,
  cost_p INTEGER NOT NULL DEFAULT 0,
  move_id INTEGER REFERENCES stock_moves(id),
  returned INTEGER NOT NULL DEFAULT 0,                   -- 1: the treatment was voided and the stock put back
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 0.46: auto codes, a treatment picked by type with the code chosen from the tooth and surfaces
CREATE TABLE IF NOT EXISTS autocode_groups (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS autocode_rules (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  group_id INTEGER NOT NULL REFERENCES autocode_groups(id),
  surfaces INTEGER NOT NULL DEFAULT 0,                   -- 0: any number of surfaces
  region TEXT NOT NULL DEFAULT 'any' CHECK (region IN ('any','front','back')),
  code_id INTEGER NOT NULL REFERENCES procedure_codes(id),
  UNIQUE (group_id, surfaces, region)
);

-- 0.47: days the clinic is closed (public holidays and its own closures)
CREATE TABLE IF NOT EXISTS closed_days (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  day TEXT NOT NULL,
  name TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  removed_at TEXT,
  removed_by INTEGER REFERENCES users(id)
);

-- 0.48: moving records in from another system
CREATE TABLE IF NOT EXISTS migration_runs (              -- one upload; the file itself is deleted once the import is done or cancelled
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  file_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'preview' CHECK (status IN ('preview','done','cancelled')),
  overrides TEXT NOT NULL DEFAULT '{}',                  -- JSON: what kind each file is, and columns matched by hand
  notation TEXT NOT NULL DEFAULT 'universal',            -- how the old system numbers teeth
  summary TEXT,                                          -- JSON: the counts, once done
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS migration_links (             -- a record's number in the old system, and what it became here
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  new_id INTEGER NOT NULL,
  UNIQUE (clinic_id, kind, source_id)
);

-- 0.50: the copy kept off site, and whether the internet is there (for the whole server, not one clinic)
CREATE TABLE IF NOT EXISTS offsite_log (
  id INTEGER PRIMARY KEY,
  ok INTEGER NOT NULL,
  detail TEXT,
  size_bytes INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS net_status (
  id INTEGER PRIMARY KEY,                                -- always 1
  ok INTEGER NOT NULL,
  checked_at TEXT NOT NULL,                              -- UTC
  failing_since TEXT
);

-- 0.52: diagnoses on claims, what a payer covers, and monthly claim batches
CREATE TABLE IF NOT EXISTS clinic_diagnoses (            -- codes a clinic adds to the starter ICD-10 list
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  code TEXT NOT NULL,
  title TEXT NOT NULL,
  UNIQUE (clinic_id, code)
);

CREATE TABLE IF NOT EXISTS payer_covered (               -- the treatments a payer pays for; none listed means the payer's usual rules
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  payer_id INTEGER NOT NULL REFERENCES payers(id),
  code_id INTEGER NOT NULL REFERENCES procedure_codes(id),
  UNIQUE (payer_id, code_id)
);

CREATE TABLE IF NOT EXISTS claim_batches (               -- one payer's claims for one month, sent together
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  payer_id INTEGER NOT NULL REFERENCES payers(id),
  month TEXT NOT NULL,                                   -- YYYY-MM
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent')),
  sent_on TEXT,
  payer_ref TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 0.53: running the service for many clinics: operators, their log, and support access a clinic grants
CREATE TABLE IF NOT EXISTS operators (                   -- the people who run the service; not clinic staff
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS operator_log (
  id INTEGER PRIMARY KEY,
  operator_id INTEGER REFERENCES operators(id),
  clinic_id INTEGER REFERENCES clinics(id),
  action TEXT NOT NULL,
  detail TEXT,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS support_grants (              -- a clinic letting support look, for a few hours
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  granted_by INTEGER REFERENCES users(id),
  until_at TEXT NOT NULL,                                -- UTC
  why TEXT,
  ended_at TEXT,
  ended_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 0.54: plans and the service's bills to clinics
CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  prices TEXT NOT NULL,                                  -- JSON: {currency: minor units a month}
  max_dentists INTEGER,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS platform_invoices (
  id INTEGER PRIMARY KEY,
  clinic_id INTEGER NOT NULL REFERENCES clinics(id),
  number INTEGER NOT NULL UNIQUE,
  plan_name TEXT NOT NULL,
  period_from TEXT NOT NULL,
  period_to TEXT NOT NULL,
  amount_p INTEGER NOT NULL,
  currency TEXT NOT NULL,
  due_on TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','paid','void')),
  pay_provider TEXT,
  pay_ref TEXT,
  paid_on TEXT,
  paid_method TEXT,
  paid_ref TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---- v0.57: speed with a large clinic --------------------------------------------------------------------------------------
-- "Has this patient got a booking coming up?" is asked for every row on the recall, reactivation and campaign lists.
-- Without this index a clinic with 20,000 patients waited almost a minute for the recall list.
CREATE INDEX IF NOT EXISTS idx_appt_patient_status ON appointments(clinic_id, patient_id, status, start_at);
-- Every table that holds a patient's records is looked up by clinic and patient. tests.test_v57 checks no table is missed.
CREATE INDEX IF NOT EXISTS idx_payments_patient ON payments(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_payment_plans_patient ON payment_plans(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_claims_patient ON claims(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_invoices_patient ON invoices(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_outbox_patient ON outbox(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_booking_requests_patient ON booking_requests(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_intake_submissions_patient ON intake_submissions(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_data_requests_patient ON data_requests(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_referrals_out_patient ON referrals_out(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_image_mounts_patient ON image_mounts(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_tasks_patient ON tasks(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_imaging_requests_patient ON imaging_requests(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_staff_messages_patient ON staff_messages(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_card_charges_patient ON card_charges(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_material_recall_patients_patient ON material_recall_patients(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_complaints_patient ON complaints(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_safety_incidents_patient ON safety_incidents(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_reactivation_holds_patient ON reactivation_holds(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_refunds_patient ON refunds(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_aftercare_given_patient ON aftercare_given(clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_proc_code ON procedures(clinic_id, code_id, status);

-- ---- v0.58: restore drills are written down, so the copies page can say when the last one was and whether it passed ----------
CREATE TABLE IF NOT EXISTS restore_drills (
  id INTEGER PRIMARY KEY,
  ok INTEGER NOT NULL,
  copy_name TEXT,
  made_at TEXT,                                          -- when the copy that was checked had been made
  patients INTEGER,
  live_patients INTEGER,
  files INTEGER,
  detail TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
