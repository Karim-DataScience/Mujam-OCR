#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extraire_prenoms_llm.py  (v3 : lecture des pages par un modèle de vision, via LM Studio)
========================================================================================
Extrait « Le Livre des prénoms arabes » (PDF scanné, sans couche de texte) vers CSV / Excel / JSON,
en envoyant chaque page à un modèle de vision servi par LM Studio (ex. prism-ml/bonsai-27b).

PIPELINE
  1. Image native de la page (PyMuPDF) -> nettoyage (efface le texte fantôme du verso, garde le rouge).
  2. Une requête par page (ou par demi-page avec --decoupe 2) : le modèle renvoie du JSON.
  3. Cache par page : relancer la même commande ne refait rien de déjà calculé.
  4. Assemblage : suites de définitions d'une page à l'autre, puis UNE LIGNE PAR PRÉNOM
     (« Youssouf/Youssef » devient deux lignes), colonnes d'équivalents, drapeaux « à vérifier ».
  5. Export : CSV (comme ton exemple), JSON, Excel (+ feuille « À vérifier » et feuille « Pages »).

USAGE RAPIDE
    python extraire_prenoms_llm.py --diagnostic                       # LM Studio + vision OK ?
    python extraire_prenoms_llm.py livre.pdf --autotest               # 4 pages avec vérité terrain
    python extraire_prenoms_llm.py livre.pdf                          # tout le dictionnaire (pages 53-338)
    python extraire_prenoms_llm.py livre.pdf --pages 55,150 --paralleles 2

Le détail est dans README.md.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import socket
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


# ============================================================================
# 1) STRUCTURE DU LIVRE (identique à la v2)
#    Table « page imprimée -> chapitre / sous-thème », recopiée de la table des matières.
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


# ============================================================================
# 2) LE PROMPT
# ============================================================================

PROMPT = """Tu lis une page scannée du « Livre des prénoms arabes » (Y. et N. Geoffroy), un dictionnaire de prénoms. Extrais TOUTES les entrées de prénoms visibles sur cette page, dans l'ordre de lecture (de haut en bas).

MISE EN PAGE D'UNE ENTRÉE
1. Un ou deux « blocs de prénom » :
   - colonne de gauche, texte NOIR en gras = prénom MASCULIN ; colonne de droite, texte ROUGE en gras = prénom FÉMININ ;
   - un bloc = 1 à 4 orthographes françaises du même prénom, empilées ligne par ligne (ex. « Adam » puis « Adame ») ;
   - à droite de la première ligne du bloc : le même prénom en écriture arabe (calligraphie), avec ou sans voyelles.
2. Ensuite UN paragraphe de définition en caractères romains (les sens en italique), commun aux deux blocs s'il y en a deux (dans ce cas : une seule entrée). Il commence en général par la translittération savante, éventuellement suivie d'un équivalent entre parenthèses, puis « : » (ex. « Âdam (Adam) : premier homme… », « Nâḍil / Nâḍila : qui l'emporte… »). Il peut commencer par un astérisque « * » : ne le recopie pas.

RÈGLES
- Recopie exactement ce qui est imprimé : orthographes françaises (accents, apostrophes, tirets, majuscules) et texte complet de la définition, sans le résumer, le traduire ni le corriger. Recolle les mots coupés en fin de ligne (« islami- » + « que » -> « islamique »).
- Arabe : recopie les lettres ET toutes les voyelles brèves visibles (fatha, damma, kasra, shadda, soukoun, tanwin) telles qu'imprimées, sans jamais en ajouter. Ne recopie PAS les barres d'allongement (tatwil « ـ »). Si l'arabe est absent ou illisible : chaîne vide "".
- genre : "MASCULIN" (noir, colonne gauche) ou "FEMININ" (rouge, colonne droite).
- Prénom MIXTE : s'il apparaît dans les deux colonnes (noir à gauche ET rouge à droite, définition souvent introduite par « (mixte) »), c'est UNE seule entrée avec deux blocs (un MASCULIN, un FEMININ).
- Appels de note : ne recopie pas les petits chiffres en exposant collés à la fin d'un mot (« famille¹⁹ » s'écrit « famille »). Les autres exposants s'écrivent en ligne (« 12ᵉ siècle » s'écrit « 12e siècle »).
- IGNORE : le titre courant et le numéro de page en haut de page (« LE LIVRE DES PRÉNOMS ARABES » ou le titre d'une section, ex. « SAINTS PERSONNAGES CÉLÈBRES »), les bandeaux de section rouges (« A. LES PROPHÈTES… »), la légende soulignée « MASCULIN / FEMININ », les paragraphes d'introduction, les notes de bas de page (petit texte sous un filet, en bas de page), et le texte pâle qui transparaît depuis le verso de la page.
- Ne fusionne jamais deux entrées différentes ; n'invente aucune entrée ; n'en oublie aucune.
- Haut de page : si la page commence par la suite d'une définition commencée sur la page précédente (aucun prénom avant), crée une entrée avec "prenoms": [] et "suite_page_precedente": true.
- Bas de page : si la définition de la dernière entrée est coupée (elle continue sur la page suivante), ou si des prénoms n'ont pas encore leur définition, mets "description_incomplete": true.
- Ne recopie pas la page en entier pendant ta réflexion : repère les entrées, puis écris directement le JSON.

FORMAT DE SORTIE : réponds UNIQUEMENT par un objet JSON (aucun texte autour, aucun commentaire), de cette forme :
{"entrees": [
  {"prenoms": [
     {"genre": "MASCULIN", "orthographes": ["Youssouf", "Youssef"], "arabe": "يُوسُف"}],
   "description": "Yûsuf (Joseph) : fils de Ya’qub. La 12e sourate du Coran lui est presque entièrement consacrée et porte son nom. Signifie en hébreu : que Dieu ajoute (d’autres enfants à celui qui vient de naître).",
   "suite_page_precedente": false, "description_incomplete": false},
  {"prenoms": [
     {"genre": "MASCULIN", "orthographes": ["Karim"], "arabe": "كَرِيم"},
     {"genre": "FEMININ", "orthographes": ["Karima"], "arabe": "كَرِيمَة"}],
   "description": "Karîm / Karîma : généreux.",
   "suite_page_precedente": false, "description_incomplete": false}
]}
Si la page ne contient aucune entrée : {"entrees": []}."""

NOTE_BANDE = {
    1: "\n\nATTENTION : tu ne vois que la MOITIÉ HAUTE de la page. La dernière entrée peut être coupée en bas (mets alors \"description_incomplete\": true).",
    2: "\n\nATTENTION : tu ne vois que la MOITIÉ BASSE de la page. Le texte du haut peut être la suite d'une définition commencée plus haut (mets alors \"prenoms\": [] et \"suite_page_precedente\": true).",
}

