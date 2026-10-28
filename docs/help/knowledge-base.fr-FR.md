# Utiliser la base de connaissances

Bowra recherche les documents ajoutés à la base de connaissances lorsque vous posez des questions. Pour conserver un fichier sans l’utiliser dans les recherches, placez-le dans **Bibliothèque → Fichiers**. Vous pourrez l’ajouter à la base plus tard.

## Organisation de la page

La page propose quatre vues. Elles figurent en onglets au-dessus du contenu ; sur écran étroit, elles deviennent une rangée de boutons à faire défiler latéralement :

- **Fichiers** : importer, rechercher, ouvrir et gérer tous les fichiers de la base.
- **Matrice documentaire** : réunir plusieurs fichiers dans un tableau, avec une question par colonne et une réponse sourcée par cellule.
- **Résultats extraits** : consulter les champs extraits par les tableaux d’extraction.
- **Modèles** : créer et gérer modèles intégrés, personnels, de bibliothèque d’équipe et tableaux d’extraction.

**Ajouter des connaissances** dans l’en-tête fonctionne depuis toutes les vues : il ouvre Fichiers et la fenêtre d’importation.

## Choisir la visibilité

À l’importation :

- **Personnel** : vous seul consultez et recherchez le fichier.
- **Espace** : les membres de l’espace actuel le recherchent. Changer d’espace change les documents affichés.
- **Équipe** : tous les membres de l’équipe le recherchent. Seuls les administrateurs d’équipe peuvent importer dans ce périmètre.

## Importer des fichiers

1. Ouvrez **Bibliothèque → Base de connaissances**, puis **Ajouter des connaissances** en haut à droite.
2. Choisissez le périmètre et déposez ou sélectionnez un fichier à la fois. Pour plusieurs fichiers, importez-les séparément ou réunissez-les en ZIP. PDF, Word, PowerPoint, Excel, CSV, Markdown, TXT, images et ZIP sont acceptés, jusqu’à 200 Mo par fichier. Les fichiers incompatibles d’une archive sont ignorés avec explication dans le résultat ; les dossiers ne sont pas importés comme fichiers.
3. Un message confirme l’importation. Le fichier apparaît et commence son traitement. **Disponible** signifie qu’il peut être recherché dans les conversations.

Un fichier vide ou supérieur à 200 Mo provoque une explication dans la fenêtre. Choisissez un autre fichier.

Pour Excel et CSV, seules les 5 000 premières lignes par fichier, toutes feuilles confondues, sont recherchables. Les lignes suivantes ne sont pas ajoutées ; une réponse utilisant le tableau indique cette limite. Pour toutes les données, divisez le tableau avant importation.

## Ajouter depuis Fichiers

Dans **Bibliothèque → Fichiers**, la colonne **Base de connaissances** indique :

- **Ajouté · état** : le fichier est dans la base. **Voir dans la base de connaissances**, **Voir la progression** ou **Réessayer dans la base de connaissances** l’ouvre.
- **Suggéré pour la base de connaissances** ou **Non ajouté** : choisissez **Ajouter à la base de connaissances**, sélectionnez Personnel, espace actuel ou Équipe, puis confirmez. **Voir la progression** dans le message ouvre directement le fichier.

L’ajout ne déplace ni ne copie le fichier ; il reste dans Fichiers.

Les fichiers téléversés dans Fichiers et reconnus comme documents de référence durables (par exemple des grilles tarifaires ou des politiques internes) sont ajoutés automatiquement à votre base de connaissances personnelle et traités, afin que l’IA puisse s’y référer lorsque c’est pertinent. Si votre équipe n’a pas activé la base de connaissances ou n’a pas configuré de modèle pour celle-ci, le fichier reste uniquement dans Fichiers. Pour annuler un ajout automatique, cliquez sur **Retirer de la base de connaissances**. Le fichier lui-même n’est pas modifié.

## État des fichiers

| État        | Signification                                                                                                                                              |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Importation | Transfert encore en cours                                                                                                                                  |
| En attente  | Traitement à venir, parfois long avec de nombreux fichiers                                                                                                 |
| Traitement  | Lecture et organisation ; plus long pour des fichiers volumineux ou numérisés                                                                              |
| Disponible  | Contenu traitable terminé et recherchable                                                                                                                  |
| Échec       | Survolez l’état pour lire la raison ; si vous gérez le fichier, utilisez le bouton Réessayer à côté, ou ouvrez le fichier pour lire la raison et réessayer |

**Disponible** concerne le contenu de base ; l’extraction des champs peut finir plus tard. Cela ne garantit pas l’aperçu immédiat du fichier original. Si l’état de source est affiché, **Suspendu pour les nouvelles tâches**, **Vérification de source indisponible** ou **Source non vérifiée** est prioritaire. Survolez l’état pour connaître la dernière vérification.

