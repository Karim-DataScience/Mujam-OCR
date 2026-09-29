# -*- coding: utf-8 -*-
"""Tests de extraire_prenoms_llm.py — sans LM Studio ni GPU (un FAUX serveur simule le modèle).

    python test_extraire_prenoms_llm.py
    python test_extraire_prenoms_llm.py -v

Le test de bout en bout utilise le vrai PDF s'il est trouvé (variable PDF_LIVRE, ou à côté du script,
ou /mnt/user-data/uploads) ; sinon il est ignoré.
"""
import csv
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image

import extraire_prenoms_llm as X
import verite_terrain as VT

EXEMPLE_PROMPT = {"entrees": [
    {"prenoms": [{"genre": "MASCULIN", "orthographes": ["Youssouf", "Youssef"], "arabe": "يُوسُف"},
                 {"genre": "FEMININ", "orthographes": ["Karim", "Karima"], "arabe": "x"}],
     "description": "d", "suite_page_precedente": False, "description_incomplete": False}]}


def rep(content="", reasoning="", finish="stop"):
    return {"content": content, "reasoning": reasoning, "finish": finish, "tokens": 10}


class TestJSON(unittest.TestCase):
    def test_apres_think_et_bloc_markdown(self):
        vrai = {"entrees": [{"prenoms": [{"genre": "MASCULIN", "orthographes": ["Adam"], "arabe": "آدَم"}],
                             "description": "Âdam (Adam) : premier homme."}]}
        texte = ("<think>Voyons. Exemple : " + json.dumps(EXEMPLE_PROMPT) + " ... </think>\n```json\n"
                 + json.dumps(vrai, ensure_ascii=False) + "\n```\nVoilà.")
        obj, _, partiel = X.extraire_page_json(rep(texte))
        self.assertFalse(partiel)
        self.assertEqual(obj["entrees"][0]["prenoms"][0]["orthographes"], ["Adam"])

    def test_json_dans_reasoning_si_contenu_vide(self):
        obj, _, _ = X.extraire_page_json(rep("", 'blabla {"entrees": []} fin'))
        self.assertEqual(obj, {"entrees": []})

    def test_coupure_length_ne_lit_pas_la_reflexion(self):
        obj, _, _ = X.extraire_page_json(rep("<think>exemple " + json.dumps(EXEMPLE_PROMPT), "", finish="length"))
        self.assertIsNone(obj)
        obj, _, _ = X.extraire_page_json(rep("", json.dumps(EXEMPLE_PROMPT), finish="length"))
        self.assertIsNone(obj)

    def test_json_tronque_recupere_entrees_completes(self):
        e1 = {"prenoms": [{"genre": "FEMININ", "orthographes": ["Sara"], "arabe": "سَارَة"}], "description": "Sâra : princesse."}
        texte = '{"entrees": [' + json.dumps(e1, ensure_ascii=False) + ', {"prenoms": [{"genre": "MASC'
        obj, avert, partiel = X.extraire_page_json(rep(texte, finish="length"))
        self.assertTrue(partiel)
        self.assertEqual(len(obj["entrees"]), 1)
        self.assertTrue(avert)

    def test_virgules_finales_tolerees(self):
        obj, _, _ = X.extraire_page_json(rep('{"entrees": [{"prenoms": [], "description": "suite",},]}'))
        self.assertEqual(len(obj["entrees"]), 1)

    def test_exemple_du_prompt_detecte(self):
        self.assertTrue(X.est_exemple(X.normaliser_page(EXEMPLE_PROMPT)))
        self.assertFalse(X.est_exemple(X.normaliser_page({"entrees": [
            {"prenoms": [{"genre": "MASCULIN", "orthographes": ["Youssef"], "arabe": ""}], "description": "x"}]})))


