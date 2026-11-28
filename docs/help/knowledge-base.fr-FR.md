# Utiliser la base de connaissances

Bowra recherche les documents ajoutés à la base de connaissances lorsque vous posez des questions. Pour conserver un fichier sans l’utiliser dans les recherches, placez-le dans **Bibliothèque → Fichiers**. Vous pourrez l’ajouter à la base plus tard.

## Descriptions et catégories pour la navigation des sources

Ouvrez les détails d’un fichier prêt. Les responsables autorisés peuvent saisir une description manuelle et attribuer jusqu’à huit catégories sur deux niveaux au maximum. La recherche retrouve les catégories absentes de la première liste. Les descriptions guident la recherche et ne remplacent pas les preuves originales.

La description manuelle est prioritaire. L’effacer puis enregistrer rétablit une description automatique encore valide. Après modification de la source, vérifiez et ressaisissez une description manuelle périmée. Les mises à jour d’état préservent les brouillons non enregistrés.

Les descriptions automatiques sont désactivées par défaut. Un propriétaire ou administrateur choisit **Descriptions des sources** dans **Paramètres d’équipe → Modèles → Affectations**. Les nouvelles versions publiées peuvent alors recevoir une description issue d’extraits limités ; enregistrer ne traite pas les anciens fichiers. Un responsable peut générer explicitement une description, ce qui consomme des crédits. Un échec laisse la recherche disponible. Actualisez avant de relancer, notamment si le résultat de la dernière requête est inconnu.

Dans l’éditeur de colonne Matrix existant, choisissez **Description** ou **Sujets** pour réutiliser des résultats existants admissibles, actuels et non modifiés, dans leurs permissions d’origine. Ce réglage ne lance pas d’extraction et ne publie pas de description commune au fichier. Les Agents reçoivent un petit répertoire autorisé, peuvent découvrir d’autres sources et lisent les preuves originales au besoin. Une absence de résultat ne prouve pas l’absence de documents.

## Organisation de la page

La page propose quatre vues. Elles figurent en onglets au-dessus du contenu ; sur écran étroit, elles deviennent une rangée de boutons à faire défiler latéralement :

- **Fichiers** : importer, rechercher, ouvrir et gérer tous les fichiers de la base.
- **Matrice documentaire** : réunir plusieurs fichiers dans un tableau, avec une question par colonne et une réponse sourcée par cellule.
- **Résultats extraits** : consulter les champs extraits par les tableaux d’extraction.
- **Modèles** : créer et gérer modèles intégrés, personnels, de bibliothèque d’équipe et tableaux d’extraction.

**Ajouter des connaissances** dans l’en-tête fonctionne depuis toutes les vues : il ouvre Fichiers et la fenêtre d’importation.

Lorsque les bases de connaissances sont activées, les quatre onglets deviennent **Bases de connaissances / Matrice de documents / Tables d’extraction / Modèles** ; voir la section suivante.

## Organiser et partager dans des bases de connaissances

Les administrateurs ouvrent cette fonction progressivement. Lorsque les bases de connaissances sont activées, le premier onglet de la page Connaissances est **Bases de connaissances**. Sans cet onglet, la page fonctionne comme le décrivent les autres sections de cet article.

Une fois la fonction activée, les documents sont rangés dans des bases de connaissances nommées. Chacune a un nom, une description, un emplacement, un réglage de qui peut l’utiliser et une liste de membres ; sauf pour les bases par défaut des espaces et de l’équipe, elle a aussi un propriétaire. Un fichier appartient à une seule base de connaissances ; pour le placer ailleurs, déplacez-le vers une autre base.

### Où se trouve une base de connaissances

- **Les miennes** : vous seul pouvez l’utiliser, dans tous vos espaces. Chacun dispose de **Mes connaissances** et peut en créer d’autres pour ranger ses documents. Vos bases personnelles ne se partagent pas ; pour partager, déplacez les fichiers vers une base d’un espace ou de l’équipe.
- **Espace actuel** : elle sert uniquement dans les conversations, agents et matrices documentaires de cet espace. Chaque espace a une base de connaissances qui porte son nom.
- **Équipe** : elle est utilisable dans tous les espaces de l’équipe. Chaque équipe a une **Base de connaissances de l’équipe**.

Mes connaissances, la base qui porte le nom de l’espace et la base de connaissances de l’équipe sont des bases par défaut créées par le système et marquées **Par défaut**. On ne peut ni les renommer, ni changer leur description, ni les supprimer, ni les transférer, et qui peut les utiliser est fixe : les membres d’un espace peuvent modifier sa base par défaut (les lecteurs peuvent seulement l’utiliser), et toute l’équipe peut utiliser la base de connaissances de l’équipe. Les personnes qui les gèrent peuvent toutefois donner plus de droits à certaines personnes dans la liste des membres, par exemple pour qu’un membre ajoute des fichiers à la base de connaissances de l’équipe.

Comme avant, les matrices documentaires n’acceptent que des fichiers des bases d’un espace ou de l’équipe, pas de vos bases personnelles. Une fois les matrices visibles pour vous uniquement activées par votre administrateur, les vôtres acceptent aussi des fichiers de vos bases personnelles ; voir « Matrices visibles pour vous uniquement ».

