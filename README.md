# Mujam-OCR

**Hybrid OCR + vision-language-model pipeline that turns scanned bilingual (Arabic / French) dictionaries into clean, structured datasets** (CSV / Excel / JSON, one row per name).

- **Layout analysis without AI**: image processing splits each page into zones (286 pages, 0 failures, 1,720 entries, ≈ 0.65 s/page), so the model only reads small crops.
- **Local vision LLM** (gemma-4-12b-qat served by LM Studio) reads the vowelled Arabic: on a full run, **4.0 % of readings are inconsistent with the transliteration, versus 25.8 % for bonsai-27b**.
- **Benchmark** on 64 Arabic zones: best full-page reader gemma-4-26b-a4b reaches **100 % recall and 0.5 % character error rate**; Tesseract, EasyOCR and several vision models are compared.
- **Result on the reference book**: 3,201 rows / 1,684 entries, 75 flagged for manual review, with verifiers for Arabic (transliteration check) and French (spell check), a manual review tool and per-document profiles.
- **Engineering**: 237 tests, 23 dated versions (one commit each, tagged), documented errors and lessons learned.
- **Stack**: Python, OpenCV, Tesseract, EasyOCR, PyMuPDF, LM Studio (local LLM), Textual (terminal UI).

---

# Mujam-OCR — extraction de dictionnaires de prénoms arabes (OCR et vision sur un livre bilingue arabe / français)

Projet commencé le **28 septembre 2026** : transformer des livres de prénoms **scannés** (français + arabe voyellé, sans couche de texte) en tableaux exploitables (CSV / Excel / JSON), **une ligne par prénom**, avec l'arabe diacrité, le genre, la définition et les orthographes associées.
Document de départ : *Le livre des prénoms arabes* (Y. et N. Geoffroy), 448 pages, dictionnaire = pages PDF 53-338. Depuis la v5, d'autres dictionnaires sont gérés par des **profils**.

> **Ce dépôt n'est pas un simple code : c'est le journal d'un moteur de lecture qui a changé trois fois de technologie.** Chaque commit est une version du projet, daté de sa vraie date de création ; chaque version a sa note dans [`docs/versions/`](docs/versions/). Le guide d'utilisation technique de la dernière version est dans [`docs/GUIDE_TECHNIQUE.md`](docs/GUIDE_TECHNIQUE.md).

## En un coup d'œil

| | Début (v1, 28 sept.) | Fin (v5.4, 5 nov.) |
|---|---|---|
| Méthode | 3 passes OCR (Tesseract + EasyOCR) | Mise en page par traitement d'image + modèle de vision local (gemma-4-12b-qat via LM Studio) + Tesseract pour le français, avec vérificateurs |
| Lignes de code (hors tests) | 822 | 7 361 |
| Tests | 0 | 237 |
| Sortie sur le livre de Geoffroy | 2 530 lignes, arabe « pas parfait » | **3 201 lignes, 1 684 entrées, 75 lignes « à vérifier »**, arabe incohérent avec la translittération : 4,0 % au run gemma, puis 96 mots revus à la main |
| Vitesse du modèle | ≈ 8 tok/s | 17 tok/s après correction des réglages, puis **≈ 64 tok/s** après passage à contexte 12 288 / parallèle 1 |

## Les trois générations du moteur

