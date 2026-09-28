# v2 — OCR multi-cœur + GPU, cache par page

> **Date de la version : mardi 29 sept. 2026, 00:22 (heure de Paris)** · archive `code_extraction_locale_v2.zip` · 2ᵉ version sur 23

## Ce qui change

- N processus Tesseract en parallèle (1 par cœur) ; image native du scan (~1 800 × 2 900 px) : 2 à 3× plus rapide et au moins aussi précise dans les tests de l'époque.
- Détection GPU CUDA réelle, messages d'aide pour RTX 50 ; PyMuPDF en option (plus besoin de Poppler).
- Cache par page (OCR et relectures séparés), page en échec journalisée et sautée, colonne « à vérifier » + feuille Excel dédiée.
- 13 tests unitaires + `--autotest` (4 pages à vérité terrain).

## Résultats et mesures

- Export du dossier de sortie : **2 601 lignes**.
- Comptage de la mise en page faite plus tard : ≈ 3 240 formes pour cette v2, contre 3 192 par traitement d'image (REPRISE §4).

## Limites et à retenir

- Jugée **imprécise** par l'utilisateur (REPRISE §2) : orthographes empilées lues comme prénoms distincts, arabe confondu. C'est ce constat qui motive le passage à un modèle de vision (v3).

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 5 (+2) |
| Modules Python (hors tests) | 2 (+1) |
| Lignes de code (hors tests) | 1 306 (+484) |
| Tests (`def test_`) | 13 (+13) |
| Taille de l'archive | 28 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
