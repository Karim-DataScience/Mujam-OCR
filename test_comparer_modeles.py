"""Tests de comparer_modeles.py sans LM Studio : un exécuteur factice enregistre les commandes et fabrique les fichiers de résultats."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import comparer_modeles as C


class FauxExecuteur(C.Executeur):
    def __init__(self):
        super().__init__(simuler=False)

    def lancer(self, cmd, sortie_fichier=None, timeout=None):
        self.historique.append(cmd)
        if "--autotest" in cmd:
            sortie = cmd[cmd.index("--sortie") + 1]
            os.makedirs(os.path.join(sortie, "autotest"), exist_ok=True)
            with open(os.path.join(sortie, "autotest", "score.json"), "w", encoding="utf-8") as f:
                json.dump({"modele": cmd[cmd.index("--modele") + 1], "score": {"vedettes": "35/35", "parasites": 0, "genre": "35/35", "arabe_exact": "13/22"},
                           "duree_moy_s": 8.0, "tok_s_moy": 50.0}, f)
        if "--relire-arabe" in cmd:
            sortie = cmd[cmd.index("--sortie") + 1]
            with open(os.path.join(sortie, "relecture_stats.json"), "w", encoding="utf-8") as f:
                json.dump({"cibles": 10, "corrigees": 4, "restantes": 6, "jetons_reflexion": 1234, "duree_s": 99.0}, f)
        if cmd[1:2] == ["ps"]:
            return 0, "bonsai-27b gemma-4-12b-qat ornith-1.0-9b"
        if cmd[1:3] == ["ls", "--json"]:
            return 0, json.dumps([{"modelKey": "google/gemma-4-12b-qat", "type": "llm"}, {"modelKey": "prism-ml/bonsai-27b", "type": "llm"}])
        return 0, ""

    def attendre(self, s):
        pass


class TestComparerModeles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, "sortie_llm")
        os.makedirs(os.path.join(self.cache, "cache"))
        with open(os.path.join(self.cache, "cache", "p058.json"), "w", encoding="utf-8") as f:
            json.dump({"entrees": [{"prenoms": [{"arabe": "يَقُوب", "orthographes": ["Yaqoub"], "genre": "MASCULIN"}],
                                    "description": "Ya'qūb (Jacob) : fils d'Ishâq, petit-fils d'Ibrâhîm."}]}, f, ensure_ascii=False)
        self.pdf = os.path.join(self.tmp, "livre.pdf")
        open(self.pdf, "wb").close()

    def test_syntaxe_des_modeles(self):
        self.assertEqual(C.lire_modele("a/b", 16384), {"nom": "a/b", "ctx": 16384, "gpu": "max", "parallel": 1})
        self.assertEqual(C.lire_modele("a/b@8192#0.6", 16384)["ctx"], 8192)
        self.assertEqual(C.lire_modele("a/b@8192#0.6", 16384)["gpu"], "0.6")
        with self.assertRaises(ValueError):
            C.lire_modele("a b@x", 16384)

    def test_commande_de_chargement(self):
        cmd = C.commande_chargement("lms", C.lire_modele("m@4096#off", 1), "cle/m")
        self.assertEqual(cmd, ["lms", "load", "cle/m", "--context-length", "4096", "--parallel", "1", "--gpu", "off", "--identifier", "m", "-y"])

    def test_sequence_complete_et_rapport(self):
        ex = FauxExecuteur()
        argv = [self.pdf, "--modeles", "prism-ml/bonsai-27b,google/gemma-4-12b-qat@12288", "--cache", self.cache, "--chainer",
                "--sortie", os.path.join(self.tmp, "cmp"), "--lms", "lms"]
        with mock.patch.object(C, "serveur_pret", return_value=True):
            self.assertEqual(C.main(argv, ex), 0)
        verbes = [c[1] for c in ex.historique if c[0] == "lms"]
        # chaque chargement est précédé d'un déchargement complet ; tout est déchargé à la fin
        for i, v in enumerate(verbes):
            if v == "load":
                self.assertIn("unload", verbes[:i])
        loads = [c for c in ex.historique if c[0] == "lms" and c[1] == "load"]
        self.assertEqual(loads[1][loads[1].index("--context-length") + 1], "12288")
        self.assertEqual(verbes[-1], "unload")
        rapport = json.load(open(os.path.join(self.tmp, "cmp", "comparaison_modeles.json"), encoding="utf-8"))
        self.assertEqual(set(rapport["modeles"]), {"prism-ml/bonsai-27b", "google/gemma-4-12b-qat"})
        self.assertEqual(rapport["modeles"]["prism-ml/bonsai-27b"]["relecture"]["stats"]["corrigees"], 4)
        self.assertEqual(len(rapport["chaine"]), 2)
        self.assertEqual(rapport["chaine_avant"]["incoherentes"], 1)           # يَقُوب est incohérent avec Ya'qūb
        md = open(os.path.join(self.tmp, "cmp", "comparaison_modeles.md"), encoding="utf-8").read()
        self.assertIn("| prism-ml/bonsai-27b |", md)
        self.assertIn("35/35", md)
        self.assertIn("## Chaîne", md)
        with open(os.path.join(self.cache, "cache", "p058.json"), encoding="utf-8") as f:   # le cache source n'est jamais modifié
            self.assertEqual(json.load(f)["entrees"][0]["prenoms"][0]["arabe"], "يَقُوب")

    def test_echec_de_chargement_n_arrete_pas_les_autres(self):
        ex = FauxExecuteur()
        base = ex.lancer

        def lancer(cmd, sortie_fichier=None, timeout=None):
            if cmd[1:2] == ["load"] and "ornith" in cmd[2]:
                ex.historique.append(cmd)
                return 1, "mémoire insuffisante"
            return base(cmd, sortie_fichier, timeout)
        ex.lancer = lancer
        argv = [self.pdf, "--modeles", "ornith-1.0-9b,prism-ml/bonsai-27b", "--taches", "autotest", "--sortie", os.path.join(self.tmp, "cmp"), "--lms", "lms"]
        with mock.patch.object(C, "serveur_pret", return_value=True):
            C.main(argv, ex)
        rapport = json.load(open(os.path.join(self.tmp, "cmp", "comparaison_modeles.json"), encoding="utf-8"))
        self.assertFalse(rapport["modeles"]["ornith-1.0-9b"]["chargement"]["ok"])
        self.assertTrue(rapport["modeles"]["prism-ml/bonsai-27b"]["chargement"]["ok"])
        self.assertIn("ÉCHEC", open(os.path.join(self.tmp, "cmp", "comparaison_modeles.md"), encoding="utf-8").read())


class TestTrouverPdf(unittest.TestCase):
    def test_pdf_inexistant_remplace_par_le_pdf_du_dossier(self):
        import extraire_prenoms_llm as X
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        open(os.path.join(tmp, "lelivredesprenom0000geof.pdf"), "wb").close()
        ancien = os.getcwd()
        os.chdir(tmp)
        self.addCleanup(os.chdir, ancien)
        with mock.patch.object(X, "log"):
            self.assertTrue(X.trouver_pdf("livre.pdf").endswith("lelivredesprenom0000geof.pdf"))
            self.assertEqual(X.trouver_pdf("lelivredesprenom0000geof.pdf"), "lelivredesprenom0000geof.pdf")


if __name__ == "__main__":
    unittest.main()
