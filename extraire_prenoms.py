#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extraire_prenoms.py  (v2 : multi-cœur + GPU + cache)
====================================================
Extrait de façon structurée « Le Livre des prénoms arabes » (PDF scanné, sans
couche de texte) vers CSV / JSON / Excel.

PIPELINE (3 étages, chacun reprenable grâce au cache par page)
  1. OCR Tesseract EN PARALLÈLE (1 processus par cœur) sur l'image native de
     chaque page : lignes, mots, confiance, couleur (rouge = féminin).
  2. Relectures EasyOCR, sur GPU si possible : le script arabe (bandes de
     lignes) puis les mots latins que Tesseract juge douteux.
  3. Assemblage : machine à états, nettoyage, drapeaux « à vérifier », export.

USAGE RAPIDE
    python extraire_prenoms.py --diagnostic                 # vérifie ton installation
    python extraire_prenoms.py livre.pdf --autotest         # 4 pages, ~1 min, vérité terrain
    python extraire_prenoms.py livre.pdf                    # tout le dictionnaire

Le détail (installation Windows, options, réglages pour un Ryzen 9 + RTX 50)
est dans README.md.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import difflib
import glob
import io
import json
import multiprocessing as mp
import os
import pickle
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

import numpy as np
import pytesseract
from PIL import Image

T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


@contextlib.contextmanager
def quiet():
    """Réduit au silence les bibliothèques bavardes (barres de progression...)."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


# ============================================================================
# 1) STRUCTURE DU LIVRE
#    Table de correspondance « page imprimée -> chapitre / sous-thème »,
#    recopiée de la table des matières imprimée à la fin de l'ouvrage.
# ============================================================================

PAGE_OFFSET = 4  # page PDF = page imprimée + 4 (constant sur tout le livre)

CHAPTERS = [
    (1, "Les prophètes", 49, 76, [
        ("A", "Les prophètes, d'Adam à Jésus", 51, 57),
        ("B", "Le Prophète Mouhammad", 58, 67),
        ("C", "La famille du Prophète", 68, 76)]),
    (2, "La vie spirituelle", 77, 106, [
        ("A", "Foi, piété et aspiration", 79, 87),
        ("B", "Servitude et adoration", 88, 101),
        ("C", "Saints personnages célèbres", 102, 104),
        ("D", "Lieux et moments rituels", 105, 106)]),
    (3, "Vérité et sagesse", 107, 130, [
        ("A", "Preuve et évidence", 109, 111),
        ("B", "Équité, stabilité et rectitude", 112, 118),
        ("C", "Raison, sagesse et connaissance", 119, 130)]),
    (4, "Puissance et protection", 131, 158, [
        ("A", "Autorité, courage et victoire", 133, 147),
        ("B", "Protection, assistance et sécurité", 148, 158)]),
    (5, "Lumière et beauté", 159, 192, [
        ("A", "Lumière et resplendissement", 161, 171),
        ("B", "Astres", 172, 176),
        ("C", "Beauté", 177, 192)]),
    (6, "Excellence et louange", 193, 216, [
        ("A", "Perfection et élection", 195, 203),
        ("B", "Noblesse et élévation", 204, 213),
        ("C", "Louange", 214, 216)]),
    (7, "Vertus", 217, 244, [
        ("A", "Bonté, bienveillance et clémence", 219, 226),
        ("B", "Pureté", 227, 230),
        ("C", "Patience, contentement et fidélité", 231, 234),
        ("D", "Générosité et reconnaissance", 235, 244)]),
    (8, "Le bonheur", 245, 274, [
        ("A", "Bien-être, félicité et prospérité", 247, 264),
        ("B", "Amour, amitié et harmonie", 265, 274)]),
    (9, "Secrets de l'âme", 275, 284, [
        ("A", "Désir et espoir", 277, 279),
        ("B", "Inspiration, confidence et quiétude", 280, 284)]),
    (10, "La nature", 285, 324, [
        ("A", "Végétation, fleurs et parfums", 287, 298),
        ("B", "Pierres précieuses", 299, 303),
        ("C", "Éléments naturels", 304, 311),
        ("D", "Animaux", 312, 324)]),
    (11, "Cette vie et l'autre", 325, 334, [
        ("A", "Vitalité et longévité", 327, 330),
        ("B", "Vie éternelle et paradis", 331, 334)]),
]

DICT_START = CHAPTERS[0][2] + PAGE_OFFSET   # 53
DICT_END = CHAPTERS[-1][3] + PAGE_OFFSET     # 338


def lookup_theme(pdf_page: int) -> tuple[str, str]:
    """(chapitre, sous-thème) d'une page PDF."""
    printed = pdf_page - PAGE_OFFSET
    for num, title, start, end, subs in CHAPTERS:
        if start <= printed <= end:
            for letter, subtitle, s0, s1 in subs:
                if s0 <= printed <= s1:
                    return f"{num}. {title}", f"{letter}. {subtitle}"
            return f"{num}. {title}", ""
    return "", ""


def known_titles() -> set[str]:
    """Titres normalisés : servent à repérer les en-têtes qui se glissent dans l'OCR."""
    t = {'masculin', 'feminin', 'féminin'}
    for _, title, _, _, subs in CHAPTERS:
        t.add(re.sub(r'[^a-zà-ÿ ]', '', title.lower()))
        for _, sub, _, _ in subs:
            t.add(re.sub(r'[^a-zà-ÿ ]', '', sub.lower()))
    return t


# ============================================================================
# 2) TEXTE : arabe / latin, lexiques
# ============================================================================

ARABIC_RE = re.compile(r'[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF\u200e\u200f]+')
# « vraie » lettre arabe (ni voyelle brève, ni ponctuation, ni marque de direction)
ARABIC_LETTER_RE = re.compile(r'[\u0621-\u064A\u066E-\u06D3\u06FA-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]')


def strip_arabic(text: str) -> tuple[str, str]:
    """Sépare un texte OCR en (partie latine, partie arabe)."""
    arabic = ' '.join(a.strip() for a in ARABIC_RE.findall(text) if a.strip())
    latin = re.sub(r'\s+', ' ', ARABIC_RE.sub('', text)).strip()
    return latin, arabic


def arab_norm(s: str) -> str:
    """Lettres de base uniquement (sans voyelles, alefs unifiés) pour comparer deux lectures."""
    s = re.sub(r'[\u064B-\u065F\u0670\u0640\u200e\u200f]', '', s)
    s = re.sub(r'[آأإٱ]', 'ا', s).replace('ى', 'ي')
    return re.sub(r'[^\u0621-\u064A]', '', s)


STOPWORDS = set("""à de des du le la les un une et ou pour que qui dans avec sur par ce cette
son sa ses en au aux d l qu s c n y ne pas plus est sont fut sera était été il elle ils elles
donne donné entre chez selon comme tout tous toute toutes leur leurs ainsi mais donc or ni car
ibn abou abu ben fille fils""".split())

# mots capitalisés fréquents dans les définitions, jamais des vedettes
COMMON_NON_NAMES = {w.lower() for w in """
Dieu Coran Allah Islam Paradis Seigneur Prophète Prophete Signifie Sourate
Nom Noms Selon Ainsi Ce Cette Ces Il Elle Ils Elles Un Une Des Les La Le
Cf Voir Coranique Coraniques Musulmans Musulman Islamique Islamiques
""".split()}