# les deux entrées d'exemple du prompt : si elles reviennent telles quelles, le modèle a recopié l'exemple
EXEMPLE_ORTHOGRAPHES = {"youssouf", "youssef", "karim", "karima"}

SCHEMA_JSON = {
    "name": "page_prenoms",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["entrees"],
        "properties": {"entrees": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["prenoms", "description", "suite_page_precedente", "description_incomplete"],
            "properties": {
                "prenoms": {"type": "array", "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["genre", "orthographes", "arabe"],
                    "properties": {
                        "genre": {"type": "string", "enum": ["MASCULIN", "FEMININ"]},
                        "orthographes": {"type": "array", "items": {"type": "string"}},
                        "arabe": {"type": "string"}}}},
                "description": {"type": "string"},
                "suite_page_precedente": {"type": "boolean"},
                "description_incomplete": {"type": "boolean"}}}}}},
}


# ============================================================================
# 3) CONFIG + ERREURS
# ============================================================================

@dataclass
class Config:
    pdf: str = ""
    url: str = "http://localhost:1234/v1"
    modele: str = "prism-ml/bonsai-27b"
    sortie: str = "sortie_llm"
    pages: list = field(default_factory=list)
    paralleles: int = 1
    decoupe: int = 1
    nettoyage: bool = True
    max_cote: int = 0
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 20
    max_tokens: int = 16000
    timeout: int = 1800
    essais: int = 2
    json_schema: bool = False
    corps_extra: dict = field(default_factory=dict)
    prompt: str = PROMPT
    refaire: bool = False
    sauver_images: bool = False
    ancien: str = ""
    interrompu: bool = False
    fatal: str = ""


class ErreurFatale(Exception):
    """Inutile de continuer (serveur absent, modèle sans vision...) : on arrête tout le lot."""


class ErreurLLM(Exception):
    """Échec récupérable d'une page (délai dépassé, réponse inexploitable...)."""


# ============================================================================
# 4) IMAGE DE LA PAGE : native, nettoyée, éventuellement coupée en deux
# ============================================================================

_LOCAL = threading.local()


def _pymupdf():
    try:
        import pymupdf                       # nom actuel du module (PyMuPDF >= 1.24.3)
        return pymupdf
    except ImportError:
        pass
    try:
        import fitz                          # ancien nom du même module
        return fitz
    except ImportError:
        raise ErreurFatale("PyMuPDF est requis : pip install pymupdf")


def _doc(pdf: str):
    """Un objet PyMuPDF par thread (les documents ne sont pas partageables entre threads)."""
    fitz = _pymupdf()
    docs = getattr(_LOCAL, "docs", None)
    if docs is None:
        docs = _LOCAL.docs = {}
    if pdf not in docs:
        docs[pdf] = fitz.open(pdf)
    return docs[pdf]


def image_native(pdf: str, page: int) -> Image.Image:
    """Image d'origine du scan (~1800x2900 px) ; à défaut, rendu de la page à ~2000 px de large."""
    doc = _doc(pdf)
    if not 1 <= page <= len(doc):
        raise ErreurFatale(f"page {page} hors du PDF ({len(doc)} pages)")
    p = doc[page - 1]
    im = None
    imgs = p.get_images(full=True)
    if len(imgs) == 1:
        try:
            info = doc.extract_image(imgs[0][0])
            im = Image.open(io.BytesIO(info["image"]))
            im.load()
        except Exception:
            im = None
    if im is None:
        fitz = _pymupdf()
        z = 2000 / p.rect.width
        pix = p.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return im.convert("RGB")


def nettoyer_page(im: Image.Image, bas: float = 0.28, haut: float = 0.65) -> Image.Image:
    """Le papier devient blanc, l'encre reste sombre, le rouge reste rouge. Les « fantômes » de
    l'autre face de la feuille (gris clair) tombent en dessous du seuil et disparaissent."""
    a = np.asarray(im.convert("RGB"), dtype=np.float32)
    lum = a.mean(axis=2)
    clair = a[lum >= np.percentile(lum, 60)]
    if clair.size == 0:
        return im.convert("RGB")
    fond = np.maximum(np.median(clair, axis=0), 1.0)          # couleur du papier, par canal
    out = np.clip((a / fond - bas) / (haut - bas), 0.0, 1.0)
    return Image.fromarray((out * 255).astype(np.uint8))


def decouper(net: Image.Image, envoi: Image.Image, n: int) -> list[Image.Image]:
    """n=2 : coupe en deux dans la plus grande zone blanche proche du milieu (jamais à travers une ligne)."""
    if n <= 1:
        return [envoi]
    a = np.asarray(net.convert("RGB"))
    par_ligne = ((255 - a.min(axis=2)) > 60).sum(axis=1)
    h = len(par_ligne)
    lo, hi = int(h * 0.35), int(h * 0.65)
    vide = par_ligne[lo:hi] <= 2
    best_len = best_start = run = 0
    for k, v in enumerate(vide):
        run = run + 1 if v else 0
        if run > best_len:
            best_len, best_start = run, k - run + 1
    coupe = lo + best_start + best_len // 2 if best_len >= 4 else lo + int(np.argmin(par_ligne[lo:hi]))
    return [envoi.crop((0, 0, envoi.width, coupe)), envoi.crop((0, coupe, envoi.width, envoi.height))]


SEUIL_BLANC = 0.03   # % de pixels sombres en dessous duquel une page est considérée comme blanche


def preparer_image(cfg: Config, page: int, bande: int) -> tuple[bytes, float]:
    """(PNG envoyé au modèle pour (page, bande), % de pixels sombres : ~0 = page blanche)."""
    im = image_native(cfg.pdf, page)
    net = nettoyer_page(im) if (cfg.nettoyage or cfg.decoupe > 1) else im
    envoi = net if cfg.nettoyage else im
    if cfg.decoupe > 1:
        envoi = decouper(net, envoi, cfg.decoupe)[bande - 1]
    if cfg.max_cote and max(envoi.size) > cfg.max_cote:
        r = cfg.max_cote / max(envoi.size)
        envoi = envoi.resize((round(envoi.width * r), round(envoi.height * r)), Image.LANCZOS)
    encre = float((np.asarray(envoi.convert("L")) < 128).mean() * 100)
    buf = io.BytesIO()
    envoi.save(buf, "PNG", compress_level=3)
    return buf.getvalue(), encre


# ============================================================================
# 5) DIALOGUE AVEC LM STUDIO (API compatible OpenAI)
# ============================================================================

