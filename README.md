# Extraction du « Livre des prénoms arabes » — v2 (multi-cœur + GPU)

Réécriture pour ta config (Ryzen 9 9955HX, RTX 5070 Ti 12 Go, 32 Go RAM),
optimisée pour du **multi-cœur** et un **GPU récent**, avec du code plus
robuste : reprise automatique par page, tests unitaires, autotest de
qualité, colonne « à vérifier ».

**Lis la section « Ce qui est solide / ce qui est incertain » avant de te fier
aux chiffres d'exactitude arabe.** Je préfère être direct là-dessus plutôt que
de survendre.

## Nouveautés vs la v1

| | v1 | v2 |
|---|---|---|
| Tesseract | 1 processus, séquentiel | **N processus en parallèle** (1/cœur) |
| Image source | rasterisée à 300 DPI (~5700×9000 px) | **image native du scan** (~1800×2900 px) : 2 à 3× plus rapide, et au moins aussi précise dans mes tests |
| GPU | non | **oui** (CUDA), avec détection réelle (pas juste « torch.cuda.is_available() ») et messages d'aide spécifiques RTX 50 |
| Reprise | reprend après la dernière page réussie | **cache par page** (OCR et relectures séparément) : relancer la même commande ne refait *rien* de déjà calculé, même après avoir changé une option |
| Erreurs | une page en échec pouvait interrompre le lot | **une page en échec est journalisée et sautée**, le reste continue |
| Contrôle qualité | aucun | **colonne "À vérifier"** (chiffres mêlés à des lettres, arabe vide, définition tronquée...) + feuille Excel séparée pour ces lignes |
| Tests | aucun | **13 tests unitaires** (`test_parsing.py`) + **autotest** (`--autotest`, 4 pages, vérité terrain) |
| Portabilité | Poppler obligatoire | **PyMuPDF en option** (`pip install pymupdf`) : plus simple sous Windows, pas d'exécutable externe à installer |

## Installation

### 1. Outils système (Tesseract obligatoire ; Poppler optionnel si PyMuPDF installé)

```bash
# Ubuntu/Debian
sudo apt-get install tesseract-ocr tesseract-ocr-fra tesseract-ocr-ara

# macOS
brew install tesseract tesseract-lang
```
Windows : installer Tesseract via [UB-Mannheim](https://github.com/UB-Mannheim/tesseract/wiki)
(cocher French + Arabic). Pas besoin de Poppler si tu installes `pymupdf` (étape 2).

### 2. Paquets Python

```bash
pip install -r requirements.txt
```

### 3. PyTorch avec le bon CUDA pour une RTX 50 (Blackwell)

**Point important pour ta carte** : `pip install easyocr` installe un PyTorch
générique qui ne reconnaît PAS encore l'architecture `sm_120` des RTX 50 —
tu obtiendrais une erreur `no kernel image is available` ou EasyOCR
tournerait silencieusement sur CPU. Force la bonne version :

```bash
pip uninstall -y torch torchvision torchaudio
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
```
Si l'erreur persiste (pilote très récent, ou cu128 pas encore diffusé pour
Blackwell au moment où tu lis ceci), essaie la version nightly :
```bash
pip install --pre torch torchvision --index-url https://download.pytorch.org/whl/nightly/cu128
```

### 4. Vérifier

```bash
python extraire_prenoms.py --diagnostic
```
Doit afficher `GPU OK : NVIDIA GeForce RTX 5070 Ti (sm_120, 12 Go)...`. Si ce
n'est pas le cas, le message d'erreur affiché contient la commande à essayer.

## Utilisation

```bash
# 1) Vérifie d'abord la qualité sur 4 pages connues (~1 min avec GPU)
python extraire_prenoms.py livre.pdf --autotest

# 2) Le livre entier
python extraire_prenoms.py livre.pdf --gpu oui --workers 14
```

