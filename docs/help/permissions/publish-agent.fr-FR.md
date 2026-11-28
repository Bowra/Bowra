# Publier un Agent

Ce guide concerne les Agents que vous avez créés et souhaitez partager avec des collègues. Enregistrer une copie de travail, choisir un public, passer une vérification et publier une version sont des étapes distinctes.

## Avant de commencer

Vérifiez que l’Agent vous appartient et que ses instructions, Skills, modèles, outils et accès aux données sont prêts pour le public souhaité. L’accès aux données de son auteur ne prouve pas celui des utilisateurs.

## Choisir le public et publier

1. Ouvrez **Créés par moi → Gérer la configuration**.
2. Choisissez le public et enregistrez les paramètres de partage.
3. Utilisez l’action proposée : **Lancer les contrôles de sécurité** ou **Préparer une version candidate** selon le parcours.
4. Vérifiez les avertissements, effectuez les changements requis et soumettez la version.
5. Si une approbation est nécessaire, le propriétaire ou un administrateur examine la version candidate et l’approuve ou la refuse. Il ouvre **Mes agents → Approbations de l’équipe** et choisit **Commencer l’examen** sur la demande. Lors d’un premier partage (jusqu’ici seul le propriétaire utilisait l’Agent), la configuration complète est dépliée, car l’approbation porte sur l’Agent entier. **Ce qui a changé** indique pour chaque élément seulement « Modifié » ou « Identique à la vN » ; la page d’approbation n’affiche pas le texte des consignes. Le résultat s’affiche sous les boutons ; **Copier le lien** permet d’envoyer le lien aux nouveaux membres, qui ne reçoivent pas de notification.

Sur la nouvelle page d’Agent, le seul bouton de test est **Tester cette modification** dans la barre de publication ; **Dernier test**, en haut de **Cas de test**, le résume. **Tester sur la vN publiée**, dans le « ⋯ » du groupe, exécute vos cas sur la seule version publiée et ne compte pas comme un test de cette modification. Avec des cas non enregistrés, **Tester cette modification** demande d’abord s’il faut enregistrer puis tester, ou tester avec les cas enregistrés uniquement.

Après **Préparer une version candidate**, la vérification des éléments de test concerne cette version préparée. Des tests manquants ou une panne d’infrastructure ne constituent pas des preuves prêtes : corrigez et réessayez. Après retrait de la candidate, les tests par défaut concernent de nouveau la version en service ; les anciens enregistrements restent consultables.

## Vérifier l’utilisation par les collègues

Le collègue doit trouver l’Agent publié depuis un compte et un espace inclus dans son public, le recruter et exécuter une tâche autorisée. Vérifiez disponibilité et résultat. Recrutement et approbation ne remplacent pas appartenance à l’équipe, accès à l’espace, disponibilité des dépendances et identité de données.

## Mettre à jour ou retirer

Enregistrez et publiez une nouvelle version lorsque le comportement change. Les collègues peuvent continuer avec la précédente jusqu’à l’approbation et la disponibilité de la nouvelle. Retirez le partage ou désactivez l’Agent lorsqu’il ne doit plus être utilisé ; vérifiez d’abord Skills et workflows dépendants.

Modifier une Skill utilisée par l’Agent ne nécessite pas de nouvelle publication : dès l’appel suivant, l’Agent publié utilise la dernière version approuvée d’une Skill d’équipe, ou la version actuelle de votre Skill personnelle.

Voir aussi : [Recruter et utiliser un Agent](use-agent.fr-FR.md) · [Configurer ses capacités](configure-capabilities.fr-FR.md).
