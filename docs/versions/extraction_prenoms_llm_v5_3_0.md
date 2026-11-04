# v5.3.0 — Benchmark complet (tous les modèles de vision)

> **Date de la version : mercredi 4 nov. 2026, 23:34 (heure de Paris)** · archive `extraction_prenoms_llm_v5_3_0.zip` · 22ᵉ version sur 23

## Ce qui change

- `--complet` / `--vlm` (modèles « vlm » via `/api/v0/models`), `--sans-crop`, `--echantillon`, `--reprendre`, `--budget-min`, `--decharger`, échauffement hors mesure, abandon après 3 erreurs, sauvegarde après chaque candidat.
- Enregistre `finish_reason`, jetons, raisonnement ; relance les réponses vides avec 4× plus de jetons. `TESSDATA_PREFIX` + `--telecharger-tessdata`. 30 tests.

## Résultats et mesures

- Mesures page entière de l'utilisateur (`--complet`, 64 zones) :

| Modèle | rappel | précision | CER des zones trouvées |
|---|---|---|---|
| **gemma-4-26b-a4b** | **100 %** | **98,6 %** | **0,5 %** |
| gemma-4-12b-qat | 96,9 % | 95,4 % | 2,0 % |
| EasyOCR | 92,2 % | 78,7 % | 6,7 % |
| gemma-3-12b | 90,6 % | 93,4 % | 4,8 % |
| gemma-4-12b (hors QAT) | 78,1 % | 76,9 % | 1,2 % |


## Limites et à retenir

- Hypothèses du silence de gemma (budget de jetons épuisé par le raisonnement ; prompt trop chargé) : **non vérifiées**.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 46 (=) |
| Modules Python (hors tests) | 11 (=) |
| Lignes de code (hors tests) | 7 121 (+233) |
| Tests (`def test_`) | 227 (+12) |
| Taille de l'archive | 1925 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
