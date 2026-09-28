# Extraction du « Livre des prénoms arabes » en local (pipeline OCR à 3 passes)

Ce dossier contient le script qui produit `prenoms_arabes.csv` / `.json` / `.xlsx`
à partir du PDF scanné. Tout est **local, gratuit et open source** :
Tesseract (Apache-2.0), EasyOCR (Apache-2.0), Poppler, pandas, openpyxl.

## Pourquoi plusieurs outils ?

Le PDF est un scan sans texte, mêlant français et arabe. Aucun OCR généraliste
n'est bon partout, donc chaque outil fait ce qu'il fait le mieux :

| Passe | Outil | Rôle |
|---|---|---|
| 1 | **Tesseract** `fra+ara` | Structure de la page, positions, couleurs (rouge = féminin), texte français |
| 2 | **EasyOCR arabe** | Relit chaque zone en script arabe repérée par Tesseract |
| 3 | **EasyOCR français** | Relit *uniquement* les mots latins que Tesseract juge peu fiables (confiance < 80), avec des garde-fous stricts avant tout remplacement (confiance ≥ 0,80, caractères plausibles, longueur cohérente, apostrophes préservées) |

Exemples vérifiés (test sur 10 entrées, pages PDF 55-56) :

| Cas | Tesseract seul | 3 passes |
|---|---|---|
| Arabe de *Idris* | `سحي` | `إدريس` |
| Arabe de *Houd* | `صود` | `هود` |
| Arabe de *Adam* | `ادم` | `آدم` |
| Sens de *Salih* (mot en italique) | `11116916,` | `intègre,` |
| Sens d'*Idris* (mot en italique) | `00/141…` | `calame` |
| Sens de *Salih* | `Thamüûd` | `Thamûd` |

⚠️ Ce test porte sur un petit échantillon : il montre que la méthode marche, pas
un taux d'amélioration global. Le reste de l'arabe n'est pas parfait (ex. *Hawa*
`حسواء`, *Chisse* `شث`, *Ibrahim* partiel).

## 1. Installer les outils système

**macOS** (Homebrew) : `brew install tesseract tesseract-lang poppler`

**Ubuntu / Debian** : `sudo apt-get install tesseract-ocr tesseract-ocr-fra tesseract-ocr-ara poppler-utils`

**Windows** : installer Tesseract ([UB-Mannheim](https://github.com/UB-Mannheim/tesseract/wiki), cocher French + Arabic)
et [Poppler pour Windows](https://github.com/oschwartz10612/poppler-windows) (ajouter `bin` au PATH).

Vérifier : `tesseract --list-langs` doit afficher `fra` et `ara`.

## 2. Installer les paquets Python

```bash
pip install -r requirements.txt
```
Au premier lancement, EasyOCR télécharge automatiquement ses modèles (~300 Mo,
depuis GitHub). Ensuite tout fonctionne hors ligne.

## 3. Lancer

```bash
python3 extraire_prenoms.py chemin/vers/lelivredesprenom0000geof.pdf
```
Par défaut : pages PDF 53 à 338 (tout le dictionnaire), résultats dans `./sortie/`.

| Option | Effet |
|---|---|
| `--debut N --fin M` | Limiter la plage de pages (pour tester : `--debut 55 --fin 60`) |
| `--dossier-sortie DIR` | Dossier de sortie |
| `--reprendre` | Reprend après une interruption (progression sauvegardée après **chaque** page) |
| `--sans-easyocr-latin` | Garde la passe arabe, saute la passe française |
| `--sans-easyocr` | Tesseract seul (le plus rapide, le moins précis) |
| `--gpu` | Passes EasyOCR sur GPU (CUDA) : beaucoup plus rapide si tu as une carte NVIDIA |
| `--dpi 300` | Résolution de rasterisation (200 va ~40 % plus vite mais perd des entrées) |

**Durée** : sur CPU, compter ~20-30 s/page avec les 3 passes, soit **1h30 à 2h30**
pour tout le livre (~1h avec `--sans-easyocr`). Sans easyocr installé, le script
le signale et continue avec Tesseract seul.

## Fichiers produits

`prenoms_arabes.csv` (UTF-8 avec BOM, s'ouvre bien dans Excel), `.json`, `.xlsx`.
Colonnes : `ID`, `Prénom(s)`, `Genre`, `Arabe`, `Translittération`, `Équivalent`,
`Signification`, `Thème`, `Sous-thème`, `Page (imprimée)`, `Page (PDF)`
(la page permet de vérifier n'importe quelle entrée dans le PDF).

## Limites connues

- Diacritiques arabes (tashkeel) : perdus par les deux moteurs ; les consonnes sont mieux lues qu'avant mais pas toujours.
- Lignes mêlant arabe et français : des mots peuvent être réordonnés ou tronqués (ex. la définition de *Khalilallah*).
- Certaines variantes bizarres sont **imprimées telles quelles** dans le livre (ex. *Ralilallah*) : ce n'est pas du bruit OCR.
- Les garde-fous de la passe 3 sont volontairement prudents : ils préfèrent laisser un mot douteux que d'introduire une erreur.

## Pour aller plus loin (non testé ici)

- **PaddleOCR** (Apache-2.0) : autre moteur avec modèle arabe, utilisable comme quatrième passe / vote.
- **Kraken** (Apache-2.0) : OCR entraînable — la meilleure piste pour un gain durable est d'entraîner un modèle sur la typographie de ce livre à partir de quelques dizaines de pages corrigées à la main.
- **Vérification ciblée** : filtrer dans le CSV les entrées à `Arabe` vide ou à `Signification` contenant beaucoup de chiffres, et les corriger à la main via la colonne page.

## Adapter à un autre livre

Le bloc `CHAPTERS` en tête de script encode la table des matières de CE livre
(chapitres, sous-thèmes, pages) ; `PAGE_OFFSET` = décalage pagination PDF / imprimée.
