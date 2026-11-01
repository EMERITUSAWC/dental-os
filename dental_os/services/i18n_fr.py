"""French wording. The key is the English wording used in the templates and in the code (see i18n.py). Keep the %(names)s and
{fields} exactly as they are in the English. A test checks this file against the templates."""
import re

_FR = {
    # ---- menus
    "Main menu": "Menu principal",
    "Today": "Aujourd'hui",
    "Diary": "Agenda",
    "Patients": "Patients",
    "Recalls": "Rappels",
    "Messages": "Messages",
    "Lab": "Labo",
    "Stock": "Stock",
    "Claims": "Remboursements",
    "Plans": "Plans de soins",
    "Memberships": "Adhésions",
    "Reports": "Rapports",
    "All clinics": "Tous les cabinets",
    "Staff": "Équipe",
    "Tools": "Outils",
    "Settings": "Paramètres",
    "Activity": "Activité",
    "Log out": "Se déconnecter",

    # ---- sign in
    "Log in": "Se connecter",
    "Username": "Nom d'utilisateur",
    "Password": "Mot de passe",
    "Your code": "Votre code",
    "Open your authenticator app and type the 6-digit code it shows. If you have lost your phone, type one of your recovery codes instead.":
        "Ouvrez votre application d'authentification et saisissez le code à 6 chiffres qu'elle affiche. Si vous avez perdu votre téléphone, saisissez plutôt l'un de vos codes de secours.",
    "Code": "Code",
    "Continue": "Continuer",
    "Start again": "Recommencer",

    # ---- documents: common
    "Patient": "Patient",
    "Date": "Date",
    "Amount": "Montant",
    "Total": "Total",
    "Balance": "Solde",
    "Note": "Note",
    "Voided:": "Annulé :",
    "tooth": "dent",
    "days": "jours",

    # ---- receipt
    "Receipt": "Reçu",
    "VOIDED": "ANNULÉ",
    "Received from": "Reçu de",
    "Paid by": "Payé par",
    "ref": "réf.",
    "Credit on account": "Crédit sur le compte",
    "Balance now": "Solde actuel",
    "Received by": "Reçu par",
    "Thank you. This is a payment receipt.": "Merci. Ceci est un reçu de paiement.",

    # ---- invoice
    "Invoice": "Facture",
    "Tax": "Taxe",
    "number": "numéro",
    "VOID": "ANNULÉE",
    "Billed to": "Facturé à",
    "Issued": "Émise le",
    "Pay by": "À payer avant le",
    "Description": "Description",
    "Subtotal": "Sous-total",
    "on": "sur",
    "(included in the prices)": "(inclus dans les prix)",
    "Payments, discounts and insurance are shown on the patient's statement.":
        "Les paiements, les remises et l'assurance figurent sur le relevé de compte du patient.",

    # ---- statement
    "Statement": "Relevé de compte",
    "What": "Détail",
    "voided": "annulé",
    "Credit:": "Crédit :",
    "Nothing to pay:": "Rien à payer :",
    "Of the amount due:": "Sur le montant dû :",
    "the insurer is expected to pay": "l'assureur doit payer",
    "Payment": "Paiement",
    "Membership fee": "Cotisation d'adhésion",
    "Cash": "Espèces",
    "Mobile money": "Mobile money",
    "Card": "Carte",
    "Bank transfer": "Virement bancaire",
    "Insurance": "Assurance",
    "Birthday offer": "Offre d'anniversaire",
    "Seasonal offer": "Offre de saison",
    "Other discount": "Autre remise",
    "Write-off": "Abandon de créance",
    "Correction": "Correction",

    # ---- quote
    "Quote": "Devis",
    "Treatment quote": "Devis de soins",
    "Plan": "Plan",
    "Phase %(n)s": "Phase %(n)s",
    "Phase %(n)s total": "Total de la phase %(n)s",
    "Total:": "Total :",
    "Deposit paid:": "Acompte versé :",
    "This is an estimate, not a fixed price. The cost may change if the dentist finds that more treatment is needed once work begins. You will be told before any change. You can say no to any part of this plan.":
        "Ceci est une estimation, pas un prix ferme. Le coût peut changer si le dentiste constate que des soins supplémentaires sont nécessaires une fois le travail commencé. Vous serez prévenu avant tout changement. Vous pouvez refuser n'importe quelle partie de ce plan.",

    # ---- consent
    "Signed by": "Signé par",
    "the patient": "le patient",
    "parent": "parent",
    "legal guardian": "tuteur légal",
    "authorised person": "personne autorisée",
    "How": "Mode",
    "Typed on screen": "Saisi à l'écran",
    "On paper": "Sur papier",
    "When": "Date et heure",
    "at": "à",
    "Recorded by": "Enregistré par",
    "Withdrawn": "Retiré",

    # ---- patient's private page
    "Hello %(name)s. This page is only for you. Please do not share its address.":
        "Bonjour %(name)s. Cette page est réservée à vous seul. Merci de ne pas partager son adresse.",
    "Your visits": "Vos rendez-vous",
    "with": "avec",
    "You confirmed": "Vous avez confirmé",
    "You said you cannot come": "Vous avez dit que vous ne pouvez pas venir",
    "I will be there": "Je serai là",
    "I cannot come": "Je ne peux pas venir",
    "You have no visits booked.": "Vous n'avez aucun rendez-vous.",
    "Your quote: %(title)s": "Votre devis : %(title)s",
    "Type your full name to accept": "Saisissez votre nom complet pour accepter",
    "I am": "Je suis",
    "the patient's parent": "le parent du patient",
    "the patient's guardian": "le tuteur légal du patient",
    "I have read the costs above and I agree to this treatment plan": "J'ai lu les coûts ci-dessus et j'accepte ce plan de soins",
    "Accepting does not book anything. The clinic will contact you to arrange the visits.":
        "Accepter ne réserve rien. Le cabinet vous contactera pour organiser les rendez-vous.",
    "Accept this quote": "Accepter ce devis",
    "Your account": "Votre compte",
    "Amount due:": "Montant dû :",
    "Please ask the clinic how to pay.": "Veuillez demander au cabinet comment payer.",
    "You have a credit of %(amount)s.": "Vous avez un crédit de %(amount)s.",
    "Nothing is due.": "Vous ne devez rien.",
    "Your health form": "Votre questionnaire de santé",
    "Thank you. The clinic is checking the form you sent.": "Merci. Le cabinet vérifie le questionnaire que vous avez envoyé.",
    "Please tell us about your health before your visit. It takes about two minutes.":
        "Merci de nous renseigner sur votre santé avant votre rendez-vous. Cela prend environ deux minutes.",
    "Update my health form": "Mettre à jour mon questionnaire de santé",
    "Fill in my health form": "Remplir mon questionnaire de santé",
    "Our team": "Notre équipe",
    "our team": "notre équipe",

    # ---- health form
    "Health form": "Questionnaire de santé",
    "Please answer as well as you can. A member of the clinical team will read it. If something is urgent, call the clinic.":
        "Répondez du mieux que vous pouvez. Un membre de l'équipe soignante le lira. En cas d'urgence, appelez le cabinet.",
    "Health conditions (for example diabetes, asthma, heart problems). Write none if there are none.":
        "Problèmes de santé (par exemple diabète, asthme, problèmes cardiaques). Écrivez « aucun » s'il n'y en a pas.",
    "Medicines you take": "Médicaments que vous prenez",
    "Allergies (medicines, latex, anything else)": "Allergies (médicaments, latex, autre)",
    "I take blood thinners (for example warfarin or aspirin every day)":
        "Je prends des anticoagulants (par exemple de la warfarine, ou de l'aspirine tous les jours)",
    "Are you pregnant?": "Êtes-vous enceinte ?",
    "Not applicable": "Sans objet",
    "No": "Non",
    "Yes": "Oui",
    "Do you smoke?": "Fumez-vous ?",
    "Prefer not to say": "Je préfère ne pas répondre",
    "Never": "Jamais",
    "I used to": "Avant, oui",
    "Anything else we should know?": "Autre chose que nous devrions savoir ?",
    "It is fine to send me appointment messages on WhatsApp": "Vous pouvez m'envoyer des messages sur mes rendez-vous par WhatsApp",
    "Send to the clinic": "Envoyer au cabinet",
    "Back": "Retour",

    # ---- online booking
    "Ask for a visit": "Demander un rendez-vous",
    "Choose a time that suits you. This is a request. The clinic will confirm it by phone or WhatsApp.":
        "Choisissez un horaire qui vous convient. Il s'agit d'une demande. Le cabinet la confirmera par téléphone ou par WhatsApp.",
    "Dentist": "Dentiste",
    "First available": "Premier créneau disponible",
    "Show times": "Voir les horaires",
    "Leave this empty": "Laissez ce champ vide",
    "Pick a time": "Choisissez un horaire",
    "There are no free times in the next %(days)s days. Please contact the clinic directly.":
        "Il n'y a aucun horaire libre dans les %(days)s prochains jours. Merci de contacter directement le cabinet.",
    "About you": "À propos de vous",
    "First name": "Prénom",
    "Last name": "Nom",
    "Phone (WhatsApp if you can)": "Téléphone (WhatsApp si possible)",
    "Email (optional)": "E-mail (facultatif)",
    "What is the visit for?": "Quel est le motif du rendez-vous ?",
    "Anything we should know? (optional, please do not write medical details here)":
        "Autre chose à nous dire ? (facultatif, merci de ne pas écrire de détails médicaux ici)",
    "Ask for this time": "Demander cet horaire",
    "We keep your details only to arrange your visit.": "Nous gardons vos coordonnées uniquement pour organiser votre rendez-vous.",
    "Request received": "Demande reçue",
    "Thank you": "Merci",
    "We have your request. It is not a confirmed booking yet. The clinic will contact you to confirm, or to suggest another time.":
        "Nous avons bien reçu votre demande. Ce n'est pas encore un rendez-vous confirmé. Le cabinet vous contactera pour confirmer, ou pour proposer un autre horaire.",
    "Consultation": "Consultation",
    "Cleaning & preventive care": "Détartrage et soins préventifs",
    "Fillings & restorations": "Plombages et restaurations",
    "Root canal treatment": "Traitement de canal",
    "Tooth extractions": "Extractions dentaires",
    "Crowns": "Couronnes",
    "Bridges": "Bridges",
    "Dentures": "Prothèses dentaires",
    "Implants": "Implants",
    "Orthodontics & smile enhancement": "Orthodontie et esthétique du sourire",
    "Dental imaging": "Imagerie dentaire",
    "Emergency or pain": "Urgence ou douleur",
    "Not sure yet": "Je ne sais pas encore",

    # ---- what the visitor sees after pressing a button, or when something is wrong
    "Thank you. We have your answer.": "Merci. Nous avons bien reçu votre réponse.",
    "Thank you. The clinic has your agreement and will contact you to book.":
        "Merci. Le cabinet a bien reçu votre accord et vous contactera pour fixer les rendez-vous.",
    "Thank you. The clinic will check your answers.": "Merci. Le cabinet vérifiera vos réponses.",
    "Too many requests from this connection. Please try again later, or contact the clinic directly.":
        "Trop de demandes depuis cette connexion. Merci de réessayer plus tard, ou de contacter directement le cabinet.",
    "We are getting a lot of requests right now. Please try again later, or contact the clinic directly.":
        "Nous recevons beaucoup de demandes en ce moment. Merci de réessayer plus tard, ou de contacter directement le cabinet.",
    "We cannot take more requests online right now. Please contact the clinic directly.":
        "Nous ne pouvons pas prendre d'autres demandes en ligne pour le moment. Merci de contacter directement le cabinet.",
    "First name is needed.": "Le prénom est obligatoire.",
    "Last name is needed.": "Le nom est obligatoire.",
    "That email address does not look right.": "Cette adresse e-mail ne semble pas correcte.",
    "Enter a phone number.": "Saisissez un numéro de téléphone.",
    "Phone numbers can only have digits, spaces and a leading +.": "Un numéro de téléphone ne peut contenir que des chiffres, des espaces et un + au début.",
    "That phone number looks the wrong length.": "Ce numéro de téléphone n'a pas la bonne longueur.",
    "Phone numbers can only have digits and spaces.": "Un numéro de téléphone ne peut contenir que des chiffres et des espaces.",
    "Start the number with the country code, for example +233 or +44.": "Commencez le numéro par l'indicatif du pays, par exemple +233 ou +44.",
    "Pick what the visit is for.": "Choisissez le motif du rendez-vous.",
    "Pick one of the times shown.": "Choisissez l'un des horaires proposés.",
    "Pick a dentist from the list.": "Choisissez un dentiste dans la liste.",
    "Sorry, that time has just been taken or is no longer offered. Please pick another.":
        "Désolé, cet horaire vient d'être pris ou n'est plus proposé. Merci d'en choisir un autre.",
    "We already have a request from this number for that time.": "Nous avons déjà une demande de ce numéro pour cet horaire.",
    "Pick one of the two answers.": "Choisissez l'une des deux réponses.",
    "That visit was not found.": "Ce rendez-vous est introuvable.",
    "That quote was not found, or it is not waiting for an answer.": "Ce devis est introuvable, ou il n'attend pas de réponse.",
    "Enter the full name of the person who agreed.": "Saisissez le nom complet de la personne qui a donné son accord.",
    "Tick the box to confirm the patient has seen the cost and agreed.": "Cochez la case pour confirmer que le patient a vu le coût et qu'il est d'accord.",
    "This patient is under 18. A parent or guardian needs to agree.": "Ce patient a moins de 18 ans. Un parent ou un tuteur doit donner son accord.",
    "Only booked appointments can be confirmed.": "Seuls les rendez-vous fixés peuvent être confirmés.",
    "Your last form is still waiting to be checked by the clinic. Thank you.": "Votre dernier questionnaire attend encore d'être vérifié par le cabinet. Merci.",
    "One of your answers is too long. Please shorten it.": "L'une de vos réponses est trop longue. Merci de la raccourcir.",
    "Please answer at least one question. If nothing applies, write 'none' under conditions.":
        "Merci de répondre à au moins une question. Si rien ne s'applique, écrivez « aucun » sous les problèmes de santé.",
}

