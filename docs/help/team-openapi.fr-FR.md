# Gérer l’accès OpenAPI de l’équipe

Les propriétaires et administrateurs ouvrent **Paramètres de l’équipe → OpenAPI** lorsque cette fonction est disponible.

1. Dans la gestion des clés, créez une clé avec un nom, les autorisations nécessaires et une expiration. Une date vide utilise la durée de 30 jours par défaut. Choisissez le préréglage Agent CI pour un pipeline qui doit proposer et évaluer des modifications d’Agent.
2. Copiez le jeton complet dès son affichage et conservez-le avec les identifiants du système appelant. Il apparaît une seule fois.
3. La documentation de l’API indique le chemin de base, le point de terminaison, les autorisations et des exemples. Envoyez le jeton comme jeton Bearer et utilisez l’adresse accessible au système appelant, avec le préfixe de déploiement. La référence est téléchargeable en Markdown.
4. En cas d’échec, vérifiez la clé, l’action et le résultat dans les journaux, puis contrôlez les autorisations et l’expiration.

Désactivez ou révoquez une clé pour interrompre son accès, y compris celui du pipeline qui l’utilise. La régénération remplace l’ancien jeton. Mettez à jour le système appelant. La révocation conserve l’historique des appels.