En cas d’échec :

- **Format non pris en charge** : convertissez dans un format accepté et réimportez.
- **Aucun texte recherchable** : vérifiez si le fichier est vide ou contient seulement des images sans texte.
- **Traitement interrompu** ou **Délai dépassé** : **Réessayer** ; divisez les fichiers très volumineux.
- **Service de traitement temporairement indisponible** : réessayez plus tard ; contactez un administrateur si cela persiste.
- **Fichier original introuvable pendant le traitement** : l’importateur ou un administrateur doit le restaurer. Si vous pouvez gérer le fichier, utilisez ensuite le bouton Réessayer à côté de l’état, **Relancer l’analyse** dans le menu de ligne ou **Réessayer** dans ses détails. Bowra vérifie d’abord l’original. S’il manque encore, le traitement ne commence pas et vous devez le restaurer ou le réimporter. Une reprise réussie retire les anciens messages d’échec.

## Rechercher des fichiers

- Au-dessus de la liste, changez le périmètre Personnel, Espace ou Équipe. Utilisez pour cela le filtre **Périmètre** de la barre d’outils (sur téléphone, sous **Filtres**). Espace indique son nom actuel ; une note précise qui peut rechercher ce périmètre.
- Si le filtre **Périmètre** propose **Tous**, il réunit les fichiers personnels, de l’espace actuel et de l’équipe que vous pouvez rechercher ; la colonne **Périmètre** indique l’origine de chaque fichier. La page retient votre dernier choix ; à la première ouverture, elle affiche l’espace actuel.
- Le champ de recherche trouve les noms de fichiers.
- **Recherche approfondie**, puis Entrée, recherche dans le contenu du périmètre actuel. La note indique nombre et périmètre ; chaque résultat présente le passage et son emplacement, page, section ou paragraphe.
- Sans résultat, changez de mots, utilisez un autre périmètre ou **Rechercher par nom de fichier** pour désactiver la recherche approfondie.
- Filtrez état, type et date ; changez le tri si proposé. Chaque bouton de filtre affiche les valeurs choisies ; pour annuler tout un groupe, ouvrez son menu et choisissez **Effacer l’état** ou **Effacer le type**.
- Avec au moins deux conditions (le mot-clé, chaque état, chaque type et la date comptent chacun pour une), elles sont listées sous la barre d’outils. Retirez-les une à une avec ×, ou choisissez **Tout effacer**. Le périmètre n’y figure pas.
- Sur téléphone, **Filtres** ouvre un panneau en bas avec les groupes Périmètre, Statut, Type et Date. Les choix s’appliquent aussitôt ; **Afficher N fichiers** ramène à la liste. Le tri et la **Recherche approfondie** restent dans la barre d’outils.
- Après importation, la liste adopte son périmètre (elle reste sur **Tous** si vous l’affichez), efface recherche et filtres et montre la première page triée par importation récente. Pour une archive encore en traitement, **Retour à la liste des fichiers** permet de suivre les états. **Terminé** actualise de même une fois le traitement fini.

Si l’adresse évolue avec vos filtres, elle conserve recherche, filtres, tri, page et fichier ouvert. Précédent revient aux résultats antérieurs ; actualiser garde votre position. Le lien transmis à un collègue ne donne pas de droits : seuls les fichiers déjà autorisés sont visibles.

Dans ce mode, le clavier fonctionne aussi : Tab atteint un filtre, Entrée l’ouvre, les flèches parcourent les options, Espace sélectionne et Esc ferme. Sélectionner et ouvrir sont distincts : Espace sur une ligne sélectionne le fichier ; Entrée sur son nom ouvre ses détails.

## Détails d’un fichier

Ouvrez une ligne pour consulter état, visibilité, importateur, date et taille.

- Lorsque le fichier est disponible, **Utiliser dans une conversation** démarre une nouvelle conversation avec le fichier joint. Cette action est proposée pour vos importations dans l’espace actuel ; les autres documents restent recherchés automatiquement.
- Le nombre de citations apparaît sous le nom lorsqu’il a été cité.
- Depuis une recherche approfondie, le passage correspondant apparaît en premier et surligné.
- **Voir la source** d’une cellule de matrice ouvre ces détails sur la même page, sur le passage cité. Fermez pour revenir à la matrice.
- La progression reste active même si vous fermez les détails. Un échec y explique sa raison et propose **Réessayer**.
- Avec les autorisations nécessaires, changez la visibilité, extrayez vers un tableau ou supprimez.
- Un aperçu indisponible explique pourquoi. **Réessayer l’aperçu**, si proposé, ne nécessite pas de fermer. Si le contenu est lisible mais non affichable, téléchargez l’original. En cas de perte d’accès ou de fichier inaccessible, vérifiez vos droits ou contactez son responsable.

