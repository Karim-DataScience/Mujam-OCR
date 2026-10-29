# v4.4.1 — Premier retour de la revue manuelle

> **Date de la version : jeudi 29 oct. 2026, 20:55 (heure de Paris)** · archive `extraction_prenoms_llm_v4_4_1.zip` · 13ᵉ version sur 23

## Ce qui change

- `--appliquer-corrections` signale les corrections à revoir (faute de frappe possible).
- Vérificateur : forme féminine d'une tête masculine, « ou », hamza porté, ة pour â final.

## Résultats et mesures

- `--relire-tetes` sur le cache gemma : **31 têtes corrigées sur 56 suspectes** (388 s).
- Export des problèmes : 96 mots (66 sans voyelles, 29 incohérents, 1 non arabe). Retour de l'utilisateur : 96 lignes, **87 arabes corrigés, 18 translittérations corrigées**, 0 identifiant inconnu.

## Limites et à retenir

- Restent : 79 drapeaux `genre_a_verifier`, 26 `sans_point_final`, 11 pages à < 3 entrées, 5 suites de définition orphelines, ≈ 9 % de pages en repli page entière.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 23 (=) |
| Modules Python (hors tests) | 8 (=) |
| Lignes de code (hors tests) | 5 831 (+31) |
| Tests (`def test_`) | 162 (+2) |
| Taille de l'archive | 159 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
