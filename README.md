# Extraction du « Livre des prénoms arabes » — v4.1 (LM Studio, streaming, TUI, journaux)

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

- **Testé** (`python test_extraire_prenoms_llm.py`, 112 tests, ≈ 2 min ; `python test_prenoms_tui.py`, 23 tests, ≈ 2 min) : le moteur face à un **faux serveur LM Studio**
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
- `lancer.bat` — lanceur Windows (double-clic)
- `verite_terrain.py` — pages 55, 56, 100, 150 lues à l'œil, utilisées par `--autotest`
- `test_extraire_prenoms_llm.py`, `test_prenoms_tui.py` — tests, sans LM Studio ni GPU
- `requirements.txt` — Pillow, NumPy, openpyxl, PyMuPDF, Textual
