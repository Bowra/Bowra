# Google Workspace

Une fois Google Workspace connecté, les Agents peuvent lire les Google Docs, Sheets et Slides que vous partagez, consulter votre agenda et réécrire des résultats dans Google lorsque vous le demandez explicitement (sans confirmation supplémentaire), toujours avec votre propre accès Google. Chaque membre n’utilise que sa propre autorisation ; personne d’autre, pas même les administrateurs de l’équipe, ne peut lire ni utiliser vos données Google.

## Administrateurs : configurer la connexion de l’équipe

Seuls les propriétaires et administrateurs de l’équipe peuvent configurer la connexion. Elle utilise l’application Google Cloud de votre entreprise ; Bowra ne fournit pas d’application partagée.

1. Dans la console Google Cloud, créez un client OAuth de type « Application Web » avec le type d’utilisateur « Interne » (votre organisation uniquement).
2. Ouvrez **Paramètres de l’équipe → Outils → Outils tiers intégrés** et choisissez Google Workspace. Copiez l’**URI de redirection autorisé** affiché dans « URI de redirection autorisées » et l’**Origine JavaScript autorisée** dans « Origines JavaScript autorisées ». Si la page demande d’abord une adresse d’accès, enregistrez une adresse HTTPS sous **Paramètres de l’équipe → Entrée publique**.
3. Déclarez les champs d’application listés sur l’écran de consentement OAuth.
4. De retour dans Bowra, saisissez l’ID client, le secret client et le domaine Google Workspace de votre entreprise, puis activez les capacités nécessaires :
   - Base (obligatoire) : lire et écrire les fichiers choisis par les membres dans Bowra ; créer de nouveaux Docs, Slides et Sheets ;
   - Lecture par lien : lire tout Doc, Sheet ou Slides dont un membre fournit le lien ;
   - Modifier les Docs et Slides existants, Écrire dans les Sheets existants : modifier les fichiers existants que le membre peut éditer ;
   - Agenda : consulter les événements de l’agenda du membre (désactivé par défaut).
5. Pour utiliser « Sélectionner depuis Google Drive » dans les conversations et la base de connaissances, créez dans Google Cloud une clé API de navigateur dont les restrictions de sites incluent l’adresse de votre équipe et `https://docs.google.com/*`, puis saisissez la clé API et le numéro de projet dans Bowra.
6. Cliquez sur **Autoriser avec mon compte Google**, effectuez une autorisation avec votre propre compte d’entreprise pour vérifier la configuration, puis activez la connexion.

La carte indique combien de membres ont donné leur autorisation. Changer l’ID client ou le secret client révoque l’autorisation Google de tous les membres ; chacun doit se reconnecter. Bowra ne demande jamais à lire les e-mails ni à parcourir l’ensemble du Drive.

Si votre propre proxy inverse ou passerelle de sécurité applique une politique de sécurité du contenu, autorisez `accounts.google.com`, `apis.google.com` et `docs.google.com` ; sinon « Sélectionner depuis Google Drive » ne pourra pas s’ouvrir.

## Membres : connecter votre compte Google

Une fois Google activé pour votre équipe, cliquez sur **Connecter Google** dans **Mes connexions** et autorisez avec le compte Google d’entreprise correspondant à votre e-mail de connexion Bowra. L’autorisation est refusée si les e-mails diffèrent ou si le compte n’appartient pas au domaine de votre entreprise.

Vous pouvez vous déconnecter à tout moment dans « Mes connexions ». Lorsque votre autorisation expire ou qu’un administrateur active une nouvelle capacité, la carte vous invite à vous reconnecter ou à mettre à jour votre autorisation.

## Utilisation dans une conversation

- Lire des fichiers : collez le lien d’un Google Doc, Sheet ou Slides, ou choisissez **Sélectionner depuis Google Drive** dans le menu **+** de la zone de saisie (sous **Plus d’outils** sur petit écran) ; les fichiers choisis sont insérés au curseur sous forme de lignes « nom lien ». L’Agent le lit avec votre accès ; les fichiers longs sont lus par parties.
- Consulter votre agenda : posez simplement la question, par exemple « Quelles réunions ai-je la semaine prochaine ? ». Votre équipe doit avoir activé la capacité Agenda.
- Réécrire dans Google : demandez-le explicitement, par exemple « Rédige cette analyse dans un Google Doc » ou « Ajoute les résultats à cette feuille ». L’Agent écrit directement ; s’il ne sait pas quel fichier ou quelle section modifier, ou si la plage cible d’une feuille contient déjà des données, il vous pose d’abord la question. L’écriture n’a lieu que dans les conversations web. Après une écriture, une carte du fichier apparaît dans le canevas à droite, avec l’heure de l’écriture : par défaut, elle montre un instantané du contenu à ce moment-là. Basculez entre **Instantané** et **Aperçu en direct** sur la carte pour voir la version actuelle dans Google (votre navigateur doit être connecté au compte Google correspondant, et certains navigateurs ne peuvent pas l’afficher ; une ligne sous l’aperçu rappelle toujours que, si rien ne s’affiche, Google n’autorise peut-être pas l’intégration ou demande une connexion : revenez alors à l’instantané ou ouvrez le fichier dans Google), ou choisissez **Ouvrir dans Google** pour ouvrir le fichier dans un nouvel onglet.

Avant d’écrire, Bowra vérifie que les chiffres proviennent des données récupérées pendant ce tour et refuse d’écrire sinon. Si une demande d’écriture a été envoyée mais que son résultat est inconnu, la réponse vous invite à vérifier dans Google ; rien n’est relancé automatiquement. Vous pouvez annuler une erreur grâce à l’historique des versions de Google Docs ou Slides.

## Importer dans la base de connaissances

Dans la base de connaissances, ouvrez la fenêtre de téléversement, cliquez sur **ou Importer depuis Google Drive** sous la zone de dépôt, puis sélectionnez des fichiers. Au plus 20 fichiers sont importés à la fois ; importez les autres de nouveau. Les Google Docs, Sheets et Slides sont convertis dans les fichiers Office correspondants ; les fichiers de plus de 10 Mo ne peuvent pas être exportés depuis Google, divisez-les ou enregistrez-les séparément auparavant. Les fichiers importés ne restent pas synchronisés avec l’original ; importez-les de nouveau après une modification.

## Questions fréquentes

- Pas de bouton « Connecter Google » : votre équipe n’a pas encore activé Google. Contactez un administrateur de l’équipe.
- Un fichier ne s’ouvre pas : vous n’y avez peut-être pas accès, ou votre équipe n’a pas activé « Lecture par lien ». Choisissez-le plutôt avec **Sélectionner depuis Google Drive**.
- « Sélectionner depuis Google Drive » ne s’ouvre pas : ouvrez Bowra via l’adresse enregistrée de votre équipe, ou demandez à un administrateur de vérifier les restrictions de sites de la clé API.
