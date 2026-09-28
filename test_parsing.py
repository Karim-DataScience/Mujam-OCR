# -*- coding: utf-8 -*-
"""Tests unitaires du parseur et des garde-fous (sans OCR, sans GPU) : `python test_parsing.py`
(ou `pytest`). À relancer après toute modification des règles de parsing."""
import numpy as np

import extraire_prenoms as ep

BLACK, RED = (20, 20, 20), (190, 40, 70)


def W(text, left, right, color=BLACK, conf=90, top=0, bottom=40):
    return {'text': text, 'left': left, 'right': right, 'top': top, 'bottom': bottom, 'color': color, 'conf': conf}


def L(words, top=0):
    return {'top': top, 'left': min(w['left'] for w in words), 'right': max(w['right'] for w in words),
            'bottom': top + 40, 'text': ' '.join(w['text'] for w in words), 'color': BLACK, 'words': words}


def test_pair_on_same_line_is_split_by_color():
    ln = L([W('Nadil', 100, 300), W('J—wb', 400, 500), W('Nadila', 900, 1100, RED), W("'", 1150, 1160)])
    cl = ep.extract_headword_clusters(ln)
    assert [(c['gender'], c['words']) for c in cl] == [('Masculin', ['Nadil']), ('Féminin', ['Nadila'])]


def test_arabic_goes_to_the_name_on_its_left():
    ln = L([W('Nadil', 100, 300), W('ناضل', 500, 800), W('Nadila', 1500, 1800, RED), W('ناضلة', 2000, 2300)])
    cl = ep.extract_headword_clusters(ln)
    assert cl[0]['arabic'] == 'ناضل' and cl[1]['arabic'] == 'ناضلة'


def test_two_arabic_words_are_joined_right_to_left():
    ln = L([W('Khalilallah', 100, 500), W('الله', 700, 900), W('خليل', 950, 1200)])
    assert ep.extract_headword_clusters(ln)[0]['arabic'] == 'خليل الله'


def test_sentence_fragments_are_not_headwords():
    assert ep.extract_headword_clusters(L([W('à', 0, 20), W('Ibrâhîm', 30, 200), W('dans', 210, 300),
                                            W('le', 310, 340), W('Coran', 350, 450), W('(4,', 460, 500),
                                            W('125).', 510, 600)])) == []
    assert ep.extract_headword_clusters(L([W('Âdam', 0, 100), W('(Adam)', 110, 250), W(':', 260, 270),
                                            W('premier', 280, 400), W('homme', 410, 500)])) == []


def _parse(lines_words):
    st = {'entries': [], 'pending': [], 'open': []}
    ep.parse_page_lines([L(ws, top=i * 100) for i, ws in enumerate(lines_words)], 55, '1. T', 'A. T', st)
    return st


def test_variants_merge_then_definition_and_gender():
    st = _parse([[W('Adam', 100, 300)], [W('Adame', 100, 300)],
                 [W('Âdam', 100, 200), W('(Adam)', 210, 300), W(':', 310, 320), W('premier', 330, 400),
                  W('homme.', 410, 500)],
                 [W('roux,', 100, 200), W('rouge', 210, 300), W('comme', 310, 400), W('le', 410, 440),
                  W('sang.', 450, 500)],
                 [W('Hawa', 800, 1000, RED)],
                 [W('Hawâ’', 100, 200), W('(Ève)', 210, 300), W(':', 310, 320), W('épouse', 330, 420),
                  W('d’Âdam.', 430, 520)]])
    rows = ep.finalize_entries(st['entries'])
    assert len(rows) == 2
    assert rows[0]['Prénom(s)'] == 'Adam / Adame' and rows[0]['Genre'] == 'Masculin'
    assert rows[0]['Translittération'] == 'Âdam' and rows[0]['Équivalent'] == 'Adam'
    assert 'roux' in rows[0]['Signification']
    assert rows[1]['Prénom(s)'] == 'Hawa' and rows[1]['Genre'] == 'Féminin'


def test_headers_and_page_numbers_are_skipped():
    for t in ('MASCULIN FEMININ', '52 LE LIVRE DES PRÉNOMS ARABES', '2', 'A. LES PROPHÈTES'):
        assert ep.is_header_skip(t), t
    assert not ep.is_header_skip('Adam')


def test_finalize_truncates_noise_tail_but_keeps_short_definitions():
    raw = [{'headwords': ['Aymane'], 'arabic': '', 'arabic_alt': '', 'gender': 'Masculin', 'chapter': '1. T',
            'subtheme': 'A. T', 'page': 1,
            'def_parts': ['Aymane : main droite. Dans les traditions sémitiques.', '70 4 7 5 & 0 SJ']},
           {'headwords': ['Hayat'], 'arabic': '', 'arabic_alt': '', 'gender': 'Féminin', 'chapter': '1. T',
            'subtheme': 'A. T', 'page': 1, 'def_parts': ['Hayât : la vie.']}]
    rows = ep.finalize_entries(raw)
    assert len(rows) == 2 and rows[0]['Signification'].endswith('sémitiques.') and rows[1]['Signification'] == 'la vie.'


