# Améliorer un Agent avec Coach

Si un Agent dont vous êtes propriétaire se trompe toujours sur un point, dites-le à Coach avec vos propres mots. Coach en fait un point d’évaluation, vérifie avec vous sur quelques vraies réponses qu’il le juge comme vous, puis modifie le brouillon de l’Agent et indique si la modification a aidé. Vous n’écrivez jamais le point vous-même ; vous dites seulement si Coach a bien jugé chaque réponse.

## Commencer

1. Ouvrez une conversation avec **@Coach**, ou choisissez **L’utiliser pour améliorer l’Agent** sur le pouce baissé d’un membre dans les éléments en attente de l’Agent.
2. Dites ce qui ne va pas, par exemple : « Il ne dit jamais quelle période couvrent les chiffres. » Coach le reformule en un point et affiche une carte de vérification.

Si un membre a baissé le pouce sans commentaire, Coach vous demande d’abord ce qui ne va pas dans cette réponse. Si l’Agent n’a encore aucune réponse, Coach rédige le point à partir de la description de l’Agent ; confirmez-le comme d’habitude. Un point qui nomme un outil compte dès que vous le confirmez ; un point d’appréciation ou de données rédigé ainsi reste indicatif jusqu’à sa vérification sur de vraies réponses.

## Gérer vos cas de test

Les résultats du groupe comptent les lancements via **Exécuter ce groupe** ou **Enregistrer et exécuter**. **Tester cette modification** compare les versions ; ses résultats restent dans **Publier**. Les anciennes réponses conservent leurs questions et attentes enregistrées, même après une modification des cas.

Dans **Évaluer**, modifiez une question enregistrée avec **Modifier la question**. **Enregistrer et exécuter** enregistre les cas actuels et exécute cette version exacte. **Enregistrer les tests** enregistre sans exécution. Si l’enregistrement échoue, rien ne démarre. Si le démarrage reste incertain après l’enregistrement, relancez pour récupérer la même demande. Si la version de l’Agent a changé, vérifiez d’abord la cible actualisée.

**Exécution réussie** confirme les vérifications d’exécution. Lisez séparément les **Critères de réponse** : les évaluations absentes ou partielles sont signalées et les jugements du modèle sont indicatifs. Choisissez **Voir les réponses et critères**, puis ouvrez un cas pour lire la question, la réponse et les attentes enregistrées, même pour un cas réussi. Un désaccord de notation ne prouve pas une erreur de l’Agent.

Les relances automatiques sont repliées sous les résultats. **Vérifier la référence avec Coach** utilise la conversation existante de vérification des réponses ; aucune vérification quotidienne n’est requise.

Dans **Évaluer**, vous pouvez supprimer n’importe quel cas, y compris le dernier, puis enregistrer les tests. Sans aucun cas, votre suite personnalisée est retirée des tests suivants ; les résultats précédents restent dans l’historique. Vous pouvez ajouter de nouveaux cas à tout moment. Une suite supprimée ne peut plus être exécutée seule. Le test de la modification effectue toujours la vérification de sécurité de base.

L’absence de cas personnalisés n’empêche pas la publication et ne nécessite aucune justification. La page et la revue des Agents partagés précisent que la qualité des réponses n’a pas été évaluée. Les autres contrôles de sécurité, les échecs réels des tests et la revue des Agents partagés restent applicables.

## Vérifier les verdicts de Coach

La carte liste quelques réponses. Chaque ligne montre la question, un extrait de la réponse avec la phrase sur laquelle Coach s’appuie en surbrillance, et la conclusion de Coach.

- Ne touchez pas une ligne si Coach a raison.
- Choisissez **Faux** si le verdict est faux, ou **Je ne sais pas** si vous ne pouvez pas trancher.
- Choisissez ensuite **Le reste est juste**. Si tous les verdicts sont justes, c’est le seul clic.

Une ligne de chaque carte est une réponse que Coach a modifiée pour en retirer ce que le point demande ; elle porte la mention **Modifiée par Coach**. Jugez-la comme les autres.

Ensuite :

- **Tout est juste :** le point est confirmé et ajouté à la suite d’évaluation de l’Agent, puis Coach commence à modifier le brouillon.
- **Une ligne Faux :** Coach propose une formulation plus claire et demande quelques réponses de plus. La nouvelle carte montre l’ancienne et la nouvelle formulation ; choisissez **Garder l’original, en vérifier 2 de plus** pour garder la vôtre.
- **Je ne sais pas :** Coach remplace cette réponse par une autre. Après trois, Coach vous demande de formuler le point autrement.
- Coach peut redemander une seule ligne ; choisissez votre réponse sur cette ligne puis **Enregistrer**.