class TestNormalisation(unittest.TestCase):
    def test_tatwil_et_marques_retires_voyelles_gardees(self):
        self.assertEqual(X.nettoyer_arabe("حَـــوَّاء\u200f"), "حَوَّاء")

    def test_genre(self):
        self.assertEqual([X.normaliser_genre(g) for g in ("masculin", "Féminin", "FEMININ", "M", "f", "", None, "??")],
                         ["MASCULIN", "FEMININ", "FEMININ", "MASCULIN", "FEMININ", "", "", ""])

    def test_orthographes_collees_par_des_barres(self):
        e = X.normaliser_entree({"prenoms": [{"genre": "masculin", "orthographes": "Chouai'b / Chouéib", "arabe": "شُعَيْب"}],
                                 "description": "* Shu’ayb (Jethro) : prophète."})
        self.assertEqual(e["prenoms"][0]["orthographes"], ["Chouai'b", "Chouéib"])
        self.assertTrue(e["description"].startswith("Shu’ayb"))          # astérisque retiré

    def test_description_mot_coupe_et_espaces(self):
        self.assertEqual(X.nettoyer_description("La tradition islami-\nque   lui attribue"), "La tradition islamique lui attribue")
        self.assertEqual(X.joindre("nom coupé en fin de pa-", "ge suivante"), "nom coupé en fin de page suivante")
        self.assertEqual(X.joindre("fin.", "Suite."), "fin. Suite.")

    def test_parse_description(self):
        self.assertEqual(X.parse_description("Âdam (Adam) : premier homme."), ("Âdam", "Adam"))
        self.assertEqual(X.parse_description("Nâḍil / Nâḍila : qui l'emporte au combat."), ("Nâḍil / Nâḍila", ""))
        self.assertEqual(X.parse_description("Ḥawâ' (Ève) : épouse d'Âdam."), ("Ḥawâ'", "Ève"))
        self.assertEqual(X.parse_description("Signifie en hébreu : que Dieu ajoute."), ("", ""))


    def test_parse_description_apostrophe_initiale_et_deux_points_colles(self):
        self.assertEqual(X.parse_description("‘Abd ash-Shahîd : le serviteur du Témoin par excellence."), ("‘Abd ash-Shahîd", ""))
        self.assertEqual(X.parse_description("Darqâwî: Ad-Darqâwî, saint marocain."), ("Darqâwî", ""))
        self.assertEqual(X.parse_description("Murûj : pluriel de Marj : prairie, pré, pelouse."), ("Murûj", ""))


class TestExplosion(unittest.TestCase):
    def entree(self, blocs, desc, page=57, incomplete=False, partiel=False):
        return {"prenoms": blocs, "description": desc, "description_incomplete": incomplete,
                "page": page, "partiel": partiel, "id": 1}

    def test_exemple_de_lutilisateur_une_ligne_par_prenom(self):
        desc = ("Yûsuf (Joseph) : fils de Ya’qub. La 12e sourate du Coran lui est presque entièrement consacrée "
                "et porte son nom. Signifie en hébreu : que Dieu ajoute (d’autres enfants à celui qui vient de naître).")
        rows = X.exploser([self.entree([{"genre": "MASCULIN", "orthographes": ["Youssouf", "Youssef"], "arabe": "يُوسُف"}], desc)])
        self.assertEqual([r["prénom_fr"] for r in rows], ["Youssouf", "Youssef"])
        for r in rows:
            self.assertEqual((r["prénom_arab_diacritiques"], r["genre"], r["description_en_français"]),
                             ("يُوسُف", "MASCULIN", desc))
            self.assertEqual(r["équivalent_étranger"], "Joseph")
            self.assertEqual(r["forme_associée"], "")
        self.assertEqual((rows[0]["prénoms_équivalents"], rows[1]["prénoms_équivalents"]), ("Youssef", "Youssouf"))
        self.assertEqual(rows[0]["thème"], "1. Les prophètes")

    def test_couple_masculin_feminin(self):
        rows = X.exploser([self.entree([
            {"genre": "MASCULIN", "orthographes": ["Haïbane"], "arabe": "هَيْبَان"},
            {"genre": "FEMININ", "orthographes": ["Haïbana", "Haibana"], "arabe": "هَيْبَانَة"}],
            "Haybân / Haybâna : craint, respecté, vénéré.", page=150)])
        self.assertEqual([r["prénom_fr"] for r in rows], ["Haïbane", "Haïbana", "Haibana"])
        self.assertEqual(rows[0]["forme_associée"], "Haïbana / Haibana")
        self.assertEqual(rows[1]["forme_associée"], "Haïbane")
        self.assertEqual(rows[1]["prénoms_équivalents"], "Haibana")
        self.assertEqual((rows[0]["page_pdf"], rows[0]["page_imprimée"]), (150, 146))

    def test_drapeaux(self):
        e = self.entree([{"genre": "MASCULIN", "orthographes": ["Hamza"], "arabe": "حمزة"}], "Hamza : lion", incomplete=True)
        r = X.exploser([e])[0]["à_vérifier"]
        for f in ("arabe_sans_voyelles", "description_coupee", "genre_a_verifier"):
            self.assertIn(f, r)
        e2 = self.entree([{"genre": "FEMININ", "orthographes": ["Sara"], "arabe": ""}], "")
        self.assertIn("arabe_vide", X.exploser([e2])[0]["à_vérifier"])
        self.assertIn("description_vide", X.exploser([e2])[0]["à_vérifier"])
        e3 = self.entree([{"genre": "FEMININ", "orthographes": ["Sar4"], "arabe": "سَارَة"}], "Sâra : princesse.")
        self.assertIn("chiffres_dans_prenom", X.exploser([e3])[0]["à_vérifier"])
        self.assertEqual(X.exploser([self.entree([{"genre": "FEMININ", "orthographes": ["Sara"], "arabe": "سَارَة"}],
                                                 "Sâra : princesse.")])[0]["à_vérifier"], "")


