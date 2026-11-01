"""French for versions 0.31 and 0.32: email through the clinic's mail account, and saved cards with automatic charges."""

STAFF19 = {
    "Also use it for my monthly fees as they fall due (optional).": "L’utiliser aussi pour mes mensualités à leur échéance (facultatif).",
    "Charge the card": "Débiter la carte", "Charges tried": "Débits tentés",
    "Email everyone who prefers email": "Envoyer un e-mail à tous ceux qui préfèrent l’e-mail",
    "Email everyone with an address": "Envoyer un e-mail à tous ceux qui ont une adresse",
    "Email goes through the clinic's own mail account, the same one staff use. Your mail provider gives these details; many call them SMTP settings. Messages never carry treatment details or amounts, the same as WhatsApp.":
        "Les e-mails partent du compte de messagerie du cabinet, celui qu’utilise l’équipe. Votre fournisseur de messagerie donne ces réglages ; beaucoup les appellent réglages SMTP. Les messages ne contiennent jamais de détails de soins ni de montants, comme pour WhatsApp.",
    "Email is on.": "L’e-mail est activé.", "Keep this card for later payments (optional).": "Garder cette carte pour de futurs paiements (facultatif).",
    "Let patients keep a card on file": "Permettre aux patients d’enregistrer une carte",
    "Let the server email the send list by itself, for patients who prefer email": "Laisser le serveur envoyer seul la liste d’envoi par e-mail, pour les patients qui préfèrent l’e-mail",
    "Mail server": "Serveur de messagerie", "Monthly fees charged automatically": "Mensualités débitées automatiquement",
    "Password (leave empty to keep the saved one)": "Mot de passe (laissez vide pour garder celui enregistré)", "Password saved.": "Mot de passe enregistré.",
    "Patients can choose, on the pay page, to keep their card with the payment company, and separately to have their monthly ortho or membership fees charged to it. Only the company's token is kept here, never the card. Check your payment company's terms and local law before switching this on.":
        "Sur la page de paiement, les patients peuvent choisir de garder leur carte chez la société de paiement et, séparément, d’y faire débiter leurs mensualités d’orthodontie ou d’adhésion. Seul le jeton de la société est gardé ici, jamais la carte. Vérifiez les conditions de votre société de paiement et la loi locale avant d’activer cette option.",
    "Patients whose preferred contact is email can be sent their messages by email.": "Les patients qui préfèrent l’e-mail peuvent recevoir leurs messages par e-mail.",
    "Port": "Port", "Remove the card": "Retirer la carte", "Remove the card, or stop automatic charges": "Retirer la carte, ou arrêter les débits automatiques",
    "Replies go to (optional)": "Les réponses vont à (facultatif)", "Saved cards": "Cartes enregistrées", "Security": "Sécurité",
    "Send a test email to": "Envoyer un e-mail de test à", "Send from this address": "Envoyer depuis cette adresse", "Send test": "Envoyer le test",
    "Sender name": "Nom de l’expéditeur", "Stop automatic charges": "Arrêter les débits automatiques", "Switch email on": "Activer l’e-mail",
    "The card is kept by the clinic's payment company, not by the clinic. You can ask the clinic to remove it at any time.":
        "La carte est gardée par la société de paiement du cabinet, pas par le cabinet. Vous pouvez demander au cabinet de la retirer à tout moment.",
    "The patient agreed:": "Le patient a accepté :", "The patient was told about this charge": "Le patient a été prévenu de ce débit",
    "User name": "Nom d’utilisateur", "Why, for example the patient asked": "Pourquoi, par exemple le patient l’a demandé",
    "automatic": "automatique", "ending": "se terminant par",
}

