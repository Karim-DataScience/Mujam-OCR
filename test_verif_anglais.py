"""Vérificateur d'arabe sur la translittération ANGLAISE (v5.7) : motifs -ullah, -uddin, ur Rahman, Abul, « X =Y », Ali en 2e mot, finale -a = ى.
Vérité terrain : benchmark/verite_passe_arabe.json (119 lectures de la 2e passe, jugées sur les images du PDF). python -m unittest test_verif_anglais"""
import json
import os
import unittest

import verif_arabe as VA

RACINE = os.path.dirname(os.path.abspath(__file__))


def cout(nom, arabe):
    return VA.coherence(nom, VA.normaliser_arabe_export(arabe))[0]


class TestMotifsAnglais(unittest.TestCase):
    def test_lectures_justes_acceptees(self):
        for nom, ar in [("Ahmadullah", "أحمد الله"), ("Barakatullah", "بركات الله"), ("Safiullah", "صفي الله"), ("Saifuddin", "سيف الدين"),
                        ("Izzuddin", "عزالدين"), ("Sadruddin", "صدر الدين"), ("Ala-ud-Din", "علاء الدين"), ("Aleemuddin", "عليم الدين"),
                        ("Sajidur Rahman", "ساجد الرحمن"), ("Abdur Rahman", "عبد الرحمن"), ("Abdus Salam", "عبد السلام"), ("Abdul Latif", "عبد اللطيف"),
                        ("Abul Barakat", "ابو البركات"), ("Abul Bashar", "ابو البشر"), ("Abu Dawud", "ابو داؤد"), ("Ebrahim (Heb) =Ibrahim", "ابراهيم"),
                        ("Ehtesham =Ihtisham", "احتشام"), ("Saghir Ali", "صغير علي"), ("Akif", "عاكف"), ("Itidal", "إعتدال"), ("Duha/Zuha", "ضحى"),
                        ("Izazuddawlah", "إعزازالدولة")]:
            self.assertLess(cout(nom, ar), 0.2, (nom, ar))

    def test_vraies_erreurs_toujours_signalees(self):
        for nom, ar in [("Bakht (Per.)", "بحث"), ("Dost Muhammad", "محمد دوست"), ("Diwan Muhammad", "محمد ديوان"), ("Isra", "إسرائيل"), ("Ajwad", "أجرد"),
                        ("Fahd", "فهيد")]:
            self.assertGreaterEqual(cout(nom, ar), 0.2, (nom, ar))

    def test_abu_seul_n_est_pas_un_article(self):
        self.assertLess(cout("Abu Dawud", "ابو داؤد"), 0.2)
        self.assertGreaterEqual(cout("Abu Dawud", "ابو البركات"), 0.5)

    def test_pas_d_effet_sur_les_noms_ordinaires(self):
        for nom, ar in [("Ahmad", "أحمد"), ("Aali", "عالي"), ("Aamir", "عامر"), ("Mudabbir", "مدبر"), ("Judd", "جد")]:
            self.assertLess(cout(nom, ar), 0.5, (nom, ar))

    def test_finale_a_ecrite_alif_maqsura(self):
        self.assertLess(cout("Musa", "موسى"), 0.2)                                # avant : 0,3
        self.assertLess(cout("Duha", "ضحى"), 0.2)


class TestVeriteTerrain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(RACINE, "benchmark", "verite_passe_arabe.json"), encoding="utf-8") as f:
            cls.lectures = json.load(f)["lectures"]

    def test_integrite(self):
        self.assertEqual(len(self.lectures), 119)
        self.assertEqual(sum(1 for o in self.lectures if not o["juste"]), 8)
        self.assertTrue(all(o["erreur"] for o in self.lectures if not o["juste"]))

    def test_bilan_au_seuil_par_defaut(self):
        """Avant le recalibrage : 85 justes adoptées, 26 justes refusées. Après : au moins 100 / au plus 8, et les 5 erreurs graves restent refusées."""
        adoptees_justes = refusees_justes = adoptees_fausses = 0
        graves_refusees = 0
        graves = {"Bakht (Per.)", "Dost Muhammad", "Diwan Muhammad", "Isra", "Fahd"}
        for o in self.lectures:
            c = cout(o["nom"], o["arabe"])
            ok = 0 <= c < 0.2
            if o["juste"]:
                adoptees_justes += ok
                refusees_justes += not ok
            else:
                adoptees_fausses += ok
                graves_refusees += (not ok) and o["nom"] in graves
        self.assertGreaterEqual(adoptees_justes, 100)
        self.assertLessEqual(refusees_justes, 8)
        self.assertEqual(graves_refusees, 5)
        self.assertLessEqual(adoptees_fausses, 3)                                # les 3 erreurs « invisibles » (espace, variante, alef) : limite connue


if __name__ == "__main__":
    unittest.main()
