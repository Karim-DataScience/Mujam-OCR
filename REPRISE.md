# REPRISE — Extraction du « Livre des prénoms arabes » (état au 20 oct. 2026, version 4.3.1)

> À donner tel quel dans une nouvelle discussion, avec `extraction_prenoms_llm_v4_2.zip` (tout le code), le PDF du livre et, si tu en as,
> le dernier **rapport de debug** (`python extraire_prenoms_llm.py --rapport`). Le bas du document contient un prompt prêt à coller.

## 1. Objectif et contexte

- **But** : extraire en CSV / Excel / JSON tout le dictionnaire du PDF scanné `lelivredesprenom0000geof.pdf` (« Le livre des prénoms arabes », Y. et N. Geoffroy,
  448 pages, **images sans couche de texte**, dictionnaire = pages PDF 53-338, page imprimée = page PDF − 4).
- **Format voulu** : une ligne par prénom (« Youssouf/Youssef » → deux lignes) avec `prénom_fr`, `prénom_arab_diacritiques` (arabe voyellé), `genre`
  (`MASCULIN`/`FEMININ`), `description_en_français`, + colonnes d'équivalents (autres orthographes, forme associée, équivalent étranger, translittération, thème, pages, drapeaux `à_vérifier`).
- **Machine de l'utilisateur** : Windows, Ryzen 9 9955HX, RTX 5070 Ti (12 Go de VRAM d'après le README de l'ancienne v2), 32 Go de RAM ; LM Studio avec
  `prism-ml/bonsai-27b` (Qwen3.6 27B quantifié ternaire/1-bit, vision, **réflexion activée par défaut**). Il mesurait **~8 tok/s** et des générations qui ne finissaient pas.
- **Souhaits exprimés** : un fichier de logs pour faciliter le dev/les corrections ; une interface CLI/TUI pour tout gérer ; **mélanger plusieurs méthodes**
  (LM Studio, Tesseract, éventuellement Unlimited-OCR de Baidu) en **comprenant la structure des pages** (recadrer, envoyer des zones, une tâche par spécialité) pour gagner en temps et en calcul.

## 2. Historique des versions