def _requete(url: str, corps: dict | None, timeout: int) -> dict:
    data = json.dumps(corps).encode("utf-8") if corps is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "Authorization": "Bearer lm-studio"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            brut = r.read()
    except urllib.error.HTTPError as e:
        texte = e.read().decode("utf-8", "replace")[:600]
        bas = texte.lower()
        parle_image = any(k in bas for k in ("image", "vision", "multimodal", "mmproj", "modality"))
        refuse = any(k in bas for k in ("not support", "unsupported", "does not accept", "not a vision", "no vision",
                                        "cannot process", "mmproj", "multimodal", "modality"))
        if e.code in (400, 422) and parle_image and refuse:
            raise ErreurFatale(
                "Le modèle chargé n'accepte pas d'image (module vision absent ?). Dans LM Studio, télécharge "
                f"aussi le fichier vision (mmproj) du modèle, ou choisis une variante « vision ». Détail : {texte}")
        if e.code == 404 or ("model" in bas and ("not found" in bas or "not loaded" in bas or "no models" in bas)):
            raise ErreurFatale(f"Modèle introuvable / non chargé dans LM Studio (HTTP {e.code}) : {texte}")
        if "context" in bas and ("length" in bas or "size" in bas or "overflow" in bas):
            raise ErreurFatale("Contexte insuffisant : recharge le modèle dans LM Studio avec un contexte d'au moins "
                               f"32 000 tokens (image + consigne + réflexion + réponse). Détail : {texte}")
        raise ErreurLLM(f"HTTP {e.code} : {texte}")
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            raise ErreurLLM(f"délai dépassé ({timeout}s)")
        raise ErreurFatale(f"Serveur LM Studio injoignable sur {url} ({e.reason}). Lance LM Studio, onglet "
                           "« Developer » -> « Start Server », et charge le modèle.")
    except (TimeoutError, socket.timeout):
        raise ErreurLLM(f"délai dépassé ({timeout}s)")
    except (ConnectionError, OSError) as e:
        raise ErreurLLM(f"connexion interrompue : {e}")
    try:
        return json.loads(brut)
    except ValueError:
        raise ErreurLLM(f"réponse non-JSON du serveur : {brut[:200]!r}")


def lister_modeles(cfg: Config) -> list[str]:
    d = _requete(cfg.url.rstrip("/") + "/models", None, 20)
    return [m.get("id", "") for m in d.get("data", []) if m.get("id")]


def resoudre_modele(cfg: Config) -> str:
    """Trouve l'identifiant réel du modèle dans LM Studio (il diffère souvent du nom affiché)."""
    ids = [i for i in lister_modeles(cfg) if "embed" not in i.lower()]
    if not ids:
        raise ErreurFatale("LM Studio ne signale aucun modèle : charge le modèle de vision, puis relance.")
    voulu = cfg.modele.strip().lower()
    if voulu:
        exact = [i for i in ids if i.lower() == voulu]
        if exact:
            return exact[0]
        cle = re.sub(r"[^a-z0-9]", "", voulu.split("/")[-1])
        flou = [i for i in ids if cle and cle in re.sub(r"[^a-z0-9]", "", i.lower())]
        if flou:
            log(f"modèle « {cfg.modele} » -> utilise « {flou[0]} »" + (f" (autres candidats : {flou[1:]})" if len(flou) > 1 else ""))
            return flou[0]
    if len(ids) == 1:
        log(f"modèle « {cfg.modele} » introuvable ; le seul modèle disponible est utilisé : « {ids[0]} »")
        return ids[0]
    raise ErreurFatale(f"Modèle « {cfg.modele} » introuvable. Disponibles : {ids}. Précise --modele.")


def appeler_llm(cfg: Config, modele: str, png: bytes, prompt: str, temperature: float, max_tokens: int | None = None) -> dict:
    b64 = base64.b64encode(png).decode("ascii")
    corps = {
        "model": modele,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": prompt}]}],
        "temperature": temperature,
        "max_tokens": max_tokens or cfg.max_tokens,
        "stream": False,
    }
    if cfg.top_p:
        corps["top_p"] = cfg.top_p
    if cfg.top_k:
        corps["top_k"] = cfg.top_k
    if cfg.json_schema:
        corps["response_format"] = {"type": "json_schema", "json_schema": SCHEMA_JSON}
    corps.update(cfg.corps_extra)
    d = _requete(cfg.url.rstrip("/") + "/chat/completions", corps, cfg.timeout)
    try:
        ch = d["choices"][0]
        msg = ch["message"]
    except (KeyError, IndexError, TypeError):
        raise ErreurLLM(f"réponse inattendue : {str(d)[:300]}")
    return {"content": msg.get("content") or "",
            "reasoning": msg.get("reasoning_content") or msg.get("reasoning") or "",
            "finish": ch.get("finish_reason") or "",
            "tokens": (d.get("usage") or {}).get("completion_tokens", 0)}


# ============================================================================
# 6) EXTRACTION DU JSON DANS LA RÉPONSE (le modèle « réfléchit » avant de répondre)
# ============================================================================

def retirer_reflexion(t: str) -> str:
    """Enlève le bloc <think>...</think> ; un bloc jamais refermé = tout est réflexion."""
    if re.search(r"</think>", t, re.I):
        return re.split(r"</think>", t, flags=re.I)[-1]
    return re.sub(r"<think>.*", "", t, flags=re.S | re.I)


def _objets_json(texte: str):
    dec = json.JSONDecoder()
    i = 0
    while i < len(texte):
        if texte[i] in "{[":
            try:
                obj, fin = dec.raw_decode(texte, i)
                yield obj
                i = fin
                continue
            except ValueError:
                pass
        i += 1


_CLES_PAGE = ("entrees", "entries", "entrées")


def _ressemble_a_page(o, texte_a_cle_page: bool) -> bool:
    """Objet « page » complet. Une entrée isolée ou une liste d'entrées n'est acceptée que si le texte ne parle
    d'aucune clé « entrees » : sinon c'est un morceau d'une réponse tronquée, et le prendre pour la page
    ferait perdre silencieusement toutes les autres entrées."""
    if isinstance(o, dict) and any(k in o for k in _CLES_PAGE):
        return True
    if texte_a_cle_page:
        return False
    if isinstance(o, dict):
        return "prenoms" in o
    return isinstance(o, list) and bool(o) and all(isinstance(x, dict) and "prenoms" in x for x in o)


def meilleur_json(texte: str):
    """Dernier objet JSON du texte qui ressemble à une page d'entrées."""
    for essai in (texte, re.sub(r",(\s*[}\]])", r"\1", texte)):      # 2e passe : virgules finales tolérées
        a_cle = bool(re.search(r'"(?:entrees|entries|entrées)"', essai))
        trouve = [o for o in _objets_json(essai) if _ressemble_a_page(o, a_cle)]
        if trouve:
            return trouve[-1]
    return None


def sauver_entrees_tronquees(texte: str):
    """Réponse coupée par max_tokens : récupère les entrées COMPLÈTES déjà écrites."""
    m = re.search(r'"entrees"\s*:\s*\[', texte)
    if not m:
        return None
    dec, i, out = json.JSONDecoder(), m.end(), []
    while True:
        while i < len(texte) and texte[i] in " \n\r\t,":
            i += 1
        if i >= len(texte) or texte[i] != "{":
            break
        try:
            obj, i = dec.raw_decode(texte, i)
        except ValueError:
            break
        out.append(obj)
    return {"entrees": out} if out else None


