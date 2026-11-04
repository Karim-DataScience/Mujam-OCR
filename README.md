# Extraction du « Livre des prénoms arabes » — v4.2 (lecture hybride, LM Studio, streaming, TUI, journaux)

Chaque page du PDF scanné est envoyée à un modèle de vision servi par LM Studio (ex. `prism-ml/bonsai-27b`), qui renvoie du
JSON. Sortie : **une ligne par prénom** (« Youssouf / Youssef » → deux lignes), en CSV, JSON et Excel.
Tout se pilote **depuis une interface plein écran (TUI)** ou en **ligne de commande** ; tout est **journalisé dans un fichier**.

## Démarrage rapide

```
pip install -r requirements.txt
python extraire_prenoms_llm.py            # sans argument, dans un terminal : ouvre la TUI
```
Sous Windows, `lancer.bat` (double-clic) fait la même chose ; Windows Terminal est conseillé pour la TUI.
Premier passage dans la TUI : **Réglages (F5)** → PDF, URL de LM Studio, modèle → *Enregistrer* ; **Accueil** → *Tester LM
Studio*, puis *Diagnostic complet* ; **Lancer / reprendre** (F7).

En ligne de commande (mêmes réglages, même cache, même journal) :
```
python extraire_prenoms_llm.py --diagnostic livre.pdf     # configuration chargée, vitesse réelle, GPU, réglages conseillés
python extraire_prenoms_llm.py livre.pdf --autotest       # 4 pages avec vérité terrain : score + durée par page
python extraire_prenoms_llm.py livre.pdf                  # tout le dictionnaire (pages PDF 53 à 338), reprenable
python extraire_prenoms_llm.py --statut                   # où en est-on ? (sans LM Studio)
python extraire_prenoms_llm.py --export                   # régénère CSV/JSON/Excel depuis le cache (sans LM Studio)
python extraire_prenoms_llm.py --rapport                  # zip de debug à m'envoyer
python extraire_prenoms_llm.py --cli                      # aide, sans ouvrir la TUI
```
LM Studio ≥ 0.4.0 (API native : réflexion pilotable, statistiques). Une version plus ancienne fonctionne en repli sur l'API OpenAI.

## Lecture hybride (nouveau en v4.2) : le bon outil pour chaque zone

Le modèle de vision lisant la page entière se trompait surtout sur la **structure** (orthographes empilées prises pour des prénoms
distincts, genre inversé), sur l'**arabe** (lettres confondues, voyelles perdues) et sur les **accents** des définitions. La lecture
hybride découpe donc le travail (`--methode auto`, défaut : hybride si Tesseract est installé, sinon modèle seul) :

| Zone | Outil | Coût |
|---|---|---|
| structure, **genre (couleur)**, orthographes empilées, définitions, légende, notes de bas de page | traitement d'image classique (`mise_en_page.py`) | ≈ 0,6 s / page, CPU |
| noms français, corps des définitions (césures recollées, « Il » lu « 11 » corrigé) | **Tesseract** `fra` (`lecture_hybride.py`) | ≈ 2-8 s / page, CPU |
| arabe voyellé de chaque bloc + « tête » de chaque définition (translittération avec ses â î û ḥ ṣ ḍ ṭ ẓ) | **modèle de vision**, sur UNE petite feuille de recadrages numérotés par page | ≈ 200 jetons de sortie au lieu de ≈ 2 000 |
| page dont la mise en page n'est pas comprise, ou feuille illisible, ou Tesseract en panne | **repli automatique** sur la lecture de la page entière par le modèle de vision | comme avant |
| pages de texte courant (introductions de chapitre) | rien : aucune requête | 0 |

La mise en page a été validée sur les 286 pages du dictionnaire : 271 pages d'entrées sans anomalie, 15 pages de texte, 0 échec ; 1 708 entrées,
2 253 blocs de prénoms (la couverture annonce « plus de 2 200 prénoms »), 939 orthographes empilées.

Installation de Tesseract : Windows → installateur « UB Mannheim » en cochant **French** ; puis `pip install pytesseract opencv-python-headless`.
Pour un meilleur français, mets `fra.traineddata` du dépôt `tessdata_best` dans un dossier et donne `--tessdata ce\dossier`.
Options : `--methode auto|hybride|vlm`, `--tesseract chemin\tesseract.exe`, `--tessdata dossier`, `--tess-lang fra`. La méthode fait partie de l'empreinte du
cache (changer de méthode recalcule). `--decoupe 2` est incompatible (la lecture hybride analyse la page entière). Pour répartir la charge,
`--paralleles 2` ou `3` fait tourner Tesseract (CPU) sur une page pendant que le GPU lit la feuille d'une autre.
Ce qui n'est **pas** encore vérifié : la qualité de l'arabe et des têtes lues par TON modèle sur les feuilles (lance `--autotest --methode hybride`).