# The wording of each kind of message when the patient's language is French and the clinic has kept the starter wording.
STARTERS_FR = {
    "confirmation": "Bonjour {first_name}, ici {clinic}. Nous avons fixé votre rendez-vous avec {dentist} le {day} à {time}. "
                    "Merci de répondre OUI pour confirmer, ou de nous dire si vous avez besoin d'un autre horaire.",
    "reminder": "Bonjour {first_name}, un rappel de {clinic} : votre rendez-vous avec {dentist} est prévu le {day} à {time}. "
                "Merci de répondre OUI pour confirmer.",
    "recall": "Bonjour {first_name}, ici {clinic}. C'est bientôt le moment de votre prochain contrôle. "
              "Répondez à ce message et nous trouverons un horaire qui vous convient.",
    "plan_followup": "Bonjour {first_name}, ici {clinic}. Nous faisons suite aux soins dont nous avons parlé. "
                     "Si vous avez des questions, ou si vous souhaitez prendre rendez-vous, répondez simplement à ce message.",
    "review_request": "Bonjour {first_name}, merci de votre visite chez {clinic}. Si vous avez été satisfait de votre visite, "
                      "pourriez-vous nous laisser un court avis ? {review_link}",
    "birthday": "Joyeux anniversaire {first_name}, de la part de toute l'équipe de {clinic}.",
    "reactivation": "Bonjour {first_name}, ici {clinic}. Cela fait un moment que nous ne vous avons pas vu. "
                    "Si vous souhaitez un contrôle, répondez à ce message et nous trouverons un horaire qui vous convient.",
    "portal_link": "Bonjour {first_name}, voici votre lien privé vers {clinic} : {link} Il affiche vos rendez-vous et vous permet "
                   "de remplir votre questionnaire de santé. Merci de ne pas le partager.",
}


