# v4.5.3 — Entrées perdues par la mise en page

> **Date de la version : lundi 2 nov. 2026, 21:59 (heure de Paris)** · archive `extraction_prenoms_llm_v4_5_3.zip` · 17ᵉ version sur 23

## Ce qui change

- Corrections de `mise_en_page.py` (`HAUT_MAX_NOM` 95 px < arabe de 97 px ; ligne de nom noire en un seul amas).

## Résultats et mesures

- 3ᵉ passe `--relire-corps` : 17 mots corrigés sur 151 (540 s) ; le français a convergé (≈ 134 mots restants, surtout légitimes).
- Vraies entrées perdues : p. 96 (Abdelmoumine), p. 320 (Ousfour/Ousfoura) et ≈ 18 autres pages à 1 entrée perdue, **sans aucun avertissement**. Total du livre : **1 708 → 1 720 entrées**.

## Limites et à retenir

- Leçon (AUDIT §F) : un contrôle par comptage (cache vs mise en page) aurait dû exister dès la v4.3.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 25 (=) |
| Modules Python (hors tests) | 9 (=) |
| Lignes de code (hors tests) | 6 118 (+32) |
| Tests (`def test_`) | 177 (+2) |
| Taille de l'archive | 171 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