NAME_WORD_RE = re.compile(r"[A-ZÀ-Ü][a-zà-ÿ'’\-]{1,25}")

HEADER_SKIP_PATTERNS = [
    re.compile(r'^\s*MASC', re.I),
    re.compile(r'^\s*F[EÉ]MIN', re.I),
    re.compile(r'^\s*\d{0,4}\s*LE LIVRE DES PR', re.I),
    re.compile(r'^\s*\d{1,4}\s*$'),
    re.compile(r'^\s*[A-D]\.\s+[A-ZÀ-Ü]'),
    re.compile(r'^\s*\d{1,2}\.\s+[A-ZÀ-Ü]{3,}'),
]


def is_header_skip(latin: str) -> bool:
    return any(p.match(latin) for p in HEADER_SKIP_PATTERNS)


def classify_color(rgb) -> str:
    """Vedettes féminines imprimées en rouge, masculines en noir."""
    r, g, b = rgb
    return 'red' if (r - (g + b) / 2) > 22 else 'dark'


# ============================================================================
# 3) IMAGE DE PAGE : l'image NATIVE du scan (~1800x2900 px), pas un 300 dpi
#    suréchantillonné (5700x9000 px, 9x plus de pixels pour la même information)
# ============================================================================

_FITZ_DOCS: dict = {}


def _fitz_doc(pdf: str):
    import fitz  # PyMuPDF
    d = _FITZ_DOCS.get(pdf)
    if d is None:
        d = _FITZ_DOCS[pdf] = fitz.open(pdf)
    return d


def _which(name: str, bindir: str = '') -> str | None:
    if bindir:
        for cand in (os.path.join(bindir, name), os.path.join(bindir, name + '.exe')):
            if os.path.exists(cand):
                return cand
    return shutil.which(name)


def _native_fitz(pdf: str, page: int, stem: str) -> str | None:
    try:
        import fitz
        doc = _fitz_doc(pdf)
        best = None
        for info in doc[page - 1].get_images(full=True):
            xref, w, h = info[0], info[2], info[3]
            if best is None or w * h > best[1]:
                best = (xref, w * h)
        if best is None or best[1] < 400_000:
            return None  # pas une page-image
        data = doc.extract_image(best[0])
        ext = (data.get('ext') or 'jpg').lower()
        if ext in ('jpg', 'jpeg', 'png'):
            out = f"{stem}.{'jpg' if ext == 'jpeg' else ext}"
            with open(out, 'wb') as f:
                f.write(data['image'])
            return out
        pix = fitz.Pixmap(doc, best[0])
        if pix.n - pix.alpha >= 4:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        out = stem + '.png'
        pix.save(out)
        return out
    except Exception:
        return None


def _native_poppler(pdf: str, page: int, stem: str, bindir: str) -> str | None:
    exe = _which('pdfimages', bindir)
    if not exe:
        return None
    prefix = stem + '_img'
    try:
        subprocess.run([exe, '-j', '-f', str(page), '-l', str(page), pdf, prefix],
                       capture_output=True, check=True)
    except Exception:
        return None
    files = glob.glob(glob.escape(prefix) + '-*')
    best, area = None, 0
    for f in files:
        try:
            with Image.open(f) as im:
                if im.width * im.height > area:
                    best, area = f, im.width * im.height
        except Exception:
            pass
    for f in files:
        if f != best:
            os.remove(f)
    if best is None or area < 400_000:
        if best:
            os.remove(best)
        return None
    if best.lower().endswith(('.ppm', '.pgm', '.pbm')):
        out = stem + '.jpg'
        Image.open(best).convert('RGB').save(out, quality=95)
        os.remove(best)
        return out
    out = stem + os.path.splitext(best)[1].lower()
    os.replace(best, out)
    return out


def _raster(pdf: str, page: int, stem: str, dpi: int, bindir: str) -> str | None:
    try:
        doc = _fitz_doc(pdf)
        pix = doc[page - 1].get_pixmap(dpi=dpi, alpha=False)
        out = stem + '.jpg'
        Image.frombytes('RGB', (pix.width, pix.height), pix.samples).save(out, quality=92)
        return out
    except Exception:
        pass
    exe = _which('pdftoppm', bindir)
    if not exe:
        return None
    prefix = stem + '_ras'
    subprocess.run([exe, '-jpeg', '-r', str(dpi), '-f', str(page), '-l', str(page), pdf, prefix],
                   capture_output=True, check=True)
    files = glob.glob(glob.escape(prefix) + '-*.jpg')
    if not files:
        return None
    out = stem + '.jpg'
    os.replace(files[0], out)
    return out


def materialize_page_image(pdf: str, page: int, img_dir: str, source: str, dpi: int, bindir: str = '') -> str:
    """Écrit (une fois) l'image de la page sur disque et renvoie son chemin.
    source='auto'/'natif' : image d'origine du scan ; 'raster' : rasterisation à `dpi`."""
    stem = os.path.join(img_dir, f"p{page:04d}")
    for ext in ('jpg', 'png'):
        if os.path.exists(f"{stem}.{ext}"):
            return f"{stem}.{ext}"
    out = None
    if source != 'raster':
        out = _native_fitz(pdf, page, stem) or _native_poppler(pdf, page, stem, bindir)
    if out is None:
        out = _raster(pdf, page, stem, dpi if source == 'raster' else 200, bindir)
    if out is None:
        raise RuntimeError("impossible d'extraire la page : installe PyMuPDF (pip install pymupdf) "
                           "ou Poppler (pdfimages/pdftoppm dans le PATH, ou --poppler DOSSIER)")
    return out


# ============================================================================
# 4) OCR TESSERACT D'UNE PAGE (exécuté dans les processus workers)
# ============================================================================

def find_tesseract(hint: str = '') -> str | None:
    cmd = hint or os.environ.get('TESSERACT_CMD') or shutil.which('tesseract')
    if not cmd and os.name == 'nt':
        for c in (r'C:\Program Files\Tesseract-OCR\tesseract.exe',
                  r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
                  os.path.expandvars(r'%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe')):
            if os.path.exists(c):
                return c
    return cmd


def setup_tesseract(hint: str = '') -> str:
    cmd = find_tesseract(hint)
    if not cmd:
        raise SystemExit("Tesseract introuvable. Installe-le (Windows : https://github.com/UB-Mannheim/tesseract/wiki, "
                         "cocher French + Arabic) puis relance, ou donne son chemin avec --tesseract.")
    pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        langs = set(pytesseract.get_languages(config=''))
    except Exception as e:
        raise SystemExit(f"Tesseract ne répond pas ({e}).")
    missing = [lang for lang in ('fra', 'ara') if lang not in langs]
    if missing:
        raise SystemExit(f"Langues Tesseract manquantes : {', '.join(missing)} (installe fra et ara).")
    return cmd


def _to_int_color(arr_row) -> tuple[int, int, int]:
    return tuple(int(v) for v in arr_row)


