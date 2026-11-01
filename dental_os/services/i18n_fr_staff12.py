"""French for version 0.20: online payments (set-up, pay links, the patient's pay page)."""

STAFF12 = {
    "A test key (it starts sk_test_) uses the company's test system, so you can try the whole thing with test cards before any real money moves.":
        "Une clé de test (elle commence par sk_test_) utilise le système de test de la société, pour tout essayer avec des cartes de test avant que de l’argent réel ne circule.",
    "All pay links for this patient": "Tous les liens de paiement de ce patient",
    "Amount:": "Montant :",
    "Ask for a payment online": "Demander un paiement en ligne",
    "Check if it is paid": "Vérifier s’il est payé",
    "Check the key works": "Vérifier que la clé fonctionne",
    "Key saved: %(hint)s": "Clé enregistrée : %(hint)s",
    "Made": "Créé le",
    "Make a new address (the old one stops working)": "Créer une nouvelle adresse (l’ancienne cesse de fonctionner)",
    "Make a pay link": "Créer un lien de paiement",
    "Message:": "Message :",
    "No pay links yet.": "Pas encore de lien de paiement.",
    "Online payments are not switched on.": "Les paiements en ligne ne sont pas activés.",
    "Online payments": "Paiements en ligne",
    "Or the patient scans this at the desk:": "Ou le patient scanne ceci à l’accueil :",
    "Paste this address into the payment company's dashboard as the webhook (Paystack: Settings, API keys and webhooks. Stripe: Developers, Webhooks, event checkout.session.completed). It lets the company tell us at once when a patient has paid, even if they close the page.":
        "Collez cette adresse comme webhook dans le tableau de bord de la société de paiement (Paystack : Settings, API keys and webhooks. Stripe : Developers, Webhooks, événement checkout.session.completed). La société nous prévient ainsi tout de suite quand un patient a payé, même s’il ferme la page.",
    "Patients can pay by card or mobile money from a link on their phone. The money goes straight to the clinic's own account with the payment company; this system never holds it. Open the account with the company first, then paste the secret key here.":
        "Les patients peuvent payer par carte ou paiement mobile depuis un lien sur leur téléphone. L’argent va directement sur le compte du cabinet auprès de la société de paiement ; ce système ne le détient jamais. Ouvrez d’abord le compte auprès de la société, puis collez la clé secrète ici.",
    "Pay link for %(name)s": "Lien de paiement pour %(name)s",
    "Pay link": "Lien de paiement",
    "Pay links": "Liens de paiement",
    "Pay now": "Payer maintenant",
    "Payment company": "Société de paiement",
    "Payment for %(name)s": "Paiement pour %(name)s",
    "Paystack does not take this clinic's currency, so use Stripe.": "Paystack n’accepte pas la devise de ce cabinet : utilisez Stripe.",
    "Please contact the clinic for a new one.": "Veuillez contacter le cabinet pour en obtenir un nouveau.",
    "Secret key (leave empty to keep the saved one)": "Clé secrète (laisser vide pour garder celle enregistrée)",
    "See the receipt": "Voir le reçu",
    "Set them up": "Les configurer",
    "Stripe only: signing secret for the webhook (whsec_...)": "Stripe seulement : secret de signature du webhook (whsec_...)",
    "Switch online payments on": "Activer les paiements en ligne",
    "Test mode": "Mode test",
    "Thank you. Your payment has been received.": "Merci. Votre paiement a bien été reçu.",
    "The clinic has your receipt. You can close this page.": "Le cabinet a votre reçu. Vous pouvez fermer cette page.",
    "The patient gets a link to pay by card or mobile money. When they pay, a receipt goes on their account by itself.":
        "Le patient reçoit un lien pour payer par carte ou paiement mobile. Quand il paie, un reçu s’ajoute tout seul à son compte.",
    "This link can no longer be used.": "Ce lien ne peut plus être utilisé.",
    "We have not received the payment yet. If you paid, wait a minute and open this page again.":
        "Nous n’avons pas encore reçu le paiement. Si vous avez payé, attendez une minute et rouvrez cette page.",
    "Webhook address": "Adresse du webhook",
    "What it is for": "Objet",
    "Works until": "Valable jusqu’au",
    "You will go to the clinic's payment company to pay by card or mobile money, then come back here.":
        "Vous allez passer par la société de paiement du cabinet pour payer par carte ou paiement mobile, puis revenir ici.",
    "for example crown, deposit": "par exemple couronne, acompte",
    "pay links": "liens de paiement",
    # service labels
    "Paystack (cards and mobile money in Ghana, Nigeria, Kenya, South Africa)": "Paystack (cartes et paiement mobile au Ghana, au Nigeria, au Kenya, en Afrique du Sud)",
    "Stripe (cards in the UK, Europe, the US and many other countries)": "Stripe (cartes au Royaume-Uni, en Europe, aux États-Unis et dans beaucoup d’autres pays)",
    "Waiting for payment": "En attente de paiement", "Paid": "Payé", "Expired": "Expiré", "Failed": "Échec",
}

MESSAGES12 = {
    "Hello %(name)s, here is the link to pay %(amount)s to %(clinic)s: %(url)s": "Bonjour %(name)s, voici le lien pour payer %(amount)s à %(clinic)s : %(url)s",
    "A Paystack secret key starts sk_live_ or sk_test_.": "Une clé secrète Paystack commence par sk_live_ ou sk_test_.",
    "A Stripe secret key starts sk_live_, sk_test_ or rk_.": "Une clé secrète Stripe commence par sk_live_, sk_test_ ou rk_.",
    "A Stripe signing secret starts whsec_.": "Un secret de signature Stripe commence par whsec_.",
    "Not paid yet.": "Pas encore payé.",
    "Only a link that is still waiting can be cancelled.": "Seul un lien encore en attente peut être annulé.",
    "Paid. The payment is on the patient's account.": "Payé. Le paiement est sur le compte du patient.",
    "Paste the secret key before switching online payments on.": "Collez la clé secrète avant d’activer les paiements en ligne.",
    "Pay link cancelled.": "Lien de paiement annulé.",
    "Pay link made. Send it to the patient.": "Lien de paiement créé. Envoyez-le au patient.",
    "Paystack does not take this clinic's currency. Use Stripe, or change the currency.": "Paystack n’accepte pas la devise de ce cabinet. Utilisez Stripe, ou changez de devise.",
    "Paystack needs an email address. Add one to the patient's record, or the clinic's email under Settings.":
        "Paystack a besoin d’une adresse e-mail. Ajoutez-en une au dossier du patient, ou l’e-mail du cabinet dans Paramètres.",
    "Pick a payment company from the list.": "Choisissez une société de paiement dans la liste.",
    "That pay link was not found.": "Ce lien de paiement est introuvable.",
    "The amount paid does not match the link. Check it with the payment company.": "Le montant payé ne correspond pas au lien. Vérifiez auprès de la société de paiement.",
    "The payment company did not give back a payment page.": "La société de paiement n’a pas renvoyé de page de paiement.",
    "The payment key could not be read. A manager should paste it again under Settings.": "La clé de paiement n’a pas pu être lue. Un responsable doit la recoller dans Paramètres.",
}
