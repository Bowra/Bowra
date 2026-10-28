# Créer et gérer votre Agent

Vous pouvez créer un Agent pour votre usage, vérifier sa configuration et confirmer sa création, puis l’ajuster et l’utiliser depuis sa page de gestion. Les entrées et options dépendent des fonctionnalités activées par votre équipe. Suivez les options réellement proposées sur la page.

## Choisir une méthode de création

- **Panneau à droite** : **Créer un Agent** dans le marché ou la page de gestion ouvre un panneau à droite du travail actuel ; la conversation et la saisie restent à gauche. Sur écran étroit, basculez entre conversation et création.
- **Demander à l’assistant** : indiquez dans la conversation que vous voulez un nouvel Agent et décrivez sur quoi il travaille, ce qu’il fait et ce qu’il doit produire. Si l’objectif reste trop vague, l’assistant vous demande une seule fois des précisions ; vous pouvez aussi demander à tout moment une configuration manuelle ou l’annulation. Une fois l’objectif clair, un brouillon généré par l’IA s’ouvre à droite, avec le nom que vous avez explicitement indiqué.
- **Brouillon avec l’IA** : décrivez le problème dans le panneau de droite, générez un brouillon, vérifiez-le et confirmez la création. Vous pouvez aussi passer à la saisie manuelle. Si aucun brouillon ne peut être généré pour le moment, passez à la configuration manuelle ; l’objectif saisi est repris.

Dans tous les cas, vous vérifiez la configuration et confirmez vous-même la création.

## Renseigner la configuration

Indiquez nom, objectif et instructions, puis sélectionnez les outils proposés. Les Skills ne sont sélectionnables que si la page le permet explicitement. La dernière étape récapitule les outils et la catégorie de risque des outils d’équipe. Vérifiez-les avant confirmation. À l’utilisation, les autorisations de l’utilisateur et l’état des connexions restent contrôlés.

