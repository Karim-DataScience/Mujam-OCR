# -*- coding: utf-8 -*-
"""Mini vérité terrain pour l'autotest (`extraire_prenoms.py --autotest`).

Elle porte sur 4 pages PDF (55, 56, 100, 150) lues à l'œil : quelques fragments
très courts de définitions, les vedettes attendues et l'arabe attendu de 22
prénoms. Petit échantillon : il sert à vérifier qu'une installation fonctionne
et à comparer deux configurations, pas à mesurer un taux d'erreur global.
"""
import difflib
import re
import unicodedata

PAGES = (55, 56, 100, 150)

HEADS = set("""adam adame hawa haoua chisse idris nouh houd salih ibrahim khalilallah ralilallah sarah sara
abdechahid abdessabour abdessamad abdelmoussawir abdelmoussaouir abdezzahir abdelaziz abdelazim abdelali
nadil nadila nidal nahik nahika habbar hazim haibane haibana haiba mouhib mouhiba""".split())

KEYS = ["premier homme", "source de vie", "cadeau", "meurtre", "docte", "calame", "plume de roseau", "couture",
        "repos", "se repent", "thamud", "integre", "probe", "vertueux", "multitude", "monotheiste",
        "honore en islam", "confident de dieu", "premiere epouse", "souveraine",
        "temoin par excellence", "tres constant", "soutien universel", "cree les formes", "apparent",
        "tout puissant", "immense", "tres haut", "sublime",
        "emporte au combat", "combat lutte", "courageux", "sabre tres tranchant", "terrasse",
        "craint respecte venere", "melee de crainte", "majestueux"]

ARABIC = {"adam": "آدم", "hawa": "حواء", "chisse": "شيث", "idris": "إدريس", "nouh": "نوح", "houd": "هود",
          "salih": "صالح", "ibrahim": "إبراهيم", "khalilallah": "خليل الله", "sarah": "سارة",
          "nadil": "ناضل", "nadila": "ناضلة", "nidal": "نضال", "nahik": "نهيك", "nahika": "نهيكة",
          "habbar": "هبار", "hazim": "هازم", "haibane": "هيبان", "haibana": "هيبانة", "haiba": "هيبة",
          "mouhib": "مهيب", "mouhiba": "مهيبة"}


def norm(s):
    s = unicodedata.normalize('NFD', s.lower())
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', s)).strip()


def arab_norm(s):
    s = re.sub(r'[\u064B-\u065F\u0670\u0640\u200e\u200f]', '', s)
    s = re.sub(r'[آأإٱ]', 'ا', s).replace('ى', 'ي')
    return re.sub(r'[^\u0621-\u064A]', '', s)


def score(rows):
    """rows = entrées finales (dicts) restreintes aux pages de test."""
    text = norm(' '.join(r['Signification'] + ' ' + r['Translittération'] for r in rows))
    keys = sum(1 for k in KEYS if k in text)
    seen, spurious = set(), 0
    ar_sims, ar_exact, ar_found = [], 0, set()
    for r in rows:
        toks = [norm(t) for t in re.split(r'[ /]+', r['Prénom(s)']) if t.strip()]
        hit = [t for t in toks if t in HEADS]
        seen.update(hit)
        if not hit:
            spurious += 1
        for t in toks:
            if t in ARABIC and r['Arabe']:
                cands = [arab_norm(x) for x in r['Arabe'].split(' / ')] + [arab_norm(r['Arabe'])]
                sim = max(difflib.SequenceMatcher(None, c, arab_norm(ARABIC[t])).ratio() for c in cands)
                ar_sims.append(sim)
                ar_exact += int(sim > 0.999)
                ar_found.add(t)
    return {'entrées': len(rows), 'sens': f"{keys}/{len(KEYS)}", 'vedettes': f"{len(seen)}/{len(HEADS)}",
            'parasites': spurious, 'arabe_lu': f"{len(ar_found)}/{len(ARABIC)}",
            'arabe_exact': f"{ar_exact}/{len(ARABIC)}",
            'arabe_sim_moy': round(sum(ar_sims) / len(ar_sims), 2) if ar_sims else 0.0}
