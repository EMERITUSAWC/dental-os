"""French for version 0.52: diagnoses on claims, covered treatments, monthly batches and the claim form.
The ICD-10 titles stay in English: the official French edition (CIM-10) should be taken from its publisher, not translated here."""

STAFF27 = {'Add the NHIS': 'Ajouter le NHIS',
 'Batches': 'Lots',
 'Being put together': 'En préparation',
 'Claim form': 'Formulaire de demande de remboursement',
 'Claim form to print': 'Formulaire de demande à imprimer',
 'Claims for': 'Demandes de',
 'Diagnoses': 'Diagnostics',
 'Diagnosis': 'Diagnostic',
 'Diagnosis (ICD-10)': 'Diagnostic (CIM-10)',
 'Draft claims for that month': 'Demandes en brouillon pour ce mois',
 'Every claim line needs a diagnosis': 'Chaque ligne de demande doit avoir un diagnostic',
 'Mark the batch as sent': 'Marquer le lot comme envoyé',
 'Monthly batches': 'Lots mensuels',
 'Monthly claim batches': 'Lots mensuels de demandes',
 "Patient's signature": 'Signature du patient',
 'Payer and member': 'Payeur et adhérent',
 "Payer's reference (optional)": 'Référence du payeur (facultatif)',
 'Print the summary': 'Imprimer le récapitulatif',
 'Put them in a batch': 'Les mettre dans un lot',
 'Signature and stamp': 'Signature et cachet',
 'Signed for the clinic': 'Signé pour le cabinet',
 'Spreadsheet (CSV)': 'Tableur (CSV)',
 'Take out': 'Retirer',
 "The NHIA says the NHIS covers, for oral health: pain relief (incision and drainage, tooth extraction and temporary relief) and dental restoration (simple amalgam fillings and temporary dressing). Dentures are not covered. Check the NHIA's current benefits list.": 'Selon '
                                                                                                                                                                                                                                                                          'la '
                                                                                                                                                                                                                                                                          'NHIA, '
                                                                                                                                                                                                                                                                          'le '
                                                                                                                                                                                                                                                                          'NHIS '
                                                                                                                                                                                                                                                                          'couvre, '
                                                                                                                                                                                                                                                                          'pour '
                                                                                                                                                                                                                                                                          'la '
                                                                                                                                                                                                                                                                          'santé '
                                                                                                                                                                                                                                                                          'bucco-dentaire '
                                                                                                                                                                                                                                                                          ': '
                                                                                                                                                                                                                                                                          'le '
                                                                                                                                                                                                                                                                          'soulagement '
                                                                                                                                                                                                                                                                          'de '
                                                                                                                                                                                                                                                                          'la '
                                                                                                                                                                                                                                                                          'douleur '
                                                                                                                                                                                                                                                                          '(incision '
                                                                                                                                                                                                                                                                          'et '
                                                                                                                                                                                                                                                                          'drainage, '
                                                                                                                                                                                                                                                                          'extraction '
                                                                                                                                                                                                                                                                          'dentaire '
                                                                                                                                                                                                                                                                          'et '
                                                                                                                                                                                                                                                                          'soulagement '
                                                                                                                                                                                                                                                                          'temporaire) '
                                                                                                                                                                                                                                                                          'et '
                                                                                                                                                                                                                                                                          'la '
                                                                                                                                                                                                                                                                          'restauration '
                                                                                                                                                                                                                                                                          'dentaire '
                                                                                                                                                                                                                                                                          '(obturations '
                                                                                                                                                                                                                                                                          'simples '
                                                                                                                                                                                                                                                                          'à '
                                                                                                                                                                                                                                                                          'l’amalgame '
                                                                                                                                                                                                                                                                          'et '
                                                                                                                                                                                                                                                                          'pansement '
                                                                                                                                                                                                                                                                          'temporaire). '
                                                                                                                                                                                                                                                                          'Les '
                                                                                                                                                                                                                                                                          'prothèses '
                                                                                                                                                                                                                                                                          'dentaires '
                                                                                                                                                                                                                                                                          'ne '
                                                                                                                                                                                                                                                                          'sont '
                                                                                                                                                                                                                                                                          'pas '
                                                                                                                                                                                                                                                                          'couvertes. '
                                                                                                                                                                                                                                                                          'Vérifiez '
                                                                                                                                                                                                                                                                          'la '
                                                                                                                                                                                                                                                                          'liste '
                                                                                                                                                                                                                                                                          'actuelle '
                                                                                                                                                                                                                                                                          'des '
                                                                                                                                                                                                                                                                          'prestations '
                                                                                                                                                                                                                                                                          'de '
                                                                                                                                                                                                                                                                          'la '
                                                                                                                                                                                                                                                                          'NHIA.',
 "The starter list is the dental block of the WHO's ICD-10, K00 to K14. Add other codes the clinic uses.": 'La liste de départ est le bloc dentaire de la '
                                                                                                           'CIM-10 de l’OMS, K00 à K14. Ajoutez les autres '
                                                                                                           'codes utilisés par le cabinet.',
 "Tick the treatments this payer pays for. If none is ticked, the payer's usual shares apply to everything. Treatment not ticked is estimated at nothing, with a note.": 'Cochez '
                                                                                                                                                                         'les '
                                                                                                                                                                         'soins '
                                                                                                                                                                         'que '
                                                                                                                                                                         'ce '
                                                                                                                                                                         'payeur '
                                                                                                                                                                         'prend '
                                                                                                                                                                         'en '
                                                                                                                                                                         'charge. '
                                                                                                                                                                         'Si '
                                                                                                                                                                         'rien '
                                                                                                                                                                         'n’est '
                                                                                                                                                                         'coché, '
                                                                                                                                                                         'les '
                                                                                                                                                                         'parts '
                                                                                                                                                                         'habituelles '
                                                                                                                                                                         'du '
                                                                                                                                                                         'payeur '
                                                                                                                                                                         's’appliquent '
                                                                                                                                                                         'à '
                                                                                                                                                                         'tout. '
                                                                                                                                                                         'Les '
                                                                                                                                                                         'soins '
                                                                                                                                                                         'non '
                                                                                                                                                                         'cochés '
                                                                                                                                                                         'sont '
                                                                                                                                                                         'estimés '
                                                                                                                                                                         'à '
                                                                                                                                                                         'zéro, '
                                                                                                                                                                         'avec '
                                                                                                                                                                         'une '
                                                                                                                                                                         'note.',
 'What it covers': 'Ce qu’il couvre',
 'added by the clinic': 'ajouté par le cabinet',
 'claims': 'demandes'}

