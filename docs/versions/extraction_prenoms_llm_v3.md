# v3 — Modèle de vision via LM Studio (page entière → JSON)

> **Date de la version : mardi 29 sept. 2026, 23:17 (heure de Paris)** · archive `extraction_prenoms_llm_v3.zip` · 3ᵉ version sur 23

## Ce qui change

- Abandon du pipeline OCR : chaque page du scan est envoyée à un modèle de vision servi par LM Studio (ex. `prism-ml/bonsai-27b`), qui renvoie du JSON.
- Une ligne par prénom (« Youssouf / Youssef » → deux lignes, même arabe, même définition).
- `--ancien` : compare l'extraction à la précédente page par page.
- 34 tests, dont un faux serveur LM Studio (réflexion `<think>`, JSON tronqué, cache, reprise).

## Résultats et mesures

- Dossier de sortie : un dossier `autotest` + le cache par page (pas d'export complet conservé).
- Vitesse mesurée plus tard sur ce type de lecture : ≈ 8 tok/s avec bonsai-27b, générations qui ne finissaient pas (REPRISE §1).

## Limites et à retenir

- La qualité réelle du modèle n'était **pas testée** à la livraison (pas d'accès à LM Studio) ; le README demande de relire 30 lignes au hasard.
- Point de vigilance n°1 : un modèle peut « compléter » une voyelle arabe au lieu de la lire.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 5 (=) |
| Modules Python (hors tests) | 2 (=) |
| Lignes de code (hors tests) | 1 307 (+1) |
| Tests (`def test_`) | 34 (+21) |
| Taille de l'archive | 34 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
