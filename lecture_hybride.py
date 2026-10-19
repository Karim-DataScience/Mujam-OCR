# -*- coding: utf-8 -*-
"""Lecture « hybride » d'une page du dictionnaire : chaque zone est confiée au bon outil.

  mise_en_page.py   structure, genre (couleur), zones                      traitement d'image classique, CPU, ~0,6 s
  Tesseract (fra)   noms français, définitions (césures, confiance)        CPU, quelques secondes
  modèle de vision  arabe voyellé + « tête » des définitions (translittération avec ses points et accents)
                    -> UNE petite feuille de recadrages numérotés par page (≈ 200 jetons de sortie au lieu de ≈ 2 000)

Le moteur (extraire_prenoms_llm.py) fait l'appel au modèle ; ce module prépare la feuille et fusionne les réponses.

Ce qu'on sait de Tesseract (fra, modèle « best ») sur ce livre : excellent sur le français courant, mais il perd les
signes de la translittération (â î û, ḥ ṣ ḍ ṭ ẓ, capitales accentuées Â È) -> la « tête » de chaque définition (avant « : »)
est relue par le modèle de vision sur un recadrage minuscule. « Il » est lu « 11 » (corrigé).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

import mise_en_page as M

CHEMINS_WINDOWS = (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")
REGLAGES = {"cmd": "", "tessdata": "", "langue": "fra"}


# ---------------------------------------------------------------- Tesseract

def configurer(cmd: str = "", tessdata: str = "", langue: str = "fra") -> None:
    REGLAGES.update(cmd=cmd or "", tessdata=tessdata or "", langue=langue or "fra")


def disponible() -> tuple[bool, str]:
    """(ok, message) : pytesseract installé, exécutable trouvé, langue disponible."""
    try:
        import pytesseract
    except ImportError:
        return False, "le paquet pytesseract est absent (pip install pytesseract)"
    cmd = REGLAGES["cmd"] or shutil.which("tesseract") or next((c for c in CHEMINS_WINDOWS if os.path.exists(c)), "")
    if not cmd:
        return False, "Tesseract est introuvable : installe-le (Windows : installateur UB Mannheim, avec la langue française) ou donne --tesseract"
    pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        langues = pytesseract.get_languages(config=f"--tessdata-dir {REGLAGES['tessdata']}" if REGLAGES["tessdata"] else "")
    except Exception as e:                                       # noqa: BLE001
        return False, f"Tesseract ne répond pas ({e})"
    if REGLAGES["langue"] not in langues:
        return False, f"la langue « {REGLAGES['langue']} » de Tesseract est absente (langues vues : {', '.join(langues)})"
    return True, f"Tesseract {pytesseract.get_tesseract_version()} ({cmd}), langue {REGLAGES['langue']}"


def _tess_data(img: Image.Image, psm: int) -> dict:
    import pytesseract
    cfg = f"--psm {psm}" + (f" --tessdata-dir {REGLAGES['tessdata']}" if REGLAGES["tessdata"] else "")
    return pytesseract.image_to_data(img, lang=REGLAGES["langue"], config=cfg, output_type=pytesseract.Output.DICT)


def _recadrer(gris: Image.Image, x0: int, y0: int, x1: int, y1: int, marge: int) -> Image.Image:
    W, H = gris.size
    return gris.crop((max(0, x0 - marge), max(0, y0 - marge), min(W, x1 + marge), min(H, y1 + marge)))


def lire_nom(gris: Image.Image, x0: int, y0: int, x1: int, y1: int) -> tuple[str, float]:
    d = _tess_data(_recadrer(gris, x0, y0, x1, y1, 10), 7)
    mots = [(t, float(c)) for t, c in zip(d["text"], d["conf"]) if t.strip() and float(c) >= 0]
    texte = re.sub(r"\s+", " ", " ".join(t for t, _ in mots)).strip(" *.,;:|_")
    return texte, (sum(c for _, c in mots) / len(mots) if mots else 0.0)


@dataclass
class LigneOCR:
    texte: str
    conf: float
    mots: list = field(default_factory=list)            # [(texte, conf, (x0, y0, x1, y1))] en coordonnées de la page


def lire_definition(gris: Image.Image, x0: int, y0: int, x1: int, y1: int) -> list[LigneOCR]:
    ox, oy = max(0, x0 - 8), max(0, y0 - 8)
    d = _tess_data(_recadrer(gris, x0, y0, x1, y1, 8), 6)
    lignes: dict = {}
    for i, t in enumerate(d["text"]):
        if not t.strip() or float(d["conf"][i]) < 0:
            continue
        box = (ox + d["left"][i], oy + d["top"][i], ox + d["left"][i] + d["width"][i], oy + d["top"][i] + d["height"][i])
        lignes.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append((t, float(d["conf"][i]), box))
    return [LigneOCR(" ".join(t for t, _, _ in m), sum(c for _, c, _ in m) / len(m), m) for _, m in sorted(lignes.items())]


def recoller(lignes: list[LigneOCR]) -> str:
    """Joint les lignes d'un paragraphe : « islami- » + « que » -> « islamique » ; « al- » + « Qâdir » (majuscule) garde son trait."""
    texte = ""
    for l in lignes:
        t = l.texte.strip()
        if not texte:
            texte = t
        elif re.search(r"[A-Za-zÀ-ÿ]-$", texte) and t[:1].islower():
            texte = texte[:-1] + t
        else:
            texte += " " + t
    return re.sub(r"^[*•]\s*", "", re.sub(r"\s+", " ", texte).strip())


