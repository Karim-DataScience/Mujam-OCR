# v4.5.1 — Premier run de --relire-corps

> **Date de la version : dimanche 1 nov. 2026, 19:44 (heure de Paris)** · archive `extraction_prenoms_llm_v4_5_1.zip` · 15ᵉ version sur 23

## Ce qui change

- `_garde_silence` : deux silences de suite → erreur claire, pages déjà traitées conservées.
- Mots de moins de 4 lettres jamais remplacés ; apostrophes ignorées.

## Résultats et mesures

- Run : 285 définitions ciblées, **198 mots corrigés**, durée **2 h 04 dont ≈ 1 h 30 perdues** (LM Studio muet à partir de p. 332, 3 essais de 300 s × 6 pages, sans avertissement).
- Définitions avec ≥ 1 mot suspect : **300 (18,0 %) → 146 (8,8 %)** ; mots suspects 370 → 176 (5,3 % → 2,5 %).
- Export : 3 165 lignes, 79 « à vérifier ».

## Limites et à retenir

- Corrections très majoritairement justes (seroiteur→serviteur, bonlieur→bonheur…), quelques ratés possibles sur mots courts (sen→son, bou→bon) → règle des < 4 lettres.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 25 (=) |
| Modules Python (hors tests) | 9 (=) |
| Lignes de code (hors tests) | 6 066 (+19) |
| Tests (`def test_`) | 173 (+2) |
| Taille de l'archive | 167 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
