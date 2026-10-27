# v4.3.2 — Comparaison automatique de modèles

> **Date de la version : mardi 27 oct. 2026, 22:42 (heure de Paris)** · archive `extraction_prenoms_llm_v4_3_2.zip` · 9ᵉ version sur 23

## Ce qui change

- `comparer_modeles.py` : charge/décharge chaque modèle via `lms`, autotest + relecture ciblée, rapport md/json.
- `trouver_pdf` ; l'autotest écrit `score.json`, la relecture `relecture_stats.json`.
- 5 tests de plus (comparateur).

## Résultats et mesures

- Le dossier `comparaison/` contient le premier rapport (28 oct. 10:06) : tableau repris dans la note de la v4.3.3.

## Limites et à retenir

- Jamais testé sur un vrai LM Studio à la livraison.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 20 (+2) |
| Modules Python (hors tests) | 7 (+1) |
| Lignes de code (hors tests) | 5 340 (+377) |
| Tests (`def test_`) | 147 (+5) |
| Taille de l'archive | 143 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