### Qui peut l’utiliser et les trois niveaux d’accès

Vous pouvez utiliser une base de connaissances si son réglage **Qui peut l’utiliser** vous inclut, si vous figurez dans sa liste de membres ou si vous en êtes gestionnaire automatique. Si plusieurs cas s’appliquent, le niveau le plus élevé l’emporte.

- **Qui peut l’utiliser** : une base personnelle n’est que pour vous. Une base d’un espace est réservée aux **Membres uniquement** (les personnes de la liste) ou ouverte à **tous les membres de l’espace** ; dans ce cas, vous choisissez aussi s’ils peuvent l’utiliser ou la modifier par défaut. Une base de l’équipe est réservée aux **Membres uniquement** ou ouverte à **toute l’équipe** (utilisation seulement).
- **Liste des membres** : vous pouvez ajouter des personnes et des groupes ; une base de l’équipe peut aussi inclure des espaces, c’est-à-dire les membres de cet espace. Une base d’un espace n’accepte que les membres de cet espace ; pour partager entre espaces, créez une base de l’équipe. Quand quelqu’un quitte un espace, ses entrées dans les bases de cet espace cessent de s’appliquer, puis s’appliquent de nouveau s’il revient.
- **Gestionnaires automatiques** : les administrateurs d’un espace peuvent gérer toutes les bases de leur espace. Le propriétaire et les administrateurs de l’équipe peuvent gérer toutes les bases de l’équipe et, dès qu’ils rejoignent un espace, la base par défaut de cet espace. Les gestionnaires automatiques ne peuvent pas être retirés de la liste. Le propriétaire et les administrateurs de l’équipe ne voient pas les bases **Membres uniquement** d’un espace du seul fait de leur rôle.

Les trois niveaux d’accès :

- **Peut utiliser** : rechercher et citer dans les conversations, les agents et les matrices documentaires ; consulter, prévisualiser et télécharger les fichiers ; vérifier les sources.
- **Peut modifier** : également ajouter des fichiers, retirer ou supprimer les fichiers ajoutés soi-même, tenir à jour leurs descriptions et catégories et relancer leur analyse.
- **Peut gérer** : également gérer tous les fichiers, la liste des membres, qui peut l’utiliser, ainsi que le nom et la description.

Le propriétaire est le gestionnaire responsable de la base ; il ne peut pas être retiré de la liste. Les bases par défaut des espaces et de l’équipe n’ont pas de propriétaire : leurs gestionnaires automatiques les gèrent. Seuls le propriétaire et les gestionnaires automatiques peuvent transférer la propriété ou supprimer la base. Les lecteurs d’un espace peuvent au plus utiliser ses bases : rechercher, consulter et télécharger, sans ajouter ni supprimer de fichiers.

### Trouver des bases et des fichiers

L’onglet **Bases de connaissances** liste les bases que vous pouvez utiliser, regroupées sous Les miennes, l’espace actuel et Équipe. Chaque ligne indique le nom, la description, qui peut l’utiliser, votre niveau, le nombre de fichiers (les échecs sont signalés) et la dernière mise à jour. L’avis sur l’index de l’équipe reste affiché au-dessus de la liste, avec les mêmes règles.

- Le champ de recherche trouve les bases et les fichiers par leur nom. Plusieurs bases peuvent porter le même nom ; distinguez-les par leur groupe et leur description.
- **Recherche approfondie**, puis Entrée, recherche dans le contenu de toutes les bases que vous pouvez utiliser. Les résultats forment un tableau de fichiers avec une colonne **Base de connaissances** supplémentaire qui indique où se trouve chaque fichier.
- Le menu **⋯** en fin de ligne propose **Partager** (**Qui a accès** si vous ne pouvez pas la gérer), **Paramètres** et **Supprimer la base de connaissances**, selon votre niveau.

L’en-tête propose **Nouvelle base de connaissances** et **Ajouter des fichiers** (nommé **Ajouter des connaissances** lorsque les bases de connaissances ne sont pas activées). Les tables d’extraction et leurs résultats se trouvent toutes deux sous **Tables d’extraction** ; **Modèles** ne contient plus que les modèles de matrice documentaire.

### Créer une base de connaissances

1. Sur la page Connaissances, choisissez **Nouvelle base de connaissances**.
2. Saisissez un nom. La description est facultative, mais indiquez de quoi parlent les documents : l’assistant s’en sert pour décider quand rechercher.
3. Choisissez où elle se trouve : Les miennes, l’espace actuel ou Équipe. Les emplacements que vous ne pouvez pas choisir restent affichés, avec la raison. Tout le monde peut créer dans Les miennes ; les administrateurs et les membres d’un espace peuvent créer dans l’espace, pas les lecteurs ; seuls le propriétaire et les administrateurs de l’équipe peuvent créer dans l’équipe.
4. Choisissez qui peut l’utiliser. Les options dépendent de l’emplacement ; pour tous les membres de l’espace, l’utilisation est proposée par défaut. Choisissez **Créer**.

La nouvelle base, vide, s’ouvre : vous pouvez ajouter des fichiers tout de suite ou les choisir dans Fichiers.

### La page d’une base de connaissances