def extraire_page_json(rep: dict):
    """-> (objet | None, avertissements, partiel)."""
    content, reasoning, finish = rep["content"], rep["reasoning"], rep["finish"]
    propre = retirer_reflexion(content)
    if finish == "length":       # réponse coupée : ne jamais chercher dans la réflexion (elle cite l'exemple du prompt)
        candidats = [propre] if propre.strip() else []
    else:
        candidats = [c for c in (propre, content, reasoning) if c.strip()]
    for texte in candidats:
        obj = meilleur_json(texte)
        if obj is not None:
            return obj, [], False
    for texte in candidats[:1]:
        obj = sauver_entrees_tronquees(texte)
        if obj is not None:
            return obj, ["réponse tronquée : entrées complètes récupérées (augmente --max-tokens)"], True
    if finish == "length":
        return None, ["réponse coupée avant la fin (la réflexion a consommé tout le budget) : augmente --max-tokens "
                      "(et le contexte du modèle dans LM Studio)"], False
    return None, [f"aucun JSON exploitable (finish={finish or '?'}, {len(content)} car.)"], False


# ============================================================================
# 7) NORMALISATION DES CHAMPS
# ============================================================================

TATWIL_ETC = re.compile(r"[\u0640\u200c\u200d\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069]")
FORMES_PRESENTATION = re.compile(r"[\uFB50-\uFDFF\uFE70-\uFEFF]")
HARAKAT = "\u064B-\u065F\u0670"


def nettoyer_arabe(s) -> str:
    s = str(s or "")
    if FORMES_PRESENTATION.search(s):
        s = unicodedata.normalize("NFKC", s)
    s = TATWIL_ETC.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def arab_norm(s: str) -> str:
    """Lettres de base uniquement (sans voyelles, alefs unifiés) : pour comparer deux lectures."""
    s = re.sub(r"[\u064B-\u065F\u0670\u0640\u200e\u200f]", "", s)
    s = re.sub(r"[آأإٱ]", "ا", s).replace("ى", "ي")
    return re.sub(r"[^\u0621-\u064A]", "", s)


def nettoyer_prenom(s) -> str:
    s = unicodedata.normalize("NFC", str(s))
    return re.sub(r"\s+", " ", s).strip(" \t*.,;:|_")


def split_variantes(s) -> list[str]:
    return [p.strip() for p in re.split(r"\s*[/;]\s*", str(s)) if p.strip()]


def normaliser_genre(g) -> str:
    g = unicodedata.normalize("NFD", str(g or "")).encode("ascii", "ignore").decode().strip().upper()
    if g.startswith("M") or g in ("H", "HOMME"):
        return "MASCULIN"
    if g.startswith("F"):
        return "FEMININ"
    return ""


def nettoyer_description(s) -> str:
    s = unicodedata.normalize("NFC", str(s or ""))
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s)          # mot coupé en fin de ligne
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^\*+\s*", "", s)


def joindre(a: str, b: str) -> str:
    a, b = a.rstrip(), b.lstrip()
    if not a:
        return b
    if re.search(r"\w-$", a) and b[:1].islower():
        return a[:-1] + b
    return a + " " + b


_DEBUTS_PHRASE = {"signifie", "nom", "prénom", "prenom", "diminutif", "variante", "forme", "ce", "cet", "cette", "il", "elle",
                  "les", "le", "la", "l", "un", "une", "de", "du", "des", "selon", "dans", "en", "sur", "voir", "cf", "se"}
_DESC_RE = re.compile(r"^(?P<translit>[^:()\n]{1,80}?)\s*(?:\((?P<equiv>[^()]{1,80})\))?\s*:\s")


_APOS = "'’‘ʿʾʻʼ`"


def parse_description(desc: str) -> tuple[str, str]:
    """« Âdam (Adam) : premier homme… » -> ('Âdam', 'Adam') ; « Nâḍil / Nâḍila : … » -> ('Nâḍil / Nâḍila', '')."""
    m = _DESC_RE.match(desc.lstrip("* ").strip())
    if not m:
        return "", ""
    tr = m.group("translit").strip()
    tete = tr.lstrip(_APOS)                                    # « ‘Abd ash-Shahîd » : la 1re vraie lettre compte
    premier = re.split(r"[\s/]+", tete)[0].lower().strip(_APOS)
    if not tete[:1].isupper() or premier in _DEBUTS_PHRASE or len(tr.split()) > 6 or "." in tr:
        return "", ""
    return tr, (m.group("equiv") or "").strip()


def normaliser_entree(e) -> dict | None:
    if not isinstance(e, dict):
        return None
    blocs_raw = e.get("prenoms") or e.get("noms") or e.get("names") or []
    if isinstance(blocs_raw, dict):
        blocs_raw = [blocs_raw]
    blocs = []
    for b in blocs_raw if isinstance(blocs_raw, list) else []:
        if isinstance(b, str):
            b = {"orthographes": [b]}
        if not isinstance(b, dict):
            continue
        ortho = b.get("orthographes") or b.get("orthographe") or b.get("prenom") or b.get("fr") or []
        ortho = split_variantes(ortho) if isinstance(ortho, str) else [x for o in ortho for x in split_variantes(o)]
        ortho = list(dict.fromkeys(o for o in (nettoyer_prenom(x) for x in ortho) if o))
        if not ortho:
            continue
        blocs.append({"genre": normaliser_genre(b.get("genre") or b.get("gender")),
                      "orthographes": ortho,
                      "arabe": nettoyer_arabe(b.get("arabe") or b.get("arabic") or b.get("ar"))})
    desc = nettoyer_description(e.get("description") or e.get("definition") or e.get("signification") or "")
    if not blocs and not desc:
        return None
    return {"prenoms": blocs, "description": desc,
            "description_incomplete": bool(e.get("description_incomplete"))}


def normaliser_page(obj) -> list[dict]:
    if isinstance(obj, list):
        brut = obj
    elif isinstance(obj, dict):
        brut = next((obj[k] for k in ("entrees", "entries", "entrées") if k in obj), None)
        if brut is None:
            brut = [obj] if "prenoms" in obj else []
    else:
        brut = []
    return [n for n in (normaliser_entree(x) for x in brut) if n]


def est_exemple(entrees: list[dict]) -> bool:
    """Le modèle a-t-il renvoyé tel quel l'exemple du prompt ?"""
    noms = {o.lower() for e in entrees for b in e["prenoms"] for o in b["orthographes"]}
    return bool(noms) and noms <= EXEMPLE_ORTHOGRAPHES and len(noms) >= 4


# ============================================================================
# 8) TRAITEMENT D'UNE PAGE + CACHE
# ============================================================================

def id_chunk(page: int, bande: int, nb: int) -> str:
    return f"p{page:03d}" if nb == 1 else f"p{page:03d}_b{bande}"


