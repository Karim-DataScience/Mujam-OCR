# Extraction du « Livre des prénoms arabes » — v3 (modèle de vision via LM Studio)

La v2 lisait les pages avec Tesseract + EasyOCR. La v3 envoie chaque page (image du scan) à un **modèle de vision
servi par LM Studio** (ex. `prism-ml/bonsai-27b`) qui renvoie directement du JSON structuré. Elle **remplace** le
pipeline OCR, et peut aussi **le compléter** : `--ancien prenoms_arabes_corrige.csv` compare les deux extractions
page par page et signale les prénoms que l'un a vus et pas l'autre.

Sortie voulue : **une ligne par prénom**. « Youssouf / Youssef » devient deux lignes, avec le même arabe, le même
genre et la même définition.

```
prénom_fr,prénom_arab_diacritiques,genre,description_en_français,prénoms_équivalents,...
"Youssouf","يُوسُف","MASCULIN","Yûsuf (Joseph) : fils de Ya’qub. ...","Youssef",...
"Youssef","يُوسُف","MASCULIN","Yûsuf (Joseph) : fils de Ya’qub. ...","Youssouf",...
```

## Ce qui a été testé, et ce qui ne l'a pas été

- **Testé** (34 tests, `python test_extraire_prenoms_llm.py`) : tout le code autour du modèle — extraction de l'image
  d'origine du scan, nettoyage, découpe en deux, dialogue avec un **faux serveur LM Studio**, lecture de réponses avec
  réflexion `<think>` / `reasoning_content` / JSON tronqué, cache et reprise, éclatement une-ligne-par-prénom, exports.
  Les tests de bout en bout utilisent le vrai PDF (variable `PDF_LIVRE`, ou `livre.pdf` à côté du script) et sont ignorés s'il est introuvable.
- **Non testé** : la qualité de lecture du modèle réel. Je n'avais pas accès à LM Studio. Le prompt a été écrit en
  regardant des pages réelles (55, 100, 107, 150, 300 : prénoms mixtes, notes de bas de page, en-têtes variables,
  définitions sur plusieurs lignes), mais **c'est `--autotest` sur ta machine qui dira ce que ça donne**.
- **Point de vigilance n°1 : les voyelles de l'arabe.** Un modèle peut « compléter » une voyelle au lieu de la lire.
  Le prompt l'interdit, et la colonne `à_vérifier` signale l'arabe vide ou sans aucune voyelle, mais rien ne détecte
  une voyelle inventée. Contrôle à l'œil un échantillon (30 lignes tirées au hasard) avant de te fier à l'ensemble.

## Installation

```bash
pip install -r requirements.txt        # Pillow, NumPy, openpyxl, PyMuPDF (aucun Tesseract, aucun PyTorch)
```
Python 3.10+ conseillé. Fonctionne sous Windows, macOS et Linux.

### Dans LM Studio

1. Charge le modèle **avec sa partie vision** (pour les GGUF, le fichier `mmproj` doit être présent).
   Si tu as le choix entre 1-bit et ternaire, la variante ternaire est annoncée comme plus fidèle.
2. **Contexte : 32 768 tokens minimum** (une page = de l'ordre de 7 000 tokens d'image selon le modèle, ~2 000 de consigne, puis la
   réflexion et la réponse). Avec un contexte trop court, le script s'arrête avec un message explicite.
3. Onglet *Developer* → démarre le serveur (par défaut `http://localhost:1234/v1`, changeable avec `--url`).
4. Ne cherche pas l'identifiant exact du modèle : `--modele prism-ml/bonsai-27b` suffit, le script retrouve
   l'identifiant réel via `/v1/models` (correspondance approchée, et il l'affiche).

## Utilisation

```bash
python extraire_prenoms_llm.py --diagnostic                  # 1) serveur, modèle, vision : tout répond ?
python extraire_prenoms_llm.py livre.pdf --autotest          # 2) 4 pages lues à l'œil, score + durée par page
python extraire_prenoms_llm.py livre.pdf                     # 3) tout le dictionnaire (pages PDF 53 à 338)
python extraire_prenoms_llm.py livre.pdf --ancien prenoms_arabes_corrige.csv    # + comparaison avec l'ancienne extraction
```

Résultats dans `sortie_llm/` : `prenoms_arabes.csv` (UTF-8 avec BOM, valeurs entre guillemets, comme ton exemple),
`prenoms_arabes.json`, `prenoms_arabes.xlsx` (feuilles **Prénoms**, **À vérifier**, **Pages**), et `cache/`.

