# v4.5.2 — 2ᵉ passe de --relire-corps

> **Date de la version : dimanche 1 nov. 2026, 20:26 (heure de Paris)** · archive `extraction_prenoms_llm_v4_5_2.zip` · 16ᵉ version sur 23

## Ce qui change

- Alignement `difflib` par fenêtre de ± 4 mots, élisions décomposées (l'homme, l'hyacinthe), mot du modèle accepté s'il apparaît ≥ 2 fois dans le corpus.

## Résultats et mesures

- 2ᵉ passe : 131 définitions / 156 mots suspects → **seulement 5 mots corrigés** (632 s) ; pages 332-338 enfin traitées.
- Diagnostic : le prompt n'envoyait pas l'indice Tesseract (pas d'ancrage) ; ≈ 130 mots restants légitimes (translittérations, mots rares), ≈ 35 vraies fautes que l'ancienne fusion ne pouvait pas corriger.

## Limites et à retenir

- Les ≈ 130 mots légitimes resteront « suspects » (sans effet).

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 25 (=) |
| Modules Python (hors tests) | 9 (=) |
| Lignes de code (hors tests) | 6 086 (+20) |
| Tests (`def test_`) | 175 (+2) |
| Taille de l'archive | 168 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