Choisissez le nom d’une base pour ouvrir sa page. L’en-tête indique son emplacement, son nom et sa description, qui peut l’utiliser, votre niveau et la dernière mise à jour. Si vous pouvez la modifier, vous voyez **Ajouter des fichiers** ; si vous pouvez la gérer, vous voyez **Partager**, et les autres voient **Qui a accès**.

La page a sa propre adresse, que vous pouvez enregistrer ou envoyer à des collègues. Ouvrir l’adresse ne donne aucun accès : sans accès, on voit « Base de connaissances introuvable », exactement comme pour une base qui n’existe pas.

La page comporte trois onglets :

- **Fichiers** : les fichiers de cette base. Ils fonctionnent comme décrit dans « Rechercher des fichiers » et « Détails d’un fichier » : recherche par nom, recherche approfondie, filtres d’état, de type et de date, tri, pagination et actions groupées sur plusieurs fichiers sélectionnés. Il n’y a ni filtre ni colonne **Périmètre**, mais une colonne **Ajouté par**.
- **Membres** : le groupe « Qui peut l’utiliser » indique qui peut l’utiliser, le niveau par défaut des membres de l’espace et où elle s’utilise. La liste en dessous donne le niveau et l’origine de chaque membre (propriétaire, automatique, ajouté directement, groupe ou espace). Les trois niveaux sont expliqués en bas.
- **Paramètres** : le nom, la description, le propriétaire (**Transférer la propriété…**) et **Supprimer la base de connaissances…**.

Quand une base n’a pas encore de fichiers, les personnes qui peuvent la modifier voient comment ajouter des fichiers ou les choisir dans Fichiers ; les autres voient à qui s’adresser. Si vous pouvez seulement l’utiliser, **Ajouter des fichiers** et **Partager** ne s’affichent pas, et la page indique qui contacter pour ajouter ou modifier des documents.

### Partager et gérer les membres

Si vous pouvez gérer une base, choisissez **Partager** :

1. Sous « Qui peut l’utiliser », choisissez le public.
2. Sous « Ajouter des membres ou des espaces », recherchez par leur nom des personnes, des groupes ou des espaces (seules les bases de l’équipe acceptent des espaces), choisissez un niveau puis **Ajouter**.
3. Dans la liste, changez le niveau d’une personne ou retirez-la. Un retrait affiche un message avec **Annuler** pendant au moins 8 secondes.

Les modifications s’appliquent immédiatement ; la recherche suivante en tient compte. Le bas de la fenêtre indique combien de personnes peuvent l’utiliser, combien parmi elles peuvent la modifier et combien peuvent la gérer. **Copier le lien** copie seulement l’adresse de la base et ne donne accès à personne.

Les autres peuvent ouvrir **Qui a accès** pour voir les mêmes informations sans les modifier. Vos bases personnelles ne se partagent pas.

### Renommer, transférer et supprimer

- **Renommer ou modifier la description** : les personnes qui gèrent la base les modifient dans **Paramètres** puis enregistrent.
- **Transférer la propriété** : le propriétaire ou un gestionnaire automatique peut la transférer à une personne qui gère déjà la base. L’ancien propriétaire continue de la gérer tant qu’il y a droit. Les bases par défaut et les bases personnelles ne se transfèrent pas.
- **Supprimer la base** : le propriétaire ou un gestionnaire automatique choisit **Supprimer la base de connaissances…** ; la confirmation indique le nombre de fichiers. Après suppression, ces fichiers quittent les connaissances et ne sont plus recherchés ; leurs originaux restent dans Fichiers. Les bases par défaut ne peuvent pas être supprimées. Si certains fichiers ne peuvent pas en sortir, la base est conservée et le message explique pourquoi.

### Ajouter des fichiers

Depuis la page d’une base, **Ajouter des fichiers** présélectionne cette base sous **Ajouter à** dans la fenêtre d’importation ; depuis la liste des bases, c’est la base par défaut de l’espace actuel. Les choix sont regroupés sous Les miennes, l’espace actuel et Équipe. Les bases auxquelles vous ne pouvez pas ajouter de fichiers restent affichées avec la raison, sans pouvoir être choisies. La vérification du modèle d’embedding, les formats et tailles acceptés, le ZIP, Google Drive, l’annulation et les explications d’erreur sont les mêmes que dans « Importer des fichiers ».

### Déplacer des fichiers vers une autre base

1. Dans les détails d’un fichier, choisissez **Déplacer vers une autre base de connaissances…**, ou sélectionnez des fichiers dans l’onglet **Fichiers** et choisissez **Déplacer vers une autre base de connaissances…**.
2. Choisissez la base cible. La liste est regroupée sous Les miennes, Espace et Équipe et indique qui peut utiliser chacune ; seules les bases que vous pouvez modifier sont proposées.
3. La fenêtre indique alors combien de personnes gagnent l’accès et combien le perdent avec ce déplacement. Confirmez avec **Déplacer**.

Pour déplacer un fichier que vous avez ajouté, vous devez pouvoir modifier la base où il se trouve ; pour le fichier de quelqu’un d’autre, vous devez pouvoir la gérer. L’original, ses versions, ses citations, les matrices documentaires et les résultats extraits sont conservés, et l’accès suit ensuite la nouvelle base. Les personnes qui perdent l’accès ne voient plus le nom ni le contenu du fichier, et les citations leur affichent une source restreinte.