- Si aucun outil ne convient, les équipes autorisant les assistants uniquement textuels peuvent laisser outils et Skills vides. Les autres doivent sélectionner au moins un outil. Voir [Configurer les capacités d’un Agent](permissions/configure-capabilities.fr-FR.md).
- Choisissez précisément les Skills nécessaires. Un Skill ajouté peut être lu ; s’il contient des scripts, l’Agent peut aussi les exécuter dans le bac à sable isolé. Vous ne sélectionnez pas d’outil distinct pour lire, exécuter ou lister des Skills. Si le déploiement n’a pas activé les scripts de Skill, l’éditeur l’indique à côté du Skill : l’Agent peut le lire mais n’exécute ses scripts qu’après activation par l’administration, sans nouvel enregistrement. Un Agent déjà publié garde ses autorisations jusqu’à sa prochaine publication. Les Skills ne donnent accès à aucune donnée ni connexion.
- L’Agent utilise la dernière version approuvée de chaque Skill. Quand un Skill utilisé est mis à jour, la page de l’Agent et la revue de publication l’indiquent et recommandent de réévaluer l’Agent. Désactiver un Skill d’équipe pour vous seul dans Skills n’affecte pas les Agents qui l’incluent ; une restriction posée par un administrateur s’applique toujours.
- Si l’enregistrement ou la publication est bloqué, le message liste chaque Skill ou outil concerné avec sa raison et la marche à suivre. **Modifié et en attente de nouvelle approbation** ou **Pas encore approuvé** : demandez l’approbation à un administrateur ou retirez le Skill. **Désactivé pour l’équipe** : demandez sa réactivation. **Fichiers illisibles** ou **contrôle de sécurité manquant ou endommagé** : téléversez de nouveau le Skill. **Ce nom désigne désormais un autre Skill** ou **modifié après son ajout** : retirez-le puis ajoutez-le de nouveau. **Paquets intégrés au Skill** : un administrateur relit la publication. Une mention de commandes comme `curl` dans les instructions n’apparaît qu’en avertissement ; les scripts s’exécutent sans accès réseau.
- Si un Skill ne peut pas être utilisé pendant une exécution, l’étape indique le Skill, l’action tentée et la raison, par exemple scripts de Skill non activés, restriction posée pour vous par un administrateur, nouvelle approbation en attente ou Skill supprimé. Suivez l’action indiquée ; répéter la même demande n’aide pas tant que la cause persiste.
- À l’étape Outils, vous pouvez choisir des plugins entiers : les plugins intégrés (comme Analyse de données ou Rapports et canevas) et les plugins d’équipe y figurent avec leur numéro de version. Un plugin apporte ses outils ; un plugin avec consignes les joint automatiquement. Un plugin non sélectionnable indique pourquoi, par exemple « Désactivé par votre équipe », « Pas prêt » ou « Disponible dès que la confirmation à chaque appel sera prête ». Un plugin sélectionné qui devient indisponible reste coché pour que vous puissiez le retirer. Quand un plugin est mis à jour, l’agent utilise le nouveau contenu à sa prochaine exécution.
- Lors de la relecture d’un brouillon généré par l’IA, vous pouvez ajouter ou retirer des plugins et des compétences au même endroit. Lors de la modification d’un agent existant (y compris depuis l’assistant de configuration), vous pouvez ajuster ses plugins ; seules les sélections modifiées sont enregistrées.
- Pour lire ultérieurement des pages fournies par l’utilisateur, indiquez « l’utilisateur fournit l’URL à chaque demande » sans lien d’une tâche précise. Vérifiez que le brouillon inclut la lecture de pages : la recherche web ne remplace pas la lecture d’un lien donné. Si une page ne peut pas être lue, l’Agent indique qu’il n’a pas pu la vérifier. Contrôlez son accessibilité ou collez le contenu et réessayez.
- Pour les données métier de l’équipe, comme revenus de magasins ou détails des ventes, le brouillon choisit l’outil intégré de requête de données, même si la description dit seulement « données de l’équipe ».
- Si une capacité obligatoire n’a aucun outil sélectionné, la vérification échoue et explique le besoin manquant, pourquoi rien n’a été sélectionné et comment corriger. Choisissez un outil adapté ; s’il est verrouillé, demandez son activation à un administrateur. La création reste bloquée jusqu’à correction.
- Si une ressource devient indisponible à l’enregistrement, la carte garde votre saisie et vous demande de la vérifier, la retirer ou la remplacer.

Un nouvel Agent est visible uniquement par vous. Il n’est ni partagé automatiquement ni ajouté au marché. Pour le partager, consultez les actions de sa page de gestion.

Dans Bowra Community, un document créé avec succès apparaît comme fichier téléchargeable dans la conversation. Mentionner un nom de fichier dans une réponse ne confirme pas sa création. Pour PowerPoint, demandez une nouvelle présentation simple avec du texte et des tableaux. Si vous fournissez un modèle à préserver, l’Agent doit expliquer cette limite et demander si une nouvelle présentation séparée vous convient avant de la créer. Vérifiez le fichier téléchargé avant de le partager ; ce Skill ne fournit ni rendu des diapositives ni garantie de fidélité au modèle.

## Enregistrer un brouillon et reprendre

Dans le panneau à droite, la création manuelle crée d’abord un brouillon avant d’afficher le formulaire. La création avec l’IA crée aussi un brouillon reprenable. Si sa création échoue, **Réessayer de créer le brouillon** poursuit la même tâche.