## Interface TUI

| Onglet | Contenu |
|---|---|
| **F1 Accueil** | état de LM Studio (modèle, contexte, requêtes simultanées, Flash Attention, vision, réflexion pilotable), avancement du cache, chemins ; boutons *Tester LM Studio*, *Diagnostic complet*, *Lancer / reprendre*, *Exporter*, *Rapport de debug* |
| **F2 Extraction** | pages, réflexion, découpe, requêtes simultanées, « refaire » ; **en direct** : barre de progression, page/min, durée restante, **GPU** (utilisation, VRAM, puissance), une ligne par requête en cours (phase, jetons, réflexion, tok/s, durée), journal |
| **F3 Pages** | une ligne par page : statut (ok / partiel / périmé / à faire / blanche / échec), entrées, durée, tok/s, remarque. *Espace* coche une page ; *Cocher à refaire* (échecs, partielles, périmées, à faire) ; *Refaire sans / avec réflexion* ; *Sortie brute* (texte reçu du modèle) ; *Image* (ouvre la page du scan) |
| **F4 Résultats** | toutes les lignes extraites, filtre texte (prénom, arabe, description), « seulement à vérifier », compteurs |
| **F5 Réglages** | tous les réglages, enregistrés dans `prenoms_config.json` ; *Valeurs par défaut* |
| **F6 Journal** | journal en direct, chemin du fichier de log, *Ouvrir le log*, *Rapport de debug*, *Effacer l'affichage* |

Touches : **F7** lancer / reprendre · **F8** arrêter (les pages terminées sont conservées) · **Q** quitter (confirmation si une extraction
tourne) · **Ctrl+P** palette de commandes. Dans les champs de saisie, les lettres sont saisies normalement (aucun raccourci ne les capte).

## Journaux et rapport de debug (pour m'aider à corriger)

Chaque session écrit, dans `sortie_llm/logs/` (créé à côté du script, ou `--sortie`) :

| Fichier | Contenu |
|---|---|
| `AAAAMMJJ_HHMMSS_<mode>.log` | texte lisible : heure, niveau (DEBUG / INFO / WARN / ERROR), **fil d'exécution**, message. En tête : version, arguments, **environnement** (Python, système, paquets, GPU) et **configuration**. Puis, pour chaque requête : paramètres, issue (`finish`, `abandon`), jetons, réflexion, tok/s, durée, fin de la réponse et de la réflexion en cas d'échec ; les **traces d'erreur complètes** ; une ligne GPU par minute |
| `AAAAMMJJ_HHMMSS_<mode>.jsonl` | un événement structuré par ligne : `demarrage`, `cache`, `modele`, `calibrage`, `requete`, `page`, `gpu` (toutes les 5 s), `export`, `diagnostic`, `exception`, `fatal` |

Les fichiers sont écrits au fil de l'eau (un plantage laisse la trace) ; les 30 sessions les plus récentes sont gardées.
Le jeton d'API n'apparaît **jamais** (masqué partout). En cas de problème :

```
python extraire_prenoms_llm.py --rapport        # ou le bouton « Rapport de debug » de la TUI
```
→ `sortie_llm/rapports/rapport_debug_AAAAMMJJ_HHMMSS.zip` : les 3 derniers `.log`, le `.jsonl` de la dernière session, l'environnement,
la configuration (prompt résumé par son empreinte), le résumé de chaque page du cache (`pages.csv`) et les dernières sorties brutes des
essais ratés. **Ni PDF, ni images, ni secret.** C'est ce zip qu'il faut m'envoyer. `--log-niveau INFO` allège le fichier (par défaut : tout).

## Configuration

`prenoms_config.json` (à côté du script ; autre chemin avec `--config` ou la variable `PRENOMS_CONFIG`). Priorité : valeurs par défaut <
fichier de configuration < options de la ligne de commande. La TUI le lit et l'écrit ; `--ecrire-config` y enregistre les options données :
`python extraire_prenoms_llm.py --url http://localhost:1234 --reflexion non --ecrire-config`.

## Audit de la v3 : ce qui n'allait pas