class TestAssemblage(unittest.TestCase):
    def res(self, entrees, statut="ok"):
        return {"entrees": entrees, "statut": statut}

    def bloc(self, nom):
        return {"genre": "MASCULIN", "orthographes": [nom], "arabe": "أ"}

    def test_suite_de_definition_sur_la_page_suivante(self):
        r = {(56, 1): self.res([{"prenoms": [self.bloc("Ibrahim")], "description": "Ibrâhîm : père de la multi-",
                                 "description_incomplete": True}]),
             (57, 1): self.res([{"prenoms": [], "description": "tude. Honoré en Islam.", "description_incomplete": False},
                                {"prenoms": [self.bloc("Ismaïl")], "description": "Ismâ‘îl : Dieu entend.",
                                 "description_incomplete": False}])}
        ent, avert = X.assembler(r, 1)
        self.assertEqual(len(ent), 2)
        self.assertEqual(ent[0]["description"], "Ibrâhîm : père de la multitude. Honoré en Islam.")
        self.assertFalse(ent[0]["description_incomplete"])
        self.assertEqual(avert, [])

    def test_prenoms_en_bas_de_page_definition_en_haut_de_la_suivante(self):
        r = {(60, 1): self.res([{"prenoms": [self.bloc("Yahya")], "description": "", "description_incomplete": True}]),
             (61, 1): self.res([{"prenoms": [], "description": "Yaḥyâ : Dieu fait vivre.", "description_incomplete": False}])}
        ent, _ = X.assembler(r, 1)
        self.assertEqual(ent[0]["description"], "Yaḥyâ : Dieu fait vivre.")

    def test_page_manquante_ne_rattache_pas_a_laveugle(self):
        r = {(60, 1): self.res([{"prenoms": [self.bloc("Yahya")], "description": "Yaḥyâ : vit", "description_incomplete": True}]),
             (62, 1): self.res([{"prenoms": [], "description": "suite d'une autre entrée.", "description_incomplete": False}])}
        ent, avert = X.assembler(r, 1)
        self.assertEqual(ent[0]["description"], "Yaḥyâ : vit")
        self.assertEqual(len(avert), 1)

    def test_demi_pages(self):
        r = {(55, 1): self.res([{"prenoms": [self.bloc("Hawa")], "description": "Ḥawâ' (Ève) : épouse d'Âdam.", "description_incomplete": True}]),
             (55, 2): self.res([{"prenoms": [], "description": "De l'hébreu : vie.", "description_incomplete": False}])}
        ent, _ = X.assembler(r, 2)
        self.assertEqual(ent[0]["description"], "Ḥawâ' (Ève) : épouse d'Âdam. De l'hébreu : vie.")


    def test_suite_rattachee_si_la_definition_precedente_semble_coupee(self):
        r = {(56, 1): self.res([{"prenoms": [self.bloc("Ibrahim")], "description": "Ibrâhîm : père de la multi-",
                                 "description_incomplete": False}]),
             (57, 1): self.res([{"prenoms": [], "description": "tude.", "description_incomplete": False}])}
        ent, avert = X.assembler(r, 1)
        self.assertEqual(ent[0]["description"], "Ibrâhîm : père de la multitude.")
        self.assertEqual(avert, [])

    def test_suite_non_rattachee_si_la_definition_precedente_est_terminee(self):
        r = {(56, 1): self.res([{"prenoms": [self.bloc("Sara")], "description": "Sâra : princesse.", "description_incomplete": False}]),
             (57, 1): self.res([{"prenoms": [], "description": "Texte d'introduction du chapitre.", "description_incomplete": False}])}
        ent, avert = X.assembler(r, 1)
        self.assertEqual(ent[0]["description"], "Sâra : princesse.")
        self.assertEqual(len(avert), 1)


