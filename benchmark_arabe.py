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
import io
import json
import os
import re
import shutil
import subprocess
import sys
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

    def _ocr(self, img) -> list[str]:
        import numpy as np
        import paddleocr
        if self._o is None:
            self._o = paddleocr.PaddleOCR(lang="ar")
        a = np.array(img.convert("RGB"))
        if hasattr(self._o, "predict"):                              # PaddleOCR 3.x
            res = self._o.predict(a)
            return [t for r in res for t in (r.get("rec_texts", []) if hasattr(r, "get") else [])]
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
                 max_tokens: int = 1024, relances: int = 1):
        self.nom = spec
        m = re.fullmatch(r"lm:(.*?):indice(?:=(\w+))?", spec)
        self.est_indice = bool(m)
        self.modele = m.group(1) if m else spec[3:]
        self.source_indice = (m.group(2) or "tess") if m else ""
        self.url, self.hint, self.hauteur, self.timeout = url.rstrip("/"), indice, hauteur, timeout
        self.max_tokens, self.relances = max_tokens, relances
        self.derniere: dict = {}

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
        return True, ""

    def _appel(self, img: Image.Image, prompt: str, max_tokens: int, timeout: float | None = None) -> tuple[str, dict]:
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
        """Premier appel hors mesure : LM Studio charge le modèle à la demande (peut prendre des minutes)."""
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
        return [LecteurLMStudio(spec, a.url, hint, a.hauteur_vlm, a.timeout, a.max_tokens, a.relances)]
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
            "erreurs": res["erreurs"], "n": len(c)}


def combinaisons(resultats: dict, seuil_accord: float = 0.2, top: int = 6) -> dict:
    """À partir des lectures par recadrage : accords deux à deux, vote majoritaire, oracle. Seuls les `top` meilleurs lecteurs entrent
    dans les paires (sinon n² lignes)."""
    avec = {k: v for k, v in resultats.items() if v["crops"]}
    out = {"accords": [], "oracle": None, "votes": []}
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


def rapport(synth: dict, comb: dict, n_items: int, pages: list[dict], abandons: dict | None = None, pas: int = 1) -> str:
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
    ok = {k: v for k, v in synth.items() if v["cer_hors_vides"] == v["cer_hors_vides"]}
    if ok:
        mn = min(ok, key=lambda k: ok[k]["cer_hors_vides"])
        vides = {k: v for k, v in synth.items() if v["vides"] == v["vides"] and v["vides"] > 0.1}
        L += ["## Lecture des résultats", "",
              f"- Meilleure qualité de lecture quand le lecteur répond : `{mn}` (CER hors vides {pct(ok[mn]['cer_hors_vides'])}, vides {pct(ok[mn]['vides'])}).",
              *([f"- Lecteurs qui se taisent trop (> 10 % de réponses vides) : " + ", ".join(f"`{k}` ({pct(v['vides'])})" for k, v in vides.items())
                 + ". Un silence n'est pas une erreur de lecture : il se corrige (relance, autre prompt, autre lecteur)."] if vides else []),
              "- Les captures de la vérité terrain sont peu définies : les erreurs absolues sont pessimistes, le classement relatif reste valable.",
              "- 64 zones = un échantillon ; un écart de quelques points entre deux lecteurs n'est pas significatif.", ""]
    return "\n".join(L)