def corriger_ocr_fr(s: str) -> str:
    """Confusions classiques de l'OCR sur ce texte : « Il » lu « 11 », bruit de fin de paragraphe (« . y ) »)."""
    s = re.sub(r"\b[1I]{2}(?=\s+[a-zéèêàâîôûç])", "Il", s)
    s = re.sub(r"(?<=[.!?])\s+[yv»]\s*\)?\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- lecture d'une page

@dataclass
class BlocLu:
    genre: str
    orthographes: list
    arabe: tuple | None                  # (x0, y0, x1, y1) à lire par le modèle de vision
    confiance_noms: float = 0.0


@dataclass
class EntreeLue:
    blocs: list
    description: str = ""
    confiance: float = 0.0
    tete_box: tuple | None = None        # zone de la « tête » (translittération + équivalent) à relire par le modèle de vision
    tete_texte: str = ""
    suite_page_precedente: bool = False
    description_incomplete: bool = False


@dataclass
class PageLue:
    entrees: list
    problemes: list
    page_texte: bool
    textes_libres: int = 0
    mise: object = None


MOTIF_TETE = re.compile(r"^[‘’'ʿʾA-Za-zÀ-ÿḥṣḍṭẓḤṢḌṬẒ\-\s/.]{2,70}?(\([^)]{1,70}\))?\s?:\s")


def trouver_tete(lignes: list[LigneOCR]):
    """Mots de la première ligne jusqu'aux deux-points : (texte, boîte englobante) ou (« », None)."""
    if not lignes:
        return "", None
    mots = lignes[0].mots
    for k, (t, _, _) in enumerate(mots):
        if ":" in t:
            tete = mots[:k] + ([(t.rstrip(":"), 0, mots[k][2])] if t.rstrip(":") else [])
            if not tete:
                return "", None
            box = (min(m[2][0] for m in tete), min(m[2][1] for m in tete), max(m[2][2] for m in tete), max(m[2][3] for m in tete))
            return " ".join(m[0] for m in tete), box
    return "", None


def lire_page(net: Image.Image) -> PageLue:
    mise = M.analyser(net)
    gris = net.convert("L")
    entrees: list[EntreeLue] = []
    problemes = list(mise.problemes)
    for e in mise.entrees:
        blocs = []
        for b in e.blocs:
            noms, confs = [], []
            for (x0, x1, y0, y1) in [(b.latin[0], b.latin[1], b.y[0], b.y[1])] + [(o[0], o[1], o[2], o[3]) for o in b.orthographes]:
                t, c = lire_nom(gris, x0, y0, x1, y1)
                if t:
                    noms.append(t)
                    confs.append(c)
            blocs.append(BlocLu(b.genre, noms, (b.arabe[0], b.y[0], b.arabe[1], b.y[1]) if b.arabe else None,
                                sum(confs) / len(confs) if confs else 0.0))
        lu = EntreeLue(blocs, suite_page_precedente=e.suite_page_precedente)
        if e.definition:
            lignes = lire_definition(gris, *e.definition)
            lu.description = corriger_ocr_fr(recoller(lignes))
            lu.confiance = sum(l.conf for l in lignes) / len(lignes) if lignes else 0.0
            lu.tete_texte, lu.tete_box = trouver_tete(lignes) if not e.suite_page_precedente else ("", None)
        else:
            lu.description_incomplete = True
        entrees.append(lu)
    for i, e in enumerate(entrees, 1):
        if e.description and not e.suite_page_precedente and not MOTIF_TETE.match(e.description):
            problemes.append(f"entrée {i} : la définition ne commence pas par « Tête : » (« {e.description[:40]} »)")
        for b in e.blocs:
            for n in b.orthographes:
                if len(n.split()) > 4 or re.search(r"[:;()0-9]", n):
                    problemes.append(f"entrée {i} : « {n} » ne ressemble pas à un prénom")
            if not b.orthographes:
                problemes.append(f"entrée {i} : nom illisible")
    return PageLue(entrees, problemes, mise.page_texte, len(mise.textes_libres), mise)


# ---------------------------------------------------------------- feuille de recadrages pour le modèle de vision

PROMPT_FEUILLE = """Tu lis des extraits d'un livre de prénoms arabes. Chaque ligne de l'image porte un repère à gauche (A1, A2… = un mot arabe ; T1, T2… = un texte latin) suivi de l'extrait.
- Repère A… : recopie le mot arabe avec TOUTES ses voyelles et tous ses signes visibles (fatha, damma, kasra, shadda, soukoun, tanwin, madda, hamza), sans les barres d'allongement (tatwil « ـ »). Ne complète rien : un signe absent de l'image reste absent. Illisible : "".
- Repère T… : recopie exactement le texte latin avec ses accents (â î û ê é è), ses points souscrits (ḥ ṣ ḍ ṭ ẓ), ses apostrophes (ʿ ʾ ’ ‘), ses tirets et ses parenthèses. Une lecture automatique approximative est donnée : corrige-la d'après l'image.
Réponds UNIQUEMENT par un objet JSON de la forme {"A1": "…", "T1": "…"}, sans texte autour."""


def _police(taille: int):
    try:
        return ImageFont.load_default(size=taille)
    except TypeError:                                            # Pillow < 10.1
        return ImageFont.load_default()


def construire_feuille(page: PageLue, gris: Image.Image):
    """-> (image, texte pour l'utilisateur, {repère: (kind, entrée, bloc)}) ; (None, "", {}) s'il n'y a rien à relire."""
    lignes, reperes = [], {}
    n_a = n_t = 0
    for i, e in enumerate(page.entrees):
        for j, b in enumerate(e.blocs):
            if b.arabe:
                n_a += 1
                reperes[f"A{n_a}"] = ("A", i, j)
                lignes.append((f"A{n_a}", _recadrer(gris, *b.arabe, 12), 150, f"prénom « {' / '.join(b.orthographes)} »"))
        if e.tete_box:
            n_t += 1
            reperes[f"T{n_t}"] = ("T", i, 0)
            lignes.append((f"T{n_t}", _recadrer(gris, *e.tete_box, 8), 64, f"lecture automatique approximative « {e.tete_texte} »"))
    if not lignes:
        return None, "", {}
    police = _police(34)
    rows = []
    for rep, crop, h_cible, _ in lignes:
        w = min(800, max(1, round(crop.width * h_cible / max(1, crop.height))))
        rows.append((rep, crop.resize((w, max(1, round(crop.height * w / crop.width))), Image.LANCZOS)))
    largeur = 110 + max(c.width for _, c in rows) + 20
    hauteur = sum(c.height + 26 for _, c in rows) + 10
    feuille = Image.new("L", (largeur, hauteur), 255)
    d = ImageDraw.Draw(feuille)
    y = 8
    for rep, c in rows:
        d.text((10, y + max(0, (c.height - 34) // 2)), rep, fill=0, font=police)
        feuille.paste(c, (110, y))
        y += c.height + 12
        d.line([(0, y), (largeur, y)], fill=170, width=1)
        y += 14
    texte = (f"{len(lignes)} extraits à lire.\n" + "\n".join(f"{rep} : {indice}" for rep, _, _, indice in lignes)
             + "\nRéponds par le JSON demandé.")
    return feuille.convert("RGB"), texte, reperes


def extraire_feuille_json(contenu: str, reflexion: str = "") -> dict | None:
    """Dernier objet JSON {« A1 »: …, « T1 »: …} de la réponse (après un éventuel bloc de réflexion)."""
    texte = re.split(r"</think>", contenu, flags=re.I)[-1] if re.search(r"</think>", contenu, re.I) else contenu
    for source in (texte, contenu, reflexion):
        if not source:
            continue
        trouves = []
        dec, i = json.JSONDecoder(), 0
        while i < len(source):
            if source[i] == "{":
                try:
                    obj, fin = dec.raw_decode(source, i)
                    if isinstance(obj, dict) and any(re.fullmatch(r"[AT]\d+", str(k)) for k in obj):
                        trouves.append(obj)
                    i = fin
                    continue
                except ValueError:
                    pass
            i += 1
        if trouves:
            return {str(k): str(v) for k, v in trouves[-1].items() if re.fullmatch(r"[AT]\d+", str(k))}
        paires = re.findall(r'"([AT]\d+)"\s*:\s*"((?:[^"\\]|\\.)*)"', source)          # réponse tronquée : on récupère les paires complètes
        if paires:
            return {k: v for k, v in paires}
    return None


# ---------------------------------------------------------------- fusion Tesseract + modèle de vision

def sans_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()


def fusionner_tete(description: str, tete_vision: str) -> tuple[str, str]:
    """Remplace la tête (avant « : ») lue par Tesseract par celle du modèle de vision si les lettres de base concordent
    (le modèle ne fait que restituer accents et points souscrits que Tesseract perd). -> (description, avertissement)."""
    m = re.match(r"^(.*?)\s*:\s+(.*)$", description)
    tete_vision = re.sub(r"\s+", " ", (tete_vision or "")).strip(" :")
    if not m or not tete_vision:
        return description, ""
    a, b = sans_accents(m.group(1)), sans_accents(tete_vision)
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.8:
        return f"{tete_vision} : {m.group(2)}", ""
    return description, f"tête divergente (Tesseract « {m.group(1)} » ≠ modèle « {tete_vision} ») : lecture Tesseract conservée"


def propager_diacritiques(description: str) -> str:
    """Dans le corps de la définition, « Adam » devient « Âdam » quand la tête (déjà corrigée) est « Âdam » : même nom, même graphie."""
    m = re.match(r"^([^(:]+?)\s*(\([^)]*\))?\s*:\s+(.*)$", description)
    if not m:
        return description
    cles = {sans_accents(t): t for t in re.findall(r"[‘’'ʿʾA-Za-zÀ-ÿḥṣḍṭẓḤṢḌṬẒ\-]+", m.group(1)) if t != sans_accents(t).capitalize() and len(t) > 2}
    if not cles:
        return description

    def remplacer(mot):
        t = mot.group(0)
        return cles.get(sans_accents(t), t) if t[:1].isupper() else t
    reste = re.sub(r"[A-Za-zÀ-ÿḥṣḍṭẓḤṢḌṬẒ]{3,}", remplacer, m.group(3))
    return f"{m.group(1)}{' ' + m.group(2) if m.group(2) else ''} : {reste}"


def appliquer_feuille(page: PageLue, reperes: dict, reponse: dict) -> tuple[list[dict], list[str]]:
    """Entrées au format du moteur ({prenoms, description, description_incomplete}) + avertissements."""
    avert: list[str] = []
    arabes, tetes = {}, {}
    for rep, (kind, i, j) in reperes.items():
        v = (reponse or {}).get(rep, "")
        (arabes if kind == "A" else tetes)[(i, j) if kind == "A" else i] = v
        if not v:
            avert.append(f"{rep} : pas de lecture du modèle de vision")
    sortie = []
    for i, e in enumerate(page.entrees):
        desc = e.description
        if e.tete_box and e.tete_texte:
            desc, w = fusionner_tete(desc, tetes.get(i, ""))
            if w:
                avert.append(f"entrée {i + 1} : {w}")
            desc = propager_diacritiques(desc)
        if e.description and e.confiance and e.confiance < 70:
            avert.append(f"entrée {i + 1} : définition peu sûre (confiance Tesseract {e.confiance:.0f} %)")
        sortie.append({"prenoms": [{"genre": b.genre, "orthographes": b.orthographes, "arabe": arabes.get((i, j), "")}
                                   for j, b in enumerate(e.blocs)],
                       "description": desc, "description_incomplete": bool(e.description_incomplete)})
    return sortie, avert
