# AUDIT du projet — 20 oct. 2026 (v4.2.1 → v4.3.0)

Périmètre : code (7 400 lignes, 8 modules), tests (142 moteur + 23 TUI), documentation, et surtout les **sorties réelles** de ton run (cache `sortie_llm_v4_2`, 171 pages, et le rapport debug).
Méthodes : ruff (F, E9, B, PLE, PLW, S), vulture, bandit, relecture de code ciblée, recoupement du cache avec l'analyse de mise en page (`stats_pages.json`), rejeu de `lire_page` sur les 286 pages, ré-export du cache avec le nouveau code.
Non mesuré : couverture de tests chiffrée, TUI sous Windows, LM Studio réel (le bac à sable n'a ni l'un ni l'autre), qualité de gemma-4-12b-qat.

## A. Défauts trouvés dans les données (corrigés)

| # | Gravité | Constat (preuve) | Correctif v4.3 |
|---|---|---|---|
| A1 | **Haute** | **Page 98 : boucle non détectée** — le modèle a répété 6 noms pendant 8 000 jetons (550 s) et 86 « entrées » ont été exportées (7 attendues). Le détecteur ne voyait que des blocs exacts ≤ 900 caractères. | Fenêtre 9 000 car. / période ≤ 3 000 ; nouveau `detecter_boucle_entrees` (un même prénom 3 fois = boucle) ; `dedoublonner` à la lecture ET à l'export (80 doublons retirés de ton cache sur p.98, 7 ailleurs). |
| A2 | **Haute** | **Entrées perdues en silence** : 6 pages « sans légende MASCULIN/FEMININ » (81, 111, 135, 163, 197, 221) sont revenues du repli avec 0 entrée (3-4 attendues chacune) et marquées `ok`. | Contrôle de complétude contre la mise en page (`etat.attendus`) ; modèle vide ou aberrant → **lecture locale seule** (arabe vide + drapeau) au lieu d'une perte ; écart ≥ 2 noté dans les avertissements. |
| A3 | Haute | **6 pages sur 18 en repli inutile** (page entière = 70 s au lieu de 25 s) : contrôle de « tête » trop strict face au bruit d'OCR. | `tete_plausible` tolérant (v4.2.1) ; 0 repli injustifié sur les pages du journal ; ≈ 10 % de pages signalées sur 240 pages (surtout des pages de citation, repli normal). |
| A4 | Haute | **Arabe faux** : 24 % des lectures évaluables incohérentes avec la translittération (Mukhtâr→زُبَيْدَة, ʿImâd ad-Dîn→فَيْصَل, س pour ص, د pour ذ…) ; 437 lignes sans voyelle ; 19 avec lettres latines. | `verif_arabe` + drapeau `arabe_incoherent_translit` ; `--relire-arabe` (réflexion + arbitrage par le vérificateur). |
| A5 | Moyenne | **Tanwins parasites** en fin de mot (يُوسُفٌ, أَيُّوبٌ). | Retrait à l'export + consigne du prompt. |
| A6 | Moyenne | **Faux positifs de mise en page** sur les pages de titre de section (p. 53 : transparence du verso lue comme texte) → repli → `[]` (correct, 10 s). | Aucun (coût négligeable) ; noté. |
| A7 | Moyenne | **Autotest trompeur** : `--methode hybride --methode vlm` → la dernière écrase la première sans rien dire ; ton rapport ne contenait que le run VLM. | Avertissement affiché pour toute option répétée. |
| A8 | Basse | Corps français = Tesseract seul, jamais relu par le modèle (petites fautes : « compalissant », « comine »). | **Non corrigé** (voir D). |

## B. Qualité du code

- ruff F/E9/PLE : **0 erreur** (1 f-string inutile corrigée). Aucun import mort, aucune variable non définie. vulture : 3 résidus, tous dans `verif_arabe.py` (paramètre inutilisé, condition morte), nettoyés.
- bandit : 3 « SHA-1 faible » = empreintes de cache, pas de sécurité → `usedforsecurity=False` ; `subprocess` sans shell (ouverture de dossier) : sans entrée non fiable ; 5 `try/except/pass` : tous sur des chemins de nettoyage (fermeture, affichage), acceptables.
- B904 (raise sans `from`) ×10, B905 (`zip` sans `strict`) ×4 : style, pas de bug constaté ; laissés tels quels pour ne pas risquer de régression sans nécessité.
- `extraire_prenoms_llm.py` = 2 900 lignes dans un seul fichier : lisible grâce aux sections numérotées mais à découper (client LM Studio / pipeline / exports / CLI) si le projet grossit. Aucune fonction au-delà de 110 lignes.
- Secrets : `masquer()` appliqué aux journaux et au rapport ; `cle_api` écrite « *** » dans la configuration du rapport. RAS.
- Tous les `open()` de texte précisent `encoding`. Windows : `lancer.bat` correct ; `sys.stdout.reconfigure(utf-8)` présent.
- `requirements.txt` : `opencv-python-headless` et `pytesseract` sont déclarés ; `pytesseract` n'est que l'appel à l'exécutable Tesseract (à installer à part, UB Mannheim, avec le modèle `fra`).

## C. Tests et documentation

- 142 tests moteur (+ 23 TUI) : tous verts. Ajoutés : boucle de noms, dédoublonnage, rescue « modèle vide », relecture ciblée avec faux modèle (adopte seulement une lecture plus cohérente, réflexion activée, tanwin retiré), option CLI.
- Écarts README/CLI corrigés (`--modele`, `--tui`, `--version`, `--relire-arabe`).
- **Trous connus** : pas de test sur un vrai LM Studio ; la TUI n'expose pas encore `--relire-arabe` ; le rescue « lecture locale seule » est testé avec des simulacres (pas sur le vrai PDF).

## D. Risques résiduels et recommandations (par priorité)

1. Recharger LM Studio avec contexte 12288 / parallèle 1 (fait : 64 tok/s mesurés au lieu de 17) ; relancer l'extraction **hybride** sur tout le livre (≈ 40 min estimées), puis `--relire-arabe`.
2. Mesurer gemma-4-12b-qat contre bonsai sur les 4 pages de vérité terrain, puis un vote entre les deux sur les mots signalés.
3. Corps français : passer Tesseract sur 2 modèles ou faire relire par le modèle les seuls mots hors lexique.
4. Étendre la vérité terrain (10 pages) : 4 pages ne permettent pas de trancher hybride vs modèle seul (arabe exact 13/22 contre 15/22).
5. Limite structurelle du vérificateur : il ne voit pas ش lu pour س+ح ni un و mal placé (coût 0,4 < seuil 0,5). Une 2ᵉ source (autre modèle) reste nécessaire pour ces cas.
