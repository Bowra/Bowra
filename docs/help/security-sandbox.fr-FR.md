# Sécurité et environnement isolé hors ligne

L’exécution de code nécessite un bac à sable et un exécuteur compatibles, configurés séparément. L’aperçu du code Community ne constitue pas un déploiement de runtime validé. Les limites ci-dessous décrivent le contrat d’isolation pris en charge ; les exploitants doivent les vérifier sur le runtime réellement installé.

Les propriétaires et administrateurs règlent les durées par défaut et maximale dans **Paramètres de l’équipe › Agents › Limites d’exécution › Calculs dans le bac à sable**. Le maximum ne peut pas dépasser le plafond du déploiement affiché. Les changements enregistrés s’appliquent aux nouvelles tâches. Pour rétablir l’héritage, choisissez **Utiliser les valeurs du déploiement**, vérifiez puis enregistrez. Les tâches en cours gardent leurs limites initiales.

Un calcul isolé demande 120 secondes par défaut ; le plafond par défaut du déploiement est de 600 secondes. Les administrateurs peuvent les ajuster. Un dépassement du temps d’exécution signifie que la durée demandée a été dépassée ; un exécuteur inaccessible indique un échec de connexion. Si la réponse ou le nettoyage n’est pas confirmé, le résultat reste inconnu : vérifiez l’exécution d’origine avant de réessayer. Augmenter le délai ne répare pas un service indisponible.

Bowra exécute le code non fiable produit par l’IA dans un environnement contraint. Cet environnement constitue une couche de défense parmi plusieurs.

## Déroulement d’un calcul isolé

La tâche fournit explicitement ses fichiers d’entrée et une commande limitée. Un conteneur isolé reçoit uniquement les données nécessaires, applique les limites d’exécution et restitue les sorties autorisées. Il est détruit après la tâche. Celle-ci ne reçoit ni fichiers ou identifiants de l’hôte, ni connexion réseau générale.

## Sens de « hors ligne »

L’environnement ne dispose d’aucune route réseau sortante. Le code ne peut pas télécharger des packages, appeler une API externe, scanner le réseau interne ou transmettre des données par le réseau. Les dépendances prises en charge sont déjà présentes. Un service externe doit passer par une intégration explicite, autorisée séparément.

## Limites d’exécution

- **Identité et opérations système** : utilisateur sans privilèges root, appels système et capacités limités.
- **Fichiers** : uniquement les entrées et sorties de la tâche ; les montages de l’hôte ne sont pas modifiables.
- **Ressources et durée** : limites de processeur, mémoire, processus, sortie et durée ; nettoyage après exécution.
- **Service d’exécution** : il vérifie la demande et n’expose pas ses identifiants au code.

## Tentatives d’attaque courantes

Les appels réseau échouent faute de route. Lire l’hôte, effectuer des opérations privilégiées ou changer les montages échoue faute d’accès et de privilèges Linux. Les multiplications de processus, sorties massives et boucles longues sont bornées par les ressources et délais. Ces contrôles limitent les conséquences sans rendre tout code arbitraire exempt de risque.

## Responsabilités partagées

Fournissez le minimum de données et aucun secret. Les administrateurs d’équipe limitent intégrations et sources externes. Les responsables du déploiement maintiennent environnement, service d’exécution, noyau hôte et règles des conteneurs ; après un changement pertinent, ils vérifient isolation réseau, montages en lecture seule, absence de privilèges root, retrait de capacités et nettoyage après dépassement du délai.

Aucun système ne garantit qu’il ne sera jamais contourné. Bowra combine isolation, droits minimaux, entrées limitées, nettoyage et mises à jour pour qu’une défaillance d’une couche n’entraîne pas automatiquement une compromission de l’hôte ou des données.