- Après une pause de saisie, les changements sont enregistrés automatiquement ; **Enregistrer le brouillon** est aussi disponible. Seul « Brouillon enregistré » confirme leur conservation. Si l’enregistrement automatique échoue, la saisie reste dans le panneau ; réessayez.
- Le brouillon dans le Canvas est enregistré automatiquement ; la barre d’outils affiche **Enregistré** ou **Enregistrement…**. Ce n’est que pendant un enregistrement en cours ou après un échec que fermer le panneau ou changer de page, conversation ou espace propose **Continuer à modifier** ou **Abandonner les changements non enregistrés**. L’abandon retire uniquement les changements non enregistrés ; le brouillon déjà enregistré reste. En cas d’échec, choisissez **Réessayer** dans la barre d’outils.
- Si un autre onglet a enregistré le même brouillon, comparez pour chaque champ sa valeur avant modification, votre saisie et la dernière valeur enregistrée. Choisissez puis enregistrez pour éviter d’écraser les changements ailleurs.
- L’entrée générale permet de reprendre un brouillon de l’espace actuel ou d’en commencer explicitement un autre. La liste présente nom, origine, état, dernier enregistrement et expiration. Un brouillon manuel reprend directement dans l’éditeur à droite.
- **Créés par moi** répertorie aussi les brouillons IA et manuels encore reprenables. L’adresse du brouillon ouvert permet d’actualiser et de le retrouver.
- Si le brouillon est introuvable, vérifiez l’espace, choisissez **Voir mes brouillons** ou recommencez. Un brouillon expiré n’est pas reprenable. Un échec temporaire de chargement peut être relancé sur la carte.

## Générer avec l’IA

1. Choisissez la création avec l’IA, saisissez l’objectif et générez les suggestions. Vous pouvez arrêter d’attendre et saisir manuellement ; les résultats arriveront dans le même brouillon.
2. Comparez chaque champ actuel aux suggestions. Adoptez seulement celles souhaitées ou ignorez-les ; vos changements ne sont jamais remplacés automatiquement. Répondez aux précisions à droite ou modifiez l’objectif et générez de nouveau.
3. Dans **Capacités et outils**, ouvrez le catalogue pour rechercher d’autres outils autorisés ou retirer les suggestions initiales. La page marque les ajouts et explique les indisponibilités. Choisissez **Enregistrer et vérifier**. Si le catalogue échoue, vos saisies et sélections restent ; réessayez plus tard. Si un outil est devenu indisponible, retirez-le ou remplacez-le. L’ajout de Skills dépend des options proposées.
4. Vérifiez et confirmez la création.

**Configurer manuellement** passe au formulaire. Les changements à droite sont enregistrés dans le même brouillon ; l’objectif d’origine devient une instruction de travail et une version courte de la description d’orientation. Un nom explicitement fourni est conservé. Vous pouvez reprendre depuis l’adresse d’origine ou **Créés par moi**. En cas de conflit de version, vérifiez et rechargez. Après un échec, la page revérifie l’état du brouillon ; si la génération n’est plus possible, passez en manuel en conservant l’objectif et les réponses supplémentaires. Modifier le nom ou les instructions n’efface pas les raisons initiales des outils recommandés.

## Modifier le brouillon depuis la conversation

Si votre équipe permet cette fonction, les brouillons IA en vérification et les brouillons manuels enregistrés sont liés à la conversation actuelle. Lorsque le panneau confirme ce lien, décrivez à gauche les champs à modifier. Après enregistrement, le panneau s’actualise ; vérifiez et confirmez la création. S’il demande de modifier directement à droite, faites-le.

- Un nouveau message reste dans le champ de saisie si le panneau a des changements non enregistrés. Enregistrez-les ou abandonnez-les, puis renvoyez.
- La conversation n’écrase pas votre saisie en cours. Rechargez si un conflit de version est signalé.
- Une carte manuelle non liée se modifie à droite ; les messages à gauche ne la changent pas.

## Après création

**Gérer l’Agent** ouvre immédiatement sa configuration. Vous pouvez aussi le retrouver dans vos Agents. Sa présence dans votre liste personnelle ne signifie pas que vous l’avez recruté. Les anciens Agents ouvrent leur page de configuration ; ceux gérés par versions ouvrent l’atelier des versions.