## Vérifier la source et restaurer l’original

Si **Vérification de source** est proposé, **Vérifier la source** consulte l’état actuel de l’original et de l’index. Le contrôle n’est pas répété automatiquement ; **Vérifier de nouveau** le relance. Trois parties apparaissent :

- **Fichier original** : le trouver ne prouve pas qu’il correspond exactement au contenu indexé. S’il est absent après une vérification normale, contactez l’importateur ou un administrateur. Une impossibilité de vérification peut venir du stockage, du réseau ou des accès ; elle ne prouve pas une perte.
- **Index** : le nombre de fragments correspond au relevé et un échantillon est lisible. Cela ne garantit ni l’intégralité du texte ni toutes les citations.
- **Correspondance de source** : si l’index n’a pas été vérifié contre l’original actuel, sa provenance n’est pas encore prouvée. Comparez chaque citation au texte source.

Vérifier ne retraite pas le fichier. Relancez après modification. La page peut aussi indiquer son usage dans de nouvelles tâches. Un original temporairement inaccessible permet parfois l’usage de l’index déjà vérifié avec un avertissement. Si les nouvelles tâches sont suspendues, suivez d’abord l’explication au lieu de relancer la matrice.

Si vous pouvez gérer le fichier et que des actions de récupération existent :

- **Restaurer l’original** : choisissez un contenu exactement identique. Il sera vérifié sans retraitement ni consommation de crédits de modèle.
- Si le contenu diffère, importez-le comme nouveau fichier. L’ancien et ses citations restent conservés.
- **Analyser de nouveau et vérifier** retraite l’original et reconstruit le contenu recherchable. Cela peut consommer des crédits : lisez l’explication.

Un changement de source pendant une tâche peut suspendre les nouveaux résultats concernés. Les modifications manuelles et marques de vérification sont conservées. Une indisponibilité brève avec contenu précédemment vérifié peut produire un avertissement ; celui-ci ne signifie pas que le texte source a été revérifié.

## Reconstruire plusieurs fichiers

Si **Reconstruire les fichiers sélectionnés…** apparaît, sélectionnez sur la page actuelle les fichiers gérables. Vérifiez nombre, taille et usage du modèle, puis confirmez. Un lot contient au maximum 20 fichiers et 100 Mo, traités un à un. Seul le contenu recherchable est reconstruit ; les tableaux ne sont pas réextraits.

La progression est regroupée sur la page. Après actualisation, vos tâches restent visibles dans l’équipe et l’espace actuels. Après interruption, rétablissez l’accès ou corrigez la source, puis **Vérifier l’accès et reprendre**. Bowra ne réessaie pas sans fin automatiquement. **Arrêter les fichiers restants** peut laisser finir un fichier déjà commencé ; les demandes de modèle envoyées peuvent consommer des crédits.

Les anciens résultats et citations de matrice ne sont pas recalculés automatiquement en cas de problème de source. Une perte d’accès masque nom, texte source et valeurs de matrice ; ils réapparaissent après rétablissement.

## Poser les mêmes questions à plusieurs fichiers

1. Ouvrez **Matrice documentaire → Nouvelle matrice** et choisissez les fichiers, ou sélectionnez-les dans Fichiers puis **Ouvrir comme matrice**.
2. Ajoutez une colonne de question, par exemple « Quel est le montant du contrat ? », choisissez le type de réponse et lancez. Chaque ligne est un fichier, chaque cellule sa réponse.
3. Ouvrez une cellule pour réponse et source. Modifiez-la ou marquez-la comme vérifiée. **Voir la source** ouvre le passage dans les détails sur place.
4. Le coût dépend de la longueur des documents ; une estimation apparaît avant lancement.

### États de matrice

- **Non exécuté** : question non traitée.
- **Traitement** : tâche en cours.
- **Échec** : ouvrez pour décider d’un nouvel essai.
- **Aucune valeur extraite** : traitement terminé sans résultat extrait. Vérifiez le texte ; cela ne prouve pas l’absence de réponse dans le fichier.

Le résumé regroupe plusieurs tâches ; **Historique des exécutions** détaille chacune. Rouvrir une matrice terminée ne réaffiche pas les anciens succès. Les échecs restent comptés ; développez puis **Voir les échecs**. Cliquez ou placez le focus sur un long titre de colonne pour voir toute la question, également visible dans la cellule. Sur téléphone, ouvrez la cellule puis fermez ses détails pour revenir.