def chemin_cache(cfg: Config, page: int, bande: int) -> str:
    return os.path.join(cfg.sortie, "cache", id_chunk(page, bande, cfg.decoupe) + ".json")


def empreinte(cfg: Config) -> str:
    """Change dès que le résultat en cache ne serait plus celui que produirait cette configuration."""
    cle = json.dumps([cfg.prompt, cfg.modele, cfg.decoupe, cfg.nettoyage, cfg.max_cote, cfg.json_schema], ensure_ascii=False)
    return hashlib.sha1(cle.encode("utf-8")).hexdigest()[:10]


def charger_cache(cfg: Config, page: int, bande: int):
    p = chemin_cache(cfg, page, bande)
    if cfg.refaire or not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if d.get("empreinte") != empreinte(cfg):
        return None                                  # autre prompt / modèle / découpage / nettoyage : périmé
    if d.get("statut") == "partiel" and cfg.max_tokens > d.get("max_tokens", 0):
        return None                                  # on retente avec un budget plus grand
    return d


def ecrire_atomique(chemin: str, texte: str) -> None:
    os.makedirs(os.path.dirname(chemin) or ".", exist_ok=True)
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(texte)
    os.replace(tmp, chemin)


def traiter_chunk(cfg: Config, modele: str, page: int, bande: int) -> dict:
    t = time.time()
    ident = id_chunk(page, bande, cfg.decoupe)
    png, encre = preparer_image(cfg, page, bande)
    if encre < SEUIL_BLANC:                      # page blanche : inutile (et risqué : le modèle inventerait) de l'envoyer
        res = {"page": page, "bande": bande, "decoupe": cfg.decoupe, "nettoyage": cfg.nettoyage,
               "empreinte": empreinte(cfg), "statut": "ok", "blanche": True,
               "avertissements": ["page blanche : aucune requête envoyée"], "modele": modele,
               "max_tokens": cfg.max_tokens, "duree": 0.0, "tokens": 0, "entrees": []}
        ecrire_atomique(chemin_cache(cfg, page, bande), json.dumps(res, ensure_ascii=False, indent=1))
        return res
    if cfg.sauver_images:
        os.makedirs(os.path.join(cfg.sortie, "images"), exist_ok=True)
        with open(os.path.join(cfg.sortie, "images", ident + ".png"), "wb") as f:
            f.write(png)
    prompt = cfg.prompt + (NOTE_BANDE[bande] if cfg.decoupe > 1 else "")
    derniere = "?"
    for essai in range(1, cfg.essais + 1):
        temp = cfg.temperature if essai == 1 else min(1.0, cfg.temperature + 0.2)   # une boucle de répétition ne se répète pas à l'identique
        try:
            rep = appeler_llm(cfg, modele, png, prompt, temp)
        except ErreurLLM as e:
            derniere = str(e)
            continue
        obj, avert, partiel = extraire_page_json(rep)
        if obj is None:
            derniere = avert[0]
            ecrire_atomique(os.path.join(cfg.sortie, "cache", "brut", ident + f"_essai{essai}.txt"),
                            rep["content"] + "\n\n=== RÉFLEXION ===\n" + rep["reasoning"])
            continue
        entrees = normaliser_page(obj)
        if est_exemple(entrees):
            derniere = "le modèle a recopié l'exemple du prompt"
            continue
        res = {"page": page, "bande": bande, "decoupe": cfg.decoupe, "nettoyage": cfg.nettoyage,
               "empreinte": empreinte(cfg), "statut": "partiel" if partiel else "ok", "avertissements": avert,
               "modele": modele,
               "max_tokens": cfg.max_tokens, "duree": round(time.time() - t, 1), "tokens": rep["tokens"],
               "entrees": entrees}
        ecrire_atomique(chemin_cache(cfg, page, bande), json.dumps(res, ensure_ascii=False, indent=1))
        ecrire_atomique(os.path.join(cfg.sortie, "cache", "brut", ident + ".txt"), rep["content"])
        return res
    raise ErreurLLM(derniere)


def executer(cfg: Config, modele: str, chunks: list[tuple[int, int]]) -> dict:
    """Lance les requêtes (en parallèle si demandé). -> {(page, bande): message d'erreur}."""
    echecs: dict = {}
    if not chunks:
        return echecs
    stop = threading.Event()
    durees: list[float] = []

    def tache(ch):
        if stop.is_set():
            return ch, None, "annulé"
        try:
            return ch, traiter_chunk(cfg, modele, *ch), None
        except ErreurFatale as e:
            stop.set()
            return ch, None, "FATAL: " + str(e)
        except Exception as e:                                    # noqa: BLE001 : une page ne doit pas tuer le lot
            return ch, None, f"{type(e).__name__}: {e}"

    ex = ThreadPoolExecutor(max_workers=max(1, cfg.paralleles))
    futs = [ex.submit(tache, ch) for ch in chunks]
    fait = 0
    try:
        for fut in as_completed(futs):
            ch, res, err = fut.result()
            fait += 1
            nom = id_chunk(ch[0], ch[1], cfg.decoupe)
            if err:
                echecs[ch] = err
                if err.startswith("FATAL"):
                    log(f"ARRÊT : {err[7:]}")
                elif err != "annulé":
                    log(f"[{fait}/{len(chunks)}] {nom} ÉCHEC : {err}")
                continue
            if not res.get("blanche"):
                durees.append(res["duree"])
            moy = sum(durees) / len(durees) if durees else 0.0
            reste = (len(chunks) - fait) * moy / max(1, cfg.paralleles)
            log(f"[{fait}/{len(chunks)}] {nom} : {len(res['entrees'])} entrées, {res['duree']:.0f}s, "
                f"{res['tokens']} tokens{' (PARTIEL)' if res['statut'] == 'partiel' else ''}{' (page blanche)' if res.get('blanche') else ''} "
                f"— reste ≈ {reste / 60:.0f} min")
    except KeyboardInterrupt:
        stop.set()
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)
    return echecs


# ============================================================================
# 9) ASSEMBLAGE : suites de pages -> une ligne par prénom
# ============================================================================

TERMINAL_RE = re.compile(r"[.!?»”)\"…]\s*$")


