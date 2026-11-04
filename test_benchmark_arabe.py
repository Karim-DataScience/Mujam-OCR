"""Tests de benchmark_arabe.py (aucun modèle réel : faux serveur LM Studio, faux lecteurs). python -m unittest test_benchmark_arabe"""
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

import benchmark_arabe as B


class Faux(B.Lecteur):
    def __init__(self, nom, fonction):
        self.nom, self.f = nom, fonction
        self.k = 0

    def lire_crop(self, img):
        return self.f(self, img)


class FauxLM(BaseHTTPRequestHandler):
    requetes: list = []
    reponse = "عالي"

    def log_message(self, *a):
        pass

    def do_GET(self):
        corps = json.dumps({"data": [{"id": "faux/modele"}, {"id": "autre/modele"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(corps)

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        FauxLM.requetes.append(json.loads(self.rfile.read(n)))
        corps = json.dumps({"choices": [{"message": {"content": "<think>je réfléchis</think>" + FauxLM.reponse}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(corps)


class TestMetriques(unittest.TestCase):
    def test_normalisation(self):
        self.assertEqual(B.normaliser("أَشْهَل"), "اشهل")                    # voyelles retirées, alef unifié
        self.assertEqual(B.normaliser("(الْكَبِيرُ) Al-Kabir : 12"), "الكبير")   # latin, chiffres, ponctuation retirés
        self.assertEqual(B.normaliser("الْمُبْدِئُ"), B.normaliser("المبدي"))   # hamza sur support = ya
        self.assertEqual(B.normaliser("أ", "strict"), "أ")                   # strict garde la forme d'alef

    def test_cer(self):
        self.assertEqual(B.cer("عبد الاحد", "عبدالاحد"), 0.0)               # espaces ignorés
        self.assertEqual(B.cer("عالي", ""), 1.0)
        self.assertAlmostEqual(B.cer("عالي", "عالم"), 0.25)
        self.assertEqual(B.cer("أَحْمَد", "احمد"), 0.0)

    def test_apparier_page(self):
        att = ["عالي", "عالم", "عامل"]
        p = B.apparier_page(att, ["عالي", "عالم", "عامل"])
        self.assertEqual((p["rappel"], p["precision"]), (1.0, 1.0))
        p = B.apparier_page(att, ["عالي", "bruit", "عالمx", "ظظظظظظ"][:1] + ["عالم", "ظظظظظظ"])
        self.assertAlmostEqual(p["rappel"], 2 / 3)
        self.assertAlmostEqual(p["precision"], 2 / 3)
        p = B.apparier_page(["عالي", "عالي"], ["عالي"])                       # une ligne ne sert qu'une fois
        self.assertEqual(p["rappel"], 0.5)
        self.assertEqual(B.apparier_page(att, [])["rappel"], 0.0)


class TestVerite(unittest.TestCase):
    def test_integrite(self):
        pages = B.charger_verite(B.VERITE_DEFAUT)
        self.assertEqual(sum(len(p["items"]) for p in pages), 64)
        for p in pages:
            with Image.open(p["chemin"]) as img:
                taille = img.size
            for it in p["items"]:
                x0, y0, x1, y1 = it["bbox"]
                self.assertTrue(0 <= x0 < x1 <= taille[0] and 0 <= y0 < y1 <= taille[1], (p["id"], it))
                self.assertTrue(B.normaliser(it["texte"], "strict"), it)
                self.assertFalse(re.search("[A-Za-z0-9]", it["texte"]), it)


class TestEvaluation(unittest.TestCase):
    def setUp(self):
        self.pages = B.charger_verite(B.VERITE_DEFAUT)[:2]
        self.verite = {(p["id"], i + 1): it["texte"] for p in self.pages for i, it in enumerate(p["items"])}

    def lecteur_parfait(self):
        att = iter(self.verite.values())
        return Faux("parfait", lambda s, img: next(att))

    def test_lecteur_parfait_et_vide(self):
        r = B.evaluer(self.lecteur_parfait(), self.pages, False)
        s = B.synthese(r)
        self.assertEqual((s["cer_leger"], s["exact"]), (0.0, 1.0))
        r = B.evaluer(Faux("vide", lambda s, img: ""), self.pages, False)
        self.assertEqual(B.synthese(r)["cer_leger"], 1.0)

    def test_erreur_du_lecteur_comptee_sans_planter(self):
        def boum(s, img):
            raise RuntimeError("panne")
        r = B.evaluer(Faux("boum", boum), self.pages, False, log=lambda *a: None)
        self.assertEqual(r["erreurs"], len(self.verite))
        self.assertEqual(B.synthese(r)["cer_leger"], 1.0)

    def test_combinaisons(self):
        a = B.evaluer(self.lecteur_parfait(), self.pages, False)
        # B se trompe sur toutes les zones impaires : accord seulement sur les paires
        vals = list(self.verite.values())
        it = iter(range(len(vals)))
        b = B.evaluer(Faux("b", lambda s, img: (lambda k: vals[k] if k % 2 == 0 else "ظظظظ")(next(it))), self.pages, False)
        c = B.combinaisons({"a": a, "b": b})
        acc = c["accords"][0]
        self.assertAlmostEqual(acc["couverture"], (len(vals) + 1) // 2 / len(vals), places=2)
        self.assertEqual(acc["cer_si_accord"], 0.0)                          # quand ils s'accordent, A est exact
        self.assertEqual(c["oracle"], 0.0)

    def test_rapport_contient_les_lecteurs(self):
        a = B.evaluer(Faux("x", lambda s, img: ""), self.pages, False)
        txt = B.rapport({"x": B.synthese(a)}, {"accords": [], "oracle": None}, 10, self.pages)
        self.assertIn("`x`", txt)
        self.assertIn("100.0 %", txt)


class TestLMStudio(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FauxLM)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/v1"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        FauxLM.requetes.clear()
        self.img = Image.new("RGB", (60, 20), "white")

    def test_disponibilite(self):
        self.assertTrue(B.LecteurLMStudio("lm:faux/modele", self.url, None).disponible()[0])
        ok, pourquoi = B.LecteurLMStudio("lm:absent/modele", self.url, None).disponible()
        self.assertFalse(ok)
        self.assertIn("faux/modele", pourquoi)
        self.assertFalse(B.LecteurLMStudio("lm:faux/modele", "http://127.0.0.1:9/v1", None).disponible()[0])

    def test_requete_et_reponse(self):
        lec = B.LecteurLMStudio("lm:faux/modele", self.url, None, hauteur=128)
        self.assertEqual(lec.lire_crop(self.img), "عالي")                    # <think> retiré
        q = FauxLM.requetes[-1]
        self.assertEqual((q["model"], q["temperature"]), ("faux/modele", 0))
        contenu = q["messages"][0]["content"]
        self.assertTrue(contenu[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn("arabe", contenu[0]["text"])

    def test_mode_indice_envoie_la_lecture_tesseract(self):
        tess = Faux("t", lambda s, img: "(عالم) 12")
        lec = B.LecteurLMStudio("lm:faux/modele:indice", self.url, tess)
        self.assertEqual(lec.modele, "faux/modele")
        lec.lire_crop(self.img)
        self.assertIn("« عالم »", FauxLM.requetes[-1]["messages"][0]["content"][0]["text"])

    def test_lire_page_rend_les_lignes_arabes(self):
        FauxLM.reponse = "عالي\nHello\nعالم"
        try:
            self.assertEqual(B.LecteurLMStudio("lm:faux/modele", self.url, None).lire_page(self.img), ["عالي", "عالم"])
        finally:
            FauxLM.reponse = "عالي"

    def test_agrandissement(self):
        lec = B.LecteurLMStudio("lm:faux/modele", self.url, None, hauteur=100)
        self.assertEqual(lec._agrandir(self.img).height, 100)
        self.assertEqual(lec._agrandir(Image.new("RGB", (60, 200))).height, 200)

    def test_cli_lister_et_candidat_inconnu(self):
        self.assertEqual(B.main(["--lister-lmstudio", "--url", self.url]), 0)
        self.assertEqual(B.main(["--candidats", "n_importe_quoi", "--sortie", tempfile.mkdtemp()]), 2)

    def test_cli_complet_sur_faux_serveur(self):
        sortie = tempfile.mkdtemp()
        try:
            self.assertEqual(B.main(["--candidats", "lm:faux/modele", "--url", self.url, "--sortie", sortie]), 0)
            with open(os.path.join(sortie, "resultats_benchmark.json"), encoding="utf-8") as f:
                self.assertIn("lm:faux/modele", json.load(f)["synthese"])
            self.assertTrue(os.path.exists(os.path.join(sortie, "rapport_benchmark.md")))
        finally:
            shutil.rmtree(sortie, ignore_errors=True)


class TestTesseract(unittest.TestCase):
    def test_options(self):
        t = B.LecteurTesseract("tess:ara:best:psm13:x3:bin", {"best": "/d"})
        self.assertEqual((t.lang, t.dossier, t.psm, t.echelle, t.bin), ("ara", "/d", 13, 3, True))
        with self.assertRaises(ValueError):
            B.LecteurTesseract("tess:inconnue", {})

    def test_grille(self):
        import argparse
        a = argparse.Namespace(tessdata=["fast=/a", "best=/b"], tesseract_cmd="", indice_tess="", url="", hauteur_vlm=128)
        noms = [l.nom for l in B.construire("grille:tess", a)]
        self.assertEqual(len(noms), 16)
        self.assertIn("tess:ara:fast:psm7:x3:bin", noms)

    def test_lecture_reelle_si_langue_arabe_installee(self):
        t = B.LecteurTesseract("tess:ara:psm7:x3:bin", {})
        ok, _ = t.disponible()
        if not ok:
            self.skipTest("langue arabe de Tesseract non installée")
        pages = B.charger_verite(B.VERITE_DEFAUT)
        p = next(p for p in pages if p["id"] == "img5_abdur")
        with Image.open(p["chemin"]) as f_img:
            img = f_img.convert("RGB")
        c = [B.cer(it["texte"], t.lire_crop(B.recadrer(img, it["bbox"]))) for it in p["items"]]
        self.assertLess(sum(c) / len(c), 0.35)


if __name__ == "__main__":
    unittest.main()
