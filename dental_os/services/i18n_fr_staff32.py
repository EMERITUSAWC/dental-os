"""French for version 0.68: the review from version 0.1 onwards."""

STAFF32 = {
    'Code from the authenticator app': 'Code de l’application d’authentification',
    'Signed notes and recorded payments cannot be changed': 'Les notes signées et les paiements enregistrés ne peuvent pas être modifiés',
}

MESSAGES32 = {
    'That transaction reference is already on a payment for this patient. Check the account before recording it again.': 'Cette référence de transaction figure déjà sur un paiement de ce patient. Vérifiez le compte avant de l’enregistrer à nouveau.',
    'The same payment was recorded a moment ago. If this really is a second payment, wait a minute and record it again.': 'Le même paiement vient d’être enregistré. S’il s’agit vraiment d’un second paiement, attendez une minute puis enregistrez-le à nouveau.',
    "A refund can't be more than the patient actually paid in. Part of this credit comes from a discount or a write-off, which is not money the clinic received.": 'Un remboursement ne peut pas dépasser ce que le patient a réellement payé. Une partie de ce crédit vient d’une remise ou d’une annulation de dette, qui n’est pas de l’argent reçu par la clinique.',
    'The write-off is more than is left to pay on this claim. Write off only the part the payer will not pay.': 'L’annulation dépasse ce qui reste à payer sur cette demande. N’annulez que la part que le payeur ne paiera pas.',
    'Messages cannot mention money: no amounts, currencies, balances or invoices. Anyone holding the phone can read them. Send the patient their private link instead.': 'Les messages ne peuvent pas parler d’argent\xa0: ni montants, ni devises, ni soldes, ni factures. Toute personne qui tient le téléphone peut les lire. Envoyez plutôt au patient son lien privé.',
    'One of the numbers is far too large. Check what was typed.': 'L’un des nombres est beaucoup trop grand. Vérifiez la saisie.',
    'Operators must use two-step login. On the server, run: flask operator-2fa --username YOUR-USERNAME, then sign in with the code.': 'Les opérateurs doivent utiliser la connexion en deux étapes. Sur le serveur, lancez\xa0: flask operator-2fa --username YOUR-USERNAME, puis connectez-vous avec le code.',
    'That username, password and code do not match.': 'Le nom d’utilisateur, le mot de passe et le code ne correspondent pas.',
    "The database refuses to change a signed note or a payment's amount, date or receipt.": 'La base refuse de modifier une note signée, ou le montant, la date ou le reçu d’un paiement.',
}
