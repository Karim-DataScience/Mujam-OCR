# v4.2 — Lecture hybride : le bon outil pour chaque zone

> **Date de la version : lundi 19 oct. 2026, 23:32 (heure de Paris)** · archive `extraction_prenoms_llm_v4_2.zip` · 5ᵉ version sur 23

## Ce qui change

- `mise_en_page.py` : zones de la page par traitement d'image (légende = filets, genre = couleur noir/rouge, ligne de nom, orthographes empilées, définition).
- `lecture_hybride.py` : Tesseract pour le français, une « feuille de recadrages » lue par le modèle de vision pour l'arabe et les têtes ; repli automatique sur la page entière.
- 129 tests moteur (REPRISE).

## Résultats et mesures

- Mise en page validée sur les 286 pages : 271 pages d'entrées sans anomalie, 15 pages de texte, 0 échec ; **1 708 entrées, 2 253 blocs de prénoms, 939 orthographes empilées, 3 192 formes** ; ≈ 0,65 s/page.
- Tesseract 5.3 sur les définitions : français courant quasi parfait mais perd la translittération (Â→A, ḥ/ḍ→h/d) ; 0,15-0,4 s/paragraphe, 2,5-8 s/page.
- Run hybride du dossier : **1 878 lignes**, 154 sans arabe ; 402 lignes sans voyelle, 21 avec lettres latines/cyrilliques (REPRISE §9).

## Limites et à retenir

- Unlimited-OCR (Baidu) envisagé comme remplaçant de Tesseract pour le français : non testé (pas de GPU dans le bac à sable).

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 12 (+4) |
| Modules Python (hors tests) | 5 (+2) |
| Lignes de code (hors tests) | 4 549 (+878) |
| Tests (`def test_`) | 129 (+17) |
| Taille de l'archive | 114 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
