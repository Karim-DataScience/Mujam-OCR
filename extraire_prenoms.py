#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extraire_prenoms.py
====================
Extrait de façon structurée le contenu du PDF scanné "Le Livre des prénoms
arabes" (Y. & N. Geoffroy) vers CSV / JSON / Excel.

Le PDF n'a pas de couche de texte (c'est un scan) : le script rasterise
chaque page en image, fait de l'OCR (français + arabe) avec Tesseract, puis
reconstruit chaque entrée du dictionnaire (prénom, genre, arabe,
translittération, signification, thème/sous-thème, page) à partir de la mise
en page : les vedettes masculines sont imprimées en noir, les féminines en
rouge, et chaque page appartient à un chapitre/sous-thème thématique dont les
bornes sont listées dans la table des matières du livre (voir STRUCTURE
ci-dessous).

INSTALLATION (voir aussi README.md)
------------------------------------
1) Outils système :
   - Tesseract OCR + les paquets de langue français et arabe
   - Poppler (fournit la commande `pdftoppm`, pour transformer les pages
     PDF en images)

   macOS (Homebrew) :
       brew install tesseract tesseract-lang poppler

   Ubuntu / Debian :
       sudo apt-get install tesseract-ocr tesseract-ocr-fra tesseract-ocr-ara poppler-utils

   Windows :
       - Installer Tesseract (https://github.com/UB-Mannheim/tesseract/wiki),
         en cochant les langues French et Arabic pendant l'installation.
       - Installer Poppler pour Windows (https://github.com/oschwartz10612/poppler-windows),
         puis ajouter son dossier "bin" au PATH.

2) Paquets Python :
       pip install pytesseract pillow numpy pandas openpyxl easyocr

   (easyocr installe PyTorch, ~2 Go ; ses modèles arabe/français, ~300 Mo, sont
   téléchargés automatiquement au premier lancement, puis tout fonctionne hors ligne.
   Sans easyocr le script marche quand même, avec Tesseract seul.)

UTILISATION
-----------
    python3 extraire_prenoms.py chemin/vers/le_livre.pdf

Options utiles :
    --debut 53 --fin 338     limiter la plage de pages PDF à traiter
    --dossier-sortie sortie  dossier où écrire les fichiers résultat
    --dpi 300                résolution de rasterisation (300 = qualité par
                              défaut utilisée pour produire les fichiers
                              livrés ; 200 est ~40% plus rapide mais un peu
                              moins fiable sur les diacritiques)
    --reprendre               reprend un traitement interrompu (utilise le
                              fichier checkpoint.pkl du dossier de sortie)
    --sans-easyocr            Tesseract seul (le plus rapide, le moins précis)
    --sans-easyocr-latin      garde la passe EasyOCR arabe, saute la française
    --gpu                     passes EasyOCR sur GPU (CUDA), beaucoup plus rapides

Le script sauvegarde sa progression après chaque page dans
<dossier_sortie>/checkpoint.pkl : s'il est interrompu (Ctrl+C, coupure de
courant...), relancez la même commande avec --reprendre pour continuer là où
il s'était arrêté, sans tout recommencer.