MESSAGES19 = {
    # the words a patient agrees to on the pay page (services/savedcards.py)
    "I agree that {clinic} may keep this card with its payment company and charge it for amounts I owe, after telling me.":
        "J’accepte que {clinic} garde cette carte chez sa société de paiement et la débite des montants que je dois, après m’avoir prévenu.",
    "I also agree that {clinic} may charge it for my monthly orthodontic or membership fees on the day they fall due.":
        "J’accepte aussi que {clinic} la débite de mes mensualités d’orthodontie ou d’adhésion le jour de leur échéance.",
    "Automatic charges stopped for this card.": "Débits automatiques arrêtés pour cette carte.",
    "Card charged. The payment is on the patient's account.": "Carte débitée. Le paiement est sur le compte du patient.",
    "Card removed. It can no longer be charged.": "Carte retirée. Elle ne peut plus être débitée.",
    "Tick that the patient was told about this charge first.": "Cochez d’abord que le patient a été prévenu de ce débit.",
    "Email is not set up. A manager sets it up under Settings, Email.": "L’e-mail n’est pas configuré. Un responsable le configure dans Paramètres, E-mail.",
    "Email is not switched on.": "L’e-mail n’est pas activé.",
    "Fill in the mail server and the sender address first.": "Remplissez d’abord le serveur de messagerie et l’adresse d’expédition.",
    "Only the patient can agree to automatic charges, on a pay page.": "Seul le patient peut accepter les débits automatiques, sur une page de paiement.",
    "Paystack charged a different amount. Check it in the Paystack dashboard.": "Paystack a débité un autre montant. Vérifiez-le dans le tableau de bord Paystack.",
    "Paystack needs the email the card was saved with, and there is none.": "Paystack a besoin de l’e-mail avec lequel la carte a été enregistrée, et il n’y en a pas.",
    "Pick how the connection is secured.": "Choisissez comment la connexion est sécurisée.",
    "Say why, for example: the patient asked.": "Dites pourquoi, par exemple : le patient l’a demandé.",
    "Sent by email.": "Envoyé par e-mail.", "Set up online payments first.": "Configurez d’abord les paiements en ligne.", "Statement emailed.": "Relevé envoyé par e-mail.",
    "Statements emailed. Anyone without an email address, or who has not agreed to messages, is left to print.":
        "Relevés envoyés par e-mail. Ceux qui n’ont pas d’adresse ou n’ont pas accepté les messages restent à imprimer.",
    "Nobody in this run has an email address and has agreed to messages. Print the statements instead.":
        "Personne dans cette série n’a d’adresse e-mail et accepté les messages. Imprimez plutôt les relevés.",
    "Test email sent. Check the inbox it went to.": "E-mail de test envoyé. Vérifiez la boîte de réception.",
    "That card has expired. Send the patient a pay link instead.": "Cette carte a expiré. Envoyez plutôt un lien de paiement au patient.",
    "That card is not on file.": "Cette carte n’est pas enregistrée.", "That pay link cannot be used.": "Ce lien de paiement ne peut pas être utilisé.",
    "The bank wants the patient to approve this payment themselves. Send them a pay link instead.": "La banque veut que le patient approuve lui-même ce paiement. Envoyez-lui plutôt un lien de paiement.",
    "The card token could not be read.": "Le jeton de la carte n’a pas pu être lu.",
    "The clinic now uses a different payment company. This card cannot be charged.": "Le cabinet utilise désormais une autre société de paiement. Cette carte ne peut pas être débitée.",
    "The mail password could not be read. A manager should type it again under Settings, Email.": "Le mot de passe de messagerie n’a pas pu être lu. Un responsable doit le saisir de nouveau dans Paramètres, E-mail.",
    "The mail server name looks wrong, for example smtp.example.com.": "Le nom du serveur de messagerie semble incorrect, par exemple smtp.example.com.",
    "The mail server refused the user name or password.": "Le serveur de messagerie a refusé le nom d’utilisateur ou le mot de passe.",
    "The port is a number, usually 587 or 465.": "Le port est un nombre, généralement 587 ou 465.",
    "The reply address does not look like an email address.": "L’adresse de réponse ne ressemble pas à une adresse e-mail.",
    "The sender address does not look like an email address.": "L’adresse d’expédition ne ressemble pas à une adresse e-mail.",
    "This clinic does not keep cards.": "Ce cabinet n’enregistre pas de cartes.", "This patient has no email address.": "Ce patient n’a pas d’adresse e-mail.",
}