Après changement d’espace, les droits sont revérifiés. Sans disponibilité, choisissez une autre matrice ou revenez à l’espace d’origine ; après rétablissement, **Réessayer**.

Un ensemble de questions fréquent peut être **enregistré comme modèle** depuis les paramètres de colonnes, puis ajouté avec **Utiliser comme matrice** dans Modèles pour reprendre toutes les colonnes à la fois. **Publier dans la bibliothèque de l’équipe** permet aux autres espaces de choisir **Copier dans mon espace**. Seules les colonnes de questions sont publiées ; fichiers et réponses restent dans l’espace.

## Extraire les champs vers un tableau

Pour des fichiers similaires, comme comptes de résultat mensuels ou contrats, un **tableau d’extraction** définit les champs à récupérer, par exemple chiffre d’affaires et bénéfice net. Chaque fichier donne une ligne ; les résultats sont consultables dans la base et interrogeables en conversation.

1. Ouvrez **Modèles → Nouveau modèle → Tableau d’extraction (extraire à l’importation)**. Décrivez les champs, vérifiez et enregistrez.
2. Dans les détails ou le menu du fichier, **Extraire vers un tableau** demande quel tableau utiliser. Plusieurs fichiers peuvent être sélectionnés ensemble. Pour Excel, choisissez directement le tableau à l’importation.
3. Une fois terminé, ouvrez **Résultats extraits**. Chaque cellule renvoie au texte source.
4. **Ne pas extraire**, sous **Extraire vers un tableau**, arrête l’extraction sans retirer le fichier de la recherche textuelle.

L’extraction suit une file et un quota quotidien ; elle peut donc prendre du temps lorsque les fichiers sont nombreux. Un « modèle » désigne uniquement un ensemble réutilisable de questions de matrice : enregistrez puis appliquez-le à une nouvelle matrice.

## Sources dans les réponses et rapports

Une phrase utilisant la base porte un numéro de citation permettant de vérifier sa source.

### Conversation

Le repère comporte une petite icône de document et poursuit la numérotation des citations de données.

- Cliquez pour ouvrir sous la phrase nom, emplacement et passage avec la partie pertinente surlignée. Recliquez ou **×** pour fermer. Dans un tableau, la fiche s’ouvre dessous. Elle est disponible à la fin de la réponse.
- **Ouvrir dans la base de connaissances** affiche le fichier sur le passage cité. Un fichier supprimé ou un espace inaccessible provoque une explication.
- Si une nouvelle analyse a fait disparaître le passage, la fiche indique une source actualisée. Ouvrez la base pour le dernier contenu.
- Sans accès, le repère est atténué et indique uniquement une source restreinte, sans nom ni contenu. Une suppression indique une source indisponible.
- Si l’ancien passage reste lisible après nouvelle analyse, il est présenté comme provenant d’un traitement antérieur, sans remplacement automatique.

### Rapport

- Survolez une citation pour voir nom, emplacement et passage ; **Ouvrir dans la base de connaissances** ouvre au passage.
- La rubrique **Sources de données** à la fin inclut aussi nom, emplacement, extrait et lien.
- La visibilité dépend de vos propres accès au fichier, et non de ceux de l’auteur du rapport. Les collègues sans accès voient **Source restreinte** et une indication réservant son contenu aux membres autorisés, sans nom ni texte.

### Source temporairement invérifiable

Un avertissement d’original indisponible signifie que le résultat utilise un contenu vérifié auparavant. Revérifiez les conclusions après rétablissement. Les avertissements du corps du rapport sont enregistrés : conservez-les dans l’exportation complète. Rouvrir une citation vérifie votre accès actuel ; elle ne permet pas de lire une source non autorisée.

## Supprimer des fichiers

Depuis les détails, la suppression après confirmation est immédiate et irréversible. Depuis une sélection ou le menu de ligne, une confirmation est suivie d’**Annuler** en haut pendant au moins 8 secondes ; cette action garde les fichiers dans la liste sans suppression. Les documents supprimés ne sont plus recherchés.

## Problèmes d’importation

La fenêtre explique les refus, reste ouverte et garde votre périmètre.

- **Impossible d’ajouter à la base** : format incompatible. Consultez les formats proposés ou exportez en PDF.
- **Transfert interrompu** : réseau coupé pendant l’envoi, souvent par une limite de passerelle d’entreprise. Si seuls les gros fichiers échouent, demandez au responsable du déploiement d’augmenter la limite. Pour un incident isolé, **Réessayer** relance le même fichier.
- **Échec de connexion réseau** : aucun contenu n’a été transmis. Vérifiez le réseau et **Réessayer**.

**Annuler l’importation** arrête un transfert en cours.
