#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extraire_prenoms_llm.py  (v4 : lecture des pages par un modèle de vision via LM Studio, en STREAMING)
====================================================================================================
Extrait « Le Livre des prénoms arabes » (PDF scanné, sans couche de texte) vers CSV / Excel / JSON,
en envoyant chaque page à un modèle de vision servi par LM Studio (ex. prism-ml/bonsai-27b).

CE QUI CHANGE PAR RAPPORT À LA v3 (audit : voir README.md)
  * Requêtes en STREAMING : progression visible (jetons, tok/s), arrêt anticipé en cas de boucle,
    de réflexion trop longue, de silence ou de Ctrl+C. La connexion est fermée -> LM Studio abandonne.
  * Réflexion du modèle COUPÉE par défaut quand LM Studio le permet (« reasoning: off ») : pour recopier une
    page, elle coûte des milliers de jetons pour rien. `--reflexion oui` la rétablit, avec un budget borné.
  * Budget de contexte vérifié : jetons d'entrée mesurés (image + consigne), sortie bornée par le contexte chargé.
  * Image réduite automatiquement à ~2 500 jetons (CUDA ne la réduit pas) : `--tokens-image 0` = image native.
  * Reprise fiable : cache par page, export intermédiaire tous les N pages, `--export` (sans LM Studio), `--statut`.
  * `--diagnostic` : configuration chargée, vitesse réelle, mesure GPU (nvidia-smi), commande `lms load` conseillée.

USAGE RAPIDE
    python extraire_prenoms_llm.py --diagnostic livre.pdf        # LM Studio + vitesse + GPU + réglages conseillés
    python extraire_prenoms_llm.py livre.pdf --autotest          # 4 pages avec vérité terrain
    python extraire_prenoms_llm.py livre.pdf                     # tout le dictionnaire (pages 53-338), reprenable
    python extraire_prenoms_llm.py --statut                      # où en est-on ? (sans LM Studio)
    python extraire_prenoms_llm.py --export                      # régénère CSV/JSON/Excel depuis le cache

Le détail est dans README.md.
"""
from __future__ import annotations

import argparse
import base64
import collections
import csv
import hashlib
import http.client
import importlib.metadata
import difflib
import glob
import io
import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.parse
import zipfile
from dataclasses import asdict, dataclass, field

import numpy as np
from PIL import Image

VERSION = "4.4.0"
T0 = time.time()

MUET = False            # True : rien sur la console (interface plein écran)
ECOUTEURS: list = []    # fonctions appelées à chaque ligne affichable : f(niveau, texte)
SECRETS: set = set()    # valeurs à masquer partout (jeton d'API...)


def masquer(texte) -> str:
    texte = str(texte)
    for s in list(SECRETS):
        if s and len(s) >= 4:
            texte = texte.replace(s, "***")
    return texte


class Journal:
    """Journal de session, écrit au fil de l'eau (une ligne = un flush : un plantage laisse quand même la trace).
       <dossier>/AAAAMMJJ_HHMMSS_<mode>.log    texte lisible : heure, niveau, fil d'exécution, message (INFO + DEBUG)
       <dossier>/AAAAMMJJ_HHMMSS_<mode>.jsonl  un événement structuré par ligne (requêtes, pages, GPU, erreurs...)
    Avant l'ouverture (le dossier n'est connu qu'après la lecture des options), les lignes attendent en mémoire."""

    ATTENTE_MAX = 5000

    def __init__(self):
        self.verrou = threading.RLock()
        self.f_txt = self.f_jsonl = None
        self.chemin = self.chemin_jsonl = ""
        self.attente_txt: list = []
        self.attente_jsonl: list = []
        self.niveau_fichier = "DEBUG"

    def ouvrir(self, dossier: str, mode: str, niveau: str = "DEBUG", garder: int = 30) -> str:
        with self.verrou:
            self.fermer()
            os.makedirs(dossier, exist_ok=True)
            base = os.path.join(dossier, time.strftime("%Y%m%d_%H%M%S") + "_" + (re.sub(r"[^a-z0-9]+", "-", mode.lower()).strip("-") or "session"))
            racine, n = base, 1
            while os.path.exists(racine + ".log"):
                n += 1
                racine = f"{base}-{n}"
            self.chemin, self.chemin_jsonl = racine + ".log", racine + ".jsonl"
            self.f_txt = open(self.chemin, "a", encoding="utf-8")
            self.f_jsonl = open(self.chemin_jsonl, "a", encoding="utf-8")
            self.niveau_fichier = str(niveau).upper()
            self.f_txt.writelines(self.attente_txt)
            self.f_jsonl.writelines(self.attente_jsonl)
            self.f_txt.flush()
            self.f_jsonl.flush()
            self.attente_txt, self.attente_jsonl = [], []
            self._purger(dossier, garder)
            return self.chemin

    @staticmethod
    def _purger(dossier: str, garder: int) -> None:
        try:
            noms = sorted(n[:-4] for n in os.listdir(dossier) if n.endswith(".log"))
            for n in noms[:-garder] if garder > 0 else []:
                for ext in (".log", ".jsonl"):
                    try:
                        os.remove(os.path.join(dossier, n + ext))
                    except OSError:
                        pass
        except OSError:
            pass

    def _ecrire(self, fichier, attente: list, ligne: str) -> None:
        with self.verrou:
            if fichier is not None:
                try:
                    fichier.write(ligne)
                    fichier.flush()
                except (OSError, ValueError):
                    pass
            elif len(attente) < self.ATTENTE_MAX:
                attente.append(ligne)

    def ecrire(self, niveau: str, msg: str) -> None:
        if niveau == "DEBUG" and self.niveau_fichier != "DEBUG":
            return
        t = time.time()
        corps = str(msg).replace("\n", "\n    ")
        ligne = (f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))}.{int(t % 1 * 1000):03d} {niveau:<5} "
                 f"[{threading.current_thread().name}] {corps}\n")
        self._ecrire(self.f_txt, self.attente_txt, ligne)

    def evenement(self, type_: str, **champs) -> None:
        d = {"t": round(time.time(), 3), "dt": round(time.time() - T0, 2), "fil": threading.current_thread().name, "type": type_}
        d.update(champs)
        try:
            ligne = masquer(json.dumps(d, ensure_ascii=False, default=str)) + "\n"
        except (TypeError, ValueError):
            ligne = json.dumps({"type": type_, "erreur_serialisation": True}) + "\n"
        self._ecrire(self.f_jsonl, self.attente_jsonl, ligne)

    def fermer(self) -> None:
        with self.verrou:
            for f in (self.f_txt, self.f_jsonl):
                if f is not None:
                    try:
                        f.close()
                    except OSError:
                        pass
            self.f_txt = self.f_jsonl = None


JOURNAL = Journal()


def log(msg: str, niveau: str = "INFO") -> None:
    """Journal (fichier) + console + écouteurs (interface). Le niveau DEBUG ne va que dans le fichier."""
    msg = masquer(msg)
    JOURNAL.ecrire(niveau, msg)
    if niveau == "DEBUG":
        return
    ligne = f"[{time.time() - T0:7.1f}s] {msg}"
    if not MUET:
        try:
            print(ligne, flush=True)
        except (OSError, ValueError):
            pass
    for f in list(ECOUTEURS):
        try:
            f(niveau, ligne)
        except Exception:                                        # noqa: BLE001 : un écouteur défaillant ne doit rien casser
            pass


def debug(msg: str) -> None:
    log(msg, "DEBUG")


def evenement(type_: str, **champs) -> None:
    JOURNAL.evenement(type_, **champs)


def log_exception(contexte: str, exc: BaseException) -> None:
    """Message court sur la console, trace complète dans le fichier de log."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log(f"{contexte} : {type(exc).__name__}: {exc}", "ERROR")
    JOURNAL.ecrire("ERROR", "trace :\n" + masquer(tb))
    JOURNAL.evenement("exception", contexte=contexte, type_exception=type(exc).__name__, message=str(exc), trace=tb)


def infos_environnement() -> dict:
    paquets = {}
    for nom in ("Pillow", "numpy", "openpyxl", "PyMuPDF", "textual", "rich"):
        try:
            paquets[nom] = importlib.metadata.version(nom)
        except Exception:                                        # noqa: BLE001
            paquets[nom] = None
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            sha = hashlib.sha1(f.read(), usedforsecurity=False).hexdigest()[:10]
    except OSError:
        sha = ""
    try:
        gpu = EchantillonGPU().instantane()
    except Exception:                                            # noqa: BLE001
        gpu = None
    return {"version_script": VERSION, "sha1_script": sha, "python": sys.version.split()[0], "plateforme": platform.platform(),
            "processeur": platform.processor(), "cpu_logiques": os.cpu_count(), "dossier_courant": os.getcwd(),
            "script": os.path.abspath(__file__), "paquets": paquets, "gpu": gpu}


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
# 2) LE PROMPT (rôle « système » : constant d'une requête à l'autre ; la page vient dans le message utilisateur)
# ============================================================================

PROMPT = """Tu transcris une page scannée du « Livre des prénoms arabes » (dictionnaire de prénoms). Réponds UNIQUEMENT par un objet JSON, sans texte avant ni après et sans analyse préalable.

MISE EN PAGE
Une entrée = un ou deux « blocs de prénom », puis UN paragraphe de définition.
- Bloc de prénom : 1 à 4 orthographes françaises empilées en gras ; à droite de la 1re ligne, le même prénom en arabe.
  Colonne de gauche, texte NOIR = MASCULIN ; colonne de droite, texte ROUGE = FEMININ.
- Deux blocs pour une même définition (ex. Nadil à gauche, Nadila à droite) = UNE entrée à deux blocs. Un prénom « mixte » apparaît dans les deux colonnes : deux blocs aussi.
- Définition : paragraphe en caractères romains (sens en italique), commun à tous les blocs de l'entrée. Elle commence souvent par un astérisque « * » : ne le recopie pas.

RÈGLES
- Recopie exactement : orthographes (accents, apostrophes, tirets, majuscules) et définition COMPLÈTE, sans résumer, traduire ni corriger. Recolle les mots coupés en fin de ligne (« islami- » + « que » -> « islamique »).
- Arabe : les lettres ET toutes les voyelles brèves et signes visibles (fatha, damma, kasra, shadda, soukoun, tanwin), jamais ajoutés ; sans les barres d'allongement (tatwil « ـ »). Illisible ou absent : "".
- IGNORE : titre courant et numéro de page (en haut), bandeaux rouges de section, légende « MASCULIN / FEMININ », paragraphes d'introduction, notes de bas de page (petit texte sous un filet), texte pâle transparent du verso, petits chiffres d'appel de note collés à un mot (« famille¹⁹ » -> « famille »). Exposants en ligne : « 12e ».
- Haut de page : si elle commence par la fin d'une définition (aucun prénom avant) : entrée avec "prenoms": [] et "suite_page_precedente": true.
- Bas de page : définition coupée, ou prénoms sans leur définition : "description_incomplete": true.
- Ces deux champs ne s'écrivent QUE lorsqu'ils valent true (sinon, ne les écris pas).
- Ne fusionne pas deux entrées, n'en invente aucune, n'en oublie aucune.

FORMAT (exemple, à ne pas recopier) :
{"entrees":[{"prenoms":[{"genre":"MASCULIN","orthographes":["Karim"],"arabe":"كَرِيم"},{"genre":"FEMININ","orthographes":["Karima"],"arabe":"كَرِيمَة"}],"description":"Karîm / Karîma : généreux."}]}
Page sans entrée : {"entrees":[]}"""

TEXTE_UTILISATEUR = "Extrais toutes les entrées de cette page (JSON uniquement)."

NOTE_BANDE = {
    1: "\nATTENTION : tu ne vois que la MOITIÉ HAUTE de la page. La dernière entrée peut être coupée en bas (mets alors \"description_incomplete\": true).",
    2: "\nATTENTION : tu ne vois que la MOITIÉ BASSE de la page. Le texte du haut peut être la suite d'une définition commencée plus haut (mets alors \"prenoms\": [] et \"suite_page_precedente\": true).",
}

# description de l'exemple du prompt : si TOUTES les entrées renvoyées l'ont, le modèle a recopié l'exemple
EXEMPLE_DESC = "Karîm / Karîma : généreux."


# ============================================================================
# 3) CONFIG + ERREURS
# ============================================================================

@dataclass
class Config:
    pdf: str = ""
    url: str = "http://localhost:1234"          # racine du serveur (« …/v1 » est accepté et ramené à la racine)
    modele: str = "prism-ml/bonsai-27b"
    sortie: str = "sortie_llm"
    pages: list = field(default_factory=list)
    paralleles: int = 1
    decoupe: int = 1
    nettoyage: bool = True
    max_cote: int = 0              # plafond MANUEL (px) du plus grand côté de l'image ; 0 = automatique (tokens_image)
    max_cote_auto: int = 0         # calculé au démarrage d'après le nombre de jetons d'image mesuré
    tokens_image: int = 2500       # cible de jetons d'image par requête ; 0 = image native
    api: str = "auto"              # auto | natif (/api/v1/chat) | openai (/v1/chat/completions)
    methode: str = "auto"          # auto | hybride | vlm : hybride = mise en page + Tesseract + feuille de recadrages lue par le modèle
    tesseract: str = ""            # chemin de tesseract(.exe) ; vide = automatique
    tessdata: str = ""             # dossier des modèles Tesseract (ex. tessdata_best) ; vide = installation par défaut
    tess_lang: str = "fra"
    reflexion: str = "auto"        # auto | non | oui
    budget_reflexion: int = 0      # jetons de réflexion tolérés ; 0 = automatique
    temperature: float | None = None   # None = 0,3 sans réflexion, 0,6 avec
    top_p: float | None = None         # None = 0,8 sans réflexion, 0,95 avec
    top_k: int = 20
    repeat_penalty: float = 0.0
    max_tokens: int = 8000         # jetons de sortie max par requête, réflexion comprise (borné par le contexte)
    timeout: int = 1200            # durée totale max d'une requête (s)
    stall: int = 300               # silence max entre deux jetons (s)
    attente: int = 900             # attente max du premier octet, c.-à-d. du préremplissage de l'image (s)
    suivi: int = 20                # intervalle (s) des lignes de progression pendant une requête (0 = aucune)
    suivi_ui: float = 0            # intervalle (s) des mises à jour destinées à une interface (0 = aucune)
    log_niveau: str = "DEBUG"      # niveau écrit dans le fichier de log : DEBUG | INFO
    essais: int = 3
    corps_extra: dict = field(default_factory=dict)
    prompt: str = PROMPT
    refaire: bool = False
    sauver_images: bool = False
    ancien: str = ""
    export_tous: int = 10          # export intermédiaire tous les N pages terminées (0 = seulement à la fin)
    sans_llm: bool = False         # --export : reconstruit les fichiers depuis le cache, sans contacter LM Studio
    cle_api: str = ""
    interrompu: bool = False       # résultats d'exécution (lus par main)
    fatal: str = ""


class ErreurFatale(Exception):
    """Inutile de continuer (serveur absent, modèle sans vision, contexte trop court...) : on arrête tout le lot."""


class ErreurLLM(Exception):
    """Échec récupérable d'une page (délai dépassé, réponse inexploitable...)."""


class ErreurReflexion(ErreurLLM):
    """LM Studio refuse le réglage de réflexion (« reasoning ») pour ce modèle."""


class Interrompu(Exception):
    """Ctrl+C ou arrêt demandé : on sort proprement, sans compter ça comme un échec de page."""


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
    plafond = cfg.max_cote or cfg.max_cote_auto
    if plafond and max(envoi.size) > plafond:
        r = plafond / max(envoi.size)
        envoi = envoi.resize((round(envoi.width * r), round(envoi.height * r)), Image.LANCZOS)
    encre = float((np.asarray(envoi.convert("L")) < 128).mean() * 100)
    buf = io.BytesIO()
    envoi.save(buf, "PNG", compress_level=3)
    return buf.getvalue(), encre


# ============================================================================
# 5) DIALOGUE AVEC LM STUDIO : streaming, arrêt anticipé, mesures
#    API native /api/v1/chat (LM Studio >= 0.4.0 : réflexion coupable, statistiques) ou, à défaut,
#    API compatible OpenAI /v1/chat/completions.
# ============================================================================

@dataclass
class InfoModele:
    id: str = ""                    # identifiant de l'instance CHARGÉE (c'est lui qu'on envoie dans « model »)
    cle: str = ""
    natif: bool = False             # API native /api/v1 disponible ?
    charge: bool = True
    ctx: int = 0                    # contexte chargé (0 = inconnu)
    parallel: int = 0               # « Max Concurrent Predictions » chargé (0 = inconnu)
    batch: int = 0                  # taille de lot d'évaluation
    flash: bool | None = None
    kv_gpu: bool | None = None
    vision: bool | None = None
    options_reflexion: list = field(default_factory=list)   # ex. ["off", "on"]
    quant: str = ""
    taille_go: float = 0.0
    format: str = ""

    @property
    def peut_couper_reflexion(self) -> bool:
        return self.natif and "off" in self.options_reflexion


