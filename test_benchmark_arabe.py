"""Tests de benchmark_arabe.py (aucun modèle réel : faux serveur LM Studio, faux lecteurs). python -m unittest test_benchmark_arabe"""
import io
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
    vides_avant: int = 0                  # nombre de réponses vides à rendre avant de répondre normalement

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/v0/models"):
            corps = json.dumps({"data": [{"id": "faux/modele", "type": "vlm", "state": "loaded"}, {"id": "autre/modele-vl", "type": "vlm", "state": "not-loaded"},
                                         {"id": "texte/seul", "type": "llm", "state": "not-loaded"}]}).encode()
        else:
            corps = json.dumps({"data": [{"id": "faux/modele"}, {"id": "autre/modele-vl"}, {"id": "texte/seul"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(corps)

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        FauxLM.requetes.append(json.loads(self.rfile.read(n)))
        if FauxLM.vides_avant > 0:
            FauxLM.vides_avant -= 1
            corps = json.dumps({"choices": [{"message": {"content": "", "reasoning_content": "je réfléchis…"}, "finish_reason": "length"}],
                                "usage": {"completion_tokens": 300}}).encode()
        else:
            corps = json.dumps({"choices": [{"message": {"content": "<think>je réfléchis</think>" + FauxLM.reponse}, "finish_reason": "stop"}],
                                "usage": {"completion_tokens": 12}}).encode()
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
        self.assertEqual(r["erreurs"], 3)                                   # abandon après 3 erreurs de suite sans succès
        self.assertIn("3 erreurs de suite", r["abandon"])

    def test_erreurs_isolees_ne_font_pas_abandonner(self):
        def une_sur_deux(s, img):
            s.k += 1
            if s.k % 2:
                raise RuntimeError("panne passagère")
            return "عالي"
        r = B.evaluer(Faux("alt", une_sur_deux), self.pages, False, log=lambda *a: None)
        self.assertEqual(r["abandon"], "")
        self.assertEqual(len(r["crops"]), len(self.verite))

    def test_echantillon_et_budget(self):
        r = B.evaluer(Faux("v", lambda s, img: ""), self.pages, False, pas=2)
        self.assertEqual(len(r["crops"]), sum((len(p["items"]) + 1) // 2 for p in self.pages))
        r = B.evaluer(Faux("v", lambda s, img: ""), self.pages, False, budget_s=-1)
        self.assertIn("budget", r["abandon"])

    def test_synthese_separe_les_vides_des_erreurs_de_lecture(self):
        vals = iter(list(self.verite.values()))
        n = iter(range(len(self.verite)))
        r = B.evaluer(Faux("m", lambda s, img: ("" if next(n) % 4 == 0 else next(vals))), self.pages, False)
        s = B.synthese(r)
        self.assertAlmostEqual(s["vides"], sum(1 for k in range(len(self.verite)) if k % 4 == 0) / len(self.verite), places=2)
        self.assertLess(s["cer_hors_vides"], s["cer_leger"])
        self.assertTrue(r["crops"][0]["vide"])

    def test_vote_majoritaire(self):
        vals = list(self.verite.values())
        def lecteur(nom, faux):
            it = iter(range(len(vals)))
            return B.evaluer(Faux(nom, lambda s, img: (lambda k: "ظظظظظ" if k in faux else vals[k])(next(it))), self.pages, False)
        res = {"a": lecteur("a", {0, 1}), "b": lecteur("b", {1, 2}), "c": lecteur("c", {1, 3})}
        c = B.combinaisons(res)
        v = c["votes"][0]
        # zone 1 : les trois se trompent de la même façon -> majorité fausse ; zones 0, 2, 3 : majorité juste
        self.assertAlmostEqual(v["couverture"], 1.0)
        self.assertAlmostEqual(v["cer_si_majorite"], B.cer(vals[1], "ظظظظظ") / len(vals), places=6)

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
        FauxLM.vides_avant = 0
        self.img = Image.new("RGB", (60, 20), "white")

    def test_disponibilite(self):
        self.assertTrue(B.LecteurLMStudio("lm:faux/modele", self.url, None).disponible()[0])
        ok, pourquoi = B.LecteurLMStudio("lm:absent/modele", self.url, None).disponible()
        self.assertFalse(ok)
        self.assertIn("--lister-lmstudio", pourquoi)
        self.assertFalse(B.LecteurLMStudio("lm:faux/modele", "http://127.0.0.1:9/v1", None).disponible()[0])

    def test_requete_et_reponse(self):
        lec = B.LecteurLMStudio("lm:faux/modele", self.url, None, hauteur=128)
        self.assertEqual(lec.lire_crop(self.img), "عالي")                    # <think> retiré
        q = FauxLM.requetes[-1]
        self.assertEqual((q["model"], q["temperature"]), ("faux/modele", 0))
        contenu = q["messages"][0]["content"]
        self.assertTrue(contenu[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn("arabe", contenu[0]["text"])
        self.assertEqual(lec.derniere["finish"], "stop")

    def test_reponse_vide_relancee_avec_plus_de_jetons(self):
        FauxLM.vides_avant = 1                                               # 1re réponse vide (budget épuisé par le raisonnement)
        lec = B.LecteurLMStudio("lm:faux/modele", self.url, None, max_tokens=100)
        self.assertEqual(lec.lire_crop(self.img), "عالي")
        self.assertEqual([q["max_tokens"] for q in FauxLM.requetes], [100, 400])
        self.assertEqual((lec.derniere["relance"], lec.derniere["finish_initial"]), (1, "length"))

    def test_reponse_vide_persistante_comptee_vide(self):
        FauxLM.vides_avant = 5
        lec = B.LecteurLMStudio("lm:faux/modele", self.url, None, relances=1)
        self.assertEqual(lec.lire_crop(self.img), "")
        self.assertEqual(len(FauxLM.requetes), 2)

    def test_mode_indice_envoie_la_lecture_de_l_ocr(self):
        tess = Faux("t", lambda s, img: "(عالم) 12")
        lec = B.LecteurLMStudio("lm:faux/modele:indice", self.url, tess)
        self.assertEqual((lec.modele, lec.source_indice), ("faux/modele", "tess"))
        lec.lire_crop(self.img)
        self.assertIn("« عالم »", FauxLM.requetes[-1]["messages"][0]["content"][0]["text"])
        self.assertEqual(B.LecteurLMStudio("lm:faux/modele:indice=easyocr", self.url, tess).source_indice, "easyocr")

    def test_lire_page_rend_les_lignes_arabes(self):
        FauxLM.reponse = "عالي\nHello\nعالم"
        try:
            self.assertEqual(B.LecteurLMStudio("lm:faux/modele", self.url, None).lire_page(self.img), ["عالي", "عالم"])
        finally:
            FauxLM.reponse = "عالي"

    def test_echauffement_hors_mesure(self):
        lec = B.LecteurLMStudio("lm:faux/modele", self.url, None)
        lec.echauffer()
        self.assertEqual(FauxLM.requetes[-1]["max_tokens"], 8)

    def test_agrandissement(self):
        lec = B.LecteurLMStudio("lm:faux/modele", self.url, None, hauteur=100)
        self.assertEqual(lec._agrandir(self.img).height, 100)
        self.assertEqual(lec._agrandir(Image.new("RGB", (60, 200))).height, 200)

    def test_decouverte_des_modeles_de_vision(self):
        self.assertEqual(B.choisir_vlm(self.url, [], []), ["autre/modele-vl", "faux/modele"])         # « texte/seul » (llm) exclu
        self.assertEqual(B.choisir_vlm(self.url, ["vl"], []), ["autre/modele-vl"])
        self.assertEqual(B.choisir_vlm(self.url, [], ["faux"]), ["autre/modele-vl"])
        self.assertEqual({m["id"]: m["type"] for m in B.modeles_lmstudio(self.url)}["texte/seul"], "llm")

    def test_cli_lister_et_candidat_inconnu(self):
        self.assertEqual(B.main(["--lister-lmstudio", "--url", self.url]), 0)
        self.assertEqual(B.main(["--candidats", "n_importe_quoi", "--sortie", tempfile.mkdtemp()]), 2)

    def test_marqueur_a_remplacer_refuse(self):
        import argparse
        a = argparse.Namespace(tessdata=[], tesseract_cmd="", indice_tess="", url=self.url, hauteur_vlm=128, timeout=10, max_tokens=100, relances=0)
        with self.assertRaises(ValueError) as e:
            B.construire("lm:<id Qwen3-VL>", a)
        self.assertIn("--lister-lmstudio", str(e.exception))

    def test_cli_complet_vlm_reprise_et_sauvegarde(self):
        sortie = tempfile.mkdtemp()
        try:
            base = ["--url", self.url, "--sortie", sortie, "--echantillon", "4", "--sans-page"]
            self.assertEqual(B.main(["--vlm", "--exclure", "autre"] + base), 0)       # = le seul vlm restant : faux/modele
            with open(os.path.join(sortie, "resultats_benchmark.json"), encoding="utf-8") as f:
                j = json.load(f)
            self.assertEqual(list(j["synthese"]), ["lm:faux/modele"])
            self.assertEqual(j["pas"], 4)
            self.assertIn("vides", j["synthese"]["lm:faux/modele"])
            n = len(FauxLM.requetes)
            self.assertEqual(B.main(["--vlm", "--exclure", "autre", "--reprendre"] + base), 0)   # rien à refaire : aucune requête de plus
            self.assertEqual(len(FauxLM.requetes), n)
            self.assertTrue(os.path.exists(os.path.join(sortie, "rapport_benchmark.md")))
        finally:
            shutil.rmtree(sortie, ignore_errors=True)

    def test_modele_sans_vision_abandonne_vite(self):
        class Refuse(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                corps = json.dumps({"data": [{"id": "texte/seul"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(corps)

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"{}")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Refuse)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            lec = B.LecteurLMStudio("lm:texte/seul", f"http://127.0.0.1:{srv.server_port}/v1", None, timeout=5)
            r = B.evaluer(lec, B.charger_verite(B.VERITE_DEFAUT)[:1], False, log=lambda *a: None)
            self.assertIn("chargement", r["abandon"])                          # l'appel d'échauffement échoue : abandon immédiat
        finally:
            srv.shutdown()
            srv.server_close()


class TestTesseract(unittest.TestCase):
    def test_options(self):
        t = B.LecteurTesseract("tess:ara:best:psm13:x3:bin", {"best": "/d"})
        self.assertEqual((t.lang, t.dossier, t.psm, t.echelle, t.bin), ("ara", "/d", 13, 3, True))
        with self.assertRaises(ValueError):
            B.LecteurTesseract("tess:inconnue", {})

    def test_dossier_de_langues_par_variable_d_environnement_sans_guillemets(self):
        t = B.LecteurTesseract("tess:ara:fast", {"fast": "C:\\tess\\fast"})
        self.assertNotIn("tessdata-dir", t._cfg(7))                          # plus de chemin entre guillemets dans la ligne de commande
        ancien = os.environ.get("TESSDATA_PREFIX")
        try:
            t._poser_env()
            self.assertEqual(os.environ["TESSDATA_PREFIX"], "C:\\tess\\fast")
        finally:
            if ancien is None:
                os.environ.pop("TESSDATA_PREFIX", None)
            else:
                os.environ["TESSDATA_PREFIX"] = ancien

    def test_grille(self):
        import argparse
        a = argparse.Namespace(tessdata=["fast=/a", "best=/b"], tesseract_cmd="", indice_tess="", url="", hauteur_vlm=128)
        noms = [l.nom for l in B.construire("grille:tess", a)]
        self.assertEqual(len(noms), 16)
        self.assertIn("tess:ara:fast:psm7:x3:bin", noms)

    def test_telechargement_tessdata(self):
        import urllib.request
        appels = []

        class Reponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def faux_urlopen(url, timeout=0):
            appels.append(url)
            return Reponse(b"x" * 200_000)
        reel = urllib.request.urlopen
        dest = tempfile.mkdtemp()
        try:
            urllib.request.urlopen = faux_urlopen
            res = B.telecharger_tessdata(dest, log=lambda *a: None)
            self.assertEqual(set(res), {"fast", "best"})
            self.assertTrue(os.path.isfile(os.path.join(dest, "best", "ara.traineddata")))
            self.assertTrue(any("tessdata_best" in u for u in appels))
            n = len(appels)
            B.telecharger_tessdata(dest, log=lambda *a: None)                # déjà présent : pas de nouveau téléchargement
            self.assertEqual(len(appels), n)
        finally:
            urllib.request.urlopen = reel
            shutil.rmtree(dest, ignore_errors=True)

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


class ServeurDyn(BaseHTTPRequestHandler):
    """Faux LM Studio dont la liste de modèles change (un `lms get` simulé en ajoute)."""
    modeles: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/v0/models"):
            corps = json.dumps({"data": [{"id": m, "type": "vlm", "state": "not-loaded"} for m in ServeurDyn.modeles]}).encode()
        else:
            corps = json.dumps({"data": [{"id": m} for m in ServeurDyn.modeles]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(corps)


class TestPreparation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), ServeurDyn)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/v1"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        import argparse
        ServeurDyn.modeles = ["google/gemma-4-12b-qat", "qwen/qwen3-vl-8b", "internvl3_5-14b", "lfm3-vl-2b@f16", "lfm3-vl-2b@q4_0",
                              "qwen/qwen2.5-vl-7b", "google/gemma-4-31b", "google/gemma-4-26b-a4b", "prism-ml/bonsai-27b"]
        self.a = argparse.Namespace(url=self.url, oui=True, sans_installation=False, tessdata=[])
        self.appels = []
        self.sauve = (B._executer, B.trouver_lms, B.telecharger_tessdata, B.PAQUETS_PIP, B.tessdata_complet)
        B._executer = self.faux_executer
        B.trouver_lms = lambda: "/faux/lms"
        B.telecharger_tessdata = lambda *a, **k: self.appels.append(("tessdata",)) or {}
        B.tessdata_complet = lambda: False
        B.PAQUETS_PIP = {"x": ("module_inexistant_xyz", ["paquet-bidon"])}
        self.telechargeables = {"qwen/qwen3-vl-2b": "qwen/qwen3-vl-2b", "olmocr-2-7b": "allenai/olmocr-2-7b-1025"}

    def tearDown(self):
        B._executer, B.trouver_lms, B.telecharger_tessdata, B.PAQUETS_PIP, B.tessdata_complet = self.sauve

    def faux_executer(self, cmd, **kw):
        self.appels.append(tuple(cmd[1:]) if cmd[0] == "/faux/lms" else tuple(cmd[2:4]))
        if len(cmd) > 2 and cmd[1] == "get" and cmd[2] in self.telechargeables:
            ServeurDyn.modeles.append(self.telechargeables[cmd[2]])
        return type("R", (), {"returncode": 0})()

    def test_resoudre_modele(self):
        e = {"ids": ["qwen2-vl", "qwen/qwen2.5-vl-7b"]}
        self.assertEqual(B.resoudre_modele(e, ["qwen/qwen2.5-vl-7b", "qwen/qwen2-vl-7b-instruct"]), "qwen/qwen2-vl-7b-instruct")   # 1er choix prioritaire
        self.assertEqual(B.resoudre_modele(e, ["qwen/qwen2.5-vl-7b"]), "qwen/qwen2.5-vl-7b")                                          # sinon voisin
        self.assertIsNone(B.resoudre_modele(e, ["google/gemma-4-12b"]))
        self.assertEqual(B.resoudre_modele({"ids": ["lfm3-vl-2b@f16", "lfm3-vl-2b"]}, ["lfm3-vl-2b@q4_0", "lfm3-vl-2b@f16"]), "lfm3-vl-2b@f16")

    def test_cites_ne_teste_que_les_modeles_cites_et_installe_le_manque(self):
        logs = []
        cands, notes, abandons = B.preparer_cites(self.a, log=logs.append)
        lm = [c for c in cands if c.startswith("lm:")]
        self.assertIn("lm:google/gemma-4-12b-qat", lm)
        self.assertIn("lm:qwen/qwen3-vl-8b", lm)
        self.assertIn("lm:qwen/qwen3-vl-2b", lm)                                  # téléchargé
        self.assertIn("lm:allenai/olmocr-2-7b-1025", lm)                          # téléchargé sous un autre nom
        self.assertIn("lm:lfm3-vl-2b@f16", lm)
        self.assertIn("lm:internvl3_5-14b", lm)                                   # voisin installé : pas de nouveau téléchargement
        self.assertIn("lm:qwen/qwen2.5-vl-7b", lm)
        for pas_cite in ("gemma-4-31b", "gemma-4-26b-a4b", "bonsai", "lfm3-vl-2b@q4_0"):
            self.assertFalse(any(pas_cite in c for c in lm), pas_cite)
        self.assertEqual(cands[:4], B.LECTEURS_CITES)
        self.assertIn("Qari-OCR (arabe)", abandons)                              # introuvable : signalé, pas fatal
        self.assertIn("Melaraby / qwen_vlm_arabic_ocr (GGUF)", abandons)
        self.assertEqual(notes["Qwen3-VL 2B"], "téléchargé")
        self.assertIn("remplacé", notes["Qwen2-VL (7B)"])
        self.assertIn(("pip", "install"), [a[:2] for a in self.appels])           # paquet Python manquant installé
        self.assertIn(("tessdata",), self.appels)                                 # langues Tesseract téléchargées
        self.assertFalse(any(a[:2] == ("get", "google/gemma-4-31b") for a in self.appels))

    def test_sans_accord_rien_n_est_installe(self):
        self.a.oui = False                                                       # et pas de terminal interactif dans les tests
        cands, notes, abandons = B.preparer_cites(self.a, log=lambda *x: None)
        self.assertFalse(any(a[0] == "get" or a[:2] == ("pip", "install") or a == ("tessdata",) for a in self.appels))
        self.assertNotIn("lm:qwen/qwen3-vl-2b", cands)
        self.assertIn("Qwen3-VL 2B", abandons)

    def test_sans_installation(self):
        self.a.sans_installation = True
        B.preparer_cites(self.a, log=lambda *x: None)
        self.assertEqual(self.appels, [])

    def test_telecharger_modele_essaie_les_termes_dans_l_ordre(self):
        e = {"ids": ["qari"], "telecharger": ["introuvable", "qwen/qwen3-vl-2b"]}
        res = B.telecharger_modele(e, lambda: list(ServeurDyn.modeles), log=lambda *x: None)
        self.assertEqual(res, "qwen/qwen3-vl-2b")
        self.assertEqual([a[1] for a in self.appels if a[0] == "get"], ["introuvable", "qwen/qwen3-vl-2b"])
        self.assertIsNone(B.telecharger_modele({"ids": ["z"], "telecharger": ["rien"]}, lambda: list(ServeurDyn.modeles), log=lambda *x: None))
        B.trouver_lms = lambda: ""
        self.assertIsNone(B.telecharger_modele(e, lambda: [], log=lambda *x: None))

    def test_serveur_deja_demarre_ne_lance_rien(self):
        self.assertTrue(B.assurer_serveur(self.url, log=lambda *x: None))
        self.assertEqual(self.appels, [])

    def test_serveur_arrete_est_demarre_par_lms(self):
        mort = "http://127.0.0.1:9/v1"
        logs = []
        self.assertFalse(B.assurer_serveur(mort, log=logs.append, attente=0.1))
        self.assertIn(("server", "start"), self.appels)
        B.trouver_lms = lambda: ""
        logs.clear()
        self.assertFalse(B.assurer_serveur(mort, log=logs.append))
        self.assertTrue(any("Start Server" in l for l in logs))

    def test_pas_de_serveur_les_modeles_de_vision_sont_ignores(self):
        self.a.url = "http://127.0.0.1:9/v1"
        B.trouver_lms = lambda: ""
        cands, notes, abandons = B.preparer_cites(self.a, log=lambda *x: None)
        self.assertEqual(cands, B.LECTEURS_CITES)

    def test_dossier_ascii(self):
        base = tempfile.mkdtemp()
        try:
            avec_accent = os.path.join(base, "Téléchargements", "fast")
            os.makedirs(avec_accent)
            with open(os.path.join(avec_accent, "ara.traineddata"), "wb") as f:
                f.write(b"x" * 1000)
            ok = B.dossier_ascii(avec_accent)
            if tempfile.gettempdir().isascii():
                self.assertTrue(ok.isascii())
                self.assertEqual(os.path.getsize(os.path.join(ok, "ara.traineddata")), 1000)
                self.assertEqual(B.dossier_ascii(avec_accent), ok)                  # déterministe, pas de nouvelle copie
            sans = os.path.join(base, "simple")
            os.makedirs(sans)
            self.assertEqual(B.dossier_ascii(sans), sans)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_rapport_limite_a_la_selection(self):
        pages = B.charger_verite(B.VERITE_DEFAUT)[:1]
        res = {n: B.evaluer(Faux(n, lambda s, img: ""), pages, False) for n in ("garde", "ancien")}
        synth = {n: B.synthese(r) for n, r in res.items()}
        sortie = tempfile.mkdtemp()
        try:
            txt = B.ecrire_sorties(sortie, synth, {}, res, {}, 13, pages, 1, {}, {"X": "remarque"}, selection={"garde"})
            self.assertIn("`garde`", txt)
            self.assertNotIn("`ancien`", txt)
            self.assertIn("remarque", txt)
            with open(os.path.join(sortie, "resultats_benchmark.json"), encoding="utf-8") as f:
                self.assertEqual(set(json.load(f)["detail"]), {"garde", "ancien"})   # le JSON garde tout
        finally:
            shutil.rmtree(sortie, ignore_errors=True)


class TestModeMoteur(unittest.TestCase):
    """Mode « moteur » : API native de LM Studio, réflexion coupée, contexte explicite (mêmes conditions que extraire_prenoms_llm)."""

    def setUp(self):
        import comparer_modeles as C
        import extraire_prenoms_llm as X
        self.C, self.X = C, X
        self.sauve = (C.trouver_lms, C.charger, X.resoudre_modele, X.appeler_llm)
        self.charges, self.appels = [], []
        C.trouver_lms = lambda: "/faux/lms"
        C.charger = lambda ex, lms, mdl, url, delai=240: self.charges.append(mdl) or {"ok": True, "duree_s": 0, "cle": mdl["nom"], "message": ""}
        self.info = type("I", (), {"ctx": 16384, "options_reflexion": ["off", "on"], "quant": "Q4_K_M", "taille_go": 5.0, "vision": True})()
        X.resoudre_modele = lambda cfg: self.info

        def faux_llm(cfg, info, png, systeme, texte, **kw):
            self.appels.append({"png": png, "texte": texte, **kw})
            return {"content": "<think>x</think>عالي", "finish": "stop", "tokens": 7, "tokens_reflexion": 0, "abandon": "", "erreur": ""}
        X.appeler_llm = faux_llm
        self.lec = B.LecteurLMStudio("lm:faux/modele", "http://127.0.0.1:1234/v1", None, contexte=12288)
        self.lec.natif = True
        self.img = Image.new("RGB", (60, 20), "white")

    def tearDown(self):
        self.C.trouver_lms, self.C.charger, self.X.resoudre_modele, self.X.appeler_llm = self.sauve

    def test_charge_avec_le_contexte_demande_et_coupe_la_reflexion(self):
        self.lec.echauffer()
        self.assertEqual(self.charges, [{"nom": "faux/modele", "ctx": 12288, "gpu": "max", "parallel": 1}])
        self.assertEqual(self.lec.lire_crop(self.img), "عالي")
        self.assertIs(self.appels[-1]["penser"], False)                       # réflexion coupée comme dans le moteur
        self.assertTrue(self.appels[-1]["png"].startswith(b"\x89PNG"))
        self.assertEqual(self.lec.derniere["finish"], "stop")
        self.assertEqual(self.lec.conditions["contexte"], 16384)
        self.assertTrue(self.lec.conditions["reflexion_coupable"])
        self.assertTrue(self.lec.conditions["charge_par_le_script"])

    def test_sans_chargement_utilise_le_modele_tel_quel(self):
        self.lec.charger_modele = False
        self.lec.echauffer()
        self.assertEqual(self.charges, [])
        self.assertFalse(self.lec.conditions["charge_par_le_script"])

    def test_contexte_trop_petit_signale(self):
        self.info.ctx = 4096
        self.lec.echauffer()
        self.assertIn("4096", self.lec.conditions["alerte"])

    def test_echec_du_chargement_leve_une_erreur_claire(self):
        self.C.charger = lambda *a, **k: {"ok": False, "message": "`lms load` a échoué (code 1)"}
        with self.assertRaises(RuntimeError) as e:
            self.lec.echauffer()
        self.assertIn("lms load", str(e.exception))

    def test_erreur_du_serveur_comptee_comme_erreur(self):
        self.lec.echauffer()
        self.X.appeler_llm = lambda *a, **k: {"erreur": "HTTP 400", "content": ""}
        with self.assertRaises(RuntimeError):
            self.lec.lire_crop(self.img)

    def test_evaluer_enregistre_les_conditions_et_la_reflexion(self):
        r = B.evaluer(self.lec, B.charger_verite(B.VERITE_DEFAUT)[:1], False, pas=6)
        self.assertEqual(r["conditions"]["contexte"], 16384)
        s = B.synthese(r)
        self.assertEqual(s["conditions"]["api"], "natif")
        self.assertEqual(s["reflexion"], 0.0)

    def test_sonde_de_l_api_native(self):
        class Natif(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                corps = json.dumps({"models": []} if self.path.startswith("/api/v1/models") else {"data": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(corps)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Natif)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertTrue(B.LecteurLMStudio("lm:x", f"http://127.0.0.1:{srv.server_port}/v1", None)._sonder_natif())
            self.assertFalse(B.LecteurLMStudio("lm:x", "http://127.0.0.1:9/v1", None)._sonder_natif())
        finally:
            srv.shutdown()
            srv.server_close()


class TestDiagnostics(unittest.TestCase):
    def setUp(self):
        self.pages = B.charger_verite(B.VERITE_DEFAUT)[:2]
        self.vals = [it["texte"] for p in self.pages for it in p["items"]]

    def lecteur(self, nom, f, meta=None):
        class L(B.Lecteur):
            def __init__(s):
                s.nom, s.k, s.derniere = nom, 0, {}

            def lire_crop(s, img):
                s.k += 1
                s.derniere = dict(meta or {})
                return f(s.k - 1)
        return L()

    def test_synthese_reflexion_jetons_longueur(self):
        r = B.evaluer(self.lecteur("pense", lambda k: self.vals[k], {"raisonnement": True, "tokens": 1500, "finish": "length"}), self.pages, False)
        s = B.synthese(r)
        self.assertEqual((s["reflexion"], s["jetons_moy"], s["longueur"]), (1.0, 1500, 1.0))

    def test_resultat_invalide_detecte(self):
        r = B.evaluer(self.lecteur("charabia", lambda k: "ظظظظظظ"), self.pages, False)
        self.assertTrue(B.synthese(r)["invalide"])
        r = B.evaluer(self.lecteur("bon", lambda k: self.vals[k]), self.pages, False)
        self.assertFalse(B.synthese(r)["invalide"])
        r = B.evaluer(self.lecteur("muet", lambda k: ""), self.pages, False)
        self.assertFalse(B.synthese(r)["invalide"])                           # se taire n'est pas halluciner

    def test_doublons_et_invalides_exclus_du_vote(self):
        a = B.evaluer(self.lecteur("a", lambda k: self.vals[k]), self.pages, False)
        b = B.evaluer(self.lecteur("b_copie_de_a", lambda k: self.vals[k]), self.pages, False)
        c = B.evaluer(self.lecteur("c", lambda k: self.vals[k] if k % 2 else "ظظظظ"), self.pages, False)
        d = B.evaluer(self.lecteur("d_invalide", lambda k: "ظظظظ"), self.pages, False)
        comb = B.combinaisons({"a": a, "b_copie_de_a": b, "c": c, "d_invalide": d}, exclus={"d_invalide"})
        self.assertEqual(comb["doublons"], {"b_copie_de_a": "a"})
        noms = {x["a"] for x in comb["accords"]} | {x["b"] for x in comb["accords"]}
        self.assertEqual(noms, {"a", "c"})
        self.assertEqual(comb["votes"], [])                                   # il ne reste que 2 lecteurs distincts : pas de vote à 3

    def test_rapport_conditions_invalides_doublons(self):
        inv = B.evaluer(self.lecteur("lm:x", lambda k: "ظظظظظظ"), self.pages, False)
        inv["conditions"] = {"api": "natif", "contexte": 16384, "reflexion_coupable": True, "quant": "Q4", "taille_go": 8.0}
        ok = B.evaluer(self.lecteur("lm:y", lambda k: self.vals[k], {"raisonnement": True, "tokens": 900}), self.pages, False)
        ok["conditions"] = {"api": "natif", "contexte": 4096, "alerte": "contexte chargé de 4096 jetons"}
        sortie = tempfile.mkdtemp()
        try:
            res = {"lm:x": inv, "lm:y": ok}
            synth = {k: B.synthese(v) for k, v in res.items()}
            txt = B.ecrire_sorties(sortie, synth, {}, res, {}, 22, self.pages, 1, {}, None, None)
            self.assertIn("Conditions de mesure", txt)
            self.assertIn("16384", txt)
            self.assertIn("jugés invalides", txt)
            self.assertIn("contexte chargé de 4096", txt)
            self.assertIn("réfléchissent à chaque réponse", txt)
        finally:
            shutil.rmtree(sortie, ignore_errors=True)


class TestInstallationFiable(unittest.TestCase):
    def test_un_telechargement_anterieur_n_est_pas_attribue_au_modele_demande(self):
        """Cas réel : pendant `lms get Qari…` (introuvable), le téléchargement d'olmOCR lancé avant se termine : il ne doit pas devenir « Qari »."""
        ids = ["a/b"]
        sauve = (B._executer, B.trouver_lms)
        B.trouver_lms = lambda: "/faux/lms"
        B._executer = lambda cmd, **kw: ids.append("allenai/olmocr-2-7b") if cmd[1] == "get" else None
        logs = []
        try:
            res = B.telecharger_modele({"ids": ["qari"], "telecharger": ["NAMAA-Space/Qari-OCR-0.1-VL-2B-Instruct"]}, lambda: list(ids), log=logs.append)
        finally:
            B._executer, B.trouver_lms = sauve
        self.assertIsNone(res)
        self.assertTrue(any("sans rapport" in l for l in logs))

    def test_le_bon_modele_est_accepte_meme_avec_un_autre_nom(self):
        ids = ["a/b"]
        sauve = (B._executer, B.trouver_lms)
        B.trouver_lms = lambda: "/faux/lms"
        B._executer = lambda cmd, **kw: ids.append("NAMAA-Space/Qari-OCR-0.1-VL-2B-GGUF") if cmd[1] == "get" else None
        try:
            res = B.telecharger_modele({"ids": ["qari"], "telecharger": ["Qari-OCR"]}, lambda: list(ids), log=lambda *a: None)
        finally:
            B._executer, B.trouver_lms = sauve
        self.assertEqual(res, "NAMAA-Space/Qari-OCR-0.1-VL-2B-GGUF")


class TestAjoutManuel(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sauve = B.FICHIER_MODELES_SUPP
        B.FICHIER_MODELES_SUPP = os.path.join(self.dir, "benchmark", "modeles_supplementaires.json")

    def tearDown(self):
        B.FICHIER_MODELES_SUPP = self.sauve
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_ajouter_et_catalogue(self):
        self.assertEqual(len(B.catalogue()), len(B.CATALOGUE_CITES))
        B.ajouter_modeles(["qwen/qwen3-vl-4b"])
        B.ajouter_modeles(["qwen/qwen3-vl-4b", "google/gemma-4-26b-a4b"])        # idempotent
        noms = [e["nom"] for e in B.catalogue()]
        self.assertEqual(noms[-2:], ["qwen/qwen3-vl-4b", "google/gemma-4-26b-a4b"])
        self.assertEqual(len(noms), len(B.CATALOGUE_CITES) + 2)

    def test_modele_ajoute_pris_en_compte_par_cites(self):
        import argparse
        B.ajouter_modeles(["google/gemma-4-26b-a4b"])
        sauve = (B.serveur_ok, B.modeles_lmstudio, B.PAQUETS_PIP, B.tessdata_complet)
        B.serveur_ok = lambda url: True
        B.modeles_lmstudio = lambda url: [{"id": "google/gemma-4-26b-a4b", "type": "vlm"}, {"id": "google/gemma-4-12b-qat", "type": "vlm"}]
        B.PAQUETS_PIP, B.tessdata_complet = {}, lambda: True
        try:
            a = argparse.Namespace(url="http://x/v1", oui=False, sans_installation=True, tessdata=[])
            cands, notes, abandons = B.preparer_cites(a, log=lambda *x: None)
        finally:
            B.serveur_ok, B.modeles_lmstudio, B.PAQUETS_PIP, B.tessdata_complet = sauve
        self.assertIn("lm:google/gemma-4-26b-a4b", cands)

    def test_cli_ajouter(self):
        self.assertEqual(B.main(["--ajouter", "qwen/qwen3-vl-4b"]), 0)
        with open(B.FICHIER_MODELES_SUPP, encoding="utf-8") as f:
            self.assertEqual(json.load(f)[0]["ids"], ["qwen/qwen3-vl-4b"])


class TestPaddle(unittest.TestCase):
    def test_lecture_du_resultat_paddleocr_3(self):
        class Res(dict):
            pass
        self.assertEqual(B.LecteurPaddle._textes([{"rec_texts": ["عالي", "عالم"]}]), ["عالي", "عالم"])
        self.assertEqual(B.LecteurPaddle._textes([{"res": {"rec_texts": ["أحمد"]}}]), ["أحمد"])
        obj = type("R", (), {"json": {"res": {"rec_texts": ["عامل"]}}})()
        self.assertEqual(B.LecteurPaddle._textes([obj]), ["عامل"])
        self.assertEqual(B.LecteurPaddle._textes(None), [])


if __name__ == "__main__":
    unittest.main()
