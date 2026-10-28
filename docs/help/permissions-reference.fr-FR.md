# Référence des autorisations : rôles et actions

Cette page aide à identifier rapidement le responsable d’une action. Les autorisations d’équipe, d’espace, de ressources, de plateforme et de données sont vérifiées séparément.

## Quatre règles

1. Le rôle d’équipe concerne l’administration globale ; il ne donne pas automatiquement accès aux espaces ni aux données métier.
2. Le rôle d’espace contrôle la collaboration locale ; il ne rend pas auteur de chaque Agent ou Skill.
3. Le public et l’état de publication d’un Agent déterminent sa découverte et son utilisation. La modification reste à l’auteur ou au gestionnaire désigné.
4. Les requêtes utilisent l’identité de données du membre actuel. Partager un rapport ou un Agent ne délègue jamais cette identité.

## Responsables

| Objet ou action                                    | Responsable principal                            | Précisions                                                                                                                  |
| -------------------------------------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| Membres, services d’équipe, validations            | Propriétaire ou administrateur de l’équipe       | Cela ne transfère pas l’identité de données personnelle d’un autre membre.                                                  |
| Membres et paramètres de l’espace                  | Administrateur de l’espace                       | Les administrateurs d’équipe ne rejoignent pas tous les espaces automatiquement.                                            |
| Contenu et versions d’Agents créés par des membres | Auteur de l’Agent                                | Il choisit le public et publie ; une approbation peut être requise.                                                         |
| Activation et configuration d’Agents intégrés      | Propriétaire ou administrateur de l’équipe       | Les outils et formats de sortie du produit ne sont pas librement remplaçables.                                              |
| Modification et activation personnelle d’un Skill  | Auteur ou membre concerné                        | Un interrupteur personnel change seulement cet usage.                                                                       |
| Approbation ou état d’un Skill d’équipe            | Propriétaire ou administrateur de l’équipe       | Un membre ne peut pas contourner une désactivation globale.                                                                 |
| Définitions d’Outcomes                             | Responsable du produit ou du déploiement         | Les rôles d’équipe configurent les fonctions prises en charge ; ils ne créent pas de contrats d’Outcome non pris en charge. |
| Plugins d’équipe                                   | Propriétaire ou administrateur de l’équipe       | Les membres consultent et choisissent ces ensembles de Tools et Skills pour les Agents qu’ils possèdent.                    |
| Données métier externes                            | Administrateur des données et identité du membre | Vérifiez séparément source et autorisation personnelle.                                                                     |

## Agent, plugin, Outcome et Skill

Un **Agent** réunit un rôle de travail et ses dépendances. Un **plugin** regroupe des Tools et Skills réutilisables sous la gestion de l’équipe. Un **Outcome** décrit un résultat promis et peut présenter preuves ou disponibilité. Ils n’accordent pas d’autorisation. Un **Skill** est une méthode réutilisable. Enregistrer un brouillon, publier une version, approuver une publication d’équipe et activer une méthode pour une personne sont distincts et relèvent de responsables différents.

Pour un refus, identifiez ressource et opération, puis vérifiez équipe, espace, ressource, capacité et données dans cet ordre. Voir [Résoudre les problèmes d’accès](permissions/troubleshoot-access.fr-FR.md).
