# Gérer les modèles de l’équipe

Dans **Affectations des modèles → Base de connaissances → Nombre de résultats**, la valeur par défaut est **15 résultats**. Choisissez 5, 10, 15, 20, 30 ou 50. La modification ne reconstruit pas l’index. Davantage de résultats peut améliorer la couverture, mais utilise plus de contexte du modèle. Les contrôles d’accès et de pertinence peuvent réduire le nombre renvoyé. Les valeurs personnalisées existantes de 1 à 50 restent disponibles.

Les propriétaires et administrateurs gèrent les services de modèles dans **Paramètres de l’équipe → Modèles**. La page distingue connexions, affectations de production et preuves de vérification, puis capacités externes et état des index.

**Descriptions des sources** est une affectation facultative, désactivée par défaut. Un modèle de texte déclaré traite les nouvelles versions publiées et les demandes explicites. Enregistrer ne traite pas les anciens fichiers ; consulter les sources n’appelle aucun modèle. Retirer l’affectation arrête les nouvelles tâches ; les descriptions encore valides restent utilisables.

## Vérifier et enregistrer les connexions séparément

Après autorisation Bowra, l’éditeur de connexion affiche un plan de vérification du dialogue et des appels de fonctions. Vérifiez le nombre de requêtes et les frais possibles, puis choisissez Démarrer ; aucun appel fournisseur ne démarre automatiquement. Vous pouvez aussi enregistrer avant de vérifier. La barre fixe garde Enregistrer, Annuler et Vérifier accessibles ; les paramètres avancés sont repliés au départ.

Si la maintenance automatique des affectations est activée pour votre installation, les modèles adaptés dont la vérification est encore valide complètent les tâches non configurées après une modification de connexion ou une vérification. Les choix valides sont conservés. En cas de suppression, les affectations concernées sont remplacées par un modèle vérifié ou effacées. Les tâches facultatives désactivées explicitement restent désactivées ; les descriptions des sources le sont par défaut. Les modèles vectoriels ne changent jamais automatiquement et conservent la confirmation de réindexation. Sans modèle principal, les conversations sont temporairement indisponibles. Gérez séparément une clé Bowra abandonnée dans Bowra API Keys si nécessaire.

## Connexions et capacités des modèles

Ajoutez ou modifiez une connexion : adresse du fournisseur, authentification, paramètres requis, identifiants de modèles et capacités prises en charge. Enregistrez d’abord la connexion, puis **Planifier la vérification** pour examiner capacités, nombre de demandes et coût éventuel avant le lancement. La vérification contacte le fournisseur et peut consommer des crédits, mais n’enregistre pas automatiquement les changements de configuration.

Dans le catalogue, réglez les coefficients de crédit **Entrée**, **Entrée en cache** et **Sortie** pour chaque modèle. Ils valent tous 1 par défaut, y compris pour les modèles existants. Le coefficient de cache ne s’applique que si le fournisseur indique des jetons servis depuis le cache. Utilisez une valeur de 0 à 1 000 avec au plus six décimales. Chaque nouvel appel de modèle enregistre les coefficients en vigueur au moment de l’appel ; les appels précédents conservent leurs valeurs, même si la tâche continue. Consultez [les crédits et leur utilisation](credits.fr-FR.md) pour le calcul et le détail.

Pour permettre aux membres de choisir un Agent avec `@`, renseignez **Fenêtre de contexte (tokens)** pour les modèles de conversation ou d’appels d’outils. Le message indiquant que la fenêtre de contexte n’est pas définie signifie que cette valeur manque. Ouvrez la connexion de conversation, trouvez le modèle dans son catalogue, utilisez la valeur de la documentation officielle du fournisseur, enregistrez et terminez **Vérifier et appliquer**. Renseignez aussi les modèles susceptibles de prendre le relais. Ne devinez pas : un champ vide signifie une fenêtre inconnue et empêche ce modèle de prendre une tâche avec référence à un Agent. Les membres peuvent ensuite revenir à leur conversation et renvoyer le brouillon conservé.