L’atelier des versions comporte trois onglets : **Configurer**, **Évaluer** et **Publier** ; seuls les onglets activés par votre équipe apparaissent. Les anciens onglets des propositions de modification, de la vérification et publication et de l’historique des versions y sont regroupés ; les anciens liens mènent automatiquement au bon endroit.

- **Configurer** : une seule page en trois sections repliables. **Ce qu’il fait** regroupe nom, description, instructions, consignes d’outils intégrées, modèle et capacité principale. **Ce qu’il peut utiliser** regroupe plugins, Skills, outils et connexions ; **Ajouter…** ouvre le catalogue dans un panneau latéral. **Qui peut l’utiliser et comment il est référencé** regroupe public, méthodes facultatives, informations de référencement, politique de copie et contrat de sortie ; cette section est repliée par défaut. Quand un élément d’une section bloque la publication, son en-tête devient rouge et en indique la raison. En haut, **Propositions en attente** liste les changements venant de pipelines externes et des retours des utilisateurs à examiner et intégrer ; la zone est masquée quand elle est vide et l’onglet **Configurer** affiche leur nombre. Chaque modification affiche la valeur actuelle à côté de la valeur proposée ; si vous n’avez pas enregistré de brouillon depuis la dernière publication, la comparaison se fait avec la version publiée, et la fusion crée le brouillon à partir de celle-ci. Si **Fusionner les modifications sélectionnées** n’est pas disponible, la carte en indique la raison, par exemple quand aucune modification n’est sélectionnée. Au-dessus, une liste des pouces vers le bas à examiner affiche une ligne par échange qu’un membre a noté d’un pouce vers le bas et vous a aussi partagé : l’étiquette **Pouce vers le bas**, le début de la question, puis qui, quand, les raisons choisies et le commentaire (**Aucun commentaire** s’il n’y en a pas). **L’utiliser pour améliorer l’Agent** ouvre une nouvelle conversation avec Coach, la question, les raisons et le commentaire préparés dans la zone de saisie ; rien n’est envoyé tant que vous n’envoyez pas. **Ignorer** retire l’élément, avec possibilité d’annuler. Dans les deux cas, l’élément quitte la liste et n’est plus compté sur l’onglet **Configurer** ; l’échange reste disponible dans **Rédiger à partir de conversations**. Un élément disparaît aussi quand le membre arrête le partage ou retire son pouce vers le bas. Un pouce vers le bas non partagé ne vous parvient jamais, et un échange partagé depuis le menu **⋯** sans pouce vers le bas n’apparaît que dans **Rédiger à partir de conversations** ; ces éléments ne déclenchent pas de notification dans la boîte de réception.
- **Évaluer** : gérer les suites d’évaluation.
- **Publier** : commence par la carte **Cette modification**, qui indique par section ce qui a changé et qui l’a changé (Coach, Manuel, Pipeline externe ou Retour), les suites d’évaluation et l’état du contrôle de publication, chacun avec un lien vers l’onglet correspondant. S’il n’y a aucune différence, elle indique que le brouillon est identique à la version en ligne. Viennent ensuite le contrôle de publication et ses actions, puis, en bas, l’historique des versions avec **Créer un brouillon depuis cette version**.
- **Pipelines externes** : les pipelines externes se gèrent avec des clés Team OpenAPI, pas depuis l’atelier ; leurs propositions apparaissent dans **Propositions en attente** comme **Proposition d’un pipeline externe**. Pour arrêter un pipeline externe, désactivez ou révoquez sa clé dans Paramètres de l’équipe › OpenAPI.

