# Outils tiers intégrés

Les propriétaires et administrateurs d’équipe peuvent connecter les services pris en charge dans **Paramètres de l’équipe → Sources d’outils**. Tavily, Datarc et Power BI sont disponibles. Leur icône apparaît dans le sélecteur et la liste des outils configurés pour les distinguer.

## Tavily

Choisissez **＋ Ajouter un outil tiers intégré**, sélectionnez Tavily, collez la clé API, puis **Vérifier et enregistrer**. Bowra vérifie la clé avant de la conserver chiffrée. Vous pouvez ensuite la modifier ou activer et désactiver l’outil.

Les Agents d’équipe disposant de la recherche web peuvent utiliser Tavily lorsqu’il est activé. Sa désactivation empêche les nouvelles recherches de l’utiliser. La clé API n’est jamais affichée aux membres ni aux Agents.

### Recherche web

Précisez le périmètre dans votre question : « seulement les actualités de la semaine passée », « uniquement les sites gouvernementaux », « exclure ce site » ou « rechercher plus en profondeur ». L’Agent filtre les résultats ; les actualités incluent leur date de publication.

### Lire des pages et explorer des sites

Si ces capacités sont activées dans votre déploiement, des options comme **Lire plusieurs pages web** apparaissent sous **Ajouter un outil** d’un Agent. Tavily permet de lire plusieurs pages publiques ensemble, répertorier les pages d’un site avant de choisir lesquelles lire, ou suivre les liens pour récupérer plusieurs pages du même site. Exemples : « Lisez et résumez ces trois liens », « Quelles pages produits ce site propose-t-il ? », « Lisez toutes les instructions d’installation de cette documentation ». Une exploration récupère environ 20 pages par défaut, jusqu’à 100, peut prendre deux à trois minutes et consomme davantage de crédits. Pour quelques pages, fournir directement les liens est plus rapide.

Pour éviter d’envoyer des informations à des sites inconnus, l’Agent lit uniquement les liens fournis dans la conversation, issus de résultats de recherche ou répertoriés et récupérés sur le même site. Les liens vers d’autres sites sont listés sans être lus. La recherche approfondie reste également sur les sites que vous avez mentionnés et retire d’abord les adresses inconnues de la question. Les adresses internes et pages nécessitant une connexion ne relèvent pas de ces capacités ; l’Agent les ouvre directement.

### Recherche approfondie

Elle nécessite aussi l’activation de ces capacités. Pour une recherche externe systématique, demandez explicitement une recherche approfondie, par exemple sur les actualités publiques de l’expansion internationale des constructeurs chinois de véhicules électriques sur les six derniers mois. L’Agent transmet la demande au moteur Tavily et reçoit quelques minutes plus tard un rapport avec liens sources. La conversation indique l’étape en cours. Un échange peut lancer au maximum une recherche approfondie ; le mode le plus coûteux nécessite une demande explicite de recherche la plus détaillée et exhaustive possible.

### Activer les capacités

Lecture de pages, exploration et recherche approfondie s’activent séparément pour chaque Agent. Les Agents intégrés d’analyse de données et d’analyse marketing peuvent lire et explorer des sites par défaut. La recherche approfondie n’est disponible par défaut que pour l’analyse marketing. Pour vos Agents, ouvrez **Configurer → Ce qu’il peut utiliser → Ajouter… → Ajouter un outil** et sélectionnez **Lire plusieurs pages web**, **Lister les liens d’un site**, **Parcourir un site web** et **Effectuer une recherche approfondie**.

### Crédits

Chaque recherche, lecture, exploration ou recherche approfondie consomme les crédits du compte Tavily de l’équipe. Les recherches plus poussées et lectures approfondies consomment deux fois plus ; les explorations sont facturées par page et la recherche approfondie coûte nettement plus qu’une recherche ordinaire. Si les crédits sont épuisés ou la clé devient invalide, l’Agent indique que la recherche web est temporairement indisponible et propose de contacter un administrateur. Celui-ci doit recharger le compte ou remplacer la clé dans Tavily, puis la mettre à jour sur cette page. Une clé sans crédits peut être enregistrée et refonctionne après rechargement.

## Datarc

Choisissez **＋ Ajouter un outil tiers intégré → Datarc** et saisissez un nom de configuration, l’adresse du service et la clé de connexion. L’adresse doit être la racine du service, par exemple `https://openapi.datarc.cn`, sans chemin d’API particulier. Après vérification, la clé est conservée chiffrée et la liste affiche « Datarc - nom de configuration ».

Une équipe peut avoir plusieurs connexions attribuées séparément aux Agents, par exemple « Datarc - Finance » et « Datarc - Sales ». Renommer une connexion conserve les affectations. Désactiver ou supprimer une connexion ne fait pas basculer automatiquement les Agents vers une autre.