def test_finalize_drops_intro_title_leaks_and_vowelless_names():
    base = {'arabic': '', 'arabic_alt': '', 'gender': 'Masculin', 'chapter': '1. T', 'page': 1,
            'def_parts': ['Xx : une définition tout à fait normale.']}
    raw = [dict(base, headwords=['Adam'], subtheme=''),               # texte d'introduction (pas de sous-thème)
           dict(base, headwords=['BEAUTÉ 179'], subtheme='A. T'),     # en-tête courant
           dict(base, headwords=['RS'], subtheme='A. T'),             # pas de voyelle
           dict(base, headwords=['Jamil', 'BEAUTÉ 179'], subtheme='A. T')]  # en-tête retiré, prénom gardé
    rows = ep.finalize_entries(raw)
    assert [r['Prénom(s)'] for r in rows] == ['Jamil']


def test_quality_flags():
    row = {'Signification': 'intègre 11116916, probe', 'Arabe': '', 'Translittération': '', 'Prénom(s)': 'Lu'}
    f = ep.quality_flags(row)
    assert {'arabe_vide', 'suite_de_chiffres', 'sans_point_final', 'translit_vide', 'vedette_courte'} <= set(f)
    ok = {'Signification': 'le lion.', 'Arabe': 'أسد', 'Arabe (Tesseract)': 'اسد', 'Translittération': 'Asad',
          'Prénom(s)': 'Assad'}
    assert ep.quality_flags(ok) == []
    div = dict(ok, **{'Arabe (Tesseract)': 'سحي'})
    assert 'arabe_divergent' in ep.quality_flags(div)


class FakeReader:
    """Imite reader.recognize : résultats renvoyés dans le désordre (comme en lot sur GPU)."""
    def __init__(self, texts):
        self.texts = texts

    def recognize(self, gray, horizontal_list, free_list, detail=1, batch_size=1, **kw):
        out = []
        for i, (x0, x1, y0, y1) in enumerate(horizontal_list):
            t, c = self.texts[i % len(self.texts)] if batch_size == 1 else self.texts[i]
            out.append([[[x0, y0], [x1, y0], [x1, y1], [x0, y1]], t, c])
        return out[::-1]  # ordre inversé


def test_recognize_boxes_maps_by_position_not_by_rank():
    boxes = [[10, 50, 10, 30], [10, 50, 40, 60], [10, 50, 70, 90]]
    got = ep.recognize_boxes(FakeReader([('a', .9), ('b', .8), ('c', .7)]), np.zeros((100, 100), np.uint8),
                             boxes, 32, 'greedy')
    assert got == [('a', .9), ('b', .8), ('c', .7)]


def _latin_case(tess, easy, conf, tess_conf=40):
    ln = L([W(tess, 100, 300, conf=tess_conf)])
    ep.refine_latin_words(np.zeros((100, 400), np.uint8), [ln], FakeReader([(easy, conf)]), 32, 'greedy', 30)
    return ln['words'][0]['text']


def test_latin_refinement_guards():
    assert _latin_case('11116916,', 'intègre,', .86) == 'intègre,'          # italique lu comme des chiffres
    assert _latin_case('Thamüûd.', 'Thamûd.', .84) == 'Thamûd.'
    assert _latin_case('Ibrâhîm', '[brâhîm', .97) == 'Ibrâhîm'               # caractère improbable
    assert _latin_case('d’Adam', 'dAdam', .90) == 'd’Adam'                    # apostrophe perdue
    assert _latin_case('Coran', 'Corah', .60) == 'Coran'                      # confiance insuffisante
    assert _latin_case('Salih', 'Sal', .95) == 'Salih'                        # longueur incohérente
    assert _latin_case('Nûh', 'Autre', .95, tess_conf=95) == 'Nûh'            # Tesseract déjà sûr : on ne relit pas


def test_theme_lookup_and_page_specs():
    assert ep.lookup_theme(55) == ('1. Les prophètes', "A. Les prophètes, d'Adam à Jésus")
    assert ep.lookup_theme(53) == ('1. Les prophètes', '')
    assert ep.lookup_theme(338) == ("11. Cette vie et l'autre", 'B. Vie éternelle et paradis')
    assert ep.lookup_theme(500) == ('', '')
    assert ep.parse_pages('55,56,100-102') == [55, 56, 100, 101, 102]
    assert 1 <= ep.auto_workers() <= 12


def test_gpu_off_and_arabic_helpers():
    assert ep.detect_gpu('non')[0] is False
    assert ep.arab_norm('آدَم') == ep.arab_norm('ادم') == 'ادم'


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f"OK    {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"ECHEC {name} : {type(e).__name__}: {e}")
    raise SystemExit(1 if fails else 0)