### Ajouter depuis Fichiers ou depuis une conversation

Dans **Bibliothèque → Fichiers**, **Ajouter à la base de connaissances** demande une base de connaissances (avec les mêmes règles que **Ajouter à** ci-dessus) au lieu d’un périmètre. **Voir dans la base de connaissances** ouvre la base où se trouve le fichier, avec ce fichier sélectionné.

Si vous demandez à l’assistant d’ajouter un fichier aux connaissances dans une conversation, précisez la base. Sans précision, ou si vous ne pouvez pas modifier cette base, l’assistant vous pose la question avant d’ajouter quoi que ce soit.

### Détails d’un fichier et citations

Dans les détails d’un fichier, la visibilité est remplacée par **Base de connaissances** (un lien vers elle) et **Qui a accès** (un résumé d’une ligne, avec **Voir les membres** pour ouvrir son onglet Membres). Le changement de visibilité est remplacé par **Déplacer vers une autre base de connaissances…**. Le reste ne change pas.

Les conversations recherchent dans toutes les bases que vous pouvez utiliser dans l’espace actuel : vos bases personnelles, celles de l’espace actuel et celles de l’équipe. Vous ne choisissez pas de base dans la conversation. Les fiches de source et les sources des rapports suivent les mêmes règles qu’avant ; sans accès, on ne voit qu’une source restreinte. **Ouvrir dans la base de connaissances** ouvre la base où se trouve le fichier, avec ce fichier sélectionné. Le groupe Connaissances de la recherche globale ne liste lui aussi que les fichiers des bases que vous pouvez utiliser.

### Avis et téléphone

- À la première ouverture de la page Connaissances après l’activation, un avis unique indique que vos documents ont été rangés selon leurs anciens périmètres : les fichiers personnels dans Mes connaissances, ceux d’un espace dans la base qui porte son nom et ceux de l’équipe dans la base de connaissances de l’équipe. Qui voit quoi n’a pas changé.
- Les lecteurs d’un espace voient aussi un avis : ils peuvent désormais seulement utiliser les bases de l’espace, sans y ajouter ni supprimer de fichiers ; au besoin, ils s’adressent à un administrateur de l’espace.
- Sur téléphone, la liste des bases et le tableau des fichiers deviennent des lignes, les boutons de l’en-tête passent à la ligne et les onglets défilent latéralement. Partager et les fenêtres à plusieurs champs s’ouvrent depuis le bas de l’écran.

Si un administrateur désactive ensuite la fonction, les fichiers des bases créées dans un espace ou dans l’équipe ne peuvent plus être recherchés ni ouverts jusqu’à sa réactivation. Les fichiers des bases par défaut et de vos bases personnelles restent disponibles.

## Choisir la visibilité

Lorsque les bases de connaissances sont activées, vous choisissez plutôt une base sous **Ajouter à** lors de l’importation, et c’est elle qui détermine qui voit le fichier ; voir la section précédente.

À l’importation :

- **Personnel** : vous seul consultez et recherchez le fichier.
- **Espace** : les membres de l’espace actuel le recherchent. Changer d’espace change les documents affichés.
- **Équipe** : tous les membres de l’équipe le recherchent. Seuls les administrateurs d’équipe peuvent importer dans ce périmètre.

Les lecteurs d’un espace consultent et recherchent ses connaissances, mais ne peuvent ni y ajouter de fichiers, ni modifier ou supprimer ceux qui s’y trouvent, y compris ceux qu’ils ont ajoutés auparavant. Leurs fichiers personnels ne sont pas concernés. Pour ajouter des documents, demandez à un administrateur de l’espace de modifier votre rôle.

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

Lorsque les bases de connaissances sont activées, **Ajouter à la base de connaissances** demande une base, et **Voir dans la base de connaissances** ouvre la base où se trouve le fichier ; voir « Ajouter depuis Fichiers ou depuis une conversation » plus haut.

Téléverser dans Fichiers ou joindre un fichier au chat conserve uniquement le fichier. Sa classification comme référence durable ne l’ajoute pas à la base de connaissances. Choisissez **Ajouter à la base de connaissances**, téléversez depuis la page Connaissances ou demandez explicitement à l’Agent d’ajouter un fichier précis avec la visibilité souhaitée. L’Agent vérifie votre demande initiale et vos droits actuels ; une cible ambiguë nécessite une clarification. Le fichier devient consultable après traitement. Un ZIP joint au chat reste un seul fichier, sans décompression ni indexation ; importez ses documents depuis la page Connaissances. Le retrait de la base conserve l’original. Une ancienne demande ne l’ajoute pas à nouveau après retrait ; une nouvelle demande explicite le peut.

Après une révision dans **Fichiers**, la base de connaissances conserve la version ajoutée et signale qu’une version plus récente existe. Dans **Fichiers**, choisissez **Ajouter la version N** et sélectionnez le périmètre voulu. Une fois la nouvelle version prête, retirez l’ancienne entrée si vous ne voulez plus la retrouver dans les recherches.

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

