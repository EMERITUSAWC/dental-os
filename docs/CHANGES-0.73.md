# Changes in 0.73: Dentrix catch-ups, part one

- `services/huddle.py`: goals (`practice_goals` table; save, copy, check), working days less closed days, progress against
  goal and pace, the huddle (last working day, month so far, today's patients with to-dos, free chair time, tomorrow's unconfirmed
  visits), unsigned drafts with age, membership savings for a quote, and the WhatsApp join link.
- `huddleviews.py`: `/huddle` (everyone; money for managers), `/reports/goals` (managers; reports area), `/notes/unsigned`
  (signers; clinical area).
- Today screen: notes waiting to be signed, a Morning huddle button.
- Plan page and printed quote: membership savings for patients with no cover and no membership; QR to WhatsApp on the quote.
- `clinics.offer_membership` (on by default), switched on Membership plans.
- French: `i18n_fr_staff37.py`; Spanish, Portuguese and Arabic for the new quote wording. Tests: `tests/test_v73.py` (14).
