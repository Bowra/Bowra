# Glisser-déposer, raccourcis et indications dans la conversation

Cet article présente le glisser-déposer, les raccourcis et les indications de la page de conversation. Pour la présentation de la page elle-même, consultez [Présentation de la page de conversation](conversation-workbench.fr-FR.md).

## Choisir un Agent, une méthode ou un résultat

- Lorsque les références rapides sont disponibles, saisissez `@` pour choisir un Agent pour ce message, ou `/` pour choisir un Skill, un résultat ou un outil. Sélectionnez un élément disponible dans le menu pour ajouter sa balise.
- Un **résultat (Outcome)** décrit ce que vous souhaitez obtenir. Un **Plugin** regroupe des outils et des Skills pour un Agent et se gère dans les ressources de celui-ci. Choisir un résultat n’ajoute pas de Plugin.
- Si la ressource propose des paramètres, ouvrez sa balise pour les modifier. Vos choix s’appliquent à ce message et ne modifient pas la configuration enregistrée de l’Agent.

## Glisser des fichiers dans la conversation

- Faites glisser des fichiers depuis votre ordinateur vers la colonne des messages et du champ de saisie. Un cadre pointillé apparaît. Lorsque **Déposer pour ajouter à ce message** s’affiche, relâchez : les fichiers deviennent des pièces jointes et commencent à être importés, comme avec **+** en bas à gauche du champ.
- Si l’ajout est impossible, le cadre devient rouge et explique pourquoi : uniquement des vidéos, de l’audio ou des types non compatibles, trop de pièces jointes, ou ajout temporairement indisponible. Relâcher n’ajoute alors rien.
- Pour plusieurs fichiers, le dépôt est possible si au moins un fichier est accepté. Ceux-ci sont importés ; les fichiers incompatibles apparaissent avec leur raison dans le champ de saisie et peuvent être retirés.
- Un message accepte jusqu’à 5 pièces jointes : tableurs, PDF, documents, images, texte et autres formats courants. La limite par fichier ordinaire est de 2 Gio avec un stockage objet, ou de 100 Mio avec un stockage local. Les fichiers Word et PowerPoint sont limités à 32 Mio, et les archives ZIP à 500 Mio.
- Vous pouvez déposer des fichiers pendant une réponse : ils seront joints au prochain message.
- La zone de résultats et les rapports n’acceptent pas ces dépôts. Glisser dans un rapport continue de réorganiser ses sections.
- Ajouter de nouveau le même fichier (même nom, même taille) ne crée pas de seconde pièce jointe : la pièce jointe existante est brièvement encadrée et les lecteurs d’écran annoncent « Ce fichier est déjà joint ».

## Modifier la largeur de la conversation et des résultats

- Sur ordinateur, faites glisser le séparateur entre les deux zones.
- Au clavier, atteignez le séparateur avec Tab, puis utilisez ← ou → pour ajuster progressivement. Début réduit la conversation au minimum ; Fin l’élargit au maximum. Les lecteurs d’écran annoncent sa proportion.
- Double-cliquez pour retrouver la largeur par défaut.
- Votre choix est conservé dans ce navigateur sur cet ordinateur.

## Agrandir les résultats avec un raccourci

- Appuyez sur ⌘⇧\ sur Mac, ou Ctrl + Maj + \ sur Windows, pour agrandir les résultats à toute la zone de travail. Répétez pour restaurer. L’infobulle du bouton d’agrandissement en haut à droite rappelle ce raccourci.
- Une bande étroite reste à gauche : sélectionnez-la pour revenir à la conversation. Esc ferme les menus et fenêtres ; il ne restaure pas cette disposition.
- ⌘\ ou Ctrl + \ sans Maj réduit ou déploie toujours la barre latérale.
- \ désigne la touche de barre oblique inverse d’un clavier américain. Elle peut porter un autre caractère sur votre clavier. Si le navigateur sait lire sa disposition, l’infobulle indique ce caractère ; sinon, elle mentionne la touche \ d’un clavier américain.
- Le raccourci est indisponible lorsqu’un menu, une liste, une fenêtre ou une bulle est ouverte. Fermez-la d’abord.
- Il fonctionne sur un écran d’ordinateur large lorsque des résultats existent. Safari utilise ⌘⇧\ pour afficher tous les onglets : si l’aperçu des onglets s’ouvre, utilisez le bouton d’agrandissement.

## Point sur Résultats

- Lorsque vous restez dans la conversation et qu’une section de rapport, un tableau de données ou un fichier apparaît, un point s’affiche sur **Résultats** en haut sur téléphone ou sur la bande réduite à droite sur ordinateur. Le lecteur d’écran annonce « nouveau contenu ».
- Le point disparaît lorsque vous ouvrez les résultats. Il reste présent après une actualisation tant que vous ne les avez pas consultés.

## Partager un échange avec le propriétaire d’un Agent comme cas d’évaluation