À la création ou modification, Bowra lit les autorisations API, les espaces de travail accessibles et l’expiration de la clé. La fenêtre les présente par autorisations, outils disponibles, espaces de travail et validité. La liste des espaces est repliée ; développez-la ou choisissez **Vérifier de nouveau** pour actualiser le périmètre. Une ancienne connexion ne fournit pas d’outils avant sa première vérification réussie, après un refus de la clé ou après expiration.

Certaines anciennes versions Datarc ne fournissent pas les autorisations de la clé. Bowra vérifie alors les espaces accessibles, affiche **Toutes les autorisations** et propose les trois capacités. Une requête reste soumise aux autorisations réelles dans Datarc. En cas de refus, vérifiez la clé et son expiration. Si l’adresse ne fournit pas l’API Datarc, utilisez bien la racine du service.

Selon les autorisations, la connexion fournit **Lister les espaces de travail**, **Examiner le schéma des données** et **Interroger les données**. Ces capacités sont incluses dans une connexion et ne nécessitent pas d’affectation individuelle à l’Agent.

## Power BI

Choisissez **＋ Ajouter un outil tiers intégré → Power BI**, nommez la connexion et renseignez l’identifiant du locataire Microsoft Entra, l’identifiant client et son secret. Vous pouvez indiquer l’espace de travail et le modèle sémantique ou laisser ces champs vides pour une sélection automatique parmi les modèles accessibles. Bowra vérifie l’accès aux requêtes avant l’enregistrement. Le secret est conservé chiffré et n’est jamais réaffiché. Plusieurs connexions sont possibles, notamment pour séparer finance et ventes.

Un administrateur de l’organisation doit enregistrer l’application dans Microsoft Entra, créer le secret, autoriser le groupe de sécurité du principal de service à utiliser l’API Power BI, ajouter ce principal à l’espace de travail et accorder l’autorisation de création (Build) sur le modèle sémantique. Si la vérification échoue, contrôlez locataire, espace, modèle et autorisation Build.

Chaque connexion possède ses modèles DAX. Les propriétaires et administrateurs peuvent les ajouter ou les retirer depuis sa carte. Désactiver ou supprimer une connexion ne redirige pas les Agents ; sa suppression supprime aussi ses modèles DAX.

Dans **Configurer → Ce qu’il peut utiliser** de l’Agent, sélectionnez sa connexion Power BI et enregistrez. Chaque connexion figure séparément ; l’Agent utilise uniquement celles qui lui sont affectées. Les anciennes connexions avec principal de service sont déplacées vers Sources d’outils. Une ancienne connexion avec autorisation déléguée doit être reconfigurée avec un principal de service, puis sélectionnée et enregistrée dans l’Agent. Power BI vérifie les autorisations du principal de service de l’équipe, pas celles de l’utilisateur individuel de la conversation.

## Affecter les connexions aux Agents

Ouvrez **Gestion des Agents → Créés par moi → Configurer → Skills et outils → Ajouter un outil**. Les outils d’équipe apparaissent sous forme de cartes de connexion :

- Le nom reprend celui de la configuration, par exemple « Datarc - Finance ».
- Une sélection ajoute toute la connexion.
- **Voir les capacités incluses** détaille les outils fournis.
- La recherche porte sur les noms de connexions et de capacités.
- **Gérer les connexions d’outils de l’équipe** ouvre la page correspondante des paramètres.

À l’enregistrement de l’Agent, Bowra conserve les outils précis d’après la dernière vérification enregistrée de la connexion. Le périmètre n’est ni revérifié ni élargi à chaque appel ; il est actualisé uniquement lorsque vous enregistrez de nouveau l’Agent. Des autorisations ajoutées ultérieurement à une clé externe ne s’ajoutent pas automatiquement aux Agents existants. Une API devenue indisponible renvoie une erreur lors de son appel.

Par exemple :

```text
Datarc - Finance
  Lister les espaces de travail, Examiner le schéma des données, Interroger les données

Datarc - Sales
  Lister les espaces de travail, Interroger les données
```

La page de l’Agent affiche deux cartes plutôt que cinq outils aux noms similaires. Chaque outil reste lié à sa connexion ; les adresses et clés ne sont jamais partagées entre les deux.

Pour les requêtes, le périmètre réel dépend de la clé, des autorisations d’appartenance de l’utilisateur Datarc et des outils enregistrés dans l’Agent. Un unique espace de travail accessible est choisi automatiquement ; avec plusieurs, l’Agent demande un choix. Si aucun compte Datarc n’est trouvé, vérifiez que votre e-mail de connexion Bowra existe dans cette instance Datarc et appartient à l’espace de travail concerné.

Les clés de connexion ne sont jamais affichées aux membres ou Agents. Un déploiement privé peut utiliser une adresse interne ; le SaaS accepte uniquement les adresses HTTPS publiques.