**Reprise automatique.** Chaque page réussie est mise en cache. Ctrl+C exporte ce qui est déjà calculé ; relancer la même
commande ne refait rien de déjà fait. Le cache est **invalidé tout seul** si tu changes le prompt, le modèle, `--decoupe`,
`--nettoyage`, `--max-cote` ou `--json-schema` (le journal l'indique). `--refaire` force le recalcul.

**Durée : à mesurer.** Elle dépend de ta machine et surtout de la longueur de la réflexion du modèle. `--autotest`
affiche le temps de chaque page ; le journal du run complet donne « reste ≈ N min » après chaque page. À titre d'ordre de
grandeur arithmétique : à 2 min par page, 286 pages font ~9,5 h. Le mieux est de lancer le run complet la nuit, ou par
tranches (`--pages 53-120`) : le cache recolle les morceaux.

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
| `translittération` | le mot avant « : » dans la définition (Yûsuf) |
| `thème`, `sous_thème` | déduits de la page grâce à la table des matières du livre |
| `page_imprimée`, `page_pdf`, `id_entrée` | traçabilité (les lignes d'une même entrée partagent `id_entrée`) |
| `à_vérifier` | drapeaux séparés par « ; » : `arabe_vide`, `arabe_non_arabe`, `arabe_sans_voyelles`, `description_vide`, `description_courte`, `description_coupee`, `sans_point_final`, `chiffres_dans_prenom`, `caracteres_suspects`, `prenom_court`, `genre_a_verifier`, `page_partielle` |

« Prénoms équivalents » pouvait s'entendre de deux façons (autres orthographes, ou équivalent dans une autre langue) :
les deux existent, dans deux colonnes. Retire celle que tu ne veux pas de la liste `COLS` (section 9 du script).

Dans le classeur Excel, la feuille **À vérifier** ne garde que les lignes à drapeau (hors `arabe_sans_voyelles`, qui est
informatif) et les mêmes lignes sont surlignées en jaune dans **Prénoms**. La feuille **Pages** donne, par page : statut,
entrées, lignes, durée, tokens, remarque.

Ordre de grandeur pour vérifier la complétude : l'ancienne extraction contient 2 601 entrées, soit ≈ 3 240 lignes une fois
éclatées. La v3 doit s'en approcher ou la dépasser. Le journal final liste les **pages en échec**, les **pages tronquées**
et les **pages suspectes** (moins de 3 entrées).

## Options utiles

| Option | Effet |
|---|---|
| `--pages 55,56,100-104` | liste explicite (remplace `--debut` / `--fin`, défaut 53 → 338) |
| `--decoupe 2` | chaque page en deux moitiés, coupées dans un blanc : plus de détail par requête (utile pour les voyelles), deux fois plus de requêtes (durée à mesurer) |
| `--paralleles 2` | requêtes simultanées, si LM Studio et la VRAM suivent (à tester : 1 → 2 → 3) |
| `--max-tokens 16000` | budget par requête, **réflexion comprise**. Une page « PARTIELLE » ou en échec avec `finish=length` : augmente-le (et le contexte) |
| `--temperature 0.6 --top-p 0.95 --top-k 20` | réglages usuels des modèles Qwen en mode réflexion (éviter 0 : boucles de répétition) |
| `--nettoyage non` | n'efface plus les « fantômes » du verso (défaut : oui) |
| `--max-cote 1568` | réduit l'image si elle est trop lourde pour ta VRAM |
| `--json-schema` | impose le format via `response_format` (à essayer si le JSON est souvent invalide) |
| `--corps-extra '{...}'` | JSON fusionné dans la requête (paramètres propres à LM Studio) |
| `--prompt-fichier p.txt` | remplace le prompt par défaut |
| `--sauver-images` | garde dans `sortie/images` les PNG réellement envoyés au modèle |
| `--essais 2`, `--timeout 1800` | tentatives par page (la 2ᵉ à température +0,2) et délai par requête |

## À propos de la réflexion du modèle

Le script suppose que le modèle **réfléchit** avant de répondre et sait lire les trois formes possibles : bloc
`<think>…</think>` dans le contenu, champ séparé `reasoning_content`, ou JSON seul. Il n'essaie pas de la désactiver par
l'API : LM Studio peut ignorer ce genre de réglage selon la version et le modèle. Si tu veux raccourcir la réflexion,
fais-le dans LM Studio (réglages du modèle) et vérifie avec `--autotest` que la qualité ne baisse pas.
Une réponse coupée par `--max-tokens` n'est jamais confondue avec un résultat : les entrées complètes déjà écrites sont
récupérées, la page est marquée « PARTIELLE » et sera retentée avec un budget plus grand.

## Dépannage

| Message | Cause probable |
|---|---|
| `Serveur LM Studio injoignable` | serveur non démarré (onglet Developer), mauvais `--url` |
| `Le modèle chargé n'accepte pas d'image` | module vision absent : télécharge/charge la variante avec vision (`mmproj`) |
| `Contexte insuffisant` | recharge le modèle avec ≥ 32 768 tokens de contexte |
| `réponse coupée avant la fin` | la réflexion consomme tout le budget : `--max-tokens 24000` (contexte ≥ 40 000) |
| `le modèle a recopié l'exemple du prompt` | il n'a pas lu l'image : vérifie la vision (`--diagnostic`) |
| page « blanche » dans la feuille Pages | normal : aucune requête n'est envoyée aux pages sans encre |

Code de sortie 2 = erreur fatale (rien n'est exporté si aucune page n'est disponible).

## Adapter à un autre livre

`CHAPTERS` et `PAGE_OFFSET` (section 1) encodent la table des matières de CE livre ; le `PROMPT` (section 2) décrit sa
mise en page (noir = masculin à gauche, rouge = féminin à droite, arabe à droite du prénom, définition dessous).

## Fichiers

- `extraire_prenoms_llm.py` — le script
- `verite_terrain.py` — pages 55, 56, 100, 150 lues à l'œil, utilisées par `--autotest`
- `test_extraire_prenoms_llm.py` — 34 tests, sans LM Studio ni GPU (~20 s avec le PDF)
- `requirements.txt`
