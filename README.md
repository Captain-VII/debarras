# Débarras

<img src="assets/icon.png" width="96" align="right" alt="Logo de Débarras">

**Voir ce qui encombre le disque, et s'en débarrasser sans risque.** Application Windows
(Python, PySide6) d'analyse et de nettoyage de disque.

- **Accueil : treemap « peut-on supprimer ? »** — chaque dossier et fichier est classé
  *Système* (bloqué), *Logiciel* (déconseillé), *Vos fichiers* ou *Nettoyable*, avec la raison.
  Arborescence triée par taille, navigation au double-clic.
- **Nettoyage guidé** : fichiers temporaires, caches des navigateurs, rapports d'erreurs,
  cache des shaders, Windows Update (via l'outil de Windows).
- **Statistiques** : répartition par type, plus gros fichiers, fichiers anciens, dossiers vides,
  caches et fichiers temporaires, installeurs oubliés — avec graphiques.
- **Doublons** (taille → empreinte partielle → empreinte complète xxHash), **images similaires**
  (empreinte visuelle) et **fichiers de même nom au contenu différent**, avec sélection
  automatique (plus récent, plus ancien, meilleure résolution, dossier prioritaire).
- **Actions sûres** : corbeille, déplacement, archive ZIP vérifiée. Jamais de suppression
  définitive, confirmation systématique, mode simulation, journal, annulation (Ctrl+Z).
- **Historique** des scans (ce qui a grossi ou diminué), **recherche** avec filtres,
  **exports** CSV et rapport HTML, thème clair/sombre, **mise à jour automatique**.

## Installation

Téléchargez `Debarras-<version>-setup.exe` depuis la page
[Releases](https://github.com/Captain-VII/debarras/releases) et lancez-le : installation pour
votre compte, sans droits administrateur, avec raccourci, entrée dans *Applications installées*
et désinstalleur. Version portable : `Debarras-<version>-win64.zip`, à décompresser où vous
voulez. Les données (cache, journal, paramètres) sont dans `%LOCALAPPDATA%\Debarras`.

L'exécutable n'est pas signé : au premier lancement, Windows SmartScreen peut demander
*Informations complémentaires › Exécuter quand même*.

## Confidentialité

Débarras fonctionne hors ligne. Seule la vérification des mises à jour contacte l'API GitHub
(`api.github.com`), sans envoyer d'autre information que la version de l'application ; elle se
désactive dans *Fichier › Paramètres*.

## Développement

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python main.py          # lancer
.venv\Scripts\python -m pytest        # tests
powershell -ExecutionPolicy Bypass -File build.ps1     # exe dans dist\Debarras + installateur
powershell -ExecutionPolicy Bypass -File release.ps1   # archive et installateur de release (+ -Publish)
```

L'installateur est construit avec [Inno Setup 6](https://jrsoftware.org/isinfo.php)
(`installer/debarras.iss`) s'il est présent.

Structure : `core/` (scan, cache SQLite, sécurité, nettoyage, doublons, images similaires,
stats, actions, historique, mise à jour), `ui/` (fenêtre et onglets PySide6), `utils/` (formats, exports),
`tests/` (pytest).

## Licence

Débarras est distribué sous licence [MIT](LICENSE). Les bibliothèques embarquées dans l'exe
gardent leurs propres licences (notamment Qt / PySide6 sous LGPL v3).
