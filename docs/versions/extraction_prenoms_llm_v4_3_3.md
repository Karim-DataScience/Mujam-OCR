# v4.3.3 — Premiers résultats de comparaison de modèles

> **Date de la version : mercredi 28 oct. 2026, 11:51 (heure de Paris)** · archive `extraction_prenoms_llm_v4_3_3.zip` · 10ᵉ version sur 23

## Ce qui change

- Relecture sans réflexion par défaut (`--reflexion oui` pour la forcer) ; lectures refusées renvoyées en indice.
- Corrections mineures (+ 1 test).

## Résultats et mesures

- Autotest 4 pages à vérité terrain, hybride, RTX 5070 Ti 12 Go :

| Modèle | arabe exact | avec voyelles | vedettes | s/page | tok/s |
|---|---|---|---|---|---|
| **google/gemma-4-12b-qat** | **22/22** | **35/35** | 35/35 | **7,9** | 55,5 |
| prism-ml/bonsai-27b | 10/22 | 14/35 | 35/35 | 9,1 | 58,1 |
| ornith-1.0-9b | 17/22 | 13/20 | 20/35 | 47,7 | 77,8 |

- Relecture avec réflexion : **18 requêtes sur 18 ont épuisé le budget de réflexion (1 500 jetons) sans réponse → 0 correction**.
- Sorties conservées : gemma 3 176 lignes (15 sans arabe), bonsai 3 156 (334 sans arabe), ornith arrêté (63 lignes).

## Limites et à retenir

- 4 pages = un indicateur, pas une preuve ; confirmé ensuite sur le livre entier (v4.3.4).

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 20 (=) |
| Modules Python (hors tests) | 7 (=) |
| Lignes de code (hors tests) | 5 351 (+11) |
| Tests (`def test_`) | 148 (+1) |
| Taille de l'archive | 144 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
