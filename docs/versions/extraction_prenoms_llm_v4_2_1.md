# v4.2.1 — Premier vrai run sur la machine : correctifs de repli

> **Date de la version : mardi 20 oct. 2026, 14:42 (heure de Paris)** · archive `extraction_prenoms_llm_v4_2_1.zip` · 6ᵉ version sur 23

## Ce qui change

- `tete_plausible()` et `nettoyer_debut()` : le contrôle « la définition commence par Tête : » était trop strict face au bruit d'OCR.
- 132 tests moteur + 23 TUI (REPRISE).

## Résultats et mesures

- Run TUI du 20 oct. (p. 53-71, bonsai-27b, sans réflexion) : lecture locale 1-3 s/page ; feuille de recadrages ≈ 25 s/page contre ≈ 70 s en page entière ; **17 tok/s** (≈ 8 avant).
- **6 pages sur 18 en repli inutile (33 %)** avant correctif ; après : 25 pages signalées sur 240 rejouées (≈ 10 %), 0 repli injustifié sur les pages du journal.
- Diagnostic GPU : contexte chargé 120 839, 4 prédictions parallèles, VRAM 11,8 / 12,2 Go (presque pleine) → réglage conseillé : contexte 12 288, parallèle 1, lot 1 024.
- Run « modèle seul » (10 pages, ≈ 600 jetons/page) : orthographes empilées mal rattachées, lettres confondues (Yaqoub `يَقُوب`).

## Limites et à retenir

- Le journal s'arrête à p. 71 (interrompu) : aucun export complet à ce stade.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 12 (=) |
| Modules Python (hors tests) | 5 (=) |
| Lignes de code (hors tests) | 4 563 (+14) |
| Tests (`def test_`) | 132 (+3) |
| Taille de l'archive | 115 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