def ocr_lines(img_path: str, min_conf: int = 20):
    """Tesseract (fra+ara) -> (lignes, largeur, hauteur, hauteur_de_ligne_médiane).
    Chaque ligne/mot porte position, couleur médiane (pixels quasi blancs ignorés) et confiance."""
    data = pytesseract.image_to_data(img_path, lang='fra+ara', config='--psm 6',
                                     output_type=pytesseract.Output.DICT, timeout=300)
    with Image.open(img_path) as im:
        arr = np.asarray(im.convert('RGB'))
    H, W = arr.shape[:2]

    groups: dict = {}
    for i in range(len(data['text'])):
        text = data['text'][i].strip()
        if not text:
            continue
        try:
            conf = float(data['conf'][i])
        except (ValueError, TypeError):
            conf = -1.0
        if conf < min_conf:
            continue
        key = (data['block_num'][i], data['par_num'][i], data['line_num'][i])
        x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
        groups.setdefault(key, []).append((text, x, y, x + w, y + h, conf))

    def color(l, t, r, b):
        crop = arr[max(0, t):min(H, b), max(0, l):min(W, r)].reshape(-1, 3)
        fg = crop[~np.all(crop > 190, axis=1)]
        return _to_int_color(np.median(fg, axis=0)) if len(fg) else (0, 0, 0)

    lines = []
    for ws in groups.values():
        words = [{'text': t, 'left': l, 'top': tp, 'right': r, 'bottom': b,
                  'color': color(l, tp, r, b), 'conf': c} for t, l, tp, r, b, c in ws]
        left, top = min(w['left'] for w in words), min(w['top'] for w in words)
        right, bottom = max(w['right'] for w in words), max(w['bottom'] for w in words)
        lines.append({'top': top, 'left': left, 'right': right, 'bottom': bottom,
                      'text': ' '.join(w['text'] for w in words),
                      'color': color(left, top, right, bottom), 'words': words})
    lines.sort(key=lambda ln: (ln['top'], ln['left']))
    heights = [w['bottom'] - w['top'] for ln in lines for w in ln['words'] if w['conf'] >= 60]
    lh = int(np.median(heights)) if heights else 30
    return lines, W, H, max(lh, 12)


def _atomic_dump(obj, path: str) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        pickle.dump(obj, f, protocol=4)
    os.replace(tmp, path)


def _load(path: str):
    with open(path, 'rb') as f:
        return pickle.load(f)


def _init_worker(tess_cmd: str) -> None:
    # Tesseract exploite déjà chaque cœur via nos N processus : on interdit son
    # multithreading interne (sinon N processus x M threads = surcharge).
    os.environ['OMP_THREAD_LIMIT'] = '1'
    if tess_cmd:
        pytesseract.pytesseract.tesseract_cmd = tess_cmd


def _worker_ocr(task):
    page, w = task
    t0 = time.time()
    try:
        img = materialize_page_image(w['pdf'], page, w['img_dir'], w['source'], w['dpi'], w['poppler'])
        lines, W, H, lh = ocr_lines(img)
        _atomic_dump({'page': page, 'img': img, 'W': W, 'H': H, 'lh': lh, 'lines': lines},
                     os.path.join(w['ocr_dir'], f"p{page:04d}.pkl"))
        return {'page': page, 'secs': time.time() - t0, 'lines': len(lines)}
    except Exception as e:  # une page défaillante ne doit pas arrêter les autres
        return {'page': page, 'secs': time.time() - t0, 'error': f"{type(e).__name__}: {e}"}


# ============================================================================
# 5) RECONNAISSANCE DES VEDETTES (prénoms) + MACHINE À ÉTATS
# ============================================================================

def _rebuild_line_text(ln: dict) -> None:
    ln['text'] = ' '.join(w['text'] for w in ln['words'])


def _pick_cluster(clusters: list[dict], word: dict) -> dict:
    """L'arabe est imprimé À DROITE de son prénom : on le rattache à la vedette la
    plus proche située à sa gauche ; à défaut, à la plus proche en abscisse."""
    tol = 0.5 * max(8, word['bottom'] - word['top'])
    left_ok = [c for c in clusters if c['_right'] <= word['left'] + tol]
    if left_ok:
        return max(left_ok, key=lambda c: c['_right'])
    xc = (word['left'] + word['right']) / 2
    return min(clusters, key=lambda c: min(abs(xc - x) for x in c['_centers']))


def extract_headword_clusters(ln: dict) -> list[dict]:
    """Repère sur une ligne un ou plusieurs prénoms (vedettes), en ignorant le bruit OCR autour.
    Un cluster = variantes orthographiques d'une même entrée (mots adjacents de même couleur) ;
    un prénom noir et un prénom rouge sur la même ligne = deux clusters (masculin / féminin).
    Le script arabe de la ligne est rattaché à sa vedette. [] si ce n'est pas une ligne de vedette."""
    text = ' '.join(w['text'] for w in ln['words'])
    if ':' in text:
        return []

    candidates, arabic_words = [], []
    for w in ln['words']:
        latin_part, arabic_part = strip_arabic(w['text'])
        m = NAME_WORD_RE.match(latin_part.strip("*|'\""))
        if m and m.group(0).lower().strip("'’-") not in STOPWORDS and m.group(0).lower() not in COMMON_NON_NAMES:
            candidates.append((w, m.group(0)))
        if arabic_part and ARABIC_LETTER_RE.search(arabic_part):
            arabic_words.append((w, arabic_part))

    if not candidates or len(candidates) > 4:
        return []
    letters_names = sum(len(re.sub(r'[^A-Za-zÀ-ÿ]', '', s)) for _, s in candidates)
    letters_line = len(re.sub(r'[^A-Za-zÀ-ÿ]', '', text))
    if letters_line == 0 or letters_names / letters_line < 0.5:
        return []  # surtout du bruit -> pas une ligne de vedette

    clusters: list[dict] = []
    for w, s in candidates:
        gender = 'Féminin' if classify_color(w['color']) == 'red' else 'Masculin'
        if clusters and clusters[-1]['gender'] == gender:
            c = clusters[-1]
            c['words'].append(s)
            c['_centers'].append((w['left'] + w['right']) / 2)
            c['_right'] = max(c['_right'], w['right'])
        else:
            clusters.append({'words': [s], 'gender': gender, 'arabic': '', 'arabic_alt': '',
                             '_centers': [(w['left'] + w['right']) / 2], '_right': w['right'],
                             '_ar': [], '_alt': []})

    for aw, atext in arabic_words:
        c = _pick_cluster(clusters, aw)
        c['_ar'].append(((aw['left'] + aw['right']) / 2, atext))
        if aw.get('alt'):
            c['_alt'].append(((aw['left'] + aw['right']) / 2, aw['alt']))
    for aw in ln.get('alt_words', []):
        _, atext = strip_arabic(aw['text'])
        if atext and ARABIC_LETTER_RE.search(atext):
            _pick_cluster(clusters, aw)['_alt'].append(((aw['left'] + aw['right']) / 2, atext))

    for c in clusters:  # ordre logique de l'arabe = de droite à gauche
        c['arabic'] = ' '.join(t for _, t in sorted(c['_ar'], key=lambda it: -it[0]))
        c['arabic_alt'] = ' '.join(t for _, t in sorted(c['_alt'], key=lambda it: -it[0]))
        for k in ('_centers', '_right', '_ar', '_alt'):
            del c[k]
    return clusters