# Plain messages and errors raised in the code (not in templates).
MESSAGES27 = {'An ICD-10 code looks like K02.1: a letter, two digits, and maybe a point and one or two more.': 'Un code CIM-10 ressemble à K02.1 : une lettre, deux '
                                                                                                  'chiffres, et éventuellement un point suivi d’un ou deux '
                                                                                                  'chiffres.',
 'Diagnosis saved.': 'Diagnostic enregistré.',
 'Give the diagnosis its title.': 'Donnez son intitulé au diagnostic.',
 'Marked as sent.': 'Marqué comme envoyé.',
 'Not covered by this payer.': 'Non couvert par ce payeur.',
 'Only a batch being put together can be changed.': 'Seul un lot en préparation peut être modifié.',
 'Only a batch being put together can be marked sent.': 'Seul un lot en préparation peut être marqué comme envoyé.',
 'Pick a diagnosis from the list, or add the code to the list first.': 'Choisissez un diagnostic dans la liste, ou ajoutez d’abord le code à la liste.',
 'Pick a month.': 'Choisissez un mois.',
 'Taken out of the batch.': 'Retiré du lot.',
 'That claim is not in this batch.': 'Cette demande n’est pas dans ce lot.',
 'That code is already on the list.': 'Ce code est déjà dans la liste.',
 'That payer was not found.': 'Ce payeur est introuvable.',
 'The NHIS is already on the payer list.': 'Le NHIS est déjà dans la liste des payeurs.',
 'The NHIS is on the payer list. Now tick the treatments it covers.': 'Le NHIS est dans la liste des payeurs. Cochez maintenant les soins qu’il couvre.',
 'There are no draft claims for that payer and month.': 'Il n’y a pas de demande en brouillon pour ce payeur et ce mois.',
 'This payer needs a diagnosis on every line. Add it on the treatment list first.': 'Ce payeur exige un diagnostic sur chaque ligne. Ajoutez-le d’abord dans '
                                                                                    'la liste des soins.',
 'This payer needs a diagnosis on every line. Add the missing ones first.': 'Ce payeur exige un diagnostic sur chaque ligne. Ajoutez d’abord ceux qui '
                                                                            'manquent.'}