- **Commencer à utiliser** ouvre une nouvelle conversation avec l’Agent sélectionné pour saisir la tâche. La même entrée apparaît dans les détails et la liste de gestion. Si version ou autorisations ont changé, revenez à la gestion comme demandé. Un nom saisi comme texte n’est pas une sélection d’Agent. Les brouillons non envoyés sont conservés.
- **Modifier la configuration** : ajustez puis enregistrez une copie de travail. Cet enregistrement ne modifie pas la version utilisée. Un nouvel Agent a déjà une version utilisable ; son atelier peut donc ne pas avoir de « Prochain brouillon ». Si les changements sont seulement dans le navigateur, enregistrez-les avant de passer à l’onglet **Publier**.
- **Consignes d’outils intégrées** : sous **Ce qu’il fait** dans l’onglet **Configurer** de l’atelier des versions, activez cette option si l’Agent doit aussi suivre les consignes de Bowra pour les outils dont il dispose, par exemple les règles de requête des données métier. Elle est désactivée par défaut. Comme les autres réglages, elle ne s’applique qu’après l’enregistrement de la copie de travail et la mise en service de la nouvelle version ; l’activer ou la désactiver crée une nouvelle version que les relecteurs voient comme une modification.
- **Appliquer une nouvelle version** concerne uniquement votre usage et n’ajoute pas l’Agent au marché. Pour les collègues, **Activer la nouvelle version** change la version actuelle et son public. Le marché affiche actuellement selon la visibilité de l’équipe. Si une validation est nécessaire, **Soumettre à l’approbation de l’administrateur** apparaît. Vérifiez public et conséquences ; voir [Publier un Agent](permissions/publish-agent.fr-FR.md).
- **Qui peut l’utiliser et comment il est référencé** : cette section repliée de l’onglet **Configurer** définit public, méthodes facultatives, informations de référencement comme description et catégorie, politique de copie et contrat de sortie.
- **Évaluations** : dans l’onglet **Évaluer**, un résultat **Échec** ou **Incomplet** propose **Voir pourquoi l’évaluation n’a pas réussi**. Une panne d’environnement d’évaluation ne juge pas la qualité de l’Agent : réessayez plus tard et contactez un administrateur si elle persiste. Des cas de test échoués nécessitent de vérifier tâche et résultat attendu. Zéro cas réussi ne prouve pas une mauvaise configuration. Si la politique a changé, actualisez, vérifiez le plan courant puis préparez de nouveau. La page applique la dernière politique sans écraser vos changements récents.
- **Points attendus dans les résultats** : quand vous ouvrez les réponses d’un cas, chaque exécution liste les points du cas avec **Respecté**, **Non respecté** ou **Non évalué** et la phrase de la réponse sur laquelle repose le verdict ; les points respectés sont repliés sous l’exécution. Si un verdict est faux dans un sens ou dans l’autre, choisissez **Mal jugé**. La réponse est enregistrée afin que les vérifications suivantes de ce point la jugent dans l’autre sens, et Coach peut proposer une reformulation ; le résultat de cette comparaison ne change pas : modifiez le point puis relancez la comparaison. Un point qui a échoué à la vérification avec une bonne et une mauvaise réponse est affiché à titre indicatif, avec la raison. Quand une vérification de version liste « Un point noté par le modèle n’a pas été respecté » comme risque, les points non respectés et leurs phrases apparaissent avec le risque, chacun avec **Mal jugé** ; vous pouvez enregistrer le risque et publier, marquer le verdict, ou les deux.

### Faire modifier un Agent existant par Coach

Nommez dans la conversation un Agent que vous possédez et décrivez les changements. Vous pouvez aussi choisir **Demander à Coach de le modifier** à côté des onglets de l’espace de versions, ou **Demander à Coach de corriger** quand une vérification de publication échoue : une nouvelle conversation s’ouvre avec **@Coach** et la demande dans la zone de saisie. Rien n’est envoyé avant que vous complétiez et envoyiez la demande. Si vous pouvez modifier cet Agent (le même droit que pour enregistrer sa copie de travail à la main), Coach écrit directement les changements dans son brouillon ; il n’y a rien à adopter champ par champ. L’éditeur de l’Agent s’ouvre à droite. Si vous ne pouvez pas le modifier, Coach se contente de suggestions dans la conversation et ne change rien.

