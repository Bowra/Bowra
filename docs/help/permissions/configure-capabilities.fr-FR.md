# Configurer les capacités d’un Agent

Ce guide aide une équipe à activer un Agent intégré ou ajuster son comportement pris en charge. Les Agents créés par les membres sont maintenus et publiés par leurs auteurs.

## Créer avec l’IA un assistant textuel

Si la vérification de brouillons IA est disponible, précisez que l’assistant travaille uniquement avec vos textes, sans requêtes de données ni accès web.

1. Vérifiez objectif et instructions.
2. Confirmez l’absence de besoin d’outils ou de Skills et le périmètre limité au texte fourni.
3. Choisissez **Créer un Agent**. Après modification du brouillon, enregistrez, vérifiez et confirmez de nouveau.

Pour des requêtes, fichiers ou scripts sans outil adapté, configurez les ressources ou passez en manuel. Une liste d’outils vide ne permet pas ces tâches. Si le brouillon IA textuel est indisponible, suivez les options manuelles proposées.

## Créer manuellement un assistant limité au texte fourni

Si la page autorise outils et Skills vides, vous pouvez créer un assistant de résumé, traduction ou révision :

1. Indiquez nom, description d’orientation et instructions limitant l’usage à votre contenu.
2. Laissez les outils vides et continuez. Les Skills peuvent aussi rester vides.
3. Vérifiez puis choisissez **Créer un Agent**. Les nouveaux Agents sont privés par défaut.

Sans cette indication, suivez les exigences de la page. Sans outils, l’assistant ne peut ni interroger les données, ni accéder aux services externes, ni exécuter des scripts. Ajoutez les ressources autorisées nécessaires. Les Agents existants suivent leur propre parcours d’enregistrement et de publication.

## Activer ou désactiver un Agent intégré

Propriétaires et administrateurs ouvrent **Agents → Gérer les Agents intégrés** et utilisent l’interrupteur de la ligne souhaitée. Lisez les conséquences si une confirmation apparaît. Attendez la mise à jour de l’état affiché. **Paramètres système** et **Personnalisé** permettent de repérer les adaptations de l’équipe.

La page contient uniquement les Agents fournis avec le produit. Les Agents créés, partagés ou obtenus ailleurs se gèrent sur leurs propres pages. Certains portent des capacités essentielles, comme l’unique Agent rédigeant les rapports du canevas : leur désactivation explique d’abord les conséquences. Elle concerne seulement l’équipe actuelle et reste réversible.

La liste comprend aussi **Coach** et l’**Agent de configuration**. Pour ces deux Agents, vous pouvez seulement les activer ou les désactiver et modifier leur nom et leur description. La boîte de dialogue masque les autres champs, car leur modèle, leurs Skills et leurs instructions sont fixés par le produit. Un Agent intégré désactivé n’apparaît plus dans la place de marché ni dans la liste @ du Chat, et ne reçoit plus de nouvelles tâches. Si vous modifiez un champ qu’un Agent n’autorise pas, l’enregistrement échoue et le message indique ce champ.

Si la configuration a changé entre-temps, la page recharge la dernière et réapplique votre action sans écraser les réglages récents. Si elle change encore immédiatement, un message vous invite à réessayer.

## Définir les limites d’exécution

La création et la configuration des Agents utilisent ces limites d’équipe, sans nombre d’étapes distinct par Agent.

Les propriétaires et administrateurs de l’équipe définissent les limites de tous les agents de l’équipe, intégrés et personnalisés, dans **Paramètres de l’équipe › Agents › Limites d’exécution** :

- **Durée maximale d’un tour** : durée maximale d’un tour de conversation. La valeur par défaut est de 30 minutes ; vous pouvez choisir de 10 minutes à 10 heures. Les agents s’arrêtent 5 minutes avant pour laisser le temps d’écrire la réponse.
- **Durée d’un appel au modèle** : suit par défaut le temps restant de l’Agent. Choisissez **Au plus** pour fixer une limite plus courte de 30 à 36 000 secondes. Cela ne prolonge pas le tour ; le délai de la connexion au modèle reste applicable.
- **Limite d’étapes par tâche** : nombre d’étapes qu’un agent peut effectuer pour une tâche. La valeur par défaut est **Illimité**. Un agent qui ne produit aucun nouveau résultat pendant plusieurs étapes est arrêté automatiquement ; une limite d’étapes est donc rarement nécessaire.
- **Appels d’outils par tour** : nombre total d’appels d’outils de tous les agents dans un tour. La valeur par défaut est 300 ; chaque outil compte de la même façon.

1. Ouvrez **Paramètres de l’équipe › Agents** et choisissez l’onglet **Limites d’exécution**.
2. Modifiez les valeurs voulues. Une valeur hors limites affiche la plage autorisée sous le champ, et Enregistrer reste indisponible.
3. Choisissez **Enregistrer**. Les limites s’appliquent dès le tour suivant ; les tours en cours ne sont pas concernés. Pour revenir aux valeurs par défaut, choisissez **Rétablir les valeurs par défaut**.

