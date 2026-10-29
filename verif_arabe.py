"""Vérification de l'arabe lu par le modèle à l'aide de la translittération latine de la « tête » (v4.3).

Principe : « Yaʿqūb » détermine presque les lettres de base de يَعْقُوب. On construit le squelette attendu (avec les
ambiguïtés de la translittération : s = س/ص, t = ت/ط, d = د/ض, h = ه/ح, sh = ش ou س+ح…), on l'aligne sur le squelette lu
(distance d'édition pondérée : une consonne fausse coûte 1, une voyelle longue/semi-voyelle 0,25-0,6, une voyelle courte 0)
et on pose un drapeau quand l'écart est net. Ne corrige JAMAIS tout seul : un drapeau + un indice, c'est tout.

Limites connues (à garder en tête) : la translittération perd les points souscrits (ح/ه, س/ص…) quand l'OCR ou le modèle
les a perdus ; une confusion entre deux lettres admises par la table (ex. ش lu pour س+ح) passe inaperçue.
"""
import re
import unicodedata

SEUIL = 0.5                                  # coût d’alignement à partir duquel on signale (≈ une consonne forte absente ou fausse, ou deux voyelles longues)

TANWIN_FINAL = re.compile(r"[\u064B\u064C\u064D]+(?=[\u0652\u0651\u064E-\u0650]*$)")
HARAKAT = r"\u064B-\u065F\u0670"


def retirer_tanwin_final(ar: str) -> str:
    """Les entrées du livre sont données sous leur forme de dictionnaire : un tanwin en fin de mot est une hallucination du
    modèle (يُوسُفٌ, أَيُّوبٌ). On le retire (un tanwin interne ou une fatha finale restent)."""
    return "/".join(re.sub(r"[\u064B\u064C\u064D](?=[\u0651\u0652\u0670]*(?:\s|$))", "", p) for p in ar.split("/"))


# ----------------------------------------------------------------------------- squelette lu
_UNIF = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ئ": "ي", "ؤ": "و", "ة": "ه", "ک": "ك", "ی": "ي"})
FAIBLES = set("اويء")


def squelette_lu(ar: str) -> list[str]:
    s = re.sub(rf"[{HARAKAT}\u0640\u200c-\u200f\s\-]", "", ar).translate(_UNIF)
    s = re.sub(r"[^\u0621-\u064A]", "", s)
    return list(s)


# ----------------------------------------------------------------------------- squelette attendu
_ACCENTS_LONGS = {c: "a" for c in "âäāàáãÂÄĀ"} | {c: "u" for c in "ûüūúùÛÜŪ"} | {c: "i" for c in "îïīíìÎÏĪ"}
_POINTS = {"Ș": "ṣ", "ș": "ṣ", "Ż": "ẓ", "ż": "ẓ", "Ħ": "ḥ", "Ĥ": "ḥ", "ħ": "ḥ", "ĥ": "ḥ", "Ḥ": "ḥ", "ş": "ṣ", "Ş": "ṣ", "Ṣ": "ṣ", "ḍ": "ḍ", "Ḍ": "ḍ", "ṭ": "ṭ", "Ṭ": "ṭ",
           "ẓ": "ẓ", "Ẓ": "ẓ", "ƶ": "ẓ", "ʿ": "ʿ", "‘": "ʿ", "`": "ʿ", "’": "’", "'": "’", "ʾ": "’"}
CONS = {
    "b": "ب", "t": "تط", "ṭ": "ط", "j": "ج", "ḥ": "ح", "h": "هح", "d": "دض", "ḍ": "ض", "r": "ر", "z": "زظ", "ẓ": "ظ",
    "s": "سص", "ṣ": "ص", "f": "ف", "q": "ق", "k": "ك", "l": "ل", "m": "م", "n": "ن", "g": "غجقك", "p": "ب", "v": "ف",
    "c": "كس", "x": "ك", "ʿ": "ع", "š": "شص",
}
DIGRAPHES = {"sh": "ش", "kh": "خ", "dh": "ذظض", "gh": "غ", "th": "ث", "ch": "ش"}
# unité = (ensemble de lettres admises, coût de suppression, nature) ; nature : S forte, F faible, O apostrophe
Unite = tuple


def _normaliser_translit(t: str) -> str:
    t = unicodedata.normalize("NFKC", t).replace("ı", "i")
    t = re.sub(r"\([^)]*\)", " ", t)
    return t.strip()


