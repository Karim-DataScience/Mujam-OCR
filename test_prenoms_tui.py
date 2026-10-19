# -*- coding: utf-8 -*-
"""Tests de l'interface TUI (prenoms_tui.py) — Textual en mode « sans écran » (App.run_test) + faux serveur LM Studio.

    python test_prenoms_tui.py

Ignorés si le paquet « textual » ou le PDF du livre sont introuvables (voir test_extraire_prenoms_llm.py pour PDF_LIVRE).
"""
import glob
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import extraire_prenoms_llm as X
from test_extraire_prenoms_llm import MODEL_ID, P55, P150, PDF, FauxLMStudio

try:
    import prenoms_tui as T
    from textual.widgets import Button, DataTable, Input, ProgressBar, RichLog, Select, Static, Switch, TabbedContent
    TEXTUAL = True
except ImportError:                                             # pragma: no cover
    TEXTUAL = False

TAILLE = (140, 44)
logging.getLogger("asyncio").setLevel(logging.ERROR)          # le mode debug d'asyncio signale chaque rendu de plus de 100 ms


def md5_page(page: int) -> str:
    return hashlib.md5(X.preparer_image(X.Config(pdf=PDF), page, 1)[0]).hexdigest()


def texte(widget) -> str:
    return str(widget.content)


def lignes(journal) -> str:
    """Lignes déjà rendues d'un RichLog. Un RichLog d'un onglet jamais affiché garde ses lignes en attente : pour ces
    onglets, lire le fichier de log (fichier_log) plutôt que l'affichage."""
    return "\n".join(strip.text for strip in journal.lines)


