# v5.1.0 — La TUI applique les profils

> **Date de la version : mercredi 4 nov. 2026, 07:14 (heure de Paris)** · archive `extraction_prenoms_llm_v5_1_0.zip` · 20ᵉ version sur 23

## Ce qui change

- Bug corrigé : la TUI ignorait les profils (toujours pages 53-338 et prompt de Geoffroy). `cfg_avec_profil` = chemin unique CLI + TUI ; champ « Profil du document » dans Réglages.
- Garde-fous : un dossier ne mélange pas deux documents ; `--autotest` refusé hors profil Geoffroy ; `test_profil_choix.py` (14 tests).

## Résultats et mesures

- Sortie Abdur Rahman (profil v5) : **1 418 lignes, 126 sans arabe**.
- Testés : 53 tests rapides ; `--statut` et TUI sur les 7 PDF réels. Moteur 148 OK dont 55 ignorés et TUI 23 ignorés faute du PDF de Geoffroy dans le bac à sable.

## Limites et à retenir

- Toujours non testé : les 7 profils avec un vrai modèle.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 38 (+1) |
| Modules Python (hors tests) | 10 (=) |
| Lignes de code (hors tests) | 6 402 (+77) |
| Tests (`def test_`) | 197 (+10) |
| Taille de l'archive | 195 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
