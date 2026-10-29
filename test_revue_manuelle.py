"""Tests de la revue manuelle (export des mots problématiques, application des corrections) et de l'arbitrage des têtes (v4.4)."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import extraire_prenoms_llm as X
import lecture_hybride as LH
import revue_manuelle as RM
import verif_arabe as VA


def entree(nom, ar, desc):
    return {"prenoms": [{"genre": "MASCULIN", "orthographes": [nom], "arabe": ar}], "description": desc, "description_incomplete": False}


class TestRevueManuelle(unittest.TestCase):
    def setUp(self):
        self.d = {"entrees": [entree("Yaqoub", "يَقُوب", "Ya'qūb (Jacob) : fils d'Ishâq."),
                              entree("Lout", "لُوط", "Lût (Loth) : prophète envoyé à Sodome."),
                              entree("Khalilallah", "خَلِيلُ اللهِ", "Khalil-ATâR : l'ami de Dieu."),
                              entree("Taha", "طه", "Ṭâhâ : lettres du Coran.")]}

    def test_liste_des_problemes(self):
        pb = RM.lister_problemes({58: self.d}, X.parse_description, VA, X.HARAKAT, inclure_voyelles=False)
        self.assertEqual([p["id"] for p in pb], ["p058-e1-b1", "p058-e3-b1"])           # Yaqoub (arabe faux) et Khalil (tête mal lue)
        pb2 = RM.lister_problemes({58: self.d}, X.parse_description, VA, X.HARAKAT, inclure_voyelles=True)
        self.assertIn("p058-e4-b1", [p["id"] for p in pb2])                              # « طه » sans voyelle

    def test_identifiants(self):
        self.assertEqual(RM.lire_identifiant("p058-e3-b1"), (58, 2, 0))
        with self.assertRaises(ValueError):
            RM.lire_identifiant("58-3")

    def test_application_des_corrections(self):
        lignes = [{"id": "p058-e1-b1", "arabe_corrige": "يَعْقُوبٌ", "translit_corrigee": "", "valide": ""},
                  {"id": "p058-e3-b1", "arabe_corrige": "", "translit_corrigee": "Khalîl-Allâh", "valide": "1"},
                  {"id": "p058-e9-b1", "arabe_corrige": "x", "translit_corrigee": "", "valide": ""},
                  {"id": "", "arabe_corrige": "", "translit_corrigee": "", "valide": ""}]
        modifies, st = RM.appliquer({58: self.d}, lignes, X.parse_description, X.nettoyer_arabe)
        self.assertEqual((st["arabe_corrige"], st["translit_corrigee"], st["valides"], st["ignorees"]), (1, 1, 2, 1))
        e = modifies[58]["entrees"]
        self.assertEqual(e[0]["prenoms"][0]["arabe"], "يَعْقُوبٌ")
        self.assertTrue(e[0]["prenoms"][0]["valide"])
        self.assertTrue(e[2]["description"].startswith("Khalîl-Allâh : l'ami de Dieu"))
        # plus signalés après correction
        self.assertEqual(RM.lister_problemes({58: modifies[58]}, X.parse_description, VA, X.HARAKAT), [])

    def test_valide_supprime_les_drapeaux_a_l_export(self):
        self.d["entrees"][0]["prenoms"][0]["valide"] = True
        e = {**self.d["entrees"][0], "id": 1, "page": 58, "partiel": False}
        r = X.exploser([e])[0]
        self.assertNotIn("arabe_incoherent_translit", r["à_vérifier"])
        del e["prenoms"][0]["valide"]
        self.assertIn("arabe_incoherent_translit", X.exploser([e])[0]["à_vérifier"])

    def test_page_html_autonome(self):
        lignes = [{"id": "p058-e1-b1", "page_pdf": 58, "page_imprimee": 54, "prenom_fr": "Yaqoub", "translittération": "Ya'qūb", "arabe_actuel": "يَقُوب",
                   "categorie": "incoherent", "indice": "x", "_b64_arabe": "data:image/png;base64,AAAA", "_b64_tete": ""}]
        h = RM.page_html(lignes, "Revue")
        self.assertIn('data-id="p058-e1-b1"', h)
        self.assertIn("data:image/png;base64,AAAA", h)
        self.assertIn("corrections.csv", h)

    def test_lecture_csv_excel_point_virgule(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        chemin = os.path.join(tmp, "c.csv")
        with open(chemin, "w", encoding="utf-8-sig") as f:
            f.write("id;arabe_corrige;translit_corrigee;valide\np058-e1-b1;يَعْقُوب;;1\n")
        self.assertEqual(RM.lire_corrections(chemin)[0]["arabe_corrige"], "يَعْقُوب")


class TestControleDesCorrections(unittest.TestCase):
    def test_signale_fautes_de_frappe_et_mots_sans_voyelle(self):
        d = {"entrees": [entree("Kuhayla", "كُحَيْلَاة", "Kuḥaylâ' : petite kohl."), entree("Moujir", "مجير", "Mujîr : protecteur."),
                         entree("Yaqoub", "يَعْقُوب", "Ya'qūb (Jacob) : fils d'Ishâq.")]}
        L = [{"id": "p058-e1-b1", "arabe_corrige": "كُحَيْلَاة"}, {"id": "p058-e2-b1", "arabe_corrige": "مجير"}, {"id": "p058-e3-b1", "arabe_corrige": "يَعْقُوب"}]
        res = RM.controler_corrections({58: d}, L, X.parse_description, VA, X.HARAKAT)
        self.assertEqual([r["id"] for r in res], ["p058-e1-b1", "p058-e2-b1"])
        self.assertIn("voyelle", res[1]["motif"])

    def test_verificateur_formes_feminines_et_ou(self):
        for tr, ar in [("Ḥākim", "حَاكِمَة"), ("Tawfîq", "تَوْفِيقَة"), ("Lu'lu'", "لُؤْلُؤ"), ("Jihân ou Jihân", "جِهان"), ("Ṣafwâ", "صَفْوَة")]:
            self.assertFalse(VA.incoherent(tr, ar)[0], (tr, ar))
        self.assertTrue(VA.incoherent("Kuḥaylâ'", "كُحَيْلَاة")[0])


class TestArbitrageDesTetes(unittest.TestCase):
    def test_tete_du_modele_adoptee_si_coherente_avec_l_arabe(self):
        desc, w = LH.fusionner_tete("Khalil-ATâR : l'ami de Dieu.", "Khalîl-Allâh", ["خَلِيلُ اللهِ"])
        self.assertTrue(desc.startswith("Khalîl-Allâh : "))
        self.assertIn("adoptée", w)

    def test_tete_du_modele_refusee_si_incoherente(self):
        desc, w = LH.fusionner_tete("Khalil-ATâR : l'ami de Dieu.", "Zorglub", ["خَلِيلُ اللهِ"])
        self.assertTrue(desc.startswith("Khalil-ATâR : "))
        self.assertIn("divergente", w)

    def test_sans_arabe_comportement_inchange(self):
        desc, w = LH.fusionner_tete("Khalil-ATâR : l'ami.", "Khalîl-Allâh")
        self.assertTrue(desc.startswith("Khalil-ATâR : "))


class TestRelireTetes(unittest.TestCase):
    def test_relecture_des_tetes_avec_faux_modele(self):
        from PIL import Image
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = X.Config(pdf="faux.pdf", sortie=tmp, pages=[56], essais=2)
        d = {"page": 56, "bande": 1, "decoupe": 1, "empreinte": X.empreinte(cfg), "statut": "ok", "avertissements": [],
             "entrees": [entree("Khalilallah", "خَلِيلُ اللهِ", "Khalil-ATâR : l'ami de Dieu."), entree("Lout", "لُوط", "Lût (Loth) : prophète.")]}
        X.ecrire_atomique(X.chemin_cache(cfg, 56, 1), json.dumps(d, ensure_ascii=False))
        mk = lambda nom, tete: LH.EntreeLue(blocs=[LH.BlocLu(genre="MASCULIN", orthographes=[nom], arabe=(0, 0, 10, 10))], tete_box=(0, 0, 20, 8), tete_texte=tete)
        lec = LH.PageLue(entrees=[mk("Khalilallah", "Khalil-ATâR"), mk("Lout", "Lût")], problemes=[], page_texte=False)
        reponses = iter(["Khalil-Allaq", "Khalîl-Allâh"])      # 1re lecture incohérente avec l'arabe (refusée), 2e bonne

        def faux_llm(cfg_, info, png, systeme, texte, **kw):
            return {"content": json.dumps({"T1": next(reponses)}, ensure_ascii=False), "reasoning": "", "abandon": "", "finish": "stop", "tokens": 12,
                    "tokens_reflexion": 0, "tokens_entree": 50, "tps": 30.0, "ttft": 0.2, "erreur": ""}

        with mock.patch.object(X, "resoudre_modele", return_value=X.InfoModele(id="faux")), mock.patch.object(X, "afficher_info"), \
                mock.patch.object(X, "image_native", return_value=Image.new("RGB", (50, 50), "white")), \
                mock.patch.object(X, "nettoyer_page", side_effect=lambda im, *a, **k: im), mock.patch.object(LH, "lire_page", return_value=lec), \
                mock.patch.object(LH, "disponible", return_value=(True, "ok")), mock.patch.object(X, "appeler_llm", side_effect=faux_llm):
            st = X.relire_tetes(cfg)
        self.assertEqual((st["cibles"], st["corrigees"]), (1, 1))                        # seule la tête de Khalil était incohérente
        with open(X.chemin_cache(cfg, 56, 1), encoding="utf-8") as f:
            apres = json.load(f)
        self.assertTrue(apres["entrees"][0]["description"].startswith("Khalîl-Allâh : l'ami"))
        self.assertTrue(apres["entrees"][1]["description"].startswith("Lût (Loth)"))
        self.assertEqual(apres["entrees"][0]["corrections_auto"][0]["avant"], "Khalil-ATâR")


if __name__ == "__main__":
    unittest.main()
