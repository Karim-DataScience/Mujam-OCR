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


def mot_connu(mot: str, dico, freq: Counter) -> bool:
    """Mot proposé par le modèle : connu du dictionnaire, ou présent au moins 2 fois dans la lecture de Tesseract elle-même (« guidance »).
    Les élisions (l'homme, qu'il) sont décomposées : les morceaux de moins de 3 lettres sont acceptés."""
    morceaux = [m for m in re.split(r"[’']", mot) if m]
    if not morceaux:
        return False
    return all(len(m) < 3 or not est_suspect(m, dico, freq, seuil_frequent=2) for m in morceaux)


def fusionner(ancien: str, propose: str, dico, freq: Counter, similarite_min: float = 0.5, rayon: int = 4) -> tuple[str, list[tuple[str, str]]]:
    """Corrige `ancien` (corps lu par Tesseract) avec `propose` (corps relu par le modèle), mot suspect par mot suspect.
    Chaque mot suspect de Tesseract est comparé aux mots du modèle situés à la même place (± `rayon` mots, position estimée par alignement) :
    on adopte le plus ressemblant (≥ 0,5) s'il est connu. Les différences adjacentes (ponctuation, coupures) n'empêchent plus la correction.
    -> (texte corrigé, [(ancien mot, nouveau mot), …]). Si rien n'est adopté, `ancien` est rendu tel quel."""
    a, b = ancien.split(), propose.split()
    if not a or not b:
        return ancien, []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    pos = [0.0] * len(a)                                  # position estimée de chaque mot de `a` dans `b`
    utilises: set[int] = set()                            # mots de `b` déjà appariés à l'identique
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        for k in range(i1, i2):
            pos[k] = j1 + (k - i1) * (j2 - j1) / max(1, i2 - i1)
        if op == "equal":
            utilises.update(range(j1, j2))
    changes: list[tuple[str, str]] = []
    sortie = list(a)
    for i, av in enumerate(a):
        pre, cur_a, suf = _coeur(av)
        if len(cur_a) < 4 or not cur_a.islower() or not est_suspect(cur_a, dico, freq):
            continue
        meilleur = None
        centre = int(round(pos[i]))
        for j in range(max(0, centre - rayon), min(len(b), centre + rayon + 1)):
            if j in utilises:
                continue
            _p, cur_b, _s = _coeur(b[j])
            if not cur_b or cur_b == cur_a or cur_b.replace("’", "'") == cur_a.replace("’", "'") or cur_b.lower() != cur_b or len(cur_b) < 3:
                continue
            if not mot_connu(cur_b, dico, freq):
                continue
            r = difflib.SequenceMatcher(None, cur_a, cur_b).ratio()
            if r >= similarite_min and (meilleur is None or r > meilleur[0]):
                meilleur = (r, j, cur_b)
        if meilleur:
            utilises.add(meilleur[1])
            sortie[i] = f"{pre}{meilleur[2]}{suf}"
            changes.append((cur_a, meilleur[2]))
    return " ".join(sortie), changes
