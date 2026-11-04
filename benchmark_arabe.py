#!/usr/bin/env python3
"""Benchmark de la LECTURE DE L'ARABE : Tesseract (variantes), EasyOCR, PaddleOCR, modèles de vision de LM Studio, et leurs combinaisons.

Deux niveaux de mesure, sur une vérité terrain (benchmark/verite_arabe.json : pages + zones arabes recadrables + texte attendu) :
  A. « recadrage »  : on donne au lecteur UNE zone arabe isolée. Mesure sa capacité de lecture seule (zone supposée trouvée).
  B. « page »       : on donne la page entière et on lui demande tout l'arabe. Mesure zone + lecture ensemble (ce que fait le moteur
                      quand le modèle lit la page entière). L'écart A/B dit si un pipeline « je trouve la zone, puis je la lis » vaut le coup.
Combinaisons évaluées à partir des lectures du niveau A : accord entre deux lecteurs (précision quand ils s'accordent, part de zones
couvertes), « oracle » (meilleur lecteur par zone = plafond d'un arbitre parfait), correction d'un VLM guidé par la lecture Tesseract (`:indice`).

Exemples (Windows : python benchmark_arabe.py ...)
  python benchmark_arabe.py --lister-lmstudio
  python benchmark_arabe.py --candidats grille:tess --tessdata fast=C:\\tess\\fast best=C:\\tess\\best
  python benchmark_arabe.py --candidats tess:ara:best:psm7:x3 lm:google/gemma-4-12b-qat lm:qwen/qwen3-vl-8b lm:google/gemma-4-12b-qat:indice easyocr
Un candidat qui ne peut pas tourner (modèle absent, paquet non installé) est ignoré avec un message, jamais fatal.
Normalisation : on ne garde que les lettres arabes ; voyelles et tatwil retirés ; « léger » unifie en plus alef (أإآٱ), ya (ى), hamza sur support.
CER = distance d'édition / longueur attendue, espaces ignorés (une lecture « عبدالاحد » n'est pas pénalisée pour l'espace)."""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
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
PROMPT_CROP = ("Recopie exactement le texte arabe écrit sur cette image. Réponds uniquement par le texte arabe, sans traduction, "
               "sans translittération, sans commentaire. Si tu hésites sur une lettre, donne ta meilleure lecture.")
PROMPT_INDICE = ("Recopie exactement le texte arabe écrit sur cette image. Un OCR classique l'a lu « {indice} » (cette lecture peut contenir "
                 "des erreurs : corrige-la d'après l'image). Réponds uniquement par le texte arabe, sans traduction, sans commentaire.")
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
class Lecteur:
    nom = "?"

    def disponible(self) -> tuple[bool, str]:
        return True, ""

    def lire_crop(self, img: Image.Image) -> str:
        raise NotImplementedError

    def lire_page(self, img: Image.Image) -> list[str]:
        return arabe_des_lignes(self.lire_crop(img))