Quand vous validez une carte, elle envoie à Coach un court message **@Coach** indiquant le résultat, pour que Coach continue dans la même conversation. La carte repliée indique alors où en est le point et ce qui se passe ensuite, par exemple **Point confirmé. Ensuite : Coach propose des cas de test à partir de ce point**. Si vous étiez en train d’écrire votre propre message, votre brouillon est conservé et la carte propose **Envoyer à Coach** pour envoyer ce message quand vous êtes prêt. Plus tard dans la conversation, vous pouvez aussi simplement écrire à **@Coach** : il reprend la vérification en cours.

Pour reformuler vous-même le point, choisissez **Reformuler le point**. La carte recommence ; rien n’est écrit dans la suite avant la confirmation. Si la carte indique qu’elle n’est plus à jour, le point a été modifié ailleurs : choisissez **Revérifier**.

Seul le propriétaire de l’Agent peut répondre à une carte de vérification. Les autres personnes voient une ligne qui l’indique.

## Lire le résultat

Une fois le brouillon modifié et une vérification exécutée, Coach affiche une carte de résultat :

- **Ce que vous avez signalé :** combien de cas portant votre point le respectaient avant et après la modification.
- **Questions que Coach n’a pas vues :** les cas mis de côté (voir plus bas), avant et après.
- **Dégradées :** d’autres cas qui réussissaient avant et échouent maintenant. Choisissez **Corriger celui qui s’est dégradé** pour demander à Coach de le corriger.
- Deux nouvelles réponses à lire.

Avec moins de trois cas, la carte affiche des nombres, pas des pourcentages. Si le modèle d’évaluation de l’équipe a changé pendant la vérification, le résultat ne compte pas ; choisissez **Relancer avec le modèle actuel** et laissez Coach lancer une nouvelle vérification.

## Cas mis de côté

Quand Coach ajoute des cas pour un point, l’un d’eux est mis de côté. Coach ne voit ni sa question ni ses réponses ; il sait seulement s’il a réussi. Si une modification réussit aussi ce cas, l’Agent s’est vraiment amélioré, et pas seulement sur les questions que Coach a vues.

Dans l’onglet **Évaluer** de l’Agent, chaque cas a un interrupteur **Mis de côté**. Vous pouvez :

- L’activer sur un autre cas du même point ; la mise de côté s’y déplace.
- Le désactiver. S’il s’agit du seul cas mis de côté du point, une confirmation vous est demandée, et les rapports suivants de Coach indiquent que la modification n’a pas pu être vérifiée de façon indépendante pour ce point.

Seul vous, propriétaire, pouvez changer les cas mis de côté ; Coach ne le peut pas. Les changements prennent effet à l’enregistrement de la suite. Les vérifications de publication exécutent toujours tous les cas.

Les vérifications de publication utilisent toujours la dernière version enregistrée de chaque suite active : dès que vous enregistrez une suite, confirmez un point ou acceptez des cas suggérés, le prochain candidat préparé est vérifié avec eux. Un candidat déjà préparé garde les cas avec lesquels il a été préparé. Si des cas ont été enregistrés depuis, l’onglet **Publier** le signale, et l’étape suivante est la comparaison : **Lancer la comparaison de versions** met d’abord à jour le plan de test avec les derniers cas enregistrés, puis la lance, car une publication nécessite des résultats qui les incluent. Les résultats précédents restent dans l’historique. Le plan n’est mis à jour que si aucune comparaison n’est en cours et que le candidat n’a pas encore été soumis pour validation. Un candidat déjà soumis garde le plan avec lequel il a été soumis et peut toujours être approuvé ; les cas enregistrés pendant la validation sont vérifiés à partir du candidat suivant. Pour les vérifier d’abord, retirez le candidat et préparez-le de nouveau.

## Origine des points et conseils de Coach

Dans l’onglet **Évaluer**, un point confirmé avec Coach indique les mots dont il vient et combien de réponses vous avez vérifiées, avec **Voir** pour le détail. Un point issu du pouce baissé d’un membre affiche **Issu des retours des membres** ; le commentaire du membre n’y apparaît pas.

Quand vous ajoutez ou modifiez un point, Coach le lit et, seulement en cas de problème, ajoute une ligne en dessous : trop vague, deux choses dans un point, un chiffre copié d’une seule réponse, ou identique ou contraire à un autre point. Choisissez **Utiliser cette reformulation**, **Séparer en deux** pour un point qui vérifie deux choses, ou **Garder tel quel** ; Coach n’en reparlera plus pour cette formulation. La suite peut aussi afficher **Coach a examiné cette suite**, avec les situations non couvertes et les cas qui demandent la même chose ; **Voir côte à côte** affiche les deux cas pour les garder tous les deux ou en supprimer un, ce qui s’applique à l’enregistrement de la suite.

Le menu **⋯** d’un point propose **Vérifier sur des réponses**, qui demande à Coach de vérifier ce point avec vous sur de vraies réponses, et, pour un point enregistré, **Voir l’étalonnage**, qui liste les exemples, comment chacun devait être et a été jugé, et le modèle d’évaluation utilisé. Ce ne sont que des conseils : ils ne bloquent ni l’enregistrement ni la publication, et rien ne s’affiche si Coach n’est pas disponible.