def parse_page_lines(lines: list[dict], page: int, chapter: str, subtheme: str, state: dict) -> dict:
    """Fait avancer la machine à états sur les lignes d'UNE page. `state` :
    'entries' (résultats), 'pending' (vedettes en attente de définition),
    'open' (entrées en cours de définition, pour absorber les lignes de suite)."""
    for ln in lines:
        latin, arabic = strip_arabic(ln['text'])
        if not latin and not arabic:
            continue
        if is_header_skip(latin):
            continue
        clusters = extract_headword_clusters(ln) if latin else []

        if clusters:
            state['open'] = []  # une nouvelle vedette clôt la définition précédente
            for c in clusters:
                if state['pending'] and state['pending'][-1]['gender'] == c['gender']:
                    p = state['pending'][-1]  # même genre que juste avant : variante orthographique
                    p['words'].extend(c['words'])
                    p['arabic'] = (p['arabic'] + ' ' + c['arabic']).strip()
                    p['arabic_alt'] = (p['arabic_alt'] + ' ' + c['arabic_alt']).strip()
                else:
                    state['pending'].append(c)
            continue

        if arabic and not latin and state['pending'] and not state['open']:
            state['pending'][-1]['arabic'] = (state['pending'][-1]['arabic'] + ' ' + arabic).strip()
            continue

        if state['pending']:  # 1re ligne non-vedette après une vedette = début de la définition
            new_open = []
            for g in state['pending']:
                e = {'headwords': g['words'], 'arabic': g['arabic'], 'arabic_alt': g['arabic_alt'],
                     'gender': g['gender'], 'chapter': chapter, 'subtheme': subtheme, 'page': page,
                     'def_parts': [latin] if latin else []}
                state['entries'].append(e)
                new_open.append(e)
            state['open'], state['pending'] = new_open, []
        elif state['open']:
            for e in state['open']:
                e['def_parts'].append(latin)
        # sinon : texte d'introduction avant la 1re entrée -> ignoré
    return state


# ============================================================================
# 6) GPU : détection, auto-test, repli CPU
# ============================================================================

GPU_HELP = (
    "  -> RTX série 50 (Blackwell, sm_120) : il faut un PyTorch compilé avec CUDA 12.8 ou plus.\n"
    "       pip uninstall -y torch torchvision torchaudio\n"
    "       pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128\n"
    "     Si 'no kernel image' / 'sm_120 is not compatible' persiste, essaie la version nightly :\n"
    "       pip install --pre torch torchvision --index-url https://download.pytorch.org/whl/nightly/cu128\n"
    "     (ou .../nightly/cu130 avec un pilote NVIDIA très récent), puis relance --diagnostic.")


def detect_gpu(mode: str = 'auto') -> tuple[bool, str]:
    """Teste réellement le GPU (un petit calcul CUDA), pas seulement sa présence :
    c'est ce qui évite l'erreur classique « no kernel image » sur les cartes récentes."""
    if mode == 'non':
        return False, "GPU désactivé (--gpu non)."
    try:
        import torch
    except Exception as e:
        return False, f"PyTorch indisponible ({e})."
    if not torch.cuda.is_available():
        return False, (f"PyTorch {torch.__version__} sans CUDA (version CPU, ou pilote NVIDIA absent).\n" + GPU_HELP)
    try:
        name = torch.cuda.get_device_name(0)
        cap = torch.cuda.get_device_capability(0)
        vram = torch.cuda.get_device_properties(0).total_memory / 2 ** 30
        x = torch.randn(2, 3, 64, 64, device='cuda')
        y = torch.nn.Conv2d(3, 8, 3).to('cuda')(x)
        torch.cuda.synchronize()
        float(y.abs().sum())
    except Exception as e:
        return False, (f"GPU détecté mais inutilisable avec PyTorch {torch.__version__} : "
                       f"{str(e).splitlines()[0][:150]}\n" + GPU_HELP)
    return True, (f"GPU OK : {name} (sm_{cap[0]}{cap[1]}, {vram:.0f} Go) - PyTorch {torch.__version__}, "
                  f"CUDA {torch.version.cuda}")


# ============================================================================
# 7) RELECTURES EASYOCR (processus principal ; GPU si dispo)
# ============================================================================

def _dec_kw(decoder: str) -> dict:
    return {'decoder': 'beamsearch', 'beamWidth': 5} if decoder == 'beamsearch' else {'decoder': 'greedy'}


def refine_arabic_bands(gray, lines, reader, batch: int, decoder: str, lh: int) -> None:
    """Passe arabe « bandes » : pour chaque ligne de vedette, EasyOCR (détection + lecture)
    lit la bande horizontale autour de la ligne. Robuste : retrouve aussi l'arabe que Tesseract
    n'avait pas repéré. La lecture Tesseract est conservée (colonne « Arabe (Tesseract) »)."""
    H, _ = gray.shape
    for ln in lines:
        if not extract_headword_clusters(ln):
            continue
        h = max(ln['bottom'] - ln['top'], lh)
        cy = (ln['top'] + ln['bottom']) / 2.0
        y0, y1 = int(max(0, cy - 1.6 * h)), int(min(H, cy + 1.6 * h))
        if y1 - y0 < 16:
            continue
        with quiet():
            res = reader.readtext(gray[y0:y1, :], detail=1, paragraph=False, batch_size=batch, **_dec_kw(decoder))
        found = []
        for box, text, conf in res:
            text = text.strip()
            n_letters = len(ARABIC_LETTER_RE.findall(text))
            if n_letters == 0 or n_letters < 0.5 * len(re.sub(r'\s', '', text)):
                continue
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            top, bottom = min(ys) + y0, max(ys) + y0
            if abs((top + bottom) / 2.0 - cy) > 1.25 * h:
                continue  # texte d'une ligne voisine
            found.append({'text': text, 'left': int(min(xs)), 'right': int(max(xs)), 'top': int(top),
                          'bottom': int(bottom), 'color': (0, 0, 0), 'conf': int(float(conf) * 100)})
        if not found:
            continue
        ln['alt_words'] = [dict(w) for w in ln['words'] if ARABIC_LETTER_RE.search(w['text'])]
        kept = []
        for w in ln['words']:
            if ARABIC_LETTER_RE.search(w['text']):
                latin_part, _ = strip_arabic(w['text'])
                if latin_part:
                    kept.append({**w, 'text': latin_part})
            else:
                kept.append(w)
        ln['words'] = sorted(kept + found, key=lambda w: w['left'])
        _rebuild_line_text(ln)


