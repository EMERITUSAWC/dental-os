"""Who may do what. Roles are fixed for now: manager, dentist, assistant, front_desk.
Keep this file the one place where the rules live, so a future per-clinic permission screen has one thing to change."""

CLINICAL_ROLES = ("manager", "dentist", "assistant")   # see and write clinical records
SIGNING_ROLES = ("manager", "dentist")                 # sign notes and add addenda


def can_clinical(user):
    return bool(user) and user["role"] in CLINICAL_ROLES


def can_sign(user):
    return bool(user) and user["role"] in SIGNING_ROLES