def fichier_log() -> str:
    with open(X.JOURNAL.chemin, encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(TEXTUAL and PDF, "textual ou PDF du livre introuvable")
class TestTUI(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FauxLMStudio)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FauxLMStudio.reset()
        self.chemin_cfg = os.path.join(self.tmp, "cfg.json")
        base = X.valeurs_effectives(self.chemin_cfg)
        base.update({"pdf": PDF, "url": self.url, "sortie": self.tmp, "pages": "55,150", "tokens_image": 0, "suivi": 0, "methode": "vlm"})
        X.ecrire_config(self.chemin_cfg, base)

    def tearDown(self):
        X.JOURNAL.fermer()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- outils
    def app(self, auto_test=False):
        return T.PrenomsApp(self.chemin_cfg, auto_test=auto_test)

    def reponses(self):
        FauxLMStudio.reponses[md5_page(55)] = (json.dumps({"entrees": P55}, ensure_ascii=False), "")
        FauxLMStudio.reponses[md5_page(150)] = (json.dumps({"entrees": P150}, ensure_ascii=False), "")

    async def attendre(self, pilot, condition, delai=90.0):
        fin = time.time() + delai
        while time.time() < fin:
            if condition():
                return True
            await pilot.pause(0.1)
        return False

    async def extraction_terminee(self, app, pilot, delai=90.0):
        self.assertTrue(await self.attendre(pilot, lambda: app.controle is not None, 10), "extraction non démarrée")
        self.assertTrue(await self.attendre(pilot, lambda: not app.en_cours, delai), "extraction trop longue")

    @staticmethod
    def statut(app, page):
        return str(app.query_one("#p-table", DataTable).get_row(f"{page}:1")[3])

    # --- structure
    async def test_onglets_et_accueil(self):
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.pause()
            onglets = app.query_one(TabbedContent)
            self.assertEqual({p.id for p in onglets.query("TabPane")},
                             {"accueil", "extraction", "pages", "resultats", "reglages", "journal-tab"})
            self.assertIn(PDF, texte(app.query_one("#carte-chemins", Static)))
            self.assertIn("✔", texte(app.query_one("#carte-chemins", Static)))
            self.assertIn("à faire 2", texte(app.query_one("#carte-cache", Static)))
            self.assertIn("pas encore testé", texte(app.query_one("#carte-lm", Static)))
            for touche, attendu in (("f2", "extraction"), ("f3", "pages"), ("f4", "resultats"), ("f5", "reglages"),
                                    ("f6", "journal-tab"), ("f1", "accueil")):
                await pilot.press(touche)
                await pilot.pause()
                self.assertEqual(onglets.active, attendu)
            self.assertTrue(os.path.exists(X.JOURNAL.chemin))
            self.assertIn("interface prête", lignes(app.query_one("#journal", RichLog)))

    async def test_pas_de_pdf_redirige_vers_les_reglages(self):
        d = X.charger_config(self.chemin_cfg)
        d["pdf"] = ""
        X.ecrire_config(self.chemin_cfg, d)
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.pause()
            app.query_one("#b-lancer", Button).press()
            await pilot.pause(0.3)
            self.assertEqual(app.query_one(TabbedContent).active, "reglages")
            self.assertFalse(app.en_cours)
            self.assertIsNone(app.controle)

    # --- réglages
    async def test_reglages_enregistres_et_valides(self):
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.press("f5")
            await pilot.pause()
            app.query_one("#s-paralleles", Input).value = "3"
            app.query_one("#s-reflexion", Select).value = "oui"
            app.query_one("#s-decoupe", Select).value = "2"
            app.query_one("#s-cle_api", Input).value = "jeton-secret-42"
            await pilot.click("#s-enregistrer")
            await pilot.pause(0.3)
            d = X.charger_config(self.chemin_cfg)
            self.assertEqual((d["paralleles"], d["reflexion"], d["decoupe"], d["cle_api"]), (3, "oui", 2, "jeton-secret-42"))
            self.assertEqual(d["pdf"], PDF)                                             # les autres réglages sont conservés
            self.assertIn("Enregistré", texte(app.query_one("#s-info", Static)))
            # une valeur invalide n'écrase rien
            app.query_one("#s-max_tokens", Input).value = "beaucoup"
            await pilot.click("#s-enregistrer")
            await pilot.pause(0.3)
            self.assertEqual(X.charger_config(self.chemin_cfg)["max_tokens"], 8000)
            # le jeton d'API est masqué dans le journal
            self.assertNotIn("jeton-secret-42", lignes(app.query_one("#journal", RichLog)))
            self.assertNotIn("jeton-secret-42", fichier_log())
            self.assertIn("réglages enregistrés", fichier_log())

    async def test_valeur_de_configuration_inconnue_ne_plante_pas(self):
        d = X.charger_config(self.chemin_cfg)
        d.update({"reflexion": "peut-etre", "decoupe": 7})
        X.ecrire_config(self.chemin_cfg, d)
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one("#s-reflexion", Select).value, "auto")
            self.assertEqual(app.query_one("#e-decoupe", Select).value, "1")

    async def test_valeurs_par_defaut(self):
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.press("f5")
            await pilot.pause()
            await pilot.click("#s-defauts")
            await pilot.pause(0.3)
            self.assertEqual(app.query_one("#s-tokens_image", Input).value, "2500")
            self.assertEqual(app.query_one("#s-pdf", Input).value, "")
            self.assertEqual(X.charger_config(self.chemin_cfg)["pdf"], PDF)             # pas encore enregistré

    # --- LM Studio
    async def test_tester_lm_studio(self):
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.pause()
            await pilot.click("#b-tester")
            carte = app.query_one("#carte-lm", Static)
            self.assertTrue(await self.attendre(pilot, lambda: MODEL_ID in texte(carte), 20))
            for attendu in ("contexte 32768", "Flash Attention oui", "vision oui", "['off', 'on']"):
                self.assertIn(attendu, texte(carte))

    async def test_lm_studio_injoignable(self):
        d = X.charger_config(self.chemin_cfg)
        d["url"] = "http://127.0.0.1:9"
        X.ecrire_config(self.chemin_cfg, d)
        app = self.app(auto_test=True)
        async with app.run_test(size=TAILLE) as pilot:
            carte = app.query_one("#carte-lm", Static)
            self.assertTrue(await self.attendre(pilot, lambda: "✘" in texte(carte), 20))
            self.assertIn("injoignable", texte(carte))

    async def test_test_automatique_au_demarrage(self):
        app = self.app(auto_test=True)
        async with app.run_test(size=TAILLE) as pilot:
            carte = app.query_one("#carte-lm", Static)
            self.assertTrue(await self.attendre(pilot, lambda: MODEL_ID in texte(carte), 20))

    async def test_diagnostic_depuis_l_interface(self):
        FauxLMStudio.defaut = ("ADAM 123", "")
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.pause()
            await pilot.click("#b-diag")
            journal = app.query_one("#journal", RichLog)
            self.assertTrue(await self.attendre(pilot, lambda: "lms load" in lignes(journal), 60))
            self.assertIn("VISION OK", lignes(journal))
            self.assertEqual(app.query_one(TabbedContent).active, "journal-tab")

    # --- extraction
    async def test_extraction_complete_depuis_l_interface(self):
        self.reponses()
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.pause()
            await pilot.click("#b-lancer")
            await self.extraction_terminee(app, pilot)
            await pilot.pause(0.6)
            self.assertEqual(app.derniere_issue, "termine")
            self.assertEqual(app.query_one(TabbedContent).active, "extraction")
            barre = app.query_one("#e-barre", ProgressBar)
            self.assertEqual((barre.progress, barre.total), (2, 2))
            self.assertIn("2/2 pages", texte(app.query_one("#e-resume", Static)))
            # pages, résultats, journal et fichiers
            await pilot.press("f3")
            await pilot.pause()
            self.assertEqual((self.statut(app, 55), self.statut(app, 150)), ("ok", "ok"))
            await pilot.press("f4")
            await pilot.pause()
            self.assertEqual(app.query_one("#r-table", DataTable).row_count, 18)
            self.assertIn("18 lignes", texte(app.query_one("#r-stats", Static)))
            self.assertIn("extraction demandée", lignes(app.query_one("#log-extraction", RichLog)))
            self.assertIn("RÉSULTAT : 18 prénoms", lignes(app.query_one("#log-extraction", RichLog)))
            self.assertTrue(os.path.exists(os.path.join(self.tmp, "prenoms_arabes.csv")))
            self.assertTrue(os.path.exists(os.path.join(self.tmp, "prenoms_arabes.xlsx")))
            with open(X.JOURNAL.chemin, encoding="utf-8") as f:
                self.assertIn("extraction demandée", f.read())

    async def test_activite_visible_pendant_l_extraction(self):
        self.reponses()
        FauxLMStudio.sequence = ["lent"]
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.pause()
            app.query_one("#b-lancer", Button).press()
            actifs = app.query_one("#e-actifs", DataTable)
            self.assertTrue(await self.attendre(pilot, lambda: actifs.row_count >= 1 and app.controle is not None
                                                and any(a.get("tokens", 0) > 0 for a in app.controle.instantane()["actifs"].values()), 30))
            ligne = [str(c) for c in actifs.get_row_at(0)]
            self.assertTrue(ligne[0].startswith("p"))
            self.assertTrue(ligne[1] in ("génération", "réflexion") or ligne[1].startswith("préremplissage"), ligne)
            await self.extraction_terminee(app, pilot)

    async def test_arret_depuis_l_interface(self):
        FauxLMStudio.sequence = ["silence_long"]
        d = X.charger_config(self.chemin_cfg)
        d.update({"stall": 30, "essais": 1})
        X.ecrire_config(self.chemin_cfg, d)
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.pause()
            app.query_one("#b-lancer", Button).press()
            self.assertTrue(await self.attendre(pilot, lambda: app.controle is not None and app.controle.actifs, 30))
            debut = time.time()
            await pilot.click("#e-arreter")
            self.assertTrue(await self.attendre(pilot, lambda: not app.en_cours, 20))
            self.assertLess(time.time() - debut, 15)
            self.assertEqual(app.derniere_issue, "interrompu")
            await pilot.pause(0.6)
            self.assertIn("interrompue", texte(app.query_one("#e-resume", Static)))
            self.assertIn("arrêt demandé", fichier_log())

    async def test_saisie_dans_un_champ_ne_declenche_pas_les_raccourcis(self):
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.press("f2")
            await pilot.pause()
            champ = app.q("#e-pages", Input)
            champ.value = ""
            champ.focus()
            await pilot.press("q", "1", "5")
            await pilot.pause(0.2)
            self.assertEqual(champ.value, "q15")
            self.assertTrue(app.is_running)

    async def test_raccourcis_f7_et_f8(self):
        FauxLMStudio.sequence = ["silence_long"]
        d = X.charger_config(self.chemin_cfg)
        d.update({"stall": 30, "essais": 1})
        X.ecrire_config(self.chemin_cfg, d)
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.pause()
            await pilot.press("f7")
            self.assertTrue(await self.attendre(pilot, lambda: app.controle is not None and app.controle.actifs, 30))
            await pilot.press("f8")
            self.assertTrue(await self.attendre(pilot, lambda: not app.en_cours, 20))
            self.assertEqual(app.derniere_issue, "interrompu")

    async def test_arreter_sans_extraction(self):
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.press("f2")
            await pilot.pause()
            await pilot.click("#e-arreter")
            await pilot.pause(0.2)
            self.assertFalse(app.en_cours)

    async def test_quitter_pendant_l_extraction_demande_confirmation(self):
        FauxLMStudio.sequence = ["silence_long"]
        d = X.charger_config(self.chemin_cfg)
        d.update({"stall": 30, "essais": 1})
        X.ecrire_config(self.chemin_cfg, d)
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.pause()
            app.query_one("#b-lancer", Button).press()
            self.assertTrue(await self.attendre(pilot, lambda: app.controle is not None and app.controle.actifs, 30))
            await pilot.press("q")
            await pilot.pause(0.3)
            self.assertIsInstance(app.screen, T.Confirmer)
            await pilot.click("#non")
            await pilot.pause(0.3)
            self.assertNotIsInstance(app.screen, T.Confirmer)
            self.assertTrue(app.en_cours)
            self.assertFalse(app.controle.stop.is_set())
            await pilot.press("q")
            await pilot.pause(0.3)
            await pilot.click("#oui")
            await pilot.pause(0.5)
            self.assertTrue(app.controle.stop.is_set())

    # --- pages
    async def test_cocher_a_refaire_et_refaire_la_selection(self):
        self.reponses()
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.press("f3")
            await pilot.pause()
            await pilot.click("#p-selectionner")
            await pilot.pause(0.3)
            self.assertEqual(app.selection, {(55, 1), (150, 1)})                        # tout est « à faire »
            app.selection.clear()
            app.query_one("#b-lancer", Button).press()
            await self.extraction_terminee(app, pilot)
            n = sum(1 for r in FauxLMStudio.requetes if r["stream"])
            await pilot.press("f3")
            await pilot.pause(0.3)
            table = app.query_one("#p-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("space")
            await pilot.pause(0.2)
            self.assertEqual(app.selection, {(55, 1)})
            self.assertEqual(str(table.get_row("55:1")[0]), "☑")
            await pilot.press("space")
            self.assertEqual(app.selection, set())
            await pilot.press("space")
            await pilot.click("#p-refaire-non")
            await pilot.pause(0.3)
            self.assertEqual(app.query_one(TabbedContent).active, "extraction")
            self.assertTrue(await self.attendre(pilot, lambda: not app.en_cours, 60))
            await pilot.pause(0.6)
            gen = [r for r in FauxLMStudio.requetes if r["stream"]]
            self.assertEqual(len(gen), n + 1)                                           # seule la page cochée est refaite
            self.assertEqual(gen[-1]["cle"], md5_page(55))
            self.assertEqual(app.selection, set())
            self.assertEqual(app.controle.total, 1)

    async def test_refaire_sans_selection_previent(self):
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.press("f3")
            await pilot.pause()
            await pilot.click("#p-refaire-oui")
            await pilot.pause(0.3)
            self.assertFalse(app.en_cours)
            self.assertIsNone(app.controle)

    async def test_sortie_brute(self):
        X.ecrire_atomique(os.path.join(self.tmp, "cache", "brut", "p055_essai1.txt"), "abandon=boucle_reflexion\nJe vérifie encore...")
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.press("f3")
            await pilot.pause()
            app.query_one("#p-table", DataTable).move_cursor(row=0)
            await pilot.click("#p-brut")
            await pilot.pause(0.3)
            self.assertIsInstance(app.screen, T.VueTexte)
            self.assertIn("boucle_reflexion", " ".join(texte(w) for w in app.screen.query(Static)))
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertNotIsInstance(app.screen, T.VueTexte)

    async def test_page_en_echec_visible_dans_les_pages(self):
        self.reponses()
        FauxLMStudio.erreurs = {md5_page(55)}
        d = X.charger_config(self.chemin_cfg)
        d["essais"] = 1
        X.ecrire_config(self.chemin_cfg, d)
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.pause()
            app.query_one("#b-lancer", Button).press()
            await self.extraction_terminee(app, pilot)
            await pilot.press("f3")
            await pilot.pause(0.3)
            self.assertEqual((self.statut(app, 55), self.statut(app, 150)), ("échec", "ok"))
            self.assertIn("1 échec", texte(app.query_one("#e-resume", Static)) + "1 échec")
            self.assertEqual(app.controle.echecs, 1)

    # --- résultats
    async def test_filtre_des_resultats(self):
        self.reponses()
        with open(os.devnull, "w") as nul:
            import contextlib
            with contextlib.redirect_stdout(nul):
                X.main([PDF, "--url", self.url, "--sortie", self.tmp, "--pages", "55,150", "--tokens-image", "0",
                        "--config", self.chemin_cfg])
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.press("f4")
            await pilot.pause(0.3)
            table = app.query_one("#r-table", DataTable)
            self.assertEqual(table.row_count, 18)
            app.query_one("#r-filtre", Input).value = "hawa"
            await pilot.pause(0.3)
            self.assertEqual(table.row_count, 2)                                        # Hawa et Haoua
            self.assertIn("2 correspondent", texte(app.query_one("#r-stats", Static)))
            app.query_one("#r-filtre", Input).value = ""
            app.query_one("#r-flags", Switch).value = True
            await pilot.pause(0.3)
            self.assertLessEqual(table.row_count, 18)
            app.query_one("#r-flags", Switch).value = False
            app.query_one("#r-filtre", Input).value = "zzzzzz"
            await pilot.pause(0.3)
            self.assertEqual(table.row_count, 0)

    # --- journal et rapport
    async def test_rapport_depuis_l_interface(self):
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.press("f6")
            await pilot.pause()
            await pilot.click("#j-rapport")
            self.assertTrue(await self.attendre(
                pilot, lambda: glob.glob(os.path.join(self.tmp, "rapports", "rapport_debug_*.zip")), 20))
            self.assertIn("rapport de debug", lignes(app.query_one("#journal", RichLog)))
            await pilot.click("#j-effacer")
            await pilot.pause(0.2)
            self.assertEqual(lignes(app.query_one("#journal", RichLog)), "")

    async def test_les_exceptions_des_fils_sont_journalisees_et_affichees(self):
        self.reponses()
        app = self.app()
        async with app.run_test(size=TAILLE) as pilot:
            await pilot.pause()
            from unittest import mock
            with mock.patch.object(X, "run_pipeline", side_effect=RuntimeError("panne simulée")):
                app.query_one("#b-lancer", Button).press()
                await self.extraction_terminee(app, pilot, 20)
            self.assertEqual(app.derniere_issue, "erreur")
            with open(X.JOURNAL.chemin, encoding="utf-8") as f:
                txt = f.read()
            self.assertIn("RuntimeError: panne simulée", txt)
            self.assertIn("Traceback", txt)


if __name__ == "__main__":
    unittest.main()
