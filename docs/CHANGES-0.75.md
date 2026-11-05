# Changes in 0.75: the AI helper, step one

- `services/aidesk.py`: `classify` and `preference` (pattern rules), `suggest_slots`, `draft_reply` (patient's language, no money,
  none for clinical worries), `read_replies`, `used` (logs "ai.used"), `no_show_risk` and `risky_visits`, `brief`, `money_patterns`,
  `summary`, `match_articles` and `support_draft`.
- Messages: the patient's messages page shows the sorted reply, free times and a draft in the message box (read before the reply is
  marked as read); Replies shows how each was sorted.
- Today: tomorrow's list sorted by no-show risk with a "May not come" mark. Huddle: today's risky visits; for managers, What to act
  on today and money patterns.
- Visit page and chairside: Before this patient.
- HQ support tickets: matching help articles and "Use as the answer"; use is written in the operator log.
- French: `i18n_fr_staff39.py`; the reply drafts in Spanish, Portuguese and Arabic. Tests: `tests/test_v75.py` (12).