Vous pouvez aussi solliciter Coach directement : tapez **@** dans la zone de saisie et choisissez **Coach**, ou ouvrez Coach depuis la place de marché des Agents. Coach n’est jamais choisi automatiquement pour d’autres demandes. Si votre équipe a désactivé Coach, il n’apparaît à aucun de ces endroits.

- Le changement va uniquement dans le brouillon ; rien n’est publié ni appliqué. La nouvelle version ne prend effet qu’après le contrôle de publication dans l’onglet **Publier**.
- Si le canevas contient des changements non enregistrés, votre message n’est pas envoyé à Coach et un bandeau s’affiche au-dessus de la zone de saisie : **Enregistrer et continuer** enregistre le canevas puis envoie le même message ; **Abandonner les modifications** supprime les changements du canevas pour que vous puissiez renvoyer.
- À chaque écriture de Coach dans le brouillon, une ligne sous la réponse indique combien de modifications y sont allées ; **Voir dans le canevas** ouvre l’Agent.
- Une Skill ajoutée par Coach doit être accompagnée des Skills dont elle a besoin. S’il en manque, Coach n’écrit rien et indique lesquelles sont nécessaires.
- Après l’écriture de Coach, un éditeur ouvert de cet Agent s’actualise, sauf s’il contient une saisie non enregistrée.
- Corrigez d’abord sous **Ce qu’il peut utiliser** tout outil ou Skill invalide ou non autorisé.

Dans le canevas, l’éditeur de l’Agent affiche de haut en bas **Modifications de Coach**, les trois sections de configuration (Ce qu’il fait, Ce qu’il peut utiliser, Qui peut l’utiliser et comment il est référencé) et la carte du contrôle de publication, où vous pouvez lancer un contrôle et publier.

- Chaque ligne de **Modifications de Coach** correspond à un champ. **Annuler** rétablit seulement ce que Coach a écrit, sans toucher à vos propres changements ; une ligne annulée propose **Réappliquer**. **Tout annuler** annule toutes les lignes.
- Si vous avez aussi modifié un champ après Coach, sa ligne affiche **Modifié aussi par vous** et **Voir le conflit**. La fenêtre présente la valeur d’avant Coach, celle écrite par Coach et la valeur actuelle. Vous pouvez choisir :
  - **Modifier dans Configurer** : changer vous-même le champ dans la configuration.
  - **Garder l’actuel** : conserver votre valeur. La ligne quitte **Modifications de Coach** et compte désormais comme un changement manuel ; le brouillon n’est pas modifié.
  - **Rétablir la valeur d’avant Coach** : après confirmation, un enregistrement ordinaire remplace votre valeur actuelle par celle d’avant Coach.
- Tant qu’un champ est en conflit, **Tout annuler** n’annule rien et propose **Annuler les N autres**.
- Tant que le canevas a des changements non enregistrés, les commandes d’annulation attendent que vous les enregistriez ou les abandonniez.

#### Après une modification du brouillon par Coach

Dans **Paramètres personnels → Compte → Agents**, ou dans le menu **⋯** de l’éditeur du canevas, choisissez ce qui se passe après une modification par Coach. Ce réglage ne concerne que les modifications que vous demandez à Coach.

- **Vérifier automatiquement** (par défaut) : après avoir écrit le brouillon, Coach lance un contrôle de publication sur ce changement, avec une limite fixe de 200 requêtes au modèle, 1 comparaison et 30 minutes au maximum. Si l’estimation peut dépasser 200 requêtes, le contrôle ne démarre pas ; on vous demande de le vérifier et de le lancer vous-même. Un contrôle automatique plus récent remplace un contrôle plus ancien encore en cours et retire le candidat de ce dernier s’il était seulement préparé. Un candidat déjà soumis à validation n’est jamais retiré : le contrôle ne démarre pas et on vous demande de retirer le candidat dans l’onglet **Publier**. Les requêtes déjà utilisées restent comptées.
- **Me demander d’abord** : Coach demande seulement le contrôle. Vérifiez l’usage estimé et lancez-le depuis la carte du contrôle de publication.

