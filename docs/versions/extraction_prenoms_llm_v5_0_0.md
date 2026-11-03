# v5.0.0 — Profils de documents

> **Date de la version : mardi 3 nov. 2026, 22:22 (heure de Paris)** · archive `extraction_prenoms_llm_v5_0_0.zip` · 19ᵉ version sur 23

## Ce qui change

- `profils.py` + 9 profils JSON (Geoffroy, Abdur Rahman, Hakim, Usmani, Pénoncourt, Hareeri, Haddadou, Treasury, générique) ; `--profil`, `--lister-profils`, détection du PDF par son nom, `profil_utilise.json`.
- Thèmes du Livre des prénoms arabes désactivés pour les autres documents ; `test_profils.py`.

## Résultats et mesures

- Le livre de Geoffroy est « terminé pour l'essentiel » : **3 201 lignes, 1 684 entrées, 75 « à vérifier »**, français convergé, voyelles finales retirées.

## Limites et à retenir

- **Jamais testé avec un vrai LM Studio** pour les 7 autres profils (prompts écrits d'après des captures).
- Un reset du bac à sable a effacé une première version de ce travail, refaite à partir de la 4.5.4.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 37 (+12) |
| Modules Python (hors tests) | 10 (+1) |
| Lignes de code (hors tests) | 6 325 (+192) |
| Tests (`def test_`) | 187 (+9) |
| Taille de l'archive | 191 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
