# Envoyer des e-mails

Après l’enregistrement d’une configuration propre à votre équipe dans **Paramètres de l’équipe → Service e-mail**, vous pouvez demander à Bowra d’envoyer un e-mail en texte brut depuis une conversation. Chaque e-mail nécessite votre confirmation personnelle sur la page. Ce guide explique d’abord l’envoi pour les membres, puis la configuration pour les propriétaires et administrateurs d’équipe.

## Envoyer depuis une conversation

Indiquez le destinataire, l’objet et le corps, puis demandez l’envoi. Par exemple : « Envoyez un e-mail à customer@example.com, objet Point sur le projet, avec le contenu suivant : … ». Vous pouvez aussi demander de transformer les résultats de l’analyse actuelle en e-mail.

### Confirmer avant l’envoi

La fenêtre **Confirmer l’e-mail** affiche tous les destinataires, l’objet et le corps. Vérifiez-les et choisissez **Envoyer**. **Ne pas envoyer**, ou l’absence de réponse pendant le délai affiché, empêche l’envoi ; Bowra l’indique dans la conversation. Un e-mail envoyé ne peut pas être rappelé.

Seule la personne ayant commencé la conversation peut voir et confirmer l’e-mail. Les tâches planifiées, Feishu et les autres canaux sans confirmation sur la page web n’envoient jamais ces e-mails.

### Chiffres dans l’e-mail

Les montants, pourcentages et autres chiffres doivent provenir des données réellement récupérées dans la conversation actuelle ou de vos propres messages. Si un chiffre du brouillon n’est pas retrouvé dans ces données, la confirmation n’apparaît pas : Bowra corrige d’abord les chiffres, puis demande votre confirmation.

Pour envoyer des résultats d’une conversation antérieure, demandez de récupérer de nouveau les données pertinentes dans la conversation actuelle avant l’envoi.

L’e-mail contient uniquement du texte brut. Aucun canevas, rapport ou autre fichier n’est joint.

### Sans service e-mail configuré

Sans configuration propre à l’équipe, Bowra indique que le service est indisponible. C’est aussi le cas si **Configuration d’envoi effective** indique l’héritage du service par défaut de l’instance. Ce service par défaut sert uniquement aux e-mails système : invitations, vérification d’adresse et réinitialisation des mots de passe. Demandez au propriétaire ou à un administrateur de configurer le service ci-dessous.

## Configurer le service e-mail de l’équipe

Les propriétaires et administrateurs peuvent configurer SMTP dans **Paramètres de l’équipe → Service e-mail**. Les invitations, vérifications, réinitialisations et notifications utilisent ensuite en priorité cette configuration ; les membres peuvent envoyer des e-mails depuis les conversations.

### Étapes

1. Ouvrez la page. **Configuration d’envoi effective** indique si l’équipe possède sa configuration, hérite de celle de l’instance ou n’en a aucune.
2. Dans **Connexion SMTP**, renseignez le serveur, le port et le chiffrement fournis par votre service e-mail. Les ports courants sont 465 pour SSL/TLS et 587 ou 2525 pour STARTTLS.
3. Dans **Authentification**, saisissez le nom d’utilisateur SMTP et le mot de passe SMTP ou d’application. La plupart des services nécessitent un mot de passe dédié, distinct de celui de connexion. Un mot de passe enregistré n’est jamais réaffiché ; laissez ce champ vide lors d’un nouvel enregistrement pour conserver le précédent.
4. Dans **Identité de l’expéditeur**, saisissez une adresse d’expédition autorisée par votre fournisseur et le nom affiché. Bowra ne fournit aucun domaine d’expédition par défaut ; une adresse autorisée est nécessaire pour envoyer un e-mail. Choisissez ensuite **Enregistrer**. **Enregistrer et tester** enregistre et envoie immédiatement un e-mail de test.
5. Choisissez **Envoyer un e-mail de test**. Il arrive dans votre propre boîte. Sa réception confirme le fonctionnement ; vérifiez aussi les courriers indésirables.

Si l’équipe n’a plus besoin de sa configuration, choisissez **Effacer la configuration**, affiché uniquement lorsqu’une configuration existe. Les e-mails système utilisent alors le service par défaut de l’instance, s’il existe, et l’envoi depuis les conversations devient indisponible.

### À savoir

- L’édition cloud SaaS utilise uniquement des serveurs accessibles depuis l’Internet public, sur les ports 25, 465, 587 ou 2525. Une adresse interne ou un autre port provoque un refus lié à la politique de sécurité ou au port non pris en charge. Utilisez l’adresse SMTP publique de votre fournisseur.
- Les déploiements privés peuvent utiliser les serveurs internes et d’autres ports.
- Seuls les propriétaires et administrateurs d’équipe voient et modifient cette configuration. Les autres membres ne voient pas cette entrée.