class LecteurTesseract(Lecteur):
    """tess[:ara|ara+eng][:fast|best|<clé de --tessdata>][:psmN][:x2|x3][:bin]  (ex. tess:ara:best:psm7:x3:bin)"""

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
                raise ValueError(f"option Tesseract inconnue « {p} » dans {spec} (clés --tessdata connues : {sorted(tessdata)})")
        self.cmd = cmd

    def disponible(self):
        try:
            import pytesseract
            if self.cmd:
                pytesseract.pytesseract.tesseract_cmd = self.cmd
            langues = pytesseract.get_languages(config=self._cfg(0).replace("--psm 0", ""))
        except Exception as e:                                       # noqa: BLE001
            return False, f"Tesseract / pytesseract indisponible : {e}"
        manque = [l for l in self.lang.split("+") if l not in langues]
        return (not manque), (f"langue(s) Tesseract absente(s) : {manque}" + (f" dans {self.dossier}" if self.dossier else "")) if manque else ""

    def _cfg(self, psm: int) -> str:
        c = f"--psm {psm} --oem 1"
        if self.dossier:
            c += f' --tessdata-dir "{self.dossier}"'
        return c

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
        return pytesseract.image_to_string(self._prep(img, False), lang=self.lang, config=self._cfg(self.psm)).strip()

    def lire_page(self, img):
        import pytesseract
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
    """lm:<id du modèle>[:indice]. `:indice` = le modèle reçoit aussi la lecture Tesseract et la corrige (OCR + VLM)."""

    def __init__(self, spec: str, url: str, tess_indice: Lecteur | None, hauteur: int = 128, timeout: float = 180.0):
        self.nom = spec
        reste = spec[3:]
        self.indice = reste.endswith(":indice")
        self.modele = reste[: -len(":indice")] if self.indice else reste
        self.url, self.tess, self.hauteur, self.timeout = url.rstrip("/"), tess_indice, hauteur, timeout

    def disponible(self):
        try:
            with urllib.request.urlopen(self.url + "/models", timeout=10) as r:
                ids = [m["id"] for m in json.load(r).get("data", [])]
        except Exception as e:                                       # noqa: BLE001
            return False, f"LM Studio injoignable sur {self.url} ({e}) : serveur local démarré ?"
        if self.modele not in ids:
            return False, f"modèle « {self.modele} » introuvable dans LM Studio (disponibles : {ids}) ; charge-le puis `--lister-lmstudio`"
        if self.indice and (self.tess is None or not self.tess.disponible()[0]):
            return False, "le mode :indice exige un lecteur Tesseract arabe disponible (--indice-tess, --tessdata)"
        return True, ""

    def _appel(self, img: Image.Image, prompt: str, max_tokens: int) -> str:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        corps = {"model": self.modele, "temperature": 0, "max_tokens": max_tokens,
                 "messages": [{"role": "user", "content": [
                     {"type": "text", "text": prompt},
                     {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}}]}]}
        rq = urllib.request.Request(self.url + "/chat/completions", json.dumps(corps).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(rq, timeout=self.timeout) as r:
            msg = json.load(r)["choices"][0]["message"]["content"]
        if isinstance(msg, list):
            msg = " ".join(p.get("text", "") for p in msg if isinstance(p, dict))
        return re.sub(r"<think>.*?</think>", "", msg or "", flags=re.S).strip()

    def _agrandir(self, img: Image.Image) -> Image.Image:
        if img.height >= self.hauteur:
            return img
        f = min(6.0, self.hauteur / max(1, img.height))
        return img.resize((int(img.width * f), int(img.height * f)), Image.LANCZOS)

    def lire_crop(self, img):
        prompt = PROMPT_CROP
        if self.indice:
            prompt = PROMPT_INDICE.format(indice=normaliser(self.tess.lire_crop(img), "strict") or "illisible")
        return self._appel(self._agrandir(img), prompt, 300)

    def lire_page(self, img):
        return arabe_des_lignes(self._appel(img, PROMPT_PAGE, 2000))


def construire(spec: str, a: argparse.Namespace) -> list[Lecteur]:
    tessdata = dict(kv.split("=", 1) for kv in (a.tessdata or []))
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
        indice = LecteurTesseract(a.indice_tess, tessdata, a.tesseract_cmd) if spec.endswith(":indice") else None
        return [LecteurLMStudio(spec, a.url, indice, a.hauteur_vlm)]
    raise ValueError(f"candidat inconnu « {spec} » (tess…, grille:tess, easyocr, paddleocr, lm:<modèle>[:indice])")


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


def evaluer(lecteur: Lecteur, pages: list[dict], niveau_page: bool, log=print) -> dict:
    res = {"crops": [], "pages": {}, "duree_crop": 0.0, "erreurs": 0}
    for p in pages:
        with Image.open(p["chemin"]) as f_img:
            img = f_img.convert("RGB")
        for i, it in enumerate(p["items"]):
            t0 = time.time()
            try:
                lu = lecteur.lire_crop(recadrer(img, it["bbox"]))
            except Exception as e:                                   # noqa: BLE001
                lu, res["erreurs"] = "", res["erreurs"] + 1
                if res["erreurs"] <= 2:
                    log(f"    ! {lecteur.nom} {p['id']}#{i + 1} : {type(e).__name__}: {e}")
            dt = time.time() - t0
            res["duree_crop"] += dt
            res["crops"].append({"page": p["id"], "i": i + 1, "attendu": it["texte"], "lu": lu, "cer_leger": cer(it["texte"], lu),
                                 "cer_strict": cer(it["texte"], lu, "strict"), "exact": normaliser(it["texte"]).replace(" ", "") == normaliser(lu).replace(" ", ""),
                                 "s": round(dt, 2)})
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
    pg = res["pages"].values()
    tot_att = sum(v["attendus"] for v in pg)
    return {"cer_leger": moyenne(x["cer_leger"] for x in c), "cer_strict": moyenne(x["cer_strict"] for x in c),
            "exact": moyenne(1.0 if x["exact"] else 0.0 for x in c), "s_par_crop": moyenne(x["s"] for x in c),
            "rappel_page": (sum(v["rappel"] * v["attendus"] for v in pg) / tot_att) if tot_att else None,
            "precision_page": moyenne(v["precision"] for v in pg) if res["pages"] else None, "erreurs": res["erreurs"]}


def combinaisons(resultats: dict, seuil_accord: float = 0.2) -> dict:
    """À partir des lectures par recadrage : accords deux à deux, oracle, vote majoritaire à trois et plus."""
    noms = list(resultats)
    n = len(resultats[noms[0]]["crops"]) if noms else 0
    out = {"accords": [], "oracle": None}
    for ia, a in enumerate(noms):
        for b in noms[ia + 1:]:
            acc = [k for k in range(n) if cer(resultats[a]["crops"][k]["lu"], resultats[b]["crops"][k]["lu"]) <= seuil_accord
                   and normaliser(resultats[a]["crops"][k]["lu"])]
            if not acc:
                out["accords"].append({"a": a, "b": b, "couverture": 0.0, "cer_si_accord": None, "cer_si_desaccord": None})
                continue
            dés = [k for k in range(n) if k not in acc]
            best = lambda k: min((resultats[a]["crops"][k], resultats[b]["crops"][k]), key=lambda x: x["cer_leger"])  # noqa: E731
            out["accords"].append({"a": a, "b": b, "couverture": len(acc) / n,
                                   "cer_si_accord": moyenne(resultats[a]["crops"][k]["cer_leger"] for k in acc),
                                   "cer_si_desaccord": moyenne(resultats[a]["crops"][k]["cer_leger"] for k in dés) if dés else None})
    if noms:
        out["oracle"] = moyenne(min(resultats[x]["crops"][k]["cer_leger"] for x in noms) for k in range(n))
    return out


def pct(x, d=1):
    return "—" if x is None or x != x else f"{100 * x:.{d}f} %"


def rapport(synth: dict, comb: dict, n_items: int, pages: list[dict]) -> str:
    L = [f"# Benchmark de lecture de l'arabe — {n_items} zones sur {len(pages)} pages", "",
         "| Lecteur | CER léger | CER strict | Zones exactes | s / zone | Page : rappel | Page : précision |", "|---|---|---|---|---|---|---|"]
    for nom, s in sorted(synth.items(), key=lambda kv: (kv[1]["cer_leger"] != kv[1]["cer_leger"], kv[1]["cer_leger"])):
        L.append(f"| `{nom}` | {pct(s['cer_leger'])} | {pct(s['cer_strict'])} | {pct(s['exact'])} | {s['s_par_crop']:.2f} | {pct(s['rappel_page'])} | {pct(s['precision_page'])} |"
                 + (f" ⚠ {s['erreurs']} erreur(s)" if s["erreurs"] else ""))
    L += ["", "*CER = erreurs de lettres / lettres attendues (plus bas = mieux). « Page » : le lecteur reçoit la page entière (zone + lecture) ; "
          "rappel = part des zones attendues retrouvées (≤ 34 % d'erreur), précision = part des lignes rendues qui sont de vraies zones.*", ""]
    if comb["accords"]:
        L += ["## Combinaisons (deux lecteurs : on garde la lecture s'ils s'accordent, sinon on signale « à vérifier »)", "",
              "| Lecteur A + Lecteur B | Zones où ils s'accordent | CER de A quand ils s'accordent | CER de A en cas de désaccord |", "|---|---|---|---|"]
        for c in sorted(comb["accords"], key=lambda c: (c["cer_si_accord"] is None, c["cer_si_accord"] or 9)):
            L.append(f"| `{c['a']}` + `{c['b']}` | {pct(c['couverture'])} | {pct(c['cer_si_accord'])} | {pct(c['cer_si_desaccord'])} |")
        L += ["", f"**Oracle** (meilleur lecteur choisi zone par zone, plafond théorique d'un arbitre parfait) : CER {pct(comb['oracle'])}.", ""]
    ok = {k: v for k, v in synth.items() if v["cer_leger"] == v["cer_leger"]}
    if ok:
        mn = min(ok, key=lambda k: ok[k]["cer_leger"])
        L += ["## Lecture des résultats", "",
              f"- Meilleur lecteur seul sur ces zones : `{mn}` (CER léger {pct(ok[mn]['cer_leger'])}, {pct(ok[mn]['exact'])} de zones exactes).",
              ("- Aucun lecteur n'est assez fiable seul (> 15 % d'erreur) : prévoir un accord entre lecteurs et une relecture des désaccords."
               if ok[mn]["cer_leger"] > 0.15 else "- Le meilleur lecteur est assez fiable seul ; les combinaisons servent surtout à signaler les doutes."),
              "- Les captures de la vérité terrain sont peu définies : les erreurs absolues sont pessimistes, le classement relatif reste valable.",
              "- 64 zones = un échantillon ; un écart de quelques points entre deux lecteurs n'est pas significatif.", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark de lecture de l'arabe", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--candidats", nargs="*", default=[], help="tess[:ara[:best][:psm7][:x3][:bin]] | grille:tess | easyocr | paddleocr | lm:<modèle>[:indice]")
    ap.add_argument("--verite", default=VERITE_DEFAUT)
    ap.add_argument("--sortie", default=os.path.join(RACINE, "benchmark", "sortie"))
    ap.add_argument("--tessdata", nargs="*", default=[], help="clé=dossier (ex. fast=C:\\tess\\fast best=C:\\tess\\best) : dossiers contenant ara.traineddata")
    ap.add_argument("--tesseract-cmd", default="")
    ap.add_argument("--indice-tess", default="tess:ara:psm7:x3", help="lecteur Tesseract qui fournit l'indice aux candidats lm:…:indice")
    ap.add_argument("--url", default="http://127.0.0.1:1234/v1")
    ap.add_argument("--hauteur-vlm", type=int, default=128, help="hauteur minimale (px) à laquelle les recadrages sont agrandis pour les modèles de vision")
    ap.add_argument("--sans-page", action="store_true", help="ne mesure pas le niveau « page entière »")
    ap.add_argument("--seuil-accord", type=float, default=0.2)
    ap.add_argument("--lister-lmstudio", action="store_true")
    a = ap.parse_args(argv)
    if a.lister_lmstudio:
        try:
            with urllib.request.urlopen(a.url.rstrip("/") + "/models", timeout=10) as r:
                for m in json.load(r).get("data", []):
                    print(m["id"])
            return 0
        except Exception as e:                                       # noqa: BLE001
            print(f"LM Studio injoignable sur {a.url} : {e}")
            return 1
    if not a.candidats:
        ap.error("indique au moins un candidat (--candidats ...) ; --lister-lmstudio pour voir les modèles chargés")
    pages = charger_verite(a.verite)
    n_items = sum(len(p["items"]) for p in pages)
    os.makedirs(a.sortie, exist_ok=True)
    resultats, synth = {}, {}
    for spec in a.candidats:
        try:
            lecteurs = construire(spec, a)
        except ValueError as e:
            print(f"✘ {e}")
            continue
        for lec in lecteurs:
            ok, pourquoi = lec.disponible()
            if not ok:
                print(f"✘ {lec.nom} ignoré : {pourquoi}")
                continue
            print(f"▶ {lec.nom} …", flush=True)
            res = evaluer(lec, pages, not a.sans_page)
            resultats[lec.nom], synth[lec.nom] = res, synthese(res)
            s = synth[lec.nom]
            print(f"   CER léger {pct(s['cer_leger'])} · exact {pct(s['exact'])} · {s['s_par_crop']:.2f} s/zone · page rappel {pct(s['rappel_page'])}")
    if not resultats:
        print("Aucun candidat n'a pu tourner.")
        return 2
    comb = combinaisons(resultats, a.seuil_accord) if len(resultats) > 1 else {"accords": [], "oracle": None}
    texte = rapport(synth, comb, n_items, pages)
    with open(os.path.join(a.sortie, "rapport_benchmark.md"), "w", encoding="utf-8") as f:
        f.write(texte)
    with open(os.path.join(a.sortie, "resultats_benchmark.json"), "w", encoding="utf-8") as f:
        json.dump({"synthese": synth, "combinaisons": comb, "detail": resultats}, f, ensure_ascii=False, indent=1)
    print("\n" + texte)
    print(f"Rapport : {os.path.join(a.sortie, 'rapport_benchmark.md')}  ·  détail par zone : resultats_benchmark.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
