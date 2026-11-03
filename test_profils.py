"""Tests des profils de documents (v5.0) : chargement, détection, application au moteur, exécution de bout en bout sur un petit PDF synthétique."""
import argparse
import contextlib
import csv
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import extraire_prenoms_llm as X
import profils as PR
from test_extraire_prenoms_llm import FauxLMStudio


def faux_pdf(chemin: str, pages: int = 3) -> None:
    import fitz
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page()
        p.insert_text((72, 100), f"Page {i + 1}", fontsize=24)
    doc.save(chemin)
    doc.close()


class TestProfils(unittest.TestCase):
    def test_tous_les_profils_sont_valides(self):
        profils = PR.liste_profils()
        self.assertGreaterEqual(len(profils), 9)
        for p in profils:
            self.assertTrue(p.get("nom") and p.get("description"), p.get("nom"))
            self.assertIn(p.get("methode", "vlm"), ("auto", "hybride", "vlm"))
            self.assertIsInstance(p.get("decalage_page_imprimee", 0), int)
            pr = PR.prompt_de(p)
            if p["nom"].startswith("geoffroy"):
                self.assertIsNone(pr)                              # prompt historique du moteur
            else:
                self.assertIn('"entrees"', pr)

    def test_gabarit_du_prompt_sans_accolade_orpheline(self):
        for p in PR.liste_profils():
            texte = PR.prompt_de(p)
            if texte:
                json.loads(PR.exemple_json(p))                       # l'exemple est du JSON valide
                self.assertNotIn("{titre}", texte)
                self.assertNotIn("{mise_en_page}", texte)

    def test_detection_par_nom_de_fichier(self):
        for nom, attendu in [("lelivredesprenom0000geof.pdf", "geoffroy_livre_des_prenoms_arabes"), ("A-Dictionary-of-Muslim-Names_by_Professor-Abdur-Rahman.pdf", "abdur_rahman_dictionary_of_muslim_names"),
                             ("dictionaryofmusl0000haki_1.pdf", "hakim_dictionary_of_muslim_names"), ("ledictionnairede0000peno_1.pdf", "penoncourt_dictionnaire_des_prenoms"),
                             ("islamic-names-dictionary.pdf", "hareeri_dictionary_of_islamic_names"), ("IslamicNamesByShaykhImaranAshrafUsmani.pdf", "usmani_islamic_names"),
                             ("Recueil_de_prénoms_Amazighs_de_M_A_HADDADOU_-_193_pages.pdf", "haddadou_prenoms_amazighs"), ("TREASURY_OF_MUSLIM_NAMES.pdf", "treasury_of_muslim_names")]:
            self.assertEqual(PR.detecter(nom)["nom"], attendu, nom)
        self.assertIsNone(PR.detecter("inconnu.pdf"))

    def test_charger_par_nom_chemin_et_erreur(self):
        self.assertEqual(PR.charger("generique")["nom"], "generique")
        with self.assertRaises(ValueError) as ctx:
            PR.charger("nexistepas")
        self.assertIn("generique", str(ctx.exception))

    def test_appliquer_profil_valeurs_par_defaut_et_options_explicites(self):
        ancien = (X.PAGE_OFFSET, X.THEMES_ACTIFS, X.EXEMPLE_DESC)
        self.addCleanup(lambda: (setattr(X, "PAGE_OFFSET", ancien[0]), setattr(X, "THEMES_ACTIFS", ancien[1]), setattr(X, "EXEMPLE_DESC", ancien[2])))
        p = PR.charger("abdur_rahman_dictionary_of_muslim_names")
        a = argparse.Namespace(debut=53, fin=338, pdf="", methode="auto", sortie="")
        X.appliquer_profil(p, a, [])
        self.assertEqual((a.debut, a.fin, a.methode), (7, 147, "vlm"))
        self.assertTrue(a.sortie.endswith("sortie_abdur_rahman_dictionary_of_muslim_names"))
        self.assertEqual(X.PAGE_OFFSET, -2)
        self.assertFalse(X.THEMES_ACTIFS)
        self.assertEqual(X.lookup_theme(100), ("", ""))
        # options données par l'utilisateur : le profil ne les écrase pas
        a = argparse.Namespace(debut=20, fin=30, pdf="", methode="hybride", sortie="mon_dossier")
        X.appliquer_profil(p, a, ["--debut", "20", "--fin=30", "--methode", "hybride", "--sortie", "mon_dossier"])
        self.assertEqual((a.debut, a.fin, a.methode, a.sortie), (20, 30, "hybride", "mon_dossier"))

    def test_profil_geoffroy_conserve_le_comportement_historique(self):
        ancien = (X.PAGE_OFFSET, X.THEMES_ACTIFS)
        self.addCleanup(lambda: (setattr(X, "PAGE_OFFSET", ancien[0]), setattr(X, "THEMES_ACTIFS", ancien[1])))
        a = argparse.Namespace(debut=53, fin=338, pdf="", methode="auto", sortie="")
        X.appliquer_profil(PR.charger("geoffroy_livre_des_prenoms_arabes"), a, [])
        self.assertEqual((a.debut, a.fin, a.methode, a.sortie, X.PAGE_OFFSET, X.THEMES_ACTIFS), (53, 338, "auto", "", 4, True))
        self.assertEqual(X.lookup_theme(55)[0], "1. Les prophètes")