Lorsque les bases de connaissances sont activées, les actions ci-dessous se font dans l’onglet **Fichiers** de chaque base, sans filtre **Périmètre**. Pour chercher dans plusieurs bases, utilisez la recherche et la recherche approfondie de la liste des bases.

- Au-dessus de la liste, changez le périmètre Personnel, Espace ou Équipe. Utilisez pour cela le filtre **Périmètre** de la barre d’outils (sur téléphone, sous **Filtres**). Espace indique son nom actuel ; une note précise qui peut rechercher ce périmètre.
- Si le filtre **Périmètre** propose **Tous**, il réunit les fichiers personnels, de l’espace actuel et de l’équipe que vous pouvez rechercher ; la colonne **Périmètre** indique l’origine de chaque fichier. La page retient votre dernier choix ; à la première ouverture, elle affiche l’espace actuel.
- Le champ de recherche trouve les noms de fichiers.
- **Recherche approfondie**, puis Entrée, recherche dans le contenu du périmètre actuel. La note indique nombre et périmètre ; chaque résultat présente le passage et son emplacement, page, section ou paragraphe. Sur téléphone, la liste n’a pas la place du passage : chaque résultat indique l’emplacement de la correspondance, par exemple « Page 3 · Résultat de recherche ».
- Sans résultat, changez de mots, utilisez un autre périmètre ou **Rechercher par nom de fichier** pour désactiver la recherche approfondie.
- Filtrez état, type et date ; changez le tri si proposé. Chaque bouton de filtre affiche les valeurs choisies ; pour annuler tout un groupe, ouvrez son menu et choisissez **Effacer l’état** ou **Effacer le type**.
- Avec au moins deux conditions (le mot-clé, chaque état, chaque type et la date comptent chacun pour une), elles sont listées sous la barre d’outils. Retirez-les une à une avec ×, ou choisissez **Tout effacer**. Le périmètre n’y figure pas.
- Sur téléphone, **Filtres** ouvre un panneau en bas avec les groupes Périmètre, Statut, Type et Date. Les choix s’appliquent aussitôt ; **Afficher N fichiers** ramène à la liste. Le tri et la **Recherche approfondie** restent dans la barre d’outils.
- Après importation, la liste adopte son périmètre (elle reste sur **Tous** si vous l’affichez), efface recherche et filtres et montre la première page triée par importation récente. Pour une archive encore en traitement, **Retour à la liste des fichiers** permet de suivre les états. **Terminé** actualise de même une fois le traitement fini.

Si l’adresse évolue avec vos filtres, elle conserve recherche, filtres, tri, page et fichier ouvert. Précédent revient aux résultats antérieurs ; actualiser garde votre position. Le lien transmis à un collègue ne donne pas de droits : seuls les fichiers déjà autorisés sont visibles.

Dans ce mode, le clavier fonctionne aussi : Tab atteint un filtre, Entrée l’ouvre, les flèches parcourent les options, Espace sélectionne et Esc ferme. Sélectionner et ouvrir sont distincts : Espace sur une ligne sélectionne le fichier ; Entrée sur son nom ouvre ses détails.

## Détails d’un fichier

Lorsque les bases de connaissances sont activées, voir « Détails d’un fichier et citations » plus haut pour ce qui change dans les détails.

Ouvrez une ligne pour consulter état, visibilité, importateur, date et taille.

- Lorsque le fichier est disponible, **Utiliser dans une conversation** démarre une nouvelle conversation avec le fichier joint. Cette action est proposée pour vos importations dans l’espace actuel ; les autres documents restent recherchés automatiquement.
- Le nombre de citations apparaît sous le nom lorsqu’il a été cité.
- Depuis une recherche approfondie, le passage correspondant apparaît en premier et surligné.
- **Voir la source** d’une cellule de matrice ouvre ces détails sur la même page, sur le passage cité. Fermez pour revenir à la matrice.
- La progression reste active même si vous fermez les détails. Un échec y explique sa raison et propose **Réessayer**.
- Avec les autorisations nécessaires, changez la visibilité, extrayez vers un tableau ou supprimez. Changer la visibilité d’un fichier ajouté par quelqu’un d’autre demande un motif d’au moins 10 caractères.
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
3. Ouvrez une cellule pour réponse et source. Modifiez-la ou marquez-la comme vérifiée. **Voir la source** ouvre le passage dans les détails sur place. La source indique aussi l’emplacement du passage : la page pour les PDF et les présentations, le paragraphe pour Word, Excel, Markdown et les autres fichiers sans pagination fixe. Aucun emplacement n’apparaît quand le texte cité est introuvable dans le fichier. Les cellules d’exécutions antérieures peuvent encore afficher une page estimée par l’IA ; relancez-les pour situer la page d’après la source.
4. Le coût dépend de la longueur des documents ; une estimation apparaît avant lancement.

Par défaut, une matrice contient au plus 20 colonnes et 500 fichiers ; en cas de dépassement, le message indique la limite réelle et le nombre actuel. Un nom de colonne ne peut pas être vide ni reprendre celui d’une autre colonne de la même matrice ; des noms qui ne diffèrent que par des espaces au début ou à la fin, ou par des caractères pleine chasse et demi-chasse, comptent comme identiques.

La liste **Matrice documentaire** présente une matrice par ligne, avec l’origine des lignes, fichiers × colonnes, la dernière exécution et son auteur. Cliquez sur le nom d’une matrice pour l’ouvrir.