| Ce que tu as observé | Cause | Correction |
|---|---|---|
| Une page ne finit jamais (plus de 8 000 jetons) | Le modèle **réfléchit par défaut** (réglage « Enable Thinking » de LM Studio, actif par défaut). Pour recopier une page, la réflexion ne sert presque à rien. La v3 laissait 16 000 jetons et 30 min par requête : à 8 tok/s, 16 000 jetons font 33 min. | Réflexion **coupée par défaut** quand LM Studio l'expose (`reasoning: "off"`) ; sinon bornée par un budget, avec détection de boucle et abandon. |
| Même symptôme, **si** le contexte chargé est de 16 384 | Image + consigne (~2 000) + 16 000 de sortie dépassent le contexte : LM Studio applique sa règle de débordement et le modèle peut perdre l'image et la consigne. | Jetons d'entrée **mesurés** ; sortie bornée par le contexte chargé ; erreur claire avec la commande `lms load`. |
| Aucune visibilité, aucun arrêt | Requêtes sans streaming ; un délai dépassé ou un Ctrl+C laissait LM Studio générer dans le vide. | **Streaming** : jetons et tok/s en direct, connexion fermée dès qu'on abandonne. |
| « La reprise ne marche pas » | Cache écrit seulement à la fin d'une page ; Ctrl+C parfois sans effet sous Windows ; dossier de sortie dépendant du dossier courant. | Cache par page, export intermédiaire, attente par pas de 0,5 s, dossier de cache affiché, `--statut`, `--export`, bouton *Arrêter*. |
| 8 tok/s | voir ci-dessous | `--diagnostic` / TUI : mesure et réglages conseillés. |

**Sur les 8 tok/s (hypothèse à vérifier avec `--diagnostic`).** En génération, le débit est borné par *bande passante mémoire ÷ taille du modèle*.
Un modèle ternaire d'environ 7 Go lu depuis la RAM (DDR5 double canal, de l'ordre de 80 à 90 Go/s) plafonne vers 10-12 tok/s ; lu depuis la
VRAM d'une carte récente (plusieurs centaines de Go/s), il atteint plusieurs dizaines de tok/s. 8 tok/s ressemble donc à un calcul fait, au moins
en partie, **par le CPU** : « GPU Offload » incomplet, runtime llama.cpp de LM Studio trop ancien pour ce format, VRAM saturée (débordement
silencieux vers la RAM sous Windows), mode d'alimentation Silencieux. Le script ne peut pas le corriger à ta place : il le **mesure** (jusqu'à
la consommation GPU pendant l'extraction) et te dit quoi régler.

## Réglages LM Studio conseillés (carte de 12 Go de VRAM)

À régler dans la fenêtre de chargement du modèle (« Manually choose model load parameters »). Le diagnostic affiche les valeurs actuelles et la commande adaptée.

| Réglage | Valeur | Pourquoi |
|---|---|---|
| Runtime (Ctrl+Shift+R) | dernier **llama.cpp CUDA** disponible, à jour | un runtime ancien peut manquer des noyaux GPU du format du modèle : le calcul retombe sur le CPU |
| GPU Offload | **maximum** | chaque couche restée sur le CPU ramène la vitesse vers celle de la RAM |
| Context Length | ≈ **12 288 à 16 384** (valeur exacte donnée par `--diagnostic`) | assez pour image + consigne + sortie ; plus grand = plus de VRAM pour le cache KV |
| Flash Attention | activé | moins de mémoire, plus rapide |
| Max Concurrent Predictions | **1** (ou la valeur de `--paralleles`) | LM Studio charge 4 par défaut |
| Evaluation Batch Size | 1024 | accélère le préremplissage de l'image |
| Offload KV Cache to GPU Memory | activé | |
| Enable Thinking (réglage du modèle) | désactivé si le diagnostic dit que la réflexion n'est pas pilotable par l'API | sinon le script la borne, mais la couper fait gagner l'essentiel du temps |
| Windows : mode d'alimentation | Performance / Turbo, **sur secteur** | le mode Silencieux bride le GPU |
| Panneau NVIDIA → « CUDA - Sysmem Fallback Policy » | « Prefer No Sysmem Fallback » | une VRAM saturée provoque une erreur au lieu d'un ralentissement silencieux |

