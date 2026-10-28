# Se connecter, créer un compte et réinitialiser son mot de passe

Les pages de connexion, d’inscription, de réinitialisation et d’invitation utilisent l’identité Bowra et suivent le réglage clair ou sombre de votre appareil.

## Se connecter

1. Ouvrez l’adresse fournie par votre équipe. Si Feishu est configuré, choisissez « Se connecter avec Feishu » et suivez les instructions.
2. Pour utiliser une adresse e-mail et un mot de passe, saisissez-les sous « ou se connecter avec une adresse e-mail » et choisissez « Se connecter ».
3. Le bouton en forme d’œil, au bout du champ du mot de passe, affiche ou masque le mot de passe.

Si le format de l’adresse e-mail est incorrect ou qu’un champ est vide, le message s’affiche sous ce champ. Lorsque vous voyez « L’adresse e-mail ou le mot de passe est incorrect », votre adresse e-mail reste saisie et le mot de passe est effacé : saisissez simplement le mot de passe de nouveau. Pour protéger les comptes, la page n’indique pas lequel des deux est faux. Après trop de tentatives, patientez comme l’indique le message, puis réessayez.

Une fois connecté, vous revenez à la page que vous essayiez d’ouvrir.

## Adresse e-mail pas encore vérifiée

« Vérifiez d’abord votre adresse e-mail » signifie que l’e-mail de vérification de ce compte n’a pas encore été ouvert. Ouvrez le lien de cet e-mail, puis connectez-vous. S’il n’est pas arrivé, choisissez « Renvoyer l’e-mail de vérification » (un nouvel envoi est possible après 60 secondes) et vérifiez votre dossier de courrier indésirable.

## Créer un compte

« S’inscrire » n’apparaît que là où l’inscription publique est autorisée. Si vous voyez « Demandez une invitation à l’administrateur de votre équipe », demandez une invitation à un administrateur.

1. Saisissez votre adresse e-mail et un mot de passe. Le nom affiché est facultatif.
2. Le mot de passe doit comporter au moins 8 caractères, avec des lettres et des chiffres ; chaque exigence affichée sous le champ est cochée au fil de la saisie.
3. Saisissez de nouveau le mot de passe sous « Confirmer le mot de passe », puis choisissez « Créer un compte ».

Si votre adresse e-mail doit être vérifiée, vous verrez « Consultez votre boîte de réception ». Si vous vous êtes trompé d’adresse, choisissez « Revenir et la modifier ».

Si la page affiche plutôt « Votre compte est prêt », votre compte a été créé mais l’e-mail de vérification n’a pas encore pu être envoyé. Choisissez « Renvoyer l’e-mail de vérification » pour réessayer ; pendant environ 15 minutes après l’inscription, la page affiche le résultat de chaque essai ; ensuite, elle confirme seulement que le renvoi a été demandé. Patientez une minute entre deux essais. S’il n’arrive toujours pas, demandez à l’administrateur de votre équipe de vérifier les paramètres de messagerie (ou l’adresse d’accès à l’application, si la page l’indique) en lui donnant la référence affichée.

## Mot de passe oublié

1. Sur la page de connexion, choisissez « Mot de passe oublié ? », saisissez l’adresse e-mail avec laquelle vous vous connectez, puis choisissez « Envoyer le lien de réinitialisation ».
2. Lorsque vous voyez « Consultez votre boîte de réception », ouvrez le lien de l’e-mail. Pour protéger les comptes, la page affiche le même message, que l’adresse soit enregistrée ou non.
3. Sur « Définir un nouveau mot de passe », saisissez deux fois le nouveau mot de passe et choisissez « Enregistrer le nouveau mot de passe ».
4. Vous verrez « Mot de passe mis à jour ». Un lien de réinitialisation que vous avez demandé vous-même vous connecte : choisissez alors le bouton « Ouvrir ». Pour un lien créé pour vous par un administrateur, choisissez « Aller à la connexion » et connectez-vous avec le nouveau mot de passe.

Les liens de réinitialisation ne fonctionnent qu’une fois et expirent. Si vous voyez « Ce lien ne fonctionne plus », choisissez « Demander un nouveau lien de réinitialisation ».

« Impossible d’envoyer des e-mails pour le moment » signifie que votre équipe n’a pas activé l’envoi d’e-mails. Adressez-vous au propriétaire de votre équipe ou à un administrateur : ils peuvent créer pour vous un lien de réinitialisation du mot de passe depuis la page Membres.

## Accepter une invitation

Ouvrez le lien de l’e-mail d’invitation. La page indique l’équipe ou l’espace auquel vous êtes invité, votre rôle, l’adresse e-mail invitée et la date d’expiration de l’invitation. Si la personne qui vous a invité a un nom affiché, la page indique aussi qui vous a invité.

- Pas encore de compte : choisissez « Créer un profil », définissez un mot de passe pour l’adresse e-mail invitée et continuez. L’adresse e-mail invitée est fixée par l’invitation et ne peut pas être modifiée.
- Vous avez déjà un compte : choisissez « Se connecter à un compte existant », puis revenez à l’invitation une fois connecté.
- Connecté avec un autre compte : choisissez « Se déconnecter et changer de compte » et utilisez le compte invité.

Si l’invitation a expiré ou a été révoquée, demandez à la personne qui vous a invité de vous en envoyer une nouvelle. Si la page indique que l’invitation a déjà été acceptée, connectez-vous simplement. Une invitation donne uniquement accès à l’équipe ou à l’espace ; elle ne modifie pas les autorisations sur les sources de données comme Datarc.

## Changer de langue

Utilisez le bouton de langue en bas de la page. La page se recharge dans la nouvelle langue ; votre adresse e-mail reste saisie, mais vous devrez saisir de nouveau le mot de passe.

## Premier démarrage de Bowra Community

Une nouvelle installation Community ouvre la configuration du premier propriétaire. Demandez le justificatif de configuration à l’exploitant du serveur, puis saisissez le nom de l’équipe, votre e-mail et un mot de passe fort. L’exploitant obtient ce justificatif avec `python -m bowhead_gateway.installation setup-token` dans l’environnement de l’application. Gardez-le confidentiel.

Connectez-vous ensuite normalement et configurez les modèles de l’équipe. Cette procédure ne crée le premier propriétaire qu’une fois ; les autres membres rejoignent l’équipe via son administration. Community ne nécessite ni activation commerciale ni renouvellement et n’impose pas de plafond d’utilisateurs. Les autorisations et budgets d’usage configurés restent applicables.