### Modifier une matrice après sa création

Ces actions se font sur ordinateur ; sur téléphone, la matrice est en lecture seule.

- **Ajouter des fichiers** : cliquez sur **Ajouter des fichiers** sous le tableau et recherchez par nom de fichier. Les fichiers sont regroupés sous « Cet espace » et « Équipe ». Les fichiers déjà dans la matrice, les fichiers personnels et ceux encore en traitement sont listés avec la raison pour laquelle ils ne peuvent pas être ajoutés ; les fichiers personnels ne peuvent aller que dans des matrices visibles pour vous uniquement (activées par votre administrateur, voir « Matrices visibles pour vous uniquement » plus bas), où vous les cochez comme tout autre fichier. Vous pouvez ajouter au plus 100 fichiers à la fois. Les nouveaux fichiers apparaissent à la fin du tableau comme « Non exécutée » ; **Exécuter** ne traite ensuite que les cellules pas encore exécutées ou en échec, et les résultats existants sont conservés.
- **Retirer des fichiers** : survolez la ligne d’un fichier et choisissez **⋯** → **Retirer de la matrice…**, ou sélectionnez plusieurs lignes puis **Retirer de la matrice**, au plus 100 à la fois. La confirmation indique combien de cellules sont supprimées et combien d’entre elles ont été modifiées à la main ou vérifiées. Les fichiers restent dans la base de connaissances et pourront être ajoutés de nouveau ; les exécutions en cours ignorent les fichiers retirés.
- **Renommer** : cliquez sur le nom de la matrice en haut, ou choisissez **⋯** → **Renommer**. Saisissez le nouveau nom et appuyez sur Entrée pour l’enregistrer, ou sur Échap pour garder le nom actuel. Le nom ne peut pas être vide.
- **Supprimer la matrice** : choisissez **⋯** → **Supprimer la matrice…**. La confirmation indique combien de résultats de cellules sont supprimés ; les exécutions en cours s’arrêtent et les crédits déjà utilisés ne sont pas remboursés. Les fichiers eux-mêmes ne sont pas touchés.

Les membres de l’espace peuvent ajouter des fichiers, renommer la matrice et retirer les fichiers sans cellule modifiée ni vérifiée. Retirer des fichiers qui ont des cellules modifiées ou vérifiées, ou supprimer la matrice, demande le créateur de la matrice, un administrateur d’espace ou un administrateur d’équipe. Vous pouvez aussi annuler d’abord ces modifications et vérifications dans les cellules, puis retirer les fichiers.

### Matrices visibles pour vous uniquement

Votre administrateur active cette fonction progressivement. Ensuite, l’étape où vous cliquez sur **Créer** pour une nouvelle matrice demande **Qui peut la voir** :

- **Membres de l’espace** (par défaut) : comme avant, les membres de l’espace actuel voient la matrice dans la liste et sur l’accueil et peuvent l’ouvrir ; chaque ligne suit toujours leur propre accès au fichier.
- **Moi uniquement** : vous seul pouvez voir et utiliser la matrice, pas même les administrateurs d’espace ou d’équipe. Vos fichiers personnels s’ajoutent aux fichiers proposés, et la liste et le haut de la page de la matrice affichent **Moi uniquement**.

Pour une matrice existante, son créateur choisit sur ordinateur **⋯** → **Rendre visible pour moi uniquement** ou **Rendre visible pour les membres de l’espace**, puis confirme.

- Quand vous la rendez visible pour vous uniquement, la confirmation nomme les membres qui ont modifié ou vérifié des cellules ou exécuté la matrice : leurs modifications et vérifications sont conservées, mais ils ne peuvent plus l’ouvrir. Tant que des exécutions d’autres membres sont en cours, le changement n’est pas possible ; attendez qu’elles se terminent. Si une colonne a un usage pour la navigation des sources, réglez-le d’abord sur « Aucun ».
- Pour la rendre visible pour les membres de l’espace, la matrice ne doit contenir aucun fichier personnel. Le message liste ces fichiers ; retirez-les de la matrice et réessayez.
- Les matrices visibles pour vous uniquement ne peuvent pas avoir d’usage pour la navigation des sources, et leur enregistrement comme modèle ne peut pas inclure de fichiers personnels ; le message liste les fichiers à retirer d’abord. Leurs exécutions comptent dans la limite d’exécutions simultanées de l’espace.
- Quand vous supprimez votre compte, vos matrices visibles pour vous uniquement sont supprimées avec leurs cellules, exécutions et historique ; les matrices visibles pour les membres de l’espace sont conservées.
- Si votre administrateur désactive ensuite la fonction, les matrices visibles pour vous uniquement restent visibles pour vous seul et gardent leurs fichiers personnels, mais vous ne pouvez plus en créer ni changer qui peut voir une matrice.

### Exécuter et relancer

**Exécuter** traite toutes les cellules inachevées, c’est-à-dire pas encore exécutées ou en échec ; les résultats existants ne sont pas relancés. Cliquez sur **▾** à côté de **Exécuter** pour choisir la portée ; les trois premiers choix indiquent combien de cellules ils couvrent :

