"""French for version 0.21: phone calls through the clinic's Twilio account."""

STAFF13 = {
    "Calls": "Appels",
    "Phone ringing:": "Le téléphone sonne :",
    "Number not on any record": "Numéro inconnu dans les dossiers",
    "Phone set-up": "Réglage du téléphone",
    "Phone calls are not switched on.": "Les appels téléphoniques ne sont pas activés.",
    "Direction": "Sens",
    "In": "Entrant",
    "Out": "Sortant",
    "call-back task": "tâche de rappel",
    "No calls yet.": "Aucun appel pour l’instant.",
    "Call": "Appeler",
    "Phone calls": "Appels téléphoniques",
    "With a Twilio phone number, calls to the clinic ring the desk phone and every screen shows who is calling. A call nobody answers becomes a call-back task. Staff can call a patient from their record: the desk phone rings first, then the patient's, and the patient sees the clinic's number.":
        "Avec un numéro Twilio, les appels au cabinet font sonner le téléphone de l’accueil et chaque écran affiche qui appelle. Un appel sans réponse devient une tâche de rappel. L’équipe peut appeler un patient depuis son dossier : le téléphone de l’accueil sonne d’abord, puis celui du patient, qui voit le numéro du cabinet.",
    "Calls are not recorded. Recording needs the caller's consent, and the rules differ by country.":
        "Les appels ne sont pas enregistrés. L’enregistrement demande le consentement de l’appelant, et les règles varient selon les pays.",
    "Auth token saved.": "Jeton d’authentification enregistré.",
    "Twilio Account SID": "Account SID Twilio",
    "Auth token (leave empty to keep the saved one)": "Jeton d’authentification (laissez vide pour garder celui enregistré)",
    "The clinic's Twilio number": "Le numéro Twilio du cabinet",
    "The desk phone that rings": "Le téléphone de l’accueil qui sonne",
    "Call-back tasks go to": "Les tâches de rappel vont à",
    "Switch phone calls on": "Activer les appels téléphoniques",
    "Address for Twilio": "Adresse pour Twilio",
    "In Twilio, open the phone number and set A call comes in to Webhook, HTTP POST, this address:":
        "Dans Twilio, ouvrez le numéro et réglez A call comes in sur Webhook, HTTP POST, avec cette adresse :",
    # call statuses (services/voice.py)
    "Ringing": "Sonne",
    "Answered": "Décroché",
    "Finished": "Terminé",
    "Missed": "Manqué",
    "Busy": "Occupé",
    "No answer": "Sans réponse",
    "Calling": "Appel en cours",
}

MESSAGES13 = {
    "The Account SID starts AC and has 32 letters and digits after it.": "L’Account SID commence par AC, suivi de 32 lettres et chiffres.",
    "The auth token is 32 letters and digits.": "Le jeton d’authentification compte 32 lettres et chiffres.",
    "Write phone numbers in full international form, for example +233302123456.": "Écrivez les numéros au format international complet, par exemple +233302123456.",
    "Fill in the Account SID, auth token, the clinic's Twilio number and the desk phone first.": "Remplissez d’abord l’Account SID, le jeton, le numéro Twilio du cabinet et le téléphone de l’accueil.",
    "This patient has no phone number to call.": "Ce patient n’a pas de numéro à appeler.",
    "The auth token could not be read. A manager should paste it again under Settings.": "Le jeton n’a pas pu être lu. Un responsable doit le coller de nouveau dans Paramètres.",
    "Your desk phone is ringing. Pick it up and the patient's phone will ring next.": "Le téléphone de l’accueil sonne. Décrochez et le téléphone du patient sonnera ensuite.",
    "Phone call answered at the desk.": "Appel décroché à l’accueil.",
    "Missed phone call.": "Appel manqué.",
    "Phone call to the patient through the clinic's line.": "Appel au patient par la ligne du cabinet.",
    # spoken to callers, in the clinic's language
    "Sorry, the clinic cannot take calls on this line right now.": "Désolé, le cabinet ne peut pas prendre d’appels sur cette ligne pour le moment.",
    "Sorry we missed your call. We will call you back as soon as we can.": "Désolé, nous avons manqué votre appel. Nous vous rappellerons dès que possible.",
    "Sorry, this call could not be connected.": "Désolé, cet appel n’a pas pu être établi.",
}