class TestImage(unittest.TestCase):
    def test_nettoyage_efface_le_fantome_et_garde_encre_et_rouge(self):
        a = np.full((300, 300, 3), (222, 224, 238), np.uint8)          # papier
        a[20:40, 20:200] = (60, 55, 75)                                # encre noire
        a[100:120, 20:200] = (205, 30, 80)                             # texte rouge
        a[200:220, 20:200] = (175, 175, 195)                           # fantôme du verso
        out = np.asarray(X.nettoyer_page(Image.fromarray(a)))
        self.assertLess(out[30, 100].max(), 30)                        # encre -> noire
        self.assertGreater(out[110, 100][0], 200)
        self.assertLess(out[110, 100][1], 60)                          # rouge -> reste rouge
        self.assertGreater(out[210, 100].min(), 240)                   # fantôme -> blanc
        self.assertGreater(out[5, 5].min(), 250)                       # papier -> blanc

    def test_decoupe_dans_la_zone_blanche(self):
        a = np.full((1000, 400, 3), 255, np.uint8)
        for y in list(range(300, 460)) + list(range(560, 700)):
            a[y, 50:350] = 0                                           # deux blocs de texte, blanc entre 460 et 560
        im = Image.fromarray(a)
        haut, bas = X.decouper(im, im, 2)
        self.assertEqual(haut.height + bas.height, 1000)
        self.assertTrue(460 <= haut.height <= 560, haut.height)


class TestExport(unittest.TestCase):
    def test_csv_comme_lexemple(self):
        rows = X.exploser([{"prenoms": [{"genre": "MASCULIN", "orthographes": ["Youssouf", "Youssef"], "arabe": "يُوسُف"}],
                            "description": "Yûsuf (Joseph) : fils de Ya’qub.", "description_incomplete": False,
                            "page": 57, "partiel": False, "id": 1}])
        d = tempfile.mkdtemp()
        try:
            ch = X.exporter(rows, [{"page_pdf": 57, "statut": "ok"}], d)
            with open(ch["csv"], encoding="utf-8-sig") as f:
                brut = f.read().splitlines()
            self.assertTrue(brut[0].startswith("prénom_fr,prénom_arab_diacritiques,genre,description_en_français,"))
            self.assertTrue(brut[1].startswith('"Youssouf","يُوسُف","MASCULIN","Yûsuf (Joseph) : fils de Ya’qub.",'))
            with open(ch["csv"], encoding="utf-8-sig") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 2)
            with open(ch["json"], encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)), 2)
            from openpyxl import load_workbook
            wb = load_workbook(ch["xlsx"])
            self.assertEqual(wb.sheetnames, ["Prénoms", "À vérifier", "Pages"])
            self.assertEqual(wb["Prénoms"].max_row, 3)
        finally:
            shutil.rmtree(d)


# ---------------------------------------------------------------- bout en bout avec un faux serveur

PDF = next((p for p in (os.environ.get("PDF_LIVRE", ""), os.path.join(os.path.dirname(os.path.abspath(__file__)), "livre.pdf"),
                        "/mnt/user-data/uploads/lelivredesprenom0000geof.pdf") if p and os.path.exists(p)), "")


def entree(blocs, desc, inc=False, suite=False):
    return {"prenoms": [{"genre": g, "orthographes": o, "arabe": a} for g, o, a in blocs], "description": desc,
            "suite_page_precedente": suite, "description_incomplete": inc}


