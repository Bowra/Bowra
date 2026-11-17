# Configurer les adresses d’accès public

Les propriétaires et administrateurs ouvrent **Paramètres de l’équipe → Accès public** pour gérer les adresses utilisées pour les liens, invitations et rappels.

1. Ajoutez une entrée avec le protocole, l’hôte, le port éventuel et le préfixe réel du déploiement, par exemple /bowhead. Utilisez une adresse accessible aux destinataires et services externes, sans paramètres de requête ni fragment.
2. Ouvrez le lien de vérification à usage unique dans un navigateur qui peut atteindre l’adresse. Vérifiez qu’il mène à cette instance Bowhead. Si le lien expire, générez-en un nouveau et contrôlez que l’entrée est active.
3. Choisissez l’adresse par défaut pour les liens d’équipe générés en arrière-plan. Vérifiez l’adresse de remplacement avant de désactiver l’ancienne. Vérifiez aussi les invitations et rappels liés à l’ancienne adresse et actualisez-les si nécessaire.

En cas d’échec, vérifiez le domaine, le proxy et le préfixe complet depuis le réseau des destinataires. La connexion ordinaire suit l’adresse ouverte dans le navigateur. Les adresses inscrites servent à générer des liens et à configurer les rappels.