Lorsqu’une limite est atteinte, l’agent s’arrête et indique ce qui est terminé et ce qui reste ; la conversation le marque **Partiellement terminé**. Répondez « continuer » pour qu’il poursuive. Des limites plus élevées permettent d’en faire plus par tâche, mais l’attente et la consommation augmentent.

## Définir les limites du bac à sable et du HTML

Dans **Paramètres de l’équipe › Agents › Limites d’exécution**, les propriétaires et administrateurs peuvent aussi régler :

- **Calculs dans le bac à sable** : durée par défaut (initialement 120 secondes) et durée maximale (initialement 600 secondes). La valeur par défaut ne peut pas dépasser le maximum. Seul le responsable du déploiement peut augmenter le plafond affiché.
- **Rapports HTML** : taille maximale de chaque fichier HTML intégré, images et données incluses. La valeur initiale est 25 MiB, le maximum 50 MiB. Les fichiers volumineux et les tableaux complexes nécessitent plus de mémoire et de temps de traitement dans le navigateur. Les valeurs d’équipe enregistrées et les valeurs du déploiement restent applicables. Augmentez ce champ et enregistrez-le si une ancienne limite bloque l’aperçu. Ce réglage limite le fichier, pas les tokens produits par le modèle.

La page affiche les valeurs du déploiement et les plages autorisées. Après modification, choisissez **Enregistrer les limites du bac à sable et du HTML**. Pour rétablir l’héritage, choisissez **Utiliser les valeurs du déploiement**, vérifiez les valeurs proposées, puis enregistrez. L’exécution des tâches et le bac à sable/HTML s’enregistrent séparément. En cas de modification simultanée, vos saisies sont conservées ; abandonnez-les et rechargez les réglages avant une nouvelle modification.

Les changements du bac à sable s’appliquent aux nouvelles tâches. Les tâches en cours gardent leurs limites initiales. Les aperçus HTML utilisent le réglage de l’équipe du rapport au prochain chargement. Augmenter la durée aide un calcul lent, mais ne répare pas un service indisponible. Vérifiez toute exécution au résultat inconnu avant de réessayer.

## Responsabilités et Skills automatiques

1. Choisissez **Configurer** sur l’Agent intégré. Le formulaire regroupe **Nom et objectif**, **Exécution** et **Consignes de l’équipe** ; les actions restent visibles pendant le défilement.
2. Modifiez nom affiché, description d’orientation ou consignes d’équipe lorsque c’est proposé.
3. Choisissez sa responsabilité principale et les Skills à lire automatiquement.
4. Si nécessaire, ajustez **Niveau du modèle**, propre à cet Agent. Les étapes, la durée et les appels d’outils se règlent pour tous les agents dans l’onglet **Limites d’exécution** ; voir la section suivante.
5. Enregistrez, rouvrez et vérifiez.
6. Testez une tâche du périmètre avec un compte autorisé pour l’espace et les données.

Ouvrez **⋯ → Rétablir les valeurs par défaut** sur une ligne personnalisée pour annuler les adaptations de l’équipe.

Si l’enregistrement échoue à cause d’une version modifiée, réessayez sur la dernière configuration. Si le service n’est pas prêt ou est indisponible, demandez à l’administrateur système de vérifier la mise à niveau, puis réessayez après rétablissement. Si l’Agent a été retiré, sa ligne disparaît et aucune action n’est nécessaire. Lorsque les paramètres ne peuvent pas être lus de façon fiable, la page bloque les changements pour éviter de prendre les valeurs du produit pour celles de l’équipe.

Responsabilités et Skills aident à sélectionner l’Agent. Ils n’accordent pas d’autorisation sur les données et ne garantissent pas la configuration d’un outil externe. Les outils, formats de sortie et appels entre Agents définis par le produit ne peuvent pas être remplacés librement ici.

## Trouver le responsable d’une activation

| Besoin                                               | Responsable                                                                                                                     |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Modèle, recherche ou génération d’images             | Propriétaire ou administrateur, **Paramètres de l’équipe → Modèles**                                                            |
| Source d’outils ou connexion externe                 | Propriétaire ou administrateur, **Paramètres de l’équipe → Sources d’outils**                                                   |
| Connexion Datarc                                     | Propriétaire ou administrateur, **Paramètres de l’équipe → Sources de données** ; chaque membre garde ses propres autorisations |
| Instructions ou Skills d’un Agent créé par un membre | Auteur de l’Agent                                                                                                               |
| Capacité absente du produit                          | Responsable du produit                                                                                                          |

Un Outcome décrit un résultat promis et peut présenter preuves ou disponibilité. Un plugin d’équipe regroupe Tools et Skills sélectionnables par les auteurs. Aucun ne remplace les autorisations des fonctionnalités, outils ou données.