- **Exécuter uniquement les inachevées** : comme **Exécuter**.
- **Relancer uniquement les obsolètes** : les cellules dont le résultat est périmé parce que la question de la colonne a changé ou que le fichier a une nouvelle version.
- **Relancer uniquement les échecs** : seulement les cellules en échec.
- **Tester d’abord sur 3 fichiers…** : seulement les 3 premiers fichiers qui ont encore des cellules inachevées. Vérifiez les résultats et ajustez les questions si besoin avant d’exécuter le reste. Le résumé des tâches marque cette exécution comme un essai.
- **Tout relancer…** : extrait de nouveau toutes les cellules, mais conserve les cellules vérifiées et modifiées à la main ; le menu indique combien il en conserve. Pour relancer une cellule vérifiée, retirez d’abord la vérification, ou ouvrez la cellule et cliquez sur **Relancer la cellule**.

**Exécuter cette colonne** et **Relancer toute la colonne…** dans le menu d’un en-tête de colonne fonctionnent de la même façon pour cette colonne.

Avant le lancement, la confirmation indique :

- Le nombre de cellules et les crédits estimés de chaque colonne. La lecture des documents est partagée par les colonnes et a sa propre ligne. Une colonne en double vérification relit chaque fichier, ce qui en fait souvent le coût principal ; la confirmation le signale.
- Le total, votre solde, la durée estimée (vous pouvez quitter la page pendant l’exécution) et la part du fichier le plus long dans l’estimation.
- **Tester d’abord sur N fichiers** : une fois cochée, seuls les premiers fichiers sont exécutés et le coût affiché est celui de l’essai.

Dans une cellule ouverte, **Relancer la cellule** et **Réessayer la cellule** indiquent environ combien de crédits elles utilisent. Une cellule seule est relancée immédiatement, sans confirmation, et remplace une valeur modifiée à la main ou vérifiée.

Les cellules en échec à cause d’un délai dépassé, d’une erreur temporaire du service de modèles ou d’une interruption sont relancées une fois automatiquement avant la fin de l’exécution, après une attente d’une demi-minute à une minute. Les tentatives échouées ne consomment pas de crédits ; une relance réussie est facturée normalement. Si la plupart des appels échouent, ce qui indique une indisponibilité temporaire du service de modèles, rien n’est relancé automatiquement et le résumé des tâches en donne la raison ; réessayez plus tard avec **Relancer uniquement les échecs**. Les cellules dont la sortie du modèle n’a pas pu être lue ont déjà consommé des crédits et ne sont pas relancées automatiquement ; relancez-les vous-même.

### Vérifier les résultats

La barre d’outils au-dessus du tableau aide à vérifier les réponses de l’IA cellule par cellule :

- **Vérifiées x / y**, à droite de la barre, indique combien des cellules que vous pouvez voir sont vérifiées.
- **Rechercher dans le tableau** trouve les lignes par nom de fichier ou par réponse.
- **Statut** n’affiche que les lignes qui ont des cellules non vérifiées, modifiées, à confiance faible, en double vérification divergente, en échec ou obsolètes. Une confiance faible signifie une confiance inférieure à 70 %, ou une réponse dont le texte source cité est introuvable dans le fichier ; une double vérification divergente signifie que les deux extractions d’une colonne en double vérification diffèrent.
- **Ajouter un filtre** filtre selon les valeurs d’une colonne. Les colonnes de texte et d’options proposent « est » et « contient » ; les colonnes de nombres proposent « est », « supérieur à » et « inférieur à », les colonnes de dates « est », « après » et « avant » ; toute colonne propose « est vide ». Cliquez sur un filtre pour le modifier ou le retirer.
- **Trier** ordonne les lignes par nom de fichier ou selon n’importe quelle colonne ; les lignes sans valeur passent en dernier.
- La recherche, les filtres et le tri sont écrits dans l’adresse de la page : revenir en arrière ou recharger les conserve, et un collègue qui ouvre le lien voit la même vue. Cliquez sur **Effacer le filtre** ou appuyez sur Échap pour afficher de nouveau toutes les lignes. Les lignes des fichiers que vous ne pouvez pas voir sont masquées pendant le filtrage.

**Marquer toute la colonne comme vérifiée…** dans le menu d’un en-tête de colonne marque d’un coup comme vérifiées toutes les cellules terminées et à jour de cette colonne, y compris celles modifiées à la main ; les cellules inachevées, en échec ou obsolètes ne sont pas marquées. La confirmation indique combien de cellules seront marquées ; les lignes masquées par les filtres sont incluses. À la fin, le message indique quelles cellules n’ont pas été marquées et pourquoi.

Quand vous ouvrez une cellule, le panneau latéral affiche en haut la question complète de la colonne. En dessous, **Historique** liste les 50 dernières entrées : qui a modifié la valeur, l’a conservée comme texte, l’a vérifiée ou a retiré la vérification, et quand ; et quand l’IA l’a extraite, relancée ou réessayée automatiquement. L’historique enregistre les actions, pas les valeurs précédentes de la cellule ; les modifications faites avant cette fonctionnalité n’ont pas d’historique. Retirer un fichier de la matrice supprime aussi l’historique de ses cellules.

