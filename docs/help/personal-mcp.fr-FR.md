# Connecter un service MCP personnel

Un service MCP distant permet à votre Agent privé d’utiliser des outils externes. La connexion est visible uniquement par vous. Les propriétaires et administrateurs d’équipe ne peuvent ni la consulter, ni la configurer, ni approuver ses appels.

## Ajouter une connexion et choisir les outils

1. Ouvrez **Centre des capacités → Mes connexions** (`/connections/`).
2. Saisissez un nom et l’adresse MCP distante. Choisissez sans authentification, jeton Bearer ou clé API ; vous pouvez préciser le nom d’en-tête exigé par le service pour la clé.
3. Choisissez **Connecter et découvrir les outils**, sélectionnez les outils nécessaires et enregistrez.

Les identifiants enregistrés ne sont pas relisibles. Laissez les champs vides pour les conserver lors d’une modification ; fournissez de nouveaux identifiants si vous changez la méthode. Bowra prend en charge MCP distant en HTTP(S), pas les commandes locales. Le cloud exige HTTPS public ; les déploiements internes acceptent les adresses internes. Utilisez des services de confiance et des identifiants aux droits minimaux.

## Connecter votre Slack

Lorsque Slack est activé par l’administrateur et que le déploiement le permet, **Connecter mon Slack** apparaît dans **Mes connexions**. Autorisez avec votre propre compte Slack. La connexion et le jeton vous appartiennent uniquement ; propriétaires et administrateurs ne peuvent ni les consulter ni les utiliser.

Au retour, la page indique la réussite ou explique quoi vérifier et relancer. Si l’échec persiste, demandez à un administrateur de terminer **Activer le MCP Slack personnel** dans **Paramètres de l’équipe → Intégrations → Slack**. L’URL de rappel OAuth personnelle et les autorisations User Token Scopes nécessaires doivent être renseignées. Slack peut demander une réinstallation après ajout d’autorisations.

Sélectionnez ensuite les outils Slack et associez-les à votre Agent privé. Cette connexion est en lecture seule : recherche et lecture de messages, canaux, fichiers et informations utilisateurs, sans envoi de messages, création de canaux ou écriture de Canvases.

## Associer à un Agent privé

Dans **Mes connexions → Outils d’Agent privé**, ou la zone d’outils de l’Agent, sélectionnez et enregistrez. L’Agent doit être votre version privée publiée dans l’équipe actuelle. Publiez de nouveau une version avant de resélectionner ses outils personnels. Les Agents d’équipe, l’assistant principal et ceux d’autres personnes ne reçoivent jamais votre connexion.

## Confirmer chaque appel

Les appels distants nécessitent par défaut votre confirmation. Vérifiez Agent, connexion, outil, résumé de l’action et tous les paramètres, notamment les données quittant Bowra.

- **Autoriser cet appel** permet une exécution avec les paramètres actuels.
- **Toujours autoriser dans cette session** permet les appels suivants au même outil uniquement dans cette session ; l’autorisation expire à sa fin, à l’actualisation ou à la désactivation de la connexion.
- **Refuser et passer** n’exécute pas l’appel.
- Sans décision, l’appel expire après deux minutes.

Un service externe peut produire des changements irréversibles. Posséder une connexion personnelle n’autorise pas l’envoi de données sensibles contraire à la politique de l’équipe.

## Réparer, désactiver ou supprimer

**Modifier la connexion / Actualiser les outils** répare l’authentification ou redécouvre les outils. L’actualisation efface les sélections et nécessite de confirmer de nouveau les associations concernées. Désactiver ou supprimer bloque les appels futurs ; un appel commencé peut se terminer. La suppression efface les identifiants et conserve seulement des traces minimales. Avant confirmation, la page liste les Agents privés encore associés. Si le résultat externe est inconnu, vérifiez-le dans le service avant d’approuver de nouveau la même action.