M, F = "MASCULIN", "FEMININ"
P55 = [
    entree([(M, ["Adam", "Adame"], "آدَم")], "Âdam (Adam) : premier homme et premier prophète. Signifie : roux, rouge comme le sang (dam), formé de terre rouge."),
    entree([(F, ["Hawa", "Haoua"], "حَـــوَّاء")], "Ḥawâ' (Ève) : épouse d'Âdam. De l'hébreu : vie, source de vie."),
    entree([(M, ["Chisse"], "شِيــــث")], "Shîth (Seth) : fils d'Âdam. Signifie : cadeau. Il fut le don fait par Dieu à Âdam pour compenser le meurtre d'Abel."),
    entree([(M, ["Idris"], "إِدْرِيــــس")], "Idrîs (Enoch) : signifie en hébreu docte, savant. La tradition islamique lui attribue l'usage initial du calame (plume de roseau) et l'origine de la couture. Un des quatre prophètes élevés aux cieux (Coran 19, 57)."),
]
P150 = [
    entree([(M, ["Nadil"], "نَاضِل"), (F, ["Nadila"], "نَاضِلَة")], "* Nâḍil / Nâḍila : qui l'emporte au combat."),
    entree([(M, ["Nidal"], "نِضَال")], "Niḍâl : combat, lutte, compétition."),
    entree([(M, ["Nahik"], "نَهِيك"), (F, ["Nahika"], "نَهِيكَة")], "* Nahîk / Nahîka : courageux, intrépide."),
    entree([(M, ["Habbar"], "هَبَّار")], "* Habbâr : sabre très tranchant."),
    entree([(M, ["Hazim"], "هَازِم")], "* Hâzim : qui terrasse l'adversaire, le met en déroute."),
    entree([(M, ["Haïbane"], "هَيْبَان"), (F, ["Haïbana"], "هَيْبَانَة")], "* Haybân / Haybâna : craint, respecté, vénéré."),
    entree([(F, ["Haïba"], "هَيْبَة")], "Hayba : respect, vénération mêlée de crainte."),
    entree([(M, ["Mouhib"], "مُهِيب"), (F, ["Mouhiba"], "مُهِيبَة")], "Muhîb / Muhîba : majestueux, imposant, solennel."),
]


class FauxLMStudio(BaseHTTPRequestHandler):
    reponses: dict = {}
    requetes: list = []
    mode = ""
    erreurs: set = set()
    dernier_corps: dict = {}
    modeles = ["text-embedding-nomic-embed-text-v1.5", "prism-ml/ternary-bonsai-27b-mlx-2bit"]

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._json(200, {"data": [{"id": m} for m in self.modeles]})

    def do_POST(self):
        corps = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        url = corps["messages"][0]["content"][0]["image_url"]["url"]
        png = __import__("base64").b64decode(url.split(",", 1)[1])
        cle = hashlib.md5(png).hexdigest()
        FauxLMStudio.requetes.append((corps["model"], cle))
        FauxLMStudio.dernier_corps = {k: v for k, v in corps.items() if k != "messages"}
        if FauxLMStudio.mode == "no_vision":
            return self._json(400, {"error": {"message": "Model does not support image input"}})
        if cle in FauxLMStudio.erreurs:
            return self._json(500, {"error": {"message": "boom"}})
        if cle not in self.reponses:
            return self._json(200, {"choices": [{"message": {"content": "{\"entrees\": []}"}, "finish_reason": "stop"}]})
        content, reasoning = self.reponses[cle]
        self._json(200, {"choices": [{"message": {"content": content, "reasoning_content": reasoning},
                                      "finish_reason": "stop"}], "usage": {"completion_tokens": 123}})