Marquer une colonne comme vérifiée demande un membre de l’espace ; les lecteurs peuvent voir la progression, les filtres, le tri et l’historique. Sur téléphone, seule la progression est affichée.

### Exporter vers Excel

Cliquez sur **Exporter vers Excel** en haut de la matrice pour la télécharger sous forme de classeur Excel portant le nom de la matrice ; les noms des feuilles et les en-têtes suivent la langue de votre interface. **Réponses** contient une ligne par fichier et une colonne par question ; les nombres, les dates et les réponses oui/non sont de vraies valeurs que vous pouvez trier, filtrer et additionner, les réponses vérifiées sont en vert et celles modifiées à la main, pas encore vérifiées, en jaune. **Sources** contient une ligne par cellule avec le fichier, la colonne et sa question, la réponse, son statut, la confiance de l’IA, la page ou le paragraphe quand ils sont connus et le texte source cité. **À propos** indique le nom de la matrice, quand et par qui elle a été exportée, combien de cellules sont masquées et ce que signifie chaque statut. Le fichier ne contient que ce que vous voyez dans la matrice : les fichiers que vous ne pouvez pas voir apparaissent comme « (Aucun accès) », sans leurs valeurs. Le texte est toujours enregistré comme texte, si bien qu’un contenu qui ressemble à une formule n’est pas exécuté, et un contenu de plus de 32 000 caractères est coupé et marqué « (tronqué) ». Tous les rôles de l’espace, lecteurs compris, peuvent exporter ; faites-le sur ordinateur, car le bouton n’apparaît pas sur téléphone. Le fichier montre la matrice au moment de l’export et ne se met pas à jour ensuite.

### États de matrice

- **Non exécuté** : question non traitée.
- **Traitement** : tâche en cours.
- **Échec** : ouvrez pour décider d’un nouvel essai. Les échecs temporaires, comme un délai dépassé, ont déjà été relancés une fois automatiquement.
- **Aucune valeur extraite** : traitement terminé sans résultat extrait. Vérifiez le texte ; cela ne prouve pas l’absence de réponse dans le fichier.

Le résumé regroupe plusieurs tâches ; **Historique des exécutions** détaille chacune. Rouvrir une matrice terminée ne réaffiche pas les anciens succès. Les échecs restent comptés ; développez puis **Voir les échecs**, ce qui règle le Statut de la barre d’outils sur En échec. Les en-têtes affichent le nom complet de la colonne sur deux lignes ; survolez, cliquez ou placez le focus sur un en-tête pour voir toute la question, également visible dans la cellule. Sur téléphone, ouvrez la cellule puis fermez ses détails pour revenir.

Après changement d’espace, les droits sont revérifiés. Sans disponibilité, choisissez une autre matrice ou revenez à l’espace d’origine ; après rétablissement, **Réessayer**.

Un ensemble de questions fréquent peut être enregistré avec **Enregistrer comme modèle** en haut de la matrice, puis ajouté avec **Utiliser comme matrice** dans Modèles pour reprendre toutes les colonnes à la fois. **Publier dans la bibliothèque de l’équipe** permet aux autres espaces de choisir **Copier dans mon espace**. Seules les colonnes de questions sont publiées ; fichiers et réponses restent dans l’espace.

## Extraire les champs vers un tableau

Pour des fichiers similaires, comme comptes de résultat mensuels ou contrats, un **tableau d’extraction** définit les champs à récupérer, par exemple chiffre d’affaires et bénéfice net. Chaque fichier donne une ligne ; les résultats sont consultables dans la base et interrogeables en conversation.

1. Ouvrez **Modèles → Nouveau modèle → Tableau d’extraction (extraire à l’importation)**. Décrivez les champs, vérifiez et enregistrez.
2. Dans les détails ou le menu du fichier, **Extraire vers un tableau** demande quel tableau utiliser. Plusieurs fichiers peuvent être sélectionnés ensemble. Pour Excel, choisissez directement le tableau à l’importation.
3. Une fois terminé, ouvrez **Résultats extraits**. Chaque cellule renvoie au texte source, avec un emplacement indiqué comme dans une matrice de documents.
4. **Ne pas extraire**, sous **Extraire vers un tableau**, arrête l’extraction sans retirer le fichier de la recherche textuelle.

Seuls les résultats des fichiers que vous pouvez lire s’affichent : ceux des fichiers personnels d’autres membres n’apparaissent ni dans la base ni en conversation.

L’extraction suit une file et un quota quotidien ; elle peut donc prendre du temps lorsque les fichiers sont nombreux. La vue **Modèles** contient deux sortes d’éléments : les modèles sont des ensembles réutilisables de questions de matrice (enregistrez une matrice comme modèle, puis appliquez-le à une nouvelle matrice) ; les tableaux d’extraction sont des ensembles de champs extraits automatiquement à l’importation, avec leurs résultats dans **Résultats extraits**. Lorsque les bases de connaissances sont activées, les tables d’extraction et leurs résultats se trouvent toutes deux sous **Tables d’extraction**, où **Nouvelle table d’extraction** en crée une, et **Modèles** ne contient plus que les modèles de matrice.

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

**Annuler l’importation** arrête un transfert en cours. Le même état de changement de source apparaît après l’actualisation ou la relecture du chat.