def assembler(resultats: dict, nb_bandes: int) -> tuple[list[dict], list[str]]:
    """Fusionne les suites de définitions d'une page (ou demi-page) à l'autre."""
    entrees, avert = [], []
    derniere, prec = None, None
    for (page, bande) in sorted(resultats):
        res = resultats[(page, bande)]
        idx = (page - 1) * nb_bandes + (bande - 1)
        if prec is not None and idx != prec + 1:
            derniere = None                       # trou (page manquante) : on ne rattache rien à l'aveugle
        prec = idx
        for raw in res["entrees"]:
            e = {"prenoms": raw["prenoms"], "description": raw["description"],
                 "description_incomplete": raw["description_incomplete"], "page": page,
                 "partiel": res.get("statut") == "partiel"}
            if not e["prenoms"]:
                if not e["description"]:
                    continue
                if derniere is not None and (derniere["description_incomplete"] or not derniere["description"]
                                             or not TERMINAL_RE.search(derniere["description"])):
                    derniere["description"] = joindre(derniere["description"], e["description"])
                    derniere["description_incomplete"] = e["description_incomplete"]
                else:
                    avert.append(f"p.{page} : suite de définition sans entrée à laquelle la rattacher : « {e['description'][:70]}… »")
                continue
            entrees.append(e)
            derniere = e
    for i, e in enumerate(entrees, 1):
        e["id"] = i
    return entrees, avert


LATIN_OK = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿŒœ'’ʿʾ‘`\- .]+$")
INFO_FLAGS = {"arabe_sans_voyelles"}


def drapeaux(row: dict, e: dict) -> list[str]:
    f = []
    ar, d, s = row["prénom_arab_diacritiques"], row["description_en_français"], row["prénom_fr"]
    if not ar:
        f.append("arabe_vide")
    else:
        if re.search(r"[A-Za-z0-9]", ar):
            f.append("arabe_non_arabe")
        if not re.search(f"[{HARAKAT}]", ar):
            f.append("arabe_sans_voyelles")
    if not d:
        f.append("description_vide")
    else:
        if len(d) < 12:
            f.append("description_courte")
        if e["description_incomplete"]:
            f.append("description_coupee")
        elif not TERMINAL_RE.search(d):
            f.append("sans_point_final")
    if re.search(r"\d", s):
        f.append("chiffres_dans_prenom")
    elif not LATIN_OK.match(s):
        f.append("caracteres_suspects")
    if len(re.sub(r"[^A-Za-zÀ-ÿ]", "", s)) < 2:
        f.append("prenom_court")
    if not row["genre"] or (row["genre"] == "MASCULIN" and re.sub(f"[{HARAKAT}]+$", "", ar).endswith("ة")):
        f.append("genre_a_verifier")
    if e["partiel"]:
        f.append("page_partielle")
    return f


COLS = ["prénom_fr", "prénom_arab_diacritiques", "genre", "description_en_français",
        "prénoms_équivalents", "forme_associée", "équivalent_étranger", "translittération",
        "thème", "sous_thème", "page_imprimée", "page_pdf", "id_entrée", "à_vérifier"]


def exploser(entrees: list[dict]) -> list[dict]:
    """UNE LIGNE PAR PRÉNOM : « Youssouf / Youssef » -> deux lignes, même arabe, même genre, même définition."""
    rows = []
    for e in entrees:
        translit, equiv = parse_description(e["description"])
        theme, sous = lookup_theme(e["page"])
        blocs = e["prenoms"]
        for i, b in enumerate(blocs):
            autre = " / ".join(s for j, x in enumerate(blocs) if j != i for s in x["orthographes"])
            for s in b["orthographes"]:
                row = {"prénom_fr": s,
                       "prénom_arab_diacritiques": b["arabe"],
                       "genre": b["genre"],
                       "description_en_français": e["description"],
                       "prénoms_équivalents": " / ".join(x for x in b["orthographes"] if x != s),
                       "forme_associée": autre,
                       "équivalent_étranger": equiv,
                       "translittération": translit,
                       "thème": theme, "sous_thème": sous,
                       "page_imprimée": e["page"] - PAGE_OFFSET, "page_pdf": e["page"],
                       "id_entrée": e["id"]}
                row["à_vérifier"] = "; ".join(drapeaux(row, e))
                rows.append(row)
    return rows


# ============================================================================
# 10) EXPORTS : CSV (format de ton exemple), JSON, Excel
# ============================================================================

def _propre(v):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", v) if isinstance(v, str) else v


def a_verifier(row: dict) -> bool:
    return any(x and x not in INFO_FLAGS for x in row["à_vérifier"].split("; "))