def variantes(translit: str) -> list[str]:
    """« Fâdil / Fâdila » -> ['Fâdil', 'Fâdila'] : l'arabe lu peut être celui de n'importe laquelle des formes."""
    t = re.sub(r"\([^)]*\)", " ", unicodedata.normalize("NFKC", translit).replace("ı", "i"))
    return [v.strip() for v in re.split(r"\s*[/,]\s*|\s+(?:et|ou)\s+", t) if len(v.strip()) >= 2]


def _unites(t: str, i: int) -> list[list[Unite]]:
    """Toutes les lectures possibles de la translittération (digraphes ambigus) -> listes d'unités."""
    if i >= len(t):
        return [[]]
    c = t[i]
    cl = c.lower()
    suite = lambda j: _unites(t, j)
    sorties = []
    if c in " -":
        return suite(i + 1)
    # voyelles
    base = _ACCENTS_LONGS.get(c)
    if base or cl in "aeiouéèêëy" and cl != "y":
        v = base or {"é": "i", "è": "a", "ê": "a", "ë": "a"}.get(cl, cl)
        long = base is not None
        ens = {"a": "ا", "i": "ي", "u": "و", "o": "و", "e": "ا"}[v]
        # â long : l'alef est souvent non écrit (alef suscrit : طه, الله, هذا) -> suppression peu coûteuse ; û / î toujours écrits
        unite = (ens, 0.2 if v == "a" else 0.4, "F") if long else (ens + "اوي", 0.0, "F")
        return [[unite] + r for r in suite(i + 1)]
    if cl == "y":
        return [[("ي", 0.6, "F")] + r for r in suite(i + 1)]
    if cl == "w":
        return [[("و", 0.6, "F")] + r for r in suite(i + 1)]
    # apostrophes
    cm = _POINTS.get(c, _POINTS.get(cl, c))
    if cm == "’":
        fin = all(ch in " -/" for ch in t[i + 1:]) or i + 1 >= len(t)
        return [[("عء", 0.2 if fin else 0.7, "O")] + r for r in suite(i + 1)]
    if cm == "ʿ":
        return [[("ع", 0.9, "S")] + r for r in suite(i + 1)]
    # digraphes : deux lectures (digraphe, ou deux lettres)
    d = t[i:i + 2].lower()
    if d in DIGRAPHES:
        for r in suite(i + 2):
            sorties.append([(DIGRAPHES[d], 1.0, "S")] + r)
        if d in ("sh", "th", "kh", "dh", "gh"):                      # « s+h » = س+ح, « t+h » = ت+ه…
            a, b = d[0], d[1]
            for r in _unites(t[:i] + a + "\u0001" + b + t[i + 2:], i):    # on force la séparation
                sorties.append(r)
        return sorties
    if c == "\u0001":
        return suite(i + 1)
    if c == "\u0003":                                              # الله
        return [[("ا", 0.3, "F"), ("ل", 1.0, "S"), ("ل", 0.8, "S"), ("ه", 1.0, "S")] + r for r in suite(i + 1)]
    if cl == "v":                                                  # « Marvam », « Havât » : y lu v par l'OCR
        return [[("في", 0.8, "S")] + r for r in suite(i + 1)]
    if c == "\u0002":
        return [[("ا", 0.3, "F"), ("ل", 0.8, "S")] + r for r in suite(i + 1)]
    key = cm if cm in CONS else cl
    if cl == "t" and (i + 1 >= len(t) or t[i + 1] in " -/"):
        return [[("تطه", 1.0, "S")] + r for r in suite(i + 1)]       # « -t » final de mot (Rahmat-Allâh) : parfois ة
    if cl == "h" and cm not in CONS:
        key = "h"
    ens = CONS.get(key)
    if ens is None:
        return suite(i + 1)
    # redoublement (shadda) : « Allah », « Ayyub » -> une seule lettre
    return [[(ens, 1.0, "S")] + r for r in suite(i + 1)]