class TestModeleParDefaut(unittest.TestCase):
    def _models(self, bonsai_charge):
        inst = [{"id": "x", "config": {"context_length": 12000, "parallel": 1}}]
        cap = {"vision": True, "reasoning": {"allowed_options": ["off", "on"], "default": "on"}}
        return {"models": [
            {"type": "llm", "key": "prism-ml/bonsai-27b", "display_name": "Bonsai", "loaded_instances": inst if bonsai_charge else [], "capabilities": cap},
            {"type": "llm", "key": "google/gemma-4-12b-qat", "display_name": "Gemma", "loaded_instances": [] if bonsai_charge else inst, "capabilities": cap}]}

    def test_modele_non_precise_utilise_le_seul_modele_charge(self):
        from unittest import mock
        with mock.patch.object(X, "detecter_api", return_value="natif"), mock.patch.object(X, "_json", return_value=self._models(False)):
            info = X.resoudre_modele(X.Config(modele="prism-ml/bonsai-27b"))
        self.assertEqual(info.cle, "google/gemma-4-12b-qat")


class TestProfilBoutEnBout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FauxLMStudio)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/v1"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FauxLMStudio.reset()
        self.ancien = (X.PAGE_OFFSET, X.THEMES_ACTIFS, X.EXEMPLE_DESC)

    def tearDown(self):
        X.PAGE_OFFSET, X.THEMES_ACTIFS, X.EXEMPLE_DESC = self.ancien
        shutil.rmtree(self.tmp, True)

    def lancer(self, *args):
        tampon = io.StringIO()
        with contextlib.redirect_stdout(tampon):
            return X.main(list(args) + ["--url", self.url, "--tokens-image", "0", "--suivi", "0"])

    def test_document_detecte_pagination_prompt_et_profil_enregistre(self):
        pdf = os.path.join(self.tmp, "A-Dictionary_Abdur.pdf")
        faux_pdf(pdf, 3)
        sortie = os.path.join(self.tmp, "sortie")
        FauxLMStudio.defaut = (json.dumps({"entrees": [{"prenoms": [{"genre": "MASCULIN", "orthographes": ["Nazim"], "arabe": "ناظم"}],
                                                          "description": "organiser, governor"}]}, ensure_ascii=False), "")
        code = self.lancer(pdf, "--debut", "1", "--fin", "2", "--sortie", sortie)
        self.assertEqual(code, 0)
        with open(os.path.join(sortie, "profil_utilise.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["nom"], "abdur_rahman_dictionary_of_muslim_names")
        with open(os.path.join(sortie, "prenoms_arabes.csv"), encoding="utf-8-sig") as f:
            lignes = list(csv.DictReader(f))
        self.assertEqual(len(lignes), 2)                                       # une entrée par page lue (méthode vlm imposée par le profil)
        self.assertEqual(lignes[0]["prénom_fr"], "Nazim")
        self.assertEqual(lignes[0]["page_imprimée"], str(1 - (-2)))              # décalage -2 : page imprimée = page PDF + 2
        self.assertEqual(lignes[0]["thème"], "")
        # le prompt envoyé au modèle est celui du profil (mise en page décrite), pas celui du Livre des prénoms arabes
        systemes = json.dumps(FauxLMStudio.requetes, ensure_ascii=False)
        self.assertIn("MALE NAMES", systemes)
        self.assertNotIn("Livre des prénoms arabes", systemes)
        # ré-export depuis le dossier : le profil enregistré est retrouvé (décalage conservé)
        self.assertEqual(self.lancer("--export", "--sortie", sortie, "--debut", "1", "--fin", "2"), 0)
        with open(os.path.join(sortie, "prenoms_arabes.csv"), encoding="utf-8-sig") as f:
            self.assertEqual(list(csv.DictReader(f))[0]["page_imprimée"], "3")

    def test_pdf_inconnu_profil_generique_et_pages_jusqu_a_la_fin(self):
        pdf = os.path.join(self.tmp, "inconnu.pdf")
        faux_pdf(pdf, 3)
        sortie = os.path.join(self.tmp, "s2")
        FauxLMStudio.defaut = (json.dumps({"entrees": []}), "")
        self.assertEqual(self.lancer(pdf, "--sortie", sortie), 0)
        with open(os.path.join(sortie, "profil_utilise.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["nom"], "generique")
        self.assertEqual(sorted(f for f in os.listdir(os.path.join(sortie, "cache")) if f.endswith(".json")), ["p001.json", "p002.json", "p003.json"])    # fin = 0 -> toutes les pages du PDF


if __name__ == "__main__":
    unittest.main()