Dans les deux cas, un contrôle réussi ne publie ni n’applique l’Agent de lui-même.

## Décrire ce que fait une bonne réponse

Dans l’onglet **Évaluer**, chaque cas de test peut lister des points attendus : des phrases simples qui décrivent ce que fait une bonne réponse. Il en existe trois types :

- **Critère d’appréciation** : quelque chose de visible dans la réponse, par exemple « compare avec le même mois de l’an dernier ».
- **Fait chiffré** : un chiffre que la réponse doit indiquer, par exemple « indique le chiffre d’affaires de la Chine de l’Est au dernier trimestre ». Il est comparé aux données que l’Agent a réellement lues pendant cette exécution.
- **Action requise** : un outil que l’Agent doit appeler avant de répondre, par exemple une requête sur les données de ventes.

**Suggérer des cas de test avec l’IA** rédige quelques points pour chaque cas, et Coach peut suggérer des points lorsqu’il propose des cas. Un point suggéré indique son origine. Il ne compte qu’après **Confirmer** ; les points que vous écrivez vous-même sont confirmés à l’enregistrement de la suite. Enregistrez ensuite vos modifications.

Si la version en ligne a déjà répondu à la question d’un cas, choisissez **Voir comment ils sont jugés sur la réponse en ligne** sous les points du cas pour afficher cette réponse et la façon dont chaque point suggéré serait évalué, avec la phrase sur laquelle l’évaluation s’appuie. Si une évaluation semble fausse, reformulez le point avant de le confirmer. Choisissez **Marquer comme bonne réponse** lorsque vous accepteriez la réponse en ligne ; cela aide à vérifier que le point distingue les bonnes réponses des mauvaises.

Chaque point affiche une ligne d’état. Pour l’instant, seule une action requise (un point qui nomme un outil) bloque une publication et affiche **Bloque la publication** ; les points d’appréciation et de données sont indicatifs et l’indiquent, y compris pendant leur calibrage ou tant que l’évaluation de ce type de point est en cours de validation. Ajoutez un point avec **Ajouter un point** sous le cas, et modifiez-le ou supprimez-le depuis son menu **⋯**. Conseils : un seul élément par point, quelque chose que l’on peut retrouver dans la réponse, et pas de chiffres exacts tirés des données du jour, qui changent d’une exécution à l’autre.

Pour transformer une remarque en point sans l’écrire vous-même, demandez à Coach puis vérifiez ses verdicts sur de vraies réponses ; voir [Améliorer un Agent avec Coach](agent-evaluation-with-coach.fr-FR.md), qui explique aussi l’interrupteur **Mis de côté** de chaque cas.

### Créer des cas d’évaluation

- **Rédiger à partir de conversations.** Dans l’onglet **Évaluer**, choisissez **Rédiger à partir de conversations** et sélectionnez jusqu’à 32 questions que vous avez posées à cet Agent. Chacune devient un cas dont la tâche est votre question telle que vous l’avez posée, et Coach suggère ce qu’une bonne réponse doit contenir. La liste montre vos propres conversations terminées, ainsi que les échanges isolés que des membres vous ont partagés depuis le menu **⋯** d’une réponse, marqués **Partagé par** et leur nom. Vous ne voyez jamais les autres conversations des membres, et la réponse d’un échange partagé n’est lue que lorsque vous en tirez un cas. Un membre peut cesser de partager à tout moment ; l’échange quitte alors votre liste, mais un cas déjà rédigé à partir de lui est votre copie et reste dans votre suite. Vérifiez les cas rédigés, puis enregistrez la suite ; rien n’est exécuté avant que vous lanciez une évaluation.
- **Anciens paramètres par défaut.** Les cas enregistrés avant le 15 octobre 2026 peuvent encore interdire les appels d’outil et vérifier strictement les chiffres inventés ; un Agent qui interroge des données peut alors échouer simplement en faisant son travail. La suite indique le nombre de cas concernés et propose **Passer aux paramètres actuels**. La mise à jour enregistre une nouvelle révision qui retire uniquement ces deux paramètres ; les résultats précédents restent inchangés.
- **Lire un contrôle de publication.** Au-dessus des changements par cas, l’onglet **Publier** indique combien de cas ont été exécutés et à quelle fréquence la nouvelle version et la version Live ont réussi. Une version affiche « pas encore de verdict » quand aucun de ses cas n’a abouti, par exemple parce qu’ils sont restés incertains.