def refine_arabic_words(img_rgb, lines, reader, decoder: str, lh: int) -> None:
    """Passe arabe « mots » : relit chaque mot arabe repéré par Tesseract (recadrage RGB + marge
    fixe dans l'image d'origine). C'est la version simple, validée avant le mode « bandes » :
    en cas de doute, c'est celle-ci qu'il faut utiliser (--arabe mots, valeur par défaut)."""
    H, W = img_rgb.shape[:2]
    pad = 12
    for ln in lines:
        changed = False
        for w in ln['words']:
            if not ARABIC_LETTER_RE.search(w['text']):
                continue
            latin_part, arabic_only = strip_arabic(w['text'])
            if len(re.sub(r'\s', '', arabic_only)) < 2:
                continue
            crop = img_rgb[max(0, w['top'] - pad):min(H, w['bottom'] + pad),
                           max(0, w['left'] - pad):min(W, w['right'] + pad)]
            if crop.size == 0:
                continue
            with quiet():
                result = reader.readtext(crop, detail=0, paragraph=False)
            easy = ' '.join(result).strip()
            if easy and ARABIC_LETTER_RE.search(easy):
                w['alt'] = arabic_only
                w['text'] = (latin_part + ' ' + easy).strip()
                changed = True
        if changed:
            _rebuild_line_text(ln)


def recognize_boxes(reader, gray, boxes: list[list[int]], batch: int, decoder: str) -> list[tuple[str, float]]:
    """Lit seulement les rectangles donnés [x0, x1, y0, y1] (pas de détection).
    En lot sur GPU, EasyOCR renvoie les résultats TRIÉS par position verticale : on les rattache
    donc à leur boîte par coordonnées, avec repli séquentiel si le rattachement est ambigu."""
    out: list[tuple[str, float]] = [('', 0.0)] * len(boxes)
    if not boxes:
        return out
    index = {(b[0], b[2]): i for i, b in enumerate(boxes)}
    with quiet():
        res = reader.recognize(gray, horizontal_list=boxes, free_list=[], detail=1, batch_size=batch,
                               **_dec_kw(decoder))
    placed = set()
    for poly, text, conf in res:
        i = index.get((int(poly[0][0]), int(poly[0][1])))
        if i is not None:
            out[i] = (text, float(conf))
            placed.add(i)
    if len(placed) < len(res) or len(index) < len(boxes):  # ambigu -> séquentiel (lent mais sûr)
        for i, b in enumerate(boxes):
            with quiet():
                r = reader.recognize(gray, horizontal_list=[b], free_list=[], detail=1, batch_size=1,
                                     **_dec_kw(decoder))
            out[i] = (r[0][1], float(r[0][2])) if r else ('', 0.0)
    return out


LATIN_OK_RE = re.compile(r"^[A-Za-zÀ-ÿŒœ0-9 '’\-.,;:!?()«»\"°*]+$")


def _tidy_latin(text: str) -> str:
    text = re.sub(r'\s+([,.;:!?)])', r'\1', text.strip())   # "hanîf) ," -> "hanîf),"
    return re.sub(r'\s+-$', '-', text)                        # "Khalîl -" -> "Khalîl-"


def refine_latin_words(gray, lines, reader, batch: int, decoder: str, lh: int,
                       conf_below: int = 80, accept_conf: float = 0.80) -> None:
    """Relit UNIQUEMENT les mots latins que Tesseract juge peu fiables (confiance < 80), avec des
    garde-fous stricts avant de remplacer : confiance EasyOCR >= 0,80, caractères plausibles,
    longueur cohérente, apostrophes conservées (EasyOCR les oublie). Corrige typiquement les
    mots en italique lus comme des chiffres (« 11116916, » -> « intègre, »)."""
    H, W = gray.shape
    padx, pady = max(2, int(0.15 * lh)), max(2, int(0.20 * lh))
    cands, boxes = [], []
    for ln in lines:
        for w in ln['words']:
            clean = w['text'].replace('\u200e', '').replace('\u200f', '').strip()
            if ARABIC_LETTER_RE.search(clean) or w.get('conf', 100) >= conf_below:
                continue
            if len(re.sub(r'[^A-Za-zÀ-ÿ0-9]', '', clean)) < 2:
                continue
            b = [max(0, w['left'] - padx), min(W, w['right'] + padx),
                 max(0, w['top'] - pady), min(H, w['bottom'] + pady)]
            if b[1] > b[0] and b[3] > b[2]:
                cands.append((ln, w, clean))
                boxes.append(b)
    if not cands:
        return
    changed = set()
    for (ln, w, tess), (txt, conf) in zip(cands, recognize_boxes(reader, gray, boxes, batch, decoder)):
        txt = _tidy_latin(txt)
        if conf < accept_conf or not txt or txt == tess or not LATIN_OK_RE.match(txt):
            continue
        ratio = len(txt) / max(1, len(tess))
        if ratio < 0.5 or ratio > 2.0:
            continue
        # un simple prefixe/suffixe plus court (ex. Tesseract "Salih" -> EasyOCR "Sal") est presque
        # toujours une troncature de lecture, pas une correction : on la rejette, contrairement à une
        # réécriture qui change des caractères au milieu du mot (ex. "00/141116" -> "calame").
        a, b = tess.rstrip('.,;:!?)'), txt.rstrip('.,;:!?)')
        if abs(len(a) - len(b)) >= 2 and (a.startswith(b) or a.endswith(b) or b.startswith(a) or b.endswith(a)):
            continue
        if re.search(r"['’]", tess) and not re.search(r"['’]", txt):
            continue
        w['text'] = txt
        changed.add(id(ln))
    for ln in lines:
        if id(ln) in changed:
            _rebuild_line_text(ln)


class RefineEngine:
    """Charge les lecteurs EasyOCR (arabe, français) une fois ; bascule sur CPU si le GPU échoue."""

    def __init__(self, cfg: 'Config', use_gpu: bool):
        self.cfg, self.use_gpu = cfg, use_gpu
        self.ar = self.fr = None
        self._load()

    def _load(self) -> None:
        import easyocr

        def make(lang):
            with quiet():
                return easyocr.Reader([lang], gpu=self.use_gpu, verbose=False)

        self.ar = make('ar') if self.cfg.arabe != 'non' else None
        self.fr = make('fr') if self.cfg.latin else None
        if not self.use_gpu:
            try:
                import torch
                torch.set_num_threads(max(1, (os.cpu_count() or 2) - self.cfg.workers))
            except Exception:
                pass

    def refine(self, lines, gray, rgb, lh):
        backup = copy.deepcopy(lines)
        try:
            self._refine(lines, gray, rgb, lh)
            return lines
        except Exception as e:
            if not self.use_gpu:
                raise
            log(f"  ! erreur GPU ({type(e).__name__}: {str(e)[:100]}) -> repli CPU pour la suite")
            self.use_gpu = False
            self._load()
            self._refine(backup, gray, rgb, lh)
            return backup

    def _refine(self, lines, gray, rgb, lh) -> None:
        c = self.cfg
        if self.ar is not None:
            if c.arabe == 'bandes':
                refine_arabic_bands(gray, lines, self.ar, c.batch, c.decodeur, lh)
            else:
                refine_arabic_words(rgb, lines, self.ar, c.decodeur, lh)
        if self.fr is not None:
            refine_latin_words(gray, lines, self.fr, c.batch, c.decodeur, lh)


# ============================================================================
# 8) NETTOYAGE FINAL + DRAPEAUX « À VÉRIFIER »
# ============================================================================

def _garbage_run_start(s: str) -> int:
    """Indice où débute une suite de >= 5 « mots » quasi sans lettres (bruit OCR), sinon -1."""
    toks, positions, idx = s.split(), [], 0
    for t in toks:
        positions.append(idx)
        idx += len(t) + 1
    start, run = None, 0
    for i, t in enumerate(toks):
        if len(re.sub(r"[^A-Za-zÀ-ÿ]", '', t)) < 2:
            if run == 0:
                start = i
            run += 1
            if run >= 5:
                return positions[start]
        else:
            run = 0
    return -1


