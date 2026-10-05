# Journal des versions

## 1.6.1 — 2026-10-05

- **Mises à jour vraiment automatiques** : vérification à chaque démarrage puis toutes les
  6 heures (au lieu d'une fois par jour), téléchargement en arrière-plan (empreinte SHA-256
  vérifiée) et installation à la fermeture de Débarras — sans fenêtre à valider. Un bouton
  dans la barre d'état permet de redémarrer tout de suite. Pour être prévenu avant chaque
  mise à jour : *Fichier › Paramètres*.
- **Steam dans Program Files** : les jeux installés dans `C:\Program Files (x86)\Steam` étaient
  classés *Système* ; ils sont désormais *Logiciel*, comme les autres programmes installés.
  Seuls la racine de Program Files et les composants de Windows (Common Files, WindowsApps…)
  restent bloqués.

## 1.6.0 — 2026-10-05

Plus simple, plus complet, d'après vos retours.

- **Mode simple par défaut** : l'accueil montre tous les disques du PC (place libre, dernière
  analyse) avec un bouton *Analyser* ; seuls les onglets Accueil et Nettoyage restent visibles.
  Tous les onglets et le choix libre d'un dossier : *Affichage › Mode avancé*.
- **Nettoyer automatiquement** : un bouton met à la corbeille, après un seul récapitulatif, ce
  qui est recréé sans contrepartie (fichiers temporaires, caches des navigateurs fermés,
  rapports d'erreurs). Jamais le cache graphique ni un navigateur ouvert.
- **Disques entiers** : Program Files et AppData sont désormais analysés (seul le dossier
  Windows reste à part) ; la carte d'un disque montre aussi l'*espace libre* et le *non
  analysé*, pour que le total corresponde à la taille réelle du disque. Les paramètres
  d'exclusion par défaut des versions précédentes sont mis à jour automatiquement.
- **Dossiers inaccessibles** : lien *Analyser en administrateur* pour relancer Débarras avec
  les droits nécessaires (les dossiers système restent protégés).
- Après la mise à jour, la première comparaison avec le scan précédent compte comme « nouveaux »
  les dossiers jusque-là exclus (Program Files, AppData) : relancez l'analyse une fois.

## 1.5.4 — 2026-10-04

- Résumé « Depuis le scan précédent » : hors bibliothèque de jeux, chaque hausse est nommée
  par son dossier parent et le dossier lui-même (« file_analyzer › dist » au lieu de
  « Users › dist »).

## 1.5.3 — 2026-10-04

- Résumé « Depuis le scan précédent » : libellés homogènes (« Utilisateur › Téléchargements »).
- README : captures d'écran (réalisées sur un disque de démonstration fictif).

## 1.5.2 — 2026-10-04

- **Jeux mieux reconnus** : un jeu qui range ses bibliothèques dans un sous-dossier
  (`Binaries\Win64`, `bin`…), comme Rocket League ou VALORANT, est désormais classé
  *Logiciel* en entier, et non plus *Vos fichiers*.
- **« Depuis le scan précédent »** : hausses regroupées par jeu ou logiciel, sans répéter un
  dossier et ses sous-dossiers (« Path of Exile 2 +140 Go »).
- **Synthèse de l'accueil** : elle ne peut plus afficher 0 partout quand l'analyse est relancée
  avant la fin de la précédente.

## 1.5.1 — 2026-10-04

- **Nettoyage guidé** : un très gros cache (plus de 4 Go, ex. cache des shaders NVIDIA) est
  proposé fichier par fichier. En 1.5.0, Windows refusait de le mettre à la corbeille d'un
  seul bloc (erreur « chemin non valide ») et rien n'était nettoyé.
- **Corbeille trop petite** : avant toute mise à la corbeille, Débarras vérifie sa place.
  Si la sélection dépasse sa capacité ou sa place libre — Windows effacerait alors
  définitivement des éléments — ou si la corbeille est désactivée sur le lecteur, l'action
  est bloquée avec la marche à suivre (vider la corbeille, sélectionner moins, archiver ou
  déplacer).

## 1.5.0 — 2026-10-04

- **Nouvelle page d'accueil** : le treemap, coloré selon ce que vous pouvez supprimer sans
  risque — rouge *Système* (Windows, Program Files, fichier d'échange…), orange *Logiciel*
  (programmes, jeux, AppData, dépôts Git), vert *Vos fichiers*, bleu *Nettoyable* (caches,
  temporaires, installeurs téléchargés). Chaque élément explique pourquoi.
- **Panneau de détail** : niveau, raison, conseil, logiciels contenus dans un dossier, et
  synthèse de l'espace par niveau avec les plus gros éléments.
- **Garde-fous renforcés** : les éléments système ne peuvent plus être supprimés (menus grisés,
  refus à l'exécution) ; supprimer un élément de logiciel demande une confirmation explicite ;
  les doublons appartenant à un logiciel ne sont jamais cochés automatiquement.
- **Nettoyage guidé** (nouvel onglet) : fichiers temporaires, caches des navigateurs (Chrome,
  Edge, Brave, Vivaldi, Firefox, Opera), rapports d'erreurs, cache des shaders ; Windows Update
  via le Nettoyage de disque de Windows. Tout passe par la corbeille.
- **Ce qui a grossi** depuis le scan précédent : résumé sur l'accueil et coloriage
  « Évolution » du treemap.
- **Installateur** (`Debarras-1.5.0-setup.exe`) : installation sans droits administrateur,
  raccourcis, entrée dans *Applications installées* et désinstalleur. La mise à jour
  automatique conserve le désinstalleur et met à jour la version affichée.

## 1.4.2 — 2026-09-28

- **Correctif mise à jour automatique** : en 1.4.0 et 1.4.1, « Installer et redémarrer »
  fermait Débarras sans installer la nouvelle version (le programme d'installation ne
  démarrait pas depuis l'exe). C'est corrigé, et Débarras ne se ferme désormais qu'une fois
  l'installation effectivement lancée — sinon il reste ouvert et le signale.
- **Depuis 1.4.0 ou 1.4.1**, téléchargez cette version manuellement (une seule fois) :
  décompressez l'archive à la place de l'ancien dossier `Debarras`.

## 1.4.1 — 2026-09-28

- Licences jointes au livrable : `LICENSE` (MIT) à côté de l'exe, et dossier `licenses`
  avec la LGPL v3 (Qt / PySide6), la GPL v3 à laquelle elle renvoie, les licences de Python,
  Pillow, xxhash, Send2Trash et du chargeur PyInstaller, et un récapitulatif
  (`THIRD_PARTY_NOTICES.txt`).

## 1.4.0 — 2026-09-28

Première version publique.

- **Mise à jour automatique** : Débarras vérifie les nouvelles versions au démarrage (une fois
  par jour, désactivable dans Paramètres) ou via *Aide › Rechercher des mises à jour*.
  L'archive est vérifiée (empreinte SHA-256) avant installation ; l'ancienne version est gardée
  en secours (dossier `Debarras.old`).
- Rappel des fonctions : arborescence et treemap, statistiques et graphiques, doublons, images
  similaires, fichiers de même nom au contenu différent, historique des scans, recherche,
  exports CSV/HTML, actions sûres (corbeille, déplacement, archive ZIP) avec simulation,
  journal et annulation.

## 1.3.0

- Onglet **Même nom** : fichiers homonymes au contenu différent (versions divergentes),
  avec lettres de version et filtres anti-bruit.

## 1.2.0

- Onglet **Images similaires** : empreinte visuelle, vignettes, aperçu, « garder la meilleure
  résolution ».

## 1.1.0

- Nouveau nom (Débarras) et logo ; cache compact (÷6 sur disque) ; recherche plus rapide.

## 1.0.0

- Première version : scan, cache, arborescence, treemap, statistiques, doublons, actions,
  historique, recherche, exports, thème sombre.
