# v4.5.0 — Français des définitions et prénoms mixtes

> **Date de la version : vendredi 30 oct. 2026, 11:58 (heure de Paris)** · archive `extraction_prenoms_llm_v4_5_0.zip` · 14ᵉ version sur 23

## Ce qui change

- `verif_francais.py` (suspect = hors dictionnaire fr + rare dans le corpus + hors lexique du domaine) ; `--relire-corps` (feuille de paragraphes).
- Colonne `mixte` ; drapeau de genre neutralisé pour les mixtes.

## Résultats et mesures

- Audit de la sortie finale gemma (3 165 lignes, 116 « à vérifier ») : structure et arabe OK ; **français Tesseract = point faible** : ≈ 17 % des définitions avec ≥ 1 faute probable (borne haute), 5 % des mots.
- 167 entrées mixtes = 205 des 211 « doublons ».
- Le dossier de sortie associé contient l'export le plus récent de la série 4.5 : **3 201 lignes**, 0 sans arabe.

## Limites et à retenir

- Qualité réelle de `--relire-corps` encore à mesurer à ce stade.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 25 (+2) |
| Modules Python (hors tests) | 9 (+1) |
| Lignes de code (hors tests) | 6 047 (+216) |
| Tests (`def test_`) | 171 (+9) |
| Taille de l'archive | 166 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
