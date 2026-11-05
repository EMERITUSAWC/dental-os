# Changes in 0.74: Dentrix catch-ups, part two

- `services/files.py`: `parse_teeth` (the clinic's own numbering, stored as FDI), `set_details`, `pictures`, `for_tooth`, `pick`,
  `gaps`. `patient_files.teeth`. Pages: `/patients/<pid>/compare`, `POST /files/<id>/details`.
- `services/notify.py`: `items` and `total`, with `LABELS`. Page: `/notifications`; Inbox has All notifications, and drafts to
  sign count in the Inbox number.
- `services/cardfee.py`: `clinics.card_fee_bp`, country rules (`BANNED`), `parse_pct`, `set_fee`, `fee_for`, `add_fee` (purpose
  `card_fee`), `fee_with`. Payment form tick box; receipt line; setting on Online payments. `billing.record_payment` accepts the
  `card_fee` purpose.
- `services/tours.py`: four tours; `user_tours` table; `/tours/<key>/done`, `/tours/again`; tour steps in `base.html`, shown by
  `static/app.js`.
- French: `i18n_fr_staff38.py`; Spanish, Portuguese and Arabic for the receipt and menu words. Tests: `tests/test_v74.py` (11).