def _clean_definition(s: str) -> str:
    cut = _garbage_run_start(s)
    if cut >= 0:
        s = s[:cut].strip()
    return re.sub(r'\s+', ' ', s).strip()


def _is_title_leak(text: str, titles: set[str]) -> bool:
    norm = re.sub(r'[^a-zà-ÿ ]', '', text.lower()).strip()
    if len(norm) < 5:
        return False
    return any(difflib.SequenceMatcher(None, norm, t).ratio() > 0.72 or t in norm
               or (norm in t and len(norm) >= 6) for t in titles)


MIXED_RE = re.compile(r'(?i)(?:[a-zà-ÿ]{2,}\d|\d[a-zà-ÿ]{2,})')
LONG_DIGITS_RE = re.compile(r'\d{6,}')
SUSPECT_RE = re.compile(r'[\[\]{}|~^_=<>\\]')

FLAG_HELP = {
    'arabe_vide': "aucun script arabe lu (information : ~1 entrée sur 2)",
    'arabe_divergent': "Tesseract et EasyOCR lisent des choses très différentes",
    'chiffres_dans_mot': "chiffres mêlés à des lettres (souvent un mot italique mal lu)",
    'suite_de_chiffres': "longue suite de chiffres dans le sens",
    'caracteres_suspects': "caractères improbables ([ ] { } | ~ ^ _ =)",
    'sans_point_final': "définition sans ponctuation finale (tronquée ?)",
    'translit_vide': "translittération non isolée",
    'vedette_courte': "nom de moins de 3 lettres",
}
INFO_FLAGS = {'arabe_vide'}


def quality_flags(row: dict) -> list[str]:
    sig, flags = row['Signification'], []
    if not row['Arabe']:
        flags.append('arabe_vide')
    elif row.get('Arabe (Tesseract)'):
        a, b = arab_norm(row['Arabe']), arab_norm(row['Arabe (Tesseract)'])
        if a and b and difflib.SequenceMatcher(None, a, b).ratio() < 0.5:
            flags.append('arabe_divergent')
    if MIXED_RE.search(sig):
        flags.append('chiffres_dans_mot')
    if LONG_DIGITS_RE.search(sig):
        flags.append('suite_de_chiffres')
    if SUSPECT_RE.search(sig):
        flags.append('caracteres_suspects')
    if not re.search(r'[.!?…)»"]\s*$', sig):
        flags.append('sans_point_final')
    if not row['Translittération']:
        flags.append('translit_vide')
    if any(len(re.sub(r'[^A-Za-zÀ-ÿ]', '', t)) < 3 for t in re.split(r'[ /]+', row['Prénom(s)']) if t):
        flags.append('vedette_courte')
    return flags


def finalize_entries(raw: list[dict], titles: set[str] | None = None) -> list[dict]:
    """Entrées brutes -> lignes finales, en filtrant le bruit résiduel."""
    titles = titles or known_titles()
    out = []
    for e in raw:
        if not e.get('subtheme'):
            continue  # avant la 1re sous-section d'un chapitre : texte d'introduction, pas une entrée
        heads = [h for h in dict.fromkeys(e['headwords']) if not _is_title_leak(h, titles)]
        heads = [h for h in heads if re.search(r'[aeiouyàâéèêëîïôöùûüAEIOUY]', h)]
        if not heads:
            continue  # aucune voyelle : pas un vrai prénom

        d = re.sub(r'\s+', ' ', ' '.join(p for p in e['def_parts'] if p)).strip()
        d = re.sub(r'-\s+', '', d)                                        # "islami- que" -> "islamique"
        d = re.sub(r'\s*\d{0,4}\s*LE LIVRE DES PR[ÉE]NOMS.*$', '', d, flags=re.I).strip()
        d = _clean_definition(d)
        if len(re.sub(r'[^A-Za-zÀ-ÿ]', '', d)) < 8:
            continue
        if len(d) > 35 and '.' not in d and ':' not in d:
            continue  # aucune ponctuation : ne ressemble pas à une phrase

        translit, gloss, meaning = '', '', d
        m = re.match(r'^(.*?)\s*:\s*(.*)$', d)
        if m:
            head, meaning = m.group(1).strip(), m.group(2).strip()
            gm = re.match(r'^(.*?)\s*\(([^)]+)\)\s*$', head)
            translit, gloss = (gm.group(1).strip(), gm.group(2).strip()) if gm else (head, '')

        alt = e.get('arabic_alt', '')
        row = {'Prénom(s)': ' / '.join(heads), 'Genre': e['gender'], 'Arabe': e['arabic'],
               'Arabe (Tesseract)': alt if alt and arab_norm(alt) != arab_norm(e['arabic']) else '',
               'Translittération': translit, 'Équivalent': gloss, 'Signification': meaning,
               'Thème': e['chapter'], 'Sous-thème': e['subtheme'], 'Page (PDF)': e['page']}
        row['À vérifier'] = '; '.join(quality_flags(row))
        out.append(row)
    return out


COLS = ['ID', 'Prénom(s)', 'Genre', 'Arabe', 'Arabe (Tesseract)', 'Translittération', 'Équivalent',
        'Signification', 'Thème', 'Sous-thème', 'Page (imprimée)', 'Page (PDF)', 'À vérifier']


def export_results(rows: list[dict], out_dir: str):
    import pandas as pd
    for i, r in enumerate(rows, 1):
        r['ID'] = i
        r['Page (imprimée)'] = r['Page (PDF)'] - PAGE_OFFSET
    df = pd.DataFrame(rows, columns=COLS) if rows else pd.DataFrame(columns=COLS)

    paths = {k: os.path.join(out_dir, f'prenoms_arabes.{k}') for k in ('csv', 'json', 'xlsx')}
    df.to_csv(paths['csv'], index=False, encoding='utf-8-sig')
    with open(paths['json'], 'w', encoding='utf-8') as f:
        json.dump(df.to_dict('records'), f, ensure_ascii=False, indent=2)

    def alerting(s):
        return any(f and f not in INFO_FLAGS for f in str(s).split('; '))

    alert = df['À vérifier'].apply(alerting) if len(df) else df['À vérifier']
    try:
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        with pd.ExcelWriter(paths['xlsx'], engine='openpyxl') as xw:
            df.to_excel(xw, index=False, sheet_name='Prénoms arabes')
            df[alert].to_excel(xw, index=False, sheet_name='À vérifier')
            widths = {'ID': 6, 'Prénom(s)': 26, 'Genre': 10, 'Arabe': 18, 'Arabe (Tesseract)': 18,
                      'Translittération': 20, 'Équivalent': 14, 'Signification': 60, 'Thème': 24,
                      'Sous-thème': 34, 'Page (imprimée)': 10, 'Page (PDF)': 9, 'À vérifier': 30}
            for ws in xw.book.worksheets:
                for cell in ws[1]:
                    cell.fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
                    cell.font = Font(bold=True, color='FFFFFF')
                for i, col in enumerate(COLS, 1):
                    ws.column_dimensions[get_column_letter(i)].width = widths.get(col, 15)
                for row in ws.iter_rows(min_row=2):
                    for cell in row:
                        cell.alignment = Alignment(vertical='top', wrap_text=(cell.column_letter in ('H', 'M')))
                ws.freeze_panes = 'A2'
                ws.auto_filter.ref = ws.dimensions
            yellow = PatternFill(start_color='FFF2CC', end_color='FFF2CC', fill_type='solid')
            main = xw.book['Prénoms arabes']
            for idx, flag in enumerate(alert.tolist(), start=2):
                if flag:
                    for cell in main[idx]:
                        cell.fill = yellow
    except ImportError:
        log("(openpyxl absent : .xlsx non généré ; CSV et JSON créés)")
    return df, paths