Lorsqu’un Agent d’équipe dont quelqu’un d’autre est propriétaire a répondu à votre question, le menu **⋯** de la réponse, à côté de **Copier**, propose **Partager avec le propriétaire comme cas d’évaluation**. Le propriétaire de cet Agent peut alors transformer cet échange précis, votre question et la réponse reçue, en cas de test qui vérifie les réponses futures de l’Agent. Rien d’autre de la conversation n’est partagé, et le propriétaire ne peut pas parcourir vos autres conversations.

- Le propriétaire voit votre nom et le début de votre question dans sa liste. Il ne lit la réponse que lorsqu’il en tire un cas.
- Pour retirer le partage, ouvrez le même menu et choisissez **Ne plus partager avec le propriétaire**. L’échange quitte la liste du propriétaire. Un cas qu’il en a déjà tiré est sa propre copie et reste dans sa suite d’évaluation ; contactez-le directement si vous souhaitez qu’il soit supprimé.
- Le partage prend aussi fin si vous supprimez la conversation ou quittez son Space.
- Le menu n’apparaît que pour les Agents d’équipe dont vous n’êtes pas propriétaire. Les réponses des Agents intégrés ou de vos propres Agents n’en ont pas ; vos propres conversations vous sont déjà accessibles.

## Signaler une mauvaise réponse d’un Agent d’équipe

Les réponses d’un Agent d’équipe ont un bouton pouce vers le bas à côté de **Copier**. Utilisez-le quand une réponse était fausse ou peu utile :

1. Sélectionnez le pouce vers le bas. Une petite boîte de dialogue s’ouvre. Tout y est facultatif ; envoyer sans rien remplir l’enregistre quand même.
2. Cochez les raisons qui s’appliquent : **Faits ou chiffres erronés**, **N’a pas répondu à ma question**, **Il manque quelque chose d’important**, **Trop succinct**, **Trop verbeux**, **Difficile à lire ou mal présenté** ou **Autre**. Ajoutez si vous le souhaitez un commentaire de 500 caractères au plus sur ce que vous attendiez.
3. Si l’Agent appartient à quelqu’un d’autre, la boîte affiche aussi **Autoriser le propriétaire de l’Agent à utiliser cet échange comme cas d’évaluation (le propriétaire verra la question, la réponse et votre retour)**, avec son nom. Elle n’est pas cochée, sauf si vous avez déjà partagé cet échange. Cochez-la seulement si vous voulez que le propriétaire voie cet échange : c’est le même consentement que **Partager avec le propriétaire comme cas d’évaluation** dans le menu **⋯**, et le propriétaire voit alors aussi vos raisons et votre commentaire. La décocher pour un échange déjà partagé arrête le partage.
4. Sélectionnez **Envoyer**. Le bouton reste mis en évidence et un message le confirme, avec **Annuler**. Annuler retire le pouce vers le bas et un partage que vous venez d’ajouter.

- Pour retirer le pouce vers le bas plus tard, sélectionnez de nouveau le bouton mis en évidence. Cela arrête aussi le partage avec le propriétaire ; vous pouvez repartager depuis le menu **⋯**.
- Sans la case cochée, votre pouce vers le bas reste privé : le propriétaire de l’Agent ne le voit pas.
- Les réponses des Agents intégrés n’ont pas de pouce vers le bas.

## Pourquoi Envoyer est indisponible

- Une courte indication sous le champ de saisie explique la raison, par exemple **Vous pourrez envoyer une fois les pièces jointes importées**. L’envoi redevient disponible lorsque la raison disparaît.
- Le lecteur d’écran annonce aussi cette raison sur le bouton Envoyer.

## Brouillons de réécriture et nouvel essai

- Lorsque vous demandez à l’IA de réécrire une section ou une sélection et fermez la fenêtre d’instructions avec ×, un clic extérieur ou Esc, votre instruction est conservée pour la prochaine ouverture sur la même section. Chaque section de chaque rapport possède son brouillon distinct, effacé après envoi. Ces brouillons ne survivent pas à l’actualisation de la page.
- En cas d’échec, **Réessayer** dans la barre de message relance le même texte avec la même instruction. Si le texte a changé entre-temps, la barre se ferme : sélectionnez de nouveau le texte souhaité.

## Clavier dans les rapports

- Pendant la modification, sélectionnez du texte et appuyez sur ⌥⏎, Option + Retour sur Mac ou Alt + Entrée sur Windows, pour atteindre la barre de mise en forme. Naviguez avec ← et →, activez avec Entrée ou Espace, puis Esc pour revenir au texte en conservant la sélection. Ce raccourci ne fonctionne pas pendant une composition de texte par une méthode de saisie.
- Dans le menu d’une section, utilisez ↑ et ↓, Entrée pour exécuter et Esc pour fermer. Les éléments indisponibles restent parcourables et le lecteur d’écran annonce leur raison.
- Sur un repère de citation, Entrée ouvre la source. Esc dans la fiche source la ferme et remet le focus sur le repère. La ligne source sous un graphique s’ouvre aussi avec Entrée ou Espace et se ferme avec Esc.
