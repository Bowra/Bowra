# Ajouter un outil MCP d’équipe

Les propriétaires et administrateurs peuvent ajouter un outil MCP dans **Paramètres de l’équipe → Sources d’outils** pour que les Agents utilisent certaines capacités d’un service externe. Vous ajoutez le service et ses outils sélectionnés ; cela ne donne pas automatiquement aux membres un accès à un compte de ce service.

## Ajouter le service et sélectionner les outils

1. Ouvrez **Paramètres de l’équipe → Sources d’outils → Ajouter un outil MCP**.
2. Choisissez **Serveur MCP** et saisissez son adresse. Si nécessaire, fournissez les identifiants ou terminez l’autorisation demandée.
3. Attendez le chargement des outils, puis sélectionnez ceux nécessaires aux Agents.
4. Saisissez un nom de source et confirmez. La liste permet ensuite de la consulter, vérifier, désactiver ou modifier.

Les identifiants enregistrés ne peuvent pas être relus ou affichés. Utilisez uniquement des services de confiance et les droits minimaux. Le cloud exige des adresses HTTPS publiques ; les déploiements internes acceptent les adresses internes.

## Gérer les outils ajoutés

Vérifiez la connexion, modifiez nom et identifiants ou désactivez la source. Une désactivation bloque les nouveaux appels ; un appel externe en cours peut se terminer. Supprimer la source efface aussi ses identifiants et ne peut pas être annulé.

Si des Agents, tâches en cours ou planifications l’utilisent, vérifiez les conséquences et résolvez les dépendances avant désactivation ou suppression. Les services externes peuvent exécuter des actions irréversibles : vérifiez les règles d’utilisation des données de l’équipe et les autorisations du service avant activation.

## Actualiser la liste des outils

Lorsqu’un service MCP ajoute ou retire des outils, choisissez **Actualiser la liste des outils** dans les actions de la source (dans le menu **⋯** à droite de la carte). Bowra récupère d’abord les outils actuellement proposés par le service et affiche les différences avec la liste enregistrée :

- **Outils ajoutés** : outils désormais proposés par le service. Si vous n’avez sélectionné qu’une partie des outils lors de l’ajout de la source, les nouveaux outils ne sont pas importés automatiquement.
- **Outils supprimés du serveur** : outils que le service ne propose plus et qui n’avaient pas été importés. Leur suppression n’a aucun effet sur les Agents.
- **Outils importés qui ne fonctionneront plus** : outils importés que le service ne propose plus. Après la mise à jour, les Agents ne pourront plus les appeler. L’aperçu indique également les Agents, tâches en cours et tâches planifiées concernés ; ces dernières sont mises en pause jusqu’à une nouvelle confirmation de leurs dépendances. Si tous les outils importés ont été supprimés, la mise à jour désactive la source.
- **Outils modifiés** : outils toujours proposés dont la description ou les paramètres ont changé. Pour chacun, l’aperçu indique ce qui a changé et s’il est importé. Après la mise à jour, les Agents utilisent la nouvelle version dès leur prochain appel à un outil importé.

Aucune modification n’est appliquée avant que vous sélectionniez **Mettre à jour la liste des outils** ; **Annuler** conserve l’état actuel. Si la liste des outils ou leur utilisation change pendant votre vérification, Bowra la récupère à nouveau et vous demande d’examiner les nouvelles modifications. Si aucun outil n’a été ajouté, supprimé ni modifié, l’aperçu indique que la liste des outils est à jour.

Un service MCP qui nécessite une autorisation doit être autorisé avant l’actualisation de sa liste d’outils.

## Choisir les outils à importer

Pour importer des outils que vous n’aviez pas sélectionnés lors de l’ajout de la source, ou des outils qu’une actualisation vient d’ajouter, choisissez **Choisir les outils** dans les actions de la source (dans le menu **⋯** à droite de la carte). Lorsqu’une source n’importe qu’une partie des outils et qu’une actualisation en ajoute de nouveaux, le message de confirmation propose aussi **Choisir les outils**, avec les nouveaux outils marqués **Nouveau**.

La liste affiche tous les outils de la dernière liste enregistrée ; les outils importés sont cochés. Cochez ceux que les Agents doivent utiliser, puis sélectionnez **Enregistrer**. Gardez au moins un outil sélectionné ; pour n’en utiliser aucun, désactivez plutôt la source.

Si vous décochez des outils actuellement importés, Bowra affiche d’abord l’impact : les outils qui seront supprimés de la sélection ainsi que les Agents, tâches en cours et tâches planifiées qui utilisent cette source. Aucune modification n’est appliquée avant que vous sélectionniez **Confirmer et enregistrer** ; **Retour** ramène à la liste. Après l’enregistrement, les Agents ne peuvent plus appeler ces outils et les tâches planifiées concernées sont mises en pause jusqu’à une nouvelle confirmation de leurs dépendances.