Changer adresse, identifiants, modèle ou paramètres de capacités peut invalider une vérification antérieure. Avant de retirer un modèle ou une connexion utilisée, remplacez ses affectations en production ou dans les capacités externes.

## Capacité de sortie et limites des requêtes

**Capacité de sortie du modèle (tokens)** affiche la capacité et son origine. Les requêtes automatiques autorisent jusqu’à 256 000 tokens, ou 32 000 si la capacité est inconnue ; une capacité plus faible et la place nécessaire à l’entrée réduisent la limite réelle. Ce sont des maxima, pas une prévision de consommation. Pour un identifiant de déploiement personnalisé, saisissez la capacité documentée. **Utiliser la capacité héritée** supprime votre valeur par modèle. Une autre modification n’enregistre pas une valeur déduite.

La limite visible indique les valeurs automatiques, fixes, plafonnées ou non envoyées par capacité. Les paramètres de requête contrôlent chaque appel ; déclarer la capacité du modèle ne fixe pas sa limite de requête. Une limite omise laisse le maximum du serveur inconnu. Les limites fixes sont conservées et doivent respecter la capacité et le contexte.

Si la sortie est tronquée, Bowra demande jusqu’à deux fois une action plus petite et complète. Le lot incomplet d’appels d’outils n’est pas exécuté et les écritures précédemment réussies ne sont pas répétées. Le statut en cours affiche la correction. Si elle échoue encore, les résultats précédents restent disponibles : demandez un rapport plus court ou faites vérifier les limites. Les limites de temps et d’étapes continuent de s’appliquer.

## Affectations de production

L’onglet **Affectation des modèles** regroupe les conversations et les tâches de connaissances. Chaque ligne affiche le modèle effectif, sa règle de sélection et les preuves disponibles. Les tâches non configurées ou inutilisées restent visibles. Modifiez les affectations pour choisir d’abord le modèle principal et l’ordre des modèles de secours. Dépliez au besoin les tâches légères, l’évaluation, les connaissances facultatives et le nombre de résultats de recherche.

**Afficher les détails de vérification** ouvre les preuves, les dates, les dernières tentatives et les vérifications individuelles sans appeler le service. Une vérification peut entraîner des frais et ne sauvegarde pas la configuration. Le récapitulatif des affectations enregistrées et leur vérification restent sur la page parente. Avant l’application, la revue montre les anciennes et nouvelles affectations ainsi que les preuves pertinentes. Seule la confirmation sauvegarde ; un message de réussite apparaît ensuite. Le bouton **Enregistrer les tentatives** conserve une sauvegarde séparée.

**Modifier les affectations** définit les modèles principal, de secours, léger et d’évaluation, ainsi que ceux des vecteurs de connaissances, du reclassement, de la vision documentaire et de l’extraction des lignes. Seuls les modèles de connexions enregistrées sont disponibles. Vérifiez les capacités nécessaires à chaque affectation et confirmez les preuves présentées.

**Afficher les détails de vérification** présente les preuves pour chaque usage. Les éléments nécessitant le plus d’attention apparaissent d’abord. **Afficher N éléments de vérification supplémentaires** développe la liste ; **Afficher moins d’éléments** la réduit. Chaque entrée présente usage, modèle, capacités utilisées, preuves existantes et dernière tentative. Pour relancer une entrée, choisissez **Vérifier**, puis **Démarrer**. **Vérifier les affectations actuelles** permet d’en choisir plusieurs. Les demandes vont au fournisseur. Ces résultats servent au diagnostic : une erreur réseau isolée ne prouve pas que la configuration est inutilisable.

**Retour à la vue d’ensemble** ouvre l’onglet des affectations de la page parente, même après un accès direct à l’éditeur. L’onglet sélectionné est conservé au rechargement.

