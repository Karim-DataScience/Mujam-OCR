# v5.2.0 — Benchmark de la lecture de l'arabe

> **Date de la version : mercredi 4 nov. 2026, 13:43 (heure de Paris)** · archive `extraction_prenoms_llm_v5_2_0.zip` · 21ᵉ version sur 23

## Ce qui change

- `benchmark_arabe.py` + `benchmark/verite_arabe.json` : **64 zones arabes sur 5 pages de livres différents** (5 images, ≈ 1,7 Mo — d'où le saut de taille de l'archive).
- Compare Tesseract (variantes), EasyOCR et les modèles de vision de LM Studio, en recadrage et en page entière ; sépare réponses vides et erreurs de lecture ; vote majoritaire, « oracle », mode `:indice`.

## Résultats et mesures

- Tesseract 5.3.4 (bac à sable), CER léger : `ara fast psm7 ×3 binarisé` **6,1 %** (79,7 % exacts) ; `best` 8,9 % ; sans agrandissement ni binarisation 17,8 % ; page entière : 0 % des zones retrouvées.
- gemma-4-12b-qat, recadrage : CER 36,3 % **mais 23 réponses vides sur 64** ; sur les 41 réponses non vides, **0,6 %**. Page entière : rappel 96,9 %, précision 95,4 %.
- EasyOCR (CPU) : 37,1 % (arabe voyellé rose 8 %, colonnes sans voyelles 66 %), 0,08 s/zone. Accord gemma + EasyOCR : 28 % des zones, 0,0 % d'erreur ; oracle 7,7 %.
- Preuve directe du « silence » : la page de l'image 5 (p. 8, 17 entrées) a 0 arabe dans le cache du profil Abdur Rahman (prompt JSON complet) alors que le prompt court « liste l'arabe » la lit à 17/17 (CER 0,7 %).

## Limites et à retenir

- Vérité terrain écrite par Claude d'après les images (à faire relire), captures peu définies, 64 zones : des écarts de quelques points ne sont pas significatifs.
- Tesseract a échoué chez l'utilisateur (chemin Windows entre guillemets) : corrigé en 5.3.0.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 46 (+8) |
| Modules Python (hors tests) | 11 (+1) |
| Lignes de code (hors tests) | 6 888 (+486) |
| Tests (`def test_`) | 215 (+18) |
| Taille de l'archive | 1918 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
