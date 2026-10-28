# Gérer les modèles de l’équipe

Dans **Affectations des modèles → Base de connaissances → Nombre de résultats**, la valeur par défaut est **15 résultats**. Choisissez 5, 10, 15, 20, 30 ou 50. La modification ne reconstruit pas l’index. Davantage de résultats peut améliorer la couverture, mais utilise plus de contexte du modèle. Les contrôles d’accès et de pertinence peuvent réduire le nombre renvoyé. Les valeurs personnalisées existantes de 1 à 50 restent disponibles.

Les propriétaires et administrateurs gèrent les services de modèles dans **Paramètres de l’équipe → Modèles**. La page distingue connexions, affectations de production et preuves de vérification, puis capacités externes et état des index.

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

**Modifier les affectations** définit les modèles principal, de secours, léger et d’évaluation, ainsi que ceux des vecteurs de connaissances, du reclassement, de la vision documentaire et de l’extraction des lignes. Seuls les modèles de connexions enregistrées sont disponibles. Vérifiez les capacités nécessaires à chaque affectation et confirmez les preuves présentées.

**Affectations de production et preuves de vérification** présente chaque usage. Les éléments nécessitant le plus d’attention apparaissent d’abord. **Afficher N éléments de vérification supplémentaires** développe la liste ; **Afficher moins d’éléments** la réduit. Chaque entrée présente usage, modèle, capacités utilisées, preuves existantes et dernière tentative. Pour relancer une entrée, choisissez **Vérifier**, puis **Démarrer**. **Vérifier les affectations actuelles** permet d’en choisir plusieurs. Les demandes vont au fournisseur. Ces résultats servent au diagnostic : une erreur réseau isolée ne prouve pas que la configuration est inutilisable.

## Nouvelles tentatives automatiques en cas d’échec

Dans **Affectations des modèles**, **Comportement du modèle** contient un seul réglage : **Nouvelles tentatives automatiques en cas d’échec**. Lorsqu’un appel échoue avant que le modèle ait commencé à répondre (délai dépassé, échec de connexion ou erreur temporaire du service), Bowra relance l’appel jusqu’à ce nombre de fois. La valeur par défaut est 3 et la plage autorisée va de 0 à 5 ; indiquez 0 pour désactiver les nouvelles tentatives. Choisissez **Enregistrer** à côté du champ ; ce réglage s’enregistre seul et ne modifie pas les affectations.

Une nouvelle tentative n’a lieu que si le modèle n’a pas encore répondu : une réponse n’est donc jamais envoyée deux fois. Chaque tentative compte dans la limite de temps du tour ; s’il ne reste pas assez de temps, Bowra arrête et signale l’échec. Pendant une nouvelle tentative, l’état d’exécution de la conversation affiche par exemple « Le modèle n’a pas répondu à temps, nouvelle tentative (1/3) ». Si la page indique que la configuration a changé, rechargez-la puis définissez à nouveau la valeur.

## Capacités externes et index

Configurez les modèles de recherche, génération d’images et autres capacités externes dans leur zone dédiée. Vérifiez les index avant d’utiliser un service de connaissances. Une connexion enregistrée n’est pas nécessairement affectée à la production ou prête à être utilisée.

Enregistrez avant de quitter, puis rouvrez la rubrique pour vérifier les valeurs. Le choix de modèles dans une conversation se limite aux options proposées par l’équipe.
