# Connecter un canal de messagerie d’équipe

Les propriétaires et administrateurs ouvrent le canal voulu dans **Paramètres de l’équipe → Canaux de messagerie**. Suivez les étapes affichées pour le déploiement. Si le canal exige un rappel, configurez une adresse accessible au service externe. [Configurer les adresses d’accès public](public-entries.fr-FR.md)

Les **Canaux connectés** sont activés et opérationnels. Dans **Canaux disponibles**, commencez ou poursuivez une configuration, ou vérifiez l’état d’un canal activé. Les canaux **Bientôt disponibles** ne sont pas encore configurables.

- **Feishu.** Enregistrez les identifiants de l’application et choisissez le mode de réception des événements proposé. Suivez les étapes de la console et les vérifications de publication. Après un premier message au bot, chaque membre confirme son compte Bowhead depuis la carte reçue.
- **Slack.** Configurez l’application Slack, enregistrez le rappel du bot et installez-le dans l’espace Slack voulu. Testez un message direct ou une mention dans un fil. L’autorisation MCP personnelle de Slack suit une procédure distincte.
- **Microsoft Teams.** Configurez le bot Azure, enregistrez l’adresse HTTPS externe complète avec le préfixe de déploiement et publiez l’application pour les conversations privées du locataire.

Testez avec un compte membre autorisé et consultez le statut du canal. Les messages sont associés à l’équipe Bowhead configurée et respectent les droits du membre.

Pour les questions sur les données, vérifiez que votre compte associé a accès à l’espace de travail Datarc concerné. Si une demande de précision vous renvoie vers Bowhead Web, ouvrez la conversation d’origine pour continuer.

## Utiliser la conversation privée Teams

1. Envoyez un message au bot et connectez-vous à Teams si nécessaire. Ouvrez **Associer Teams**, connectez-vous au compte Bowhead correspondant à votre adresse professionnelle et confirmez. Le lien expire dans dix minutes. Les membres précédemment associés par e-mail confirment une fois. Renvoyez ensuite votre question dans Teams.
2. Choisissez l’espace de votre question. Envoyez `/space` pour changer d’espace avant la question suivante. Chaque espace conserve sa conversation. Revenez dans l’espace précédent pour recevoir un résultat qui y est en attente.
3. Sélectionnez les réponses ou saisissez le texte demandé sur la carte. **Envoyer et continuer** reprend la tâche initiale. Si vous avez déjà répondu sur le Web, la première réponse est conservée. Quand la carte renvoie à Bowhead Web, ouvrez-y la conversation d’origine.
