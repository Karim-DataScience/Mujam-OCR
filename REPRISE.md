# REPRISE — Extraction du « Livre des prénoms arabes » (état au 20 oct. 2026, version 4.3.0)

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

## 9. v4.3.0 — vérification de l'arabe par la translittération (20 oct. 2026)

Constat sur tes sorties : 4.2 = run **hybride** (154 p., 1 878 lignes ; orthographes empilées bien groupées, mais 402 lignes sans voyelle, 21 avec lettres latines/cyrilliques, erreurs de lettres, tanwins parasites) ;
4.2.1 = run **modèle seul** (≈ 600 jetons/page, 10 p.) : orthographes empilées mal rattachées (genre FEMININ + arabe vide), lettres confondues (Yaqoub يَقُوب, Youssef يُسُوف…).
Ajouts : `verif_arabe.py` — squelette attendu d'après la translittération de la tête (ambiguïtés admises : s=س/ص, t=ت/ط, d=د/ض, h=ه/ح, sh=ش ou س+ح, ʿ/’, article assimilé, variantes « Fâdil / Fâdila »),
alignement pondéré avec le squelette lu, drapeau **`arabe_incoherent_translit`** (seuil 0,5) avec indice (« lu … ; attendu ≈ … »), retrait du **tanwin final** (hallucination du modèle), indice de translittération
ajouté à chaque recadrage arabe de la feuille. Mesure sur les 1 116 lectures du run 4.2 évaluables : 24 % signalées ; l'échantillon relu à la main est en grande majorité de vraies erreurs
(Mukhtâr→زُبَيْدَة, ʿImâd ad-Dîn→فَيْصَل, Manşûr→مَنسُور avec س au lieu de ص, Mundhir→مُنْدِر avec د au lieu de ذ…), quelques faux positifs (translittération du livre mal lue par Tesseract : « Ghivâth », « Mazâvà »).
**Limites** : une confusion entre deux lettres admises par la table (ش lu pour س+ح) ou un و mal placé (Yúsuf→يُسُوف, coût 0,4) passe sous le seuil ; le drapeau ne corrige rien.
**Pas encore fait (prochaine étape)** : relecture ciblée — renvoyer au modèle uniquement les recadrages signalés, avec « lecture précédente incohérente, attendu ≈ … », réflexion activée ; vote entre deux modèles (bonsai + gemma-4-12b-qat) ; test de gemma sur les 4 pages de vérité terrain.
Tests : 137 (moteur) + 23 (TUI).
