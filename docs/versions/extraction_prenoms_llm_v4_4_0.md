# v4.4.0 — Revue manuelle + relecture des têtes

> **Date de la version : jeudi 29 oct. 2026, 09:11 (heure de Paris)** · archive `extraction_prenoms_llm_v4_4_0.zip` · 12ᵉ version sur 23

## Ce qui change

- Arbitrage de tête par l'arabe (extraction + `--relire-tetes`).
- `revue_manuelle.py` : `--exporter-problemes` (page HTML) et `--appliquer-corrections`.
- Coût de l'alef suscrit abaissé (طه, الله). 145 tests moteur + 10 revue + 23 TUI + 5 comparateur (décompte REPRISE).

## Résultats et mesures

- Relecture gemma des 72 mots signalés (sans réflexion, 508 s) : **11 corrigés, 62 restent** ; taux d'incohérence gemma 3,6 %.
- Cause des restes (vérifiée sur les recadrages) : surtout des **têtes mal lues par Tesseract** (Khalil-ATâR, Safivv-AÏlâh) alors que l'arabe de gemma est juste.

## Limites et à retenir

- La réflexion n'a jamais été utilisée dans l'extraction ni la relecture (0 jeton).

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 23 (+3) |
| Modules Python (hors tests) | 8 (+1) |
| Lignes de code (hors tests) | 5 800 (+437) |
| Tests (`def test_`) | 160 (+10) |
| Taille de l'archive | 158 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
