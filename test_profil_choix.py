"""Choix du profil de document, en ligne de commande ET dans la TUI (v5.1) : même chemin (X.cfg_avec_profil).
N'a besoin d'aucun PDF réel : un PDF synthétique est créé à la volée (nommé comme l'un des vrais pour tester la détection).
Lancer : python -m unittest test_profil_choix"""
import argparse
import json
import logging
import os
import shutil
import tempfile
import unittest

import fitz

import extraire_prenoms_llm as X

try:
    import prenoms_tui as T
    from textual.widgets import DataTable, Input, Select, Static
    TEXTUAL = True
except ImportError:                                             # pragma: no cover
    TEXTUAL = False

logging.getLogger("asyncio").setLevel(logging.ERROR)
N_PAGES = 12


def faire_pdf(dossier: str, nom: str, n: int = N_PAGES) -> str:
    chemin = os.path.join(dossier, nom)
    doc = fitz.open()
    for i in range(n):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    doc.save(chemin)
    doc.close()
    return chemin


def namespace(**kw) -> argparse.Namespace:
    d = X.valeurs_effectives(os.path.join(tempfile.gettempdir(), "prenoms_aucune_config.json"))
    d.update(kw)
    return argparse.Namespace(**d)


class TestCfgAvecProfil(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.treasury = faire_pdf(self.tmp, "TREASURY_OF_MUSLIM_NAMES.pdf")
        self.inconnu = faire_pdf(self.tmp, "document_inconnu.pdf")

    def tearDown(self):
        X.reinitialiser_profil()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_profil_detecte_par_le_nom_pages_et_sortie_du_profil(self):
        cfg, profil = X.cfg_avec_profil(namespace(pdf=self.treasury), [])
        self.assertEqual(profil["nom"], "treasury_of_muslim_names")
        self.assertEqual(cfg.pages, list(range(1, N_PAGES + 1)))                 # fin = 0 dans le profil : dernière page du PDF
        self.assertTrue(cfg.sortie.endswith("sortie_treasury_of_muslim_names") or "treasury" in os.path.basename(cfg.sortie))
        self.assertNotEqual(os.path.basename(cfg.sortie), "sortie_llm")
        self.assertEqual(cfg.profil_nom, "treasury_of_muslim_names")
        self.assertNotEqual(cfg.prompt, X.Config().prompt)                       # prompt du profil, pas celui du livre de Geoffroy

    def test_pages_donnees_par_l_utilisateur_priment(self):
        cfg, _ = X.cfg_avec_profil(namespace(pdf=self.treasury, pages="3,5-6"), ["--pages"])
        self.assertEqual(cfg.pages, [3, 5, 6])

    def test_profil_explicite_prime_sur_le_nom_du_pdf(self):
        cfg, profil = X.cfg_avec_profil(namespace(pdf=self.treasury, profil="usmani_islamic_names"), [])
        self.assertEqual(profil["nom"], "usmani_islamic_names")
        self.assertEqual(cfg.pages, list(range(1, N_PAGES + 1)))

    def test_profil_inconnu_leve_valueerror(self):
        with self.assertRaises(ValueError):
            X.cfg_avec_profil(namespace(pdf=self.treasury, profil="n_existe_pas"), [])

    def test_pdf_inconnu_profil_generique(self):
        _, profil = X.cfg_avec_profil(namespace(pdf=self.inconnu, autotest=True), ["--autotest"])
        self.assertEqual(profil["nom"], "generique")

    def test_changer_de_profil_ne_garde_rien_du_precedent(self):
        X.cfg_avec_profil(namespace(pdf=self.treasury), [])
        X.cfg_avec_profil(namespace(pdf=self.treasury, profil="haddadou_prenoms_amazighs"), [])
        decalage_haddadou = X.PAGE_OFFSET
        cfg, profil = X.cfg_avec_profil(namespace(pdf=self.treasury, profil="geoffroy_livre_des_prenoms_arabes"), [])
        self.assertEqual(profil["nom"], "geoffroy_livre_des_prenoms_arabes")
        self.assertEqual((X.PAGE_OFFSET, X.THEMES_ACTIFS, X.EXEMPLE_DESC), (X.PAGE_OFFSET_DEFAUT, True, X.EXEMPLE_DESC_DEFAUT))
        self.assertIsNotNone(decalage_haddadou)

    def test_refuse_de_melanger_deux_documents_dans_un_dossier(self):
        sortie = os.path.join(self.tmp, "sortie")
        os.makedirs(os.path.join(sortie, "cache"))
        with open(os.path.join(sortie, "cache", "p0001_b1.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(sortie, "profil_utilise.json"), "w") as f:
            json.dump({"nom": "usmani_islamic_names"}, f)
        with self.assertRaises(ValueError) as e:
            X.cfg_avec_profil(namespace(pdf=self.treasury, profil="treasury_of_muslim_names", sortie=sortie), [])
        self.assertIn("usmani_islamic_names", str(e.exception))
        cfg, _ = X.cfg_avec_profil(namespace(pdf=self.treasury, profil="usmani_islamic_names", sortie=sortie), [])    # même profil : accepté
        self.assertEqual(cfg.sortie, sortie)

    def test_autotest_refuse_hors_livre_de_geoffroy(self):
        cfg, _ = X.cfg_avec_profil(namespace(pdf=self.treasury), [])
        with self.assertRaises(X.ErreurFatale):
            X.autotest(cfg)

    def test_profil_enregistre_dans_la_configuration(self):
        self.assertIn("profil", X.CLES_CONFIG)

    def test_memoriser_profil_ecrit_le_fichier(self):
        cfg, profil = X.cfg_avec_profil(namespace(pdf=self.treasury, sortie=os.path.join(self.tmp, "s")), [])
        X.memoriser_profil(cfg, profil)
        with open(os.path.join(cfg.sortie, "profil_utilise.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["nom"], "treasury_of_muslim_names")


@unittest.skipUnless(TEXTUAL, "textual introuvable")
class TestTUIProfil(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pdf = faire_pdf(self.tmp, "TREASURY_OF_MUSLIM_NAMES.pdf")
        self.chemin_cfg = os.path.join(self.tmp, "cfg.json")
        base = X.valeurs_effectives(self.chemin_cfg)
        base.update({"pdf": self.pdf, "sortie": os.path.join(self.tmp, "sortie"), "pages": "", "profil": "", "suivi": 0})
        X.ecrire_config(self.chemin_cfg, base)

    def tearDown(self):
        X.JOURNAL.fermer()
        X.reinitialiser_profil()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_la_tui_applique_le_profil_detecte_et_ses_pages(self):
        app = T.PrenomsApp(self.chemin_cfg, auto_test=False)
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause()
            self.assertIn("profil", {c[0] for c in T.CHAMPS})
            self.assertEqual(str(app.query_one("#s-profil", Select).value), "auto")
            self.assertIn("Profil : treasury_of_muslim_names", str(app.query_one("#carte-chemins", Static).content))
            cfg = app.cfg_courante()
            self.assertEqual(cfg.pages, list(range(1, N_PAGES + 1)))                 # pas 53-338 !
            self.assertNotEqual(cfg.prompt, X.Config().prompt)
            self.assertEqual(app.query_one("#p-table", DataTable).row_count, N_PAGES)

    async def test_changer_le_profil_dans_les_reglages(self):
        app = T.PrenomsApp(self.chemin_cfg, auto_test=False)
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause()
            app.query_one("#s-profil", Select).value = "usmani_islamic_names"
            app.enregistrer_reglages()
            await pilot.pause()
            self.assertEqual(app.reglages["profil"], "usmani_islamic_names")
            self.assertIn("Profil : usmani_islamic_names", str(app.query_one("#carte-chemins", Static).content))
            with open(self.chemin_cfg, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["profil"], "usmani_islamic_names")
            app.query_one("#s-profil", Select).value = "auto"
            app.enregistrer_reglages()
            self.assertEqual(app.reglages["profil"], "")

    async def test_pages_saisies_dans_l_onglet_extraction_priment(self):
        app = T.PrenomsApp(self.chemin_cfg, auto_test=False)
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause()
            app.query_one("#e-pages", Input).value = "2-4"
            self.assertEqual(app.cfg_courante().pages, [2, 3, 4])

    async def test_erreur_de_profil_signalee_sans_planter(self):
        app = T.PrenomsApp(self.chemin_cfg, auto_test=False)
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause()
            app.reglages["profil"] = "n_existe_pas"
            self.assertIsNone(app.cfg_ou_alerte())


if __name__ == "__main__":
    unittest.main()
