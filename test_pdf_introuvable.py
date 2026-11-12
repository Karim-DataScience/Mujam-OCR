"""Recherche du PDF (--profil) et message d'erreur explicite. python -m unittest test_pdf_introuvable"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import extraire_prenoms_llm as X

ABDUR = "A-Dictionary-of-Muslim-Names_by_Professor-Abdur-Rahman (1).pdf"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ancien = os.getcwd()
        for nom in (ABDUR, "dictionaryofmusl0000haki_1.pdf", "TREASURY OF MUSLIM NAMES.pdf", "IslamicNamesByShaykhImaranAshrafUsmani.pdf"):
            open(os.path.join(self.tmp, nom), "wb").close()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.ancien)
        X.reinitialiser_profil()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestTrouverPdf(Base):
    def test_profil_trouve_son_pdf_avec_espaces_et_parentheses(self):
        r = X.trouver_pdf("<pdf Abdur Rahman>", "abdur_rahman_dictionary_of_muslim_names")
        self.assertEqual(os.path.basename(r), ABDUR)

    def test_autres_profils(self):
        self.assertEqual(os.path.basename(X.trouver_pdf("", "hakim_dictionary_of_muslim_names")), "dictionaryofmusl0000haki_1.pdf")
        self.assertEqual(os.path.basename(X.trouver_pdf("", "treasury_of_muslim_names")), "TREASURY OF MUSLIM NAMES.pdf")

    def test_fichier_existant_prioritaire(self):
        self.assertEqual(X.trouver_pdf("TREASURY OF MUSLIM NAMES.pdf", "abdur_rahman_dictionary_of_muslim_names"), "TREASURY OF MUSLIM NAMES.pdf")

    def test_plusieurs_pdf_pour_un_profil_ne_choisit_pas(self):
        open(os.path.join(self.tmp, "abdur-autre-edition.pdf"), "wb").close()
        self.assertEqual(X.trouver_pdf("x.pdf", "abdur_rahman_dictionary_of_muslim_names"), "x.pdf")

    def test_profil_sans_pdf_ni_profil_connu(self):
        self.assertEqual(X.trouver_pdf("x.pdf", "haddadou_prenoms_amazighs"), "x.pdf")
        self.assertEqual(X.trouver_pdf("x.pdf", "profil_inexistant"), "x.pdf")
        self.assertEqual(X.trouver_pdf("x.pdf"), "x.pdf")                      # plusieurs PDF, aucun « prenom » : pas de devinette


class TestMessage(Base):
    def test_exemple_recopie(self):
        m = X.aide_pdf_introuvable("<pdf Abdur Rahman>")
        self.assertIn("exemple", m)
        self.assertIn(ABDUR, m)
        self.assertIn("abdur_rahman_dictionary_of_muslim_names", m)            # profil reconnu pour chaque PDF présent
        self.assertIn("--profil", m)

    def test_espaces_non_proteges(self):
        m = X.aide_pdf_introuvable("A-Dictionary-of-Muslim-Names_by_Professor-Abdur-Rahman")
        self.assertIn("GUILLEMETS", m)
        self.assertNotIn("exemple", m)

    def test_cli_erreur_explicite(self):
        err = io.StringIO()
        with mock.patch.object(X.sys, "argv", ["x", "<pdf Abdur Rahman>", "--completer-arabe", "--sortie", self.tmp]), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            X.main()
        self.assertIn("remplace-le", err.getvalue())

    def test_cli_profil_trouve_le_pdf(self):
        vus = {}

        def faux(cfg):
            vus["pdf"] = cfg.pdf
            return {"adoptes": 0, "refuses": 0, "pages": 0, "blocs_vides": 0, "non_lus": 0, "pages_douteuses": 0, "requetes": 0, "duree_s": 0}
        with mock.patch.object(X.sys, "argv", ["x", "--profil", "abdur_rahman_dictionary_of_muslim_names", "--completer-arabe", "--sortie", self.tmp]), \
                mock.patch.object(X, "completer_arabe", side_effect=faux), mock.patch.object(X, "run_pipeline", return_value=([], [], [])), \
                contextlib.suppress(SystemExit):
            X.main()
        self.assertEqual(os.path.basename(vus.get("pdf", "")), ABDUR)


class TestCacheAbsent(Base):
    def test_sans_cache_la_passe_s_arrete_et_n_exporte_rien(self):
        sortie = os.path.join(self.tmp, "sortie_vide")
        err = io.StringIO()
        with mock.patch.object(X.sys, "argv", ["x", "--profil", "abdur_rahman_dictionary_of_muslim_names", "--completer-arabe", "--sortie", sortie]), \
                mock.patch.object(X, "resoudre_modele") as modele, self.assertRaises(SystemExit) as e:
            X.main()
        self.assertEqual(e.exception.code, 2)
        modele.assert_not_called()                                              # LM Studio n'est même pas sollicité
        self.assertFalse(os.path.exists(os.path.join(sortie, "prenoms_arabes.csv")))
        self.assertFalse(os.path.exists(os.path.join(sortie, "passe_arabe_stats.json")))

    def test_stats_cache_absent(self):
        cfg = X.Config(sortie=os.path.join(self.tmp, "s"), pdf="x.pdf", pages=[7, 8])
        st = X.completer_arabe(cfg)
        self.assertTrue(st["cache_absent"])
        self.assertEqual(st["requetes"], 0)


if __name__ == "__main__":
    unittest.main()
