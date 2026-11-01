"""Correction conservatrice du français des définitions lues par Tesseract (v4.5).

Tesseract lit bien le français courant mais laisse des fautes (« seroiteur », « lunnière », « fennne », « bonlieur »). Le modèle de vision relit le paragraphe
sur l'image ; ici on décide QUELS mots on lui emprunte :
  - seuls les mots SUSPECTS de Tesseract (absents du dictionnaire français et rares dans le corpus) peuvent être remplacés ;
  - le mot proposé doit être connu du dictionnaire (ou fréquent dans le corpus) et ressembler au mot d'origine (similarité ≥ 0,5) ;
  - tout le reste de la lecture de Tesseract est conservé tel quel (on ne laisse jamais le modèle « réécrire » le texte).
Dépendance facultative : `pip install pyspellchecker` (dictionnaire français).
"""
import difflib
import re
from collections import Counter

MOT = re.compile(r"(?<![\wÀ-ÿ])[a-zàâçéèêëîïôûùüÿœ]{4,}(?![\wÀ-ÿ])")
# mots légitimes du domaine souvent absents d'un dictionnaire courant
LEXIQUE_DOMAINE = {
    "sourate", "sourates", "kunya", "hanif", "soufi", "soufis", "soufisme", "persan", "persane", "persans", "omeyyade", "omeyyades", "abbasside", "abbassides",
    "andalou", "andalouse", "baraka", "imam", "imams", "hadith", "hadiths", "calife", "califes", "coranique", "coraniques", "qurayshite", "quraychite", "quraychites",
    "mecquois", "mecquoise", "médinois", "médinoise", "chiite", "chiites", "sunnite", "sunnites", "surérogatoire", "prophétique", "mystique", "ottoman", "ottomane",
    "ayatollah", "oumma", "djihad", "jihad", "hégire", "mawla", "tribu", "ansar", "muhajirun", "koweïtien", "maghrébin", "maghrébine", "maghreb", "berbère", "amazigh",
}


def charger_dictionnaire():
    """-> SpellChecker français, ou None si pyspellchecker n'est pas installé."""
    try:
        from spellchecker import SpellChecker
    except ImportError:
        return None
    return SpellChecker(language="fr")


def corps(description: str) -> tuple[str, str]:
    """(tête, corps) : coupe au premier « : » des 90 premiers caractères ; ('', description) sinon."""
    m = re.match(r"^(.{1,90}?)\s*:\s+(.*)$", description, re.S)
    return (m.group(1), m.group(2)) if m else ("", description)


def frequences(descriptions) -> Counter:
    c: Counter = Counter()
    for d in descriptions:
        c.update(MOT.findall(corps(d)[1]))
    return c


def est_suspect(mot: str, dico, freq: Counter, seuil_frequent: int = 3) -> bool:
    """Mot minuscule d'au moins 4 lettres, inconnu du dictionnaire, hors lexique du domaine et rare dans le corpus."""
    if mot in LEXIQUE_DOMAINE or freq.get(mot, 0) >= seuil_frequent:
        return False
    return mot not in dico


def mots_suspects(texte: str, dico, freq: Counter) -> list[str]:
    return [m for m in MOT.findall(texte) if est_suspect(m, dico, freq)]


def _coeur(mot: str) -> tuple[str, str, str]:
    m = re.match(r"^([^\wÀ-ÿ]*)(.*?)([^\wÀ-ÿ]*)$", mot, re.S)
    return m.group(1), m.group(2), m.group(3)


def fusionner(ancien: str, propose: str, dico, freq: Counter, similarite_min: float = 0.5) -> tuple[str, list[tuple[str, str]]]:
    """Corrige `ancien` (corps lu par Tesseract) avec `propose` (corps relu par le modèle), mot suspect par mot suspect.
    -> (texte corrigé, [(ancien mot, nouveau mot), …]). Si rien n'est adopté, `ancien` est rendu tel quel."""
    a, b = ancien.split(), propose.split()
    if not a or not b:
        return ancien, []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    changes: list[tuple[str, str]] = []
    sortie = list(a)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or (i2 - i1) != (j2 - j1):                 # on ne traite que les remplacements mot pour mot
            continue
        for k in range(i2 - i1):
            av, nv = a[i1 + k], b[j1 + k]
            pre, cur_a, suf = _coeur(av)
            _p, cur_b, _s = _coeur(nv)
            if not cur_a or not cur_b or cur_a == cur_b or cur_a.replace("’", "'") == cur_b.replace("’", "'"):
                continue                                              # identiques, ou seule l'apostrophe typographique diffère
            if len(cur_a) < 4:
                continue                                              # mots courts : trop ambigus pour un remplacement automatique
            if not (cur_a.islower() and est_suspect(cur_a, dico, freq)):
                continue                                              # le mot de Tesseract n'était pas suspect : on le garde
            if cur_b.lower() != cur_b or est_suspect(cur_b, dico, freq) or len(cur_b) < 3:
                continue                                              # le mot du modèle doit être connu (ou fréquent)
            if difflib.SequenceMatcher(None, cur_a, cur_b).ratio() < similarite_min:
                continue
            sortie[i1 + k] = f"{pre}{cur_b}{suf}"
            changes.append((cur_a, cur_b))
    return " ".join(sortie), changes
