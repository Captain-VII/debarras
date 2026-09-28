# Journal des versions

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
