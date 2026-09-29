# -*- coding: utf-8 -*-
"""Mini vérité terrain pour l'autotest (`extraire_prenoms_llm.py --autotest`).

Elle porte sur 4 pages PDF (55, 56, 100, 150) lues à l'œil : les prénoms attendus page par page,
leur genre, l'arabe attendu (lettres de base, sans voyelles) de 22 prénoms et quelques fragments
de définitions. Petit échantillon : il sert à vérifier qu'une installation fonctionne et à comparer
deux configurations (ex. --decoupe 1 contre 2), pas à mesurer un taux d'erreur global.
"""
import difflib
import re
import unicodedata

PAGES = (55, 56, 100, 150)

TRUTH = {
    55: {"heads": "adam adame hawa haoua chisse idris",
         "keys": ["premier homme", "source de vie", "cadeau", "meurtre", "docte", "calame",
                  "plume de roseau", "couture"]},
    56: {"heads": "nouh houd salih ibrahim khalilallah ralilallah sarah sara",
         "keys": ["repos", "se repent", "thamud", "integre", "probe", "vertueux", "multitude",
                  "monotheiste", "honore en islam", "confident de dieu", "premiere epouse", "souveraine"]},
    100: {"heads": "abdechahid abdessabour abdessamad abdelmoussawir abdelmoussaouir abdezzahir abdelaziz "
                   "abdelazim abdelali",
          "keys": ["temoin par excellence", "tres constant", "soutien universel", "cree les formes", "apparent",
                   "tout puissant", "immense", "tres haut", "sublime"]},
    150: {"heads": "nadil nadila nidal nahik nahika habbar hazim haibane haibana haiba mouhib mouhiba",
          "keys": ["emporte au combat", "combat lutte", "courageux", "sabre tres tranchant", "terrasse",
                   "craint respecte venere", "melee de crainte", "majestueux"]},
}

ARABIC = {"adam": "آدم", "hawa": "حواء", "chisse": "شيث", "idris": "إدريس", "nouh": "نوح", "houd": "هود",
          "salih": "صالح", "ibrahim": "إبراهيم", "khalilallah": "خليل الله", "sarah": "سارة",
          "nadil": "ناضل", "nadila": "ناضلة", "nidal": "نضال", "nahik": "نهيك", "nahika": "نهيكة",
          "habbar": "هبار", "hazim": "هازم", "haibane": "هيبان", "haibana": "هيبانة", "haiba": "هيبة",
          "mouhib": "مهيب", "mouhiba": "مهيبة"}

FEMININ = set("hawa haoua sarah sara nadila nahika haibana haiba mouhiba".split())


def norm(s):
    s = unicodedata.normalize('NFD', s.lower())
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', s)).strip()


def arab_norm(s):
    s = re.sub(r'[\u064B-\u065F\u0670\u0640\u200e\u200f]', '', s)
    s = re.sub(r'[آأإٱ]', 'ا', s).replace('ى', 'ي')
    return re.sub(r'[^\u0621-\u064A]', '', s)


def score(rows, pages=None):
    """rows = lignes finales (dicts aux clés v3) ; `pages` restreint la comparaison aux pages traitées."""
    pages = [p for p in (pages or PAGES) if p in TRUTH]
    heads = {h for p in pages for h in TRUTH[p]["heads"].split()}
    keys = [k for p in pages for k in TRUTH[p]["keys"]]
    attendus_ar = {h for h in heads if h in ARABIC}
    rows = [r for r in rows if r["page_pdf"] in pages]

    texte = norm(' '.join(r['description_en_français'] + ' ' + r['translittération'] for r in rows))
    vus = {norm(r['prénom_fr']) for r in rows} & heads
    parasites = sum(1 for r in rows if norm(r['prénom_fr']) not in heads)

    genre_ok = genre_tot = 0
    sims, exacts, trouves = [], set(), set()
    avec_voyelles = 0
    for r in rows:
        n = norm(r['prénom_fr'])
        if n in heads:
            genre_tot += 1
            attendu = 'FEMININ' if n in FEMININ else 'MASCULIN'
            genre_ok += int(r['genre'] == attendu)
        if n in ARABIC and r['prénom_arab_diacritiques']:
            sim = difflib.SequenceMatcher(None, arab_norm(r['prénom_arab_diacritiques']), arab_norm(ARABIC[n])).ratio()
            sims.append(sim)
            trouves.add(n)
            if sim > 0.999:
                exacts.add(n)
        avec_voyelles += int(bool(re.search(r'[\u064B-\u065F\u0670]', r['prénom_arab_diacritiques'])))

    return {'lignes': len(rows),
            'sens': f"{sum(k in texte for k in keys)}/{len(keys)}",
            'vedettes': f"{len(vus)}/{len(heads)}",
            'parasites': parasites,
            'genre': f"{genre_ok}/{genre_tot}",
            'arabe_lu': f"{len(trouves)}/{len(attendus_ar)}",
            'arabe_exact': f"{len(exacts)}/{len(attendus_ar)}",
            'arabe_sim_moy': round(sum(sims) / len(sims), 2) if sims else 0.0,
            'arabe_voyelles': f"{avec_voyelles}/{len(rows)}"}
