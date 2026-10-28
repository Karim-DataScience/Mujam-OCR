# v4.3.4 — Run complet gemma vs bonsai + correction d'une erreur d'audit

> **Date de la version : mercredi 28 oct. 2026, 22:34 (heure de Paris)** · archive `extraction_prenoms_llm_v4_3_4.zip` · 11ᵉ version sur 23

## Ce qui change

- Une réponse vide valide est acceptée ; la lecture locale n'intervient que si le modèle échoue ou s'emballe ; anciens caches locaux ignorés à l'export.
- Vérificateur affiné (-Allâh, ʻ final = ة, y lu v, ș/š/ż, double lam) : faux positifs retirés.

## Résultats et mesures

- Run complet hybride (286 pages, ≈ 40 min) :

| | lignes | arabe incohérent | sans voyelle | sans arabe | lettres latines |
|---|---|---|---|---|---|
| **gemma-4-12b-qat** | 3 165 | **4,0 %** | 108 | 15 | 2 |
| bonsai-27b | 3 156 | 25,8 % | 1 301 | 334 | 21 |
| ornith-1.0-9b | arrêté p. 65 | ≈ 15 % | – | – | – |

- Conclusion : **gemma-4-12b-qat est le lecteur à utiliser.** Taux gemma avant/après affinage du vérificateur : 7,5 % → 4,0 %.

## Limites et à retenir

- Erreur corrigée : les 11 pages « lecture locale » (53, 81, 111, 135, 163, 197, 221, 249, 279, 289, 329) sont des pages de **titre** de section ; le « rescue » y fabriquait ≈ 40 entrées fantômes.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 20 (=) |
| Modules Python (hors tests) | 7 (=) |
| Lignes de code (hors tests) | 5 363 (+12) |
| Tests (`def test_`) | 150 (+2) |
| Taille de l'archive | 145 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