def _typography(text):
    """French puts a non-breaking space before ? ! : and ; and inside « ». Written plainly above, fixed here."""
    text = re.sub(r" ([?!:;])", " \\1", text)
    return text.replace("« ", "« ").replace(" »", " »")


from .i18n_fr_staff import STAFF as _STAFF  # noqa: E402
_clash = sorted(k for k in _STAFF if k in _FR and _FR[k] != _STAFF[k])
if _clash:
    raise RuntimeError("French wording given twice with different text: %s" % _clash)
_FR.update(_STAFF)
from .i18n_fr_staff2 import STAFF2 as _STAFF2  # noqa: E402
_clash = sorted(k for k in _STAFF2 if k in _FR and _FR[k] != _STAFF2[k])
if _clash:
    raise RuntimeError("French wording given twice with different text: %s" % _clash)
_FR.update(_STAFF2)
from .i18n_fr_staff3 import STAFF3 as _STAFF3  # noqa: E402
_clash = sorted(k for k in _STAFF3 if k in _FR and _FR[k] != _STAFF3[k])
if _clash:
    raise RuntimeError("French wording given twice with different text: %s" % _clash)
_FR.update(_STAFF3)
from .i18n_fr_staff4 import STAFF4 as _STAFF4  # noqa: E402
_clash = sorted(k for k in _STAFF4 if k in _FR and _FR[k] != _STAFF4[k])
if _clash:
    raise RuntimeError("French wording given twice with different text: %s" % _clash)
