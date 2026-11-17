# Alertes et livraison des tâches

L’Agent d’une tâche planifiée peut signaler une exécution, par exemple quand une mesure franchit un seuil. Une note apparaît dans les notifications. Un signalement peut aussi apparaître dans le navigateur. Une alerte urgente peut envoyer un e-mail au propriétaire. Si un e-mail de résultats lui est déjà destiné, celui-ci peut porter la mention urgente et remplacer l’e-mail d’alerte supplémentaire. [Consulter les notifications](notification-center.fr-FR.md)

## Choisir quand recevoir les résultats

Modifiez la tâche planifiée puis activez l’e-mail de résultats dans les notifications. Choisissez l’envoi après chaque exécution réussie ou uniquement pour les exécutions signalées. Les autres exécutions restent visibles dans l’historique.

L’Agent décide si une exécution mérite un signalement en suivant les consignes de la tâche. Décrivez précisément les déclencheurs. Choisissez un envoi à chaque exécution si un résultat manqué aurait des conséquences. Une tâche peut envoyer une alerte par exécution, dans la limite de cinq par jour. Après cette limite, le résultat d’une autre exécution signalée peut toujours être livré normalement. Les e-mails urgents nécessitent une adhésion active et une messagerie d’équipe configurée.

L’e-mail de résultats marqué urgent est envoyé à la fin de l’exécution. Si l’exécution échoue, s’arrête pour demander une précision ou reste en cours dix minutes après l’alerte, l’e-mail d’alerte est envoyé séparément. Si les alertes sont désactivées pour votre déploiement, les résultats sont livrés à chaque exécution.