| Version | Contenu |
|---|---|
| v2 (fournie par l'utilisateur) | Tesseract + EasyOCR, résultats jugés imprécis |
| v3 | page entière → modèle de vision via LM Studio, JSON, cache par page |
| v4 / v4.1 | streaming, réflexion coupée (`reasoning: "off"`), budget de contexte, calibrage de l'image, abandon sur boucle/silence, reprise, **journaux** (`.log` + `.jsonl`), `--rapport`, **TUI Textual**, configuration `prenoms_config.json` |
| **v4.2** | **lecture hybride** : mise en page par traitement d'image + Tesseract + feuille de recadrages lue par le modèle de vision, repli automatique sur la page entière |

## 3. Fichiers (dans le zip)

`extraire_prenoms_llm.py` (moteur, CLI, journaux, config, rapport) · `prenoms_tui.py` (TUI) · `mise_en_page.py` (zones d'une page) ·
`lecture_hybride.py` (Tesseract, feuille, fusion) · `verite_terrain.py` (4 pages lues à l'œil pour `--autotest`) · `test_extraire_prenoms_llm.py`,
`test_prenoms_tui.py` · `README.md` · `requirements.txt` · `lancer.bat` · `analyse/stats_pages.json` (analyse de mise en page des 286 pages).

Tests : `python test_extraire_prenoms_llm.py` (129 tests, ≈ 3 min) et `python test_prenoms_tui.py` (23 tests, ≈ 2 min), sans LM Studio ni GPU (faux serveur
LM Studio : API native + OpenAI, streaming, pannes). Les tests de bout en bout utilisent le vrai PDF (`PDF_LIVRE` ou `livre.pdf` à côté) et Tesseract (ignorés s'il manque).
**Jamais testé** : un vrai LM Studio, la TUI sur Windows, la qualité de lecture du modèle sur les feuilles.

## 4. Constats établis (avec chiffres)

**Résultat de la v4 fourni par l'utilisateur** (`prenoms_arabes.csv`, 180 lignes, pages 55-77) et sortie brute `p055_b2.txt` :
- structure : orthographes empilées d'un même prénom lues comme des prénoms distincts, parfois avec le mauvais genre (`Adame` FEMININ), et **arabe vide** pour la 2ᵉ orthographe (`Haoua`, `Ralilallah`…) ;
- arabe : lettres confondues (خليم pour خليل, طوط pour لوط, أوب pour أيوب, رخمة pour رحمة, هُد pour هود, حاجر pour هاجر), intrusions cyrilliques/latines (`أдамه`, `سarah`), **32 % des lignes sans aucune voyelle** ;
- descriptions : césures non recollées (`islami-que`, `Co-ran`, 19 cas), accents faux (`Àdam`, `Idrís`, `Ħawâ'`), mot « corrigé » (`roux` → `rouge`) ;
- pages 63-71 : de vraies entrées dans une mise en page **sans légende** « MASCULIN / FEMININ » (suites de rubrique).

**Mise en page par traitement d'image (`mise_en_page.py`)** — validée sur les 286 pages : 271 pages d'entrées sans anomalie, 15 pages de texte (54, 62, 82, 92, 112, 134, 136,
164, 198, 222, 250, 280, 288, 290, 330), 0 échec ; **1 708 entrées, 2 253 blocs de prénoms** (la couverture annonce « plus de 2 200 prénoms »), 939 orthographes empilées,
3 192 formes (l'ancienne v2 : ≈ 3 240) ; ≈ 0,65 s/page. Sur les pages 55-61 et 72-75, les comptes d'entrées et de formes sont **identiques** à ceux du modèle ; le modèle avait
raté des entrées en fin de page 76-77. Principes : légende = filets de soulignement (ou ligne mixte noir/rouge) ; **genre = couleur** (noir/rouge) ; ligne de nom = latin puis arabe de même
couleur (le 1ᵉʳ amas est le français, les suivants l'arabe) ; orthographe empilée = ligne latine alignée sous un nom, aussi épaisse ; définition = le reste ; filet de notes (zone < 16 % de la
largeur, bas de page) ignoré ; sous-titres à puce rouge = « textes libres » ignorés (102 sur le livre, à vérifier au besoin).

**Tesseract 5.3** (`fra`, modèle `best` ≈ 4 Mo) sur les définitions : français courant quasi parfait ; il **perd** les signes de la translittération (Â→A, î→i, ḥ/ḍ→h/d, È→E,
« Il » lu « 11 », ê→é) ; 0,15-0,4 s par paragraphe, 2,5-8 s par page. Le modèle `fra` rapide (apt) fait plus d'erreurs (« rüuge », « roseant »). → la « tête » de chaque définition
(texte avant « : ») est relue par le modèle de vision sur un recadrage minuscule, avec la lecture de Tesseract comme indice.

**Unlimited-OCR (Baidu, juin 2026, MIT)** — non testé (pas de GPU/Hugging Face dans le bac à sable). Modèle de « document parsing » de type DeepSeek-OCR, pensé pour les documents longs ;
inférence Transformers (torch 2.10, CUDA 12.9), vLLM ou SGLang ; sortie avec balises `<|det|>type [x1,y1,x2,y2]<|/det|>`. Réserves : serveur séparé (pas dans LM Studio), **VRAM à partager** avec
le modèle de 27 B sur 12 Go (déchargement alterné), pas de preuve sur l'**arabe voyellé calligraphique**. Utilité probable : remplacer Tesseract pour le corps français (gain incertain), pas la lecture de l'arabe.
Piste : un connecteur « moteur OCR HTTP » (API compatible OpenAI) + banc d'essai, à n'activer que si l'autotest montre un gain.

**Vitesse (8 tok/s)** — hypothèse : calcul au moins en partie sur CPU (7 Go lus à ≈ 85 Go/s ≈ 8-12 tok/s). Causes à vérifier avec `--diagnostic` : « GPU Offload » incomplet, runtime llama.cpp CUDA de LM Studio
trop ancien pour le format Q2_0/ternaire, VRAM saturée (débordement silencieux), mode Silencieux. Le diagnostic mesure aussi le préremplissage (< 150 jetons/s = CPU).

## 5. Décisions de conception à connaître

- Cache par page dans `sortie_llm/cache/pNNN.json`, **empreinte** = prompt + consigne + modèle + découpe + nettoyage + max_cote + tokens_image + **méthode** (changer l'un recalcule) ; `--refaire`, `--export` (garde les caches périmés), `--statut`.
- Sortie moteur = entrées normalisées `{prenoms: [{genre, orthographes, arabe}], description, description_incomplete}` ; `assembler()` fusionne les suites de définitions d'une page à l'autre ; `exploser()` fait une ligne par orthographe.
- Hybride : `etat.hybride` décidé au démarrage (`preparer_hybride`) ; par page : `lire_page` (CPU) → si `page_texte` : aucune requête ; si `problemes` : **repli** page entière ; sinon feuille (images `A1…` arabe, `T1…` têtes, indices = lecture Tesseract/orthographes)
  → JSON `{A1: …, T1: …}` → `appliquer_feuille` (fusion de tête si les lettres de base concordent à ≥ 80 %, propagation des diacritiques de la tête dans le corps). Feuille illisible après `--essais` → repli.
- Journaux : `sortie_llm/logs/*.log|jsonl` (secrets masqués), `--rapport` → zip sans PDF ni image. TUI : tout passe par le moteur ; `Controle` = pont (arrêt, activité, progression).
- Pièges rencontrés : Textual réactive l'onglet d'un widget qui prend le focus (corrigé par `Onglets._on_tab_pane_focused`) ; `RichLog` d'un onglet jamais affiché garde ses lignes en attente ; sous Windows Ctrl+C
  ne coupe pas un verrou bloquant (attente par pas de 0,5 s) ; **le bac à sable de l'assistant peut être réinitialisé : tout est dans le zip**.

## 6. Prochaines étapes (par priorité)

1. **Sur la machine de l'utilisateur** : installer Tesseract (UB Mannheim, français) + `pip install -r requirements.txt` ; `--diagnostic livre.pdf` ; `livre.pdf --autotest --methode hybride` puis `--methode vlm` (comparer score et durée) ; envoyer le **zip `--rapport`**.
2. **Qualité de l'arabe** : regarder les feuilles réellement envoyées (`--sauver-images` garde maintenant les feuilles dans `sortie_llm/images/pNNN_feuille.png`) ; tester hauteur des recadrages (150 px), image non nettoyée (voyelles fines), indice de prénom.
3. **Vérifier l'arabe par la translittération** : la tête (« Nâḍil ») détermine presque les lettres arabes (schéma de translittération du livre, p. 46 du PDF) → squelette attendu vs lecture du modèle, drapeau en cas d'écart.
4. **Lexique global des diacritiques** : au moment de l'export, appliquer à toutes les définitions les formes validées des têtes (Adam → Âdam dans « fils d'Adam ») ; propagation actuelle limitée à une même définition.
5. **Unlimited-OCR / autre moteur** : connecteur HTTP + banc d'essai par type de zone (nom, tête, corps, arabe) sur les pages de vérité terrain ; n'adopter que s'il bat Tesseract.
6. **Étendre la vérité terrain** (10 pages avec diacritiques lus à l'œil) pour mesurer au-delà de 4 pages.
7. **TUI** : colonne « méthode » (hybride/vlm) dans Pages, bouton « Relire avec le modèle seul », réglage `--paralleles` guidé ; **README/GPU** : retour du diagnostic de l'utilisateur.
8. Vérifs ponctuelles : pages 63-71 (sans légende), les 102 « textes libres » (aucune entrée perdue ?), entrées en bas de page (`description_incomplete`), `--decoupe 2` (incompatible avec l'hybride).

## 7. Prompt à coller dans la nouvelle discussion

```
Je reprends un projet : extraction structurée (CSV/Excel/JSON) du PDF scanné « Le livre des prénoms arabes » (448 p., dictionnaire = pages 53-338).
Je joins extraction_prenoms_llm_v4_2.zip (code complet + README.md + REPRISE.md) et le PDF. Lis REPRISE.md en premier : il contient le contexte,
l'état (v4.2 : lecture hybride = mise en page par traitement d'image + Tesseract + feuille de recadrages lue par le modèle de vision dans LM Studio,
repli automatique sur la page entière), les constats chiffrés, les décisions de conception et la liste des prochaines étapes.
Je suis sous Windows (Ryzen 9 9955HX, RTX 5070 Ti 12 Go, 32 Go), LM Studio + prism-ml/bonsai-27b. Tout est en français.
Prochaine étape : <ex. « voici le rapport de debug de mon --autotest --methode hybride, analyse-le et corrige »>.
Le bac à sable peut être réinitialisé : travaille depuis le zip, relance les tests (python test_extraire_prenoms_llm.py, python test_prenoms_tui.py)
et re-livre un zip complet à la fin.
```

## 8. Journal du 20 oct. 2026 (premier vrai run de la v4.2 sur la machine de l'utilisateur) — analyse et correctifs v4.2.1

Constats (journal TUI `20261020_100209_tui`, p053-p071, bonsai-27b, Tesseract 5.5.3, sans réflexion) :
- **Le moteur hybride fonctionne** : lecture locale 1-3 s/page, feuille de recadrages = 1 requête de 100-200 jetons (≈ 25 s/page) contre ≈ 70 s pour la page entière ; **17 tok/s** (contre ~8 avant).
- **6 pages sur 18 sont retombées sur la page entière à tort** : le contrôle « la définition commence par Tête : » était trop strict face au bruit d'OCR (`Nüûh (Noé) :'de`, `Dhüûün-n-nûn :lhomnie`,
  puce rouge lue `#`, ʿayn lu `*`, `Al-Yasa®`, `Shaymâ”`). Corrigé : `tete_plausible()` + `nettoyer_debut()` dans `lecture_hybride.py` (3 tests ajoutés). Rejoué sur p55-75 : 0 repli injustifié (21 pages).
- p053 = page de titre de section (« 1. LES PROPHÈTES », avec transparence du verso) : la mise en page y voit de fausses entrées → repli → le modèle répond `[]` (correct) ; coût ≈ 10 s, sans conséquence.
- **GPU** : le diagnostic a relevé contexte chargé 120 839, « Max Concurrent Predictions » 4, lot 512, VRAM 11,8/12,2 Go (presque pleine) → à corriger dans LM Studio (contexte 12288, parallèle 1, lot 1024) ;
  préremplissage lent (≈ 140 jetons/s sur la feuille) = symptôme de VRAM saturée. Non encore appliqué au moment du run.
- Le journal s'arrête à p071 (interrompu par l'utilisateur) : aucun export complet n'a été produit.

Suite (même jour) : banc de lecture locale rejoué sur 240 des 286 pages (Tesseract 5.3) : 25 pages signalées (≈ 10 %, contre 6/18 = 33 % avant correctif) ; les restes sont surtout
des pages d'ouverture de section avec citation « “ … » (repli normal, le modèle répond `[]`) et quelques têtes à formes multiples. Affinements : `tete_plausible` accepte `, ; /` et chiffres dans la tête,
refuse un guillemet initial suivi d'une espace (citation) ; un `“`/`"` collé à une lettre = ʿayn mal lu (« “Abd » -> « ʿAbd »). Rejoué sur p56/60/61/66/67/71/73 : 0 repli. Tests : 132 (moteur) + 23 (TUI) OK.
À faire côté utilisateur : recharger le modèle dans LM Studio (contexte 12288, parallèle 1, lot 1024), relancer `--autotest --methode hybride`, puis envoyer le `--rapport`.

## 9. v4.3.0-4.3.1 — vérification de l'arabe par la translittération (20 oct. 2026)

Constat sur tes sorties : 4.2 = run **hybride** (154 p., 1 878 lignes ; orthographes empilées bien groupées, mais 402 lignes sans voyelle, 21 avec lettres latines/cyrilliques, erreurs de lettres, tanwins parasites) ;
4.2.1 = run **modèle seul** (≈ 600 jetons/page, 10 p.) : orthographes empilées mal rattachées (genre FEMININ + arabe vide), lettres confondues (Yaqoub يَقُوب, Youssef يُسُوف…).
Ajouts : `verif_arabe.py` — squelette attendu d'après la translittération de la tête (ambiguïtés admises : s=س/ص, t=ت/ط, d=د/ض, h=ه/ح, sh=ش ou س+ح, ʿ/’, article assimilé, variantes « Fâdil / Fâdila »),
alignement pondéré avec le squelette lu, drapeau **`arabe_incoherent_translit`** (seuil 0,5) avec indice (« lu … ; attendu ≈ … »), retrait du **tanwin final** (hallucination du modèle), indice de translittération
ajouté à chaque recadrage arabe de la feuille. Mesure sur les 1 116 lectures du run 4.2 évaluables : 24 % signalées ; l'échantillon relu à la main est en grande majorité de vraies erreurs
(Mukhtâr→زُبَيْدَة, ʿImâd ad-Dîn→فَيْصَل, Manşûr→مَنسُور avec س au lieu de ص, Mundhir→مُنْدِر avec د au lieu de ذ…), quelques faux positifs (translittération du livre mal lue par Tesseract : « Ghivâth », « Mazâvà »).
**Limites** : une confusion entre deux lettres admises par la table (ش lu pour س+ح) ou un و mal placé (Yúsuf→يُسُوف, coût 0,4) passe sous le seuil ; le drapeau ne corrige rien.
**Pas encore fait (prochaine étape)** : relecture ciblée — renvoyer au modèle uniquement les recadrages signalés, avec « lecture précédente incohérente, attendu ≈ … », réflexion activée ; vote entre deux modèles (bonsai + gemma-4-12b-qat) ; test de gemma sur les 4 pages de vérité terrain.
Tests : 137 (moteur) + 23 (TUI).

## 10. v4.3.0 (suite) — audit complet + relecture ciblée (20 oct. 2026)

Voir **AUDIT.md** (constats chiffrés, correctifs, risques résiduels). Ajouts : détection de boucle d'entrées (p. 98 : 8 000 jetons / 550 s / 86 entrées fantômes), dédoublonnage à la lecture et à l'export,
contrôle de complétude contre la mise en page + repli « lecture locale seule » (6 pages à 0 entrée perdues en silence), avertissement d'option répétée en CLI, et **`--relire-arabe`** : relecture AVEC réflexion
des seuls mots arabes incohérents avec la translittération ; adoption seulement si le vérificateur trouve la nouvelle lecture meilleure (gain ≥ 0,25) ; corrections écrites dans le cache (`relectures`).
Ré-export du cache 4.2 avec le nouveau code : 1 878 → 1 791 lignes (87 doublons retirés), 413 lignes « arabe_incoherent_translit ».
Reste : bouton TUI pour `--relire-arabe` ; vote bonsai / gemma-4-12b-qat ; relecture du corps français ; vérité terrain étendue ; lancer l'extraction hybride complète (≈ 40 min) puis la relecture.
Tests : 142 (moteur) + 23 (TUI).

Note de livraison : le premier zip « v4_3_0 » (sans `--relire-arabe`) et le second portaient le même nom ; la version **4.3.1** (zip `extraction_prenoms_llm_v4_3_1.zip`) contient `--relire-arabe` et `AUDIT.md`. Vérification : `python extraire_prenoms_llm.py --version` doit afficher 4.3.1.

## 11. v4.3.2 — comparaison automatique de modèles (27 oct. 2026)
`comparer_modeles.py` (charge/décharge via `lms`, autotest + relecture ciblée par modèle, chaîne, rapport md/json) ; `trouver_pdf` (le PDF du dossier est retrouvé si le nom donné n'existe pas — erreur rencontrée avec « livre.pdf ») ;
l'autotest écrit `score.json`, la relecture `relecture_stats.json`. Tests : `test_comparer_modeles.py` (5) en plus de 142 + 23. Jamais testé sur un vrai LM Studio.

## 12. v4.3.3 — premiers résultats de comparaison (28 oct. 2026)
Autotest (4 p.) : **gemma-4-12b-qat 22/22 arabe exact, 35/35 voyelles, 7,9 s/p** ; bonsai-27b 10/22, 14/35, 9,1 s/p ; ornith-1.0-9b 17/22 mais 20/35 vedettes et 47,7 s/p (structure ratée, lent).
Relecture avec réflexion : **18 requêtes sur 18 (3 modèles) ont épuisé le budget de réflexion (1 500 jetons, en anglais, vérification lettre par lettre) sans rendre de réponse -> 0 correction.** Corrigé : relecture SANS réflexion par défaut
(`--reflexion oui` pour la forcer), lectures refusées renvoyées en indice. Prochaine étape : extraction complète avec gemma-4-12b-qat (≈ 38 min estimées) puis `--relire-arabe --modele google/gemma-4-12b-qat`.

## 13. v4.3.4 — run complet gemma-4-12b-qat vs bonsai-27b (28 oct. 2026) et correction d'une erreur de ma part
Run complet hybride (286 p., 271 pages d'entrées, ≈ 40 min) : **gemma 3 165 lignes, 4,0 % d'arabe incohérent avec la translittération, 108 sans voyelle, 15 sans arabe, 2 avec lettres latines** ;
**bonsai 3 156 lignes, 25,8 % incohérent, 1 301 sans voyelle, 334 sans arabe, 21 latines**. ornith-1.0-9b arrêté p. 65 (lent, 15 % incohérent, structure ratée). -> **gemma-4-12b-qat est le lecteur à utiliser.**
Erreur corrigée : les 11 pages « lecture locale » (53, 81, 111, 135, 163, 197, 221, 249, 279, 289, 329) sont des pages de TITRE de section ; mon « rescue » y fabriquait des entrées fantômes. Désormais réponse vide valide = acceptée ; caches locaux anciens ignorés à l'export.
Vérificateur affiné (-Allâh, ʻt final de mot = ة, y lu v par l'OCR, ș/š/ż, double lam de l'article) : faux positifs retirés (gemma 7,5 % -> 4,0 %). Les incohérences restantes sont surtout des TÊTES mal lues (Gjhâin, Wabhîd, Safivv-AÏlâh) et quelques vraies erreurs (Ghivâth→غِيَاص).
Reste : relecture des ≈ 119 mots signalés avec gemma (sans réflexion) ; relecture des têtes par le modèle ; corps français ; 26 pages en repli page entière (≈ 9 %).

## 14. v4.4.0 — revue manuelle + relecture des têtes (28 oct. 2026)
Relecture gemma des 72 mots signalés (sans réflexion, 508 s) : 11 corrigés, 62 restent ; réflexion jamais utilisée dans l'extraction ni la relecture (0 jeton) : seule la comparaison l'avait essayée (budget épuisé, 0 réponse).
Cause des restes (vérifiée sur les recadrages) : surtout des TÊTES mal lues par Tesseract (Khalil-ATâR, Safivv-AÏlâh) alors que l'arabe de gemma est juste ; quelques vraies erreurs (Ghiyâth lu غِيَاص). Ajouts : arbitrage de tête par l'arabe (extraction + `--relire-tetes`),
`revue_manuelle.py` (`--exporter-problemes`, `--appliquer-corrections`), coût de l'alef suscrit abaissé (طه, الله), journal `relecture_mot` / `relecture_tete` (lectures proposées). Taux d'incohérence gemma 3,6 %.
Tests : 145 + 10 (revue) + 23 (TUI) + 5 (comparateur).

## 15. v4.4.1 — premier retour de la revue manuelle (29 oct. 2026)
`--relire-tetes` sur le cache gemma : 31 têtes corrigées sur 56 suspectes (388 s). Export des problèmes : 96 mots (66 sans voyelles, 29 incohérents, 1 non arabe). corrections.csv rendu par l'utilisateur : 96 lignes, 87 arabes corrigés, 18 translittérations corrigées, 0 identifiant inconnu.
`--appliquer-corrections` signale maintenant les corrections à revoir (faute de frappe possible : كُحَيْلَاة pour Kuḥaylâ', مجير sans voyelle, translittération « Tufayi » mal lue). Vérificateur : forme féminine d'une tête masculine (ة finale facultative), « ou », hamza porté (Lu'lu'), ة pour â final (Ṣafwâ).
Reste : 79 drapeaux `genre_a_verifier` (surtout des prénoms « mixtes » ou masculins en ة : à traiter ou à neutraliser), 26 `sans_point_final`, 11 pages avec < 3 entrées, 5 suites de définition orphelines, ~9 % de pages en repli page entière.

## 16. v4.5.0 — français des définitions, prénoms mixtes (29 oct. 2026)
Audit de la sortie finale gemma (3 165 lignes, 116 « à vérifier ») : structure et arabe OK ; **français Tesseract = point faible** (≈ 17 % des définitions avec ≥ 1 faute probable, borne haute ; 5 % des mots).
Ajouts : `verif_francais.py` (suspect = hors dictionnaire fr + rare dans le corpus + hors lexique du domaine ; fusion mot à mot conservatrice), `--relire-corps` (feuille de paragraphes D1…, `EntreeLue.def_box`),
colonne `mixte`, drapeau de genre neutralisé pour les mixtes (167 entrées mixtes = 205 des 211 « doublons »). Les définitions déjà relues par le modèle ne sont pas réessayées (mots identiques = légitimes).
Reste : 5 suites de définition orphelines (p. 63, 93, 96, 228, 320), 11 pages à < 3 entrées, 44 `genre_a_verifier` non mixtes, 2 corrections manuelles à revoir (p191-e7-b1, p153-e7-b1), qualité réelle de `--relire-corps` à mesurer sur ton LM Studio.

## 17. v4.5.1 — premier run de `--relire-corps` (1er nov. 2026)
Run : 285 définitions ciblées, 198 mots corrigés, durée 2 h 04 dont ≈ 1 h 30 PERDUES : à partir de p. 332, LM Studio est resté muet (`stall` 300 s × 3 essais ≈ 909 s par page, 6 pages : 332, 334-338) sans aucun avertissement
(le code ne journalisait pas l'abandon « silence »). Correctif : `_garde_silence` (2 silences de suite -> ErreurFatale claire, pages déjà traitées conservées) dans `--relire-arabe/-tetes/-corps`.
Qualité (liste des 198 changements relue) : très majoritairement justes (seroiteur→serviteur, lunnière→lumière, fennne→femme, bonlieur→bonheur, intriédiatement→immédiatement…) ; ratés possibles sur les mots courts (sen→son, bou→bon, sir→sûr, nine→mine) et une apostrophe typographique redressée (aujourd’hui) :
corrigés (mots < 4 lettres jamais remplacés ; apostrophes ignorées). Définitions avec ≥ 1 mot suspect : 300 (18,0 %) -> 146 (8,8 %) ; mots suspects 370 -> 176 (5,3 % -> 2,5 %) ; le reste = translittérations en italique légitimes + pages 332-338 non traitées.
À refaire : relancer `--relire-corps` (ne traite que ce qui reste, quelques minutes). Export final : 3 165 lignes, 79 « à vérifier » (42 genre_a_verifier non mixtes, 26 sans_point_final, 5 description_coupee, 4 caracteres_suspects…).

## 18. v4.5.2 — 2ᵉ passe de `--relire-corps` (1er nov. 2026)
2ᵉ passe : 131 définitions / 156 mots suspects ; seulement 5 mots corrigés (632 s ; pages 332-338 enfin traitées). Diagnostic : le prompt envoyé au modèle ne contenait PAS l'indice Tesseract (pas d'ancrage) ; les mots restants sont soit légitimes
(≈ 130 : translittérations en italique, mots rares : jujube, houri, gnose…), soit des fautes (≈ 35 : fennne, femine, lunnère, honime, oinbre, nuilieu, parfun, forrnée…) que l'ancienne fusion ne pouvait pas corriger : (1) alignement `difflib` mot pour mot
(une différence voisine fusionnait les blocs et bloquait le remplacement) ; (2) un mot proposé avec élision (l'homme, l'hyacinthe) était jugé « inconnu » ; (3) « guidance » (2 occurrences) refusé car seuil de fréquence 3.
Corrigé : alignement par fenêtre de ± 4 mots autour de la position estimée, élisions décomposées, mot du modèle accepté s'il apparaît ≥ 2 fois dans le corpus. À relancer : `--relire-corps` (≈ 10 min). Les ≈ 130 mots légitimes resteront « suspects » (aucun effet).

## 19. v4.5.3 — entrées perdues par la mise en page (1er nov. 2026)
3ᵉ passe `--relire-corps` (v4.5.2 confirmée) : 17 mots corrigés sur 151 suspects (540 s) ; le français a convergé (reste ≈ 134 mots, surtout légitimes).
Investigation des « suites orphelines » : p. 63 et 93 = vrais paragraphes de texte (à ignorer) ; **p. 96 (Abdelmoumine), p. 320 (Ousfour/Ousfoura) = vraies entrées perdues**, et ≈ 18 autres pages perdaient 1 entrée sans signalement.
Causes : `HAUT_MAX_NOM` 95 < arabe de 97 px ; ligne de nom noire en un seul amas (trait épais ≥ 1,6, largeur 560-900 px). Corrections dans `mise_en_page.py` ; total du livre 1 708 → 1 720 entrées.
À faire : `--completer-pages` (relit les ≈ 20 pages concernées), puis relancer `--relire-tetes`, `--relire-arabe`, `--relire-corps` (7 pages avaient déjà des relectures auto : 60, 96, 105, 157, 183, 229, 293), puis `--exporter-problemes` pour les nouvelles entrées.

## 20. v4.5.4 — `--completer-pages` appliqué, voyelles finales (2 nov. 2026)
`--completer-pages` : 19 pages refaites en 5 min, 3 165 -> 3 201 lignes (1 665 -> 1 684 entrées), les 8 entrées contrôlées sont récupérées avec un arabe juste ; seule 1 des 264 lignes de ces pages porte un drapeau (genre) ; 75 « à vérifier » au total.
Constat sur le scan (p. 56, 59, 101) : le livre n'imprime AUCUNE voyelle de déclinaison finale (نُوح, إِبْرَاهِيم, خَلِيل الله) ; gemma en ajoutait 219 dammas + 111 kasras sur le dernier mot et 191 dammas dans les annexions (≈ 430 lignes, 13 %) ;
les 96 mots saisis à la main n'ont aucune marque finale (sauf طَهَ : fatha, conservée). Correctif : `normaliser_arabe_export` à l'export + consigne dans les prompts. Reste : relancer `--relire-tetes/-arabe/-corps` sur les 19 pages refaites, `--exporter-problemes`.

## 21. v5.0.0 — profils de documents (3 nov. 2026) — ÉTAT DE FIN DE DISCUSSION
Livré dans ce zip : `profils.py`, `profils/*.json` (9 profils), options `--profil` / `--lister-profils`, détection du PDF par son nom, `profil_utilise.json` écrit dans le dossier de sortie, repli sur le seul modèle chargé quand `--modele` n'est pas précisé,
`THEMES_ACTIFS` (thèmes du Livre des prénoms arabes désactivés pour les autres documents), tests `test_profils.py`. Un reset du bac à sable a effacé une première version de ce travail : refaite à partir du zip 4.5.4.
**Jamais testé avec un vrai LM Studio** : les 7 profils autres que Geoffroy (prompts écrits d'après des captures de pages : Abdur Rahman tableau 3 colonnes ; Hakim 4 colonnes ; Usmani liste « Nom : arabe / sens » avec Male/Female Names ; Pénoncourt « Nom (arabe) : définition » par sections Masculin/Féminin ;
Hareeri noms d'Allah puis prénoms ; Haddadou entrées numérotées « 0297- NOM, To, fém. » sans arabe ; Treasury = PHOTOS de livre ouvert, deux pages par image).
Plages de pages (PDF) : Abdur 7-147 (décalage -2) ; Hakim 19-161 (+2) ; Usmani 1-fin (-1, non vérifié) ; Pénoncourt 17-241 (+2) ; Hareeri 17-237 (0) ; Haddadou 21-178 (+1, index 179-193 exclu) ; Treasury 1-fin (0).
Le livre « Le livre des prénoms arabes » est TERMINÉ pour l'essentiel (3 201 lignes, 1 684 entrées, 75 lignes « à vérifier » ; relecture du français convergée ; 19 pages complétées ; voyelles finales de déclinaison retirées à l'export) ; restent mineurs :
2 corrections manuelles à revoir (p191-e7-b1 كُحَيْلَاء ?, p153-e7-b1 مُجِير ?), `--relire-corps` à relancer avec `--modele google/gemma-4-12b-qat` sur les 19 pages refaites (124 définitions suspectes dont la plupart légitimes), 42 genre_a_verifier non mixtes, 11 pages courtes non examinées.

## 21 bis. v5.1.0 — la TUI applique les profils (4 nov. 2026)
- **Bug** : `choisir_profil` / `appliquer_profil` n'étaient appelés que par `main()` ; la TUI appelait `construire_cfg` directement -> toujours pages 53-338, prompt de Geoffroy, `sortie_llm`, quel que soit le PDF.
- **Correctif** : `cfg_avec_profil(a, args)` (moteur) = chemin unique CLI + TUI ; `memoriser_profil` ; `reinitialiser_profil` (changer de profil sans relancer ne garde plus le décalage / l'exemple du précédent) ; champ « Profil du document » dans Réglages (clé `profil` de la configuration) ; l'Accueil affiche le profil.
- **Garde-fous** : `controler_dossier_profil` refuse de mélanger deux documents dans un dossier ; `--autotest` refusé hors profil Geoffroy.
- **Testé** : `test_profil_choix.py` (14 tests : PDF synthétique + TUI headless), `--statut` et TUI headless sur les 7 PDF réels (profil, plages, nb de pages). 53 tests rapides OK ; moteur 148 OK dont 55 ignorés et TUI 23 ignorés **faute du PDF de Geoffroy dans ce bac à sable** (non rejoués ici : à relancer chez toi).
- **Toujours non testé** : les 7 profils avec un vrai modèle.

## 22. Prompt à coller dans la nouvelle discussion
```
Je reprends un projet d'extraction de dictionnaires de prénoms (PDF -> CSV/Excel/JSON) avec LM Studio. Je joins extraction_prenoms_llm_v5_0_0.zip (code complet, README.md, REPRISE.md, AUDIT.md, profils/) et, si utile, les PDF et le dossier de sortie du livre déjà traité.
Lis REPRISE.md en premier (surtout les §13 à §21) : contexte, constats chiffrés, décisions et état exact.
Je suis sous Windows (Ryzen 9 9955HX, RTX 5070 Ti 12 Go, 32 Go), LM Studio avec google/gemma-4-12b-qat (retenu après comparaison : bien meilleur que bonsai-27b et ornith-1.0-9b pour l'arabe), Tesseract 5.5 installé. Tout est en français.
Situation : « Le livre des prénoms arabes » est terminé pour l'essentiel (voir §21). Le script est maintenant piloté par des profils de documents (profils/*.json) ; les 7 autres profils (Abdur Rahman, Hakim, Usmani, Pénoncourt, Hareeri, Haddadou/amazigh, Treasury) n'ont JAMAIS été testés avec un vrai modèle.
Prochaine étape : <ex. « voici le résultat de --profil usmani_islamic_names --pages 40-43 : analyse-le et corrige le profil » ou « finis les points mineurs du livre »>.
Règles de travail : le bac à sable peut être réinitialisé -> travaille depuis le zip, relance les tests (python test_extraire_prenoms_llm.py, test_prenoms_tui.py, python -m unittest test_profils test_revue_manuelle test_comparer_modeles test_francais) et re-livre un zip complet à la fin sous un NOUVEAU nom de version.
Sois honnête sur ce qui n'est pas testé ; vérifie sur l'image avant de conclure à une perte de données (une erreur d'audit passée venait de là).
```