Avec ton CPU (16 cœurs / 32 threads) et ta RTX 5070 Ti, compter grossièrement
**10-20 minutes** pour les 286 pages (Tesseract en parallèle sur CPU pendant
qu'EasyOCR tourne sur GPU) — contre 1h30-2h30 en CPU pur sur un portable
classique. Le detail dépend beaucoup de `--batch` (essaie 32, monte à 64 si
`nvidia-smi` montre de la VRAM libre, baisse à 8-16 si tu vois une erreur
"out of memory").

### Options principales

| Option | Effet |
|---|---|
| `--workers 14` | processus Tesseract en parallèle (0 = auto ≈ cœurs physiques − 1) |
| `--gpu oui/non/auto` | GPU pour EasyOCR (par défaut auto : utilisé s'il passe le test réel) |
| `--batch 32` | taille de lot GPU EasyOCR |
| `--arabe mots` | relecture arabe mot par mot (défaut, validé) — `bandes` est expérimental, `non` désactive |
| `--sans-latin` | désactive seulement la relecture des mots latins douteux |
| `--sans-easyocr` | Tesseract seul, aucune relecture |
| `--pages 55,56,100-104` | traite une liste explicite de pages |
| `--refaire` | ignore le cache pour les pages demandées |
| `--tesseract CHEMIN` / `--poppler DOSSIER` | si les outils ne sont pas dans le PATH |

Le **cache** (`sortie/cache/`) sépare l'OCR de base et les relectures : si tu
relances avec une autre option (ex. `--arabe non` puis `--arabe mots`), seul
ce qui a changé est recalculé.

## Ce qui est solide / ce qui est incertain

**Solide, mesuré** :
- Le passage à l'image native (au lieu du 300 DPI suréchantillonné) : 2-3×
  plus rapide, sans perte de qualité mesurée sur mes pages de test.
- Le parallélisme Tesseract : aucune perte de qualité (c'est le même calcul,
  juste réparti), gain ≈ proportionnel au nombre de cœurs utilisés.
- Les garde-fous de la relecture latine (tests unitaires dédiés) : ils
  corrigent des cas concrets (mot italique lu comme des chiffres) sans
  introduire les régressions que j'ai détectées puis corrigées en cours de
  route (troncatures acceptées à tort, par ex.).

**Incertain, à ton propre risque** : la relecture EasyOCR de l'arabe. En
testant ce script, j'ai mesuré des résultats **incohérents d'une exécution à
l'autre sur les mêmes pages** — parfois une nette amélioration (comme montré
dans la v1), parfois un résultat pire que Tesseract seul, sans que j'aie pu
identifier la cause exacte dans le temps disponible (peut-être une
sensibilité d'EasyOCR au cadrage exact, peut-être une interaction entre les
passes). Le mode par défaut (`--arabe mots`) est la version la plus simple et
la plus testée, mais **ne considère pas le taux d'exactitude arabe comme
fiable tant que tu ne l'as pas vérifié toi-même** avec `--autotest` sur ta
machine. La colonne `Arabe (Tesseract)` est conservée à côté de `Arabe`
quand les deux moteurs sont en désaccord, précisément pour te permettre de
comparer plutôt que de faire confiance aveuglément.

`--autotest` te donne un repère chiffré en ~1 minute : si `arabe_exact` est
proche de 0/22 sur ta machine, désactive l'arabe (`--arabe non`) et garde
seulement les gains solides (vitesse, français, structure).

## Fichiers

- `extraire_prenoms.py` — le script (1236 lignes, commenté)
- `verite_terrain.py` — les 4 pages + réponses attendues utilisées par `--autotest`
- `test_parsing.py` — 13 tests unitaires (`python test_parsing.py`), sans OCR ni GPU, s'exécutent en une seconde
- `requirements.txt`

## Colonnes du fichier de sortie

`ID`, `Prénom(s)`, `Genre`, `Arabe`, `Arabe (Tesseract)` (rempli seulement si
les deux moteurs divergent), `Translittération`, `Équivalent`,
`Signification`, `Thème`, `Sous-thème`, `Page (imprimée)`, `Page (PDF)`,
**`À vérifier`** (liste de drapeaux : `arabe_vide`, `arabe_divergent`,
`chiffres_dans_mot`, `suite_de_chiffres`, `caracteres_suspects`,
`sans_point_final`, `translit_vide`, `vedette_courte`). Le fichier Excel a
une deuxième feuille ne contenant que les lignes à drapeau (hors
`arabe_vide`, trop fréquent pour être utile seul), et surligne ces mêmes
lignes en jaune dans la feuille principale.

## Adapter à un autre livre

Le bloc `CHAPTERS` en tête de fichier encode la table des matières de CE
livre ; `PAGE_OFFSET` = décalage pagination PDF / imprimée.