## Traduire la présentation de l’Agent

Si la page de gestion propose des traductions de la présentation, choisissez la langue d’origine et une autre langue cible : chinois simplifié, anglais, allemand ou français. Rédigez vous-même le nom et la description traduits, ou demandez à l’IA de préparer un brouillon. Vérifiez le texte, enregistrez le brouillon, puis confirmez la traduction. Le texte confirmé prend effet lors de la prochaine publication et passe par les mêmes contrôles et approbations que l’original.

Dès qu’une traduction existe, la langue d’origine ne peut plus être modifiée. Si le texte original change, vérifiez et enregistrez de nouveau la traduction avant de la confirmer. La traduction concerne uniquement le nom et la description publics ; elle ne change ni les instructions ni les autorisations de l’Agent. Seul le propriétaire de l’Agent peut gérer ses traductions.

## Utiliser l’Agent dans une conversation

L’Agent principal l’invite lorsque la tâche convient et que les ressources et autorisations le permettent. **Saisir seulement son nom ne garantit pas qu’il traite la tâche.** Développez le déroulement de la réponse pour vérifier les Agents participants.

Avec les références rapides :

- Tapez `@` pour choisir un Agent et `/` pour un Skill. Sélectionner tout le texte puis taper ces caractères remplace la saisie et ouvre la liste ; les coller ne l’ouvre pas. Avec seulement un Skill, Bowra recherche un Agent adapté actuellement disponible. Vérifiez noms et pièces jointes avant l’envoi.
- Répondez aux précisions sur la carte de la conversation actuelle. Si une pièce jointe a été retirée ou modifiée, soumettez de nouveau la tâche. Changer d’Agent ou de Skill nécessite aussi une nouvelle tâche.
- Tab complète le texte ordinaire de l’historique ; il ne rétablit pas les anciens noms `@` et `/` comme références sélectionnées. Sélectionnez-les de nouveau et vérifiez le nombre de **Références**.
- **Modifier et envoyer** d’un ancien message remplit un champ vide sans écraser un brouillon. Les noms `@` et `/` deviennent du texte simple ; références et pièces jointes ne sont pas restaurées. Resélectionnez-les et vérifiez **Références**, notamment si les autorisations ont changé. Les cartes de précision d’origine se traitent toujours sur la carte sans nouvel envoi.
- Pour un Skill grisé sous `/`, lisez la raison. « Ce Skill n’a pas encore de contenu en lecture seule pour les références rapides » signifie qu’il n’est pas sélectionnable ici, même si l’Agent peut habituellement le lire. Son mainteneur doit ajouter ce contenu.

Pour un calcul détaillé de formule, demandez-le. Si l’Agent fournit une substitution vérifiable, la réponse affiche formule et résultat mis en forme ; les formules larges défilent horizontalement. Les formules non vérifiables ne sont pas remplies automatiquement.

## Si la création échoue

La carte conserve votre saisie pour correction ou nouvel essai. Après création, les outils et instructions se modifient dans la gestion de l’Agent. L’effet sur la version utilisée dépend de l’état affiché.
