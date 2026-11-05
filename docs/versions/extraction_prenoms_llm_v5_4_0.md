# v5.4.0 — Benchmark « --cites » : installer et tester les outils choisis

> **Date de la version : jeudi 5 nov. 2026, 12:36 (heure de Paris)** · archive `extraction_prenoms_llm_v5_4_0.zip` · 23ᵉ version sur 23

## Ce qui change

- `--cites` : Tesseract fast/best, EasyOCR, PaddleOCR, Gemma 4 12B QAT, Qwen3-VL 8B/2B, Melaraby, InternVL, olmOCR 2, LFM3-VL 2B, Qwen2-VL, Qari-OCR ; démarre le serveur (`lms server start`), annonce le plan, installe après accord (`--oui`) : pip, `ara.traineddata`, `lms get`.
- `dossier_ascii()` : copie les langues Tesseract dans un dossier sans accent (cause probable de « langue ara absente » avec `D:\Téléchargements`). 40 tests avec faux `lms` et faux serveur.

## Résultats et mesures

- Pas de nouvelle mesure propre à cette version : les chiffres de référence sont ceux de la v5.3.0.
- Piste prioritaire (non implémentée) : « deuxième passe arabe » — si une page rend des entrées sans arabe alors que le profil en attend, relancer avec le prompt court « liste l'arabe » et réaffecter les lignes aux entrées.

## Limites et à retenir

- **Non testé** : `lms get` / `lms server start`, noms du catalogue (certains modèles peuvent ne pas exister en GGUF), pip de PaddleOCR et chemin `TESSDATA_PREFIX` sous Windows.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 46 (=) |
| Modules Python (hors tests) | 11 (=) |
| Lignes de code (hors tests) | 7 361 (+240) |
| Tests (`def test_`) | 237 (+10) |
| Taille de l'archive | 1932 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