def arrondi_1024(n: int) -> int:
    return ((int(n) + 1023) // 1024) * 1024


def _norm_id(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def trouver_pdf(chemin: str) -> str:
    """Le PDF donné n'existe pas (ex. « livre.pdf » alors que le fichier s'appelle lelivredesprenom0000geof.pdf) : on cherche un
    PDF dans le dossier courant puis dans celui du script, en préférant un nom contenant « prenom » ou « livre »."""
    if chemin and os.path.isfile(chemin):
        return chemin
    trouves: list = []
    for dossier in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        for f in sorted(glob.glob(os.path.join(dossier, "*.pdf"))):
            if f not in trouves:
                trouves.append(f)
    preferes = [f for f in trouves if re.search(r"prenom|livre", os.path.basename(f), re.I)]
    choix = preferes if len(preferes) == 1 else trouves if len(trouves) == 1 else []
    if choix:
        log(f"PDF « {chemin or '(non précisé)'} » introuvable : utilisation de « {choix[0]} »", "WARN")
        return choix[0]
    return chemin


def commande_lms(cle: str, ctx: int, parallel: int = 1) -> str:
    return f"lms load {cle} --context-length {ctx} --parallel {max(1, parallel)} --gpu max --identifier {cle} -y"


# ---------------------------------------------------------------- HTTP de base (http.client : contrôle fin des délais)

def _cible(cfg: Config) -> tuple[str, str, int, str]:
    brut = cfg.url if "://" in cfg.url else "http://" + cfg.url
    u = urllib.parse.urlsplit(brut)
    chemin = u.path.rstrip("/")
    if chemin.endswith("/v1"):
        chemin = chemin[:-3]
    schema = u.scheme or "http"
    return schema, u.hostname or "localhost", u.port or (443 if schema == "https" else 80), chemin


def _entetes(cfg: Config) -> dict:
    cle = cfg.cle_api or os.environ.get("LM_API_TOKEN", "") or "lm-studio"
    return {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + cle}


def _connexion(cfg: Config, timeout: float):
    schema, hote, port, _ = _cible(cfg)
    cls = http.client.HTTPSConnection if schema == "https" else http.client.HTTPConnection
    return cls(hote, port, timeout=timeout)


def _chemin(cfg: Config, suite: str) -> str:
    return _cible(cfg)[3] + suite


def _msg_injoignable(cfg: Config, e) -> str:
    return (f"Serveur LM Studio injoignable sur {cfg.url} ({e}). Lance LM Studio, onglet « Developer » -> "
            "« Start Server », et charge le modèle.")


def _classer_http(statut: int, texte: str):
    """Transforme une réponse HTTP en erreur : fatale (inutile de continuer) ou récupérable (page suivante)."""
    t = texte[:600]
    bas = t.lower()
    if statut in (401, 403):
        raise ErreurFatale(f"LM Studio refuse l'accès (HTTP {statut}) : l'authentification de l'API est activée. Crée un jeton "
                           f"d'API dans les réglages du serveur LM Studio et passe-le avec --cle-api (ou la variable LM_API_TOKEN). Détail : {t}")
    parle_image = any(k in bas for k in ("image", "vision", "multimodal", "mmproj", "modality"))
    refuse = any(k in bas for k in ("not support", "unsupported", "does not accept", "not a vision", "no vision",
                                    "cannot process", "mmproj", "multimodal", "modality"))
    if statut in (400, 422) and parle_image and refuse:
        raise ErreurFatale(
            "Le modèle chargé n'accepte pas d'image (module vision absent ?). Dans LM Studio, télécharge aussi le "
            f"fichier vision (mmproj) du modèle, ou choisis une variante « vision ». Détail : {t}")
    if statut in (400, 422) and "reasoning" in bas:
        raise ErreurReflexion(f"réglage de réflexion refusé par le modèle : {t}")
    if statut == 404 or ("model" in bas and ("not found" in bas or "not loaded" in bas or "no models" in bas)):
        raise ErreurFatale(f"Modèle introuvable / non chargé dans LM Studio (HTTP {statut}) : {t}")
    if "context" in bas and ("length" in bas or "size" in bas or "overflow" in bas):
        raise ErreurFatale("Contexte insuffisant : recharge le modèle dans LM Studio avec un contexte d'au moins "
                           f"16 384 jetons (voir --diagnostic). Détail : {t}")
    raise ErreurLLM(f"HTTP {statut} : {t}")


def _requete_simple(cfg: Config, methode: str, suite: str, corps=None, timeout: float = 30):
    """Requête non-streaming -> (statut HTTP, texte). Serveur injoignable = erreur fatale."""
    conn = _connexion(cfg, timeout)
    try:
        try:
            conn.connect()
        except OSError as e:
            raise ErreurFatale(_msg_injoignable(cfg, e))
        data = json.dumps(corps).encode("utf-8") if corps is not None else None
        try:
            conn.request(methode, _chemin(cfg, suite), body=data, headers=_entetes(cfg))
            resp = conn.getresponse()
            brut = resp.read()
        except (socket.timeout, TimeoutError):
            raise ErreurLLM(f"délai dépassé ({timeout}s)")
        except (OSError, http.client.HTTPException) as e:
            raise ErreurLLM(f"connexion interrompue : {e}")
        return resp.status, brut.decode("utf-8", "replace")
    finally:
        conn.close()


def _json(cfg: Config, methode: str, suite: str, corps=None, timeout: float = 30) -> dict:
    statut, texte = _requete_simple(cfg, methode, suite, corps, timeout)
    if statut >= 400:
        _classer_http(statut, texte)
    try:
        return json.loads(texte)
    except ValueError:
        raise ErreurLLM(f"réponse non-JSON du serveur : {texte[:200]!r}")


# ---------------------------------------------------------------- modèle chargé : identifiant, contexte, capacités

def detecter_api(cfg: Config) -> str:
    if cfg.api in ("natif", "openai"):
        return cfg.api
    statut, texte = _requete_simple(cfg, "GET", "/api/v1/models", None, 15)
    if statut in (401, 403):
        _classer_http(statut, texte)                  # lève ErreurFatale : inutile de se rabattre sur l'API OpenAI
    return "natif" if statut == 200 else "openai"


def _info_natif(m: dict) -> InfoModele:
    inst = (m.get("loaded_instances") or [None])[0]
    cap = m.get("capabilities") or {}
    raison = cap.get("reasoning") or m.get("reasoning") or {}
    q = m.get("quantization") or {}
    bits = q.get("bits_per_weight")
    info = InfoModele(natif=True, cle=m.get("key", ""), charge=bool(inst), vision=cap.get("vision"),
                      options_reflexion=[str(o) for o in (raison.get("allowed_options") or [])],
                      quant=" ".join(x for x in (str(q.get("name") or ""), f"({bits} bits)" if bits else "") if x),
                      taille_go=round((m.get("size_bytes") or 0) / 1e9, 1), format=m.get("format") or "")
    if inst:
        c = inst.get("config") or {}
        info.id = inst.get("id") or info.cle
        info.ctx = int(c.get("context_length") or 0)
        info.parallel = int(c.get("parallel") or 0)
        info.batch = int(c.get("eval_batch_size") or 0)
        info.flash = c.get("flash_attention")
        info.kv_gpu = c.get("offload_kv_cache_to_gpu")
    return info


def resoudre_modele(cfg: Config) -> InfoModele:
    """Retrouve le modèle CHARGÉ dans LM Studio (identifiant exact, contexte, capacités). Il diffère souvent du nom
    affiché ; la correspondance est approchée. Un modèle non chargé est une erreur fatale : l'envoyer tel quel
    déclencherait un chargement avec les réglages par défaut (contexte minuscule)."""
    voulu = cfg.modele.strip().lower()
    cle = _norm_id(voulu.split("/")[-1])
    if detecter_api(cfg) == "natif":
        d = _json(cfg, "GET", "/api/v1/models")
        llms = [m for m in d.get("models", []) if m.get("type", "llm") == "llm"]
        if not llms:
            raise ErreurFatale("LM Studio ne signale aucun modèle de langage : télécharge puis charge le modèle de vision.")

        def noms(m):
            return [m.get("key", ""), m.get("display_name", "")] + [i.get("id", "") for i in m.get("loaded_instances") or []]

        exact = [m for m in llms if voulu and any(str(n).lower() == voulu for n in noms(m))]
        flou = [m for m in llms if cle and any(cle in _norm_id(n) for n in noms(m))]
        candidats = exact or flou
        charges = [m for m in candidats if m.get("loaded_instances")]
        choix = (charges or candidats or [None])[0]
        if choix is None:
            seuls = [m for m in llms if m.get("loaded_instances")]
            if len(seuls) != 1:
                raise ErreurFatale(f"Modèle « {cfg.modele} » introuvable. Disponibles : {[m.get('key') for m in llms]}. Précise --modele.")
            choix = seuls[0]
            log(f"modèle « {cfg.modele} » introuvable ; le seul modèle chargé est utilisé : « {choix.get('key')} »")
        elif len(candidats) > 1:
            log(f"plusieurs modèles correspondent à « {cfg.modele} » : {[m.get('key') for m in candidats]} -> « {choix.get('key')} » "
                "(précise --modele pour en choisir un autre)")
        info = _info_natif(choix)
        if not info.charge:
            raise ErreurFatale(f"Le modèle « {info.cle} » n'est pas chargé dans LM Studio. Charge-le avec un contexte suffisant, par exemple : "
                               f"{commande_lms(info.cle, 16384)}")
        if info.vision is False:
            raise ErreurFatale(f"Le modèle « {info.cle} » ne déclare pas de vision : il ne peut pas lire d'image.")
        return info
    # API compatible OpenAI : pas de détails sur la configuration de chargement
    d = _json(cfg, "GET", "/v1/models")
    ids = [m.get("id", "") for m in d.get("data", []) if m.get("id") and "embed" not in m["id"].lower()]
    if not ids:
        raise ErreurFatale("LM Studio ne signale aucun modèle : charge le modèle de vision, puis relance.")
    exact = [i for i in ids if i.lower() == voulu]
    flou = [i for i in ids if cle and cle in _norm_id(i)]
    if exact:
        choix = exact[0]
    elif flou:
        choix = flou[0]
        log(f"modèle « {cfg.modele} » -> utilise « {choix} »" + (f" (autres candidats : {flou[1:]})" if len(flou) > 1 else ""))
    elif len(ids) == 1:
        choix = ids[0]
        log(f"modèle « {cfg.modele} » introuvable ; le seul modèle disponible est utilisé : « {choix} »")
    else:
        raise ErreurFatale(f"Modèle « {cfg.modele} » introuvable. Disponibles : {ids}. Précise --modele.")
    return InfoModele(id=choix, cle=choix, natif=False)


# ---------------------------------------------------------------- détection de boucle

_NOM_JSON = re.compile(r'"orthographes"\s*:\s*\[\s*"([^"]{2,})"')


def detecter_boucle_entrees(texte: str) -> bool:
    """Vrai si un même prénom revient 3 fois dans la réponse JSON : jamais légitime sur une page, toujours une boucle
    (p. 98 du run du 20 oct. 2026 : 6 noms répétés pendant 8 000 jetons / 550 s, 86 entrées fantômes)."""
    comptes: dict = {}
    for n in _NOM_JSON.findall(texte):
        n = n.strip().lower()
        comptes[n] = comptes.get(n, 0) + 1
        if comptes[n] >= 3:
            return True
    return False


def detecter_boucle(texte: str, prose: bool = False) -> bool:
    """Vrai si la fin du texte n'est plus qu'une répétition : un même bloc répété (≥ 120 caractères au total),
    ou, pour de la prose (réflexion), des suites de 4 mots massivement déjà vues."""
    if len(texte) < 400:
        return False
    fin = texte[-9000:]
    m = len(fin)
    for p in range(8, min(3000, m // 3) + 1):               # un cycle de plusieurs entrées JSON fait 1 000-2 500 caractères
        reps = max(3, -(-120 // p))
        if m >= p * reps and fin.endswith(fin[-p:] * reps):
            return True
    if prose and m >= 1200:
        mots = fin.split()[-300:]
        total = len(mots) - 3
        if total >= 120:
            vus, dup = set(), 0
            for i in range(total):
                g = tuple(mots[i:i + 4])
                if g in vus:
                    dup += 1
                else:
                    vus.add(g)
            if dup / total > 0.6:
                return True
    return False


# ---------------------------------------------------------------- corps des requêtes

def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _corps_natif(cfg, info, png, systeme, texte, temperature, top_p, max_tokens, penser, stream) -> dict:
    entree = []
    if png:
        entree.append({"type": "image", "data_url": _data_url(png)})
    entree.append({"type": "text", "content": texte})
    corps = {"model": info.id, "input": entree, "stream": stream, "store": False, "max_output_tokens": int(max_tokens)}
    if systeme:
        corps["system_prompt"] = systeme
    if temperature is not None:
        corps["temperature"] = temperature
    if top_p:
        corps["top_p"] = top_p
    if cfg.top_k:
        corps["top_k"] = cfg.top_k
    if cfg.repeat_penalty:
        corps["repeat_penalty"] = cfg.repeat_penalty
    if penser is False and "off" in info.options_reflexion:
        corps["reasoning"] = "off"
    elif penser is True and "on" in info.options_reflexion:
        corps["reasoning"] = "on"
    corps.update(cfg.corps_extra)
    return corps


def _corps_openai(cfg, info, png, systeme, texte, temperature, top_p, max_tokens, penser, stream) -> dict:
    contenu = []
    if png:
        contenu.append({"type": "image_url", "image_url": {"url": _data_url(png)}})
    contenu.append({"type": "text", "text": texte})
    messages = ([{"role": "system", "content": systeme}] if systeme else []) + [{"role": "user", "content": contenu}]
    corps = {"model": info.id, "messages": messages, "max_tokens": int(max_tokens), "stream": stream}
    if temperature is not None:
        corps["temperature"] = temperature
    if top_p:
        corps["top_p"] = top_p
    if cfg.top_k:
        corps["top_k"] = cfg.top_k
    if cfg.repeat_penalty:
        corps["repeat_penalty"] = cfg.repeat_penalty
    if stream:
        corps["stream_options"] = {"include_usage": True}
    if penser is False:
        corps["chat_template_kwargs"] = {"enable_thinking": False}     # sans effet garanti : le budget de réflexion veille
    corps.update(cfg.corps_extra)
    return corps


def _corps(cfg, info, *args) -> dict:
    return (_corps_natif if info.natif else _corps_openai)(cfg, info, *args)


# ---------------------------------------------------------------- flux SSE -> événements normalisés

def _lignes_sse(resp):
    """(événement, données) pour chaque message d'un flux Server-Sent Events."""
    evenement, donnees = "", []
    rebut = getattr(resp, "rebut", None)
    while True:
        ligne = resp.readline()
        if not ligne:
            if donnees:
                yield evenement, "\n".join(donnees)
            return
        ligne = ligne.decode("utf-8", "replace").rstrip("\r\n")
        if ligne == "":
            if donnees:
                yield evenement, "\n".join(donnees)
            evenement, donnees = "", []
        elif ligne.startswith(":"):
            continue
        elif ligne.startswith("event:"):
            evenement = ligne[6:].strip()
        elif ligne.startswith("data:"):
            donnees.append(ligne[5:].lstrip())
        elif rebut is not None and len(rebut) < 5:
            rebut.append(ligne[:200])                # ni événement ni donnée : probablement pas du SSE


def _flux_natif(resp):
    """Événements de /api/v1/chat -> ('prefill', 0..1) | ('reflexion', texte) | ('reponse', texte) | ('erreur', msg) | ('fin', résultat)."""
    for ev, data in _lignes_sse(resp):
        try:
            d = json.loads(data)
        except ValueError:
            continue
        t = d.get("type") or ev
        if t == "reasoning.delta":
            yield ("reflexion", d.get("content") or "")
        elif t == "message.delta":
            yield ("reponse", d.get("content") or "")
        elif t == "prompt_processing.progress":
            yield ("prefill", float(d.get("progress") or 0))
        elif t == "prompt_processing.start":
            yield ("prefill", 0.0)
        elif t == "error":
            e = d.get("error") or {}
            yield ("erreur", e.get("message") or str(e) or "erreur inconnue")
        elif t == "chat.end":
            yield ("fin", d.get("result") or {})


class PhaseOpenAI:
    """Sépare réflexion et réponse dans le champ « content » de l'API OpenAI, quand LM Studio ne les distingue pas :
    balise <think> ouverte, ou ouverte implicitement par le modèle (le texte libre précède alors « </think> »)."""

    def __init__(self):
        self.etat, self.tete, self.tampon = "debut", "", ""

    def _reflexion(self, t: str):
        self.tampon += t
        i = self.tampon.find("</think>")
        if i < 0:
            sortie, self.tampon = self.tampon[:-7], self.tampon[-7:]      # la balise peut être coupée entre deux deltas
            return [("reflexion", sortie)] if sortie else []
        avant, apres = self.tampon[:i], self.tampon[i + 8:]
        self.tampon, self.etat = "", "reponse"
        return ([("reflexion", avant)] if avant else []) + ([("reponse", apres)] if apres else [])

    def separer(self, t: str):
        if self.etat == "reponse":
            return [("reponse", t)]
        if self.etat == "reflexion":
            return self._reflexion(t)
        self.tete += t
        s = self.tete.lstrip()
        if s.startswith("<think>"):
            self.etat, reste, self.tete = "reflexion", s[len("<think>"):], ""
            return self._reflexion(reste) if reste else []
        if s.startswith(("{", "```", "[")):
            self.etat, reste, self.tete = "reponse", self.tete, ""
            return [("reponse", reste)]
        if len(s) >= 12:
            self.etat, reste, self.tete = "reflexion", self.tete, ""
            return self._reflexion(reste)
        return []

    def vider(self):
        reste = self.tete + self.tampon
        self.tete = self.tampon = ""
        if not reste:
            return []
        return [("reflexion" if self.etat == "reflexion" else "reponse", reste)]


def _flux_openai(resp):
    phase, fin_raison, usage = PhaseOpenAI(), "", None
    for _, data in _lignes_sse(resp):
        if data.strip() == "[DONE]":
            break
        try:
            d = json.loads(data)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("error"):
            e = d["error"]
            yield ("erreur", e.get("message", str(e)) if isinstance(e, dict) else str(e))
            return
        if d.get("usage"):
            usage = d["usage"]
        for ch in d.get("choices") or []:
            delta = ch.get("delta") or {}
            r = delta.get("reasoning_content") or delta.get("reasoning") or ""
            if r:
                yield ("reflexion", r)
            c = delta.get("content") or ""
            if c:
                for typ, t in phase.separer(c):
                    yield (typ, t)
            if ch.get("finish_reason"):
                fin_raison = ch["finish_reason"]
    for typ, t in phase.vider():
        yield (typ, t)
    u = usage or {}
    yield ("fin", {"finish": fin_raison, "stats": {"input_tokens": u.get("prompt_tokens"),
                                                   "total_output_tokens": u.get("completion_tokens")}})


# ---------------------------------------------------------------- une requête en streaming

def _ouvrir_flux(cfg: Config, info: InfoModele, corps: dict):
    """Ouvre la requête de chat en streaming. -> (connexion, réponse, socket). Les erreurs HTTP sont classées."""
    conn = _connexion(cfg, cfg.attente)
    try:
        conn.connect()
    except OSError as e:
        conn.close()
        raise ErreurFatale(_msg_injoignable(cfg, e))
    sock = conn.sock
    try:
        suite = "/api/v1/chat" if info.natif else "/v1/chat/completions"
        conn.request("POST", _chemin(cfg, suite), body=json.dumps(corps).encode("utf-8"), headers=_entetes(cfg))
        resp = conn.getresponse()
    except (socket.timeout, TimeoutError):
        conn.close()
        raise ErreurLLM(f"aucune réponse du serveur après {cfg.attente}s (préremplissage de l'image trop long ?)")
    except (OSError, http.client.HTTPException) as e:
        conn.close()
        raise ErreurLLM(f"connexion interrompue : {e}")
    if resp.status >= 400:
        texte = resp.read().decode("utf-8", "replace")
        conn.close()
        _classer_http(resp.status, texte)
    resp.rebut = []
    try:
        sock.settimeout(cfg.stall)          # désormais : silence maximal toléré entre deux jetons
    except OSError:
        pass
    return conn, resp, sock


def appeler_llm(cfg: Config, info: InfoModele, png: bytes | None, systeme: str | None, texte: str, *,
                temperature: float | None, top_p: float | None, max_tokens: int, penser: bool, budget: int = 0,
                stop: threading.Event | None = None, suivi=None, pas_suivi: float = 0) -> dict:
    """Une requête en STREAMING. S'interrompt (et ferme la connexion : LM Studio abandonne la génération) dès qu'on
    détecte une boucle, une réflexion qui dépasse `budget` jetons, un silence, un délai ou une interruption.
    -> dict : content, reasoning, finish, abandon, erreur, tokens, tokens_reflexion, tokens_entree, ttft, tps, duree."""
    t0 = time.time()
    corps = _corps(cfg, info, png, systeme, texte, temperature, top_p, max_tokens, penser, True)
    conn, resp, sock = _ouvrir_flux(cfg, info, corps)
    contenu, pensee = [], []
    fin_ref = fin_rep = ""
    n_ref = n_rep = 0
    t_premier, t_suivi, prefill = None, t0, 0.0
    abandon, erreur, fin = "", "", {}
    n_utiles = 0
    try:
        for typ, val in (_flux_natif(resp) if info.natif else _flux_openai(resp)):
            now = time.time()
            n_utiles += typ != "fin"
            if stop is not None and stop.is_set():
                abandon = "interruption"
                break
            if typ == "prefill":
                prefill = val
            elif typ in ("reflexion", "reponse"):
                if t_premier is None:
                    t_premier = now
                if typ == "reflexion":
                    pensee.append(val)
                    n_ref += 1
                    fin_ref = (fin_ref + val)[-4000:]
                    if budget and n_ref > budget:
                        abandon = "budget_reflexion"
                    elif n_ref % 48 == 0 and detecter_boucle(fin_ref, prose=True):
                        abandon = "boucle_reflexion"
                else:
                    contenu.append(val)
                    n_rep += 1
                    fin_rep = (fin_rep + val)[-9000:]
                    if n_rep % 48 == 0 and (detecter_boucle(fin_rep) or detecter_boucle_entrees(fin_rep)):
                        abandon = "boucle_reponse"
                if not abandon and n_ref + n_rep > max_tokens + 200:
                    abandon = "longueur"
            elif typ == "erreur":
                erreur = val
                break
            elif typ == "fin":
                fin = val
            if not abandon and now - t0 > cfg.timeout:
                abandon = "delai"
            if abandon:
                break
            if suivi is not None and pas_suivi and now - t_suivi >= pas_suivi:
                t_suivi = now
                suivi({"elapsed": now - t0, "tokens": n_ref + n_rep, "tokens_reflexion": n_ref, "prefill": prefill,
                       "tps": (n_ref + n_rep) / (now - t_premier) if t_premier and now - t_premier > 1 else 0.0})
    except (socket.timeout, TimeoutError):
        abandon = abandon or "silence"
    except (OSError, http.client.HTTPException) as e:
        if not abandon:
            erreur = f"connexion interrompue : {e}"
    finally:
        for fermer in (resp.close, conn.close, sock.close):
            try:
                fermer()
            except Exception:                                    # noqa: BLE001 : la fermeture ne doit jamais échouer bruyamment
                pass
    if not abandon and not erreur and n_utiles == 0:
        erreur = ("réponse sans aucun événement reconnu (Content-Type : " + (resp.getheader("Content-Type") or "?") + ")"
                  + (f" ; début de la réponse : {resp.rebut!r}" if getattr(resp, "rebut", None) else "")
                  + ". Version de LM Studio différente ? essaie --api openai.")
    t_fin = time.time()
    st = fin.get("stats") or {}
    tokens = n_ref + n_rep
    sortie = int(st.get("total_output_tokens") or tokens)
    tok_ref = st.get("reasoning_output_tokens")
    tps, ttft = st.get("tokens_per_second"), st.get("time_to_first_token_seconds")
    if tps is None and t_premier is not None and t_fin - t_premier > 0.5 and tokens > 3:
        tps = tokens / (t_fin - t_premier)
    if ttft is None and t_premier is not None:
        ttft = t_premier - t0
    if abandon:
        finish = "abandon:" + abandon
    elif erreur:
        finish = "erreur"
    elif fin.get("finish"):
        finish = fin["finish"]
    else:
        finish = "length" if sortie >= max_tokens - 2 else "stop"
    return {"content": "".join(contenu), "reasoning": "".join(pensee), "finish": finish, "abandon": abandon,
            "erreur": erreur, "tokens": sortie, "tokens_reflexion": int(tok_ref) if tok_ref is not None else n_ref,
            "tokens_entree": int(st.get("input_tokens") or 0), "ttft": ttft,
            "tps": float(tps) if tps else None, "duree": t_fin - t0}


def sonder_tokens(cfg: Config, info: InfoModele, png: bytes | None, systeme: str | None, texte: str) -> dict:
    """Nombre de jetons d'entrée d'une requête (sans générer) : {'entree': n, 'ttft': secondes | None}."""
    if info.natif:
        corps = _corps_natif(cfg, info, png, systeme, texte, 0.0, None, 1, False, False)
        st = _json(cfg, "POST", "/api/v1/chat", corps, timeout=cfg.attente).get("stats") or {}
        return {"entree": int(st.get("input_tokens") or 0), "ttft": st.get("time_to_first_token_seconds")}
    corps = _corps_openai(cfg, info, png, systeme, texte, 0.0, None, 1, False, False)
    u = _json(cfg, "POST", "/v1/chat/completions", corps, timeout=cfg.attente).get("usage") or {}
    return {"entree": int(u.get("prompt_tokens") or 0), "ttft": None}


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
    coupee = finish == "length" or finish.startswith("abandon")
    if coupee:       # réponse coupée : ne jamais chercher dans la réflexion (elle cite l'exemple du prompt)
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
    if coupee:
        return None, [f"réponse coupée avant la fin ({finish}) : réflexion trop longue ou boucle ; "
                      "voir --reflexion, --max-tokens et le contexte du modèle dans LM Studio"], False
    return None, [f"aucun JSON exploitable (finish={finish or '?'}, {len(content)} car.)"], False


# ============================================================================
# 7) NORMALISATION DES CHAMPS
# ============================================================================

TATWIL_ETC = re.compile(r"[\u0640\u200c\u200d\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069]")
FORMES_PRESENTATION = re.compile(r"[\uFB50-\uFDFF\uFE70-\uFEFF]")
HARAKAT = "\u064B-\u065F\u0670"


try:
    import verif_arabe as VA
except ImportError:                                   # module facultatif
    VA = None


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
    return dedoublonner([n for n in (normaliser_entree(x) for x in brut) if n])


def dedoublonner(entrees: list[dict]) -> list[dict]:
    """Retire les entrées strictement répétées sur une page (mêmes graphies, même début de définition) : artefact de boucle."""
    vus, sortie = set(), []
    for e in entrees:
        cle = (tuple(o.lower() for b in e["prenoms"] for o in b["orthographes"]), re.sub(r"\W+", "", e["description"].lower())[:60])
        if cle in vus:
            continue
        vus.add(cle)
        sortie.append(e)
    return sortie


def est_exemple(entrees: list[dict]) -> bool:
    """Le modèle a-t-il renvoyé tel quel l'exemple du prompt ?"""
    return bool(entrees) and all(e["description"].strip() == EXEMPLE_DESC for e in entrees)


# ============================================================================
# 8) TRAITEMENT D'UNE PAGE : état partagé, cache, calibrage, fils d'exécution
# ============================================================================

def fmt_duree(s: float) -> str:
    s = int(max(0, s))
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60:02d}"
    if s >= 60:
        return f"{s // 60} min"
    return f"{s} s"


class Controle:
    """Pont entre le moteur et une interface (TUI) : arrêt demandé, activité en cours, progression, GPU."""

    def __init__(self):
        self.stop = threading.Event()
        self.verrou = threading.Lock()
        self.actifs: dict = {}
        self.etat = "attente"          # attente | calibrage | en cours | termine | interrompu | erreur
        self.total = self.fait = self.echecs = 0
        self.debut = 0.0
        self.reste_s: float | None = None
        self.gpu: dict | None = None
        self.message = ""

    def maj_actif(self, ident: str, **champs) -> None:
        with self.verrou:
            self.actifs.setdefault(ident, {"debut": time.time()}).update(champs)

    def fin_actif(self, ident: str) -> None:
        with self.verrou:
            self.actifs.pop(ident, None)

    def instantane(self) -> dict:
        with self.verrou:
            return {"etat": self.etat, "total": self.total, "fait": self.fait, "echecs": self.echecs, "debut": self.debut,
                    "reste_s": self.reste_s, "gpu": dict(self.gpu) if self.gpu else None, "message": self.message,
                    "actifs": {k: dict(v) for k, v in self.actifs.items()}}


class Etat:
    """État partagé par les fils : modèle, budgets, réglage de la réflexion (appris en route), arrêt demandé."""

    def __init__(self, cfg: Config, info: InfoModele, controle: Controle | None = None):
        self.cfg, self.info = cfg, info
        self.controle = controle
        self.stop = controle.stop if controle is not None else threading.Event()
        self.verrou = threading.Lock()
        self.sortie_max = cfg.max_tokens
        self.tokens_entree = 0
        self.peut_couper = info.peut_couper_reflexion
        self.penser = cfg.reflexion == "oui" or (cfg.reflexion == "auto" and not self.peut_couper)
        self.coupures_ignorees = 0
        self.attendus: dict = {}             # page -> nombre d'entrées vu par la mise en page (contrôle de complétude)
        self.hybride = False                 # lecture hybride (mise en page + Tesseract) disponible et demandée

    def budget(self, penser: bool) -> int:
        """Jetons de réflexion tolérés avant d'abandonner la requête."""
        if not penser:
            return 200                    # réflexion « coupée » : au-delà, LM Studio ignore le réglage
        if self.cfg.budget_reflexion:
            return self.cfg.budget_reflexion
        return max(1000, self.sortie_max - 2500)

    def coupure_ignoree(self) -> None:
        """Le modèle a réfléchi malgré « reasoning: off ». Au 2e cas, on cesse d'essayer pour toute la suite."""
        with self.verrou:
            self.coupures_ignorees += 1
            if self.coupures_ignorees == 2 and self.peut_couper:
                self.peut_couper, self.penser = False, True
                log("LM Studio ignore la désactivation de la réflexion : les pages seront traitées AVEC réflexion (bornée). "
                    "Pour la couper, décoche « Enable Thinking » dans les réglages du modèle dans LM Studio.")

    def sans_parametre_reflexion(self) -> None:
        with self.verrou:
            if self.peut_couper:
                log("LM Studio refuse le réglage de réflexion pour ce modèle : il n'est plus envoyé.")
            self.info.options_reflexion = []
            self.peut_couper, self.penser = False, True


# ---------------------------------------------------------------- cache

def id_chunk(page: int, bande: int, nb: int) -> str:
    return f"p{page:03d}" if nb == 1 else f"p{page:03d}_b{bande}"


def chemin_cache(cfg: Config, page: int, bande: int) -> str:
    return os.path.join(cfg.sortie, "cache", id_chunk(page, bande, cfg.decoupe) + ".json")


def empreinte(cfg: Config) -> str:
    """Change dès que le résultat en cache ne serait plus celui que produirait cette configuration."""
    cle = json.dumps([cfg.prompt, TEXTE_UTILISATEUR, cfg.modele, cfg.decoupe, cfg.nettoyage, cfg.max_cote, cfg.tokens_image, cfg.methode],
                     ensure_ascii=False)
    return hashlib.sha1(cle.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def lire_cache(cfg: Config, page: int, bande: int, tolerer: bool = False):
    """-> (résultat réutilisable | None, périmé ?). `tolerer` (--export) : un résultat périmé, calculé avec une autre
    configuration, est rendu quand même (avec périmé = True) : mieux vaut l'exporter que le perdre."""
    p = chemin_cache(cfg, page, bande)
    if not os.path.exists(p):
        return None, False
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None, False
    if d.get("empreinte") != empreinte(cfg):
        return (d, True) if tolerer else (None, True)     # autre prompt / modèle / découpage / nettoyage / image : périmé
    if d.get("statut") == "partiel" and cfg.max_tokens > d.get("max_tokens", 0) and not tolerer:
        return None, False                           # on retente avec un budget plus grand
    return d, False


def ecrire_atomique(chemin: str, texte: str) -> None:
    os.makedirs(os.path.dirname(chemin) or ".", exist_ok=True)
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(texte)
    os.replace(tmp, chemin)


# ---------------------------------------------------------------- calibrage : jetons d'entrée, taille de l'image, budget de sortie

def budgeter(cfg: Config, etat: Etat) -> None:
    """Borne la sortie par le contexte chargé (image + consigne + sortie ≤ contexte, pour chaque requête simultanée)."""
    ctx, entree = etat.info.ctx, etat.tokens_entree
    if not ctx or not entree:
        return
    marge, voulu = 128, min(cfg.max_tokens, 3000)
    p = max(1, cfg.paralleles)
    while p > 1 and ctx // p - entree - marge < voulu:
        p -= 1
    if p != cfg.paralleles:
        log(f"contexte chargé : {ctx} jetons. {cfg.paralleles} requêtes simultanées ({entree} jetons d'entrée chacune) n'y "
            f"tiennent pas : {p} à la fois. Augmente le contexte pour paralléliser.")
        cfg.paralleles = p
    dispo = ctx // p - entree - marge
    if dispo < 1500:
        raise ErreurFatale(
            f"Contexte trop court : {entree} jetons d'entrée (image + consigne) pour un contexte chargé de {ctx}. Il reste "
            f"{max(0, dispo)} jetons pour la réponse (1 500 au minimum). Recharge le modèle avec un contexte d'au moins "
            f"{arrondi_1024(entree + cfg.max_tokens + marge)} : {commande_lms(etat.info.cle or cfg.modele, arrondi_1024(entree + cfg.max_tokens + marge), p)} "
            "(ou réduis l'image : --tokens-image 1500).")
    etat.sortie_max = min(cfg.max_tokens, dispo)
    if etat.sortie_max < cfg.max_tokens:
        log(f"budget de sortie limité à {etat.sortie_max} jetons par le contexte chargé ({ctx}) ; "
            f"recharge avec un contexte de {arrondi_1024(entree + cfg.max_tokens + marge)} pour le lever.")
    if etat.penser and etat.sortie_max < 6000:
        log("⚠ réflexion autorisée avec moins de 6 000 jetons de sortie : elle risque de ne pas laisser la place à la réponse.")


def calibrer(cfg: Config, etat: Etat, chunk: tuple[int, int]) -> None:
    """Mesure les jetons d'entrée (consigne seule, puis consigne + image d'une vraie page), réduit l'image si elle dépasse
    la cible `--tokens-image`, puis borne le budget de sortie par le contexte chargé."""
    page, bande = chunk
    texte = TEXTE_UTILISATEUR + (NOTE_BANDE[bande] if cfg.decoupe > 1 else "")
    cfg.max_cote_auto = 0
    try:
        t_txt = sonder_tokens(cfg, etat.info, None, cfg.prompt, texte)["entree"]
        png, _ = preparer_image(cfg, page, bande)
        mesure = sonder_tokens(cfg, etat.info, png, cfg.prompt, texte)
    except ErreurLLM as e:
        log(f"calibrage impossible ({e}) : l'image et le budget de sortie ne sont pas ajustés.")
        return
    t_tot = mesure["entree"]
    if not t_tot:
        log("calibrage : le serveur ne renvoie pas le nombre de jetons d'entrée ; l'image et le budget ne sont pas ajustés.")
        return
    t_img = max(0, t_tot - t_txt)
    pre = f", préremplissage ≈ {t_tot / mesure['ttft']:.0f} jetons/s" if mesure.get("ttft") else ""
    log(f"jetons d'entrée mesurés : consigne ≈ {t_txt}, image ≈ {t_img}{pre}")
    if cfg.tokens_image and not cfg.max_cote and t_img > cfg.tokens_image * 1.15:
        w, h = Image.open(io.BytesIO(png)).size
        r = (cfg.tokens_image / t_img) ** 0.5
        cfg.max_cote_auto = int(max(w, h) * r)
        t_img2 = int(t_img * r * r)
        log(f"image réduite : plus grand côté {max(w, h)} -> {cfg.max_cote_auto} px (≈ {t_img2} jetons au lieu de {t_img}). "
            "`--tokens-image 0` garde l'image native.")
        t_tot = t_txt + t_img2
    etat.tokens_entree = t_tot
    budgeter(cfg, etat)


# ---------------------------------------------------------------- une page (ou demi-page)

def _suivi(ident: str, etat: "Etat", cfg: Config):
    """Rappel de progression d'une requête : alimente le registre d'activité (interface) et, tous les `cfg.suivi`
    secondes, une ligne de journal."""
    dernier = [time.time()]

    def f(s: dict) -> None:
        if etat.controle is not None:
            etat.controle.maj_actif(ident, tokens=s["tokens"], tokens_reflexion=s["tokens_reflexion"], tps=s["tps"],
                                    prefill=s["prefill"], elapsed=s["elapsed"])
        now = time.time()
        if cfg.suivi and now - dernier[0] >= cfg.suivi:
            dernier[0] = now
            pre = f", préremplissage {s['prefill'] * 100:.0f} %" if s["tokens"] == 0 and s["prefill"] else ""
            ref = f" dont {s['tokens_reflexion']} de réflexion" if s["tokens_reflexion"] else ""
            log(f"  {ident} en cours : {s['tokens']} jetons{ref}, {s['tps']:.1f} tok/s, {s['elapsed']:.0f}s{pre}")
    return f


def _journaliser_essai(ident: str, essai: int, penser: bool, temp: float, top_p, etat: "Etat", rep: dict, png: bytes) -> None:
    tps = round(rep["tps"], 1) if rep["tps"] else None
    ttft = round(rep["ttft"], 1) if rep["ttft"] else None
    debug(f"[{ident}] essai {essai} : penser={penser} T={temp:.2f} top_p={top_p} budget_reflexion={etat.budget(penser)} "
          f"max_tokens={etat.sortie_max} -> finish={rep['finish']} abandon={rep['abandon'] or '-'} jetons={rep['tokens']} "
          f"(réflexion {rep['tokens_reflexion']}) entrée={rep['tokens_entree']} tok/s={tps} ttft={ttft} durée={rep['duree']:.1f}s")
    if rep["abandon"] or rep["erreur"]:
        debug(f"[{ident}] fin de la réponse reçue : {rep['content'][-300:]!r} | fin de la réflexion : "
              f"{rep['reasoning'][-300:]!r} | erreur : {rep['erreur']!r}")
    evenement("requete", chunk=ident, essai=essai, penser=penser, temperature=round(temp, 2), top_p=top_p,
              budget_reflexion=etat.budget(penser), max_tokens=etat.sortie_max, finish=rep["finish"], abandon=rep["abandon"],
              erreur=rep["erreur"], tokens=rep["tokens"], tokens_reflexion=rep["tokens_reflexion"],
              tokens_entree=rep["tokens_entree"], tps=tps, ttft=ttft, duree=round(rep["duree"], 1), png_ko=round(len(png) / 1024))


def _traiter_chunk(cfg: Config, etat: Etat, page: int, bande: int) -> dict:
    t = time.time()
    ident = id_chunk(page, bande, cfg.decoupe)
    png, encre = preparer_image(cfg, page, bande)
    base = {"page": page, "bande": bande, "decoupe": cfg.decoupe, "nettoyage": cfg.nettoyage,
            "empreinte": empreinte(cfg), "modele": etat.info.id, "max_tokens": etat.sortie_max}
    chemin = chemin_cache(cfg, page, bande)
    if encre < SEUIL_BLANC:                      # page blanche : inutile (et risqué : le modèle inventerait) de l'envoyer
        res = {**base, "statut": "ok", "blanche": True, "avertissements": ["page blanche : aucune requête envoyée"],
               "duree": 0.0, "tokens": 0, "tokens_reflexion": 0, "tokens_entree": 0, "tps": None, "ttft": None,
               "penser": False, "entrees": []}
        ecrire_atomique(chemin, json.dumps(res, ensure_ascii=False, indent=1))
        return res
    if cfg.sauver_images:
        os.makedirs(os.path.join(cfg.sortie, "images"), exist_ok=True)
        with open(os.path.join(cfg.sortie, "images", ident + ".png"), "wb") as f:
            f.write(png)
    texte = TEXTE_UTILISATEUR + (NOTE_BANDE[bande] if cfg.decoupe > 1 else "")
    penser, derniere, meilleur = etat.penser, "?", None
    pas = min([x for x in (cfg.suivi, cfg.suivi_ui) if x] or [0])
    for essai in range(1, cfg.essais + 1):
        if etat.stop.is_set():
            raise Interrompu()
        temp = cfg.temperature if cfg.temperature is not None else (0.6 if penser else 0.3)
        temp = min(1.0, temp + 0.15 * (essai - 1))                   # une boucle ne se répète pas à l'identique
        top_p = cfg.top_p if cfg.top_p is not None else (0.95 if penser else 0.8)
        debut_essai = time.time()
        try:
            rep = appeler_llm(cfg, etat.info, png, cfg.prompt, texte, temperature=temp, top_p=top_p,
                              max_tokens=etat.sortie_max, penser=penser, budget=etat.budget(penser),
                              stop=etat.stop, suivi=_suivi(ident, etat, cfg), pas_suivi=pas)
        except ErreurReflexion as e:
            derniere = str(e)
            log(f"  {ident} essai {essai}/{cfg.essais} : {derniere}", "WARN")
            etat.sans_parametre_reflexion()
            penser = True
            continue
        except ErreurLLM as e:
            derniere = str(e)
            log(f"  {ident} essai {essai}/{cfg.essais} : {derniere}", "WARN")
            evenement("requete_erreur", chunk=ident, essai=essai, message=derniere, duree=round(time.time() - debut_essai, 1))
            continue
        _journaliser_essai(ident, essai, penser, temp, top_p, etat, rep, png)
        if rep["abandon"] == "interruption":
            raise Interrompu()
        if rep["erreur"]:
            derniere = rep["erreur"]
            log(f"  {ident} essai {essai}/{cfg.essais} : erreur du serveur : {derniere}", "WARN")
            try:
                _classer_http(400, rep["erreur"])                        # lève ErreurFatale si l'erreur est sans issue
            except ErreurReflexion:
                etat.sans_parametre_reflexion()
                penser = True
            except ErreurLLM:
                pass
            continue
        obj, avert, partiel = extraire_page_json(rep)
        if obj is None:
            derniere = (f"{rep['abandon'] or rep['finish']} après {rep['tokens']} jetons"
                        + (f" dont {rep['tokens_reflexion']} de réflexion" if rep["tokens_reflexion"] else ""))
            ecrire_atomique(os.path.join(cfg.sortie, "cache", "brut", f"{ident}_essai{essai}.txt"),
                            f"abandon={rep['abandon']} finish={rep['finish']} penser={penser}\n--- RÉPONSE ---\n{rep['content']}\n\n"
                            f"--- RÉFLEXION (fin) ---\n{rep['reasoning'][-4000:]}")
            log(f"  {ident} essai {essai}/{cfg.essais} sans résultat : {derniere} (sortie brute : cache/brut/{ident}_essai{essai}.txt)", "WARN")
            if not penser and rep["abandon"] == "budget_reflexion":      # il réfléchit malgré « off » -> on le laisse réfléchir
                etat.coupure_ignoree()
                penser = True
            elif penser and rep["abandon"] in ("budget_reflexion", "boucle_reflexion") and etat.peut_couper:
                penser = False                                          # la réflexion s'emballe -> sans réflexion
            continue
        entrees = normaliser_page(obj)
        if est_exemple(entrees):
            derniere = "le modèle a recopié l'exemple du prompt"
            log(f"  {ident} essai {essai}/{cfg.essais} : {derniere}", "WARN")
            continue
        if rep["abandon"]:
            avert = avert + [f"génération interrompue : {rep['abandon']}"]
        if not entrees and encre > 0.4:
            avert = avert + ["aucune entrée détectée sur une page non blanche"]
        res = {**base, "statut": "partiel" if partiel else "ok", "avertissements": avert,
               "duree": round(time.time() - t, 1), "tokens": rep["tokens"], "tokens_reflexion": rep["tokens_reflexion"],
               "tokens_entree": rep["tokens_entree"] or etat.tokens_entree,
               "tps": round(rep["tps"], 1) if rep["tps"] else None, "ttft": round(rep["ttft"], 1) if rep["ttft"] else None,
               "penser": penser, "entrees": entrees}
        if (partiel or (not entrees and encre > 0.4)) and essai < cfg.essais:
            if meilleur is None or len(entrees) > len(meilleur["entrees"]):
                meilleur = res                                          # on garde le meilleur résultat incomplet
            derniere = "réponse partielle" if partiel else "aucune entrée sur une page non blanche"
            log(f"  {ident} essai {essai}/{cfg.essais} : {derniere} ({len(entrees)} entrées conservées si rien de mieux)", "WARN")
            continue
        if meilleur is not None and len(meilleur["entrees"]) > len(entrees):
            res = meilleur
        res["duree"] = round(time.time() - t, 1)
        ecrire_atomique(chemin, json.dumps(res, ensure_ascii=False, indent=1))
        ecrire_atomique(os.path.join(cfg.sortie, "cache", "brut", ident + ".txt"), rep["content"])
        return res
    if meilleur is not None:
        meilleur["duree"] = round(time.time() - t, 1)
        ecrire_atomique(chemin, json.dumps(meilleur, ensure_ascii=False, indent=1))
        return meilleur
    raise ErreurLLM(derniere)


def preparer_hybride(cfg: Config) -> bool:
    """La lecture hybride (mise en page + Tesseract + feuille de recadrages) est-elle demandée ET possible ?"""
    if cfg.methode == "vlm":
        return False
    if cfg.decoupe != 1:
        if cfg.methode == "hybride":
            log("--decoupe 2 est incompatible avec la lecture hybride (qui analyse la page entière) : lecture par le modèle seul.", "WARN")
        return False
    try:
        import lecture_hybride as LH
        LH.configurer(cfg.tesseract, cfg.tessdata, cfg.tess_lang)
        ok, msg = LH.disponible()
    except ImportError as e:
        ok, msg = False, f"module manquant ({e})"
    if ok:
        log(f"lecture hybride : {msg}")
        return True
    if cfg.methode == "hybride":
        raise ErreurFatale(f"--methode hybride impossible : {msg}")
    log(f"lecture hybride indisponible ({msg}) : lecture par le modèle de vision seul.", "WARN")
    return False


def _traiter_page_hybride(cfg: Config, etat: Etat, page: int, local_seul: bool = False) -> dict | None:
    """Page entière : structure par traitement d'image, textes latins par Tesseract, arabe et têtes de définition lus par le
    modèle de vision sur UNE petite feuille de recadrages. Rend None pour demander le repli sur la lecture par le modèle seul."""
    import lecture_hybride as LH
    t = time.time()
    ident = id_chunk(page, 1, 1)
    net = nettoyer_page(image_native(cfg.pdf, page))
    encre = float((np.asarray(net.convert("L")) < 128).mean() * 100)
    base = {"page": page, "bande": 1, "decoupe": 1, "nettoyage": cfg.nettoyage, "empreinte": empreinte(cfg), "modele": etat.info.id,
            "max_tokens": etat.sortie_max, "methode": "hybride"}
    vide = {"tokens": 0, "tokens_reflexion": 0, "tokens_entree": 0, "tps": None, "ttft": None, "penser": False}
    chemin = chemin_cache(cfg, page, 1)
    if encre < SEUIL_BLANC:
        res = {**base, **vide, "statut": "ok", "blanche": True, "avertissements": ["page blanche : aucune requête envoyée"], "duree": 0.0, "entrees": []}
        ecrire_atomique(chemin, json.dumps(res, ensure_ascii=False, indent=1))
        return res
    try:
        lec = LH.lire_page(net)
    except Exception as e:                                       # noqa: BLE001 : Tesseract absent/planté : on se rabat sur le modèle de vision
        log_exception(f"{ident} : lecture locale (Tesseract) impossible", e)
        return None
    debug(f"[{ident}] lecture locale : {len(lec.entrees)} entrées, {lec.textes_libres} textes libres, problèmes={lec.problemes}, {time.time() - t:.1f}s")
    if lec.page_texte:
        res = {**base, **vide, "statut": "ok", "avertissements": ["page de texte courant (sans entrée) : aucune requête envoyée"],
               "duree": round(time.time() - t, 1), "entrees": []}
        ecrire_atomique(chemin, json.dumps(res, ensure_ascii=False, indent=1))
        return res
    if not lec.page_texte:
        etat.attendus[page] = len(lec.entrees)
    if lec.problemes and not local_seul:
        log(f"  {ident} : mise en page non comprise ({lec.problemes[0]}{' …' if len(lec.problemes) > 1 else ''})", "WARN")
        evenement("hybride_repli", chunk=ident, problemes=lec.problemes)
        return None
    feuille, texte, reperes = LH.construire_feuille(lec, net.convert("L")) if not local_seul else (None, "", {})
    rep, dico, penser, derniere = None, {}, etat.penser, "?"
    if feuille is not None:
        tampon = io.BytesIO()
        feuille.save(tampon, "PNG")
        png = tampon.getvalue()
        if cfg.sauver_images:                                    # la feuille réellement envoyée au modèle (pour juger la lisibilité de l'arabe)
            os.makedirs(os.path.join(cfg.sortie, "images"), exist_ok=True)
            with open(os.path.join(cfg.sortie, "images", ident + "_feuille.png"), "wb") as f:
                f.write(png)
        pas = min([x for x in (cfg.suivi, cfg.suivi_ui) if x] or [0])
        for essai in range(1, cfg.essais + 1):
            if etat.stop.is_set():
                raise Interrompu()
            temp = min(1.0, (cfg.temperature if cfg.temperature is not None else (0.6 if penser else 0.3)) + 0.15 * (essai - 1))
            top_p = cfg.top_p if cfg.top_p is not None else (0.95 if penser else 0.8)
            try:
                rep = appeler_llm(cfg, etat.info, png, LH.PROMPT_FEUILLE, texte, temperature=temp, top_p=top_p,
                                  max_tokens=min(etat.sortie_max, 500 + 70 * len(reperes)), penser=penser, budget=etat.budget(penser),
                                  stop=etat.stop, suivi=_suivi(ident, etat, cfg), pas_suivi=pas)
            except ErreurReflexion as e:
                derniere = str(e)
                etat.sans_parametre_reflexion()
                penser = True
                continue
            except ErreurLLM as e:
                derniere = str(e)
                log(f"  {ident} feuille, essai {essai}/{cfg.essais} : {derniere}", "WARN")
                continue
            if rep["abandon"] == "interruption":
                raise Interrompu()
            _journaliser_essai(ident + "-feuille", essai, penser, temp, top_p, etat, rep, png)
            if rep["erreur"]:
                derniere = rep["erreur"]
                try:
                    _classer_http(400, rep["erreur"])
                except ErreurReflexion:
                    etat.sans_parametre_reflexion()
                    penser = True
                except ErreurLLM:
                    pass
                continue
            dico = LH.extraire_feuille_json(rep["content"], "" if rep["abandon"] or rep["finish"] == "length" else rep["reasoning"]) or {}
            if len([k for k in reperes if k in dico]) >= max(1, len(reperes) // 2):
                break
            derniere = f"{rep['abandon'] or rep['finish']} : {len(dico)} lecture(s) exploitable(s) sur {len(reperes)}"
            log(f"  {ident} feuille, essai {essai}/{cfg.essais} sans résultat : {derniere}", "WARN")
            if not penser and rep["abandon"] == "budget_reflexion":
                etat.coupure_ignoree()
                penser = True
            elif penser and rep["abandon"] in ("budget_reflexion", "boucle_reflexion") and etat.peut_couper:
                penser = False
            dico = {}
        if not dico:
            log(f"  {ident} : la feuille de recadrages n'a pas pu être lue ({derniere})", "WARN")
            return None
    brutes, avert = LH.appliquer_feuille(lec, reperes, dico)
    if local_seul:
        avert = [f"lecture locale seule : le modèle n'a rien rendu d'utilisable, arabe non lu ({len(lec.problemes)} problème(s) de mise en page)"] + avert
        base = {**base, "methode": "local", "raison": "rescue"}
    entrees = normaliser_page({"entrees": brutes})
    res = {**base, "statut": "ok", "avertissements": avert, "duree": round(time.time() - t, 1), "tokens": rep["tokens"] if rep else 0,
           "tokens_reflexion": rep["tokens_reflexion"] if rep else 0, "tokens_entree": rep["tokens_entree"] if rep else 0,
           "tps": round(rep["tps"], 1) if rep and rep["tps"] else None, "ttft": round(rep["ttft"], 1) if rep and rep["ttft"] else None,
           "penser": penser, "entrees": entrees}
    ecrire_atomique(chemin, json.dumps(res, ensure_ascii=False, indent=1))
    return res


def _traiter_dispatch(cfg: Config, etat: Etat, page: int, bande: int) -> dict:
    if etat.hybride and cfg.decoupe == 1:
        res = _traiter_page_hybride(cfg, etat, page)
        if res is not None:
            return res
        log(f"  {id_chunk(page, bande, cfg.decoupe)} : repli sur la lecture de la page entière par le modèle de vision", "WARN")
    res = _traiter_chunk(cfg, etat, page, bande)
    attendu = etat.attendus.get(page) if etat.hybride and cfg.decoupe == 1 else None
    if attendu:
        n = len(res["entrees"])
        # NB : « le modèle répond [] » sur une page non blanche est une réponse VALIDE (pages de titre de section dont le verso transparaît :
        # la mise en page y voit de fausses entrées, p. 53, 81, 111… ; le rescue de la v4.3.0 y fabriquait ~40 entrées fantômes). On ne
        # reprend donc la lecture locale que si le modèle a ÉCHOUÉ (statut non « ok ») ou s'est emballé.
        echec = n == 0 and res.get("statut") != "ok"
        if echec or n > 2 * attendu + 2 or res.get("statut") == "partiel" and n > attendu + 3:
            log(f"  p{page:03d} : le modèle rend {n} entrée(s) pour {attendu} vue(s) par la mise en page : lecture locale seule (arabe à relire)", "WARN")
            evenement("rescue_local", page=page, rendues=n, attendues=attendu)
            sauve = _traiter_page_hybride(cfg, etat, page, local_seul=True)
            if sauve is not None and sauve["entrees"]:
                return sauve
        elif abs(n - attendu) >= 2:
            res.setdefault("avertissements", []).append(f"écart avec la mise en page : {n} entrée(s) lue(s), {attendu} attendue(s)")
            res["attendu"] = attendu
            ecrire_atomique(chemin_cache(cfg, page, bande), json.dumps(res, ensure_ascii=False, indent=1))
    return res


def traiter_chunk(cfg: Config, etat: Etat, page: int, bande: int) -> dict:
    """Une page (ou demi-page) : enregistrement dans le registre d'activité, journal de l'issue, relance des exceptions."""
    ident = id_chunk(page, bande, cfg.decoupe)
    if etat.controle is not None:
        etat.controle.maj_actif(ident, page=page, debut=time.time(), tokens=0, tokens_reflexion=0, tps=0.0, prefill=0.0, elapsed=0.0)
    try:
        res = _traiter_dispatch(cfg, etat, page, bande)
        evenement("page", chunk=ident, methode=res.get("methode", "vlm"), statut=res["statut"], entrees=len(res["entrees"]), duree=res["duree"], tokens=res["tokens"],
                  tokens_reflexion=res.get("tokens_reflexion"), tokens_entree=res.get("tokens_entree"), tps=res.get("tps"),
                  ttft=res.get("ttft"), blanche=bool(res.get("blanche")), penser=res.get("penser"))
        return res
    except Interrompu:
        evenement("page_annulee", chunk=ident)
        raise
    except ErreurFatale as e:
        log(f"{ident} : erreur fatale : {e}", "ERROR")
        evenement("fatal", chunk=ident, message=str(e))
        raise
    except Exception as e:                                       # noqa: BLE001 : journalisé avec sa trace, puis relancé
        log_exception(f"{ident} : échec de la page", e)
        raise
    finally:
        if etat.controle is not None:
            etat.controle.fin_actif(ident)


# ---------------------------------------------------------------- fils d'exécution

def executer(cfg: Config, etat: Etat, chunks: list, resultats: dict, apres_page) -> dict:
    """Traite les chunks avec `cfg.paralleles` fils (daemon). Le fil principal ne fait que surveiller, par pas de 0,5 s :
    Ctrl+C reste interrompable, y compris sous Windows où un verrou bloquant ne l'est pas. -> {chunk: erreur}."""
    echecs: dict = {}
    if not chunks:
        return echecs
    taches, sorties = queue.Queue(), queue.Queue()
    for ch in chunks:
        taches.put(ch)

    def travailleur():
        while not etat.stop.is_set():
            try:
                ch = taches.get_nowait()
            except queue.Empty:
                return
            try:
                sorties.put((ch, traiter_chunk(cfg, etat, *ch), None))
            except Interrompu:
                sorties.put((ch, None, "annulé"))
                return
            except ErreurFatale as e:
                etat.stop.set()
                sorties.put((ch, None, "FATAL: " + str(e)))
            except Exception as e:                                    # noqa: BLE001 : une page ne doit pas tuer le lot
                sorties.put((ch, None, f"{type(e).__name__}: {e}"))

    n = max(1, min(cfg.paralleles, len(chunks)))
    fils = [threading.Thread(target=travailleur, daemon=True) for _ in range(n)]
    for f in fils:
        f.start()
    fait, durees, debut = 0, [], time.time()
    ctrl, arret_vu = etat.controle, None
    if ctrl is not None:
        ctrl.debut, ctrl.total, ctrl.fait, ctrl.echecs = debut, len(chunks), 0, 0
    try:
        while True:
            if etat.stop.is_set():                              # arrêt demandé (interface ou erreur fatale) : 3 s aux fils
                arret_vu = arret_vu or time.time()
                if time.time() - arret_vu > 3:
                    break
            try:
                ch, res, err = sorties.get(timeout=0.5)
            except queue.Empty:
                if not any(f.is_alive() for f in fils) and sorties.empty():
                    break
                continue
            fait += 1
            nom = id_chunk(ch[0], ch[1], cfg.decoupe)
            if ctrl is not None:
                ctrl.fait = fait
            if err:
                echecs[ch] = err
                if ctrl is not None:
                    ctrl.echecs = len(echecs)
                if err.startswith("FATAL"):
                    log(f"ARRÊT : {err[7:]}")
                elif err != "annulé":
                    log(f"[{fait}/{len(chunks)}] {nom} ÉCHEC : {err}")
                continue
            resultats[ch] = res
            if not res.get("blanche"):
                durees.append(res["duree"])
            moy = sum(durees) / len(durees) if durees else 0.0
            reste = (len(chunks) - fait) * moy / n
            if ctrl is not None:
                ctrl.reste_s = reste
            ref = f" dont {res['tokens_reflexion']} de réflexion" if res.get("tokens_reflexion") else ""
            vitesse = f", {res['tps']:.1f} tok/s" if res.get("tps") else ""
            log(f"[{fait}/{len(chunks)}] {nom} : {len(res['entrees'])} entrées, {res['duree']:.0f}s, {res['tokens']} jetons{ref}{vitesse}"
                f"{' (PARTIEL)' if res['statut'] == 'partiel' else ''}{' (page blanche)' if res.get('blanche') else ''}"
                f" — {fait / max(1e-9, (time.time() - debut) / 60):.2f} page/min, reste ≈ {fmt_duree(reste)}")
            apres_page(fait)
    except KeyboardInterrupt:
        etat.stop.set()
        limite = time.time() + 3
        while time.time() < limite and any(f.is_alive() for f in fils):
            time.sleep(0.1)
        raise
    for ch in chunks:
        if ch not in resultats and ch not in echecs:
            echecs[ch] = "non traité"
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
        if res.get("methode") == "local" and res.get("raison") != "rescue":      # lecture locale des v4.3.0-4.3.3 : pages de titre -> entrées fantômes
            avert.append(f"p.{page} : lecture locale d'une version antérieure ignorée (page de titre de section probable, {len(res['entrees'])} entrée(s) fantôme(s))")
            continue
        brutes = dedoublonner(res["entrees"])                 # caches écrits avant la v4.3 : entrées fantômes d'une boucle
        if len(brutes) < len(res["entrees"]):
            avert.append(f"p.{page} : {len(res['entrees']) - len(brutes)} entrée(s) en double ignorée(s) (boucle du modèle)")
        for raw in brutes:
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


def translit_de(row: dict) -> str:
    return row.get("translittération") or ""


def drapeaux(row: dict, e: dict, b: dict | None = None) -> list[str]:
    f = []
    ar, d, s = row["prénom_arab_diacritiques"], row["description_en_français"], row["prénom_fr"]
    valide = bool(b and b.get("valide"))                 # arabe relu et validé / corrigé à la main (revue_manuelle)
    if valide:
        pass
    elif not ar:
        f.append("arabe_vide")
    else:
        if re.search(r"[A-Za-z0-9]", ar):
            f.append("arabe_non_arabe")
        if not re.search(f"[{HARAKAT}]", ar):
            f.append("arabe_sans_voyelles")
    if ar and VA is not None and translit_de(row) and not valide:
        ko, indice = VA.incoherent(translit_de(row), ar)
        if ko:
            f.append("arabe_incoherent_translit")
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
                       "prénom_arab_diacritiques": (VA.retirer_tanwin_final(b["arabe"]) if VA is not None else b["arabe"]),
                       "genre": b["genre"],
                       "description_en_français": e["description"],
                       "prénoms_équivalents": " / ".join(x for x in b["orthographes"] if x != s),
                       "forme_associée": autre,
                       "équivalent_étranger": equiv,
                       "translittération": translit,
                       "thème": theme, "sous_thème": sous,
                       "page_imprimée": e["page"] - PAGE_OFFSET, "page_pdf": e["page"],
                       "id_entrée": e["id"]}
                row["à_vérifier"] = "; ".join(drapeaux(row, e, b))
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
    colp = ["page_pdf", "page_imprimée", "statut", "entrées", "lignes", "durée_s", "tokens_entrée", "tokens_sortie",
            "tokens_réflexion", "tok_s", "remarque"]
    wp = wb.create_sheet("Pages")
    wp.append(colp)
    for c in wp[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), entete
    for p in infos_pages:
        wp.append([_propre(p.get(c, "")) for c in colp])
    for k in range(1, len(colp) + 1):
        wp.column_dimensions[get_column_letter(k)].width = 14 if k < 11 else 70
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
# 11) PIPELINE, STATUT, DIAGNOSTIC, AUTOTEST
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


def construire(cfg: Config, resultats: dict, echecs: dict | None = None, interrompu: bool = False):
    """Résultats en mémoire -> (lignes finales, état par page, avertissements)."""
    echecs = echecs or {}
    bandes = range(1, cfg.decoupe + 1)
    entrees, avert = assembler(resultats, cfg.decoupe)
    rows = exploser(entrees)
    par_page: dict[int, int] = {}
    for r in rows:
        par_page[r["page_pdf"]] = par_page.get(r["page_pdf"], 0) + 1
    infos = []
    for p in cfg.pages:
        cs = [resultats.get((p, b)) for b in bandes]
        manque = [b for b in bandes if resultats.get((p, b)) is None]
        errs = [echecs[(p, b)] for b in manque if echecs.get((p, b)) and echecs[(p, b)] not in ("non traité", "annulé")]
        if manque:
            statut = "à faire" if not errs else ("échec" if len(manque) == len(cs) else "incomplet")
        else:
            statut = "partiel" if any(c["statut"] == "partiel" for c in cs) else "ok"
        faits = [c for c in cs if c]
        tps = [c["tps"] for c in faits if c.get("tps")]
        remarque = "; ".join(errs) or ("interrompu" if manque and interrompu else "")
        if faits and not manque and all(c.get("blanche") for c in faits):
            remarque = "page blanche (aucune requête envoyée)"
        infos.append({"page_pdf": p, "page_imprimée": p - PAGE_OFFSET, "statut": statut,
                      "entrées": sum(len(c["entrees"]) for c in faits), "lignes": par_page.get(p, 0),
                      "durée_s": round(sum(c["duree"] for c in faits), 1),
                      "tokens_entrée": sum(c.get("tokens_entree") or 0 for c in faits),
                      "tokens_sortie": sum(c.get("tokens") or 0 for c in faits),
                      "tokens_réflexion": sum(c.get("tokens_reflexion") or 0 for c in faits),
                      "tok_s": round(sum(tps) / len(tps), 1) if tps else "", "remarque": remarque})
    return rows, infos, avert


def _oui_non(v) -> str:
    return "?" if v is None else ("oui" if v else "non")


def afficher_info(info: InfoModele, cfg: Config) -> None:
    log(f"modèle : {info.id} — API {'native /api/v1' if info.natif else 'compatible OpenAI (LM Studio < 0.4 ?)'} — {cfg.url}")
    if info.natif:
        det = ", ".join(x for x in (info.quant, f"{info.taille_go} Go" if info.taille_go else "", info.format) if x)
        if det:
            log(f"  {det}")
        log(f"  contexte chargé {info.ctx or '?'} ; requêtes simultanées {info.parallel or '?'} ; taille de lot {info.batch or '?'} ; "
            f"Flash Attention {_oui_non(info.flash)} ; KV sur GPU {_oui_non(info.kv_gpu)}")
        log(f"  vision {_oui_non(info.vision)} ; réglage de réflexion : {info.options_reflexion or 'non exposé par LM Studio'}")


def _rappel_gpu(controle: Controle | None):
    derniere = [0.0]

    def f(m: dict) -> None:
        if controle is not None:
            controle.gpu = m
        evenement("gpu", **m)
        if time.time() - derniere[0] >= 60:
            derniere[0] = time.time()
            debug(f"GPU : utilisation {m.get('util')} %, VRAM {m.get('mem')}/{m.get('mem_tot')} Mo, {m.get('pw')} W "
                  f"(limite {m.get('pw_lim')}), {m.get('clk')} MHz, {m.get('temp')} °C")
    return f


def _run_pipeline(cfg: Config, controle: Controle | None = None):
    pages = cfg.pages
    bandes = range(1, cfg.decoupe + 1)
    chunks = [(p, b) for p in pages for b in bandes]
    resultats, a_faire, perimes, gardes = {}, [], 0, 0
    for ch in chunks:
        d, perime = (None, False) if (cfg.refaire and not cfg.sans_llm) else lire_cache(cfg, *ch, tolerer=cfg.sans_llm)
        if d is not None:
            resultats[ch] = d
            gardes += int(perime)
        else:
            a_faire.append(ch)
            perimes += int(perime)
    log(f"cache : {os.path.abspath(os.path.join(cfg.sortie, 'cache'))}")
    log(f"{len(pages)} page(s) demandée(s) : {len(resultats)} déjà en cache, {len(a_faire)} à traiter"
        f" (découpe {cfg.decoupe}, nettoyage {'oui' if cfg.nettoyage else 'non'}, parallèle {cfg.paralleles}).")
    if perimes:
        log(f"  dont {perimes} en cache mais périmés (prompt, modèle, découpage, nettoyage ou image différents) : recalculés.")
    if gardes:
        log(f"  dont {gardes} calculées avec une autre configuration (prompt, modèle, image...) : conservées par --export.")
    evenement("cache", pages=len(pages), en_cache=len(resultats), a_traiter=len(a_faire), perimes=perimes, gardes=gardes,
              dossier=os.path.abspath(cfg.sortie))
    echecs: dict = {}
    cfg.interrompu, cfg.fatal = False, ""
    if a_faire and cfg.sans_llm:
        log("--export : LM Studio n'est pas contacté ; les pages absentes du cache restent « à faire ».")
    elif a_faire:
        info = resoudre_modele(cfg)
        afficher_info(info, cfg)
        evenement("modele", **asdict(info))
        etat = Etat(cfg, info, controle)
        etat.hybride = preparer_hybride(cfg)
        calibrer(cfg, etat, a_faire[0])
        evenement("calibrage", tokens_entree=etat.tokens_entree, sortie_max=etat.sortie_max, max_cote_auto=cfg.max_cote_auto,
                  paralleles=cfg.paralleles, penser=etat.penser, contexte=info.ctx)
        log(f"méthode : {'HYBRIDE (mise en page + Tesseract + feuille de recadrages)' if etat.hybride else 'modèle de vision seul'} ; "
            f"mode : {'AVEC' if etat.penser else 'SANS'} réflexion ; sortie max {etat.sortie_max} jetons ; "
            f"{cfg.paralleles} requête(s) à la fois ; abandon si la réflexion dépasse {etat.budget(etat.penser)} jetons ou boucle.")

        def apres_page(n: int) -> None:
            if cfg.export_tous and n % cfg.export_tous == 0:
                r, i, _ = construire(cfg, resultats, {}, False)
                ch = exporter(r, i, cfg.sortie)
                log(f"export intermédiaire : {len(r)} lignes -> {ch.get('csv')}")
                evenement("export", intermediaire=True, lignes=len(r), **ch)

        if controle is not None:
            controle.etat = "en cours"
        try:
            with EchantillonGPU(5.0, rappel=_rappel_gpu(controle)):
                echecs = executer(cfg, etat, a_faire, resultats, apres_page)
        except KeyboardInterrupt:
            cfg.interrompu = True
            log("interruption : export de ce qui est déjà calculé (relance la même commande pour reprendre).")
        cfg.fatal = next((v[7:] for v in echecs.values() if v.startswith("FATAL")), "")
        if etat.stop.is_set() and not cfg.fatal and not cfg.interrompu:
            cfg.interrompu = True
            log("arrêt demandé : export de ce qui est déjà calculé (relance la même commande pour reprendre).")
    return construire(cfg, resultats, echecs, cfg.interrompu)


def run_pipeline(cfg: Config, controle: Controle | None = None):
    """Cache -> (calibrage -> requêtes) -> lignes finales. `controle` : pont avec une interface (arrêt, activité, progression)."""
    if controle is not None:
        controle.etat, controle.message = "calibrage", ""
    try:
        r = _run_pipeline(cfg, controle)
    except BaseException as e:
        if controle is not None:
            controle.etat, controle.message = "erreur", str(e)
        raise
    if controle is not None:
        controle.etat = "erreur" if cfg.fatal else ("interrompu" if cfg.interrompu else "termine")
        controle.message = cfg.fatal
    return r


def resume(rows: list[dict], infos: list[dict], avert: list[str]) -> None:
    n_entrees = len({r["id_entrée"] for r in rows})
    log(f"RÉSULTAT : {len(rows)} prénoms (lignes) issus de {n_entrees} entrées — "
        f"{sum(r['genre'] == 'MASCULIN' for r in rows)} masculins, {sum(r['genre'] == 'FEMININ' for r in rows)} féminins.")
    log(f"  arabe rempli : {sum(bool(r['prénom_arab_diacritiques']) for r in rows)}/{len(rows)} ; "
        f"lignes « à vérifier » : {sum(a_verifier(r) for r in rows)}.")
    faites = [p for p in infos if p["statut"] in ("ok", "partiel") and p["durée_s"]]
    if faites:
        vit = [p["tok_s"] for p in faites if p["tok_s"] != ""]
        log(f"  {sum(p['durée_s'] for p in faites) / len(faites):.0f} s par page en moyenne"
            + (f", {sum(vit) / len(vit):.1f} tok/s" if vit else "")
            + f", {sum(p['tokens_réflexion'] for p in faites)} jetons de réflexion au total.")
    mauvais = [p["page_pdf"] for p in infos if p["statut"] in ("échec", "incomplet")]
    afaire = [p["page_pdf"] for p in infos if p["statut"] == "à faire"]
    partiels = [p["page_pdf"] for p in infos if p["statut"] == "partiel"]
    vides = [p["page_pdf"] for p in infos if p["statut"] == "ok" and p["entrées"] < 3
             and not str(p["remarque"]).startswith("page blanche") and DICT_START + 2 <= p["page_pdf"] <= DICT_END]
    if mauvais:
        log(f"  PAGES EN ÉCHEC ({len(mauvais)}) : {mauvais[:40]}{' …' if len(mauvais) > 40 else ''} -> relance la même commande.")
    if afaire:
        log(f"  PAGES PAS ENCORE TRAITÉES ({len(afaire)}) : {afaire[:40]}{' …' if len(afaire) > 40 else ''} -> relance la même commande.")
    if partiels:
        log(f"  pages partielles ({len(partiels)}) : {partiels[:40]} -> relance avec un --max-tokens plus grand (et un contexte suffisant).")
    if vides:
        log(f"  pages suspectes (moins de 3 entrées) : {vides[:40]}{' …' if len(vides) > 40 else ''}")
    for a in avert[:15]:
        log("  ⚠ " + a)
    if len(avert) > 15:
        log(f"  … et {len(avert) - 15} autre(s) avertissement(s).")


def resultats_du_cache(cfg: Config, tolerer: bool = True) -> dict:
    """{(page, bande): résultat} lus dans le cache, sans rien journaliser (utilisé par la TUI)."""
    out = {}
    for p in cfg.pages:
        for b in range(1, cfg.decoupe + 1):
            d, _ = lire_cache(cfg, p, b, tolerer=tolerer)
            if d is not None:
                out[(p, b)] = d
    return out


def etat_cache(cfg: Config) -> dict:
    """Où en est-on ? Lit uniquement le cache. -> {'cats': {statut: [pages]}, 'reste': n, 'moyenne': s/page | None, 'reste_s': s | None}."""
    cats: dict[str, list] = {"ok": [], "partiel": [], "blanche": [], "périmé": [], "à faire": []}
    durees = []
    for p in cfg.pages:
        for b in range(1, cfg.decoupe + 1):
            d, perime = lire_cache(cfg, p, b)
            if d is None:
                cats["périmé" if perime else "à faire"].append(p)
            elif d.get("blanche"):
                cats["blanche"].append(p)
            else:
                cats["partiel" if d.get("statut") == "partiel" else "ok"].append(p)
                durees.append(d.get("duree") or 0)
    reste = len(cats["à faire"]) + len(cats["périmé"]) + len(cats["partiel"])
    moy = sum(durees) / len(durees) if durees else None
    return {"cats": cats, "reste": reste, "moyenne": moy,
            "reste_s": reste * moy / max(1, cfg.paralleles) if (moy and reste) else None}


def afficher_statut(cfg: Config) -> None:
    """Où en est-on ? Lit uniquement le cache : LM Studio n'est pas nécessaire."""
    e = etat_cache(cfg)
    log(f"cache : {os.path.abspath(os.path.join(cfg.sortie, 'cache'))}")
    log(f"{len(cfg.pages)} page(s) concernée(s) (découpe {cfg.decoupe}) :")
    for k, v in e["cats"].items():
        if v:
            log(f"  {k:8s} : {len(v):4d}" + (f"   {v[:25]}{' …' if len(v) > 25 else ''}" if k != "ok" else ""))
    if e["cats"]["périmé"]:
        log("  (périmé = calculé avec un autre prompt, modèle, découpage, nettoyage ou taille d'image ; --refaire ou relance pour recalculer)")
    if e["moyenne"] and e["reste"]:
        log(f"durée moyenne mesurée : {e['moyenne']:.0f} s par page -> reste ≈ {fmt_duree(e['reste_s'])} (parallèle {cfg.paralleles}).")


# ---------------------------------------------------------------- mesure GPU (nvidia-smi)

class EchantillonGPU:
    """Échantillonne nvidia-smi (s'il existe) pendant un bloc `with` : utilisation, VRAM, puissance, fréquence."""
    CMD = ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,power.draw,power.limit,clocks.sm,temperature.gpu,name",
           "--format=csv,noheader,nounits"]

    def __init__(self, intervalle: float = 1.0, rappel=None):
        self.intervalle = intervalle
        self.rappel = rappel
        self.mesures: list[dict] = []
        self.disponible = shutil.which("nvidia-smi") is not None
        self._stop = threading.Event()
        self._fil = None

    @staticmethod
    def _nombre(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def instantane(self):
        if not self.disponible:
            return None
        try:
            r = subprocess.run(self.CMD, capture_output=True, text=True, timeout=8,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            c = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            n = self._nombre
            return {"util": n(c[0]), "mem": n(c[1]), "mem_tot": n(c[2]), "pw": n(c[3]), "pw_lim": n(c[4]),
                    "clk": n(c[5]), "temp": n(c[6]), "nom": c[7] if len(c) > 7 else ""}
        except (OSError, IndexError, subprocess.SubprocessError):
            self.disponible = False
            return None

    def _boucle(self):
        while not self._stop.wait(self.intervalle):
            m = self.instantane()
            if m:
                self.mesures.append(m)
                if self.rappel is not None:
                    try:
                        self.rappel(m)
                    except Exception:                            # noqa: BLE001
                        pass

    def __enter__(self):
        if self.disponible:
            self._stop.clear()
            self._fil = threading.Thread(target=self._boucle, daemon=True)
            self._fil.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._fil is not None:
            self._fil.join(timeout=10)
        return False

    def resume(self):
        m = self.mesures
        if not m:
            return None

        def moy(k):
            v = [x[k] for x in m if x.get(k) is not None]
            return sum(v) / len(v) if v else None

        def maxi(k):
            v = [x[k] for x in m if x.get(k) is not None]
            return max(v) if v else None
        return {"n": len(m), "util_moy": moy("util"), "mem_max": maxi("mem"), "mem_tot": m[0].get("mem_tot"),
                "pw_moy": moy("pw"), "pw_lim": m[0].get("pw_lim"), "clk_moy": moy("clk"), "temp_max": maxi("temp"),
                "nom": m[0].get("nom", "")}


# ---------------------------------------------------------------- diagnostic

def diagnostic(cfg: Config) -> None:
    from PIL import ImageDraw
    log(f"1/5  serveur {cfg.url} …")
    info = resoudre_modele(cfg)
    afficher_info(info, cfg)
    gpu = EchantillonGPU(1.0)
    base = gpu.instantane()
    if base:
        log(f"     GPU {base['nom']} : VRAM {base['mem']:.0f}/{base['mem_tot']:.0f} Mo occupés au repos, utilisation {base['util']:.0f} %")
    else:
        log("     nvidia-smi introuvable : pas de mesure GPU.")

    log("2/5  vitesse de génération (texte seul, réflexion coupée si possible) …")
    with gpu:
        rep = appeler_llm(cfg, info, None, None, "Compte de 1 à 120 en chiffres, séparés par des virgules, sans rien d'autre.",
                          temperature=0.0, top_p=None, max_tokens=300, penser=False, budget=0)
    tps = rep["tps"]
    g = gpu.resume()
    log(f"     {rep['tokens']} jetons en {rep['duree']:.1f}s -> " + (f"{tps:.1f} tok/s" if tps else "vitesse non mesurable")
        + (f" (premier jeton après {rep['ttft']:.1f}s)" if rep["ttft"] else ""))
    if rep["tokens_reflexion"]:
        log(f"     {rep['tokens_reflexion']} jetons de réflexion malgré la demande de la couper.")
    if g:
        log(f"     GPU pendant la génération : utilisation moyenne {g['util_moy']:.0f} %, VRAM max {g['mem_max']:.0f}/{g['mem_tot']:.0f} Mo"
            + (f", puissance moyenne {g['pw_moy']:.0f} W (limite {g['pw_lim']:.0f} W)" if g["pw_moy"] and g["pw_lim"] else "")
            + (f", {g['clk_moy']:.0f} MHz, {g['temp_max']:.0f} °C max" if g["clk_moy"] and g["temp_max"] else ""))

    log("3/5  vision (lecture d'une image contenant « ADAM 123 ») …")
    petit = Image.new("RGB", (100, 30), "white")
    ImageDraw.Draw(petit).text((8, 9), "ADAM 123", fill="black")
    buf = io.BytesIO()
    petit.resize((700, 210), Image.NEAREST).save(buf, "PNG")
    rv = appeler_llm(cfg, info, buf.getvalue(), None, "Quel texte est écrit dans l'image ? Réponds uniquement par ce texte.",
                     temperature=0.0, top_p=None, max_tokens=600, penser=False, budget=300)
    vu = "ADAM" in (rv["content"] + rv["reasoning"]).upper()
    if vu:
        verdict = "VISION OK"
    elif rv["abandon"] == "budget_reflexion":
        verdict = "test non concluant : le modèle réfléchit malgré la demande de couper la réflexion (voir le bilan)"
    else:
        verdict = "réponse inattendue : la vision ne semble pas fonctionner"
    log(f"     réponse en {rv['duree']:.1f}s : {rv['content'].strip()[:60]!r} -> {verdict}")

    log("4/5  jetons d'entrée d'une vraie page …")
    entree_cible, t_txt, t_img, vitesse_prefill = 0, 0, 0, None
    if cfg.pdf and os.path.exists(cfg.pdf):
        page = cfg.pages[0] if cfg.pages and cfg.pages[0] != DICT_START else 55      # 55 : page type du dictionnaire
        cfg.max_cote_auto = 0
        try:
            t_txt = sonder_tokens(cfg, info, None, cfg.prompt, TEXTE_UTILISATEUR)["entree"]
            png, _ = preparer_image(cfg, page, 1)
            m = sonder_tokens(cfg, info, png, cfg.prompt, TEXTE_UTILISATEUR)
            t_img = max(0, m["entree"] - t_txt)
            vitesse_prefill = m["entree"] / m["ttft"] if m.get("ttft") else None
            pre = f" ; préremplissage ≈ {vitesse_prefill:.0f} jetons/s" if vitesse_prefill else ""
            log(f"     page {page} : consigne ≈ {t_txt} jetons, image ≈ {t_img} jetons{pre}")
            entree_cible = t_txt + (min(t_img, cfg.tokens_image) if cfg.tokens_image else t_img)
            if cfg.tokens_image and t_img > cfg.tokens_image * 1.15:
                log(f"     -> l'image sera réduite à ≈ {cfg.tokens_image} jetons (--tokens-image) ; entrée visée ≈ {entree_cible}")
        except ErreurLLM as e:
            log(f"     mesure impossible : {e}")
    else:
        log("     (ajoute le chemin du PDF à la commande pour mesurer une vraie page)")

    log("5/5  bilan")
    ctx_reco = arrondi_1024((entree_cible or 4000) + cfg.max_tokens + 256) * max(1, cfg.paralleles)
    constats: list[str] = []
    if tps is not None and tps < 15:
        constats.append(f"Génération lente : {tps:.1f} tok/s. Un modèle de ce poids entièrement sur une carte récente tient bien plus que 15 tok/s : "
                        "la limite vient de là où le calcul se fait, pas du script.")
    if vitesse_prefill is not None and vitesse_prefill < 150:
        constats.append(f"Préremplissage très lent ({vitesse_prefill:.0f} jetons/s) : typique d'un calcul sur le CPU (un GPU récent traite "
                        "plusieurs centaines de jetons/s). Vérifie « GPU Offload » et la version du runtime llama.cpp CUDA.")
    if g and g["util_moy"] is not None and g["util_moy"] < 35 and (tps or 0) < 25:
        constats.append(f"GPU peu sollicité pendant la génération ({g['util_moy']:.0f} %) : le calcul se fait probablement sur le CPU "
                        "(« GPU Offload » incomplet dans LM Studio, ou runtime llama.cpp sans noyau CUDA pour ce format de modèle).")
    if g and g["mem_tot"] and g["mem_max"] / g["mem_tot"] > 0.92:
        constats.append(f"VRAM presque pleine ({g['mem_max']:.0f}/{g['mem_tot']:.0f} Mo) : sous Windows, le pilote peut déborder en silence vers la RAM "
                        "système, ce qui écroule la vitesse. Réduis le contexte et l'image, ou ferme les applications qui utilisent le GPU.")
    if g and g["pw_moy"] and g["pw_lim"] and g["util_moy"] and g["util_moy"] > 70 and g["pw_moy"] < 0.5 * g["pw_lim"]:
        constats.append("Le GPU est occupé mais consomme peu par rapport à sa limite : vérifie le mode d'alimentation (Silencieux/Éco bride le GPU) "
                        "et le branchement secteur.")
    if info.ctx and entree_cible and info.ctx < (entree_cible + 3000) * max(1, cfg.paralleles):
        constats.append(f"Contexte chargé trop juste ({info.ctx}) pour {max(1, cfg.paralleles)} requête(s) de ≈ {entree_cible} jetons d'entrée + sortie.")
    if info.parallel and info.parallel > max(1, cfg.paralleles):
        constats.append(f"« Max Concurrent Predictions » = {info.parallel} alors que le script envoie {max(1, cfg.paralleles)} requête(s) à la fois : "
                        f"mets-le à {max(1, cfg.paralleles)} (moins de mémoire réservée).")
    if info.flash is False:
        constats.append("Flash Attention est désactivé : active-le au chargement du modèle.")
    if info.batch and info.batch < 1024:
        constats.append(f"Taille de lot d'évaluation = {info.batch} : 1024 accélère le préremplissage de l'image.")
    if info.kv_gpu is False:
        constats.append("Le cache KV n'est pas sur le GPU : active « Offload KV Cache to GPU Memory ».")
    if info.natif and not info.peut_couper_reflexion and info.options_reflexion == []:
        constats.append("LM Studio n'expose pas de réglage de réflexion pour ce modèle : le script la bornera, mais la couper "
                        "(« Enable Thinking » dans les réglages du modèle) fera gagner l'essentiel du temps.")
    if not constats:
        constats.append("Aucun problème évident détecté.")
    evenement("diagnostic", tps=tps, gpu=g, info=asdict(info), jetons_consigne=t_txt, jetons_image=t_img,
              prefill_tps=vitesse_prefill, contexte_conseille=ctx_reco, constats=constats)
    for c in constats:
        log("  • " + c)
    log("Réglages conseillés dans LM Studio (fenêtre de chargement, « Manually choose model load parameters ») :")
    log(f"  contexte {ctx_reco} ; GPU Offload au maximum ; Flash Attention activé ; taille de lot 1024 ; Max Concurrent Predictions {max(1, cfg.paralleles)}.")
    log("  Ou en ligne de commande (décharge d'abord le modèle : bouton « Eject » ou `lms unload --all`) :")
    log("  " + commande_lms(info.cle or cfg.modele, ctx_reco, cfg.paralleles))


# ============================================================================
# 11 bis) RELECTURE CIBLÉE DE L'ARABE (v4.3) : le modèle propose, le vérificateur tranche
# ============================================================================

def relire_arabe(cfg: Config) -> dict:
    """Relit, AVEC réflexion, uniquement les mots arabes dont la lecture est incohérente avec la translittération de la tête.
    Pour chaque page du cache : mise en page recalculée (CPU), feuille réduite aux recadrages suspects, indice « première lecture
    incohérente ; lettres attendues ≈ … ». Une nouvelle lecture n'est ADOPTÉE que si le vérificateur (verif_arabe) la juge nettement
    plus cohérente (gain ≥ 0,25) : sinon l'ancienne reste, drapeau compris. Les corrections sont écrites dans le cache de la page
    (champ `relectures`), donc conservées par --export. -> statistiques."""
    import lecture_hybride as LH
    if VA is None:
        raise ErreurFatale("verif_arabe.py introuvable : la relecture ciblée a besoin du vérificateur.")
    LH.configurer(cfg.tesseract, cfg.tessdata, cfg.tess_lang)
    ok, msg = LH.disponible()
    if not ok:
        raise ErreurFatale(f"--relire-arabe a besoin de Tesseract (mise en page) : {msg}")
    stats = {"pages": 0, "cibles": 0, "corrigees": 0, "restantes": 0, "non_alignees": 0, "requetes": 0, "jetons_reflexion": 0, "duree_s": 0.0}
    t_debut = time.time()
    a_relire = []
    for p in cfg.pages:
        d, _ = lire_cache(cfg, p, 1, tolerer=True)
        if not d or d.get("blanche") or not d.get("entrees"):
            continue
        cibles = {}
        for i, e in enumerate(d["entrees"]):
            translit, _equiv = parse_description(e["description"])
            if not translit:
                continue
            for j, b in enumerate(e["prenoms"]):
                if b.get("valide"):
                    continue
                c, indice = VA.coherence(translit, b["arabe"]) if b["arabe"] else (9.0, f"aucune lecture ; translittération « {translit} »")
                if c >= VA.SEUIL:
                    cibles[(i, j)] = {"translit": translit, "avant": b["arabe"], "cout": c, "indice": indice}
        if cibles:
            a_relire.append((p, d, cibles))
    log(f"relecture ciblée : {sum(len(c) for _, _, c in a_relire)} mot(s) arabe(s) incohérent(s) sur {len(a_relire)} page(s) du cache.")
    if not a_relire:
        return stats
    info = resoudre_modele(cfg)
    afficher_info(info, cfg)
    etat = Etat(cfg, info, None)
    budget = cfg.budget_reflexion or 3000
    penser = cfg.reflexion == "oui"          # MESURÉ le 28 oct. 2026 : avec réflexion, 18 requêtes sur 18 (3 modèles) épuisent le budget sans rendre
    #                                          de réponse -> 0 correction. Sans réflexion, gemma-4-12b lit 22/22 mots de l'autotest. Réflexion = option.
    for p, d, cibles in a_relire:
        if etat.stop.is_set():
            break
        ident = id_chunk(p, 1, 1)
        net = nettoyer_page(image_native(cfg.pdf, p))
        lec = LH.lire_page(net)
        entrees = d["entrees"]
        alignee = (not lec.page_texte and len(lec.entrees) == len(entrees)
                   and all(len(le.blocs) == len(e["prenoms"]) for le, e in zip(lec.entrees, entrees, strict=True)))
        if not alignee:
            stats["non_alignees"] += len(cibles)
            log(f"  {ident} : la mise en page ne correspond pas au cache ({len(lec.entrees)} vs {len(entrees)} entrées) : {len(cibles)} mot(s) laissé(s) tel(s) quel(s)", "WARN")
            continue
        # garde-fou : on ne relit un recadrage que si le prénom latin correspond bien à celui du cache
        for (i, j) in list(cibles):
            if not lec.entrees[i].blocs[j].arabe or difflib.SequenceMatcher(
                    None, LH.sans_accents(" ".join(lec.entrees[i].blocs[j].orthographes)),
                    LH.sans_accents(" ".join(entrees[i]["prenoms"][j]["orthographes"]))).ratio() < 0.7:
                del cibles[(i, j)]
                stats["non_alignees"] += 1
        stats["pages"] += 1
        stats["cibles"] += len(cibles)
        restant = dict(cibles)
        refusees: dict = {}                                  # (i, j) -> lectures déjà proposées et rejetées par le vérificateur
        gris = net.convert("L")
        journal = []
        for essai in range(1, max(2, cfg.essais) + 1):
            if not restant or etat.stop.is_set():
                break
            indices = {k: f"première lecture « {v['avant']} » jugée incohérente ; {v['indice']} (une lettre parmi celles entre crochets)"
                          + (" ; lectures déjà essayées et refusées : " + ", ".join(f"« {x} »" for x in refusees[k]) if refusees.get(k) else "")
                       for k, v in restant.items()}
            feuille, _txt, reperes = LH.construire_feuille(lec, gris, seulement=indices)
            if feuille is None:
                break
            tampon = io.BytesIO()
            feuille.save(tampon, "PNG")
            temp = min(1.0, 0.6 + 0.15 * (essai - 1)) if penser else min(1.0, 0.2 + 0.2 * (essai - 1))
            max_tok = min(max(cfg.max_tokens, 1500), budget + 300 + 80 * len(reperes)) if penser else 200 + 90 * len(reperes)
            try:
                rep = appeler_llm(cfg, info, tampon.getvalue(), LH.PROMPT_RELECTURE, "Relis ces mots arabes.", temperature=temp, top_p=0.95,
                                  max_tokens=max_tok, penser=penser, budget=budget,
                                  stop=etat.stop, suivi=None, pas_suivi=0)
            except ErreurReflexion:
                continue
            except ErreurLLM as e:
                log(f"  {ident} relecture, essai {essai} : {e}", "WARN")
                continue
            if rep["abandon"] == "interruption":
                raise Interrompu()
            _journaliser_essai(ident + "-relecture", essai, penser, temp, 0.95, etat, rep, tampon.getvalue())
            if rep["abandon"] == "budget_reflexion":
                log(f"  {ident} relecture, essai {essai} : réflexion épuisée ({budget} jetons) sans réponse : augmente --budget-reflexion ou retire --reflexion oui", "WARN")
            stats["requetes"] += 1
            stats["jetons_reflexion"] += int(rep.get("tokens_reflexion") or 0)
            dico = LH.extraire_feuille_json(rep["content"], "" if rep["abandon"] or rep["finish"] == "length" else rep["reasoning"]) or {}
            for rp, (_k, i, j) in reperes.items():
                v = VA.retirer_tanwin_final(nettoyer_arabe(dico.get(rp, "")))
                if not v or (i, j) not in restant:
                    continue
                cible = restant[(i, j)]
                cout, _ = VA.coherence(cible["translit"], v)
                evenement("relecture_mot", chunk=ident, repere=rp, essai=essai, translit=cible["translit"], avant=cible["avant"], propose=v,
                          cout_avant=cible["cout"], cout_propose=cout, adopte=bool(cout >= 0 and cout <= cible["cout"] - 0.25))
                if not (cout >= 0 and cout <= cible["cout"] - 0.25):
                    refusees.setdefault((i, j), []).append(v)
                if cout >= 0 and cout <= cible["cout"] - 0.25:
                    entrees[i]["prenoms"][j]["arabe"] = v
                    journal.append({"entree": i + 1, "bloc": j + 1, "avant": cible["avant"], "apres": v, "cout_avant": cible["cout"],
                                    "cout_apres": cout, "essai": essai, "jetons_reflexion": rep["tokens_reflexion"]})
                    if cout < VA.SEUIL:
                        del restant[(i, j)]
                    else:
                        restant[(i, j)] = {**cible, "avant": v, "cout": cout, "indice": VA.coherence(cible["translit"], v)[1]}
        stats["corrigees"] += len(journal)
        stats["restantes"] += len(restant)
        if journal:
            d.setdefault("relectures", []).extend(journal)
            d.setdefault("avertissements", []).append(f"relecture ciblée : {len(journal)} mot(s) arabe(s) corrigé(s) ({len(restant)} encore incohérent(s))")
            ecrire_atomique(chemin_cache(cfg, p, 1), json.dumps(d, ensure_ascii=False, indent=1))
        evenement("relecture", chunk=ident, cibles=len(cibles), corrigees=len(journal), restantes=len(restant))
        log(f"  {ident} : {len(cibles)} suspect(s), {len(journal)} corrigé(s), {len(restant)} encore incohérent(s)")
    stats["duree_s"] = round(time.time() - t_debut, 1)
    log(f"RELECTURE : {stats['corrigees']} mot(s) corrigé(s) sur {stats['cibles']} suspect(s) ; {stats['restantes']} restent signalés "
        f"« arabe_incoherent_translit » ; {stats['non_alignees']} non relu(s) (mise en page différente du cache).")
    return stats



# ============================================================================
# 11 ter) REVUE MANUELLE (v4.4) : exporter les mots problématiques, appliquer les corrections
# ============================================================================

def _page_alignee(lec, entrees: list) -> bool:
    return (not lec.page_texte and len(lec.entrees) == len(entrees)
            and all(len(le.blocs) == len(e["prenoms"]) for le, e in zip(lec.entrees, entrees, strict=True)))


def exporter_problemes(cfg: Config, inclure_voyelles: bool = False) -> str:
    """Écrit <sortie>/problemes/{problemes.html, problemes.csv, images/} : recadrages du mot arabe et de la tête pour chaque mot signalé."""
    import lecture_hybride as LH
    import revue_manuelle as RM
    pages = {}
    for p in cfg.pages:
        d, _ = lire_cache(cfg, p, 1, tolerer=True)
        if d and d.get("entrees") and not d.get("blanche") and not (d.get("methode") == "local" and d.get("raison") != "rescue"):
            pages[p] = d
    problemes = RM.lister_problemes(pages, parse_description, VA, HARAKAT, inclure_voyelles)
    dossier = os.path.join(cfg.sortie, "problemes")
    dossier_img = os.path.join(dossier, "images")
    os.makedirs(dossier_img, exist_ok=True)
    LH.configurer(cfg.tesseract, cfg.tessdata, cfg.tess_lang)
    ok, msg = LH.disponible()
    if not ok:
        log(f"Tesseract indisponible ({msg}) : la liste est écrite SANS recadrages.", "WARN")
    par_page: dict = {}
    for pb in problemes:
        par_page.setdefault(pb["page"], []).append(pb)
    sans_image = 0
    for p, liste in sorted(par_page.items()):
        lec = None
        if ok:
            net = nettoyer_page(image_native(cfg.pdf, p))
            lec = LH.lire_page(net)
            gris = net.convert("L")
            if not _page_alignee(lec, pages[p]["entrees"]):
                lec = None
        for pb in liste:
            pb["_b64_arabe"] = pb["_b64_tete"] = ""
            pb["image_arabe"] = pb["image_tete"] = ""
            if lec is None:
                sans_image += 1
                continue
            bloc = lec.entrees[pb["i"]].blocs[pb["j"]]
            if bloc.arabe:
                png = RM._png(LH._recadrer(gris, *bloc.arabe, 14), 110)
                pb["image_arabe"] = f"images/{pb['id']}_ar.png"
                pb["_b64_arabe"] = RM._b64(png)
                ecrire_atomique_octets(os.path.join(dossier, pb["image_arabe"]), png)
            if lec.entrees[pb["i"]].tete_box:
                png = RM._png(LH._recadrer(gris, *lec.entrees[pb["i"]].tete_box, 8), 56)
                pb["image_tete"] = f"images/{pb['id']}_tete.png"
                pb["_b64_tete"] = RM._b64(png)
                ecrire_atomique_octets(os.path.join(dossier, pb["image_tete"]), png)
    lignes = [{"id": pb["id"], "page_pdf": pb["page"], "page_imprimee": pb["page"] - PAGE_OFFSET, "prenom_fr": pb["prenom_fr"],
               "translittération": pb["translit"], "arabe_actuel": pb["arabe"], "categorie": pb["categorie"], "indice": pb["indice"],
               "image_arabe": pb["image_arabe"], "image_tete": pb["image_tete"], "arabe_corrige": "", "translit_corrigee": "", "valide": "",
               "_b64_arabe": pb["_b64_arabe"], "_b64_tete": pb["_b64_tete"]} for pb in problemes]
    RM.ecrire_csv(os.path.join(dossier, "problemes.csv"), lignes)
    ecrire_atomique(os.path.join(dossier, "problemes.html"), RM.page_html(lignes, "Revue des mots arabes"))
    par_cat = collections.Counter(pb["categorie"] for pb in problemes)
    log(f"REVUE : {len(problemes)} mot(s) à relire sur {len(par_page)} page(s) : " + ", ".join(f"{n} {RM.CATEGORIES[c]}" for c, n in par_cat.most_common()) + ".")
    if sans_image:
        log(f"  {sans_image} mot(s) sans recadrage (mise en page différente du cache) : repère-les par la page du PDF.", "WARN")
    log(f"  Ouvre {os.path.join(dossier, 'problemes.html')} dans ton navigateur, corrige, télécharge corrections.csv, puis :")
    log("  python extraire_prenoms_llm.py --appliquer-corrections corrections.csv --sortie " + cfg.sortie)
    return os.path.join(dossier, "problemes.html")


def ecrire_atomique_octets(chemin: str, octets: bytes) -> None:
    os.makedirs(os.path.dirname(chemin) or ".", exist_ok=True)
    tmp = chemin + ".tmp"
    with open(tmp, "wb") as f:
        f.write(octets)
    os.replace(tmp, chemin)


def appliquer_corrections_cli(cfg: Config, fichier: str) -> dict:
    import revue_manuelle as RM
    lignes = RM.lire_corrections(fichier)
    pages = {}
    for L in lignes:
        try:
            p, _i, _j = RM.lire_identifiant(L.get("id", ""))
        except ValueError:
            continue
        if p not in pages:
            d, _ = lire_cache(cfg, p, 1, tolerer=True)
            if d:
                pages[p] = d
    modifies, st = RM.appliquer(pages, lignes, parse_description, nettoyer_arabe)
    for p, d in modifies.items():
        ecrire_atomique(chemin_cache(cfg, p, 1), json.dumps(d, ensure_ascii=False, indent=1))
    log(f"CORRECTIONS : {st['valides']} mot(s) validé(s) ({st['arabe_corrige']} arabe corrigé, {st['translit_corrigee']} translittération corrigée), "
        f"{st['ignorees']} ligne(s) ignorée(s) sur {st['lignes']}.")
    return st


def relire_tetes(cfg: Config) -> dict:
    """Relit au modèle de vision les TÊTES de définition (« Khalîl-Allâh : ») dont la lecture Tesseract est incohérente avec l'arabe de l'entrée.
    L'arabe sert d'arbitre : la nouvelle tête n'est adoptée que si elle devient cohérente avec lui (coût < seuil, gain ≥ 0,5). Écrit dans le cache."""
    import lecture_hybride as LH
    if VA is None:
        raise ErreurFatale("verif_arabe.py introuvable.")
    LH.configurer(cfg.tesseract, cfg.tessdata, cfg.tess_lang)
    ok, msg = LH.disponible()
    if not ok:
        raise ErreurFatale(f"--relire-tetes a besoin de Tesseract (mise en page) : {msg}")
    stats = {"pages": 0, "cibles": 0, "corrigees": 0, "restantes": 0, "non_alignees": 0, "requetes": 0, "duree_s": 0.0}
    t_debut = time.time()
    a_relire = []
    for p in cfg.pages:
        d, _ = lire_cache(cfg, p, 1, tolerer=True)
        if not d or d.get("blanche") or not d.get("entrees") or (d.get("methode") == "local" and d.get("raison") != "rescue"):
            continue
        cibles = {}
        for i, e in enumerate(d["entrees"]):
            if any(b.get("valide") for b in e["prenoms"]):
                continue
            translit, _eq = parse_description(e["description"])
            arabes = [b["arabe"] for b in e["prenoms"] if b["arabe"]]
            c = LH.cout_tete(translit, arabes) if translit else -1.0
            if c >= VA.SEUIL:
                cibles[i] = {"translit": translit, "cout": c, "arabes": arabes}
        if cibles:
            a_relire.append((p, d, cibles))
    log(f"relecture des têtes : {sum(len(c) for _, _, c in a_relire)} tête(s) incohérente(s) avec l'arabe sur {len(a_relire)} page(s) du cache.")
    if not a_relire:
        return stats
    info = resoudre_modele(cfg)
    afficher_info(info, cfg)
    etat = Etat(cfg, info, None)
    for p, d, cibles in a_relire:
        if etat.stop.is_set():
            break
        ident = id_chunk(p, 1, 1)
        net = nettoyer_page(image_native(cfg.pdf, p))
        lec = LH.lire_page(net)
        if not _page_alignee(lec, d["entrees"]):
            stats["non_alignees"] += len(cibles)
            log(f"  {ident} : la mise en page ne correspond pas au cache : {len(cibles)} tête(s) laissée(s) telle(s) quelle(s)", "WARN")
            continue
        for i in list(cibles):
            if not lec.entrees[i].tete_box:
                del cibles[i]
                stats["non_alignees"] += 1
        stats["pages"] += 1
        stats["cibles"] += len(cibles)
        restant, journal, gris = dict(cibles), [], net.convert("L")
        for essai in range(1, max(2, cfg.essais) + 1):
            if not restant or etat.stop.is_set():
                break
            indices = {i: f"lecture automatique approximative « {lec.entrees[i].tete_texte} » (jugée incohérente avec l'arabe de l'entrée)" for i in restant}
            feuille, _txt, reperes = LH.construire_feuille(lec, gris, seulement_tetes=indices)
            if feuille is None:
                break
            tampon = io.BytesIO()
            feuille.save(tampon, "PNG")
            temp = min(1.0, 0.2 + 0.2 * (essai - 1))
            try:
                rep = appeler_llm(cfg, info, tampon.getvalue(), LH.PROMPT_TETES, "Relis ces débuts de définition.", temperature=temp, top_p=0.95,
                                  max_tokens=200 + 80 * len(reperes), penser=False, budget=0, stop=etat.stop, suivi=None, pas_suivi=0)
            except (ErreurReflexion, ErreurLLM) as e:
                log(f"  {ident} têtes, essai {essai} : {e}", "WARN")
                continue
            if rep["abandon"] == "interruption":
                raise Interrompu()
            stats["requetes"] += 1
            dico = LH.extraire_feuille_json(rep["content"], "" if rep["abandon"] or rep["finish"] == "length" else rep["reasoning"]) or {}
            for rp, (_k, i, _j) in reperes.items():
                v = re.sub(r"\s+", " ", str(dico.get(rp, ""))).strip(" :")
                if not v or i not in restant:
                    continue
                cible = restant[i]
                nouveau = LH.cout_tete(v, cible["arabes"])
                adopte = bool(0 <= nouveau < VA.SEUIL and nouveau <= cible["cout"] - 0.5)
                evenement("relecture_tete", chunk=ident, repere=rp, essai=essai, avant=cible["translit"], propose=v, cout_avant=cible["cout"],
                          cout_propose=nouveau, adopte=adopte)
                if adopte:
                    m = re.match(r"^(.*?)\s*:\s+(.*)$", d["entrees"][i]["description"], re.S)
                    if m:
                        d["entrees"][i]["description"] = f"{v} : {m.group(2)}"
                        d["entrees"][i].setdefault("corrections_auto", []).append({"champ": "tête", "avant": m.group(1), "apres": v, "cout_avant": cible["cout"], "cout_apres": nouveau})
                        journal.append(i)
                        del restant[i]
        stats["corrigees"] += len(journal)
        stats["restantes"] += len(restant)
        if journal:
            d.setdefault("avertissements", []).append(f"relecture des têtes : {len(journal)} tête(s) corrigée(s)")
            ecrire_atomique(chemin_cache(cfg, p, 1), json.dumps(d, ensure_ascii=False, indent=1))
        log(f"  {ident} : {len(cibles)} tête(s) suspecte(s), {len(journal)} corrigée(s), {len(restant)} encore incohérente(s)")
    stats["duree_s"] = round(time.time() - t_debut, 1)
    log(f"TÊTES : {stats['corrigees']} corrigée(s) sur {stats['cibles']} suspecte(s) ; {stats['restantes']} restent incohérentes ; {stats['non_alignees']} non relue(s).")
    return stats


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
    faites = [p for p in infos if p["durée_s"]]
    ecrire_atomique(os.path.join(cfg.sortie, "score.json"), json.dumps(
        {"modele": cfg.modele, "methode": cfg.methode, "score": s, "pages": list(cfg.pages),
         "duree_moy_s": round(sum(p["durée_s"] for p in faites) / len(faites), 1) if faites else None,
         "tok_s_moy": next(iter([round(sum(v) / len(v), 1) for v in [[p["tok_s"] for p in faites if p["tok_s"] != ""]] if v]), None)},
        ensure_ascii=False, indent=1))
    if faites:
        moy = sum(p["durée_s"] for p in faites) / len(faites)
        log(f"   durée moyenne : {moy:.0f} s par page -> ≈ {fmt_duree(moy * (DICT_END - DICT_START + 1) / max(1, cfg.paralleles))} "
            f"pour les {DICT_END - DICT_START + 1} pages du dictionnaire (parallèle {cfg.paralleles}).")


# ============================================================================
# 12) CONFIGURATION, RAPPORT DE DEBUG, LIGNE DE COMMANDE
# ============================================================================

CONFIG_DEFAUT = os.environ.get("PRENOMS_CONFIG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "prenoms_config.json")

# réglages enregistrés dans le fichier de configuration (et modifiables dans la TUI) : noms = destinations argparse
CLES_CONFIG = ("pdf", "url", "modele", "api", "cle_api", "sortie", "pages", "debut", "fin", "paralleles", "decoupe", "nettoyage",
               "tokens_image", "max_cote", "reflexion", "budget_reflexion", "temperature", "top_p", "top_k", "repeat_penalty",
               "max_tokens", "timeout", "stall", "attente", "suivi", "essais", "export_tous", "corps_extra", "prompt_fichier",
               "ancien", "log_niveau", "methode", "tesseract", "tessdata", "tess_lang")


def creer_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Extraction du Livre des prénoms arabes avec un modèle de vision (LM Studio). "
                                 "Sans argument, dans un terminal : interface TUI.")
    ap.add_argument("pdf", nargs="?", default="", help="chemin du PDF")
    ap.add_argument("--url", default="http://localhost:1234", help="serveur LM Studio (défaut http://localhost:1234 ; « …/v1 » accepté)")
    ap.add_argument("--modele", default="prism-ml/bonsai-27b", help="nom du modèle CHARGÉ (l'identifiant exact est retrouvé automatiquement)")
    ap.add_argument("--api", choices=("auto", "natif", "openai"), default="auto", help="auto = API native /api/v1 si disponible (LM Studio ≥ 0.4)")
    ap.add_argument("--cle-api", default="", help="jeton d'API LM Studio si l'authentification est activée (ou variable LM_API_TOKEN)")
    ap.add_argument("--debut", type=int, default=DICT_START, help=f"première page PDF (défaut {DICT_START})")
    ap.add_argument("--fin", type=int, default=DICT_END, help=f"dernière page PDF (défaut {DICT_END})")
    ap.add_argument("--pages", default="", help="liste explicite, ex. 55,56,100-104 (remplace --debut/--fin)")
    ap.add_argument("--sortie", default="", help="dossier de sortie (défaut : sortie_llm à côté du script)")
    ap.add_argument("--paralleles", type=int, default=1, help="requêtes simultanées (voir README : Max Concurrent Predictions et contexte)")
    ap.add_argument("--decoupe", type=int, choices=(1, 2), default=1, help="2 = chaque page en deux moitiés (plus de détail par requête)")
    ap.add_argument("--nettoyage", choices=("oui", "non"), default="oui", help="efface le texte fantôme du verso (défaut oui)")
    ap.add_argument("--tokens-image", type=int, default=2500, help="cible de jetons d'image par requête ; l'image est réduite en conséquence (0 = image native)")
    ap.add_argument("--max-cote", type=int, default=0, help="plafond manuel du plus grand côté de l'image, en px (remplace --tokens-image)")
    ap.add_argument("--reflexion", choices=("auto", "non", "oui"), default="auto",
                    help="auto = coupée si LM Studio le permet ; oui = autorisée, mais bornée (--budget-reflexion)")
    ap.add_argument("--budget-reflexion", type=int, default=0, help="jetons de réflexion tolérés avant abandon (0 = automatique)")
    ap.add_argument("--temperature", type=float, default=None, help="défaut : 0,3 sans réflexion, 0,6 avec")
    ap.add_argument("--top-p", type=float, default=None, help="défaut : 0,8 sans réflexion, 0,95 avec")
    ap.add_argument("--top-k", type=int, default=20, help="0 = ne pas envoyer")
    ap.add_argument("--repeat-penalty", type=float, default=0.0, help="0 = ne pas envoyer")
    ap.add_argument("--max-tokens", type=int, default=8000, help="jetons de sortie max par requête, réflexion comprise (borné par le contexte chargé)")
    ap.add_argument("--timeout", type=int, default=1200, help="durée totale max d'une requête, en secondes")
    ap.add_argument("--stall", type=int, default=300, help="silence max entre deux jetons, en secondes")
    ap.add_argument("--attente", type=int, default=900, help="attente max du premier octet (préremplissage de l'image), en secondes")
    ap.add_argument("--suivi", type=int, default=20, help="secondes entre deux lignes de progression pendant une requête (0 = aucune)")
    ap.add_argument("--essais", type=int, default=3, help="tentatives par page")
    ap.add_argument("--export-tous", type=int, default=10, help="export intermédiaire tous les N pages terminées (0 = seulement à la fin)")
    ap.add_argument("--corps-extra", default="", help="JSON fusionné dans la requête, ex. '{\"min_p\": 0.05}'")
    ap.add_argument("--prompt-fichier", default="", help="remplace le prompt par défaut par le contenu de ce fichier")
    ap.add_argument("--refaire", action="store_true", help="ignore le cache et recalcule les pages demandées")
    ap.add_argument("--sauver-images", action="store_true", help="garde dans sortie/images les PNG envoyés au modèle")
    ap.add_argument("--ancien", default="", help="CSV de l'ancienne extraction OCR : produit comparaison_ancien.csv")
    ap.add_argument("--diagnostic", action="store_true", help="configuration chargée, vitesse, GPU, jetons d'entrée, réglages conseillés ; puis quitte")
    ap.add_argument("--relire-arabe", action="store_true", dest="relire_arabe",
                    help="relit AVEC réflexion les seuls mots arabes incohérents avec la translittération (cache existant), puis ré-exporte")
    ap.add_argument("--relire-tetes", action="store_true", dest="relire_tetes",
                    help="relit au modèle de vision les têtes de définition incohérentes avec l'arabe (arbitrage par l'arabe), puis ré-exporte")
    ap.add_argument("--exporter-problemes", action="store_true", dest="exporter_problemes",
                    help="écrit <sortie>/problemes/ : page HTML avec les recadrages des mots arabes à relire (à corriger toi-même)")
    ap.add_argument("--inclure-voyelles", action="store_true", dest="inclure_voyelles", help="avec --exporter-problemes : ajoute aussi les mots sans voyelles")
    ap.add_argument("--appliquer-corrections", default="", dest="appliquer_corrections", metavar="FICHIER",
                    help="applique corrections.csv (issu de problemes.html ou de problemes.csv) au cache, puis ré-exporte")
    ap.add_argument("--autotest", action="store_true", help="traite 4 pages et compare à la vérité terrain")
    ap.add_argument("--statut", action="store_true", help="état du cache (pages faites / à faire / périmées), sans LM Studio")
    ap.add_argument("--export", action="store_true", help="régénère CSV/JSON/Excel depuis le cache, sans LM Studio")
    ap.add_argument("--methode", choices=("auto", "hybride", "vlm"), default="auto",
                    help="auto = hybride (mise en page + Tesseract + feuille de recadrages) si Tesseract est installé, sinon modèle de vision seul")
    ap.add_argument("--tesseract", default="", help="chemin de tesseract(.exe) (défaut : automatique)")
    ap.add_argument("--tessdata", default="", help="dossier des modèles Tesseract, ex. tessdata_best (défaut : installation par défaut)")
    ap.add_argument("--tess-lang", default="fra", help="langue de Tesseract (défaut fra)")
    ap.add_argument("--rapport", action="store_true", help="crée un zip de debug (logs récents, configuration sans secrets, résumé du cache, sorties brutes) à m'envoyer")
    ap.add_argument("--tui", action="store_true", help="ouvre l'interface plein écran (par défaut quand on lance le script sans argument)")
    ap.add_argument("--cli", action="store_true", help="ne pas ouvrir la TUI quand on lance le script sans argument : affiche l'aide")
    ap.add_argument("--config", default="", help="fichier de configuration JSON (défaut : prenoms_config.json à côté du script, ou variable PRENOMS_CONFIG)")
    ap.add_argument("--ecrire-config", action="store_true", help="enregistre les options données dans le fichier de configuration, puis quitte")
    ap.add_argument("--log-niveau", type=str.upper, choices=("DEBUG", "INFO"), default="DEBUG", help="niveau écrit dans le fichier de log (défaut DEBUG : tout)")
    ap.add_argument("--version", action="version", version=f"extraire_prenoms_llm {VERSION}")
    return ap


def charger_config(chemin: str) -> dict:
    if not chemin or not os.path.exists(chemin):
        return {}
    try:
        with open(chemin, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        log(f"configuration illisible ({chemin}) : {e}", "WARN")
        return {}
    return d if isinstance(d, dict) else {}


def ecrire_config(chemin: str, valeurs: dict) -> None:
    ecrire_atomique(chemin, json.dumps({k: valeurs[k] for k in CLES_CONFIG if k in valeurs}, ensure_ascii=False, indent=2, sort_keys=True))


def valeurs_effectives(chemin: str | None = None) -> dict:
    """Valeurs par défaut des options, surchargées par le fichier de configuration (c'est ce que voit la TUI)."""
    ap = creer_parser()
    valides = {x.dest for x in ap._actions}
    ap.set_defaults(**{k: v for k, v in charger_config(chemin or CONFIG_DEFAUT).items() if k in valides})
    return vars(ap.parse_args([]))


def pages_depuis(a) -> list[int]:
    return parse_pages(a.pages) if a.pages else list(range(a.debut, a.fin + 1))


def construire_cfg(a) -> Config:
    """Options (argparse ou dictionnaire de la TUI) -> Config. Lève ValueError si une option est invalide."""
    sortie = a.sortie or os.path.join(os.path.dirname(os.path.abspath(__file__)), "sortie_llm")
    cfg = Config(pdf=a.pdf, url=a.url, modele=a.modele, sortie=sortie, paralleles=max(1, int(a.paralleles)), decoupe=int(a.decoupe),
                 nettoyage=a.nettoyage == "oui", max_cote=int(a.max_cote), tokens_image=int(a.tokens_image), api=a.api,
                 reflexion=a.reflexion, budget_reflexion=int(a.budget_reflexion), temperature=a.temperature, top_p=a.top_p,
                 top_k=int(a.top_k), repeat_penalty=float(a.repeat_penalty), max_tokens=int(a.max_tokens), timeout=int(a.timeout),
                 stall=int(a.stall), attente=int(a.attente), suivi=int(a.suivi), essais=max(1, int(a.essais)), refaire=bool(a.refaire),
                 sauver_images=bool(a.sauver_images), ancien=a.ancien, export_tous=int(a.export_tous), sans_llm=bool(a.export),
                 cle_api=a.cle_api, log_niveau=a.log_niveau, methode=a.methode, tesseract=a.tesseract,
                 tessdata=a.tessdata, tess_lang=a.tess_lang)
    if a.corps_extra:
        try:
            cfg.corps_extra = json.loads(a.corps_extra) if isinstance(a.corps_extra, str) else dict(a.corps_extra)
        except ValueError:
            raise ValueError("--corps-extra doit être du JSON valide")
        if not isinstance(cfg.corps_extra, dict):
            raise ValueError("--corps-extra doit être un objet JSON : {...}")
    if a.prompt_fichier:
        with open(a.prompt_fichier, encoding="utf-8") as f:
            cfg.prompt = f.read()
    cfg.pages = pages_depuis(a)
    SECRETS.update(x for x in (cfg.cle_api, os.environ.get("LM_API_TOKEN", "")) if x)
    return cfg


def cfg_pour_log(cfg: Config) -> dict:
    d = asdict(cfg)
    d["prompt"] = f"sha1:{hashlib.sha1(cfg.prompt.encode('utf-8'), usedforsecurity=False).hexdigest()[:10]} ({len(cfg.prompt)} caractères)"
    d["cle_api"] = "***" if cfg.cle_api else ""
    d["pages"] = f"{len(cfg.pages)} pages ({cfg.pages[0]}-{cfg.pages[-1]})" if cfg.pages else "aucune"
    return d


def _queue_fichier(chemin: str, max_octets: int) -> bytes:
    try:
        with open(chemin, "rb") as f:
            f.seek(0, os.SEEK_END)
            taille = f.tell()
            f.seek(max(0, taille - max_octets))
            data = f.read()
    except OSError:
        return b""
    return (b"[... debut tronque ...]\n" if taille > max_octets else b"") + data


def resume_cache_csv(cfg: Config) -> str:
    tampon = io.StringIO()
    w = csv.writer(tampon, lineterminator="\n")
    w.writerow(["page_pdf", "bande", "statut", "perime", "blanche", "entrees", "duree_s", "tokens", "tokens_reflexion",
                "tokens_entree", "tok_s", "ttft_s", "penser", "avertissements"])
    for p in cfg.pages:
        for b in range(1, cfg.decoupe + 1):
            d, perime = lire_cache(cfg, p, b, tolerer=True)
            if d is None:
                w.writerow([p, b, "a_faire"] + [""] * 11)
                continue
            w.writerow([p, b, d.get("statut"), perime, bool(d.get("blanche")), len(d.get("entrees") or []), d.get("duree"),
                        d.get("tokens"), d.get("tokens_reflexion"), d.get("tokens_entree"), d.get("tps"), d.get("ttft"),
                        d.get("penser"), " | ".join(d.get("avertissements") or [])])
    return tampon.getvalue()


def creer_rapport(cfg: Config, chemin_config: str = "", brut_max: int = 25) -> str:
    """Zip de debug prêt à envoyer : logs récents, environnement, configuration SANS secrets, résumé du cache, sorties brutes
    récentes. Ni PDF, ni images."""
    dossier = os.path.join(cfg.sortie, "rapports")
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, "rapport_debug_" + time.strftime("%Y%m%d_%H%M%S") + ".zip")
    dossier_logs = os.path.join(cfg.sortie, "logs")
    logs = sorted(n for n in os.listdir(dossier_logs) if n.endswith(".log")) if os.path.isdir(dossier_logs) else []
    brut = os.path.join(cfg.sortie, "cache", "brut")
    dumps = []
    if os.path.isdir(brut):
        dumps = sorted((os.path.join(brut, n) for n in os.listdir(brut) if "_essai" in n), key=os.path.getmtime, reverse=True)[:brut_max]
    with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED) as z:
        for n in logs[-3:]:
            z.writestr("logs/" + n, masquer(_queue_fichier(os.path.join(dossier_logs, n), 2_000_000).decode("utf-8", "replace")))
        if logs and os.path.exists(os.path.join(dossier_logs, logs[-1][:-4] + ".jsonl")):
            z.writestr("logs/" + logs[-1][:-4] + ".jsonl",
                       masquer(_queue_fichier(os.path.join(dossier_logs, logs[-1][:-4] + ".jsonl"), 2_000_000).decode("utf-8", "replace")))
        z.writestr("environnement.json", masquer(json.dumps(infos_environnement(), ensure_ascii=False, indent=2, default=str)))
        z.writestr("configuration.json", masquer(json.dumps(cfg_pour_log(cfg), ensure_ascii=False, indent=2, default=str)))
        if chemin_config and os.path.exists(chemin_config):
            z.writestr("prenoms_config.json", masquer(_queue_fichier(chemin_config, 100_000).decode("utf-8", "replace")))
        z.writestr("pages.csv", resume_cache_csv(cfg))
        for f in dumps:
            z.writestr("brut/" + os.path.basename(f), masquer(_queue_fichier(f, 20_000).decode("utf-8", "replace")))
        z.writestr("LISEZMOI.txt", (
            f"Rapport de debug généré le {time.strftime('%Y-%m-%d %H:%M:%S')} (extraire_prenoms_llm {VERSION}).\n\n"
            "logs/*.log      journal lisible des dernières sessions (heure, niveau, fil, message)\n"
            "logs/*.jsonl    événements structurés de la dernière session (requêtes, pages, GPU, erreurs)\n"
            "environnement.json   Python, système, versions des paquets, GPU (nvidia-smi)\n"
            "configuration.json   réglages utilisés (prompt résumé par son empreinte, jeton d'API masqué)\n"
            "pages.csv       état de chaque page dans le cache (statut, durée, jetons, tok/s, avertissements)\n"
            "brut/           dernières sorties brutes des essais ratés (fin des réponses et des réflexions)\n\n"
            "Le zip ne contient ni le PDF, ni les images des pages, ni de secret.\n"))
    return chemin


def installer_hooks() -> None:
    """Toute exception non rattrapée (fil principal ou secondaire) finit, avec sa trace, dans le fichier de log."""
    if getattr(sys.excepthook, "_prenoms", False):
        return

    def hook(type_, valeur, tb):
        if not issubclass(type_, KeyboardInterrupt):
            log_exception("exception non rattrapée", valeur)
        sys.__excepthook__(type_, valeur, tb)

    def hook_fil(args):
        if args.exc_type is not SystemExit and args.exc_value is not None:
            log_exception(f"exception non rattrapée dans le fil {args.thread.name if args.thread else '?'}", args.exc_value)

    hook._prenoms = True
    sys.excepthook = hook
    threading.excepthook = hook_fil


def journaliser_demarrage(mode: str, argv, cfg: Config) -> None:
    """En-tête du journal : version, arguments (secrets masqués), environnement, configuration. Le texte aide à la lecture,
    l'événement JSONL aux analyses automatiques."""
    env = infos_environnement()
    cmd = masquer(" ".join(str(x) for x in argv))
    debug(f"version {VERSION} — mode {mode}")
    debug(f"arguments : {cmd}")
    debug("environnement : " + json.dumps(env, ensure_ascii=False, default=str))
    debug("configuration : " + json.dumps(cfg_pour_log(cfg), ensure_ascii=False, default=str))
    evenement("demarrage", version=VERSION, mode=mode, argv=cmd, config=cfg_pour_log(cfg), environnement=env)


def lancer_tui(chemin_config: str) -> int:
    sys.modules.setdefault("extraire_prenoms_llm", sys.modules[__name__])      # même instance du moteur si lancé en script
    try:
        import prenoms_tui
    except ImportError as e:
        if getattr(e, "name", "") in ("textual", "rich") or "textual" in str(e):
            print("L'interface TUI nécessite le paquet « textual » :  pip install textual\n"
                  "Sans lui, utilise les options en ligne de commande (python extraire_prenoms_llm.py --help).")
        else:
            print(f"Impossible de charger prenoms_tui.py ({e}). Il doit se trouver à côté de ce script.")
        return 2
    return prenoms_tui.lancer(chemin_config)


def _terminal_interactif() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default="")
    chemin_cfg = pre.parse_known_args(argv)[0].config or CONFIG_DEFAUT
    ap = creer_parser()
    valides = {x.dest for x in ap._actions}
    ap.set_defaults(**{k: v for k, v in charger_config(chemin_cfg).items() if k in valides})
    a = ap.parse_args(argv)
    repetees = sorted({o for o in argv if o.startswith("--") and "=" not in o and argv.count(o) > 1})
    for o in repetees:
        print(f"ATTENTION : l'option {o} est donnée plusieurs fois, seule la DERNIÈRE compte (argparse écrase en silence).", file=sys.stderr)
    installer_hooks()
    if a.tui or (not argv and not a.cli and _terminal_interactif()):
        return lancer_tui(chemin_cfg)
    if not argv or argv == ["--cli"]:
        ap.print_help()                                        # rien à faire : l'aide plutôt qu'une erreur d'usage
        return 0
    if not (a.export or a.statut or a.diagnostic or a.rapport or a.ecrire_config or a.appliquer_corrections):
        a.pdf = trouver_pdf(a.pdf)
    try:
        cfg = construire_cfg(a)
    except (ValueError, OSError) as e:
        ap.error(str(e))
    if a.ecrire_config:
        ecrire_config(chemin_cfg, {k: getattr(a, k) for k in CLES_CONFIG})
        log(f"configuration enregistrée : {chemin_cfg}")
        return 0
    if a.rapport:
        log(f"rapport de debug : {creer_rapport(cfg, chemin_cfg)}")
        return 0
    mode = "diagnostic" if a.diagnostic else "autotest" if a.autotest else "relecture" if a.relire_arabe or a.relire_tetes else "problemes" if a.exporter_problemes else "corrections" if a.appliquer_corrections else "statut" if a.statut else "export" if a.export else "extraction"
    JOURNAL.ouvrir(os.path.join(cfg.sortie, "logs"), mode, cfg.log_niveau)
    log(f"journal : {JOURNAL.chemin}")
    journaliser_demarrage(mode, argv, cfg)
    try:
        if a.diagnostic:
            diagnostic(cfg)
            return 0
        if a.statut:
            afficher_statut(cfg)
            return 0
        if not a.export and not a.appliquer_corrections and (not a.pdf or not os.path.exists(a.pdf)):
            ap.error("indique le chemin du PDF (fichier introuvable)")
        if a.autotest:
            autotest(cfg)
            return 0
        if a.exporter_problemes:
            exporter_problemes(cfg, a.inclure_voyelles)
            return 0
        if a.appliquer_corrections:
            appliquer_corrections_cli(cfg, a.appliquer_corrections)
            cfg.sans_llm = True
        if a.relire_tetes:
            relire_tetes(cfg)
            cfg.sans_llm = True
        if a.relire_arabe:
            st_rel = relire_arabe(cfg)
            ecrire_atomique(os.path.join(cfg.sortie, "relecture_stats.json"), json.dumps({"modele": cfg.modele, **st_rel}, ensure_ascii=False, indent=1))
            cfg.sans_llm = True                                # ré-export depuis le cache (relectures comprises)
        rows, infos, avert = run_pipeline(cfg)
        if cfg.fatal and not rows:
            sys.exit(2)                                        # rien à exporter
        ch = exporter(rows, infos, cfg.sortie)
        evenement("export", intermediaire=False, lignes=len(rows), **ch)
        resume(rows, infos, avert)
        if cfg.ancien:
            comparer_ancien(rows, cfg.ancien, cfg.pages, cfg.sortie)
        log("Fichiers : " + " | ".join(ch.values()))
        log(f"Journal complet : {JOURNAL.chemin}  (en cas de problème, `--rapport` crée un zip à m'envoyer)")
        if cfg.fatal:
            sys.exit(2)
        return 130 if cfg.interrompu else 0
    except (ErreurFatale, ErreurLLM) as e:
        log(f"ERREUR : {e}", "ERROR")
        evenement("fatal", message=str(e))
        sys.exit(2)
    except KeyboardInterrupt:
        log("interruption.")
        return 130
    except Exception as e:                                     # noqa: BLE001 : trace complète dans le journal
        log_exception("erreur inattendue", e)
        log(f"Le détail est dans le fichier de log : {JOURNAL.chemin} ; `--rapport` crée un zip à m'envoyer.", "ERROR")
        sys.exit(1)
    finally:
        JOURNAL.fermer()


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    if code == 130:
        os._exit(130)          # les requêtes encore en vol ne doivent pas retarder la sortie
    sys.exit(code)
