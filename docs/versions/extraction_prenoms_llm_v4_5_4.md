# v4.5.4 — --completer-pages et voyelles finales

> **Date de la version : lundi 2 nov. 2026, 22:23 (heure de Paris)** · archive `extraction_prenoms_llm_v4_5_4.zip` · 18ᵉ version sur 23

## Ce qui change

- `--completer-pages` : relit les pages à entrées perdues. `normaliser_arabe_export` : retire les voyelles de déclinaison finales.

## Résultats et mesures

- `--completer-pages` : **19 pages refaites en 5 min**, 3 165 → **3 201 lignes** (1 665 → 1 684 entrées) ; les 8 entrées contrôlées sont récupérées avec un arabe juste ; 75 lignes « à vérifier ».
- Le livre n'imprime aucune voyelle de déclinaison finale ; gemma en ajoutait 219 dammas + 111 kasras sur le dernier mot et 191 dammas dans les annexions : **≈ 430 lignes (13 %)** corrigées à l'export.

## Limites et à retenir

- À relancer sur les 19 pages refaites : `--relire-tetes`, `--relire-arabe`, `--relire-corps`.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 25 (=) |
| Modules Python (hors tests) | 9 (=) |
| Lignes de code (hors tests) | 6 133 (+15) |
| Tests (`def test_`) | 178 (+1) |
| Taille de l'archive | 172 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
