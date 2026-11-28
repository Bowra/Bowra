# Connecter un service MCP personnel

Un service MCP distant permet à votre Agent privé d’utiliser des outils externes. La connexion est visible uniquement par vous. Les propriétaires et administrateurs d’équipe ne peuvent ni la consulter, ni la configurer, ni approuver ses appels.

## Ajouter une connexion et choisir les outils

1. Ouvrez **Centre des capacités → Mes connexions** (`/connections/`).
2. Saisissez un nom et l’adresse MCP distante. Choisissez sans authentification, jeton Bearer ou clé API ; vous pouvez préciser le nom d’en-tête exigé par le service pour la clé.
3. Choisissez **Connecter et découvrir les outils**, sélectionnez les outils nécessaires et enregistrez.

Les identifiants enregistrés ne sont pas relisibles. Laissez les champs vides pour les conserver lors d’une modification ; fournissez de nouveaux identifiants si vous changez la méthode. Bowra prend en charge MCP distant en HTTP(S), pas les commandes locales. Le cloud exige HTTPS public ; les déploiements internes acceptent les adresses internes. Utilisez des services de confiance et des identifiants aux droits minimaux.

## Vous authentifier sur un service avec votre propre compte

Certains services MCP ne fournissent pas de jeton et demandent de vous authentifier avec votre propre compte, par exemple Notion, Linear, Sentry ou n8n. Choisissez **Se connecter au service** sous **Authentification**. Si vous laissez **Aucune authentification** et que le service demande une authentification, la boîte de dialogue bascule sur cette option et indique où vous vous authentifierez.

1. Saisissez un nom et l’adresse MCP distante, puis choisissez **Continuer vers la connexion**.
2. Authentifiez-vous sur le service et approuvez l’accès demandé. Certains services permettent de restreindre ce que Bowhead peut faire (par exemple en lecture seule) ; les outils disponibles ensuite suivent ce choix.
3. De retour dans **Mes connexions**, sélectionnez les outils nécessaires et enregistrez. La connexion indique où vous vous êtes authentifié.

Bowhead conserve votre authentification pour cette seule connexion et la renouvelle si nécessaire ; elle survit aux redémarrages et n’est montrée à personne. Si le service met fin à l’authentification (par exemple après une longue inactivité, ou parce que l’accès de Bowhead y a été retiré), la connexion indique que votre authentification a pris fin : choisissez **S’authentifier de nouveau**. Vos sélections d’outils et associations d’Agents sont conservées ; les outils dont la définition a changé doivent être sélectionnés de nouveau. D’ici là, vos Agents continuent avec leurs autres outils, et un appel aux outils de cette connexion n’est pas envoyé : l’Agent vous demande de vous authentifier de nouveau.

Une authentification annulée ou inachevée affiche **Authentification non terminée** avec **Reprendre l’authentification**. Pour une connexion authentifiée, seul le nom peut être modifié ; pour une autre adresse, ajoutez une nouvelle connexion. À la suppression, Bowhead oublie votre authentification ; pour retirer aussi l’accès de Bowhead au service, supprimez-le des applications connectées du service.

Certains services n’acceptent que les applications enregistrées par leur administrateur (par exemple GitHub, Google et Microsoft). Bowhead ne peut pas encore s’y authentifier ; utilisez un jeton personnel ou une clé d’API si le service en propose.

| Situation                                                                       | Où se connecter                                           | Compte vu par le service                                      |
| ------------------------------------------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------- |
| L’équipe partage un compte de service géré par un administrateur                | Centre de capacités → Outils → Gérer les sources d’outils | L’authentification de l’administrateur, partagée par l’équipe |
| Chaque personne doit agir en son nom et le service propose une authentification | Mes connexions → Se connecter au service                  | Le vôtre                                                      |
| Le service vous fournit un jeton personnel ou une clé d’API                     | Mes connexions → Bearer Token / API Key                   | Le vôtre                                                      |
| Slack                                                                           | Mes connexions → Connecter votre Slack                    | Le vôtre, via l’application Slack de l’équipe                 |

## Connecter votre Slack

Lorsque Slack est activé par l’administrateur et que le déploiement le permet, **Connecter mon Slack** apparaît dans **Mes connexions**. Autorisez avec votre propre compte Slack. La connexion et le jeton vous appartiennent uniquement ; propriétaires et administrateurs ne peuvent ni les consulter ni les utiliser.

Au retour, la page indique la réussite ou explique quoi vérifier et relancer. Si l’échec persiste, demandez à un administrateur de terminer **Activer le MCP Slack personnel** dans **Paramètres de l’équipe → Intégrations → Slack**. L’URL de rappel OAuth personnelle et les autorisations User Token Scopes nécessaires doivent être renseignées. Slack peut demander une réinstallation après ajout d’autorisations.

Sélectionnez ensuite les outils Slack et associez-les à votre Agent privé. Cette connexion est en lecture seule : recherche et lecture de messages, canaux, fichiers et informations utilisateurs, sans envoi de messages, création de canaux ou écriture de Canvases.

## Associer à un Agent privé

Si aucun outil n’est disponible dans **Mes outils d’agent**, ajoutez une connexion ou activez les outils nécessaires dans une connexion existante. Développez ensuite l’agent à configurer.

Dans **Mes connexions → Outils d’Agent privé**, ou la zone d’outils de l’Agent, sélectionnez et enregistrez. L’Agent doit être votre version privée publiée dans l’équipe actuelle. Publiez de nouveau une version avant de resélectionner ses outils personnels. Les Agents d’équipe, l’assistant principal et ceux d’autres personnes ne reçoivent jamais votre connexion.

## Confirmer chaque appel

Les appels distants nécessitent par défaut votre confirmation. La demande apparaît dans la conversation sous l’étape en attente. Vérifiez Agent, connexion, outil, résumé de l’action et tous les paramètres, notamment les données quittant Bowra.

- **Autoriser cet appel** permet une exécution avec les paramètres actuels.
- **Toujours autoriser dans cette session** permet les appels suivants au même outil uniquement dans cette session ; l’autorisation expire à sa fin, à l’actualisation ou à la désactivation de la connexion.
- **Refuser et passer** n’exécute pas l’appel.
- Sans décision, l’appel expire après trois minutes.

Après votre décision, la demande reste dans la conversation sous forme d’une ligne indiquant votre décision. Si vous êtes sur une autre page, un avis mène à la conversation et le titre de l’onglet indique le nombre d’actions en attente.

Un service externe peut produire des changements irréversibles. Posséder une connexion personnelle n’autorise pas l’envoi de données sensibles contraire à la politique de l’équipe.

## Réparer, désactiver ou supprimer

**Modifier la connexion / Actualiser les outils** répare l’authentification ou redécouvre les outils. Lorsque le service refuse les identifiants d’une connexion, celle-ci indique que l’authentification a expiré ; vos Agents continuent avec leurs autres outils, et un appel aux outils de cette connexion n’est pas envoyé : l’Agent vous demande d’abord de mettre à jour les identifiants. L’actualisation efface les sélections et nécessite de confirmer de nouveau les associations concernées. Désactiver ou supprimer bloque les appels futurs ; un appel commencé peut se terminer. La suppression efface les identifiants et conserve seulement des traces minimales. Avant confirmation, la page liste les Agents privés encore associés. Si le résultat externe est inconnu, vérifiez-le dans le service avant d’approuver de nouveau la même action.
