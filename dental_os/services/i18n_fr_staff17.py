"""French for version 0.25: DICOM X-rays in the viewer, and the imaging bridge."""

STAFF17 = {
    "Add the X-ray computer below.": "Ajoutez l’ordinateur de radiographie ci-dessous.",
    "Agent last seen": "Agent vu pour la dernière fois le",
    "Agent not seen yet": "Agent pas encore vu",
    "Agent running": "Agent en marche",
    "DICOM file": "Fichier DICOM",
    "DICOM files that carry it are filed on this patient whenever they arrive.": "Les fichiers DICOM qui le portent sont classés sur ce patient dès leur arrivée.",
    "For example Surgery 1": "Par exemple Salle 1",
    "Images received:": "Images reçues :",
    "Imaging bridge": "Passerelle d’imagerie",
    "It can also open the sensor software for the waiting patient, if that software accepts a patient on its command line. The settings file explains how.":
        "Il peut aussi ouvrir le logiciel du capteur pour le patient en attente, si ce logiciel accepte un patient en ligne de commande. Le fichier de réglages explique comment.",
    "Make a read and write API key under Tools, API and webhooks.": "Créez une clé d’API en lecture et écriture dans Outils, API et webhooks.",
    "Measurements are in millimetres from the file's pixel size.": "Les mesures sont en millimètres d’après la taille des pixels du fichier.",
    "On the X-ray computer, install Python 3 and copy tools/imaging_agent.py from this system's files.": "Sur l’ordinateur de radiographie, installez Python 3 et copiez tools/imaging_agent.py depuis les fichiers de ce système.",
    "Patient ID to type in the sensor software:": "Identifiant patient à saisir dans le logiciel du capteur :",
    "Run it once with --setup to write its settings file, then run it with no options. It needs no other software.": "Lancez-le une fois avec --setup pour écrire son fichier de réglages, puis lancez-le sans option. Il n’a besoin d’aucun autre logiciel.",
    "Send X-rays from the sensor computer straight to the right patient.": "Envoyez les radios de l’ordinateur du capteur directement au bon patient.",
    "Take X-rays now": "Prendre des radios maintenant",
    "The imaging agent is a small program for the computer the X-ray sensor is plugged into. It watches the folder where the sensor software saves images and files each new image on the patient staff picked with Take X-rays now. It works with any sensor software that saves PNG, JPEG or DICOM files into a folder.":
        "L’agent d’imagerie est un petit programme pour l’ordinateur où est branché le capteur de radiographie. Il surveille le dossier où le logiciel du capteur enregistre les images et classe chaque nouvelle image sur le patient choisi avec Prendre des radios maintenant. Il fonctionne avec tout logiciel de capteur qui enregistre des fichiers PNG, JPEG ou DICOM dans un dossier.",
    "The name inside this X-ray file does not match this patient. Check it was filed on the right record.": "Le nom inscrit dans ce fichier de radio ne correspond pas à ce patient. Vérifiez qu’il a été classé dans le bon dossier.",
    "X-ray computer": "Ordinateur de radiographie",
    "X-ray computers": "Ordinateurs de radiographie",
    "is sending its new images to this patient.": "envoie ses nouvelles images à ce patient.",
    "no name in the file": "pas de nom dans le fichier",
}

MESSAGES17 = {
    "Name the X-ray computer with letters and digits, for example Surgery 1.": "Nommez l’ordinateur de radiographie avec des lettres et des chiffres, par exemple Salle 1.",
    "Pick one of the clinic's X-ray computers.": "Choisissez l’un des ordinateurs de radiographie du cabinet.",
    "That X-ray session was not found.": "Cette séance de radiographie est introuvable.",
    "The image data is not valid base64.": "Les données de l’image ne sont pas en base64 valide.",
    "No patient is waiting for X-rays on this computer. Press Take X-rays now on the patient first.": "Aucun patient n’attend de radios sur cet ordinateur. Appuyez d’abord sur Prendre des radios maintenant sur le patient.",
    "The X-ray computer is ready for this patient. New images from it are filed here for the next 30 minutes.": "L’ordinateur de radiographie est prêt pour ce patient. Ses nouvelles images sont classées ici pendant les 30 prochaines minutes.",
    "X-ray session finished.": "Séance de radiographie terminée.",
}