Équivalent en ligne de commande (éjecte d'abord le modèle dans LM Studio) :
`lms load prism-ml/bonsai-27b --context-length 16384 --parallel 1 --gpu max --identifier prism-ml/bonsai-27b -y`

La variante **1-bit** de Bonsai 27B (~3,9 Go) laisse beaucoup de VRAM libre mais est moins fidèle que la ternaire (~7,2 Go) : arbitrage vitesse / qualité à trancher avec `--autotest`.

## Réflexion, image, reprise

- **Réflexion** : `--reflexion auto` (défaut) coupe la réflexion si LM Studio expose le réglage, sinon la laisse mais la borne ; `non` la coupe ; `oui` l'autorise, abandonnée au-delà de `--budget-reflexion` jetons ou si elle boucle. Si LM Studio ignore « off », le script passe en « avec réflexion bornée » pour la suite et te dit comment la couper côté LM Studio. Stratégie en deux passes : tout sans réflexion (rapide), puis, pour les seules pages signalées (onglet Pages → cocher → *Refaire avec réfl.*), la réflexion.
- **Image** : `--tokens-image 2500` (défaut) mesure les jetons d'image réels et **réduit l'image** pour viser cette cible (`0` = image native). `--max-cote N` impose un plafond en pixels ; `--decoupe 2` envoie chaque page en deux moitiés.
- **Reprise** : une page est « faite » dès que son JSON est dans `sortie_llm/cache/`. Le cache est invalidé tout seul si tu changes le prompt, le modèle, `--decoupe`, `--nettoyage`, `--max-cote` ou `--tokens-image` (le journal l'indique) ; `--refaire` force le recalcul. Export intermédiaire tous les `--export-tous` pages ; arrêt (Ctrl+C ou F8) exporte ce qui est fait. `--export` reconstruit les fichiers depuis le cache sans LM Studio (pages d'une autre configuration conservées, avec avertissement).

## Colonnes

| Colonne | Contenu |
|---|---|
| `prénom_fr` | **une** orthographe française |
| `prénom_arab_diacritiques` | l'arabe imprimé, voyelles brèves comprises, sans les barres d'allongement (tatwil) |
| `genre` | `MASCULIN` (noir, colonne gauche) ou `FEMININ` (rouge, colonne droite) |
| `description_en_français` | le paragraphe complet, mots coupés recollés, appels de note et « * » retirés, suites d'une page à l'autre fusionnées |
| `prénoms_équivalents` | les **autres orthographes** du même prénom (Youssouf → Youssef) |
| `forme_associée` | le prénom de l'autre genre dans la même entrée (Nadil ↔ Nadila) |
| `équivalent_étranger` | ce qui est entre parenthèses dans la définition (Yûsuf **(Joseph)** → Joseph) |
| `translittération` | le mot avant « : » dans la définition |
| `thème`, `sous_thème` | déduits de la page grâce à la table des matières |
| `page_imprimée`, `page_pdf`, `id_entrée` | traçabilité |
| `à_vérifier` | drapeaux séparés par « ; » : `arabe_vide`, `arabe_non_arabe`, `arabe_sans_voyelles`, `description_vide`, `description_courte`, `description_coupee`, `sans_point_final`, `chiffres_dans_prenom`, `caracteres_suspects`, `prenom_court`, `genre_a_verifier`, `page_partielle` |

Le classeur contient **Prénoms** (lignes à vérifier surlignées), **À vérifier** et **Pages** (statut, entrées, durée, jetons d'entrée / de sortie /
de réflexion, tok/s, remarque). Repère de complétude : l'ancienne extraction compte 2 601 entrées, soit ≈ 3 240 lignes une fois éclatées.
`--ancien prenoms_arabes_corrige.csv` compare page par page.

## Options utiles

| Option | Effet |
|---|---|
| `--pages 55,56,100-104` | liste explicite (remplace `--debut` / `--fin`, défaut 53 → 338) |
| `--paralleles N` | requêtes simultanées. À essayer **après** avoir réglé la vitesse d'une requête : LM Studio doit avoir « Max Concurrent Predictions » ≥ N et un contexte ≥ N × (entrée + sortie) ; le script réduit N si le contexte ne suffit pas et affiche `page/min` |
| `--max-tokens 8000` | sortie max par requête, réflexion comprise (bornée par le contexte chargé) |
| `--timeout 1200`, `--stall 300`, `--attente 900` | durée totale d'une requête, silence max entre deux jetons, attente du préremplissage |
| `--temperature`, `--top-p`, `--top-k`, `--repeat-penalty` | défauts : 0,3 / 0,8 sans réflexion ; 0,6 / 0,95 avec ; top-k 20 |
| `--suivi 20` | secondes entre deux lignes de progression pendant une requête (0 = aucune) |
| `--essais 3` | tentatives par page (température +0,15 à chaque essai ; bascule sans/avec réflexion selon l'échec) |
| `--api natif\|openai`, `--cle-api` | force l'API (défaut `auto`) ; jeton si l'authentification LM Studio est activée (ou variable `LM_API_TOKEN`) |
| `--corps-extra '{...}'`, `--prompt-fichier p.txt` | JSON fusionné dans la requête ; prompt personnalisé (invalide le cache) |
| `--sauver-images` | garde dans `sortie_llm/images` les PNG réellement envoyés |

## Dépannage

| Message | Cause probable |
|---|---|
| `Serveur LM Studio injoignable` | serveur non démarré (onglet Developer), mauvais `--url` |
| `Le modèle « … » n'est pas chargé` | charge-le avec la commande `lms load …` affichée (un chargement automatique utiliserait un contexte minuscule) |
| `Contexte trop court` | recharge avec le contexte indiqué, ou `--tokens-image 1500` |
| `n'accepte pas d'image` | module vision (mmproj) absent du modèle chargé |
| `LM Studio ignore la désactivation de la réflexion` | décoche « Enable Thinking » dans les réglages du modèle |
| `abandon … boucle_reflexion` / `budget_reflexion` | réflexion trop longue ou en boucle ; textes partiels dans `cache/brut/` (TUI : Pages → *Sortie brute*) |
| `silence` | plus de jeton pendant `--stall` secondes (modèle bloqué ou machine saturée) |
| `LM Studio refuse l'accès (HTTP 401/403)` | authentification de l'API activée : jeton avec `--cle-api` ou `LM_API_TOKEN` |
| `réponse sans aucun événement reconnu` | version de LM Studio différente de la documentation 0.4 : essaie `--api openai` |
| La TUI ne s'ouvre pas | `pip install textual` ; sinon la ligne de commande reste entièrement utilisable |
| Autre erreur | `--rapport` (ou le bouton), puis envoie le zip |

Codes de sortie : 0 ok · 1 erreur inattendue (trace dans le log) · 2 erreur fatale (rien n'est exporté si aucune page n'est disponible) · 130 interrompu.

## Ce qui est testé, et ce qui ne l'est pas

- **Testé** (`python test_extraire_prenoms_llm.py`, 129 tests, ≈ 3 min ; `python test_prenoms_tui.py`, 23 tests, ≈ 2 min) : le moteur face à un **faux serveur LM Studio**
  (API native et OpenAI, streaming chunké ou non, pannes : boucle de réflexion, budget dépassé, réglage « off » ignoré ou refusé, silence, absence de
  réponse, erreurs HTTP, authentification, modèle sans vision, contexte trop court, Ctrl+C) ; les journaux (écriture, masquage des secrets, traces,
  purge), la configuration, le rapport de debug ; et la **TUI** en mode « sans écran » (Textual `run_test`) : onglets, réglages, test de LM Studio,
  diagnostic, extraction complète, activité en direct, arrêt, raccourcis, pages à refaire, sortie brute, filtre des résultats, rapport. Les tests de bout en bout utilisent
  le vrai PDF (variable `PDF_LIVRE`, ou `livre.pdf` à côté du script).
- **Non testé** : un vrai LM Studio (formats d'API écrits d'après la documentation 0.4 ; `--api openai` force le repli), la qualité de lecture du
  modèle (surtout les voyelles de l'arabe : `--autotest` puis un échantillon à l'œil), et la TUI sur ton terminal Windows (testée avec Textual 8.2.8).
- Le script **diagnostique** la lenteur mais ne la corrige pas : si le calcul se fait sur le CPU, c'est dans LM Studio que ça se règle.

## Fichiers

- `extraire_prenoms_llm.py` — moteur, ligne de commande, journaux, configuration, rapport
- `prenoms_tui.py` — interface plein écran (Textual)
- `mise_en_page.py` — analyse de la page par traitement d'image (zones, genre, entrées)
- `lecture_hybride.py` — Tesseract, feuille de recadrages pour le modèle de vision, fusion
- `REPRISE.md` — document de reprise du projet (contexte, état, constats, prochaines étapes)
- `lancer.bat` — lanceur Windows (double-clic)
- `verite_terrain.py` — pages 55, 56, 100, 150 lues à l'œil, utilisées par `--autotest`
- `test_extraire_prenoms_llm.py`, `test_prenoms_tui.py` — tests, sans LM Studio ni GPU
- `requirements.txt` — Pillow, NumPy, openpyxl, PyMuPDF, Textual


## Vérification de l'arabe et relecture ciblée (v4.3)

- **Contrôle automatique** : à l'export, l'arabe de chaque prénom est comparé à la translittération de la tête de définition (`verif_arabe.py`). Un écart net pose le drapeau
  `arabe_incoherent_translit` dans `à_vérifier` (le champ ne corrige rien). Le tanwin final (يُوسُفٌ) est retiré : le livre donne la forme de dictionnaire.
- **`--relire-arabe`** : après une extraction, relit les seuls mots arabes signalés (SANS réflexion par défaut : mesuré le 28 oct. 2026, la réflexion épuisait son budget sans répondre ; `--reflexion oui` pour la forcer) (feuille réduite aux recadrages suspects + indice « lettres attendues ≈ … »).
  Une nouvelle lecture n'est adoptée que si le vérificateur la juge nettement plus cohérente ; sinon l'ancienne et son drapeau restent. Les corrections sont écrites dans le cache (`relectures`) puis le CSV/Excel/JSON est ré-exporté.
  Exemple : `python extraire_prenoms_llm.py livre.pdf --relire-arabe --essais 3 --budget-reflexion 3000`. Nécessite Tesseract (mise en page) et un modèle chargé dans LM Studio.
- **Garde-fous** (audit du 20 oct. 2026) : boucle de noms répétés détectée en quelques centaines de jetons ; entrées en double supprimées ; si le modèle rend 0 entrée (ou un nombre aberrant) alors que la mise en page en voit, la page est reprise en « lecture locale seule » (arabe vide, drapeau) au lieu d'être perdue.
- Autres options : `--modele NOM` (modèle chargé, ex. `google/gemma-4-12b-qat`), `--tui` (interface plein écran), `--version`. Une option répétée en ligne de commande affiche maintenant un avertissement (seule la dernière compte).

## Comparer et combiner plusieurs modèles automatiquement (v5.0.0) : `comparer_modeles.py`

Charge chaque modèle avec le bon contexte via la CLI `lms` de LM Studio (Settings > Developer > « Install the lms CLI »), lance les tâches, décharge, et écrit `comparaison/comparaison_modeles.md`.
**Attention : chaque chargement est précédé de `lms unload --all`** (les modèles déjà chargés sont déchargés).

```powershell
# autotest (4 pages à vérité terrain) + relecture ciblée du cache existant, pour chaque modèle, puis chaîne (modèles enchaînés)
python comparer_modeles.py lelivredesprenom0000geof.pdf --modeles "prism-ml/bonsai-27b,google/gemma-4-12b-qat,ornith-1.0-9b@8192" --cache sortie_llm --chainer
# voir les commandes sans rien exécuter
python comparer_modeles.py livre.pdf --simuler
```
Syntaxe d'un modèle : `nom[@contexte][#gpu]` (contexte par défaut 16384 ; gpu = `max`, `off` ou une fraction 0-1 pour un gros modèle partiellement déchargé sur la RAM).
Le PDF est retrouvé automatiquement s'il est dans le dossier (même pour `extraire_prenoms_llm.py`). La relecture travaille sur une COPIE du cache : l'original n'est jamais modifié.


### Résultats mesurés le 28 oct. 2026 (autotest, 4 pages à vérité terrain, hybride, RTX 5070 Ti 12 Go)
| Modèle | arabe exact | avec voyelles | vedettes | s/page |
|---|---|---|---|---|
| **google/gemma-4-12b-qat** | **22/22** | **35/35** | 35/35 | **7,9** |
| prism-ml/bonsai-27b | 10/22 | 14/35 | 35/35 | 9,1 |
| ornith-1.0-9b | 17/22 | 13/20 | 20/35 | 47,7 |
Recommandation : extraire et relire avec gemma-4-12b-qat. Échantillon de 4 pages : à confirmer sur le livre entier (`python extraire_prenoms_llm.py PDF --methode hybride --modele google/gemma-4-12b-qat --sortie sortie_gemma`).

## Revue manuelle et relecture des têtes (v4.4)

Constat sur le run gemma complet : la plupart des mots arabes encore « incohérents » sont en réalité justes ; c'est la **tête latine** lue par Tesseract qui est fausse
(« Khalil-ATâR » pour « Khalîl-Allâh », « Safivv-AÏlâh » pour « Ṣafiyy-Allâh »). Trois outils :

- `--relire-tetes` : relit au modèle de vision les têtes incohérentes avec l'arabe ; une nouvelle tête n'est adoptée que si elle devient cohérente avec l'arabe (l'arabe sert d'arbitre). Sans réflexion, une requête par page.
  Lors d'une extraction neuve, le même arbitrage est appliqué automatiquement (`fusionner_tete`).
- `--exporter-problemes [--inclure-voyelles]` : écrit `<sortie>/problemes/problemes.html` (page autonome : recadrage du mot arabe + de la tête pour chaque mot signalé, champs à remplir, bouton « Télécharger corrections.csv »),
  `problemes.csv` (modifiable dans Excel) et `images/`.
- `--appliquer-corrections corrections.csv` : écrit tes corrections dans le cache ; un mot corrigé ou coché « correct » est marqué validé (plus de drapeau `arabe_*`) ; la tête corrigée remplace celle de la définition ; ré-export.

```powershell
python extraire_prenoms_llm.py lelivredesprenom0000geof.pdf --relire-tetes --modele google/gemma-4-12b-qat --sortie sortie_llm_googlegemma-4-12b-qat
python extraire_prenoms_llm.py lelivredesprenom0000geof.pdf --exporter-problemes --sortie sortie_llm_googlegemma-4-12b-qat --inclure-voyelles
# ouvre sortie_llm_googlegemma-4-12b-qat\problemes\problemes.html, corrige, télécharge corrections.csv, puis :
python extraire_prenoms_llm.py --appliquer-corrections corrections.csv --sortie sortie_llm_googlegemma-4-12b-qat
```

## Français des définitions et prénoms « mixtes » (v4.5)

- **`--relire-corps`** : corrige le français lu par Tesseract. Les définitions qui contiennent des mots suspects (absents du dictionnaire, rares dans le corpus : « seroiteur », « lunnière », « fennne »)
  sont relues par le modèle de vision (une requête par page, paragraphes recadrés), mais **on ne lui emprunte que les mots qui remplacent un mot suspect par un mot connu et ressemblant** : il ne réécrit jamais le texte.
  Les mots que le modèle lit comme Tesseract sont considérés légitimes (translittérations en italique, mots du domaine). Nécessite `pip install pyspellchecker` et Tesseract. Sur ton cache : ≈ 300 définitions ciblées sur 1 670 (≈ 370 mots suspects, dont une partie légitime).
  `python extraire_prenoms_llm.py lelivredesprenom0000geof.pdf --relire-corps --modele google/gemma-4-12b-qat --sortie sortie_llm_googlegemma-4-12b-qat`
- **Prénoms « mixtes »** : les entrées notées « (mixte) » dans le livre sont exportées dans les deux genres (une ligne MASCULIN, une ligne FEMININ). Nouvelle colonne **`mixte`** (« oui ») pour les repérer / dédoublonner,
  et le drapeau `genre_a_verifier` n'est plus posé sur un masculin en ة quand la définition dit « (mixte) » ou « selon l'usage ».

## Entrées perdues par la mise en page : `--completer-pages` (v5.0.0)

L'analyse d'image classait parfois une ligne de nom comme « suite de définition » : (1) français et arabe NOIRS collés en un seul amas (« Abdelmoumine » p. 96, noms en « Abd… » à arabe allongé) ;
(2) arabe très haut (« Ousfour / Ousfoura » p. 320 : 97 px pour un seuil de 95, désormais 110). Ces entrées disparaissaient de l'export sans avertissement (8 contrôlées à l'œil : Balqis p. 60, Qounout p. 88, Abdelmoumine p. 96, Abdelmoughni p. 101,
Salaheddine p. 157, Khéireddine p. 224, Loutfi p. 229, Ousfour p. 320 ; ≈ 20 pages concernées, 1 708 → 1 720 entrées pour tout le livre).
`--completer-pages` compare, pour chaque page lue en hybride, le nombre d'entrées en cache à celui de la mise en page corrigée ; les pages incomplètes sont sauvegardées dans `<sortie>/cache_avant_completude/`, retirées du cache, puis relues par l'extraction normale (LM Studio requis).
Les relectures automatiques (`--relire-arabe/-tetes/-corps`) de ces pages sont à relancer ensuite ; tes corrections manuelles ne sont pas concernées (aucune page commune).
`python extraire_prenoms_llm.py lelivredesprenom0000geof.pdf --completer-pages --modele google/gemma-4-12b-qat --sortie sortie_llm_googlegemma-4-12b-qat`

## Voyelles finales (v5.0.0)
Le livre imprime les noms arabes sous leur forme de pause : « نُوح », « إِبْرَاهِيم », « خَلِيل الله » (vérifié sur le scan). Le modèle ajoutait une damma / kasra de déclinaison sur ≈ 13 % des lignes (« نُوحُ », « خَلِيلُ اللهِ »).
L'export les retire (`verif_arabe.normaliser_arabe_export`, avec le tanwin) et les consignes envoyées au modèle l'interdisent. La fatha finale est conservée (« طَهَ »). Aucune relecture nécessaire : c'est appliqué à l'export, sur le cache existant.

## Plusieurs documents : les profils (v5.0)
Le moteur est commun ; ce qui est propre à un livre vit dans `profils/<nom>.json` (pages utiles, décalage de pagination, description de la mise en page envoyée au modèle, règles de genre et d'arabe, méthode de lecture).
`python extraire_prenoms_llm.py --lister-profils` ; le profil est détecté d'après le nom du PDF, ou choisi avec `--profil NOM`. Un PDF inconnu utilise le profil `generique` (le modèle lit chaque page entière).
Le profil appliqué est enregistré dans `<sortie>/profil_utilise.json` : l'export et les relectures le retrouvent. Les options données en ligne de commande (`--debut`, `--fin`, `--pages`, `--methode`, `--sortie`) l'emportent sur le profil.
Profils fournis : geoffroy (Livre des prénoms arabes, seul à utiliser la lecture hybride), abdur_rahman, hakim, usmani, penoncourt, hareeri, haddadou (amazigh), treasury (photos), generique.
**Non testés avec un vrai modèle** : les prompts des 7 autres profils ont été écrits d'après l'aspect des pages ; essaie 3-4 pages (`--pages 40-43`) avant de lancer un document entier. Les passes de relecture (`--relire-*`, `--completer-pages`) ne valent que pour le profil Geoffroy.
Document écarté : `indexconcordance0000hard_1.pdf` (996 pages) n'est pas un dictionnaire de prénoms.

### Changer de document dans la TUI (v5.1.0)
Avant la v5.1.0, seule la ligne de commande appliquait les profils : la TUI restait sur les réglages du Livre des prénoms arabes (pages 53-338, son prompt, `sortie_llm`). Désormais la TUI passe par le même code que la ligne de commande (`cfg_avec_profil`).
Pour changer de document : **Réglages (F5)** → champ *Fichier PDF* → champ *Profil du document* (`auto` = reconnu d'après le nom du PDF, sinon choisis-le dans la liste) → **Enregistrer**. L'Accueil affiche le profil actif et les pages ; l'onglet Pages liste les pages du profil. Laisse *Pages* et *Dossier de sortie* **vides** pour utiliser ceux du profil (chaque document a son dossier `sortie_<profil>`). Ce que tu saisis dans *Pages* ou *Méthode* l'emporte sur le profil.
Garde-fous : un dossier de sortie qui contient déjà un autre document est refusé (message clair) ; `--autotest` est refusé hors du Livre de Geoffroy (sa vérité terrain n'existe que pour lui).
Nouveau document : copie `profils/generique.json` en `profils/mon_document.json`, renseigne `motifs_fichier`, `pages`, `decalage_page_imprimee`, `mise_en_page`, puis relance la TUI (la liste est relue au démarrage).

## Benchmark de la lecture de l'arabe : `benchmark_arabe.py` (v5.2.0)
Compare, sur une vérité terrain (`benchmark/verite_arabe.json`, 64 zones arabes sur 5 pages de livres différents), Tesseract (variantes), EasyOCR, PaddleOCR et les modèles de vision de LM Studio, seuls ou combinés. Deux niveaux : **recadrage** (le lecteur reçoit une zone arabe isolée : sa capacité de lecture) et **page** (il reçoit la page entière : repérage + lecture). Combinaisons : accord entre deux lecteurs, « oracle », VLM guidé par une lecture Tesseract (`lm:<modèle>:indice`).
```
python benchmark_arabe.py --lister-lmstudio
python benchmark_arabe.py --candidats grille:tess --sans-page --tessdata fast=C:\tess\fast best=C:\tess\best
python benchmark_arabe.py --candidats tess:ara:fast:psm7:x3:bin lm:google/gemma-4-12b-qat lm:google/gemma-4-12b-qat:indice lm:<id Qwen3-VL> easyocr --tessdata fast=C:\tess\fast
```
Tesseract arabe : le dossier doit contenir `ara.traineddata` (et `eng` si `ara+eng`) ; versions « fast » et « best » sur github.com/tesseract-ocr/tessdata et tessdata_best. Rapport : `benchmark/sortie/rapport_benchmark.md` + `resultats_benchmark.json` (détail par zone).
**Résultats mesurés dans le bac à sable (Tesseract 5.3.4, 64 zones, CER « léger », voyelles ignorées)** : meilleure variante `tess:ara:fast:psm7:x3:bin` = **6,1 % d'erreur, 79,7 % de zones exactes** (ordre de grandeur : sans agrandissement ×3 ni binarisation 17,8 % ; `best` psm7 sans prétraitement 38 %). Par page : colonnes arabes (Abdur Rahman, Treasury) 1,5 % ; arabe rose voyellé 3,8 % ; arabe minuscule collé à « Nom : » et arabe voyellé du livre français 13,5-13,9 % (surtout des caractères parasites des recadrages). **Page entière : Tesseract retrouve 0 % des zones.** Autrement dit : Tesseract lit correctement une zone arabe bien isolée et prétraitée, mais ne sait pas la trouver. Les modèles de vision (Gemma, Qwen…) n'ont PAS pu être mesurés dans le bac à sable : à lancer chez toi.
Limites : vérité terrain écrite par Claude d'après les images (à faire relire), captures peu définies (erreurs absolues pessimistes), 64 zones (écarts de quelques points non significatifs), voyelles non évaluées.
