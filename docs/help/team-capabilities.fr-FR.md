# Utiliser les plugins de l’équipe

Un plugin est une sélection réutilisable de Tools et de Skills. Les administrateurs d’équipe lui donnent un nom et une description, puis choisissent les ressources disponibles. Par exemple, un plugin « Recherche » peut réunir des outils de recherche web et un Skill de recherche.

Les noms intégrés sont en anglais par défaut et utilisent les libellés chinois disponibles lorsque la langue de l’interface est le chinois. Les noms que vous donnez à vos propres Tools, Skills, plugins et agents restent inchangés.

## Consulter et utiliser

Les propriétaires et administrateurs peuvent ouvrir **Centre des capacités → Plugins** pour consulter les sélections de l’équipe et leur contenu. Lors de la création ou de la modification de votre Agent, ouvrez **Skills et outils**, sélectionnez un plugin et enregistrez. Vous pouvez également sélectionner individuellement des Tools et Skills.

L’Agent utilise le contenu le plus récent à sa prochaine exécution. Une exécution en cours conserve sa sélection initiale. Si un plugin est désactivé, les Agents qui l’utilisent continuent de fonctionner, sans les Tools et Skills qu’il apporte ; les Tools sélectionnés individuellement ne sont pas concernés.

Un plugin facilite la sélection des ressources. L’exécution d’un Tool ou d’un Skill dépend toujours de leur disponibilité et des autorisations de l’utilisateur actuel, de l’Agent et de la ressource.

## Désactiver des plugins et des outils intégrés

- **Plugins** : chaque plugin a un interrupteur **Moi** qui ne s’applique qu’à vous ; la notification en bas permet d’annuler. Les responsables et administrateurs d’équipe voient aussi un interrupteur **Équipe**. Avant un changement pour toute l’équipe, Bowra liste les agents concernés (y compris les agents intégrés) et les tâches planifiées ; le changement s’applique dès leur prochaine exécution. Quand l’équipe désactive un plugin, les membres ne peuvent pas le réactiver pour eux-mêmes.
- **Outils intégrés** : dans **Centre des capacités → Outils**, les outils intégrés sont regroupés par usage. Les responsables et administrateurs d’équipe peuvent activer ou désactiver chacun d’eux. Un outil désactivé n’est plus disponible pour aucun agent, même via un plugin. Avant d’activer un outil qui écrit ou envoie du contenu à l’extérieur (envoyer un e-mail, écrire dans Google Docs, etc.), vous devez confirmer le risque. Sélectionnez **Détails** sur un outil pour voir à quoi il sert et, pour les responsables et administrateurs, quels agents et tâches planifiées l’utilisent ; l’option **Toujours confirmer** y est déjà enregistrée, mais ne prend effet qu’une fois la confirmation par appel disponible.
- Désactiver ne modifie jamais la configuration d’un agent ; réactiver rétablit tout automatiquement.

## Gérer les plugins

La liste existante apparaît dans **Paramètres de l’équipe → Plugins**. Si elle est vide, choisissez **Créer un plugin** en haut à droite. Sélectionnez le nom d’un plugin pour le modifier ou **Annuler** pour revenir à la liste. Saisissez un nom et une description, sélectionnez des Tools et Skills, puis enregistrez.

Les outils sont répartis entre **Outils ajoutés** et **Outils système**. Les premiers incluent les connexions tierces intégrées activées et les sources d’outils de l’équipe. Chaque opération indique sa connexion pour choisir uniquement celles nécessaires. Les opérations désactivées ou indisponibles ne peuvent pas être ajoutées. Si un outil sélectionné devient indisponible, désélectionnez-le avant d’enregistrer.

La modification d’un plugin actualise les ressources des Agents concernés à leur prochaine exécution. Avant de le désactiver, prévenez les propriétaires des Agents pour qu’ils adaptent leur configuration.

Pour supprimer définitivement un plugin, choisissez **Supprimer** dans son entrée, vérifiez le nombre d’Agents concernés et confirmez. Ses Tools et Skills sont immédiatement retirés de leurs prochaines exécutions ; les exécutions en cours conservent leur sélection initiale. Les brouillons d’Agents perdent également cette sélection. Les anciennes versions restent consultables, mais restaurer une version ne resélectionne pas un plugin supprimé.

Un **Outcome** décrit le résultat qu’un Agent promet, comme « écrire un rapport ». Il est distinct de la sélection de ressources d’un plugin.
