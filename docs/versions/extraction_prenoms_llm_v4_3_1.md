# v4.3.1 — Audit complet + relecture ciblée de l'arabe

> **Date de la version : mardi 27 oct. 2026, 22:34 (heure de Paris)** · archive `extraction_prenoms_llm_v4_3_1.zip` · 8ᵉ version sur 23

## Ce qui change

- `--relire-arabe` : relecture des seuls mots incohérents, avec indice « attendu ≈ … » ; adoption si le vérificateur trouve la nouvelle lecture meilleure (gain ≥ 0,25).
- Détection de boucle d'entrées, dédoublonnage à la lecture et à l'export, avertissement d'option répétée.
- `AUDIT.md` ; 142 tests moteur + 23 TUI.

## Résultats et mesures

- Page 98 : boucle non détectée (8 000 jetons, 550 s, 86 « entrées » pour 7 attendues).
- Ré-export du cache 4.2 : 1 878 → 1 791 lignes (87 doublons retirés), 413 lignes `arabe_incoherent_translit`.
- Qualité du code : ruff 0 erreur ; 3 avertissements bandit (SHA-1 de cache) traités.

## Limites et à retenir

- L'audit contenait une **erreur** (A2 : « entrées perdues en silence » sur 6 pages) corrigée le 28 oct. : c'étaient des pages de titre. Leçon notée : vérifier l'image avant de conclure à une perte.
- Date : REPRISE indique le 20 oct. ; l'archive porte la date du 27 oct. (re-zippée).

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 18 (+5) |
| Modules Python (hors tests) | 6 (=) |
| Lignes de code (hors tests) | 4 963 (+184) |
| Tests (`def test_`) | 142 (+5) |
| Taille de l'archive | 133 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