def squelettes_attendus(translit: str) -> list[list[Unite]]:
    t = _normaliser_translit(translit)
    # article « al-, an-, ar-, as-, ash-, ad-… »
    ART = r"(?:a[lnrsdtzşṣṭḍẓ]|ash|adh|ath)"
    t = re.sub(rf"(^|[\s\-‑–]){ART}[-‑–]", lambda m: m.group(1) + "\u0002", t, flags=re.I)        # article assimilé : ال
    t = re.sub(r"(?<![A-Za-z])a[lhniI1]{1,3}[âaāàáä]h", "\u0003", t, flags=re.I)      # Allâh (et ses lectures OCR « AHâh », « ANHâh »)
    res = _unites(t, 0)
    # fusion des consonnes doublées identiques (shadda)
    propres = []
    for lecture in res[:12]:
        if lecture and lecture[0][2] == "F" and lecture[0][1] < 0.6 and not t.startswith("\u0002"):
            lecture = [(lecture[0][0] + "ع", min(lecture[0][1], 0.4), "F")] + lecture[1:]   # initiale : أ ou ع
        out = []
        for x in lecture:
            if out and x[2] in "SF" and out[-1][2] == x[2] and x[0] == out[-1][0] and x[1] != 0.8 and out[-1][1] != 0.8 and (x[2] == "S" or x[0] in ("ي", "و")):
                continue
            out.append(x)
        if out and out[-1][2] == "F" and out[-1][0].startswith("ا") and t[-1:].lower() == "a" and t[-1:] not in _ACCENTS_LONGS:
            out[-1] = ("هاة", 0.3, "F")                              # « -a » final = ة / ه
        if out and out[-1][2] == "F" and out[-1][1] == 0.2 and t[-1:] in "âäāàá":
            out[-1] = ("اهيى", 0.2, "F")                                   # « Ṣafwâ » : صَفْوَى / صَفْوَة
        propres.append(out)
    return propres or [[]]


# ----------------------------------------------------------------------------- alignement
def _cout_insertion(l: str) -> float:
    return 0.1 if l == "ء" else (0.25 if l in FAIBLES else 1.0)


def cout_alignement(attendu: list[Unite], lu: list[str]) -> float:
    n, m = len(attendu), len(lu)
    D = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        D[i][0] = D[i - 1][0] + attendu[i - 1][1]
    for j in range(1, m + 1):
        D[0][j] = D[0][j - 1] + _cout_insertion(lu[j - 1])
    for i in range(1, n + 1):
        ens, cdel, nat = attendu[i - 1]
        for j in range(1, m + 1):
            l = lu[j - 1]
            if l in ens:
                sub = 0.0
            elif nat == "F" and l in FAIBLES:
                sub = 0.5 if cdel >= 0.6 else 0.3                        # y/w consonantiques : on les confond moins volontiers
            elif nat == "O" and l in FAIBLES:
                sub = 0.15                                               # hamza portée par و / ي / ا (Lu'lu' = لُؤْلُؤ)
            else:
                sub = 1.0
            D[i][j] = min(D[i - 1][j - 1] + sub, D[i - 1][j] + cdel, D[i][j - 1] + _cout_insertion(l))
    return D[n][m]


def coherence(translit: str, arabe: str) -> tuple[float, str]:
    """-> (coût minimal d'alignement, indice lisible). Coût 0 = parfait ; ≥ SEUIL = incohérent ; (-1, « ») si non évaluable."""
    if not translit or not arabe:
        return -1.0, ""
    lectures = [squelette_lu(a) for a in re.split(r"\s*[/،,;]\s*", arabe) if a.strip()]
    lectures = [l for l in lectures if len(l) >= 2]
    if not lectures:
        return -1.0, ""
    meilleur = None
    for lu in lectures:
        for v in variantes(translit) or [translit]:
            for att in squelettes_attendus(v):
                if not att:
                    continue
                c = cout_alignement(att, lu)
                if lu[-1] == "ه" and len(lu) > 2:                       # ة finale d'une forme féminine non citée dans la tête
                    c = min(c, cout_alignement(att, lu[:-1]) + 0.3)
                if meilleur is None or c < meilleur[0]:
                    meilleur = (c, att, v, lu)
    if meilleur is None:
        return -1.0, ""
    c, att, v, lu = meilleur
    attendu_txt = "".join(("" if (u[2] == "F" and u[1] == 0.0) else (u[0] if len(u[0]) == 1 else "[" + u[0] + "]")) for u in att)
    return round(c, 2), f"lu « {''.join(lu)} » ; attendu ≈ « {attendu_txt} » d'après « {v} »"


def incoherent(translit: str, arabe: str) -> tuple[bool, str]:
    c, indice = coherence(translit, arabe)
    return (c >= SEUIL), indice
