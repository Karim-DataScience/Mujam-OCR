#!/usr/bin/env python3
"""Benchmark de la LECTURE DE L'ARABE : Tesseract (variantes), EasyOCR, PaddleOCR, modèles de vision de LM Studio, et leurs combinaisons.

Deux niveaux de mesure, sur une vérité terrain (benchmark/verite_arabe.json : pages + zones arabes recadrables + texte attendu) :
  A. « recadrage »  : on donne au lecteur UNE zone arabe isolée. Mesure sa capacité de lecture seule (zone supposée trouvée).
  B. « page »       : on donne la page entière et on lui demande tout l'arabe. Mesure zone + lecture ensemble (ce que fait le moteur
                      quand le modèle lit la page entière). L'écart A/B dit si un pipeline « je trouve la zone, puis je la lis » vaut le coup.
Combinaisons évaluées à partir des lectures du niveau A : accord entre deux lecteurs (précision quand ils s'accordent, part de zones
couvertes), « oracle » (meilleur lecteur par zone = plafond d'un arbitre parfait), correction d'un VLM guidé par la lecture Tesseract (`:indice`).

Exemples (Windows : python benchmark_arabe.py ...)
  python benchmark_arabe.py --telecharger-tessdata              (télécharge ara.traineddata fast + best dans benchmark/tessdata)
  python benchmark_arabe.py --lister-lmstudio                   (modèles de LM Studio, avec leur type : llm / vlm)
  python benchmark_arabe.py --complet                           (Tesseract + EasyOCR + PaddleOCR + TOUS les modèles de vision de LM Studio)
  python benchmark_arabe.py --complet --echantillon 2 --reprendre   (2x plus rapide ; reprend là où une exécution interrompue s'est arrêtée)
  python benchmark_arabe.py --vlm qwen gemma internvl --candidats easyocr tess:ara:fast:psm7:x3:bin
  python benchmark_arabe.py --candidats lm:google/gemma-4-12b-qat lm:google/gemma-4-12b-qat:indice=easyocr
Un candidat qui ne peut pas tourner (modèle absent, paquet non installé) est ignoré avec un message, jamais fatal. Les résultats sont écrits
après CHAQUE candidat (rien n'est perdu si tu interromps). Un modèle qui ne sait pas lire les images est abandonné après 3 erreurs de suite.
Normalisation : on ne garde que les lettres arabes ; voyelles et tatwil retirés ; « léger » unifie en plus alef (أإآٱ), ya (ى), hamza sur support.
CER = distance d'édition / longueur attendue, espaces ignorés (une lecture « عبدالاحد » n'est pas pénalisée pour l'espace)."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request

from PIL import Image

RACINE = os.path.dirname(os.path.abspath(__file__))
VERITE_DEFAUT = os.path.join(RACINE, "benchmark", "verite_arabe.json")
DIACRITIQUES = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
LETTRE = re.compile("[\u0621-\u064A\u0671-\u06D3\u06FA-\u06FF]")
TESSDATA_LOCAL = os.path.join(RACINE, "benchmark", "tessdata")
PROMPT_CROP = ("Recopie exactement le texte arabe écrit sur cette image. Réponds uniquement par le texte arabe, sans traduction, "
               "sans translittération, sans commentaire. Si tu hésites sur une lettre, donne ta meilleure lecture.")
PROMPT_INDICE = ("Recopie exactement le texte arabe écrit sur cette image. Un OCR classique l'a lu « {indice} » (cette lecture peut contenir "
                 "des erreurs : corrige-la d'après l'image). Réponds uniquement par le texte arabe, sans traduction, sans commentaire.")
_executer = subprocess.run                                          # remplaçable dans les tests (aucune commande réelle)
DEPOTS_TESSDATA = {"fast": "tessdata", "best": "tessdata_best"}         # « fast » = dépôt `tessdata` (modèles par défaut des installeurs)
PROMPT_PAGE = ("Liste, un par ligne, chaque mot ou groupe de mots écrit en alphabet arabe sur cette page, dans l'ordre de lecture "
               "(de haut en bas). Ne traduis pas, ne translittère pas, n'ajoute rien d'autre que le texte arabe.")


# ----------------------------------------------------------------------------------------------- normalisation et métriques
def normaliser(s: str, niveau: str = "leger") -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = DIACRITIQUES.sub("", s)
    s = "".join(c if LETTRE.match(c) else " " for c in s)
    if niveau == "leger":
        s = (s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ٱ", "ا").replace("ى", "ي").replace("ئ", "ي")
             .replace("ؤ", "و").replace("ء", "").replace("ی", "ي").replace("ک", "ك"))
    return re.sub(r"\s+", " ", s).strip()


def distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prec = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prec[j] + 1, cur[j - 1] + 1, prec[j - 1] + (ca != cb)))
        prec = cur
    return prec[-1]


def cer(attendu: str, lu: str, niveau: str = "leger") -> float:
    a, b = normaliser(attendu, niveau).replace(" ", ""), normaliser(lu, niveau).replace(" ", "")
    return distance(a, b) / max(1, len(a))


def arabe_des_lignes(texte: str) -> list[str]:
    """Lignes d'une sortie brute -> séquences de lettres arabes (une par ligne ayant de l'arabe)."""
    out = []
    for ligne in (texte or "").splitlines():
        n = normaliser(ligne, "strict")
        if n:
            out.append(n)
    return out


def apparier_page(attendus: list[str], lignes: list[str], seuil: float = 0.34) -> dict:
    """Rappel / précision d'une lecture de page : chaque zone attendue est cherchée parmi les lignes lues (une ligne ne sert qu'une fois)."""
    libres = list(range(len(lignes)))
    trouves, cers = 0, []
    for att in attendus:
        meilleur, mj = 9.0, None
        for j in libres:
            c = cer(att, lignes[j])
            if c < meilleur:
                meilleur, mj = c, j
        if mj is not None and meilleur <= seuil:
            trouves += 1
            cers.append(meilleur)
            libres.remove(mj)
    return {"rappel": trouves / max(1, len(attendus)), "precision": trouves / max(1, len(lignes)) if lignes else 0.0,
            "cer_trouves": (sum(cers) / len(cers)) if cers else None, "lignes": len(lignes), "attendus": len(attendus)}


# ----------------------------------------------------------------------------------------------- lecteurs
# ----------------------------------------------------------------------------------------------- lecteurs
class Lecteur:
    nom = "?"

    def disponible(self) -> tuple[bool, str]:
        return True, ""

    def lire_crop(self, img: Image.Image) -> str:
        raise NotImplementedError

    def lire_page(self, img: Image.Image) -> list[str]:
        return arabe_des_lignes(self.lire_crop(img))


class LecteurTesseract(Lecteur):
    """tess[:ara|ara+eng][:fast|best|<clé de --tessdata>][:psmN][:x2|x3][:bin]  (ex. tess:ara:best:psm7:x3:bin)
    Le dossier de langues est donné par la variable TESSDATA_PREFIX (pas par --tessdata-dir : les guillemets autour d'un chemin Windows
    étaient conservés tels quels par pytesseract et le fichier ara.traineddata restait introuvable)."""

    def __init__(self, spec: str, tessdata: dict, cmd: str = ""):
        self.nom = spec
        self.lang, self.dossier, self.psm, self.echelle, self.bin = "ara", "", 7, 1, False
        for p in spec.split(":")[1:]:
            if p.startswith("ara"):
                self.lang = p
            elif p.startswith("psm"):
                self.psm = int(p[3:])
            elif re.fullmatch(r"x\d", p):
                self.echelle = int(p[1:])
            elif p == "bin":
                self.bin = True
            elif p in tessdata:
                self.dossier = tessdata[p]
            else:
                raise ValueError(f"option Tesseract inconnue « {p} » dans {spec} (clés --tessdata connues : {sorted(tessdata)} ; "
                                 "lance --telecharger-tessdata pour obtenir fast et best)")
        self.cmd = cmd
        self.dossier = dossier_ascii(self.dossier)

    def _poser_env(self):
        if self.dossier:
            os.environ["TESSDATA_PREFIX"] = self.dossier
        if self.cmd:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = self.cmd

    def disponible(self):
        try:
            import pytesseract
            self._poser_env()
            langues = pytesseract.get_languages(config="")
        except Exception as e:                                       # noqa: BLE001
            return False, f"Tesseract / pytesseract indisponible : {e}"
        manque = [l for l in self.lang.split("+") if l not in langues]
        if manque:
            return False, (f"langue(s) Tesseract absente(s) : {manque}" + (f" dans {self.dossier}" if self.dossier else "")
                           + " (python benchmark_arabe.py --telecharger-tessdata)")
        return True, ""

    @staticmethod
    def _cfg(psm: int) -> str:
        return f"--psm {psm} --oem 1"

    def _prep(self, img: Image.Image, page: bool) -> Image.Image:
        g = img.convert("L")
        if self.echelle > 1 and not page:
            g = g.resize((g.width * self.echelle, g.height * self.echelle), Image.LANCZOS)
        if self.bin:
            try:
                import cv2
                import numpy as np
                _, b = cv2.threshold(np.array(g), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                g = Image.fromarray(b)
            except ImportError:
                pass
        if not page:                                                 # marge blanche : Tesseract lit mal un texte collé aux bords
            fond = Image.new("L", (g.width + 24, g.height + 24), 255)
            fond.paste(g, (12, 12))
            g = fond
        return g

    def lire_crop(self, img):
        import pytesseract
        self._poser_env()
        return pytesseract.image_to_string(self._prep(img, False), lang=self.lang, config=self._cfg(self.psm)).strip()

    def lire_page(self, img):
        import pytesseract
        self._poser_env()
        return arabe_des_lignes(pytesseract.image_to_string(self._prep(img, True), lang=self.lang, config=self._cfg(6)))


class LecteurEasyOCR(Lecteur):
    nom = "easyocr"

    def __init__(self):
        self._r = None

    def disponible(self):
        try:
            import easyocr                                           # noqa: F401
            return True, ""
        except ImportError:
            return False, "paquet easyocr non installé (pip install easyocr ; il télécharge ses modèles au premier lancement)"

    def _reader(self):
        if self._r is None:
            import easyocr
            self._r = easyocr.Reader(["ar", "en"], gpu=True, verbose=False)
        return self._r

    def lire_crop(self, img):
        import numpy as np
        return " ".join(self._reader().readtext(np.array(img.convert("RGB")), detail=0, paragraph=False))

    def lire_page(self, img):
        import numpy as np
        return arabe_des_lignes("\n".join(self._reader().readtext(np.array(img.convert("RGB")), detail=0, paragraph=False)))


class LecteurPaddle(Lecteur):
    """NON TESTÉ dans le bac à sable (API variable selon la version de PaddleOCR) : peut demander un ajustement."""
    nom = "paddleocr"

    def __init__(self):
        self._o = None

    def disponible(self):
        try:
            import paddleocr                                         # noqa: F401
            return True, ""
        except ImportError:
            return False, "paquet paddleocr non installé (pip install paddlepaddle paddleocr)"

    @staticmethod
    def _textes(res) -> list[str]:
        out = []
        for r in res or []:
            d = r if hasattr(r, "get") else getattr(r, "json", {}) or {}
            d = d.get("res", d) if hasattr(d, "get") else {}
            out += [str(t) for t in (d.get("rec_texts") or [])]
        return out

    def _ocr(self, img) -> list[str]:
        import numpy as np
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        os.environ.setdefault("FLAGS_use_mkldnn", "0")               # évite l'erreur oneDNN « ConvertPirAttribute2RuntimeAttribute » (Paddle 3.x, CPU Windows)
        import paddleocr
        if self._o is None:
            try:                                                      # PaddleOCR 3.x : sans les modèles annexes (orientation, dé-gauchissement), sans oneDNN
                self._o = paddleocr.PaddleOCR(lang="ar", use_doc_orientation_classify=False, use_doc_unwarping=False,
                                              use_textline_orientation=False, enable_mkldnn=False)
            except (TypeError, ValueError):
                self._o = paddleocr.PaddleOCR(lang="ar")
        a = np.array(img.convert("RGB"))
        if hasattr(self._o, "predict"):                              # PaddleOCR 3.x
            return self._textes(self._o.predict(a))
        res = self._o.ocr(a, cls=False)                              # PaddleOCR 2.x
        return [l[1][0] for page in (res or []) for l in (page or [])]

    def lire_crop(self, img):
        return " ".join(self._ocr(img))

    def lire_page(self, img):
        return arabe_des_lignes("\n".join(self._ocr(img)))


class LecteurLMStudio(Lecteur):
    """lm:<id du modèle>[:indice[=tess|easyocr]]. `:indice` = le modèle reçoit aussi la lecture d'un OCR classique et la corrige.
    Diagnostic : on garde la raison d'arrêt (`finish_reason`), le nombre de jetons et la présence d'un raisonnement ; une réponse vide est
    relancée avec un budget de jetons x4 (un modèle « raisonneur » peut épuiser max_tokens dans sa réflexion et ne rien renvoyer)."""

    def __init__(self, spec: str, url: str, indice: Lecteur | None, hauteur: int = 128, timeout: float = 240.0,
                 max_tokens: int = 1024, relances: int = 1, contexte: int = 16384, charger: bool = True, api: str = "auto",
                 budget_reflexion: int = 2000):
        self.nom = spec
        m = re.fullmatch(r"lm:(.*?):indice(?:=(\w+))?", spec)
        self.est_indice = bool(m)
        self.modele = m.group(1) if m else spec[3:]
        self.source_indice = (m.group(2) or "tess") if m else ""
        self.url, self.hint, self.hauteur, self.timeout = url.rstrip("/"), indice, hauteur, timeout
        self.max_tokens, self.relances = max_tokens, relances
        self.contexte, self.charger_modele, self.api, self.budget_reflexion = contexte, charger, api, budget_reflexion
        self.derniere: dict = {}
        self.natif, self.cfg, self.info, self.conditions = False, None, None, {}

    def _racine(self) -> str:
        return self.url[:-3] if self.url.endswith("/v1") else self.url

    def _sonder_natif(self) -> bool:
        """API native de LM Studio (>= 0.4) : /api/v1/models. Seule elle permet de couper la réflexion et de connaître le contexte chargé."""
        try:
            with urllib.request.urlopen(self._racine() + "/api/v1/models", timeout=5) as r:
                d = json.load(r)
            return isinstance(d, dict) and isinstance(d.get("models"), list)
        except Exception:                                            # noqa: BLE001
            return False

    def _preparer_natif(self):
        """Mêmes conditions que le moteur : le modèle est (re)chargé avec un contexte explicite (`lms load --context-length`), puis retrouvé par
        l'API native (contexte réel, vision, réglage de réflexion) ; les requêtes passent par X.appeler_llm avec la réflexion COUPÉE."""
        import comparer_modeles as C
        import extraire_prenoms_llm as X
        lms = C.trouver_lms()
        charge_par_script = False
        if self.charger_modele and lms:
            r = C.charger(C.Executeur(), lms, {"nom": self.modele, "ctx": self.contexte, "gpu": "max", "parallel": 1}, self.url)
            if not r["ok"]:
                raise RuntimeError(r["message"])
            charge_par_script = True
        self.cfg = X.Config(url=self._racine(), modele=self.modele, timeout=self.timeout, api="natif")
        self.info = X.resoudre_modele(self.cfg)                      # ErreurFatale si non chargé / sans vision
        self.conditions = {"api": "natif", "contexte": self.info.ctx, "reflexion_coupable": "off" in self.info.options_reflexion,
                           "quant": self.info.quant, "taille_go": self.info.taille_go, "vision": self.info.vision,
                           "charge_par_le_script": charge_par_script}
        if self.info.ctx and self.info.ctx < 8192:
            self.conditions["alerte"] = f"contexte chargé de {self.info.ctx} jetons : risque de troncature (image + consigne + réponse)"

    def _appel_natif(self, img: Image.Image, prompt: str, max_tokens: int) -> tuple[str, dict]:
        import extraire_prenoms_llm as X
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        r = X.appeler_llm(self.cfg, self.info, buf.getvalue(), None, prompt, temperature=0.3, top_p=0.8, max_tokens=max_tokens,
                          penser=False, budget=self.budget_reflexion)
        if r.get("erreur"):
            raise RuntimeError(str(r["erreur"]))
        meta = {"finish": r.get("finish"), "tokens": r.get("tokens"), "tokens_reflexion": r.get("tokens_reflexion"), "abandon": r.get("abandon"),
                "raisonnement": bool(r.get("tokens_reflexion") or r.get("reasoning"))}
        return re.sub(r"<think>.*?</think>", "", r.get("content") or "", flags=re.S).strip(), meta

    def disponible(self):
        try:
            with urllib.request.urlopen(self.url + "/models", timeout=10) as r:
                ids = [m["id"] for m in json.load(r).get("data", [])]
        except Exception as e:                                       # noqa: BLE001
            return False, f"LM Studio injoignable sur {self.url} ({e}) : serveur local démarré ?"
        if self.modele not in ids:
            return False, f"modèle « {self.modele} » introuvable dans LM Studio (--lister-lmstudio pour les identifiants exacts)"
        if self.est_indice and (self.hint is None or not self.hint.disponible()[0]):
            return False, f"le mode :indice={self.source_indice} exige que ce lecteur soit disponible ({self.hint.disponible()[1] if self.hint else 'absent'})"
        self.natif = self.api != "openai" and self._sonder_natif()
        return True, ""

    def _appel(self, img: Image.Image, prompt: str, max_tokens: int, timeout: float | None = None) -> tuple[str, dict]:
        if self.natif and self.info is not None:
            return self._appel_natif(img, prompt, max_tokens)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        corps = {"model": self.modele, "temperature": 0, "max_tokens": max_tokens,
                 "messages": [{"role": "user", "content": [
                     {"type": "text", "text": prompt},
                     {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}}]}]}
        rq = urllib.request.Request(self.url + "/chat/completions", json.dumps(corps).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(rq, timeout=timeout or self.timeout) as r:
            data = json.load(r)
        ch = data["choices"][0]
        msg = ch.get("message", {})
        texte = msg.get("content") or ""
        if isinstance(texte, list):
            texte = " ".join(p.get("text", "") for p in texte if isinstance(p, dict))
        meta = {"finish": ch.get("finish_reason"), "tokens": (data.get("usage") or {}).get("completion_tokens"),
                "raisonnement": bool(msg.get("reasoning_content") or msg.get("reasoning") or "<think>" in texte)}
        return re.sub(r"<think>.*?</think>", "", texte, flags=re.S).strip(), meta

    def echauffer(self):
        """Hors mesure : charge le modèle (mode moteur : contexte explicite, réflexion coupée) ou, en mode OpenAI, fait un premier appel
        (LM Studio charge alors le modèle à la demande, avec ses réglages par défaut)."""
        if self.natif:
            self._preparer_natif()
            return
        self.conditions = {"api": "openai", "contexte": None, "reflexion_coupable": False,
                           "alerte": "API OpenAI : réflexion et contexte non pilotés (LM Studio < 0.4 ou --api openai)"}
        self._appel(Image.new("RGB", (64, 64), "white"), "Réponds : OK", 8, timeout=900)

    def _agrandir(self, img: Image.Image) -> Image.Image:
        if img.height >= self.hauteur:
            return img
        f = min(6.0, self.hauteur / max(1, img.height))
        return img.resize((int(img.width * f), int(img.height * f)), Image.LANCZOS)

    def _lire(self, img: Image.Image, prompt: str, max_tokens: int) -> str:
        texte, meta = self._appel(img, prompt, max_tokens)
        essais = 0
        while not normaliser(texte, "strict") and essais < self.relances:
            essais += 1
            texte, meta2 = self._appel(img, prompt, max_tokens * 4)
            meta = {**meta2, "relance": essais, "finish_initial": meta.get("finish")}
        self.derniere = meta
        return texte

    def lire_crop(self, img):
        prompt = PROMPT_CROP
        if self.est_indice:
            prompt = PROMPT_INDICE.format(indice=normaliser(self.hint.lire_crop(img), "strict") or "illisible")
        return self._lire(self._agrandir(img), prompt, self.max_tokens)

    def lire_page(self, img):
        return arabe_des_lignes(self._lire(img, PROMPT_PAGE, self.max_tokens * 2))


# ----------------------------------------------------------------------------------------------- LM Studio : découverte, déchargement
def modeles_lmstudio(url: str) -> list[dict]:
    """[{id, type}] ; `type` = llm / vlm / embeddings d'après l'API native de LM Studio (/api/v0/models), « ? » si indisponible."""
    base = url.rstrip("/")
    racine = base[:-3] if base.endswith("/v1") else base
    try:
        with urllib.request.urlopen(racine + "/api/v0/models", timeout=10) as r:
            return [{"id": m["id"], "type": m.get("type", "?"), "etat": m.get("state", "")} for m in json.load(r).get("data", [])]
    except Exception:                                                # noqa: BLE001
        with urllib.request.urlopen(base + "/models", timeout=10) as r:
            return [{"id": m["id"], "type": "?", "etat": ""} for m in json.load(r).get("data", [])]


def choisir_vlm(url: str, motifs: list[str], exclusions: list[str]) -> list[str]:
    ms = modeles_lmstudio(url)
    if not any(m["type"] == "vlm" for m in ms):
        raise ValueError("LM Studio ne dit pas quels modèles sont des modèles de vision (API /api/v0 absente ou aucun « vlm ») : "
                         "donne-les à la main avec --candidats lm:<id> (ids exacts : --lister-lmstudio)")
    ids = sorted(m["id"] for m in ms if m["type"] == "vlm")
    if motifs:
        ids = [i for i in ids if any(p.lower() in i.lower() for p in motifs)]
    return [i for i in ids if not any(x.lower() in i.lower() for x in exclusions)]


def decharger_lmstudio() -> bool:
    exe = shutil.which("lms")
    if not exe:
        return False
    try:
        subprocess.run([exe, "unload", "--all"], capture_output=True, timeout=90)
        return True
    except Exception:                                                # noqa: BLE001
        return False


def telecharger_tessdata(destination: str = TESSDATA_LOCAL, log=print) -> dict:
    """Télécharge ara.traineddata (versions fast et best) depuis github.com/tesseract-ocr ; rend {clé: dossier}."""
    res = {}
    for cle, depot in DEPOTS_TESSDATA.items():
        dossier = os.path.join(destination, cle)
        cible = os.path.join(dossier, "ara.traineddata")
        if not os.path.isfile(cible) or os.path.getsize(cible) < 100_000:
            os.makedirs(dossier, exist_ok=True)
            url = f"https://raw.githubusercontent.com/tesseract-ocr/{depot}/main/ara.traineddata"
            log(f"téléchargement {url} …")
            with urllib.request.urlopen(url, timeout=120) as r, open(cible + ".part", "wb") as f:
                shutil.copyfileobj(r, f)
            os.replace(cible + ".part", cible)
        res[cle] = dossier
    return res


def tessdata_connus(a) -> dict:
    d = dict(kv.split("=", 1) for kv in (a.tessdata or []))
    for cle in DEPOTS_TESSDATA:                                      # dossiers téléchargés par --telecharger-tessdata
        p = os.path.join(TESSDATA_LOCAL, cle)
        if cle not in d and os.path.isfile(os.path.join(p, "ara.traineddata")):
            d[cle] = p
    return d


def dossier_ascii(dossier: str) -> str:
    """Tesseract sous Windows ne charge pas toujours les langues depuis un chemin contenant des accents (ex. D:\\Téléchargements\\…). Si le
    chemin n'est pas ASCII, les fichiers .traineddata sont copiés dans un dossier ASCII (temp, PUBLIC…) et c'est celui-là qui est utilisé."""
    if not dossier or dossier.isascii() or not os.path.isdir(dossier):
        return dossier
    for base in (tempfile.gettempdir(), os.environ.get("PUBLIC", ""), os.environ.get("ALLUSERSPROFILE", "")):
        if not base or not base.isascii():
            continue
        cible = os.path.join(base, "tessdata_arabe", hashlib.md5(dossier.encode("utf-8")).hexdigest()[:8])
        try:
            os.makedirs(cible, exist_ok=True)
            for nom in os.listdir(dossier):
                if nom.endswith(".traineddata"):
                    src, dst = os.path.join(dossier, nom), os.path.join(cible, nom)
                    if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(src):
                        shutil.copyfile(src, dst)
            return cible
        except OSError:
            continue
    return dossier


def construire(spec: str, a: argparse.Namespace) -> list[Lecteur]:
    if "<" in spec or ">" in spec:
        raise ValueError(f"« {spec} » : remplace le texte entre < > par un vrai identifiant (liste exacte : python benchmark_arabe.py --lister-lmstudio)")
    tessdata = tessdata_connus(a)
    if spec == "grille:tess":                                        # grille de variantes Tesseract
        cles = list(tessdata) or [""]
        return [LecteurTesseract(":".join(["tess", "ara"] + ([k] if k else []) + [f"psm{p}"] + ([f"x{x}"] if x > 1 else []) + (["bin"] if b else [])),
                                 tessdata, a.tesseract_cmd)
                for k in cles for p in (7, 13) for x in (1, 3) for b in (False, True)]
    if spec.startswith("tess"):
        return [LecteurTesseract(spec, tessdata, a.tesseract_cmd)]
    if spec == "easyocr":
        return [LecteurEasyOCR()]
    if spec == "paddleocr":
        return [LecteurPaddle()]
    if spec.startswith("lm:"):
        m = re.fullmatch(r"lm:(.*?):indice(?:=(\w+))?", spec)
        hint = None
        if m:
            source = m.group(2) or "tess"
            hint = LecteurEasyOCR() if source == "easyocr" else LecteurTesseract(a.indice_tess, tessdata, a.tesseract_cmd)
        return [LecteurLMStudio(spec, a.url, hint, a.hauteur_vlm, a.timeout, a.max_tokens, a.relances,
                                getattr(a, "contexte", 16384), not getattr(a, "sans_chargement", False), getattr(a, "api", "auto"),
                                getattr(a, "budget_reflexion", 2000))]
    raise ValueError(f"candidat inconnu « {spec} » (tess…, grille:tess, easyocr, paddleocr, lm:<modèle>[:indice[=tess|easyocr]])")


# ----------------------------------------------------------------------------------------------- exécution
def charger_verite(chemin: str) -> list[dict]:
    with open(chemin, encoding="utf-8") as f:
        doc = json.load(f)
    base = os.path.dirname(os.path.abspath(chemin))
    for p in doc["pages"]:
        p["chemin"] = os.path.join(base, p["image"])
    return doc["pages"]


def recadrer(img: Image.Image, bbox, marge: int = 2) -> Image.Image:
    x0, y0, x1, y1 = bbox
    return img.crop((max(0, x0 - marge), max(0, y0 - marge), min(img.width, x1 + marge), min(img.height, y1 + marge)))


def evaluer(lecteur: Lecteur, pages: list[dict], niveau_page: bool, log=print, pas: int = 1, budget_s: float | None = None,
            niveau_crop: bool = True) -> dict:
    """`pas` : une zone sur `pas` (échantillon rapide, identique pour tous les candidats). Abandon (res['abandon']) après 3 erreurs de suite
    sans aucun succès (modèle sans vision, mauvais identifiant…) ou si le budget de temps est dépassé."""
    res = {"crops": [], "pages": {}, "duree_crop": 0.0, "erreurs": 0, "abandon": ""}
    debut = time.time()
    if hasattr(lecteur, "echauffer"):
        try:
            lecteur.echauffer()
        except Exception as e:                                       # noqa: BLE001
            res["abandon"] = f"chargement / premier appel impossible : {type(e).__name__}: {e}"
            return res
    res["conditions"] = dict(getattr(lecteur, "conditions", {}) or {})
    succes, suite = 0, 0
    for p in pages:
        with Image.open(p["chemin"]) as f_img:
            img = f_img.convert("RGB")
        for i, it in enumerate(p["items"] if niveau_crop else []):
            if i % pas:
                continue
            if budget_s and time.time() - debut > budget_s:
                res["abandon"] = f"budget de temps dépassé ({budget_s / 60:.0f} min) après {len(res['crops'])} zones"
                return res
            t0 = time.time()
            try:
                lu = lecteur.lire_crop(recadrer(img, it["bbox"]))
                succes, suite = succes + 1, 0
            except Exception as e:                                   # noqa: BLE001
                lu, res["erreurs"], suite = "", res["erreurs"] + 1, suite + 1
                if res["erreurs"] <= 2:
                    log(f"    ! {lecteur.nom} {p['id']}#{i + 1} : {type(e).__name__}: {e}")
                if suite >= 3 and succes == 0:
                    res["abandon"] = f"3 erreurs de suite sans aucun succès ({type(e).__name__}: {str(e)[:120]}) : modèle sans vision ou mal chargé ?"
                    return res
            dt = time.time() - t0
            res["duree_crop"] += dt
            meta = dict(getattr(lecteur, "derniere", {}) or {})
            res["crops"].append({"page": p["id"], "i": i + 1, "attendu": it["texte"], "lu": lu, "cer_leger": cer(it["texte"], lu),
                                 "cer_strict": cer(it["texte"], lu, "strict"),
                                 "exact": normaliser(it["texte"]).replace(" ", "") == normaliser(lu).replace(" ", ""),
                                 "vide": not normaliser(lu, "strict"), "s": round(dt, 2), "meta": meta})
        if niveau_page:
            try:
                lignes = lecteur.lire_page(img)
            except Exception as e:                                   # noqa: BLE001
                lignes = []
                log(f"    ! {lecteur.nom} page {p['id']} : {type(e).__name__}: {e}")
            res["pages"][p["id"]] = {**apparier_page([it["texte"] for it in p["items"]], lignes), "lignes_lues": lignes}
    return res


def moyenne(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def synthese(res: dict) -> dict:
    c = res["crops"]
    pg = list(res["pages"].values())
    tot_att = sum(v["attendus"] for v in pg)
    non_vides = [x for x in c if not x["vide"]]
    trouves = [(v["cer_trouves"], v["rappel"] * v["attendus"]) for v in pg if v["cer_trouves"] is not None]
    return {"cer_leger": moyenne(x["cer_leger"] for x in c), "cer_strict": moyenne(x["cer_strict"] for x in c),
            "vides": moyenne(1.0 if x["vide"] else 0.0 for x in c), "cer_hors_vides": moyenne(x["cer_leger"] for x in non_vides),
            "exact": moyenne(1.0 if x["exact"] else 0.0 for x in c), "s_par_crop": moyenne(x["s"] for x in c),
            "rappel_page": (sum(v["rappel"] * v["attendus"] for v in pg) / tot_att) if tot_att else None,
            "precision_page": moyenne(v["precision"] for v in pg) if pg else None,
            "cer_page": (sum(a * b for a, b in trouves) / sum(b for _, b in trouves)) if trouves and sum(b for _, b in trouves) else None,
            "erreurs": res["erreurs"], "n": len(c),
            "conditions": res.get("conditions", {}),
            "reflexion": moyenne(1.0 if (x.get("meta") or {}).get("raisonnement") else 0.0 for x in c) if c else float("nan"),
            "jetons_moy": moyenne((x.get("meta") or {}).get("tokens") for x in c if (x.get("meta") or {}).get("tokens")),
            "longueur": moyenne(1.0 if (x.get("meta") or {}).get("finish") == "length" else 0.0 for x in c) if c else float("nan"),
            "invalide": bool(len(non_vides) >= 8 and moyenne(x["cer_leger"] for x in non_vides) > 0.8 and len(non_vides) > 0.5 * len(c))}


def combinaisons(resultats: dict, seuil_accord: float = 0.2, top: int = 6, exclus: set | None = None) -> dict:
    """À partir des lectures par recadrage : accords deux à deux, vote majoritaire, oracle. Seuls les `top` meilleurs lecteurs entrent
    dans les paires (sinon n² lignes)."""
    avec, vus, doublons = {}, {}, {}
    for k, v in resultats.items():
        if not v["crops"] or k in (exclus or ()):
            continue
        signature = tuple(c["lu"] for c in v["crops"])
        if signature in vus:                                         # même modèle installé deux fois : il fausserait accords et votes
            doublons[k] = vus[signature]
            continue
        vus[signature] = k
        avec[k] = v
    out = {"accords": [], "oracle": None, "votes": [], "doublons": doublons, "exclus": sorted(exclus or ())}
    if not avec:
        return out
    classes = sorted(avec, key=lambda k: moyenne(c["cer_leger"] for c in avec[k]["crops"]))
    noms = classes[:top]
    n = min(len(avec[k]["crops"]) for k in noms)
    for ia, a in enumerate(noms):
        for b in noms[ia + 1:]:
            acc = [k for k in range(n) if cer(avec[a]["crops"][k]["lu"], avec[b]["crops"][k]["lu"]) <= seuil_accord and normaliser(avec[a]["crops"][k]["lu"])]
            des = [k for k in range(n) if k not in acc]
            out["accords"].append({"a": a, "b": b, "couverture": len(acc) / n if n else 0.0,
                                   "cer_si_accord": moyenne(avec[a]["crops"][k]["cer_leger"] for k in acc) if acc else None,
                                   "cer_si_desaccord": moyenne(avec[a]["crops"][k]["cer_leger"] for k in des) if (acc and des) else None})
    out["oracle"] = moyenne(min(avec[x]["crops"][k]["cer_leger"] for x in noms) for k in range(n))
    for taille in (3, 5):
        groupe = noms[:taille]
        if len(groupe) < 3 or taille > len(noms):
            continue
        choisis, sans = [], []
        for k in range(n):
            lectures = [(x, avec[x]["crops"][k]) for x in groupe if not avec[x]["crops"][k]["vide"]]
            meilleur = None
            for x, c in lectures:
                voisins = [y for y, d in lectures if cer(c["lu"], d["lu"]) <= seuil_accord]
                if len(voisins) >= 2 and (meilleur is None or len(voisins) > meilleur[0]):
                    meilleur = (len(voisins), c)
            if meilleur:
                choisis.append(meilleur[1]["cer_leger"])
            else:
                sans.append(avec[groupe[0]]["crops"][k]["cer_leger"])
        out["votes"].append({"lecteurs": groupe, "couverture": len(choisis) / n if n else 0.0,
                             "cer_si_majorite": moyenne(choisis) if choisis else None, "cer_sans_majorite": moyenne(sans) if sans else None})
    return out


def pct(x, d=1):
    return "—" if x is None or x != x else f"{100 * x:.{d}f} %"


def rapport(synth: dict, comb: dict, n_items: int, pages: list[dict], abandons: dict | None = None, pas: int = 1, notes: dict | None = None) -> str:
    L = [f"# Benchmark de lecture de l'arabe — {n_items} zones sur {len(pages)} pages" + (f" (échantillon : 1 zone sur {pas})" if pas > 1 else ""), "",
         "| Lecteur | CER léger | dont réponses vides | CER hors vides | Zones exactes | s / zone | Page : rappel | Page : précision | Page : CER des zones trouvées |",
         "|---|---|---|---|---|---|---|---|---|"]
    cle = lambda kv: (kv[1]["cer_leger"] != kv[1]["cer_leger"], kv[1]["cer_leger"], -(kv[1]["rappel_page"] or 0))  # noqa: E731
    for nom, s in sorted(synth.items(), key=cle):
        L.append(f"| `{nom}` | {pct(s['cer_leger'])} | {pct(s['vides'])} | {pct(s['cer_hors_vides'])} | {pct(s['exact'])} | "
                 f"{('—' if s['s_par_crop'] != s['s_par_crop'] else f'{s['s_par_crop']:.2f}')} | {pct(s['rappel_page'])} | {pct(s['precision_page'])} | {pct(s['cer_page'])} |"
                 + (f" ⚠ {s['erreurs']} erreur(s)" if s["erreurs"] else ""))
    L += ["", "*CER = erreurs de lettres / lettres attendues (plus bas = mieux). **Réponses vides** : le lecteur n'a rien rendu (compté comme 100 % d'erreur "
          "dans « CER léger ») ; « CER hors vides » mesure la qualité de lecture quand il répond. « Page » : le lecteur reçoit la page entière "
          "(repérage + lecture) ; rappel = part des zones attendues retrouvées (≤ 34 % d'erreur), précision = part des lignes rendues qui sont de vraies zones.*", ""]
    cond = {k: v["conditions"] for k, v in synth.items() if v.get("conditions")}
    if cond:
        L += ["## Conditions de mesure des modèles LM Studio", "",
              "| Modèle | API | Contexte chargé | Réflexion coupable | Quantification / taille | Réponses avec réflexion | Jetons par réponse | Arrêts « length » |", "|---|---|---|---|---|---|---|---|"]
        for k, c in cond.items():
            sy = synth[k]
            ctx = c.get("contexte")
            L.append(f"| `{k}` | {c.get('api', '?')} | {ctx if ctx else '? (non piloté)'} | {'oui' if c.get('reflexion_coupable') else 'non / inconnu'} | "
                     f"{(c.get('quant') or '—')} {('· ' + str(c['taille_go']) + ' Go') if c.get('taille_go') else ''} | {pct(sy.get('reflexion'), 0)} | "
                     f"{('—' if sy.get('jetons_moy') != sy.get('jetons_moy') else f'{sy['jetons_moy']:.0f}')} | {pct(sy.get('longueur'), 0)} |"
                     + (f" ⚠ {c['alerte']}" if c.get("alerte") else ""))
        L.append("")
    invalides = [k for k, v in synth.items() if v.get("invalide")]
    if invalides:
        L += ["## Résultats jugés invalides (exclus des accords et votes)", ""] + [
            f"- `{k}` : plus de 80 % d'erreur alors qu'il répond : il ne semble pas lire l'image (réponses sans rapport avec la zone). Ce n'est pas un verdict sur le modèle : "
            "problème de chargement / de projecteur de vision dans LM Studio probable." for k in invalides] + [""]
    if comb.get("doublons"):
        L += ["## Doublons ignorés", ""] + [f"- `{k}` donne exactement les mêmes lectures que `{v}` : même modèle installé deux fois, compté une seule fois." for k, v in comb["doublons"].items()] + [""]
    if notes:
        L += ["## Remarques sur les outils cités", ""] + [f"- {k} : {v}" for k, v in notes.items()] + [""]
    if abandons:
        L += ["## Candidats abandonnés ou ignorés", ""] + [f"- `{k}` : {v}" for k, v in abandons.items()] + [""]
    if comb.get("accords"):
        L += ["## Accord entre deux lecteurs (on garde la lecture s'ils s'accordent, sinon « à vérifier »)", "",
              "| Lecteur A + Lecteur B | Zones où ils s'accordent | CER de A si accord | CER de A si désaccord |", "|---|---|---|---|"]
        for c in sorted(comb["accords"], key=lambda c: (c["cer_si_accord"] is None, -(c["couverture"] if (c["cer_si_accord"] or 0) <= 0.03 else 0),
                                                        c["cer_si_accord"] or 9))[:10]:
            L.append(f"| `{c['a']}` + `{c['b']}` | {pct(c['couverture'])} | {pct(c['cer_si_accord'])} | {pct(c['cer_si_desaccord'])} |")
        L.append("")
    if comb.get("votes"):
        L += ["## Vote majoritaire (au moins 2 lecteurs d'accord parmi les meilleurs)", "",
              "| Lecteurs | Zones avec majorité | CER si majorité | CER sans majorité (meilleur lecteur seul) |", "|---|---|---|---|"]
        for v in comb["votes"]:
            L.append(f"| {len(v['lecteurs'])} meilleurs : " + ", ".join(f"`{x}`" for x in v["lecteurs"]) + f" | {pct(v['couverture'])} | {pct(v['cer_si_majorite'])} | {pct(v['cer_sans_majorite'])} |")
        L.append("")
    if comb.get("oracle") is not None:
        L += [f"**Oracle** (meilleur lecteur choisi zone par zone : plafond théorique d'un arbitre parfait) : CER {pct(comb['oracle'])}.", ""]
    ok = {k: v for k, v in synth.items() if v["cer_hors_vides"] == v["cer_hors_vides"] and not v.get("invalide")}
    if ok:
        mn = min(ok, key=lambda k: ok[k]["cer_hors_vides"])
        vides = {k: v for k, v in synth.items() if v["vides"] == v["vides"] and v["vides"] > 0.1}
        penseurs = {k: v for k, v in synth.items() if v.get("reflexion") == v.get("reflexion") and (v.get("reflexion") or 0) > 0.5}
        L += ["## Lecture des résultats", "",
              f"- Meilleure qualité de lecture quand le lecteur répond : `{mn}` (CER hors vides {pct(ok[mn]['cer_hors_vides'])}, vides {pct(ok[mn]['vides'])}).",
              *([f"- Lecteurs qui se taisent trop (> 10 % de réponses vides) : " + ", ".join(f"`{k}` ({pct(v['vides'])})" for k, v in vides.items())
                 + ". Un silence n'est pas une erreur de lecture : il se corrige (relance, autre prompt, autre lecteur)."] if vides else []),
              *([f"- Modèles qui réfléchissent à chaque réponse : " + ", ".join(f"`{k}` ({v['jetons_moy']:.0f} jetons, {v['s_par_crop']:.1f} s/zone)" for k, v in penseurs.items())
                 + ". La réflexion coûte du temps et peut épuiser le budget de jetons (réponse vide) : pour de la lecture, la couper dans LM Studio (réglage du modèle) "
                 "ou préférer une version sans réflexion."] if penseurs else []),
              "- Les captures de la vérité terrain sont peu définies : les erreurs absolues sont pessimistes, le classement relatif reste valable.",
              "- 64 zones = un échantillon ; un écart de quelques points entre deux lecteurs n'est pas significatif.", ""]
    return "\n".join(L)


# ----------------------------------------------------------------------------------------------- les modèles et outils CITÉS + installation
# Seuls ces outils / modèles sont testés avec --cites (liste de l'utilisateur) : « ids » = identifiants LM Studio acceptés (le premier trouvé gagne ;
# comparaison exacte puis « contient »), « telecharger » = termes essayés avec `lms get` s'il manque. « remplacant » = présent sur la machine, version voisine.
CATALOGUE_CITES = [
    {"nom": "Gemma 4 12B QAT (référence actuelle)", "ids": ["google/gemma-4-12b-qat"], "telecharger": ["google/gemma-4-12b-qat"]},
    {"nom": "Qwen3-VL 8B", "ids": ["qwen/qwen3-vl-8b"], "telecharger": ["qwen/qwen3-vl-8b"]},
    {"nom": "Qwen3-VL 2B", "ids": ["qwen3-vl-2b"], "telecharger": ["qwen/qwen3-vl-2b", "Qwen3-VL-2B-Instruct"]},
    {"nom": "Melaraby / qwen_vlm_arabic_ocr (GGUF)", "ids": ["qwen_vlm_arabic_ocr"], "telecharger": ["https://huggingface.co/Melaraby/qwen_vlm_arabic_ocr-GGUF", "qwen_vlm_arabic_ocr"]},
    {"nom": "InternVL (2.5 cité ; 3.5 installé)", "ids": ["internvl2_5", "internvl2.5", "internvl3_5-14b"], "telecharger": ["internvl2_5-8b", "internvl3_5-8b"]},
    {"nom": "olmOCR 2 (7B)", "ids": ["olmocr"], "telecharger": ["olmocr-2-7b", "allenai/olmOCR-2-7B-1025"]},
    {"nom": "LFM3-VL 2B", "ids": ["lfm3-vl-2b@f16", "lfm3-vl-2b"], "telecharger": ["lfm3-vl-2b"]},
    {"nom": "Qwen2-VL (7B)", "ids": ["qwen2-vl", "qwen/qwen2.5-vl-7b"], "telecharger": ["qwen/qwen2-vl-7b", "qwen2-vl-7b-instruct"]},
    {"nom": "Qari-OCR (arabe)", "ids": ["qari"], "telecharger": ["https://huggingface.co/cstr/qari-ocr-crispembed-GGUF", "Qari-OCR-0.2.2.1"]},   # 0.2.2.1 = version fine-tunée la plus précise ; quantification 8 bits conseillée
]
FICHIER_MODELES_SUPP = os.path.join(RACINE, "benchmark", "modeles_supplementaires.json")


def catalogue() -> list[dict]:
    """CATALOGUE_CITES + les modèles ajoutés à la main (benchmark/modeles_supplementaires.json : [{nom, ids, telecharger}])."""
    cat = list(CATALOGUE_CITES)
    try:
        with open(FICHIER_MODELES_SUPP, encoding="utf-8") as f:
            for e in json.load(f):
                if isinstance(e, dict) and e.get("ids") and e["nom"] not in {c["nom"] for c in cat}:
                    cat.append({"nom": e["nom"], "ids": list(e["ids"]), "telecharger": list(e.get("telecharger") or [])})
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return cat


def ajouter_modeles(ids: list[str], fichier: str | None = None) -> list[str]:
    """Ajoute des modèles LM Studio (identifiants exacts de --lister-lmstudio) à la liste testée par --cites."""
    fichier = fichier or FICHIER_MODELES_SUPP
    try:
        with open(fichier, encoding="utf-8") as f:
            actuel = json.load(f)
    except (OSError, ValueError):
        actuel = []
    deja = {tuple(e.get("ids", [])) for e in actuel if isinstance(e, dict)}
    for i in ids:
        if (i,) not in deja:
            actuel.append({"nom": i, "ids": [i], "telecharger": [i]})
    os.makedirs(os.path.dirname(fichier), exist_ok=True)
    with open(fichier, "w", encoding="utf-8") as f:
        json.dump(actuel, f, ensure_ascii=False, indent=1)
    return [e["nom"] for e in actuel]


LECTEURS_CITES = ["tess:ara:fast:psm7:x3:bin", "tess:ara:best:psm7:x3:bin", "easyocr", "paddleocr"]
PAQUETS_PIP = {"easyocr": ("easyocr", ["easyocr"]), "paddleocr": ("paddleocr", ["paddlepaddle", "paddleocr"]),
               "pytesseract": ("pytesseract", ["pytesseract"]), "cv2": ("cv2", ["opencv-python-headless"])}


def trouver_lms() -> str:
    exe = shutil.which("lms")
    if exe:
        return exe
    for nom in ("lms.exe", "lms"):
        p = os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", nom)
        if os.path.isfile(p):
            return p
    return ""


def serveur_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=5):
            return True
    except Exception:                                                # noqa: BLE001
        return False


def assurer_serveur(url: str, log=print, attente: float = 60.0) -> bool:
    if serveur_ok(url):
        return True
    exe = trouver_lms()
    if not exe:
        log("✘ LM Studio : serveur injoignable et commande `lms` introuvable. Ouvre LM Studio → onglet Developer → « Start Server » "
            "(ou, une fois, dans un terminal : `~/.lmstudio/bin/lms bootstrap`), puis relance.")
        return False
    log("LM Studio : serveur injoignable, démarrage (`lms server start`) …")
    try:
        _executer([exe, "server", "start"], timeout=120, capture_output=True)
    except Exception as e:                                           # noqa: BLE001
        log(f"  démarrage impossible : {e}")
    fin = time.time() + attente
    while time.time() < fin:
        if serveur_ok(url):
            return True
        time.sleep(2)
    log("✘ LM Studio : le serveur ne répond toujours pas.")
    return False


def resoudre_modele(entree: dict, installes: list[str]) -> str | None:
    for motif in entree["ids"]:
        m = motif.lower()
        exact = [i for i in installes if i.lower() == m]
        if exact:
            return exact[0]
        contient = sorted(i for i in installes if m in i.lower())
        if contient:
            return contient[0]
    return None


def telecharger_modele(entree: dict, ids_actuels, log=print) -> str | None:
    """`lms get <terme> --yes` pour chaque terme jusqu'à ce qu'un nouveau modèle apparaisse dans LM Studio ; rend son identifiant."""
    exe = trouver_lms()
    if not exe:
        log("  ✘ commande `lms` introuvable : téléchargement impossible (LM Studio → onglet Discover, ou `lms bootstrap`).")
        return None
    for terme in entree["telecharger"]:
        avant = set(ids_actuels())
        log(f"  ↓ lms get {terme} (peut durer longtemps) …")
        try:
            _executer([exe, "get", terme, "--yes"], timeout=4 * 3600)
        except Exception as e:                                       # noqa: BLE001
            log(f"    échec : {type(e).__name__}: {e}")
            continue
        nouveaux = [i for i in ids_actuels() if i not in avant]
        mots = [m.lower() for m in entree["ids"]] + [re.sub(r"[^a-z0-9]+", "", terme.lower().split("/")[-1])]
        lies = [i for i in nouveaux if resoudre_modele(entree, [i]) or any(m and m in re.sub(r"[^a-z0-9]+", "", i.lower()) for m in mots[-1:])]
        if lies:
            return lies[0]
        if nouveaux:                                                 # ex. un téléchargement ANTÉRIEUR qui se termine pendant celui-ci : ne pas l'attribuer à ce modèle
            log(f"    nouveau modèle sans rapport avec « {terme} » ({nouveaux}) : ignoré (téléchargement précédent qui se termine ?)")
        else:
            log(f"    « {terme} » : rien de nouveau dans LM Studio (introuvable, réseau coupé, ou déjà présent sous un autre nom)")
    return None


def installer_pip(paquets: list[str], log=print) -> bool:
    log(f"  pip install {' '.join(paquets)} …")
    try:
        r = _executer([sys.executable, "-m", "pip", "install", *paquets], timeout=3600)
        importlib.invalidate_caches()
        return getattr(r, "returncode", 0) == 0
    except Exception as e:                                           # noqa: BLE001
        log(f"    échec : {type(e).__name__}: {e}")
        return False


def tessdata_complet() -> bool:
    return all(os.path.isfile(os.path.join(TESSDATA_LOCAL, c, "ara.traineddata")) for c in DEPOTS_TESSDATA)


def preparer_cites(a, log=print, saisie=input) -> tuple[list[str], dict, dict]:
    """Prépare le benchmark des outils CITÉS : démarre LM Studio, repère ce qui est présent, annonce ce qui manque, l'installe après accord
    (--oui, ou réponse à la question ; sans terminal interactif et sans --oui : rien n'est installé). Rend (candidats, notes, abandons)."""
    notes, abandons = {}, {}
    candidats = []
    serveur = assurer_serveur(a.url, log)
    ids = (lambda: [m["id"] for m in modeles_lmstudio(a.url)]) if serveur else (lambda: [])
    installes = ids()
    manquants, modeles = [], {}
    for e in catalogue():
        mid = resoudre_modele(e, installes)
        modeles[e["nom"]] = mid
        if mid is None:
            manquants.append(e)
    pip_manque = [(m, p) for m, p in PAQUETS_PIP.values() if importlib.util.find_spec(m) is None]
    tess_manque = not tessdata_complet() and not a.tessdata
    plan = []
    if pip_manque:
        plan.append("Python (pip) : " + ", ".join(p for _, ps in pip_manque for p in ps))
    if tess_manque:
        plan.append("Tesseract : ara.traineddata (fast + best, ~15 Mo) dans benchmark/tessdata")
    if serveur and manquants:
        plan.append("LM Studio (téléchargements, plusieurs Go chacun) : " + ", ".join(e["nom"] for e in manquants))
    if not serveur:
        log("→ Sans serveur LM Studio, les modèles de vision sont ignorés.")
    if plan and not a.sans_installation:
        log("\nCe qui manque et sera installé :")
        for p in plan:
            log("  • " + p)
        ok = a.oui
        if not ok and sys.stdin and sys.stdin.isatty():
            ok = saisie("Installer maintenant ? [O/n] ").strip().lower() in ("", "o", "oui", "y", "yes")
        if not ok:
            log("→ Installation non faite (relance avec --oui, ou réponds O). Les outils manquants seront ignorés.")
        else:
            for m, ps in pip_manque:
                if not installer_pip(ps, log):
                    abandons[ps[-1]] = f"installation pip échouée ({' '.join(ps)}) : installe-le à la main"
            if tess_manque:
                try:
                    telecharger_tessdata()
                except Exception as e:                               # noqa: BLE001
                    abandons["tessdata"] = f"téléchargement impossible : {e}"
            if serveur:
                for e in manquants:
                    nouveau = telecharger_modele(e, ids, log)
                    if nouveau:
                        modeles[e["nom"]] = nouveau
                        notes[e["nom"]] = "téléchargé"
                    else:
                        abandons[e["nom"]] = ("introuvable / non téléchargeable automatiquement : installe-le dans LM Studio (onglet Discover, "
                                              "format GGUF), puis relance avec --reprendre")
    candidats += LECTEURS_CITES
    for e in catalogue():
        mid = modeles.get(e["nom"])
        if mid:
            candidats.append("lm:" + mid)
            if not any(m.lower() in mid.lower() for m in e["ids"][:1]):
                notes[e["nom"]] = f"remplacé par « {mid} » (version voisine déjà installée)" if e["nom"] not in notes else notes[e["nom"]]
        elif serveur and e["nom"] not in abandons:
            abandons[e["nom"]] = "absent de LM Studio (installation refusée ou non faite)"
    return candidats, notes, abandons


def vue(resultats: dict, synth: dict, selection: set | None) -> tuple[dict, dict]:
    """Ne montre dans le rapport que la sélection de cette exécution (le JSON garde tout)."""
    if selection is None:
        return resultats, synth
    return ({k: v for k, v in resultats.items() if k in selection}, {k: v for k, v in synth.items() if k in selection})


def ecrire_sorties(sortie: str, synth: dict, comb: dict, resultats: dict, abandons: dict, n_items: int, pages: list[dict], pas: int, partiels: dict,
                   notes: dict | None = None, selection: set | None = None, seuil: float = 0.2, sans_crop: bool = False) -> str:
    res_v, synth_v = vue(resultats, synth, selection)
    invalides = {k for k, v in synth_v.items() if v.get("invalide")}
    comb_v = combinaisons(res_v, seuil, exclus=invalides) if len(res_v) > 1 and not sans_crop else {"accords": [], "oracle": None, "votes": []}
    texte = rapport(synth_v, comb_v, n_items, pages, abandons, pas, notes)
    with open(os.path.join(sortie, "rapport_benchmark.md"), "w", encoding="utf-8") as f:
        f.write(texte)
    with open(os.path.join(sortie, "resultats_benchmark.json"), "w", encoding="utf-8") as f:
        json.dump({"pas": pas, "synthese": synth, "combinaisons": comb_v, "abandons": abandons, "detail": resultats, "partiels": partiels, "notes": notes or {}},
                  f, ensure_ascii=False, indent=1)
    return texte


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark de lecture de l'arabe", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--cites", action="store_true", help="UNIQUEMENT les outils et modèles cités (Tesseract, EasyOCR, PaddleOCR, Gemma 4 12B QAT, Qwen3-VL 8B/2B, "
                                                       "Melaraby, InternVL, olmOCR 2, LFM3-VL 2B, Qwen2-VL, Qari-OCR) ; démarre LM Studio et installe ce qui manque")
    ap.add_argument("--oui", action="store_true", help="installe sans poser de question (pip, tessdata, modèles LM Studio)")
    ap.add_argument("--sans-installation", action="store_true", help="n'installe rien (les outils manquants sont ignorés)")
    ap.add_argument("--preparer-seulement", action="store_true", help="installe / vérifie les outils cités puis s'arrête (pas de mesure)")
    ap.add_argument("--candidats", nargs="*", default=[], help="tess[:ara[:best][:psm7][:x3][:bin]] | grille:tess | easyocr | paddleocr | lm:<modèle>[:indice[=tess|easyocr]]")
    ap.add_argument("--complet", action="store_true", help="TOUT ce qui est sur la machine : Tesseract, EasyOCR, PaddleOCR et TOUS les modèles de vision de LM Studio (long)")
    ap.add_argument("--vlm", nargs="*", default=None, help="ajoute les modèles de vision de LM Studio dont l'identifiant contient un de ces mots (sans mot : tous)")
    ap.add_argument("--exclure", nargs="*", default=[], help="retire les modèles dont l'identifiant contient un de ces mots (ex. 31b)")
    ap.add_argument("--tout-afficher", action="store_true", help="le rapport montre aussi les mesures conservées d'exécutions précédentes (sinon : seulement la sélection)")
    ap.add_argument("--verite", default=VERITE_DEFAUT)
    ap.add_argument("--sortie", default=os.path.join(RACINE, "benchmark", "sortie"))
    ap.add_argument("--reprendre", action="store_true", help="ne refait pas les candidats déjà présents dans le dossier de sortie (mêmes options requises)")
    ap.add_argument("--echantillon", type=int, default=1, metavar="K", help="une zone sur K (identique pour tous les candidats) : K=2 ≈ 2x plus rapide")
    ap.add_argument("--budget-min", type=float, default=30.0, help="minutes max par candidat pour le niveau recadrage (0 = illimité)")
    ap.add_argument("--sans-page", action="store_true", help="ne mesure pas le niveau « page entière »")
    ap.add_argument("--sans-crop", action="store_true", help="ne mesure que le niveau « page entière » (très rapide : 5 appels par modèle)")
    ap.add_argument("--decharger", action="store_true", help="décharge les modèles de LM Studio (commande `lms unload --all`) avant chaque modèle de vision")
    ap.add_argument("--tessdata", nargs="*", default=[], help="clé=dossier contenant ara.traineddata (sinon : dossiers de --telecharger-tessdata)")
    ap.add_argument("--telecharger-tessdata", action="store_true", help="télécharge ara.traineddata fast + best dans benchmark/tessdata puis continue")
    ap.add_argument("--tesseract-cmd", default="")
    ap.add_argument("--indice-tess", default="tess:ara:fast:psm7:x3:bin", help="lecteur Tesseract qui fournit l'indice aux candidats lm:…:indice")
    ap.add_argument("--url", default="http://127.0.0.1:1234/v1")
    ap.add_argument("--hauteur-vlm", type=int, default=128, help="hauteur minimale (px) à laquelle les recadrages sont agrandis pour les modèles de vision")
    ap.add_argument("--max-tokens", type=int, default=1024, help="jetons max par réponse de recadrage (une réponse vide est relancée avec x4)")
    ap.add_argument("--relances", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--seuil-accord", type=float, default=0.2)
    ap.add_argument("--lister-lmstudio", action="store_true")
    ap.add_argument("--ajouter", nargs="+", metavar="ID", help="ajoute des modèles LM Studio (identifiants de --lister-lmstudio) à la liste de --cites (fichier benchmark/modeles_supplementaires.json)")
    ap.add_argument("--contexte", type=int, default=16384, help="contexte (jetons) avec lequel chaque modèle est chargé (`lms load --context-length`), comme le moteur (défaut 16384)")
    ap.add_argument("--sans-chargement", action="store_true", help="ne charge / décharge rien : utilise le modèle tel qu'il est déjà chargé dans LM Studio")
    ap.add_argument("--api", choices=["auto", "openai"], default="auto", help="auto = API native de LM Studio (réflexion coupée, contexte connu) si disponible ; openai = API compatible OpenAI (réflexion non pilotée)")
    ap.add_argument("--budget-reflexion", type=int, default=2000, help="jetons de réflexion tolérés avant abandon de la requête (mode natif)")
    a = ap.parse_args(argv)
    if a.ajouter:
        noms = ajouter_modeles(a.ajouter)
        print(f"✔ {len(a.ajouter)} modèle(s) ajouté(s) à {FICHIER_MODELES_SUPP} ; modèles supplémentaires : {noms}")
        if not (a.cites or a.candidats or a.complet or a.vlm is not None or a.preparer_seulement):
            return 0
    if a.lister_lmstudio:
        if not assurer_serveur(a.url):
            return 1
        try:
            for m in modeles_lmstudio(a.url):
                print(f"{m['id']:50s} {m['type']:11s} {m['etat']}")
            return 0
        except Exception as e:                                       # noqa: BLE001
            print(f"LM Studio injoignable sur {a.url} : {e}")
            return 1
    if a.telecharger_tessdata:
        try:
            for cle, dossier in telecharger_tessdata().items():
                print(f"✔ tessdata {cle} : {dossier}")
        except Exception as e:                                       # noqa: BLE001
            print(f"✘ téléchargement impossible : {e}")
            return 1
    candidats = list(a.candidats)
    notes, abandons = {}, {}
    if a.cites or a.preparer_seulement:
        cites, notes, abandons = preparer_cites(a)
        if a.preparer_seulement:
            for k, v in {**{k: f"{v}" for k, v in notes.items()}, **abandons}.items():
                print(f"  - {k} : {v}")
            print("Préparation terminée." + (" Des outils restent manquants (voir ci-dessus)." if abandons else ""))
            return 0
        candidats += cites
    if a.complet:
        candidats += ["tess:ara:fast:psm7:x3:bin", "tess:ara:best:psm7:x3:bin", "easyocr", "paddleocr"]
    if a.complet or a.vlm is not None:
        if assurer_serveur(a.url):
            try:
                candidats += ["lm:" + i for i in choisir_vlm(a.url, a.vlm or [], a.exclure)]
            except Exception as e:                                   # noqa: BLE001
                print(f"✘ modèles de vision de LM Studio : {e}")
    candidats = [c for c in dict.fromkeys(candidats) if not any(x.lower() in c.lower() for x in a.exclure if c.startswith("lm:"))]
    if not candidats:
        ap.error("indique des candidats (--cites, --candidats …, --vlm …, --complet) ; --lister-lmstudio pour voir les modèles")
    if any(c.startswith("lm:") for c in candidats) and not serveur_ok(a.url):
        assurer_serveur(a.url)
    pages = charger_verite(a.verite)
    n_items = sum(len(p["items"]) for p in pages)
    os.makedirs(a.sortie, exist_ok=True)
    resultats, synth, partiels = {}, {}, {}
    chemin_json = os.path.join(a.sortie, "resultats_benchmark.json")
    if a.reprendre and os.path.isfile(chemin_json):
        with open(chemin_json, encoding="utf-8") as f:
            ancien = json.load(f)
        if ancien.get("pas") == a.echantillon:
            resultats, synth, partiels = ancien["detail"], ancien["synthese"], ancien.get("partiels", {})
            print(f"↻ reprise : {len(resultats)} candidat(s) déjà mesuré(s) conservé(s)")
        else:
            print("↻ reprise ignorée : --echantillon différent de l'exécution précédente")
    selection: set | None = None if a.tout_afficher else set()
    pas = max(1, a.echantillon)

    def sauver():
        return ecrire_sorties(a.sortie, synth, {}, resultats, abandons, n_items, pages, pas, partiels, notes, selection, a.seuil_accord, a.sans_crop)
    decharge_signale = False
    for spec in candidats:
        try:
            lecteurs = construire(spec, a)
        except ValueError as e:
            print(f"✘ {e}")
            abandons[spec] = str(e)
            continue
        for lec in lecteurs:
            if selection is not None:
                selection.add(lec.nom)
            if lec.nom in resultats:
                print(f"↷ {lec.nom} déjà mesuré (reprise)")
                continue
            ok, pourquoi = lec.disponible()
            if not ok:
                print(f"✘ {lec.nom} ignoré : {pourquoi}")
                abandons[lec.nom] = pourquoi
                continue
            if a.decharger and isinstance(lec, LecteurLMStudio):
                if not decharger_lmstudio() and not decharge_signale:
                    print("  (commande `lms` introuvable : déchargement non fait ; LM Studio gère lui-même la mémoire)")
                    decharge_signale = True
            print(f"▶ {lec.nom} …", flush=True)
            res = evaluer(lec, pages, not a.sans_page, pas=pas, budget_s=(a.budget_min * 60 or None), niveau_crop=not a.sans_crop)
            if res["abandon"]:
                print(f"   ✘ abandonné : {res['abandon']}")
                abandons[lec.nom] = res["abandon"]
                partiels[lec.nom] = res
            else:
                resultats[lec.nom], synth[lec.nom] = res, synthese(res)
                s = synth[lec.nom]
                print(f"   CER léger {pct(s['cer_leger'])} (vides {pct(s['vides'])}, hors vides {pct(s['cer_hors_vides'])}) · exact {pct(s['exact'])} · "
                      f"page rappel {pct(s['rappel_page'])}")
            sauver()                                                  # sauvegarde à chaque candidat
    if selection is not None and not (selection & set(resultats)):
        print("Aucun candidat n'a pu tourner.")
        sauver()
        return 2
    if not resultats:
        print("Aucun candidat n'a pu tourner.")
        return 2
    texte = sauver()
    print("\n" + texte)
    print(f"Rapport : {os.path.join(a.sortie, 'rapport_benchmark.md')}  ·  détail par zone : resultats_benchmark.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