@unittest.skipUnless(PDF, "PDF du livre introuvable (variable PDF_LIVRE)")
class TestBoutEnBout(unittest.TestCase):
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
        FauxLMStudio.requetes = []
        FauxLMStudio.reponses = {}
        FauxLMStudio.mode = ""
        FauxLMStudio.erreurs = set()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def lire(self):
        with open(os.path.join(self.tmp, "prenoms_arabes.json"), encoding="utf-8") as f:
            return json.load(f)

    def cfg(self, decoupe=1):
        return X.Config(pdf=PDF, url=self.url, sortie=self.tmp, decoupe=decoupe)

    def md5(self, cfg, page, bande=1):
        return hashlib.md5(X.preparer_image(cfg, page, bande)[0]).hexdigest()

    def test_modele_resolu_meme_si_le_nom_differe(self):
        self.assertEqual(X.resoudre_modele(self.cfg()), "prism-ml/ternary-bonsai-27b-mlx-2bit")

    def test_pipeline_complet_reprise_et_score(self):
        cfg = self.cfg()
        # page 55 : réflexion balisée dans le contenu + JSON en bloc markdown ; page 150 : réflexion séparée
        FauxLMStudio.reponses[self.md5(cfg, 55)] = (
            "<think>je regarde la page… " + json.dumps(EXEMPLE_PROMPT) + "</think>\n```json\n"
            + json.dumps({"entrees": P55}, ensure_ascii=False) + "\n```", "")
        FauxLMStudio.reponses[self.md5(cfg, 150)] = (json.dumps({"entrees": P150}, ensure_ascii=False), "réflexion séparée")
        X.main([PDF, "--url", self.url, "--pages", "55,150", "--sortie", self.tmp])
        lignes = self.lire()
        self.assertEqual(len(lignes), 18)                                  # 6 + 12 prénoms, un par ligne
        par = {r["prénom_fr"]: r for r in lignes}
        self.assertEqual(par["Adame"]["prénom_arab_diacritiques"], "آدَم")
        self.assertEqual(par["Adame"]["prénoms_équivalents"], "Adam")
        self.assertEqual(par["Haoua"]["prénom_arab_diacritiques"], "حَوَّاء")     # tatwil retiré
        self.assertEqual(par["Haoua"]["genre"], "FEMININ")
        self.assertEqual(par["Nadila"]["forme_associée"], "Nadil")
        self.assertEqual(par["Nadila"]["description_en_français"], "Nâḍil / Nâḍila : qui l'emporte au combat.")
        self.assertEqual(par["Idris"]["équivalent_étranger"], "Enoch")
        self.assertEqual(par["Habbar"]["thème"], "4. Puissance et protection")
        s = VT.score(lignes, [55, 150])
        self.assertEqual((s["vedettes"], s["parasites"], s["genre"], s["arabe_exact"]), ("18/18", 0, "18/18", "16/16"))
        self.assertEqual(s["sens"], "16/16")
        for f in ("prenoms_arabes.csv", "prenoms_arabes.xlsx"):
            self.assertTrue(os.path.exists(os.path.join(self.tmp, f)))
        # relance : tout vient du cache, aucune requête de plus
        n = len(FauxLMStudio.requetes)
        X.main([PDF, "--url", self.url, "--pages", "55,150", "--sortie", self.tmp])
        self.assertEqual(len(FauxLMStudio.requetes), n)
        # --refaire : les deux pages sont redemandées
        X.main([PDF, "--url", self.url, "--pages", "55,150", "--sortie", self.tmp, "--refaire"])
        self.assertEqual(len(FauxLMStudio.requetes), n + 2)

    def test_demi_pages_avec_suite_de_definition(self):
        cfg = self.cfg(decoupe=2)
        haut = [P55[0], entree([(F, ["Hawa", "Haoua"], "حَوَّاء")], "Ḥawâ' (Ève) : épouse d'Âdam.", inc=True)]
        bas = [entree([], "De l'hébreu : vie, source de vie.", suite=True), P55[2], P55[3]]
        FauxLMStudio.reponses[self.md5(cfg, 55, 1)] = (json.dumps({"entrees": haut}, ensure_ascii=False), "")
        FauxLMStudio.reponses[self.md5(cfg, 55, 2)] = (json.dumps({"entrees": bas}, ensure_ascii=False), "")
        X.main([PDF, "--url", self.url, "--pages", "55", "--decoupe", "2", "--paralleles", "2", "--sortie", self.tmp])
        lignes = self.lire()
        self.assertEqual([r["prénom_fr"] for r in lignes], ["Adam", "Adame", "Hawa", "Haoua", "Chisse", "Idris"])
        self.assertEqual(lignes[2]["description_en_français"], "Ḥawâ' (Ève) : épouse d'Âdam. De l'hébreu : vie, source de vie.")
        self.assertNotIn("description_coupee", lignes[2]["à_vérifier"])

    def test_coupure_fixe_dans_un_blanc_sur_la_vraie_page(self):
        im = X.image_native(PDF, 55)
        net = X.nettoyer_page(im)
        haut, bas = X.decouper(net, im, 2)
        a = np.asarray(net)
        ligne_de_coupe = a[haut.height]
        self.assertLessEqual(int(((255 - ligne_de_coupe.min(axis=1)) > 60).sum()), 2)     # aucune encre sur la ligne de coupe

    def test_comparaison_avec_ancien_csv(self):
        cfg = self.cfg()
        FauxLMStudio.reponses[self.md5(cfg, 55)] = (json.dumps({"entrees": P55}, ensure_ascii=False), "")
        ancien = os.path.join(self.tmp, "ancien.csv")
        with open(ancien, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ID", "Prénom(s)", "Page (PDF)"])
            w.writerows([[1, "Adam / Adame", 55], [2, "Hawa / Haoua", 55], [3, "Chisse", 55], [4, "Idris", 55], [5, "Zorglub", 55]])
        X.main([PDF, "--url", self.url, "--pages", "55", "--sortie", self.tmp, "--ancien", ancien])
        with open(os.path.join(self.tmp, "comparaison_ancien.csv"), encoding="utf-8-sig") as f:
            ligne = list(csv.reader(f))[1]
        self.assertEqual((ligne[0], ligne[1], ligne[2], ligne[3], ligne[4]), ("55", "7", "6", "zorglub", ""))

    def test_parametres_de_generation_envoyes(self):
        cfg = self.cfg()
        FauxLMStudio.reponses[self.md5(cfg, 55)] = (json.dumps({"entrees": P55}, ensure_ascii=False), "")
        X.main([PDF, "--url", self.url, "--pages", "55", "--sortie", self.tmp])
        c = FauxLMStudio.dernier_corps
        self.assertEqual((c["temperature"], c["top_p"], c["top_k"], c["max_tokens"]), (0.6, 0.95, 20, 16000))
        self.assertEqual(c["model"], "prism-ml/ternary-bonsai-27b-mlx-2bit")

    def test_page_blanche_aucune_requete(self):
        X.main([PDF, "--url", self.url, "--pages", "134", "--sortie", self.tmp])
        self.assertEqual(FauxLMStudio.requetes, [])
        from openpyxl import load_workbook
        wb = load_workbook(os.path.join(self.tmp, "prenoms_arabes.xlsx"))
        lignes = list(wb["Pages"].iter_rows(values_only=True))
        self.assertEqual((lignes[1][0], lignes[1][2]), (134, "ok"))
        self.assertIn("page blanche", lignes[1][7])

    def test_cache_perime_si_le_prompt_change(self):
        cfg = self.cfg()
        FauxLMStudio.reponses[self.md5(cfg, 55)] = (json.dumps({"entrees": P55}, ensure_ascii=False), "")
        X.main([PDF, "--url", self.url, "--pages", "55", "--sortie", self.tmp])
        X.main([PDF, "--url", self.url, "--pages", "55", "--sortie", self.tmp])
        self.assertEqual(len(FauxLMStudio.requetes), 1)                    # 2e passage : cache
        prompt = os.path.join(self.tmp, "prompt.txt")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("consigne différente")
        X.main([PDF, "--url", self.url, "--pages", "55", "--sortie", self.tmp, "--prompt-fichier", prompt])
        self.assertEqual(len(FauxLMStudio.requetes), 2)                    # prompt changé : recalcul

    def test_modele_sans_vision_arrete_tout_sans_exporter(self):
        FauxLMStudio.mode = "no_vision"
        with self.assertRaises(SystemExit) as cm:
            X.main([PDF, "--url", self.url, "--pages", "55,56,57", "--sortie", self.tmp])
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "prenoms_arabes.csv")))

    def test_une_page_en_echec_nempeche_pas_les_autres(self):
        cfg = self.cfg()
        FauxLMStudio.reponses[self.md5(cfg, 150)] = (json.dumps({"entrees": P150}, ensure_ascii=False), "")
        FauxLMStudio.erreurs = {self.md5(cfg, 55)}
        X.main([PDF, "--url", self.url, "--pages", "55,150", "--sortie", self.tmp, "--essais", "2"])
        self.assertEqual({r["page_pdf"] for r in self.lire()}, {150})
        self.assertEqual(sum(1 for _, k in FauxLMStudio.requetes if k == self.md5(cfg, 55)), 2)      # 2 essais
        from openpyxl import load_workbook
        wb = load_workbook(os.path.join(self.tmp, "prenoms_arabes.xlsx"))
        statuts = {r[0]: r[2] for r in list(wb["Pages"].iter_rows(values_only=True))[1:]}
        self.assertEqual(statuts, {55: "échec", 150: "ok"})


if __name__ == "__main__":
    unittest.main()