PIPELINE À 3 PASSES (chaque outil sur ce qu'il fait le mieux)
  1. Tesseract (fra+ara) : structure de la page, positions, couleurs (genre),
     texte français.
  2. EasyOCR arabe : relit chaque zone en script arabe repérée par Tesseract.
  3. EasyOCR français : relit uniquement les mots latins que Tesseract juge peu
     fiables (confiance < 80), avec des garde-fous stricts avant tout remplacement.

Le traitement complet des ~286 pages prend de l'ordre de 1h30 à 2h30 sur CPU avec
les 3 passes (environ 25 min de moins avec --sans-easyocr-latin, ~1h avec
--sans-easyocr). Bien plus rapide avec --gpu.
"""

import argparse
import difflib
import json
import os
import pickle
import re
import subprocess
import sys
import time

import numpy as np
import pandas as pd
import pytesseract
from PIL import Image


# ============================================================================
# 1) STRUCTURE DU LIVRE
#    Table de correspondance "page imprimée -> chapitre / sous-thème",
#    recopiée à la main depuis la "Table des matières" imprimée à la fin de
#    l'ouvrage (pages imprimées 436-438). Le décalage entre la pagination du
#    PDF et la pagination imprimée est constant sur tout le livre.
# ============================================================================

PAGE_OFFSET = 4  # page PDF = page imprimée + 4 (vérifié du début à la fin)

CHAPTERS = [
    (1, "Les prophètes", 49, 76, [
        ("A", "Les prophètes, d'Adam à Jésus", 51, 57),
        ("B", "Le Prophète Mouhammad", 58, 67),
        ("C", "La famille du Prophète", 68, 76),
    ]),
    (2, "La vie spirituelle", 77, 106, [
        ("A", "Foi, piété et aspiration", 79, 87),
        ("B", "Servitude et adoration", 88, 101),
        ("C", "Saints personnages célèbres", 102, 104),
        ("D", "Lieux et moments rituels", 105, 106),
    ]),
    (3, "Vérité et sagesse", 107, 130, [
        ("A", "Preuve et évidence", 109, 111),
        ("B", "Équité, stabilité et rectitude", 112, 118),
        ("C", "Raison, sagesse et connaissance", 119, 130),
    ]),
    (4, "Puissance et protection", 131, 158, [
        ("A", "Autorité, courage et victoire", 133, 147),
        ("B", "Protection, assistance et sécurité", 148, 158),
    ]),
    (5, "Lumière et beauté", 159, 192, [
        ("A", "Lumière et resplendissement", 161, 171),
        ("B", "Astres", 172, 176),
        ("C", "Beauté", 177, 192),
    ]),
    (6, "Excellence et louange", 193, 216, [
        ("A", "Perfection et élection", 195, 203),
        ("B", "Noblesse et élévation", 204, 213),
        ("C", "Louange", 214, 216),
    ]),
    (7, "Vertus", 217, 244, [
        ("A", "Bonté, bienveillance et clémence", 219, 226),
        ("B", "Pureté", 227, 230),
        ("C", "Patience, contentement et fidélité", 231, 234),
        ("D", "Générosité et reconnaissance", 235, 244),
    ]),
    (8, "Le bonheur", 245, 274, [
        ("A", "Bien-être, félicité et prospérité", 247, 264),
        ("B", "Amour, amitié et harmonie", 265, 274),
    ]),
    (9, "Secrets de l'âme", 275, 284, [
        ("A", "Désir et espoir", 277, 279),
        ("B", "Inspiration, confidence et quiétude", 280, 284),
    ]),
    (10, "La nature", 285, 324, [
        ("A", "Végétation, fleurs et parfums", 287, 298),
        ("B", "Pierres précieuses", 299, 303),
        ("C", "Éléments naturels", 304, 311),
        ("D", "Animaux", 312, 324),
    ]),
    (11, "Cette vie et l'autre", 325, 334, [
        ("A", "Vitalité et longévité", 327, 330),
        ("B", "Vie éternelle et paradis", 331, 334),
    ]),
]

DICTIONARY_PDF_START = CHAPTERS[0][2] + PAGE_OFFSET   # 53
DICTIONARY_PDF_END = CHAPTERS[-1][3] + PAGE_OFFSET     # 338


def pdf_to_printed(pdf_page):
    return pdf_page - PAGE_OFFSET


def lookup_theme(pdf_page):
    """Renvoie (chapitre, sous-thème) pour une page PDF donnée."""
    printed = pdf_to_printed(pdf_page)
    for num, title, start, end, subs in CHAPTERS:
        if start <= printed <= end:
            chap_label = f"{num}. {title}"
            for letter, subtitle, sstart, send in subs:
                if sstart <= printed <= send:
                    return chap_label, f"{letter}. {subtitle}"
            return chap_label, ""
    return "", ""


def build_known_titles():
    """Titres de chapitres/sous-thèmes normalisés, utilisés pour repérer et
    éliminer les fragments d'en-tête qui se glissent parfois dans l'OCR."""
    titles = {'masculin', 'feminin', 'féminin'}
    for _, title, _, _, subs in CHAPTERS:
        titles.add(re.sub(r'[^a-zà-ÿ ]', '', title.lower()))
        for _, subtitle, _, _ in subs:
            titles.add(re.sub(r'[^a-zà-ÿ ]', '', subtitle.lower()))
    return titles


# ============================================================================
# 2) OCR PAGE PAR PAGE : lignes, mots, couleurs
# ============================================================================

ARABIC_RE = re.compile(r'[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF\u200e\u200f]+')


def strip_arabic(text):
    """Sépare un texte OCR en (partie_latine, partie_arabe)."""
    arabic_parts = ARABIC_RE.findall(text)
    latin = re.sub(r'\s+', ' ', ARABIC_RE.sub('', text)).strip()
    arabic = ' '.join(a.strip() for a in arabic_parts if a.strip())
    return latin, arabic


def classify_color(rgb):
    """Le livre imprime les vedettes féminines en rouge et les masculines en
    noir : on classe une couleur RGB moyenne comme 'red' ou 'dark'."""
    r, g, b = rgb
    return 'red' if (r - (g + b) / 2) > 22 else 'dark'


# ----------------------------------------------------------------------------
# 2bis) SECOND OUTIL, SPÉCIALISÉ ARABE : EasyOCR (deep learning, gratuit,
#       open source, 100% local — https://github.com/JaidedAI/EasyOCR)
#
# Tesseract reste utilisé pour tout le reste (structure de la page, texte
# latin/français, couleurs) : il s'en sort très bien sur le français. Mais
# sur l'arabe de ce livre, EasyOCR lit sensiblement mieux les consonnes de
# base (ex. Idris: Tesseract lit "سحي" illisible, EasyOCR lit "إدريس"
# correctement). On fait donc un DEUXIÈME PASSAGE ciblé : chaque zone où
# Tesseract a repéré du script arabe est recadrée dans l'image d'origine et
# relue par EasyOCR ; son résultat remplace celui de Tesseract s'il est non
# vide, sinon on garde le résultat Tesseract (repli).
#
# Coût : environ +5-6 secondes par page (donc le traitement complet prend
# environ le double de temps). Si easyocr n'est pas installé, le script
# continue avec Tesseract seul pour l'arabe (avertissement affiché une fois).
# ----------------------------------------------------------------------------

EASYOCR_USE_GPU = False  # mis à True par l'option --gpu (nécessite PyTorch + CUDA)
_EASYOCR_READERS = {}


def get_easyocr_reader(lang='ar'):
    """Charge (une seule fois par langue) un lecteur EasyOCR : 'ar' (arabe) ou
    'fr' (français/latin). Renvoie None si easyocr est absent ou échoue : le
    script continue alors avec Tesseract seul pour cette passe (repli)."""
    if lang in _EASYOCR_READERS:
        return _EASYOCR_READERS[lang] or None
    try:
        import io, contextlib
        import easyocr
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            _EASYOCR_READERS[lang] = easyocr.Reader([lang], gpu=EASYOCR_USE_GPU, verbose=False)
    except Exception as e:
        print(f"(EasyOCR '{lang}' indisponible ({e}) -> Tesseract seul pour cette passe. "
              f"Pour l'activer : pip install easyocr)")
        _EASYOCR_READERS[lang] = False
        return None
    return _EASYOCR_READERS[lang]


def _rebuild_line_text(ln):
    ln['text'] = ' '.join(w['text'] for w in ln['words'])


def refine_arabic_words(img, lines, reader, min_len=2):
    """PASSE 2 (outil n°2, spécialisé arabe) : relit avec EasyOCR chaque mot où
    Tesseract a détecté du script arabe (recadrage + marge dans l'image
    d'origine). Le résultat EasyOCR remplace celui de Tesseract s'il est non
    vide ; sinon on garde Tesseract. Modifie `lines` sur place."""
    if reader is None:
        return lines
    import io, contextlib
    arr = np.array(img)
    H, W = arr.shape[0], arr.shape[1]
    pad = 12
    for ln in lines:
        changed = False
        for w in ln['words']:
            _, arabic_only = strip_arabic(w['text'])
            if not arabic_only or len(arabic_only) < min_len:
                continue
            l = max(0, w['left'] - pad); t = max(0, w['top'] - pad)
            r = min(W, w['right'] + pad); b = min(H, w['bottom'] + pad)
            if r <= l or b <= t:
                continue
            crop = arr[t:b, l:r]
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    result = reader.readtext(crop, detail=0, paragraph=False)
                easy_text = ' '.join(result).strip()
            except Exception:
                easy_text = ''
            if easy_text and ARABIC_RE.search(easy_text):
                latin_part, _ = strip_arabic(w['text'])
                w['text'] = (latin_part + ' ' + easy_text).strip()
                changed = True
        if changed:
            _rebuild_line_text(ln)
    return lines


REAL_ARABIC_RE = re.compile(r'[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]')
ALLOWED_LATIN_RE = re.compile(r"^[A-Za-zÀ-ÿŒœ0-9 '’\-.,;:!?()«»\"°*]+$")


def _tidy_easyocr_latin(text):
    text = text.strip()
    text = re.sub(r'\s+([,.;:!?)])', r'\1', text)  # "hanîf) ," -> "hanîf),"
    text = re.sub(r'\s+-$', '-', text)              # "Khalîl -" -> "Khalîl-"
    return text


def refine_latin_words(img, lines, reader, conf_below=80, accept_conf=0.80):
    """PASSE 3 (outil n°3, modèle français/latin d'EasyOCR) : ne relit QUE les
    mots latins que Tesseract juge peu fiables (confiance < conf_below). Le
    remplacement est volontairement prudent : on n'accepte la lecture EasyOCR
    que si (a) sa confiance est >= accept_conf, (b) elle ne contient que des
    caractères plausibles (pas de "[", "{", "|"...), (c) sa longueur reste
    dans un rapport raisonnable avec celle de Tesseract, et (d) elle ne perd
    pas une apostrophe présente chez Tesseract (EasyOCR a tendance à les
    oublier). Corrige typiquement les mots en italique lus comme des suites de
    chiffres ("11116916," -> "intègre,") et des diacritiques mal lus."""
    if reader is None:
        return lines
    import io, contextlib
    arr = np.array(img)
    H, W = arr.shape[0], arr.shape[1]
    pad = 8
    cands, boxes = [], []
    for ln in lines:
        for w in ln['words']:
            clean = w['text'].replace('\u200e', '').replace('\u200f', '').strip()
            if REAL_ARABIC_RE.search(clean):
                continue
            if w.get('conf', 100) >= conf_below:
                continue
            if len(re.sub(r'[^A-Za-zÀ-ÿ0-9]', '', clean)) < 2:
                continue
            box = [max(0, w['left'] - pad), min(W, w['right'] + pad),
                   max(0, w['top'] - pad), min(H, w['bottom'] + pad)]
            if box[1] <= box[0] or box[3] <= box[2]:
                continue
            cands.append((ln, w, clean))
            boxes.append(box)
    if not cands:
        return lines
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            results = reader.recognize(arr, horizontal_list=boxes, free_list=[], detail=1)
    except Exception:
        return lines
    if len(results) != len(cands):
        return lines
    changed_lines = set()
    for (ln, w, tess_txt), res in zip(cands, results):
        easy_txt = _tidy_easyocr_latin(res[1])
        easy_conf = float(res[2])
        if easy_conf < accept_conf or not easy_txt or easy_txt == tess_txt:
            continue
        if not ALLOWED_LATIN_RE.match(easy_txt):
            continue
        ratio = len(easy_txt) / max(1, len(tess_txt))
        if ratio < 0.5 or ratio > 2.0:
            continue
        if re.search(r"['’]", tess_txt) and not re.search(r"['’]", easy_txt):
            continue
        w['text'] = easy_txt
        changed_lines.add(id(ln))
    for ln in lines:
        if id(ln) in changed_lines:
            _rebuild_line_text(ln)
    return lines


def get_lines_with_color(img_path, lang='fra+ara', min_conf=20):
    """OCR une image de page et regroupe les mots en lignes, avec pour
    chaque ligne et chaque mot sa position et sa couleur moyenne (échantillonnée
    directement sur l'image, pixels quasi blancs ignorés)."""
    img = Image.open(img_path).convert('RGB')
    data = pytesseract.image_to_data(img, lang=lang, config='--psm 6',
                                      output_type=pytesseract.Output.DICT)
    lines = {}
    for i in range(len(data['text'])):
        text = data['text'][i].strip()
        if not text:
            continue
        try:
            conf = float(data['conf'][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < min_conf:  # élimine le bruit OCR de très faible confiance
            continue
        key = (data['block_num'][i], data['par_num'][i], data['line_num'][i])
        lines.setdefault(key, {'words': [], 'l': [], 't': [], 'r': [], 'b': [], 'c': []})
        x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
        lines[key]['words'].append(text)
        lines[key]['c'].append(conf)
        lines[key]['l'].append(x); lines[key]['t'].append(y)
        lines[key]['r'].append(x + w); lines[key]['b'].append(y + h)

    arr = np.array(img)

    def sample(l, t, r, b):
        crop = arr[t:b, l:r].reshape(-1, 3)
        fg = crop[~np.all(crop > 190, axis=1)]
        return tuple(np.median(fg, axis=0).astype(int)) if len(fg) else (0, 0, 0)

    out = []
    for v in lines.values():
        left, top, right, bottom = min(v['l']), min(v['t']), max(v['r']), max(v['b'])
        words = [{'text': wt, 'left': wl, 'top': wtp, 'right': wr, 'bottom': wb,
                  'color': sample(wl, wtp, wr, wb), 'conf': wc}
                 for wt, wl, wtp, wr, wb, wc in zip(v['words'], v['l'], v['t'], v['r'], v['b'], v['c'])]
        out.append({'top': top, 'left': left, 'right': right, 'bottom': bottom,
                     'text': ' '.join(v['words']), 'color': sample(left, top, right, bottom),
                     'words': words})
    out.sort(key=lambda l: (l['top'], l['left']))
    return out


# ============================================================================
# 3) RECONNAISSANCE DES VEDETTES (prénoms) ET DES DÉFINITIONS
# ============================================================================

STOPWORDS = set("""à de des du le la les un une et ou pour que qui dans avec sur par ce cette
son sa ses en au aux d l qu s c n y ne pas plus est sont fut sera était été il elle ils elles
donne donné entre chez selon comme tout tous toute toutes leur leurs ainsi mais donc or ni car
ibn abou abu ben fille fils""".split())

COMMON_NON_NAMES = set(w.lower() for w in """
Dieu Coran Allah Islam Paradis Seigneur Prophète Prophete Signifie Sourate
Nom Noms Selon Ainsi Ce Cette Ces Il Elle Ils Elles Un Une Des Les La Le
Cf Voir Coranique Coraniques Musulmans Musulman Islamique Islamiques
""".split())

NAME_WORD_RE = re.compile(r"[A-ZÀ-Ü][a-zà-ÿ'’\-]{1,25}")

HEADER_SKIP_PATTERNS = [
    re.compile(r'^\s*MASC', re.I),
    re.compile(r'^\s*F[EÉ]MIN', re.I),
    re.compile(r'^\s*\d{0,4}\s*LE LIVRE DES PR', re.I),
    re.compile(r'^\s*\d{1,4}\s*$'),
    re.compile(r'^\s*[A-D]\.\s+[A-ZÀ-Ü]'),
    re.compile(r'^\s*\d{1,2}\.\s+[A-ZÀ-Ü]{3,}'),
]


def is_header_skip(latin_text):
    return any(p.match(latin_text) for p in HEADER_SKIP_PATTERNS)


def extract_headword_clusters(ln):
    """Cherche, sur une ligne OCR, un ou plusieurs mots-vedettes (prénoms) en
    ignorant le bruit OCR autour d'eux (souvent des restes de diacritiques
    arabes mal reconnus). Regroupe les mots adjacents de même couleur (un
    "cluster" = les variantes orthographiques d'une même entrée) ; deux
    couleurs différentes sur la même ligne (ex. un prénom masculin et son
    équivalent féminin imprimés côte à côte) donnent deux clusters distincts.
    Le script arabe présent sur la ligne est rattaché au cluster le plus
    proche horizontalement. Renvoie [] si la ligne ne ressemble pas à une
    ligne de vedette (ex. c'est une phrase de définition)."""
    text = ' '.join(w['text'] for w in ln['words'])
    if ':' in text:
        return []

    candidates, arabic_words = [], []
    for w in ln['words']:
        m = NAME_WORD_RE.match(w['text'].strip("*|'\""))
        if m and m.group(0).lower().strip("'’-") not in STOPWORDS \
                and m.group(0).lower() not in COMMON_NON_NAMES:
            candidates.append((w, m.group(0)))
        else:
            _, arabic_only = strip_arabic(w['text'])
            if arabic_only:
                arabic_words.append((w, arabic_only))

    if not candidates or len(candidates) > 4:
        return []

    letters_words = sum(len(re.sub(r'[^A-Za-zÀ-ÿ]', '', s)) for _, s in candidates)
    letters_line = len(re.sub(r'[^A-Za-zÀ-ÿ]', '', text))
    if letters_line == 0 or letters_words / letters_line < 0.5:
        return []  # la ligne est surtout du bruit -> ce n'est pas une vedette

    clusters = []
    for w, s in candidates:
        gender = 'Féminin' if classify_color(w['color']) == 'red' else 'Masculin'
        if clusters and clusters[-1]['gender'] == gender:
            clusters[-1]['words'].append(s)
            clusters[-1]['centers'].append((w['left'] + w['right']) / 2)
        else:
            clusters.append({'words': [s], 'gender': gender, 'arabic': '',
                              'centers': [(w['left'] + w['right']) / 2]})

    for aw, atext in arabic_words:
        ac = (aw['left'] + aw['right']) / 2
        best = min(clusters, key=lambda c: min(abs(ac - x) for x in c['centers']))
        best['arabic'] = (best['arabic'] + ' ' + atext).strip()

    for c in clusters:
        del c['centers']
    return clusters


def parse_page_lines(lines, page_num, chapter, subtheme, state):
    """Fait avancer la machine à états sur les lignes d'UNE page. `state` doit
    contenir 'entries' (liste cumulative de résultats), 'pending' (vedettes
    en attente d'une définition) et 'open' (entrées en cours de définition,
    pour absorber les lignes de suite d'un paragraphe)."""
    entries = state['entries']
    for ln in lines:
        latin, arabic = strip_arabic(ln['text'])
        if not latin and not arabic:
            continue
        if is_header_skip(latin):
            continue

        clusters = extract_headword_clusters(ln) if latin else []

        if clusters:
            if state['open']:
                state['open'] = []  # une nouvelle vedette clôt la définition précédente
            for c in clusters:
                if state['pending'] and state['pending'][-1]['gender'] == c['gender']:
                    # même genre que la vedette juste avant -> variante orthographique
                    state['pending'][-1]['words'].extend(c['words'])
                    if c['arabic']:
                        state['pending'][-1]['arabic'] = \
                            (state['pending'][-1]['arabic'] + ' ' + c['arabic']).strip()
                else:
                    state['pending'].append(c)
        else:
            if arabic and not latin and state['pending'] and not state['open']:
                # ligne purement arabe juste après une vedette -> on la rattache
                state['pending'][-1]['arabic'] = \
                    (state['pending'][-1]['arabic'] + ' ' + arabic).strip()
                continue

            if state['pending']:
                # première ligne "non-vedette" après une vedette -> ouvre sa définition
                new_open = []
                for g in state['pending']:
                    entry = {'headwords': g['words'], 'arabic': g['arabic'], 'gender': g['gender'],
                              'chapter': chapter, 'subtheme': subtheme, 'page': page_num,
                              'def_parts': [latin] if latin else []}
                    entries.append(entry)
                    new_open.append(entry)
                state['open'] = new_open
                state['pending'] = []
            elif state['open']:
                for e in state['open']:
                    e['def_parts'].append(latin)
            # sinon : texte d'introduction avant la première entrée -> ignoré
    return state


# ============================================================================
# 4) NETTOYAGE FINAL ET MISE EN FORME DES ENTRÉES
# ============================================================================

def _garbage_run_start(s):
    """Indice où débute une suite d'au moins 5 "mots" quasi sans lettres à la
    suite (signe de bruit OCR), ou -1 si aucune. Sert à tronquer la fin
    corrompue d'une définition sans perdre le début, valide."""
    toks = s.split()
    positions, idx = [], 0
    for t in toks:
        positions.append(idx)
        idx += len(t) + 1
    run_start, run_len = None, 0
    for i, t in enumerate(toks):
        if len(re.sub(r"[^A-Za-zÀ-ÿ]", '', t)) < 2:
            if run_len == 0:
                run_start = i
            run_len += 1
            if run_len >= 5:
                return positions[run_start]
        else:
            run_len = 0
    return -1


def _clean_definition(s):
    cut = _garbage_run_start(s)
    if cut >= 0:
        s = s[:cut].strip()
    return re.sub(r'\s+', ' ', s).strip()


def _is_title_leak(text, known_titles):
    """Détecte qu'une "vedette" extraite est en fait un fragment d'en-tête de
    chapitre/sous-thème qui s'est glissé dans le texte (proximité floue avec
    un titre connu)."""
    norm = re.sub(r'[^a-zà-ÿ ]', '', text.lower()).strip()
    if len(norm) < 5:
        return False
    for title in known_titles:
        if difflib.SequenceMatcher(None, norm, title).ratio() > 0.72 \
                or title in norm or (norm in title and len(norm) >= 6):
            return True
    return False


def finalize_entries(raw_entries, known_titles=None):
    """Transforme les entrées brutes accumulées par parse_page_lines en
    dictionnaires finaux prêts à exporter, en filtrant le bruit résiduel."""
    if known_titles is None:
        known_titles = build_known_titles()
    out = []
    for e in raw_entries:
        if not e.get('subtheme'):
            continue  # avant la 1ere sous-section (A./B./C.) d un chapitre -> texte d intro
        headwords = [h for h in dict.fromkeys(e['headwords']) if not _is_title_leak(h, known_titles)]
        headwords = [h for h in headwords if re.search(r'[aeiouyàâéèêëîïôöùûüAEIOUY]', h)]
        if not headwords:
            continue  # aucune voyelle -> ce n'est pas un vrai prénom

        definition = ' '.join(p for p in e['def_parts'] if p).strip()
        definition = re.sub(r'\s+', ' ', definition)
        definition = re.sub(r'-\s+', '', definition)  # "islami- que" -> "islamique"
        definition = re.sub(r'\s*\d{0,4}\s*LE LIVRE DES PR[ÉE]NOMS.*$', '', definition, flags=re.I).strip()
        definition = _clean_definition(definition)

        if len(re.sub(r'[^A-Za-zÀ-ÿ]', '', definition)) < 8:
            continue  # plus rien d'exploitable après nettoyage
        if len(definition) > 35 and '.' not in definition and ':' not in definition:
            continue  # aucune ponctuation -> ne ressemble pas à une vraie phrase

        transliteration, gloss, meaning = '', '', definition
        m = re.match(r'^(.*?)\s*:\s*(.*)$', definition)
        if m:
            head, meaning = m.group(1).strip(), m.group(2).strip()
            gm = re.match(r'^(.*?)\s*\(([^)]+)\)\s*$', head)
            if gm:
                transliteration, gloss = gm.group(1).strip(), gm.group(2).strip()
            else:
                transliteration = head

        out.append({
            'Prénom(s)': ' / '.join(headwords),
            'Genre': e['gender'],
            'Arabe': e['arabic'],
            'Translittération': transliteration,
            'Équivalent': gloss,
            'Signification': meaning,
            'Thème': e['chapter'],
            'Sous-thème': e['subtheme'],
            'Page (PDF)': e['page'],
        })
    return out


# ============================================================================
# 5) BOUCLE PRINCIPALE : rasterisation + OCR page par page, avec reprise
# ============================================================================

def load_checkpoint(path):
    if os.path.exists(path):
        with open(path, 'rb') as f:
            return pickle.load(f)
    return {'entries': [], 'pending': [], 'open': [], 'last_page': None}


def save_checkpoint(state, path):
    with open(path, 'wb') as f:
        pickle.dump(state, f)


def process_pdf(pdf_path, start, end, out_dir, dpi=300, resume=False, progress_every=10,
                 use_easyocr=True, use_easyocr_latin=True):
    os.makedirs(out_dir, exist_ok=True)
    ckpt_path = os.path.join(out_dir, 'checkpoint.pkl')
    tmp_dir = os.path.join(out_dir, '_tmp_pages')
    os.makedirs(tmp_dir, exist_ok=True)

    reader_ar = get_easyocr_reader('ar') if use_easyocr else None
    reader_fr = get_easyocr_reader('fr') if (use_easyocr and use_easyocr_latin) else None
    print("Passe 1 : Tesseract (structure, couleurs, français, arabe brut)")
    if reader_ar is not None:
        print("Passe 2 : EasyOCR arabe (relecture des zones en script arabe)")
    if reader_fr is not None:
        print("Passe 3 : EasyOCR français (relecture des mots peu fiables de Tesseract)")

    state = load_checkpoint(ckpt_path) if resume else \
        {'entries': [], 'pending': [], 'open': [], 'last_page': None}
    if resume and state['last_page'] is not None:
        start = max(start, state['last_page'] + 1)
        print(f"Reprise après la page {state['last_page']} -> on continue à {start}.")

    t0 = time.time()
    for p in range(start, end + 1):
        prefix = os.path.join(tmp_dir, 'p')
        subprocess.run(['pdftoppm', '-jpeg', '-r', str(dpi), '-f', str(p), '-l', str(p),
                         pdf_path, prefix],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # pdftoppm nomme le fichier "<prefix>-<NNN>.jpg" (NNN = numéro de page, zéro-rempli)
        matches = [f for f in os.listdir(tmp_dir) if f.startswith('p-') and f.endswith('.jpg')]
        if not matches:
            print(f"  (page {p} : aucune image produite par pdftoppm, page ignorée)")
            continue
        real_path = os.path.join(tmp_dir, matches[0])

        chapter, subtheme = lookup_theme(p)
        lines = get_lines_with_color(real_path)
        if reader_ar is not None or reader_fr is not None:
            img = Image.open(real_path).convert('RGB')
            if reader_ar is not None:
                lines = refine_arabic_words(img, lines, reader_ar)
            if reader_fr is not None:
                lines = refine_latin_words(img, lines, reader_fr)
        state = parse_page_lines(lines, p, chapter, subtheme, state)
        state['last_page'] = p
        os.remove(real_path)

        save_checkpoint(state, ckpt_path)  # progression sauvegardée après CHAQUE page

        if (p - start + 1) % progress_every == 0 or p == end:
            elapsed = time.time() - t0
            done = p - start + 1
            print(f"  page {p}/{end} ({done} traitées) - {elapsed:.0f}s écoulées, "
                  f"{elapsed/done:.1f}s/page, {len(state['entries'])} entrées brutes", flush=True)

    if os.path.isdir(tmp_dir) and not os.listdir(tmp_dir):
        os.rmdir(tmp_dir)
    return state


def export_results(state, out_dir):
    results = finalize_entries(state['entries'])
    for i, r in enumerate(results, 1):
        r['ID'] = i
        r['Page (imprimée)'] = r['Page (PDF)'] - PAGE_OFFSET

    cols = ['ID', 'Prénom(s)', 'Genre', 'Arabe', 'Translittération', 'Équivalent',
            'Signification', 'Thème', 'Sous-thème', 'Page (imprimée)', 'Page (PDF)']
    df = pd.DataFrame(results)[cols]

    csv_path = os.path.join(out_dir, 'prenoms_arabes.csv')
    json_path = os.path.join(out_dir, 'prenoms_arabes.json')
    xlsx_path = os.path.join(out_dir, 'prenoms_arabes.xlsx')

    df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    try:
        from openpyxl.styles import Font, Alignment, PatternFill
        from openpyxl.utils import get_column_letter
        from openpyxl import load_workbook

        df.to_excel(xlsx_path, index=False, sheet_name='Prénoms arabes')
        wb = load_workbook(xlsx_path)
        ws = wb['Prénoms arabes']
        for cell in ws[1]:
            cell.fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
            cell.font = Font(bold=True, color='FFFFFF')
        widths = {'ID': 6, 'Prénom(s)': 26, 'Genre': 10, 'Arabe': 18, 'Translittération': 20,
                  'Équivalent': 14, 'Signification': 60, 'Thème': 24, 'Sous-thème': 34,
                  'Page (imprimée)': 10, 'Page (PDF)': 9}
        for i, col in enumerate(df.columns, 1):
            ws.column_dimensions[get_column_letter(i)].width = widths.get(col, 15)
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical='top', wrap_text=(cell.column_letter in ('C', 'G', 'I')))
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        wb.save(xlsx_path)
    except ImportError:
        print("(openpyxl absent : fichier .xlsx non généré, mais CSV/JSON créés)")

    print(f"\n{len(df)} entrées exportées :")
    print(f"  - {csv_path}")
    print(f"  - {json_path}")
    print(f"  - {xlsx_path}")
    return df


# ============================================================================
# 6) LIGNE DE COMMANDE
# ============================================================================

def main():
    global EASYOCR_USE_GPU
    parser = argparse.ArgumentParser(
        description="Extrait 'Le Livre des prénoms arabes' (PDF scanné) vers CSV/JSON/Excel.")
    parser.add_argument('pdf', help="Chemin vers le fichier PDF")
    parser.add_argument('--debut', type=int, default=DICTIONARY_PDF_START,
                         help=f"Première page PDF à traiter (défaut : {DICTIONARY_PDF_START}, "
                              f"début du dictionnaire)")
    parser.add_argument('--fin', type=int, default=DICTIONARY_PDF_END,
                         help=f"Dernière page PDF à traiter (défaut : {DICTIONARY_PDF_END}, "
                              f"fin du dictionnaire)")
    parser.add_argument('--dossier-sortie', default='sortie',
                         help="Dossier où écrire checkpoint + fichiers finaux (défaut : ./sortie)")
    parser.add_argument('--dpi', type=int, default=300,
                         help="Résolution de rasterisation des pages (défaut : 300)")
    parser.add_argument('--reprendre', action='store_true',
                         help="Reprend un traitement précédent depuis checkpoint.pkl")
    parser.add_argument('--sans-easyocr', action='store_true',
                         help="Désactive TOUTES les passes EasyOCR (Tesseract seul : le plus "
                              "rapide, mais le moins précis, surtout sur l'arabe)")
    parser.add_argument('--sans-easyocr-latin', action='store_true',
                         help="Désactive seulement la passe EasyOCR française (garde la passe arabe)")
    parser.add_argument('--gpu', action='store_true',
                         help="Utilise le GPU (CUDA) pour les passes EasyOCR : bien plus rapide")
    args = parser.parse_args()
    EASYOCR_USE_GPU = args.gpu

    if not os.path.exists(args.pdf):
        sys.exit(f"Fichier introuvable : {args.pdf}")

    use_easyocr = not args.sans_easyocr
    use_latin = use_easyocr and not args.sans_easyocr_latin
    per_page = 10 + (7 if use_easyocr else 0) + (4 if use_latin else 0)  # estimation grossière, CPU
    print(f"Traitement des pages {args.debut} à {args.fin} de {args.pdf}")
    print(f"(~{(args.fin - args.debut + 1) * per_page / 60:.0f} minutes estimées à {args.dpi} DPI)\n")

    state = process_pdf(args.pdf, args.debut, args.fin, args.dossier_sortie,
                         dpi=args.dpi, resume=args.reprendre,
                         use_easyocr=use_easyocr, use_easyocr_latin=use_latin)
    export_results(state, args.dossier_sortie)


if __name__ == '__main__':
    main()