Pendant la modification, **Vérifier les affectations du brouillon** contrôle les modèles choisis. Confirmez le nombre de requêtes et les frais possibles avant de démarrer. La vérification n’applique pas les affectations et conserve le brouillon. Les connexions de même nom affichent leur identifiant pour distinguer les choix avant et après modification.

## Nouvelles tentatives automatiques en cas d’échec

Dans **Affectations des modèles**, **Comportement du modèle** contient un seul réglage : **Nouvelles tentatives automatiques en cas d’échec**. Lorsqu’un appel échoue avant que le modèle ait commencé à répondre (délai dépassé, échec de connexion ou erreur temporaire du service), Bowra relance l’appel jusqu’à ce nombre de fois. La valeur par défaut est 3 et la plage autorisée va de 0 à 5 ; indiquez 0 pour désactiver les nouvelles tentatives. Choisissez **Enregistrer** à côté du champ ; ce réglage s’enregistre seul et ne modifie pas les affectations.

Une nouvelle tentative n’a lieu que si le modèle n’a pas encore répondu : une réponse n’est donc jamais envoyée deux fois. Chaque tentative compte dans la limite de temps du tour ; s’il ne reste pas assez de temps, Bowra arrête et signale l’échec. Pendant une nouvelle tentative, l’état d’exécution de la conversation affiche par exemple « Le modèle n’a pas répondu à temps, nouvelle tentative (1/3) ». Si la page indique que la configuration a changé, rechargez-la puis définissez à nouveau la valeur.

## Capacités externes et index

Configurez les modèles de recherche, génération d’images et autres capacités externes dans leur zone dédiée. Vérifiez les index avant d’utiliser un service de connaissances. Une connexion enregistrée n’est pas nécessairement affectée à la production ou prête à être utilisée.

Enregistrez avant de quitter, puis rouvrez la rubrique pour vérifier les valeurs. Le choix de modèles dans une conversation se limite aux options proposées par l’équipe.

## Si la vérification ne reçoit aucune réponse utilisable

HTTP 200 signifie que le service a répondu, mais pas forcément que le modèle a fourni une réponse utilisable. Un contenu vide, de la réflexion sans réponse ou un format incompatible peuvent produire ce résultat. Il ne s’agit donc pas nécessairement d’un problème de connexion.

1. Choisissez **Voir les détails de la réponse**. Les propriétaires et administrateurs de l’équipe peuvent consulter le modèle retourné, la raison de l’arrêt, les types de contenu reconnus, les comptes de tokens valides, les principaux paramètres du test et la réponse du service.
2. Si la limite de longueur est atteinte, vérifiez la limite de sortie et les paramètres de réflexion avant de relancer la vérification. Si le service termine normalement sans réponse, demandez à son administrateur de vérifier le modèle et le format retourné. Les informations absentes sont indiquées par **Non indiqué par le service** ; la page ne suppose pas de cause.
3. Choisissez **Copier les détails du diagnostic** avant d’enregistrer la connexion, de fermer le résultat ou d’actualiser la page. Les résultats terminés ne se rouvrent pas automatiquement. Si la copie automatique est indisponible, utilisez la boîte de dialogue de copie manuelle.

Les paramètres affichés concernent principalement le modèle, la longueur de sortie et la réflexion, lorsqu’ils sont présents ; ils n’incluent ni les identifiants ni la requête complète. Le résumé de consommation contient les comptes numériques de tokens reconnus. Les autres champs restent dans l’extrait de réponse. Le JSON est mis en forme pour la lecture ; les espaces et sauts de ligne à l’intérieur des chaînes sont conservés. Les identifiants connus sont masqués, y compris leurs répétitions dans d’autres champs ou événements du flux. Vérifiez néanmoins le contenu avant de le partager. Après masquage, seuls les 64 premiers Kio de la réponse sont affichés et copiés.

Relancez la vérification pour une ancienne tentative ou des détails non enregistrés, après avoir vérifié le nombre de requêtes et leur coût éventuel. Consulter ou copier les détails existants n’appelle pas le modèle et n’enregistre pas de modification de configuration.