def exporter(rows: list[dict], infos_pages: list[dict], dossier: str, nom: str = "prenoms_arabes") -> dict:
    os.makedirs(dossier, exist_ok=True)
    chemins = {}
    # CSV : en-tête sans guillemets, valeurs entre guillemets, UTF-8 avec BOM (Excel-friendly)
    chemins["csv"] = os.path.join(dossier, nom + ".csv")
    with open(chemins["csv"], "w", encoding="utf-8-sig", newline="") as f:
        f.write(",".join(COLS) + "\n")
        w = csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator="\n")
        for r in rows:
            w.writerow([r[c] for c in COLS])
    chemins["json"] = os.path.join(dossier, nom + ".json")
    with open(chemins["json"], "w", encoding="utf-8") as f:
        json.dump([{c: r[c] for c in COLS} for r in rows], f, ensure_ascii=False, indent=1)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        log("openpyxl absent : pas d'Excel (pip install openpyxl)")
        return chemins
    wb = Workbook()
    jaune = PatternFill("solid", fgColor="FFF2CC")
    entete = PatternFill("solid", fgColor="1F4E78")
    larg = {"prénom_fr": 20, "prénom_arab_diacritiques": 24, "genre": 11, "description_en_français": 80,
            "prénoms_équivalents": 22, "forme_associée": 20, "équivalent_étranger": 16, "translittération": 20,
            "thème": 24, "sous_thème": 30, "page_imprimée": 9, "page_pdf": 9, "id_entrée": 9, "à_vérifier": 28}

    def feuille(ws, lignes, colonnes, surligner=False):
        ws.append(colonnes)
        for c in ws[1]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), entete
        for r in lignes:
            ws.append([_propre(r[c]) for c in colonnes])
            if surligner and a_verifier(r):
                for c in ws[ws.max_row]:
                    c.fill = jaune
        for k, c in enumerate(colonnes, 1):
            ws.column_dimensions[get_column_letter(k)].width = larg.get(c, 14)
            if c == "prénom_arab_diacritiques":
                for cell in ws[get_column_letter(k)][1:]:
                    cell.font, cell.alignment = Font(name="Arial", size=14), Alignment(horizontal="right", readingOrder=2)
            elif c == "description_en_français":
                for cell in ws[get_column_letter(k)][1:]:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions

    ws = wb.active
    ws.title = "Prénoms"
    feuille(ws, rows, COLS, surligner=True)
    feuille(wb.create_sheet("À vérifier"), [r for r in rows if a_verifier(r)], COLS)
    colp = ["page_pdf", "page_imprimée", "statut", "entrées", "lignes", "durée_s", "tokens", "remarque"]
    wp = wb.create_sheet("Pages")
    wp.append(colp)
    for c in wp[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), entete
    for p in infos_pages:
        wp.append([_propre(p.get(c, "")) for c in colp])
    for k in range(1, len(colp) + 1):
        wp.column_dimensions[get_column_letter(k)].width = 14 if k < 8 else 70
    chemins["xlsx"] = os.path.join(dossier, nom + ".xlsx")
    wb.save(chemins["xlsx"])
    return chemins


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def comparer_ancien(rows: list[dict], chemin_ancien: str, pages: list[int], dossier: str) -> str | None:
    """Compare page par page avec le CSV de l'ancienne extraction OCR : repère les oublis probables du modèle."""
    try:
        with open(chemin_ancien, encoding="utf-8-sig", newline="") as f:
            anciens = list(csv.DictReader(f))
    except OSError as e:
        log(f"--ancien illisible : {e}")
        return None
    par_page_a: dict[int, set] = {}
    for r in anciens:
        try:
            p = int(r.get("Page (PDF)", ""))
        except ValueError:
            continue
        par_page_a.setdefault(p, set()).update(norm(x) for x in re.split(r"\s*/\s*", r.get("Prénom(s)", "")) if norm(x))
    par_page_n: dict[int, set] = {}
    for r in rows:
        par_page_n.setdefault(r["page_pdf"], set()).add(norm(r["prénom_fr"]))
    sortie = os.path.join(dossier, "comparaison_ancien.csv")
    ecarts = 0
    with open(sortie, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writerow(["page_pdf", "nb_ancien", "nb_nouveau", "seulement_ancien", "seulement_nouveau"])
        for p in pages:
            a, n = par_page_a.get(p, set()), par_page_n.get(p, set())
            if a - n or n - a:
                ecarts += 1
            w.writerow([p, len(a), len(n), " | ".join(sorted(a - n)), " | ".join(sorted(n - a))])
    log(f"comparaison avec l'ancienne extraction : {ecarts} page(s) avec écarts -> {sortie}")
    return sortie


# ============================================================================
# 11) PIPELINE, DIAGNOSTIC, AUTOTEST
# ============================================================================

def parse_pages(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def run_pipeline(cfg: Config) -> tuple[list[dict], list[dict], list[str]]:
    pages = cfg.pages
    bandes = range(1, cfg.decoupe + 1)
    chunks = [(p, b) for p in pages for b in bandes]
    resultats, a_faire, perimes = {}, [], 0
    for ch in chunks:
        d = charger_cache(cfg, *ch)
        if d is not None:
            resultats[ch] = d
        else:
            a_faire.append(ch)
            if not cfg.refaire and os.path.exists(chemin_cache(cfg, *ch)):
                perimes += 1
    log(f"{len(pages)} page(s) demandée(s) : {len(resultats)} déjà en cache, {len(a_faire)} à traiter"
        f" (découpe {cfg.decoupe}, nettoyage {'oui' if cfg.nettoyage else 'non'}, parallèle {cfg.paralleles}).")
    if perimes:
        log(f"  dont {perimes} en cache mais périmés (prompt, modèle, découpage ou nettoyage différents) : recalculés.")
    echecs: dict = {}
    interrompu = False
    if a_faire:
        modele = resoudre_modele(cfg)
        log(f"modèle : {modele} — {cfg.url}")
        try:
            echecs = executer(cfg, modele, a_faire)
        except KeyboardInterrupt:
            interrompu = True
            log("interruption : export de ce qui est déjà calculé (relance la même commande pour reprendre).")
        for ch in a_faire:
            d = charger_cache(cfg, *ch)
            if d is not None:
                resultats[ch] = d
        cfg.fatal = next((v[7:] for v in echecs.values() if v.startswith("FATAL")), "")
    entrees, avert = assembler(resultats, cfg.decoupe)
    rows = exploser(entrees)
    infos = []
    for p in pages:
        cs = [resultats.get((p, b)) for b in bandes]
        manque = [b for b in bandes if resultats.get((p, b)) is None]
        errs = [echecs.get((p, b)) for b in manque if echecs.get((p, b))]
        statut = "échec" if manque and len(manque) == len(cs) else ("incomplet" if manque else
                 ("partiel" if any(c["statut"] == "partiel" for c in cs) else "ok"))
        n_ent = sum(len(c["entrees"]) for c in cs if c)
        remarque = "; ".join(errs) if errs else ("interrompu" if interrompu and manque else "")
        if not manque and all(c.get("blanche") for c in cs):
            remarque = "page blanche (aucune requête envoyée)"
        infos.append({"page_pdf": p, "page_imprimée": p - PAGE_OFFSET, "statut": statut, "entrées": n_ent,
                      "lignes": sum(1 for r in rows if r["page_pdf"] == p),
                      "durée_s": round(sum(c["duree"] for c in cs if c), 1),
                      "tokens": sum(c["tokens"] for c in cs if c),
                      "remarque": remarque})
    cfg.interrompu = interrompu
    return rows, infos, avert


def resume(rows: list[dict], infos: list[dict], avert: list[str]) -> None:
    n_entrees = len({r["id_entrée"] for r in rows})
    log(f"RÉSULTAT : {len(rows)} prénoms (lignes) issus de {n_entrees} entrées — "
        f"{sum(r['genre'] == 'MASCULIN' for r in rows)} masculins, {sum(r['genre'] == 'FEMININ' for r in rows)} féminins.")
    log(f"  arabe rempli : {sum(bool(r['prénom_arab_diacritiques']) for r in rows)}/{len(rows)} ; "
        f"lignes « à vérifier » : {sum(a_verifier(r) for r in rows)}.")
    mauvais = [p["page_pdf"] for p in infos if p["statut"] in ("échec", "incomplet")]
    partiels = [p["page_pdf"] for p in infos if p["statut"] == "partiel"]
    vides = [p["page_pdf"] for p in infos if p["statut"] == "ok" and p["entrées"] < 3
             and not str(p["remarque"]).startswith("page blanche") and DICT_START + 2 <= p["page_pdf"] <= DICT_END]
    if mauvais:
        log(f"  PAGES EN ÉCHEC ({len(mauvais)}) : {mauvais[:40]}{' …' if len(mauvais) > 40 else ''} -> relance la même commande.")
    if partiels:
        log(f"  pages tronquées ({len(partiels)}) : {partiels[:40]} -> relance avec un --max-tokens plus grand.")
    if vides:
        log(f"  pages suspectes (moins de 3 entrées) : {vides[:40]}{' …' if len(vides) > 40 else ''}")
    for a in avert[:15]:
        log("  ⚠ " + a)
    if len(avert) > 15:
        log(f"  … et {len(avert) - 15} autre(s) avertissement(s).")


def diagnostic(cfg: Config) -> None:
    from PIL import ImageDraw
    log(f"1/2  serveur {cfg.url} …")
    ids = lister_modeles(cfg)
    log(f"     modèles vus par LM Studio : {ids}")
    modele = resoudre_modele(cfg)
    log(f"     modèle retenu : {modele}")
    log("2/2  test de vision (lecture d'une image contenant « ADAM 123 ») …")
    petit = Image.new("RGB", (100, 30), "white")
    ImageDraw.Draw(petit).text((8, 9), "ADAM 123", fill="black")
    grand = petit.resize((700, 210), Image.NEAREST)
    buf = io.BytesIO()
    grand.save(buf, "PNG")
    t = time.time()
    rep = appeler_llm(cfg, modele, buf.getvalue(),
                      "Quel texte est écrit dans l'image ? Réponds uniquement par ce texte.", 0.0, max_tokens=4000)
    ok = "ADAM" in retirer_reflexion(rep["content"]).upper() or "ADAM" in rep["content"].upper()
    log(f"     réponse en {time.time() - t:.0f}s : {retirer_reflexion(rep['content']).strip()[:80]!r} -> "
        f"{'VISION OK' if ok else 'réponse inattendue : la vision ne semble pas fonctionner'}")
    log("     réflexion : " + ("séparée dans reasoning_content" if rep["reasoning"] else
                               ("balises <think> dans le contenu (le script les retire)" if "<think>" in rep["content"].lower()
                                else "aucune trace")) + f" ; {rep['tokens']} tokens générés.")


def autotest(cfg: Config) -> None:
    import verite_terrain as vt
    cfg.pages = list(vt.PAGES)
    cfg.sortie = os.path.join(cfg.sortie, "autotest")
    rows, infos, avert = run_pipeline(cfg)
    if cfg.fatal:
        raise ErreurFatale(cfg.fatal)
    resume(rows, infos, avert)
    s = vt.score(rows, cfg.pages)
    log("AUTOTEST (4 pages lues à l'œil : 55, 56, 100, 150) :")
    for k, v in s.items():
        log(f"   {k:14s} {v}")
    log("   Repère : 'vedettes' proche du total, 'parasites' 0, 'genre' et 'arabe_exact' élevés = bonne configuration.")


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Extraction du Livre des prénoms arabes avec un modèle de vision (LM Studio).")
    ap.add_argument("pdf", nargs="?", default="", help="chemin du PDF")
    ap.add_argument("--url", default="http://localhost:1234/v1", help="serveur LM Studio (défaut http://localhost:1234/v1)")
    ap.add_argument("--modele", default="prism-ml/bonsai-27b", help="nom du modèle (l'identifiant exact est retrouvé automatiquement)")
    ap.add_argument("--debut", type=int, default=DICT_START, help=f"première page PDF (défaut {DICT_START})")
    ap.add_argument("--fin", type=int, default=DICT_END, help=f"dernière page PDF (défaut {DICT_END})")
    ap.add_argument("--pages", default="", help="liste explicite, ex. 55,56,100-104 (remplace --debut/--fin)")
    ap.add_argument("--sortie", default="sortie_llm", help="dossier de sortie")
    ap.add_argument("--paralleles", type=int, default=1, help="requêtes simultanées (2-4 si LM Studio et la VRAM suivent)")
    ap.add_argument("--decoupe", type=int, choices=(1, 2), default=1, help="2 = chaque page en deux moitiés (plus de détail par requête)")
    ap.add_argument("--nettoyage", choices=("oui", "non"), default="oui", help="efface le texte fantôme du verso (défaut oui)")
    ap.add_argument("--max-cote", type=int, default=0, help="réduit l'image si son plus grand côté dépasse N px (0 = image native)")
    ap.add_argument("--temperature", type=float, default=0.6, help="0.6 = réglage conseillé pour les modèles Qwen en mode réflexion (éviter 0 : boucles de répétition)")
    ap.add_argument("--top-p", type=float, default=0.95, help="0 = ne pas envoyer")
    ap.add_argument("--top-k", type=int, default=20, help="0 = ne pas envoyer")
    ap.add_argument("--max-tokens", type=int, default=16000, help="budget de génération par requête, réflexion comprise")
    ap.add_argument("--timeout", type=int, default=1800, help="délai maximal par requête, en secondes")
    ap.add_argument("--essais", type=int, default=2, help="tentatives par page")
    ap.add_argument("--json-schema", action="store_true", help="impose le format via response_format (peut réduire la réflexion)")
    ap.add_argument("--corps-extra", default="", help="JSON fusionné dans la requête, ex. '{\"top_p\": 0.9}'")
    ap.add_argument("--prompt-fichier", default="", help="remplace le prompt par défaut par le contenu de ce fichier")
    ap.add_argument("--refaire", action="store_true", help="ignore le cache et recalcule les pages demandées")
    ap.add_argument("--sauver-images", action="store_true", help="garde dans sortie/images les PNG envoyés au modèle")
    ap.add_argument("--ancien", default="", help="CSV de l'ancienne extraction OCR : produit comparaison_ancien.csv")
    ap.add_argument("--diagnostic", action="store_true", help="teste LM Studio et la vision, puis quitte")
    ap.add_argument("--autotest", action="store_true", help="traite 4 pages et compare à la vérité terrain")
    a = ap.parse_args(argv)

    cfg = Config(pdf=a.pdf, url=a.url, modele=a.modele, sortie=a.sortie, paralleles=a.paralleles, decoupe=a.decoupe,
                 nettoyage=a.nettoyage == "oui", max_cote=a.max_cote, temperature=a.temperature, top_p=a.top_p, top_k=a.top_k,
                 max_tokens=a.max_tokens, timeout=a.timeout, essais=max(1, a.essais), json_schema=a.json_schema,
                 refaire=a.refaire, sauver_images=a.sauver_images, ancien=a.ancien)
    if a.corps_extra:
        try:
            cfg.corps_extra = json.loads(a.corps_extra)
        except ValueError:
            ap.error("--corps-extra doit être du JSON valide")
    if a.prompt_fichier:
        with open(a.prompt_fichier, encoding="utf-8") as f:
            cfg.prompt = f.read()
    try:
        if a.diagnostic:
            diagnostic(cfg)
            return
        if not a.pdf or not os.path.exists(a.pdf):
            ap.error("indique le chemin du PDF (fichier introuvable)")
        if a.autotest:
            autotest(cfg)
            return
        cfg.pages = parse_pages(a.pages) if a.pages else list(range(a.debut, a.fin + 1))
        rows, infos, avert = run_pipeline(cfg)
    except ErreurFatale as e:
        log(f"ERREUR : {e}")
        sys.exit(2)
    if cfg.fatal and not rows:
        sys.exit(2)                                            # rien à exporter
    ch = exporter(rows, infos, cfg.sortie)
    resume(rows, infos, avert)
    if cfg.ancien:
        comparer_ancien(rows, cfg.ancien, cfg.pages, cfg.sortie)
    log("Fichiers : " + " | ".join(ch.values()))
    if cfg.fatal:
        sys.exit(2)
    if cfg.interrompu:
        sys.stdout.flush()
        os._exit(130)          # les requêtes encore en vol n'empêchent pas la sortie


if __name__ == "__main__":
    main()
