# v4.1 — Streaming, journaux, TUI, reprise

> **Date de la version : mardi 6 oct. 2026, 23:02 (heure de Paris)** · archive `extraction_prenoms_llm_v4_1.zip` · 4ᵉ version sur 23

## Ce qui change

- Streaming des réponses, réflexion coupée (`reasoning: off`), budget de contexte, calibrage de l'image, abandon sur boucle ou silence, reprise.
- Journaux (`.log` + `.jsonl`), `--rapport` (zip de debug), configuration `prenoms_config.json`.
- Interface plein écran (TUI Textual) ; `lancer.bat`.
- 112 tests (décompte sur l'archive).

## Résultats et mesures

- Sortie du dossier : **346 lignes** (run partiel), dont 40 sans arabe.
- Résultat v4 fourni par l'utilisateur (pages 55-77, 180 lignes) : orthographes empilées lues comme prénoms distincts (parfois mauvais genre), arabe confondu (خليم pour خليل, رخمة pour رحمة…), **32 % des lignes sans voyelle**, 19 césures non recollées (REPRISE §4).

## Limites et à retenir

- Pages 63-71 : vraies entrées dans une mise en page sans légende MASCULIN/FEMININ, que le modèle manque.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 8 (+3) |
| Modules Python (hors tests) | 3 (+1) |
| Lignes de code (hors tests) | 3 671 (+2364) |
| Tests (`def test_`) | 112 (+78) |
| Taille de l'archive | 88 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