_FR.update(_STAFF4)
from .i18n_fr_staff5 import STAFF5 as _STAFF5  # noqa: E402
_clash = sorted(k for k in _STAFF5 if k in _FR and _FR[k] != _STAFF5[k])
if _clash:
    raise RuntimeError("French wording given twice with different text: %s" % _clash)
_FR.update(_STAFF5)
from .i18n_fr_staff6 import STAFF6 as _STAFF6  # noqa: E402
_clash = sorted(k for k in _STAFF6 if k in _FR and _FR[k] != _STAFF6[k])
if _clash:
    raise RuntimeError("French wording given twice with different text: %s" % _clash)
_FR.update(_STAFF6)
from .i18n_fr_staff7 import MESSAGES as _STAFF7  # noqa: E402
_clash = sorted(k for k in _STAFF7 if k in _FR and _FR[k] != _STAFF7[k])
if _clash:
    raise RuntimeError("French wording given twice with different text: %s" % _clash)
_FR.update(_STAFF7)
from .i18n_fr_staff8 import STAFF8 as _STAFF8, MESSAGES8 as _MESSAGES8  # noqa: E402
for _part in (_STAFF8, _MESSAGES8):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff9 import STAFF9 as _STAFF9, MESSAGES9 as _MESSAGES9  # noqa: E402
for _part in (_STAFF9, _MESSAGES9):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff10 import STAFF10 as _STAFF10, MESSAGES10 as _MESSAGES10  # noqa: E402
for _part in (_STAFF10, _MESSAGES10):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff11 import STAFF11 as _STAFF11, MESSAGES11 as _MESSAGES11  # noqa: E402
for _part in (_STAFF11, _MESSAGES11):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff12 import STAFF12 as _STAFF12, MESSAGES12 as _MESSAGES12  # noqa: E402
for _part in (_STAFF12, _MESSAGES12):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff13 import STAFF13 as _STAFF13, MESSAGES13 as _MESSAGES13  # noqa: E402
for _part in (_STAFF13, _MESSAGES13):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff14 import STAFF14 as _STAFF14, MESSAGES14 as _MESSAGES14  # noqa: E402
for _part in (_STAFF14, _MESSAGES14):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff15 import STAFF15 as _STAFF15, MESSAGES15 as _MESSAGES15  # noqa: E402
for _part in (_STAFF15, _MESSAGES15):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff16 import STAFF16 as _STAFF16, MESSAGES16 as _MESSAGES16  # noqa: E402
for _part in (_STAFF16, _MESSAGES16):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff17 import STAFF17 as _STAFF17, MESSAGES17 as _MESSAGES17  # noqa: E402
for _part in (_STAFF17, _MESSAGES17):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff18 import STAFF18 as _STAFF18, MESSAGES18 as _MESSAGES18  # noqa: E402
for _part in (_STAFF18, _MESSAGES18):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff19 import STAFF19 as _STAFF19, MESSAGES19 as _MESSAGES19  # noqa: E402
for _part in (_STAFF19, _MESSAGES19):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff20 import STAFF20 as _STAFF20, MESSAGES20 as _MESSAGES20  # noqa: E402
for _part in (_STAFF20, _MESSAGES20):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff21 import STAFF21 as _STAFF21, MESSAGES21 as _MESSAGES21  # noqa: E402
for _part in (_STAFF21, _MESSAGES21):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff22 import STAFF22 as _STAFF22, MESSAGES22 as _MESSAGES22  # noqa: E402
for _part in (_STAFF22, _MESSAGES22):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff23 import STAFF23 as _STAFF23, MESSAGES23 as _MESSAGES23  # noqa: E402
for _part in (_STAFF23, _MESSAGES23):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff24 import STAFF24 as _STAFF24, MESSAGES24 as _MESSAGES24  # noqa: E402
for _part in (_STAFF24, _MESSAGES24):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff25 import STAFF25 as _STAFF25, MESSAGES25 as _MESSAGES25  # noqa: E402
for _part in (_STAFF25, _MESSAGES25):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff26 import STAFF26 as _STAFF26, MESSAGES26 as _MESSAGES26  # noqa: E402
for _part in (_STAFF26, _MESSAGES26):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff27 import STAFF27 as _STAFF27, MESSAGES27 as _MESSAGES27  # noqa: E402
for _part in (_STAFF27, _MESSAGES27):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff28 import STAFF28 as _STAFF28, MESSAGES28 as _MESSAGES28  # noqa: E402
for _part in (_STAFF28, _MESSAGES28):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff29 import STAFF29 as _STAFF29, MESSAGES29 as _MESSAGES29  # noqa: E402
for _part in (_STAFF29, _MESSAGES29):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
from .i18n_fr_staff30 import STAFF30 as _STAFF30, MESSAGES30 as _MESSAGES30  # noqa: E402
from .i18n_fr_staff31 import STAFF31 as _STAFF31, MESSAGES31 as _MESSAGES31  # noqa: E402
from .i18n_fr_staff32 import STAFF32 as _STAFF32, MESSAGES32 as _MESSAGES32  # noqa: E402
for _part in (_STAFF30, _MESSAGES30, _STAFF31, _MESSAGES31, _STAFF32, _MESSAGES32):
    _clash = sorted(k for k in _part if k in _FR and _FR[k] != _part[k])
    if _clash:
        raise RuntimeError("French wording given twice with different text: %s" % _clash)
    _FR.update(_part)
# Plain messages and errors raised in the code (not in templates). tests.test_v10 checks each is still in the code.
MESSAGE_KEYS = set(_STAFF7) | set(_MESSAGES8) | set(_MESSAGES9) | set(_MESSAGES10) | set(_MESSAGES11) | set(_MESSAGES12) | set(_MESSAGES13) | set(_MESSAGES14) | set(_MESSAGES15) | set(_MESSAGES16) | set(_MESSAGES17) | set(_MESSAGES18) | set(_MESSAGES19) | set(_MESSAGES20) | set(_MESSAGES21) | set(_MESSAGES22) | set(_MESSAGES23) | set(_MESSAGES24) | set(_MESSAGES25) | set(_MESSAGES26) | set(_MESSAGES27) | set(_MESSAGES28) | set(_MESSAGES29) | set(_MESSAGES30) | set(_MESSAGES31) | set(_MESSAGES32)

FR = {k: _typography(v) for k, v in _FR.items()}
STARTERS_FR = {k: _typography(v) for k, v in STARTERS_FR.items()}
