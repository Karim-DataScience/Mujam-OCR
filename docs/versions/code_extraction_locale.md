# v1 — Pipeline OCR local à 3 passes

> **Date de la version : lundi 28 sept. 2026, 07:17 (heure de Paris)** · archive `code_extraction_locale.zip` · 1ᵉ version sur 23

## Ce qui change

- Tesseract `fra+ara` : structure de la page, positions, couleurs (rouge = féminin), texte français.
- EasyOCR arabe : relit chaque zone arabe repérée par Tesseract.
- EasyOCR français : relit seulement les mots latins peu fiables (confiance < 80), avec garde-fous avant tout remplacement.
- Tout est local, gratuit, open source (Tesseract, EasyOCR, Poppler, pandas, openpyxl).

## Résultats et mesures

- Test sur 10 entrées (pages PDF 55-56) : Idris `سحي` → `إدريس`, Houd `صود` → `هود`, Adam `ادم` → `آدم`, sens d'Idris `00/141…` → `calame`.
- Export du dossier de sortie : **2 530 lignes** (`prenoms_arabes.csv`).

## Limites et à retenir

- Échantillon de 10 entrées : montre que la méthode marche, pas un taux global. Restent faux : Hawa `حسواء`, Chisse `شث`, Ibrahim partiel.
- Traitement séquentiel, image rasterisée à 300 DPI (~5 700 × 9 000 px).

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 3 |
| Modules Python (hors tests) | 1 |
| Lignes de code (hors tests) | 822 |
| Tests (`def test_`) | 0 |
| Taille de l'archive | 16 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
