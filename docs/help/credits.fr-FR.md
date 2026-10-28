# Consulter et attribuer des crédits

Community ne nécessite pas d’activation commerciale et n’impose pas de plafond d’utilisateurs. Les crédits restent des budgets d’usage administrés ; l’exploitant paie séparément les modèles et l’infrastructure. Un nom de formule désigne ici un niveau d’usage, pas un abonnement Community acheté.

Ce guide vous aide lorsqu’un message indique que vos crédits sont épuisés ou lorsque vous devez en attribuer à un membre. Les propriétaires et administrateurs d’équipe peuvent répartir les crédits de l’équipe.

## Fonctionnement des crédits

- Les analyses consomment des crédits pour les messages, les précisions apportées, les questions depuis les canaux connectés, les tests de workflows et les tâches planifiées.
- Le coût dépend du contenu réellement traité pendant un échange et est appliqué à sa fin.
- Les nouveaux travaux comptent séparément les jetons d’entrée, les entrées servies depuis le cache et les sorties de chaque modèle. Chaque nombre est multiplié par le coefficient enregistré pour l’appel, puis les résultats sont additionnés, divisés par 1 000 et arrondis une seule fois à l’entier supérieur. Les trois coefficients valent 1 par défaut ; les anciennes écritures gardent leur calcul initial. Si un fournisseur déclare les jetons en cache séparément de l’entrée, le résultat peut changer même avec trois coefficients à 1.
- Pour lancer une analyse, il faut à la fois un solde personnel disponible et des crédits dans le solde de l’équipe.
- Les allocations mensuelles sont réinitialisées avec l’abonnement. Les crédits inutilisés ne sont pas reportés.

## Vérifier un solde épuisé et reprendre

L’indicateur en haut à droite affiche les crédits restants. Sous 20 % de l’allocation, il change de couleur et indique la part restante ; sous 5 %, il passe à la couleur d’alerte et signale que les crédits sont presque épuisés. Sélectionnez l’indicateur (ou appuyez sur Entrée lorsqu’il a le focus) pour ouvrir un résumé : votre forfait, une barre montrant la part de l’allocation de la période déjà utilisée, la date de réinitialisation de l’allocation et un avertissement lorsque les crédits deviennent bas. Appuyez sur Échap ou sélectionnez ailleurs pour le fermer. Sur un téléphone ou dans une fenêtre étroite, l’indicateur se trouve en bas du menu de navigation et le résumé s’ouvre depuis le bas de l’écran. Si votre rôle permet de consulter les crédits de l’équipe, le résumé propose aussi un lien vers le détail de votre propre consommation. **Paramètres personnels → Compte** présente le solde et l’allocation mensuels. Lorsque les crédits sont épuisés, un nouveau travail ne peut pas démarrer. Dans une conversation déjà en cours, les agents s’arrêtent à l’étape suivante, indiquent ce qui est terminé et ce qui reste, et vous pouvez poursuivre après l’ajout de crédits ; la consommation jusque-là peut rendre le solde affiché légèrement négatif. Après une attribution, relancez la même carte de précision ou le complément ayant échoué : vous n’avez pas besoin de recréer la question.

Demandez des crédits au propriétaire ou à un administrateur de l’équipe.

## Attribuer des crédits à un membre

Pour les propriétaires et administrateurs d’équipe :

1. Ouvrez **Console d’administration → Crédits et facturation**.
2. Recherchez le membre dans **Utilisateurs** et choisissez **Attribuer des crédits**.
3. Saisissez le montant, ajoutez éventuellement un motif et confirmez. Le montant est déduit du solde de l’équipe.
4. Si ce solde est insuffisant, choisissez **Ajuster les crédits de l’équipe**, indiquez la nouvelle allocation et le motif, puis revenez à l’attribution.

Vous pouvez aussi modifier directement l’allocation, l’abonnement ou le solde d’un membre. Vérifiez que son **Solde / allocation** est à jour. Un solde négatif est d’abord compensé. Un solde d’équipe à zéro bloque les nouvelles analyses pour tous, même si certains membres disposent encore de crédits personnels.

Dans le détail de consommation d’un membre, la colonne du modèle indique les modèles enregistrés pour les nouveaux débits. Développez une ligne pour voir les trois catégories de jetons et leurs coefficients ; plusieurs modèles apparaissent séparément. Une consommation sans appel de modèle confirmé apparaît comme modèle inconnu avec des coefficients de 1. Les anciennes écritures peuvent ne pas indiquer de modèle. L’export CSV du membre contient également les modèles et leur consommation enregistrée.

Voir aussi : [Rôles et autorisations de l’équipe](permissions/team-roles.fr-FR.md).