def ecrire_sorties(sortie: str, synth: dict, comb: dict, resultats: dict, abandons: dict, n_items: int, pages: list[dict], pas: int, partiels: dict) -> str:
    texte = rapport(synth, comb, n_items, pages, abandons, pas)
    with open(os.path.join(sortie, "rapport_benchmark.md"), "w", encoding="utf-8") as f:
        f.write(texte)
    with open(os.path.join(sortie, "resultats_benchmark.json"), "w", encoding="utf-8") as f:
        json.dump({"pas": pas, "synthese": synth, "combinaisons": comb, "abandons": abandons, "detail": resultats, "partiels": partiels},
                  f, ensure_ascii=False, indent=1)
    return texte


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark de lecture de l'arabe", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--candidats", nargs="*", default=[], help="tess[:ara[:best][:psm7][:x3][:bin]] | grille:tess | easyocr | paddleocr | lm:<modèle>[:indice[=tess|easyocr]]")
    ap.add_argument("--complet", action="store_true", help="Tesseract (fast, best), EasyOCR, PaddleOCR et TOUS les modèles de vision de LM Studio")
    ap.add_argument("--vlm", nargs="*", default=None, help="ajoute les modèles de vision de LM Studio dont l'identifiant contient un de ces mots (sans mot : tous)")
    ap.add_argument("--exclure", nargs="*", default=[], help="retire les modèles dont l'identifiant contient un de ces mots (ex. 31b)")
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
    a = ap.parse_args(argv)
    if a.lister_lmstudio:
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
    if a.complet:
        candidats += ["tess:ara:fast:psm7:x3:bin", "tess:ara:best:psm7:x3:bin", "easyocr", "paddleocr"]
    if a.complet or a.vlm is not None:
        try:
            candidats += ["lm:" + i for i in choisir_vlm(a.url, a.vlm or [], a.exclure)]
        except Exception as e:                                       # noqa: BLE001
            print(f"✘ modèles de vision de LM Studio : {e}")
    candidats = list(dict.fromkeys(candidats))
    if not candidats:
        ap.error("indique des candidats (--candidats …, --vlm …, --complet) ; --lister-lmstudio pour voir les modèles")
    pages = charger_verite(a.verite)
    n_items = sum(len(p["items"]) for p in pages)
    os.makedirs(a.sortie, exist_ok=True)
    resultats, synth, abandons, partiels = {}, {}, {}, {}
    chemin_json = os.path.join(a.sortie, "resultats_benchmark.json")
    if a.reprendre and os.path.isfile(chemin_json):
        with open(chemin_json, encoding="utf-8") as f:
            ancien = json.load(f)
        if ancien.get("pas") == a.echantillon:
            resultats, synth, abandons, partiels = ancien["detail"], ancien["synthese"], ancien.get("abandons", {}), ancien.get("partiels", {})
            print(f"↻ reprise : {len(resultats)} candidat(s) déjà mesuré(s) conservé(s)")
        else:
            print("↻ reprise ignorée : --echantillon différent de l'exécution précédente")
    decharge_signale = False
    for spec in candidats:
        try:
            lecteurs = construire(spec, a)
        except ValueError as e:
            print(f"✘ {e}")
            abandons[spec] = str(e)
            continue
        for lec in lecteurs:
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
            res = evaluer(lec, pages, not a.sans_page, pas=max(1, a.echantillon), budget_s=(a.budget_min * 60 or None), niveau_crop=not a.sans_crop)
            if res["abandon"]:
                print(f"   ✘ abandonné : {res['abandon']}")
                abandons[lec.nom] = res["abandon"]
                partiels[lec.nom] = res
            else:
                resultats[lec.nom], synth[lec.nom] = res, synthese(res)
                s = synth[lec.nom]
                print(f"   CER léger {pct(s['cer_leger'])} (vides {pct(s['vides'])}, hors vides {pct(s['cer_hors_vides'])}) · exact {pct(s['exact'])} · "
                      f"page rappel {pct(s['rappel_page'])}")
            comb = combinaisons(resultats, a.seuil_accord) if len(resultats) > 1 and not a.sans_crop else {"accords": [], "oracle": None, "votes": []}
            ecrire_sorties(a.sortie, synth, comb, resultats, abandons, n_items, pages, max(1, a.echantillon), partiels)     # sauvegarde à chaque candidat
    if not resultats:
        print("Aucun candidat n'a pu tourner.")
        return 2
    comb = combinaisons(resultats, a.seuil_accord) if len(resultats) > 1 and not a.sans_crop else {"accords": [], "oracle": None, "votes": []}
    texte = ecrire_sorties(a.sortie, synth, comb, resultats, abandons, n_items, pages, max(1, a.echantillon), partiels)
    print("\n" + texte)
    print(f"Rapport : {os.path.join(a.sortie, 'rapport_benchmark.md')}  ·  détail par zone : resultats_benchmark.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