1. **OCR classique (v1-v2, 28-29 sept.)** — Tesseract `fra+ara` pour la structure et le français, EasyOCR pour relire l'arabe et les mots douteux. Local, gratuit, mais l'arabe voyellé calligraphique est mal lu et les orthographes empilées d'un même prénom sont prises pour des prénoms différents. Jugé imprécis.
2. **Modèle de vision sur la page entière (v3-v4.1, 29 sept. - 6 oct.)** — chaque page est envoyée à un modèle de vision servi par LM Studio (bonsai-27b) qui renvoie du JSON. Gros progrès de structure, mais lent (≈ 8 tok/s), arabe confondu (32 % de lignes sans voyelle sur un run v4), boucles de génération.
3. **Lecture hybride (v4.2 → v5.4, depuis le 19 oct.)** — la page est découpée **sans modèle** (traitement d'image : légende = filets, genre = couleur noir/rouge, ligne de nom, définition), Tesseract lit le français, le modèle de vision ne lit que de petites zones (têtes, arabe) sur une « feuille de recadrages », avec repli sur la page entière. Ajouts : vérificateur d'arabe par translittération, relecture ciblée, vérificateur de français, revue manuelle, complétion des pages perdues.

La mise en page seule (sans IA) donne le plan du livre : sur les 286 pages, **271 pages d'entrées, 15 pages de texte, 0 échec, 1 708 entrées (1 720 après correction en v4.5.3), ≈ 0,65 s/page** — c'est ce qui permet de ne payer le modèle que sur de petites zones (hybride : 7,9 s/page avec gemma, ≈ 25 s/page avec bonsai ; page entière avec bonsai : ≈ 70 s/page).

## Comparaison des modèles pour lire le livre (v4.3.3 - v4.3.4, 28 oct.)

Machine : Ryzen 9 9955HX, RTX 5070 Ti 12 Go, 32 Go de RAM, LM Studio.

**Autotest : 4 pages à vérité terrain lues à l'œil, mode hybride**

| Modèle | arabe exact | avec voyelles | vedettes | s/page | tok/s |
|---|---|---|---|---|---|
| **google/gemma-4-12b-qat** | **22/22** | **35/35** | 35/35 | **7,9** | 55,5 |
| prism-ml/bonsai-27b | 10/22 | 14/35 | 35/35 | 9,1 | 58,1 |
| ornith-1.0-9b | 17/22 | 13/20 | 20/35 | 47,7 | 77,8 |

**Run complet sur 286 pages (≈ 40 min)**

| | lignes | arabe incohérent avec la translittération | lignes sans voyelle | lignes sans arabe | lettres latines dans l'arabe |
|---|---|---|---|---|---|
| **gemma-4-12b-qat** | 3 165 | **4,0 %** | 108 | 15 | 2 |
| bonsai-27b | 3 156 | 25,8 % | 1 301 | 334 | 21 |
| ornith-1.0-9b | arrêté à la p. 65 | ≈ 15 % | – | – | – |

Réflexion (« thinking ») : sur 18 requêtes de relecture, les 18 ont épuisé le budget de 1 500 jetons sans répondre → relecture **sans** réflexion par défaut.

## Benchmark de la lecture de l'arabe (v5.2 - v5.3, 4 nov.)

Vérité terrain : **64 zones arabes sur 5 pages de livres différents** (`benchmark/verite_arabe.json`, écrite par Claude d'après les images : à faire relire). CER = taux d'erreur par caractère, voyelles ignorées.

**Lecture d'une zone isolée (recadrage)**

| Lecteur | CER | Remarque |
|---|---|---|
| Tesseract `ara` fast, psm 7, ×3, binarisé | 6,1 % (79,7 % exacts) | mesuré dans le bac à sable de Claude, Tesseract 5.3.4 |
| Tesseract `ara` best, mêmes réglages | 8,9 % | |
| Tesseract sans agrandissement ni binarisation | 17,8 % | le prétraitement compte |
| EasyOCR (CPU, 0,08 s/zone) | 37,1 % | 8 % sur arabe voyellé rose, 66 % sur colonnes sans voyelles |
| gemma-4-12b-qat | 36,3 % | **dont 23 réponses vides sur 64** ; sur les 41 réponses : **0,6 %** |
| gemma + EasyOCR d'accord | 0,0 % d'erreur | sur 28 % des zones seulement |
| Oracle (meilleur des lecteurs à chaque zone) | 7,7 % | plafond d'une combinaison parfaite |

**Page entière (repérage + lecture), mesures de l'utilisateur**

| Modèle | rappel | précision | CER des zones trouvées |
|---|---|---|---|
| **gemma-4-26b-a4b** | **100 %** | **98,6 %** | **0,5 %** |
| gemma-4-12b-qat | 96,9 % | 95,4 % | 2,0 % |
| EasyOCR | 92,2 % | 78,7 % | 6,7 % |
| gemma-3-12b | 90,6 % | 93,4 % | 4,8 % |
| gemma-4-12b (hors QAT) | 78,1 % | 76,9 % | 1,2 % |

À retenir : (1) le modèle de vision lit presque parfaitement **quand il répond** ; son vrai défaut est le **silence** (réponse vide) ; (2) le prompt compte : la page 8 du profil Abdur Rahman avait 0 arabe avec le prompt JSON complet et 17/17 avec le prompt court « liste l'arabe » ; (3) un mauvais indice OCR **empoisonne** le modèle (77 % d'erreur avec indice Tesseract médiocre) ; (4) Tesseract seul ne retrouve aucune zone en page entière mais lit bien un recadrage préparé.
Non testés : PaddleOCR, Unlimited-OCR (Baidu), Qwen3-VL, olmOCR 2, Qari-OCR et les autres modèles du catalogue `--cites`.

## Progression de la qualité de la sortie

| Étape | Sortie | Mesure de qualité |
|---|---|---|
| v1 (28 sept.) | 2 530 lignes | 10 entrées vérifiées, arabe encore faux sur plusieurs |
| v2 (29 sept.) | 2 601 lignes | jugé imprécis |
| v4 (≈ 6 oct., run utilisateur p. 55-77) | 180 lignes | **32 %** des lignes sans voyelle |
| v4.2 (19 oct., bonsai hybride) | 1 878 lignes | 24 % d'arabe incohérent, 402 lignes sans voyelle |
| v4.3.4 (28 oct., gemma hybride) | 3 165 lignes | **4,0 %** incohérent, 108 sans voyelle |
| v4.4.1 (29 oct.) | 3 165 lignes | 96 mots revus à la main : 87 arabes et 18 translittérations corrigés |
| v4.5.1 (1 nov.) | 3 165 lignes | définitions avec ≥ 1 mot suspect : 18,0 % → 8,8 % |
| v4.5.4 (2 nov.) | **3 201 lignes / 1 684 entrées** | 75 « à vérifier » ; ≈ 430 lignes (13 %) débarrassées de voyelles de déclinaison que le livre n'imprime pas |

Les nombres de lignes ne sont comparables qu'à partir de la v4.3 (avant, doublons, orthographes empilées et formes ne sont pas comptés de la même façon).

## Chronologie des 23 versions

| # | Date (Paris) | Version | Titre | Résultat clé | Lignes de code / tests |
|---|---|---|---|---|---|
| 1 | 28 sept. 07:17 | [v1](docs/versions/code_extraction_locale.md) | Pipeline OCR local à 3 passes | OCR 3 passes (Tesseract + EasyOCR) → 2 530 lignes | 822 / 0 |
| 2 | 29 sept. 00:22 | [v2](docs/versions/code_extraction_locale_v2.md) | OCR multi-cœur + GPU, cache par page | OCR multi-cœur + GPU → 2 601 lignes, jugé imprécis | 1 306 / 13 |
| 3 | 29 sept. 23:17 | [v3](docs/versions/extraction_prenoms_llm_v3.md) | Modèle de vision via LM Studio (page entière → JSON) | 1er passage à un modèle de vision (page entière → JSON) | 1 307 / 34 |
| 4 | 6 oct. 23:02 | [v4.1](docs/versions/extraction_prenoms_llm_v4_1.md) | Streaming, journaux, TUI, reprise | Streaming, journaux, TUI ; 32 % de lignes sans voyelle (v4) | 3 671 / 112 |
| 5 | 19 oct. 23:32 | [v4.2](docs/versions/extraction_prenoms_llm_v4_2.md) | Lecture hybride : le bon outil pour chaque zone | Lecture hybride ; 1 708 entrées détectées par la mise en page ; 1 878 lignes | 4 549 / 129 |
| 6 | 20 oct. 14:42 | [v4.2.1](docs/versions/extraction_prenoms_llm_v4_2_1.md) | Premier vrai run sur la machine : correctifs de repli | 17 tok/s ; repli injustifié 33 % → ≈ 10 % | 4 563 / 132 |
| 7 | 20 oct. 22:48 | [v4.3.0](docs/versions/extraction_prenoms_llm_v4_3_0.md) | Vérification de l'arabe par la translittération | Vérificateur d'arabe : 24 % de lectures incohérentes | 4 779 / 137 |
| 8 | 27 oct. 22:34 | [v4.3.1](docs/versions/extraction_prenoms_llm_v4_3_1.md) | Audit complet + relecture ciblée de l'arabe | Audit ; `--relire-arabe` ; boucle p. 98 (86 entrées pour 7) | 4 963 / 142 |
| 9 | 27 oct. 22:42 | [v4.3.2](docs/versions/extraction_prenoms_llm_v4_3_2.md) | Comparaison automatique de modèles | Comparateur automatique de modèles | 5 340 / 147 |
| 10 | 28 oct. 11:51 | [v4.3.3](docs/versions/extraction_prenoms_llm_v4_3_3.md) | Premiers résultats de comparaison de modèles | gemma 22/22 arabe exact vs bonsai 10/22 | 5 351 / 148 |
| 11 | 28 oct. 22:34 | [v4.3.4](docs/versions/extraction_prenoms_llm_v4_3_4.md) | Run complet gemma vs bonsai + correction d'une erreur d'audit | Run complet : gemma 4,0 % d'incohérence vs bonsai 25,8 % | 5 363 / 150 |
| 12 | 29 oct. 09:11 | [v4.4.0](docs/versions/extraction_prenoms_llm_v4_4_0.md) | Revue manuelle + relecture des têtes | Relecture de 72 mots : 11 corrigés ; revue manuelle | 5 800 / 160 |
| 13 | 29 oct. 20:55 | [v4.4.1](docs/versions/extraction_prenoms_llm_v4_4_1.md) | Premier retour de la revue manuelle | 31 têtes corrigées / 56 ; 87 arabes corrigés à la main | 5 831 / 162 |
| 14 | 30 oct. 11:58 | [v4.5.0](docs/versions/extraction_prenoms_llm_v4_5_0.md) | Français des définitions et prénoms mixtes | Français : ≈ 17 % de définitions fautives (borne haute) | 6 047 / 171 |
| 15 | 1 nov. 19:44 | [v4.5.1](docs/versions/extraction_prenoms_llm_v4_5_1.md) | Premier run de --relire-corps | `--relire-corps` : 198 mots corrigés ; suspects 18,0 % → 8,8 % | 6 066 / 173 |
| 16 | 1 nov. 20:26 | [v4.5.2](docs/versions/extraction_prenoms_llm_v4_5_2.md) | 2ᵉ passe de --relire-corps | 2ᵉ passe : 5 mots corrigés (prompt sans indice) | 6 086 / 175 |
| 17 | 2 nov. 21:59 | [v4.5.3](docs/versions/extraction_prenoms_llm_v4_5_3.md) | Entrées perdues par la mise en page | 3ᵉ passe : 17 mots ; entrées perdues découvertes (1 708 → 1 720) | 6 118 / 177 |
| 18 | 2 nov. 22:23 | [v4.5.4](docs/versions/extraction_prenoms_llm_v4_5_4.md) | --completer-pages et voyelles finales | 19 pages complétées : 3 201 lignes, 1 684 entrées | 6 133 / 178 |
| 19 | 3 nov. 22:22 | [v5.0.0](docs/versions/extraction_prenoms_llm_v5_0_0.md) | Profils de documents | 9 profils de documents ; livre de Geoffroy « terminé » | 6 325 / 187 |
| 20 | 4 nov. 07:14 | [v5.1.0](docs/versions/extraction_prenoms_llm_v5_1_0.md) | La TUI applique les profils | La TUI applique les profils | 6 402 / 197 |
| 21 | 4 nov. 13:43 | [v5.2.0](docs/versions/extraction_prenoms_llm_v5_2_0.md) | Benchmark de la lecture de l'arabe | Benchmark de lecture de l'arabe (64 zones) | 6 888 / 215 |
| 22 | 4 nov. 23:34 | [v5.3.0](docs/versions/extraction_prenoms_llm_v5_3_0.md) | Benchmark complet (tous les modèles de vision) | gemma-4-26b-a4b 100 % de rappel, CER 0,5 % | 7 121 / 227 |
| 23 | 5 nov. 12:36 | [v5.4.0](docs/versions/extraction_prenoms_llm_v5_4_0.md) | Benchmark « --cites » : installer et tester les outils choisis | `--cites` : installation et test des outils choisis | 7 361 / 237 |

## Erreurs qui ont coûté du temps (et ce qu'on en a retenu)

- **Boucle de génération (p. 98)** : 8 000 jetons, 550 s, 86 « entrées » pour 7 attendues → détection de boucle et dédoublonnage (v4.3.1).
- **Pages de titre prises pour des pertes** : l'audit de la v4.3.1 concluait à tort à des entrées perdues ; 11 pages étaient des pages de titre. Un « rescue » y fabriquait ≈ 40 entrées fantômes (corrigé en v4.3.4). *Vérifier l'image avant de conclure.*
- **≈ 20 entrées réellement perdues sans avertissement** (v4.5.3) : un seuil de 95 px < arabe de 97 px. *Un contrôle par comptage cache vs mise en page aurait dû exister dès la v4.3.*
- **1 h 30 perdues sur 2 h 04** lors du premier `--relire-corps` : LM Studio muet, 3 essais × 300 s × 6 pages, sans alerte (garde-fou ajouté en v4.5.1).
- **Voyelles de déclinaison inventées** : ≈ 430 lignes (13 %) avec des marques que le livre n'imprime pas (v4.5.4).

## Limites connues et prochaines étapes

- Les 7 profils autres que Geoffroy n'ont **jamais été testés avec un vrai modèle** à la date de la v5.4 (seule la sortie Abdur Rahman existe : 1 418 lignes, 126 sans arabe, 7 pages sur 80 sans aucun arabe).
- La vérité terrain du benchmark (64 zones) est petite et écrite par Claude : des écarts de quelques points ne sont pas significatifs.
- Les noms de modèles du catalogue `--cites`, `lms get`, PaddleOCR et le chemin Tesseract sous Windows sont **non testés**.
- **Prochaine étape prioritaire** : une « deuxième passe arabe » dans le moteur (relancer avec le prompt court « liste l'arabe » quand une page rend des entrées sans arabe), puis zonage par profil si le benchmark le justifie.
- Reste mineur sur le livre de Geoffroy : 2 corrections manuelles à revoir (p. 191 et p. 153), 42 `genre_a_verifier` non mixtes, 11 pages courtes non examinées.

## Reproduire / naviguer dans l'historique

```bash
git log --oneline --date=iso          # une version = un commit, daté de sa création
git show v5.4.0 --stat                # tags : v1…v5.4.0 (v1 n'a pas de tag)
git diff v4.5.4 v5.0.0 --stat         # ce qui a changé entre deux versions
```

Les sorties (`sortie*/`, `comparaison/`, caches `.pkl`, journaux), les PDF des livres et les archives ne sont **pas** versionnés (voir `.gitignore`) : ce sont des données de travail, et les livres sont protégés par le droit d'auteur.

## Sources et méthode des chiffres

Les nombres viennent de `REPRISE.md`, `AUDIT.md` et `README.md` des archives, des rapports `comparaison/comparaison_modeles.md` et des exports des dossiers de sortie. Les lignes de code et les tests sont **recomptés** sur chaque archive (`def test_`). Les dates sont celles de la dernière modification des fichiers de chaque archive, lues en UTC puis converties à l'heure de Paris ; la date indiquée dans `REPRISE.md` peut différer quand une archive a été re-zippée plus tard (cas signalé dans la note de la v4.3.1).