# ============================================================================
# 9) ORCHESTRATION
# ============================================================================

@dataclass
class Config:
    pdf: str = ''
    sortie: str = 'sortie'
    workers: int = 0
    source: str = 'auto'      # auto / natif : image d'origine du scan ; raster : rasterisation à --dpi
    dpi: int = 300
    arabe: str = 'mots'     # bandes / mots / non
    latin: bool = True
    gpu: str = 'auto'         # auto / oui / non
    batch: int = 32
    decodeur: str = 'greedy'  # greedy / beamsearch
    refaire: bool = False
    tess_cmd: str = ''
    poppler: str = ''


def auto_workers() -> int:
    """~ nombre de cœurs physiques - 1 (on suppose 2 threads/cœur), plafonné à 12 :
    au-delà, un portable throttle et le gain est faible."""
    return max(1, min(12, (os.cpu_count() or 2) // 2 - 1))


def parse_pages(spec: str) -> list[int]:
    pages: set[int] = set()
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-', 1)
            pages.update(range(int(a), int(b) + 1))
        elif part:
            pages.add(int(part))
    return sorted(pages)


def make_engine(cfg: Config):
    try:
        import easyocr  # noqa: F401
    except ImportError:
        log("EasyOCR absent -> Tesseract seul (pip install easyocr pour activer les relectures).")
        return None
    use_gpu, msg = detect_gpu(cfg.gpu)
    log(msg)
    if cfg.gpu == 'oui' and not use_gpu:
        raise SystemExit("--gpu oui demandé mais le GPU n'est pas utilisable (voir ci-dessus).")
    log("Chargement d'EasyOCR (téléchargement des modèles ~300 Mo au tout premier lancement)...")
    return RefineEngine(cfg, use_gpu)


def run_pipeline(cfg: Config, pages: list[int]):
    out = cfg.sortie
    os.makedirs(out, exist_ok=True)
    src = 'natif' if cfg.source != 'raster' else f'raster{cfg.dpi}'
    img_dir = os.path.join(out, f'pages_{src}')
    ocr_dir = os.path.join(out, 'cache', f'ocr_{src}')
    refine_on = cfg.arabe != 'non' or cfg.latin
    raf_dir = os.path.join(out, 'cache', f"raf_{src}_{cfg.arabe}-{cfg.decodeur}-{'fr' if cfg.latin else 'nofr'}")
    for d in (img_dir, ocr_dir, raf_dir):
        os.makedirs(d, exist_ok=True)
    ocr_path = lambda p: os.path.join(ocr_dir, f'p{p:04d}.pkl')  # noqa: E731
    raf_path = lambda p: os.path.join(raf_dir, f'p{p:04d}.pkl')  # noqa: E731
    if cfg.refaire:
        for p in pages:
            for f in (ocr_path(p), raf_path(p)):
                if os.path.exists(f):
                    os.remove(f)

    tess_cmd = setup_tesseract(cfg.tess_cmd)
    todo = [p for p in pages if not os.path.exists(ocr_path(p))]
    engine = None
    if refine_on and any(not os.path.exists(raf_path(p)) for p in pages):
        engine = make_engine(cfg)
    if engine is None:
        refine_on = False

    failed: list[tuple[int, str]] = []
    refined: set[int] = set()
    n_ocr = n_raf = 0
    t_run = time.time()
    wcfg = dict(pdf=cfg.pdf, img_dir=img_dir, ocr_dir=ocr_dir, source=cfg.source, dpi=cfg.dpi,
                poppler=cfg.poppler)

    def do_refine(p: int) -> None:
        nonlocal n_raf
        if not refine_on or p in refined or os.path.exists(raf_path(p)):
            refined.add(p)
            return
        try:
            d = _load(ocr_path(p))
            with Image.open(d['img']) as im:
                im = im.convert('RGB')
                rgb = np.asarray(im)
                gray = np.asarray(im.convert('L'))
            _atomic_dump({'lines': engine.refine(d['lines'], gray, rgb, d['lh'])}, raf_path(p))
            n_raf += 1
        except Exception as e:
            failed.append((p, f"relecture : {type(e).__name__}: {e}"))
            log(f"  x page {p} : relecture impossible ({type(e).__name__}: {str(e)[:100]})")
        refined.add(p)

    pool = None
    try:
        it = None
        if todo:
            nw = max(1, min(cfg.workers or auto_workers(), len(todo)))
            log(f"OCR Tesseract : {len(todo)} page(s) sur {nw} processus (image {src})")
            pool = mp.get_context('spawn').Pool(nw, initializer=_init_worker, initargs=(tess_cmd,))
            it = pool.imap_unordered(_worker_ocr, [(p, wcfg) for p in todo], chunksize=1)
        for p in pages:  # pages déjà en cache OCR : relues pendant que les workers tournent
            if os.path.exists(ocr_path(p)):
                do_refine(p)
        if it is not None:
            for r in it:
                n_ocr += 1
                if r.get('error'):
                    failed.append((r['page'], r['error']))
                    log(f"  x page {r['page']} : {r['error'][:110]}")
                else:
                    do_refine(r['page'])
                if n_ocr % 5 == 0 or n_ocr == len(todo):
                    el = time.time() - t_run
                    rate = n_ocr / el
                    log(f"  {n_ocr}/{len(todo)} pages | {rate * 60:.1f} pages/min | reste ~ "
                        f"{(len(todo) - n_ocr) / rate / 60:.0f} min")
    finally:
        if pool is not None:
            pool.terminate()
            pool.join()

    state = {'entries': [], 'pending': [], 'open': []}
    unrefined = 0
    for p in pages:
        src_file = raf_path(p) if (refine_on and os.path.exists(raf_path(p))) else ocr_path(p)
        if not os.path.exists(src_file):
            continue
        unrefined += int(refine_on and src_file == ocr_path(p))
        ch, sub = lookup_theme(p)
        parse_page_lines((_load(src_file)['lines']), p, ch, sub, state)
    rows = finalize_entries(state['entries'])
    info = {'pages': len(pages), 'ocr': n_ocr, 'relues': n_raf, 'echecs': failed, 'non_relues': unrefined,
            'secondes': time.time() - t_run}
    return rows, info


def summarize(rows: list[dict], info: dict) -> None:
    n = max(1, len(rows))
    with_ar = sum(1 for r in rows if r['Arabe'])
    alerts = sum(1 for r in rows if any(f and f not in INFO_FLAGS for f in r['À vérifier'].split('; ')))
    log(f"{len(rows)} entrées | arabe présent : {with_ar} ({100 * with_ar / n:.0f} %) | "
        f"à vérifier : {alerts} ({100 * alerts / n:.0f} %)")
    if info['echecs']:
        log(f"{len(info['echecs'])} page(s) en échec (relance la même commande pour réessayer) : "
            + ', '.join(str(p) for p, _ in info['echecs'][:15]))
    if info['non_relues']:
        log(f"{info['non_relues']} page(s) sans relecture EasyOCR (Tesseract seul pour celles-là).")


# ============================================================================
# 10) DIAGNOSTIC, AUTOTEST, LIGNE DE COMMANDE
# ============================================================================

def diagnostic(cfg: Config) -> None:
    print(f"Python {sys.version.split()[0]} sur {sys.platform}")
    print(f"CPU logiques : {os.cpu_count()} -> workers Tesseract conseillés : {auto_workers()} (--workers pour changer)")
    try:
        import psutil
        print(f"RAM : {psutil.virtual_memory().total / 2 ** 30:.0f} Go")
    except ImportError:
        pass
    cmd = find_tesseract(cfg.tess_cmd)
    if cmd:
        try:
            pytesseract.pytesseract.tesseract_cmd = cmd
            langs = pytesseract.get_languages(config='')
            ok = all(lang in langs for lang in ('fra', 'ara'))
            print(f"Tesseract {pytesseract.get_tesseract_version()} : {cmd} | fra+ara : {'OK' if ok else 'MANQUE'}")
        except Exception as e:
            print(f"Tesseract trouvé ({cmd}) mais inutilisable : {e}")
    else:
        print("Tesseract : INTROUVABLE (--tesseract CHEMIN, ou ajoute-le au PATH)")
    try:
        import fitz  # noqa: F401
        print("Extraction d'images : PyMuPDF OK (aucun Poppler nécessaire)")
    except ImportError:
        if _which('pdfimages', cfg.poppler):
            print("Extraction d'images : Poppler OK (conseil : pip install pymupdf, plus simple sous Windows)")
        else:
            print("Extraction d'images : AUCUN outil -> pip install pymupdf (ou installe Poppler)")
    try:
        import easyocr  # noqa: F401
        print("EasyOCR : OK")
        print(detect_gpu('auto')[1])
    except ImportError:
        print("EasyOCR : absent (pip install easyocr) -> Tesseract seul")


def autotest(cfg: Config) -> None:
    import verite_terrain as vt
    if not cfg.pdf:
        raise SystemExit("--autotest a besoin du PDF : extraire_prenoms.py livre.pdf --autotest")
    cfg.sortie = cfg.sortie if cfg.sortie != 'sortie' else 'sortie_autotest'
    t0 = time.time()
    rows, info = run_pipeline(cfg, list(vt.PAGES))
    print("\nRÉSULTAT AUTOTEST (pages", vt.PAGES, f") en {time.time() - t0:.0f}s :")
    print(vt.score(rows))
    summarize(rows, info)
    print("Repère (v2 sur CPU, image native, arabe en bandes + relecture française) : "
          "sens ~35/37, vedettes 35/35, arabe exact >= 12/22. Très en dessous : lance --diagnostic.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extrait « Le Livre des prénoms arabes » (PDF scanné) vers CSV / JSON / Excel.")
    ap.add_argument('pdf', nargs='?', default='', help="chemin du PDF")
    ap.add_argument('--debut', type=int, default=DICT_START, help=f"première page PDF (défaut {DICT_START})")
    ap.add_argument('--fin', type=int, default=DICT_END, help=f"dernière page PDF (défaut {DICT_END})")
    ap.add_argument('--pages', default='', help="liste explicite, ex. 55,56,100-104 (remplace --debut/--fin)")
    ap.add_argument('--sortie', '--dossier-sortie', dest='sortie', default='sortie', help="dossier de sortie")
    ap.add_argument('--workers', type=int, default=0, help="processus Tesseract en parallèle (0 = auto)")
    ap.add_argument('--source', choices=('auto', 'natif', 'raster'), default='auto',
                    help="image d'entrée : d'origine du scan (rapide, défaut) ou rasterisée à --dpi")
    ap.add_argument('--dpi', type=int, default=300, help="DPI si --source raster")
    ap.add_argument('--arabe', choices=('bandes', 'mots', 'non'), default='mots',
                    help="relecture de l'arabe par EasyOCR : mots isolés (défaut, validé) ou bandes de lignes (expérimental)")
    ap.add_argument('--sans-latin', action='store_true', help="pas de relecture EasyOCR des mots latins douteux")
    ap.add_argument('--sans-easyocr', action='store_true', help="Tesseract seul (le plus rapide, le moins précis)")
    ap.add_argument('--gpu', choices=('auto', 'oui', 'non'), default='auto', help="GPU pour EasyOCR (défaut auto)")
    ap.add_argument('--batch', type=int, default=32, help="taille de lot EasyOCR (baisse-la si mémoire GPU saturée)")
    ap.add_argument('--decodeur', choices=('greedy', 'beamsearch'), default='greedy',
                    help="décodeur EasyOCR : beamsearch = un peu plus précis, un peu plus lent")
    ap.add_argument('--refaire', action='store_true', help="ignore le cache et recalcule les pages demandées")
    ap.add_argument('--reprendre', action='store_true', help="(inutile : la reprise est automatique)")
    ap.add_argument('--tesseract', default='', help="chemin de tesseract(.exe) si absent du PATH")
    ap.add_argument('--poppler', default='', help="dossier bin de Poppler si absent du PATH (sans PyMuPDF)")
    ap.add_argument('--diagnostic', action='store_true', help="vérifie l'installation (Tesseract, GPU...) et quitte")
    ap.add_argument('--autotest', action='store_true', help="traite 4 pages et compare à la vérité terrain")
    a = ap.parse_args()

    cfg = Config(pdf=a.pdf, sortie=a.sortie, workers=a.workers, source=a.source, dpi=a.dpi,
                 arabe='non' if a.sans_easyocr else a.arabe, latin=not (a.sans_latin or a.sans_easyocr),
                 gpu=a.gpu, batch=a.batch, decodeur=a.decodeur, refaire=a.refaire,
                 tess_cmd=a.tesseract, poppler=a.poppler)
    if a.diagnostic:
        return diagnostic(cfg)
    if a.autotest:
        return autotest(cfg)
    if not cfg.pdf or not os.path.exists(cfg.pdf):
        raise SystemExit("Donne le chemin du PDF : python extraire_prenoms.py livre.pdf  (ou --diagnostic)")

    pages = parse_pages(a.pages) if a.pages else list(range(a.debut, a.fin + 1))
    log(f"{len(pages)} page(s) | Tesseract x{cfg.workers or auto_workers()} | arabe : {cfg.arabe} | "
        f"français : {'oui' if cfg.latin else 'non'} | gpu : {cfg.gpu}")
    rows, info = run_pipeline(cfg, pages)
    df, paths = export_results(rows, cfg.sortie)
    summarize(rows, info)
    log("Fichiers : " + ' | '.join(paths.values()))


if __name__ == '__main__':
    mp.freeze_support()
    main()
