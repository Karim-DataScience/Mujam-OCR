# -*- coding: utf-8 -*-
"""Tests de extraire_prenoms_llm.py (v4) — sans LM Studio ni GPU : un FAUX serveur LM Studio simule le modèle
(API native + API OpenAI, streaming SSE, pannes : boucle de réflexion, silence, réglage « off » ignoré, erreurs HTTP...).

    python test_extraire_prenoms_llm.py
    python test_extraire_prenoms_llm.py -v

Les tests de bout en bout utilisent le vrai PDF (variable PDF_LIVRE, ou livre.pdf à côté du script) et sont ignorés
s'il est introuvable.
"""
import _thread
import argparse
import base64
import contextlib
import csv
import glob
import hashlib
import io
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import numpy as np
from PIL import Image

# la configuration de l'utilisateur ne doit jamais influencer les tests
os.environ["PRENOMS_CONFIG"] = os.path.join(tempfile.gettempdir(), "prenoms_config_tests_absent.json")

import extraire_prenoms_llm as X
import lecture_hybride as LH
import mise_en_page as MP
import verite_terrain as VT

EXEMPLE_PROMPT = {"entrees": [
    {"prenoms": [{"genre": "MASCULIN", "orthographes": ["Youssouf", "Youssef"], "arabe": "يُوسُف"},
                 {"genre": "FEMININ", "orthographes": ["Karim", "Karima"], "arabe": "x"}],
     "description": "d", "suite_page_precedente": False, "description_incomplete": False}]}


def rep(content="", reasoning="", finish="stop"):
    return {"content": content, "reasoning": reasoning, "finish": finish, "tokens": 10}


class TestAuditV43(unittest.TestCase):
    """Défauts trouvés à l'audit du 20 oct. 2026 : boucle non détectée (p. 98), doublons exportés, relecture ciblée."""

    @staticmethod
    def _ent(n):
        return ('{"prenoms":[{"genre":"MASCULIN","orthographes":["%s"],"arabe":"عَبْد"}],'
                '"description":"Abd al-X : le serviteur du Vivant et du Très-Compatissant, nom composé."},') % n

    def test_boucle_de_plusieurs_entrees_detectee(self):
        noms = ["Abdelhannane", "Abdelhaye", "Abdelmouhy", "Abdelkhaliq", "Abdelrahman", "Abderrahim"]
        txt = '{"entrees":[' + "".join(self._ent(n) for n in noms * 3)
        self.assertTrue(X.detecter_boucle(txt))
        self.assertTrue(X.detecter_boucle_entrees(txt))
        self.assertFalse(X.detecter_boucle_entrees('{"entrees":[' + "".join(self._ent(n) for n in noms * 2)))

    def test_dedoublonnage(self):
        noms = ["Abdelhannane", "Abdelhaye", "Abdelmouhy"]
        brut = json.loads(("[" + "".join(self._ent(n) for n in noms * 4)).rstrip(",") + "]")
        self.assertEqual(len(X.normaliser_page({"entrees": brut})), 3)

    def test_relire_arabe_adopte_seulement_une_lecture_plus_coherente(self):
        import lecture_hybride as LH
        from PIL import Image
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = X.Config(pdf="faux.pdf", sortie=tmp, pages=[58], essais=2)
        ent = lambda nom, ar, desc: {"prenoms": [{"genre": "MASCULIN", "orthographes": [nom], "arabe": ar}], "description": desc, "description_incomplete": False}
        d = {"page": 58, "bande": 1, "decoupe": 1, "empreinte": X.empreinte(cfg), "statut": "ok", "avertissements": [], "entrees": [
            ent("Yaqoub", "يَقُوب", "Ya'qūb (Jacob) : fils d'Ishâq, petit-fils d'Ibrâhîm."),
            ent("Lout", "لُوط", "Lût (Loth) : prophète envoyé à Sodome et Gomorrhe."),
            ent("Idriss", "إِدْرِيس", "Idrîs (Hénoch) : prophète cité dans le Coran, élevé au ciel.")]}
        X.ecrire_atomique(X.chemin_cache(cfg, 58, 1), json.dumps(d, ensure_ascii=False))
        blocs = lambda nom: [LH.BlocLu(genre="MASCULIN", orthographes=[nom], arabe=(0, 0, 10, 10))]
        lec = LH.PageLue(entrees=[LH.EntreeLue(blocs=blocs(n)) for n in ("Yaqoub", "Lout", "Idriss")], problemes=[], page_texte=False)
        appels = []

        def faux_llm(cfg_, info, png, systeme, texte, **kw):
            appels.append(kw.get("penser"))
            # 1re tentative : une lecture toujours fausse ; 2e : la bonne
            v = "يَقُوب" if len(appels) == 1 else "يَعْقُوبٌ"
            return {"content": json.dumps({"A1": v}, ensure_ascii=False), "reasoning": "", "abandon": "", "finish": "stop", "tokens": 20,
                    "tokens_reflexion": 150, "tokens_entree": 100, "tps": 30.0, "ttft": 0.2, "erreur": ""}

        with mock.patch.object(X, "resoudre_modele", return_value=X.InfoModele(id="faux")), mock.patch.object(X, "afficher_info"), \
                mock.patch.object(X, "image_native", return_value=Image.new("RGB", (50, 50), "white")), \
                mock.patch.object(X, "nettoyer_page", side_effect=lambda im, *a, **k: im), mock.patch.object(LH, "lire_page", return_value=lec), \
                mock.patch.object(LH, "disponible", return_value=(True, "ok")), mock.patch.object(X, "appeler_llm", side_effect=faux_llm), \
                mock.patch.object(X, "_journaliser_essai"):
            st = X.relire_arabe(cfg)
        self.assertEqual(st["cibles"], 1)                     # seul Yaqoub était incohérent
        self.assertEqual(st["corrigees"], 1)
        self.assertEqual(len(appels), 2)                      # la 1re lecture (inchangée) est refusée, la 2e adoptée
        self.assertTrue(all(appels))                          # réflexion activée
        with open(X.chemin_cache(cfg, 58, 1), encoding="utf-8") as f:
            apres = json.load(f)
        self.assertEqual(apres["entrees"][0]["prenoms"][0]["arabe"], "يَعْقُوب")      # sans tanwin final
        self.assertEqual(apres["entrees"][1]["prenoms"][0]["arabe"], "لُوط")          # intact
        self.assertEqual(apres["relectures"][0]["avant"], "يَقُوب")
    def test_modele_qui_ne_rend_rien_alors_que_la_mise_en_page_voit_des_entrees(self):
        cfg = X.Config(pdf="faux.pdf", sortie=tempfile.mkdtemp(), pages=[81])
        self.addCleanup(shutil.rmtree, cfg.sortie, True)
        etat = X.Etat(cfg, X.InfoModele(id="faux"))
        etat.hybride = True
        etat.attendus[81] = 4
        sauve = {"statut": "ok", "entrees": [{"prenoms": []}] * 4, "methode": "local"}
        appels = []

        def faux_hybride(c, e, page, local_seul=False):
            appels.append(local_seul)
            return sauve if local_seul else None

        with mock.patch.object(X, "_traiter_page_hybride", side_effect=faux_hybride), \
                mock.patch.object(X, "_traiter_chunk", return_value={"statut": "ok", "entrees": [], "avertissements": []}):
            res = X._traiter_dispatch(cfg, etat, 81, 1)
        self.assertIs(res, sauve)                           # les 4 entrées ne sont plus perdues
        self.assertEqual(appels, [False, True])

    def test_option_cli_relire_arabe(self):
        a = X.creer_parser().parse_args(["livre.pdf", "--relire-arabe"])
        self.assertTrue(a.relire_arabe)



class TestVerifArabe(unittest.TestCase):
    """v4.3 : contrôle de l'arabe lu par le modèle à l'aide de la translittération de la tête."""

    def test_lectures_correctes_non_signalees(self):
        import verif_arabe as VA
        for tr, ar in [("Ya'qūb", "يَعْقُوب"), ("Ayyûb", "أَيُّوب"), ("Yúsuf", "يُوسُف"), ("Lût", "لُوط"), ("Hud", "هود"), ("Sayyid / Sayyida", "سَيِّد"),
                       ("Fâdil / Fâdila", "فَاضِلَة"), ("Sayf ad-Dîn", "سَيْف الدين"), ("Mishkât", "مَشْكَاة"), ("Âşım", "عَاصِم"), ("Ishâq", "إِسْحَاق")]:
            self.assertFalse(VA.incoherent(tr, ar)[0], (tr, ar))

    def test_lectures_fausses_signalees(self):
        import verif_arabe as VA
        for tr, ar in [("Ya'qūb", "يَقُوب"), ("Ayyûb", "أُوُوب"), ("Lût", "لوك"), ("Âdām", "آد"), ("Mukhtâr", "زُبَيْدَة")]:
            ko, indice = VA.incoherent(tr, ar)
            self.assertTrue(ko, (tr, ar))
            self.assertIn("attendu", indice)

    def test_non_evaluable(self):
        import verif_arabe as VA
        self.assertEqual(VA.coherence("", "يُوسُف")[0], -1.0)
        self.assertEqual(VA.coherence("Yûsuf", "")[0], -1.0)

    def test_tanwin_final_retire_seulement_en_fin_de_mot(self):
        import verif_arabe as VA
        self.assertEqual(VA.retirer_tanwin_final("يُوسُفٌ"), "يُوسُف")
        self.assertEqual(VA.retirer_tanwin_final("مَدْيَانٍ / زُلَيْخَةً"), "مَدْيَان / زُلَيْخَة")
        self.assertEqual(VA.retirer_tanwin_final("دُعَاءً"), "دُعَاء")
        self.assertEqual(VA.retirer_tanwin_final("مُحَمَّد"), "مُحَمَّد")

    def test_exploser_pose_le_drapeau_et_retire_le_tanwin(self):
        e = {"id": 1, "page": 58, "description": "Ya'qūb (Jacob) : fils d'Ishâq, petit-fils d'Ibrâhîm.", "description_incomplete": False, "partiel": False,
             "prenoms": [{"genre": "MASCULIN", "orthographes": ["Yaqoub"], "arabe": "يَقُوب"}]}
        r = X.exploser([e])[0]
        self.assertIn("arabe_incoherent_translit", r["à_vérifier"])
        e["prenoms"][0]["arabe"] = "يَعْقُوبٌ"
        r = X.exploser([e])[0]
        self.assertNotIn("arabe_incoherent_translit", r["à_vérifier"])
        self.assertEqual(r["prénom_arab_diacritiques"], "يَعْقُوب")


class TestTetePlausible(unittest.TestCase):
    """v4.2.1 : le bruit d'OCR sur la « tête » ne doit plus provoquer de repli sur la page entière (journal du 20 oct. 2026)."""

    def test_cas_du_journal(self):
        for d in ["Nüûh (Noé) :'de l’hébreu : repos.", "Dhüûün-n-nûn :lhomnie au grand poisson ;", "# Shâfiq : compalissant.",
                  "*Abd-Alâh : le serviteur de Dieu", "Al-*Arabi : l'Arabe par excellence.", "# Wäsil: qui umt;",
                  "Al-Yasa® (Elisée) : prophète", "Shaymâ” : sœur de lait du Prophète.", "“Abd-Allâh : le serviteur de Dieu", "“Azîz : cher.", "Adam : premier homme."]:
            self.assertTrue(LH.tete_plausible(d), d)

    def test_vrai_texte_libre_reste_refuse(self):
        for d in ["“ Mouhammad n'est le père d'aucun homme parmi vous ; mais il est l'envoyé de Dieu",
                  "Peu avant sa mort, le Prophète (#) répéta par trois fois", "Les noms des prophètes que l'on trouve ici sont cités"]:
            self.assertFalse(LH.tete_plausible(d), d)

    def test_nettoyer_debut(self):
        self.assertEqual(LH.nettoyer_debut("# Shâfiq : x"), "Shâfiq : x")
        self.assertEqual(LH.nettoyer_debut("Al-*Arabi : x"), "Al-ʿArabi : x")
        self.assertEqual(LH.nettoyer_debut("mot* : x"), "mot* : x")
        self.assertEqual(LH.nettoyer_debut("“Azîz : cher"), "ʿAzîz : cher")


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
        copie = {"entrees": [{"prenoms": [{"genre": "MASCULIN", "orthographes": ["Karim"], "arabe": "كَرِيم"}],
                              "description": X.EXEMPLE_DESC}]}
        self.assertTrue(X.est_exemple(X.normaliser_page(copie)))
        autre = {"entrees": [{"prenoms": [{"genre": "MASCULIN", "orthographes": ["Karim"], "arabe": ""}],
                              "description": "Karîm : noble."}]}
        self.assertFalse(X.est_exemple(X.normaliser_page(autre)))
        self.assertFalse(X.est_exemple([]))


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




# ---------------------------------------------------------------- tests unitaires du client et du pipeline v4

class TestBoucle(unittest.TestCase):
    def test_phrase_repetee(self):
        t = "Je vérifie encore l'arabe de ce prénom pour être sûr. " * 20
        self.assertTrue(X.detecter_boucle(t))
        self.assertTrue(X.detecter_boucle(t, prose=True))

    def test_texte_court_ou_normal_non_detecte(self):
        self.assertFalse(X.detecter_boucle("abcabcabc"))
        rnd = random.Random(1)
        vocab = [f"mot{i}" for i in range(300)]
        prose = " ".join(rnd.choice(vocab) for _ in range(700))
        self.assertFalse(X.detecter_boucle(prose, prose=True))

    def test_variantes_detectees_en_prose_seulement(self):
        t = " ".join(f"Je vérifie encore l'entrée numéro {i} pour être sûr que tout est correct." for i in range(60))
        self.assertTrue(X.detecter_boucle(t, prose=True))
        self.assertFalse(X.detecter_boucle(t))

    def test_json_realiste_non_detecte(self):
        self.assertFalse(X.detecter_boucle(json.dumps({"entrees": P150 + P55}, ensure_ascii=False)))

    def test_longue_suite_de_caracteres_identiques(self):
        self.assertTrue(X.detecter_boucle("x" * 600))


class TestPhaseOpenAI(unittest.TestCase):
    def sortie(self, morceaux):
        ph, out = X.PhaseOpenAI(), []
        for m in morceaux:
            out += ph.separer(m)
        out += ph.vider()
        ref = "".join(t for k, t in out if k == "reflexion")
        rep_ = "".join(t for k, t in out if k == "reponse")
        return ref, rep_

    def test_json_direct(self):
        self.assertEqual(self.sortie(['{"entrees"', ': []}']), ("", '{"entrees": []}'))

    def test_balise_think_ouverte(self):
        ref, rep_ = self.sortie(["<thi", "nk>Je regarde la page.", "</think>", '{"a": 1}'])
        self.assertEqual((ref, rep_), ("Je regarde la page.", '{"a": 1}'))

    def test_reflexion_implicite_balise_fermante_coupee_entre_deux_morceaux(self):
        ref, rep_ = self.sortie(["Je regarde la page et je réfléchis longuement.", "</thi", "nk>", "\n{\"a\": 1}"])
        self.assertEqual(ref, "Je regarde la page et je réfléchis longuement.")
        self.assertEqual(rep_.strip(), '{"a": 1}')

    def test_bloc_markdown_est_une_reponse(self):
        ref, rep_ = self.sortie(["```json\n", '{"a": 1}', "\n```"])
        self.assertEqual(ref, "")
        self.assertIn('{"a": 1}', rep_)

    def test_reponse_courte_videe_a_la_fin(self):
        self.assertEqual(self.sortie(["OK"]), ("", "OK"))


class _FauxFlux:
    def __init__(self, data: bytes):
        self.f = io.BytesIO(data)

    def readline(self):
        return self.f.readline()


class TestSSE(unittest.TestCase):
    def test_lignes_sse(self):
        data = b'event: a\ndata: {"x":1}\n\n: commentaire\nevent: b\ndata: {"y":2}\n\ndata: [DONE]\n\n'
        self.assertEqual(list(X._lignes_sse(_FauxFlux(data))), [("a", '{"x":1}'), ("b", '{"y":2}'), ("", "[DONE]")])

    def test_flux_natif(self):
        evs = [("prompt_processing.progress", {"progress": 0.5}), ("reasoning.delta", {"content": "pense"}),
               ("message.delta", {"content": "{}"}), ("error", {"error": {"message": "boum"}}),
               ("chat.end", {"result": {"stats": {"input_tokens": 12}}})]
        data = "".join(f"event: {t}\ndata: {json.dumps(dict(d, type=t))}\n\n" for t, d in evs).encode()
        self.assertEqual(list(X._flux_natif(_FauxFlux(data))),
                         [("prefill", 0.5), ("reflexion", "pense"), ("reponse", "{}"), ("erreur", "boum"),
                          ("fin", {"stats": {"input_tokens": 12}})])

    def test_flux_openai_usage_et_raison(self):
        chunks = [{"choices": [{"delta": {"reasoning_content": "hm"}}]}, {"choices": [{"delta": {"content": "{}"}}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}]
        data = ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
        evs = list(X._flux_openai(_FauxFlux(data)))
        self.assertEqual(evs[:2], [("reflexion", "hm"), ("reponse", "{}")])
        self.assertEqual(evs[-1], ("fin", {"finish": "stop", "stats": {"input_tokens": 7, "total_output_tokens": 3}}))


class TestUrlEtCommande(unittest.TestCase):
    def test_cible(self):
        cfg = X.Config(url="http://127.0.0.1:1234/v1")
        self.assertEqual(X._cible(cfg), ("http", "127.0.0.1", 1234, ""))
        self.assertEqual(X._cible(X.Config(url="localhost:5000/")), ("http", "localhost", 5000, ""))
        self.assertEqual(X._chemin(X.Config(url="http://h:1/proxy/v1/"), "/api/v1/chat"), "/proxy/api/v1/chat")

    def test_commande_lms_et_arrondi(self):
        self.assertEqual(X.arrondi_1024(9000), 9216)
        self.assertEqual(X.arrondi_1024(1024), 1024)
        c = X.commande_lms("prism-ml/bonsai-27b", 12288, 1)
        self.assertEqual(c, "lms load prism-ml/bonsai-27b --context-length 12288 --parallel 1 --gpu max "
                            "--identifier prism-ml/bonsai-27b -y")


class TestBudget(unittest.TestCase):
    def etat(self, ctx, entree, parallele=1, reflexion="non"):
        cfg = X.Config(paralleles=parallele, reflexion=reflexion)
        info = X.InfoModele(id="m", cle="m", natif=True, ctx=ctx, options_reflexion=["off", "on"])
        etat = X.Etat(cfg, info)
        etat.tokens_entree = entree
        return cfg, etat

    def test_sortie_bornee_par_le_contexte(self):
        cfg, etat = self.etat(12000, 6000)
        with contextlib.redirect_stdout(io.StringIO()):
            X.budgeter(cfg, etat)
        self.assertEqual(etat.sortie_max, 12000 - 6000 - 128)

    def test_contexte_trop_court(self):
        cfg, etat = self.etat(6500, 6000)
        with self.assertRaises(X.ErreurFatale) as cm:
            X.budgeter(cfg, etat)
        self.assertIn("Contexte trop court", str(cm.exception))
        self.assertIn("lms load", str(cm.exception))

    def test_parallelisme_reduit(self):
        cfg, etat = self.etat(12000, 3000, parallele=3)
        with contextlib.redirect_stdout(io.StringIO()):
            X.budgeter(cfg, etat)
        self.assertEqual(cfg.paralleles, 1)
        cfg, etat = self.etat(30000, 3000, parallele=3)
        with contextlib.redirect_stdout(io.StringIO()):
            X.budgeter(cfg, etat)
        self.assertEqual(cfg.paralleles, 3)

    def test_modes_de_reflexion(self):
        _, e = self.etat(32768, 3000, reflexion="auto")
        self.assertFalse(e.penser)                                      # LM Studio permet « off » : réflexion coupée
        _, e = self.etat(32768, 3000, reflexion="oui")
        self.assertTrue(e.penser)
        self.assertEqual(e.budget(False), 200)
        self.assertEqual(e.budget(True), max(1000, e.sortie_max - 2500))
        cfg = X.Config(reflexion="auto")
        e = X.Etat(cfg, X.InfoModele(id="m", natif=True, options_reflexion=[]))
        self.assertTrue(e.penser)                                       # pas de réglage exposé : réflexion bornée


class TestCacheEtStatuts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = X.Config(sortie=self.tmp, pages=[55, 56])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def ecrire(self, page, **champs):
        d = {"page": page, "bande": 1, "statut": "ok", "empreinte": X.empreinte(self.cfg), "max_tokens": 8000,
             "duree": 3.0, "tokens": 10, "entrees": []}
        d.update(champs)
        X.ecrire_atomique(X.chemin_cache(self.cfg, page, 1), json.dumps(d))

    def test_lire_cache(self):
        self.assertEqual(X.lire_cache(self.cfg, 55, 1), (None, False))                  # absent
        self.ecrire(55)
        d, perime = X.lire_cache(self.cfg, 55, 1)
        self.assertEqual((d["page"], perime), (55, False))
        self.cfg.prompt = "autre prompt"
        self.assertEqual(X.lire_cache(self.cfg, 55, 1), (None, True))                   # prompt changé : périmé

    def test_partiel_retente_avec_un_budget_plus_grand(self):
        self.ecrire(55, statut="partiel", max_tokens=4000)
        self.assertEqual(X.lire_cache(self.cfg, 55, 1), (None, False))
        self.cfg.max_tokens = 3000
        self.assertIsNotNone(X.lire_cache(self.cfg, 55, 1)[0])

    def test_empreinte_ignore_les_reglages_de_generation(self):
        a = X.empreinte(self.cfg)
        self.cfg.temperature, self.cfg.max_tokens, self.cfg.reflexion = 0.9, 123, "oui"
        self.assertEqual(a, X.empreinte(self.cfg))
        self.cfg.tokens_image = 1000
        self.assertNotEqual(a, X.empreinte(self.cfg))

    def test_construire_statuts(self):
        res = {"page": 55, "bande": 1, "statut": "ok", "entrees": [], "duree": 2.0, "tokens": 5, "tokens_reflexion": 0,
               "tokens_entree": 100, "tps": 20.0}
        rows, infos, _ = X.construire(self.cfg, {(55, 1): res}, {(56, 1): "ErreurLLM: HTTP 500 : boom"})
        self.assertEqual([(i["page_pdf"], i["statut"]) for i in infos], [(55, "ok"), (56, "échec")])
        self.assertIn("boom", infos[1]["remarque"])
        rows, infos, _ = X.construire(self.cfg, {(55, 1): res}, {})
        self.assertEqual(infos[1]["statut"], "à faire")
        self.assertEqual(infos[0]["tok_s"], 20.0)


class TestGPU(unittest.TestCase):
    def test_sans_nvidia_smi(self):
        with mock.patch.object(X.shutil, "which", return_value=None):
            g = X.EchantillonGPU()
        self.assertFalse(g.disponible)
        self.assertIsNone(g.instantane())
        self.assertIsNone(g.resume())

    def test_lecture_et_resume(self):
        sortie = "37, 4321, 12227, 55.2, 115.00, 2100, 61, NVIDIA GeForce RTX 5070 Ti Laptop GPU\n"
        faux = mock.Mock(stdout=sortie)
        with mock.patch.object(X.shutil, "which", return_value="nvidia-smi"), \
                mock.patch.object(X.subprocess, "run", return_value=faux):
            g = X.EchantillonGPU(0.01)
            m = g.instantane()
            self.assertEqual((m["util"], m["mem"], m["mem_tot"], m["nom"]), (37.0, 4321.0, 12227.0, "NVIDIA GeForce RTX 5070 Ti Laptop GPU"))
            with g:
                time.sleep(0.15)
        r = g.resume()
        self.assertIsNotNone(r)
        self.assertEqual((r["util_moy"], r["mem_max"], r["mem_tot"]), (37.0, 4321.0, 12227.0))

    def test_valeurs_non_disponibles(self):
        faux = mock.Mock(stdout="10, 100, 12000, [N/A], [N/A], 1500, 50, GPU\n")
        with mock.patch.object(X.shutil, "which", return_value="nvidia-smi"), \
                mock.patch.object(X.subprocess, "run", return_value=faux):
            m = X.EchantillonGPU().instantane()
        self.assertIsNone(m["pw"])
        self.assertEqual(m["util"], 10.0)


# ---------------------------------------------------------------- journal, configuration, rapport, pont moteur/interface

class TestJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        X.JOURNAL.fermer()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def lire(chemin):
        with open(chemin, encoding="utf-8") as f:
            return f.read()

    def test_attente_avant_ouverture_puis_ecriture(self):
        j = X.Journal()
        j.ecrire("INFO", "avant l'ouverture")
        chemin = j.ouvrir(self.tmp, "Mode Test")
        self.assertTrue(chemin.endswith("_mode-test.log"))
        j.ecrire("DEBUG", "détail\nsur deux lignes")
        j.evenement("essai", a=1, b="é")
        j.fermer()
        txt = self.lire(chemin)
        self.assertIn("INFO  [MainThread] avant l'ouverture", txt)
        self.assertIn("DEBUG [MainThread] détail\n    sur deux lignes", txt)
        ev = [json.loads(ligne) for ligne in self.lire(chemin[:-4] + ".jsonl").splitlines()]
        self.assertEqual((ev[0]["type"], ev[0]["a"], ev[0]["b"]), ("essai", 1, "é"))
        self.assertTrue({"t", "dt", "fil"} <= set(ev[0]))

    def test_niveau_info_filtre_le_debug(self):
        j = X.Journal()
        chemin = j.ouvrir(self.tmp, "x", "INFO")
        j.ecrire("DEBUG", "bruit")
        j.ecrire("INFO", "utile")
        j.fermer()
        txt = self.lire(chemin)
        self.assertNotIn("bruit", txt)
        self.assertIn("utile", txt)

    def test_purge_des_anciens_journaux(self):
        for i in range(6):
            for ext in (".log", ".jsonl"):
                open(os.path.join(self.tmp, f"2020010{i}_000000_x{ext}"), "w").close()
        j = X.Journal()
        j.ouvrir(self.tmp, "x", garder=3)
        j.fermer()
        self.assertEqual(len([n for n in os.listdir(self.tmp) if n.endswith(".log")]), 3)
        self.assertEqual(len([n for n in os.listdir(self.tmp) if n.endswith(".jsonl")]), 3)

    def test_deux_sessions_dans_la_meme_seconde(self):
        j1, j2 = X.Journal(), X.Journal()
        a, b = j1.ouvrir(self.tmp, "x"), j2.ouvrir(self.tmp, "x")
        j1.fermer()
        j2.fermer()
        self.assertNotEqual(a, b)

    def test_les_secrets_sont_masques_partout(self):
        X.SECRETS.add("sk-secret-123")
        try:
            chemin = X.JOURNAL.ouvrir(self.tmp, "m")
            with contextlib.redirect_stdout(io.StringIO()) as o:
                X.log("jeton sk-secret-123 utilisé")
            X.evenement("e", jeton="sk-secret-123")
            X.JOURNAL.fermer()
        finally:
            X.SECRETS.discard("sk-secret-123")
        tout = self.lire(chemin) + self.lire(chemin[:-4] + ".jsonl") + o.getvalue()
        self.assertNotIn("sk-secret-123", tout)
        self.assertIn("***", tout)

    def test_trace_complete_dans_le_fichier_pas_dans_la_console(self):
        chemin = X.JOURNAL.ouvrir(self.tmp, "exc")
        try:
            raise RuntimeError("boum")
        except RuntimeError as e:
            with contextlib.redirect_stdout(io.StringIO()) as o:
                X.log_exception("contexte", e)
        X.JOURNAL.fermer()
        txt = self.lire(chemin)
        self.assertIn("Traceback", txt)
        self.assertIn("RuntimeError: boum", txt)
        self.assertNotIn("Traceback", o.getvalue())
        self.assertIn("contexte : RuntimeError: boum", o.getvalue())
        ev = [json.loads(ligne) for ligne in self.lire(chemin[:-4] + ".jsonl").splitlines()]
        self.assertEqual(ev[-1]["type"], "exception")
        self.assertIn("boum", ev[-1]["message"])

    def test_ecouteurs_et_mode_muet(self):
        recu = []
        X.ECOUTEURS.append(lambda niveau, ligne: recu.append((niveau, ligne)))
        X.MUET = True
        try:
            with contextlib.redirect_stdout(io.StringIO()) as o:
                X.log("bonjour")
                X.debug("silence")
        finally:
            X.MUET = False
            X.ECOUTEURS.clear()
        self.assertEqual(o.getvalue(), "")
        self.assertEqual(len(recu), 1)
        self.assertIn("bonjour", recu[0][1])


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.chemin = os.path.join(self.tmp, "cfg.json")

    def tearDown(self):
        X.JOURNAL.fermer()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valeurs_effectives(self):
        with open(self.chemin, "w", encoding="utf-8") as f:
            json.dump({"url": "http://h:9", "reflexion": "oui", "inconnu": 1}, f)
        v = X.valeurs_effectives(self.chemin)
        self.assertEqual((v["url"], v["reflexion"], v["tokens_image"]), ("http://h:9", "oui", 2500))
        self.assertNotIn("inconnu", v)

    def test_fichier_absent_ou_illisible(self):
        self.assertEqual(X.charger_config(os.path.join(self.tmp, "absent.json")), {})
        with open(self.chemin, "w", encoding="utf-8") as f:
            f.write("{pas du json")
        with contextlib.redirect_stdout(io.StringIO()) as o:
            self.assertEqual(X.charger_config(self.chemin), {})
        self.assertIn("illisible", o.getvalue())

    def test_ecrire_ne_garde_que_les_cles_de_config(self):
        X.ecrire_config(self.chemin, {"url": "u", "refaire": True, "pdf": "p.pdf"})
        with open(self.chemin, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"pdf": "p.pdf", "url": "u"})

    def test_options_de_la_ligne_de_commande_surchargent_et_ecrire_config_fusionne(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(X.main(["--config", self.chemin, "--url", "http://z:1", "--ecrire-config"]), 0)
            self.assertEqual(X.main(["--config", self.chemin, "--reflexion", "oui", "--ecrire-config"]), 0)
        with open(self.chemin, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual((d["url"], d["reflexion"]), ("http://z:1", "oui"))

    def test_construire_cfg(self):
        a = argparse.Namespace(**X.valeurs_effectives(self.chemin))
        cfg = X.construire_cfg(a)
        self.assertEqual((cfg.pages[0], cfg.pages[-1], cfg.decoupe, cfg.reflexion), (53, 338, 1, "auto"))
        self.assertTrue(cfg.sortie.endswith("sortie_llm"))
        a.corps_extra = "{pas json"
        with self.assertRaises(ValueError):
            X.construire_cfg(a)
        a.corps_extra = "[1, 2]"
        with self.assertRaises(ValueError):
            X.construire_cfg(a)
        a.corps_extra = '{"min_p": 0.1}'
        self.assertEqual(X.construire_cfg(a).corps_extra, {"min_p": 0.1})

    def test_la_cle_d_api_est_masquee_dans_les_journaux(self):
        a = argparse.Namespace(**X.valeurs_effectives(self.chemin))
        a.cle_api = "azerty-1234"
        try:
            cfg = X.construire_cfg(a)
            self.assertIn("azerty-1234", X.SECRETS)
            self.assertEqual(X.cfg_pour_log(cfg)["cle_api"], "***")
            self.assertNotIn("azerty-1234", json.dumps(X.cfg_pour_log(cfg)))
        finally:
            X.SECRETS.discard("azerty-1234")

    def test_sans_argument_et_sans_terminal_ou_avec_cli_affiche_l_aide(self):
        for argv in ([], ["--cli"]):
            with mock.patch.object(X, "_terminal_interactif", return_value=False), \
                    mock.patch.object(X, "lancer_tui", return_value=0) as lt, contextlib.redirect_stdout(io.StringIO()) as o:
                self.assertEqual(X.main(argv), 0)
            lt.assert_not_called()
            self.assertIn("usage:", o.getvalue())
        with mock.patch.object(X, "_terminal_interactif", return_value=True), \
                mock.patch.object(X, "lancer_tui", return_value=0) as lt, contextlib.redirect_stdout(io.StringIO()) as o:
            self.assertEqual(X.main(["--cli"]), 0)             # même dans un terminal : --cli = pas de TUI
        lt.assert_not_called()
        self.assertIn("usage:", o.getvalue())

    def test_lancement_automatique_de_la_tui_sans_argument(self):
        with mock.patch.object(X, "_terminal_interactif", return_value=True), \
                mock.patch.object(X, "lancer_tui", return_value=0) as lt:
            self.assertEqual(X.main([]), 0)
            lt.assert_called_once()
            lt.reset_mock()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    X.main(["--cli", "--sortie", self.tmp])           # --cli : pas de TUI, donc erreur d'usage (pas de PDF)
            lt.assert_not_called()
            self.assertEqual(X.main(["--tui"]), 0)


class TestRapport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        X.JOURNAL.fermer()
        X.SECRETS.discard("topsecret-123")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_contenu_du_zip_sans_secret(self):
        cfg = X.Config(sortie=self.tmp, pages=[55, 56], cle_api="topsecret-123")
        X.SECRETS.add("topsecret-123")
        X.JOURNAL.ouvrir(os.path.join(self.tmp, "logs"), "extraction")
        with contextlib.redirect_stdout(io.StringIO()):
            X.log("la clé topsecret-123 ne doit pas fuiter")
            X.debug("détail")
        X.evenement("requete", chunk="p055")
        X.JOURNAL.fermer()
        X.ecrire_atomique(X.chemin_cache(cfg, 55, 1), json.dumps(
            {"page": 55, "bande": 1, "statut": "ok", "empreinte": X.empreinte(cfg), "max_tokens": 8000, "duree": 12.5,
             "tokens": 900, "tokens_reflexion": 0, "tokens_entree": 3000, "tps": 15.0, "ttft": 4.0, "penser": False,
             "avertissements": ["a"], "entrees": [{}]}))
        X.ecrire_atomique(os.path.join(self.tmp, "cache", "brut", "p056_essai1.txt"), "abandon=boucle topsecret-123\n...")
        chemin_cfg = os.path.join(self.tmp, "prenoms_config.json")
        with open(chemin_cfg, "w", encoding="utf-8") as f:
            json.dump({"cle_api": "topsecret-123", "url": "u"}, f)
        z = X.creer_rapport(cfg, chemin_cfg)
        self.assertTrue(os.path.exists(z) and z.endswith(".zip"))
        with zipfile.ZipFile(z) as zf:
            contenu = {n: zf.read(n).decode("utf-8", "replace") for n in zf.namelist()}
        for attendu in ("LISEZMOI.txt", "environnement.json", "configuration.json", "pages.csv", "prenoms_config.json",
                        "brut/p056_essai1.txt"):
            self.assertIn(attendu, contenu)
        self.assertTrue(any(n.startswith("logs/") and n.endswith(".log") for n in contenu))
        self.assertTrue(any(n.startswith("logs/") and n.endswith(".jsonl") for n in contenu))
        for n, texte in contenu.items():
            self.assertNotIn("topsecret-123", texte, n)
        lignes = contenu["pages.csv"].splitlines()
        self.assertEqual(lignes[1], "55,1,ok,False,False,1,12.5,900,0,3000,15.0,4.0,False,a")
        self.assertTrue(lignes[2].startswith("56,1,a_faire"))
        self.assertIn("python", json.loads(contenu["environnement.json"]))

    def test_option_rapport(self):
        with contextlib.redirect_stdout(io.StringIO()) as o:
            code = X.main(["--rapport", "--sortie", self.tmp, "--config", os.path.join(self.tmp, "absent.json")])
        self.assertEqual(code, 0)
        self.assertEqual(len(glob.glob(os.path.join(self.tmp, "rapports", "rapport_debug_*.zip"))), 1)
        self.assertIn("rapport de debug", o.getvalue())


class TestControle(unittest.TestCase):
    def test_registre_d_activite(self):
        c = X.Controle()
        c.maj_actif("p055", tokens=10)
        c.maj_actif("p055", tps=3.5)
        c.maj_actif("p056", tokens=1)
        s = c.instantane()
        self.assertEqual(set(s["actifs"]), {"p055", "p056"})
        self.assertEqual((s["actifs"]["p055"]["tokens"], s["actifs"]["p055"]["tps"]), (10, 3.5))
        c.fin_actif("p055")
        self.assertEqual(set(c.instantane()["actifs"]), {"p056"})
        s["actifs"]["p056"]["tokens"] = 999                       # l'instantané est une copie
        self.assertEqual(c.instantane()["actifs"]["p056"]["tokens"], 1)

    def test_etat_cache_et_resultats_du_cache(self):
        tmp = tempfile.mkdtemp()
        try:
            cfg = X.Config(sortie=tmp, pages=[55, 56, 57])
            base = {"bande": 1, "statut": "ok", "empreinte": X.empreinte(cfg), "max_tokens": 8000, "duree": 10.0, "entrees": []}
            X.ecrire_atomique(X.chemin_cache(cfg, 55, 1), json.dumps(dict(base, page=55)))
            X.ecrire_atomique(X.chemin_cache(cfg, 56, 1), json.dumps(dict(base, page=56, empreinte="autre")))
            e = X.etat_cache(cfg)
            self.assertEqual((e["cats"]["ok"], e["cats"]["périmé"], e["cats"]["à faire"]), ([55], [56], [57]))
            self.assertEqual((e["reste"], e["moyenne"], e["reste_s"]), (2, 10.0, 20.0))
            self.assertEqual(set(X.resultats_du_cache(cfg)), {(55, 1), (56, 1)})                  # périmé conservé
            self.assertEqual(set(X.resultats_du_cache(cfg, tolerer=False)), {(55, 1)})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- mise en page, lecture hybride

class TestLectureHybrideUnitaires(unittest.TestCase):
    def test_recoller_les_cesures(self):
        L = LH.LigneOCR
        self.assertEqual(LH.recoller([L("La tradition islami-", 90), L("que lui attribue", 90)]), "La tradition islamique lui attribue")
        self.assertEqual(LH.recoller([L("de ‘Abd al-", 90), L("Qâdir al-Jîlânî", 90)]), "de ‘Abd al- Qâdir al-Jîlânî")      # majuscule : trait conservé
        self.assertEqual(LH.recoller([L("* Nâḍil : qui l'emporte", 90)]), "Nâḍil : qui l'emporte")

    def test_corrections_ocr(self):
        self.assertEqual(LH.corriger_ocr_fr("Signifie : cadeau. 11 fut le don"), "Signifie : cadeau. Il fut le don")
        self.assertEqual(LH.corriger_ocr_fr("Haybân : craint, respecté, vénéré. y )"), "Haybân : craint, respecté, vénéré.")
        self.assertEqual(LH.corriger_ocr_fr("(Coran 19, 57)"), "(Coran 19, 57)")

    def test_fusion_de_la_tete(self):
        d, w = LH.fusionner_tete("Hawä' (Eve) : épouse d'Adam.", "Ḥawâ' (Ève)")
        self.assertEqual((d, w), ("Ḥawâ' (Ève) : épouse d'Adam.", ""))
        d, w = LH.fusionner_tete("Hawä' (Eve) : épouse d'Adam.", "Zubayr (Zubayr)")
        self.assertTrue(d.startswith("Hawä'"))
        self.assertIn("divergente", w)
        self.assertEqual(LH.fusionner_tete("Sans tete", "Âdam"), ("Sans tete", ""))

    def test_propagation_des_diacritiques_dans_le_corps(self):
        self.assertEqual(LH.propager_diacritiques("Âdam (Adam) : père d'Adam et d'Ève."), "Âdam (Adam) : père d'Âdam et d'Ève.")
        self.assertEqual(LH.propager_diacritiques("Karim : généreux."), "Karim : généreux.")

    def test_lecture_de_la_reponse_du_modele(self):
        self.assertEqual(LH.extraire_feuille_json('```json\n{"A1": "آدَم", "T1": "Âdam (Adam)"}\n```'), {"A1": "آدَم", "T1": "Âdam (Adam)"})
        self.assertEqual(LH.extraire_feuille_json('<think>essai {"A1": "x"}</think>{"A1": "y"}'), {"A1": "y"})
        self.assertEqual(LH.extraire_feuille_json('{"A1": "آدَم", "A2": "حَوَّ'), {"A1": "آدَم"})                           # tronquée
        self.assertIsNone(LH.extraire_feuille_json("je ne sais pas"))

    def test_tete_de_la_definition(self):
        L = LH.LigneOCR
        mots = [("Hawä'", 90, (10, 5, 80, 30)), ("(Eve)", 90, (90, 5, 150, 30)), (":", 90, (155, 5, 160, 30)), ("épouse", 90, (170, 5, 240, 30))]
        self.assertEqual(LH.trouver_tete([L("Hawä' (Eve) : épouse", 90, mots)]), ("Hawä' (Eve)", (10, 5, 150, 30)))
        mots2 = [("Darqâwî:", 90, (10, 5, 100, 30)), ("Ad-Darqâwî", 90, (110, 5, 200, 30))]
        self.assertEqual(LH.trouver_tete([L("Darqâwî: Ad-Darqâwî", 90, mots2)])[0], "Darqâwî")
        self.assertEqual(LH.trouver_tete([]), ("", None))


@unittest.skipUnless(PDF, "PDF du livre introuvable (variable PDF_LIVRE)")
class TestMiseEnPage(unittest.TestCase):
    """Structure lue par traitement d'image sur de vraies pages (comptes vérifiés à l'œil sur les scans)."""

    @staticmethod
    def analyser(page):
        return MP.analyser(X.nettoyer_page(X.image_native(PDF, page)))

    def test_page_55_prophetes(self):
        m = self.analyser(55)
        self.assertEqual((len(m.entrees), m.problemes), (4, []))
        self.assertEqual([[b.genre[0] for b in e.blocs] for e in m.entrees], [["M"], ["F"], ["M"], ["M"]])
        self.assertEqual([len(e.blocs[0].orthographes) for e in m.entrees], [1, 1, 0, 0])               # Adame, Haoua empilés
        self.assertTrue(all(b.arabe for e in m.entrees for b in e.blocs))

    def test_page_150_couples_masculin_feminin(self):
        m = self.analyser(150)
        self.assertEqual((len(m.entrees), sum(len(e.blocs) for e in m.entrees), m.problemes), (8, 12, []))
        self.assertEqual([len(e.blocs) for e in m.entrees], [2, 1, 2, 1, 1, 2, 1, 2])

    def test_pages_100_300_107(self):
        self.assertEqual(len(self.analyser(100).entrees), 8)
        self.assertEqual(len(self.analyser(300).entrees), 8)
        m = self.analyser(107)
        self.assertEqual((len(m.entrees), m.problemes), (5, []))                                         # le filet de notes est ignoré

    def test_page_de_suite_sans_legende(self):
        m = self.analyser(66)
        self.assertEqual((len(m.entrees), m.sans_legende, m.problemes), (7, True, []))      # 7 entrées visibles sur le scan

    def test_pages_de_texte(self):
        for p in (54, 62):
            m = self.analyser(p)
            self.assertTrue(m.page_texte, p)
            self.assertEqual(m.entrees, [])


def _tesseract_ok() -> bool:
    LH.configurer()
    return LH.disponible()[0]


@unittest.skipUnless(PDF and _tesseract_ok(), "PDF ou Tesseract (avec la langue française) introuvable")
class TestLecturePageHybride(unittest.TestCase):
    def test_page_55(self):
        net = X.nettoyer_page(X.image_native(PDF, 55))
        page = LH.lire_page(net)
        self.assertEqual(page.problemes, [])
        noms = [[x.lower() for x in b.orthographes] for e in page.entrees for b in e.blocs]
        self.assertEqual(noms, [["adam", "adame"], ["hawa", "haoua"], ["chisse"], ["idris"]])
        self.assertIn("premier homme", page.entrees[0].description)
        self.assertTrue(page.entrees[0].tete_texte.lower().startswith("adam"))
        feuille, texte, reperes = LH.construire_feuille(page, net.convert("L"))
        self.assertEqual(sorted(reperes), ["A1", "A2", "A3", "A4", "T1", "T2", "T3", "T4"])
        self.assertIn("8 extraits à lire", texte)
        self.assertLess(feuille.height, 1400)


# ---------------------------------------------------------------- bout en bout avec un FAUX serveur LM Studio

MODEL_ID = "prism-ml/ternary-bonsai-27b"


def _sans_image(o):
    if isinstance(o, dict):
        return {k: _sans_image(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_sans_image(v) for v in o]
    if isinstance(o, str) and o.startswith("data:image"):
        return "<image>"
    return o


class FauxLMStudio(BaseHTTPRequestHandler):
    """Imite LM Studio : API native (/api/v1/models, /api/v1/chat) et API compatible OpenAI, en streaming SSE,
    avec les pannes qu'on veut tester (boucle de réflexion, silence, réglage « off » ignoré, erreurs HTTP...)."""
    protocol_version = "HTTP/1.1"

    @classmethod
    def reset(cls):
        cls.natif = True
        cls.ctx = 32768
        cls.parallel = 4
        cls.options_reflexion = ["off", "on"]
        cls.charge = True
        cls.honore_off = True          # False : le modèle réfléchit même si « reasoning: off » est demandé
        cls.pensee = 30                # nombre d'événements de réflexion quand elle est active
        cls.reponses = {}              # md5 du PNG -> (contenu, texte de réflexion)
        cls.defaut = None              # réponse pour les images inconnues et les requêtes sans image
        cls.erreurs = set()            # md5 d'images pour lesquelles on répond HTTP 500
        cls.sequence = []              # mode de chaque requête de chat en streaming, dans l'ordre
        cls.mode_global = ""           # sans_vision | reasoning_refuse
        cls.requetes = []
        cls.deconnexions = 0
        cls.tps = 42.0
        cls.tokens_texte = 900
        cls.chunked = True             # False : flux délimité par la fermeture de la connexion (HTTP/1.0)
        cls.jeton = ""                 # non vide : l'API exige « Authorization: Bearer <jeton> »
        cls.feuille_invalide = False   # True : répond n'importe quoi à la feuille de recadrages

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _modele_natif(self):
        c = FauxLMStudio
        inst = [{"id": MODEL_ID, "config": {"context_length": c.ctx, "eval_batch_size": 512, "parallel": c.parallel,
                                            "flash_attention": True, "offload_kv_cache_to_gpu": True}}] if c.charge else []
        return {"type": "llm", "publisher": "prism-ml", "key": MODEL_ID, "display_name": "Ternary Bonsai 27B",
                "architecture": "qwen3_5", "quantization": {"name": "Q2_0", "bits_per_weight": 2.0},
                "size_bytes": 7150000000, "params_string": "27B", "loaded_instances": inst, "max_context_length": 262144,
                "format": "gguf", "capabilities": {"vision": True, "trained_for_tool_use": False,
                                                   "reasoning": {"allowed_options": c.options_reflexion, "default": "on"}}}

    def _refuse(self):
        c = FauxLMStudio
        if c.jeton and self.headers.get("Authorization") != f"Bearer {c.jeton}":
            self._json(401, {"error": {"message": "Invalid or missing API token"}})
            return True
        return False

    def do_GET(self):
        c = FauxLMStudio
        if self._refuse():
            return
        if self.path == "/api/v1/models":
            if not c.natif:
                return self._json(404, {"error": {"message": "Unexpected endpoint or method"}})
            return self._json(200, {"models": [{"type": "embedding", "key": "text-embedding-nomic-embed-text-v1.5",
                                                "loaded_instances": []}, self._modele_natif()]})
        if self.path == "/v1/models":
            return self._json(200, {"data": [{"id": "text-embedding-nomic-embed-text-v1.5"}, {"id": MODEL_ID}]})
        self._json(404, {"error": {"message": "unknown"}})

    ARABE = (("adam", "آدَم"), ("hawa", "حَوَّاء"), ("haoua", "حَوَّاء"), ("chisse", "شِيث"), ("idris", "إِدْرِيس"))
    TETES = (("adam (", "Âdam (Adam)"), ("haw", "Ḥawâ' (Ève)"), ("sh", "Shîth (Seth)"), ("idr", "Idrîs (Enoch)"))

    def _feuille(self, corps, natif):
        """Si la requête est une feuille de recadrages (hybride), répond au JSON {« A1 »: arabe, « T1 »: tête} d'après les indices."""
        if natif:
            texte = " ".join(i.get("content", "") for i in corps.get("input") or [] if isinstance(i, dict) and i.get("type") == "text")
        else:
            texte = " ".join(p.get("text", "") for m in corps.get("messages") or [] if isinstance(m.get("content"), list)
                             for p in m["content"] if p.get("type") == "text")
        if "extraits à lire" not in texte:
            return None
        if FauxLMStudio.feuille_invalide:
            return "je ne sais pas lire cette feuille"
        rep = {}
        for m in re.finditer(r"^(A\d+) : prénom « (.*?) »$", texte, re.M):
            bas = m.group(2).lower()
            rep[m.group(1)] = next((v for k, v in self.ARABE if k in bas), "")
        for m in re.finditer(r"^(T\d+) : lecture automatique approximative « (.*?) »$", texte, re.M):
            bas = m.group(2).lower()
            rep[m.group(1)] = next((v for k, v in self.TETES if bas.startswith(k)), m.group(2))
        return json.dumps(rep, ensure_ascii=False)

    @staticmethod
    def _png(corps, natif):
        if natif:
            for it in corps.get("input") or []:
                if isinstance(it, dict) and it.get("type") == "image":
                    return base64.b64decode(it["data_url"].split(",", 1)[1])
        else:
            for m in corps.get("messages") or []:
                if isinstance(m.get("content"), list):
                    for p in m["content"]:
                        if p.get("type") == "image_url":
                            return base64.b64decode(p["image_url"]["url"].split(",", 1)[1])
        return None

    def do_POST(self):
        c = FauxLMStudio
        corps = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self._refuse():
            return
        natif = self.path == "/api/v1/chat"
        if not natif and self.path != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "unknown"}})
        png = self._png(corps, natif)
        cle = hashlib.md5(png).hexdigest() if png else ""
        stream = bool(corps.get("stream"))
        mode = c.sequence.pop(0) if (stream and c.sequence) else ""
        taille = Image.open(io.BytesIO(png)).size if png else None
        c.requetes.append({"chemin": self.path, "stream": stream, "cle": cle, "mode": mode, "taille": taille,
                           "corps": _sans_image(corps)})
        if c.mode_global == "sans_vision":
            return self._json(400, {"error": {"message": "Model does not support image input"}})
        if c.mode_global == "reasoning_refuse" and "reasoning" in corps:
            return self._json(400, {"error": {"message": "The model does not support the reasoning setting you specified"}})
        if cle in c.erreurs:
            return self._json(500, {"error": {"message": "boom"}})
        entree = c.tokens_texte + (taille[0] * taille[1] // 1024 if taille else 0)
        self.contenu_force = self._feuille(corps, natif)
        if not stream:
            stats = {"input_tokens": entree, "total_output_tokens": 1, "reasoning_output_tokens": 0,
                     "tokens_per_second": c.tps, "time_to_first_token_seconds": 0.5}
            if natif:
                return self._json(200, {"model_instance_id": MODEL_ID, "output": [{"type": "message", "content": "x"}], "stats": stats})
            return self._json(200, {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}],
                                    "usage": {"prompt_tokens": entree, "completion_tokens": 1}})
        if mode == "avant_entetes":
            time.sleep(3)
        if mode == "json_au_lieu_de_sse":
            return self._json(200, {"inattendu": "pas du SSE"})
        try:
            self._stream(natif, corps, cle, mode, entree)
        except (BrokenPipeError, ConnectionResetError, OSError):
            c.deconnexions += 1

    def _w(self, data: bytes):
        if FauxLMStudio.chunked:
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        else:
            self.wfile.write(data)
        self.wfile.flush()

    def _ev(self, t, **d):
        d["type"] = t
        self._w(f"event: {t}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n".encode("utf-8"))

    def _data(self, obj):
        self._w(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))

    def _fin_flux(self):
        if FauxLMStudio.chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        self.close_connection = True

    def _stream(self, natif, corps, cle, mode, entree):
        c = FauxLMStudio
        contenu, raison = c.reponses.get(cle) or c.defaut or ('{"entrees": []}', "")
        if self.contenu_force is not None:
            contenu = self.contenu_force
        if natif:
            off = corps.get("reasoning") == "off"
        else:
            off = (corps.get("chat_template_kwargs") or {}).get("enable_thinking") is False
        penser = (not off) or (not c.honore_off)
        if mode == "boucle" and penser:
            ref = None                                              # réflexion infinie
        elif mode == "reflexion_longue" and penser:
            ref = [f"mot{i} " for i in range(600)]
        elif penser:
            ref = [raison[i:i + 10] for i in range(0, len(raison), 10)] if raison else [f"mot{i} " for i in range(c.pensee)]
        else:
            ref = []
        morceaux = [contenu[i:i + 6] for i in range(0, len(contenu), 6)]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        if c.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        if natif:
            self._ev("chat.start", model_instance_id=MODEL_ID)
            self._ev("prompt_processing.start")
            self._ev("prompt_processing.progress", progress=0.5)
            self._ev("prompt_processing.end")
            if mode == "erreur_contexte":
                self._ev("error", error={"type": "invalid_request", "message": "Context size has been exceeded."})
                self._fin_flux()
                return
            if mode == "silence":
                time.sleep(3)
            if mode == "silence_long":
                time.sleep(10)
            if ref is None:
                self._ev("reasoning.start")
                for _ in range(20000):
                    self._ev("reasoning.delta", content="Je vérifie encore l'arabe de ce prénom. ")
                    time.sleep(0.0005)
                return
            if ref:
                self._ev("reasoning.start")
                for p in ref:
                    self._ev("reasoning.delta", content=p)
                self._ev("reasoning.end")
            self._ev("message.start")
            for p in morceaux:
                self._ev("message.delta", content=p)
                if mode == "lent":
                    time.sleep(0.02)
            self._ev("message.end")
            stats = {"input_tokens": entree, "total_output_tokens": len(ref) + len(morceaux),
                     "reasoning_output_tokens": len(ref), "tokens_per_second": c.tps, "time_to_first_token_seconds": 0.5}
            self._ev("chat.end", result={"model_instance_id": MODEL_ID, "output": [{"type": "message", "content": contenu}],
                                         "stats": stats})
            self._fin_flux()
            return
        if mode == "silence":
            time.sleep(3)
        if mode == "think_inline":
            texte = "Je regarde la page et je réfléchis un moment.</think>" + contenu
            for i in range(0, len(texte), 6):
                self._data({"choices": [{"delta": {"content": texte[i:i + 6]}}]})
            n_ref = 8
        else:
            for p in ref or []:
                self._data({"choices": [{"delta": {"reasoning_content": p}}]})
            for p in morceaux:
                self._data({"choices": [{"delta": {"content": p}}]})
            n_ref = len(ref or [])
        self._data({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        self._data({"choices": [], "usage": {"prompt_tokens": entree, "completion_tokens": len(morceaux) + n_ref}})
        self._w(b"data: [DONE]\n\n")
        self._fin_flux()


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
        self.out = ""
        FauxLMStudio.reset()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    # --- outils
    def lancer(self, *args, pdf=True):
        base = ([PDF] if pdf else []) + ["--url", self.url, "--sortie", self.tmp, "--tokens-image", "0", "--suivi", "0", "--methode", "vlm"]
        tampon = io.StringIO()
        try:
            with contextlib.redirect_stdout(tampon):
                return X.main(base + list(args))
        finally:
            self.out += tampon.getvalue()

    def md5(self, page, bande=1, decoupe=1):
        cfg = X.Config(pdf=PDF, decoupe=decoupe)
        return hashlib.md5(X.preparer_image(cfg, page, bande)[0]).hexdigest()

    def lire(self):
        with open(os.path.join(self.tmp, "prenoms_arabes.json"), encoding="utf-8") as f:
            return json.load(f)

    def cache(self, page, bande=1, decoupe=1):
        with open(os.path.join(self.tmp, "cache", X.id_chunk(page, bande, decoupe) + ".json"), encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def generations():
        return [r for r in FauxLMStudio.requetes if r["stream"]]

    def pages_sheet(self):
        from openpyxl import load_workbook
        wb = load_workbook(os.path.join(self.tmp, "prenoms_arabes.xlsx"))
        return list(wb["Pages"].iter_rows(values_only=True))

    def reponses_p55_p150(self):
        FauxLMStudio.reponses[self.md5(55)] = ("```json\n" + json.dumps({"entrees": P55}, ensure_ascii=False) + "\n```", "")
        FauxLMStudio.reponses[self.md5(150)] = (json.dumps({"entrees": P150}, ensure_ascii=False), "")

    # --- modèle chargé
    def test_modele_resolu_par_correspondance_approchee(self):
        info = X.resoudre_modele(X.Config(url=self.url, modele="prism-ml/bonsai-27b"))
        self.assertEqual((info.id, info.natif, info.ctx, info.parallel), (MODEL_ID, True, 32768, 4))
        self.assertTrue(info.peut_couper_reflexion)
        self.assertEqual((info.flash, info.batch, info.kv_gpu, info.vision), (True, 512, True, True))
        self.assertIn("Q2_0", info.quant)

    def test_modele_non_charge_est_fatal(self):
        FauxLMStudio.charge = False
        with self.assertRaises(X.ErreurFatale) as cm:
            X.resoudre_modele(X.Config(url=self.url))
        self.assertIn("lms load", str(cm.exception))

    def test_api_openai_si_lm_studio_ancien(self):
        FauxLMStudio.natif = False
        info = X.resoudre_modele(X.Config(url=self.url))
        self.assertEqual((info.id, info.natif, info.ctx), (MODEL_ID, False, 0))

    def test_serveur_injoignable(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()) as o:
            X.main([PDF, "--url", "http://127.0.0.1:9", "--sortie", self.tmp, "--pages", "55"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("injoignable", o.getvalue())

    # --- chemin nominal
    def test_pipeline_complet_reprise_et_score(self):
        self.reponses_p55_p150()
        self.assertEqual(self.lancer("--pages", "55,150"), 0)
        lignes = self.lire()
        self.assertEqual(len(lignes), 18)                                  # 6 + 12 prénoms, un par ligne
        par = {r["prénom_fr"]: r for r in lignes}
        self.assertEqual(par["Adame"]["prénom_arab_diacritiques"], "آدَم")
        self.assertEqual(par["Adame"]["prénoms_équivalents"], "Adam")
        self.assertEqual(par["Haoua"]["prénom_arab_diacritiques"], "حَوَّاء")     # tatwil retiré
        self.assertEqual(par["Haoua"]["genre"], "FEMININ")
        self.assertEqual(par["Nadila"]["forme_associée"], "Nadil")
        self.assertEqual(par["Idris"]["équivalent_étranger"], "Enoch")
        self.assertEqual(par["Habbar"]["thème"], "4. Puissance et protection")
        s = VT.score(lignes, [55, 150])
        self.assertEqual((s["vedettes"], s["parasites"], s["genre"], s["arabe_exact"]), ("18/18", 0, "18/18", "16/16"))
        gen = self.generations()
        self.assertEqual(len(gen), 2)
        self.assertTrue(all(g["corps"]["reasoning"] == "off" for g in gen))   # réflexion coupée par défaut
        c = self.cache(55)
        self.assertEqual((c["penser"], c["tokens_reflexion"], c["tps"]), (False, 0, 42.0))
        # relance : tout vient du cache
        n = len(gen)
        self.lancer("--pages", "55,150")
        self.assertEqual(len(self.generations()), n)
        self.assertIn("2 déjà en cache", self.out)
        # --refaire : les deux pages sont redemandées
        self.lancer("--pages", "55,150", "--refaire")
        self.assertEqual(len(self.generations()), n + 2)

    def test_parametres_de_generation_envoyes(self):
        self.reponses_p55_p150()
        self.lancer("--pages", "55")
        c = self.generations()[-1]["corps"]
        self.assertEqual((c["temperature"], c["top_p"], c["top_k"], c["max_output_tokens"]), (0.3, 0.8, 20, 8000))
        self.assertEqual((c["model"], c["store"], c["stream"], c["reasoning"]), (MODEL_ID, False, True, "off"))
        self.assertEqual(c["system_prompt"], X.PROMPT)
        self.assertEqual([i["type"] for i in c["input"]], ["image", "text"])

    def test_reflexion_oui_garde_la_reflexion(self):
        self.reponses_p55_p150()
        self.lancer("--pages", "55", "--reflexion", "oui")
        c = self.generations()[-1]["corps"]
        self.assertEqual((c["reasoning"], c["temperature"], c["top_p"]), ("on", 0.6, 0.95))
        res = self.cache(55)
        self.assertEqual((res["penser"], res["tokens_reflexion"]), (True, 30))

    def test_demi_pages_avec_suite_de_definition(self):
        haut = [P55[0], entree([(F, ["Hawa", "Haoua"], "حَوَّاء")], "Ḥawâ' (Ève) : épouse d'Âdam.", inc=True)]
        bas = [entree([], "De l'hébreu : vie, source de vie.", suite=True), P55[2], P55[3]]
        FauxLMStudio.reponses[self.md5(55, 1, 2)] = (json.dumps({"entrees": haut}, ensure_ascii=False), "")
        FauxLMStudio.reponses[self.md5(55, 2, 2)] = (json.dumps({"entrees": bas}, ensure_ascii=False), "")
        self.lancer("--pages", "55", "--decoupe", "2", "--paralleles", "2")
        lignes = self.lire()
        self.assertEqual([r["prénom_fr"] for r in lignes], ["Adam", "Adame", "Hawa", "Haoua", "Chisse", "Idris"])
        self.assertEqual(lignes[2]["description_en_français"], "Ḥawâ' (Ève) : épouse d'Âdam. De l'hébreu : vie, source de vie.")
        self.assertNotIn("description_coupee", lignes[2]["à_vérifier"])

    def test_page_blanche_aucune_requete_de_generation(self):
        self.lancer("--pages", "134")
        self.assertEqual(self.generations(), [])
        lignes = self.pages_sheet()
        self.assertEqual((lignes[1][0], lignes[1][2]), (134, "ok"))
        self.assertIn("page blanche", lignes[1][10])

    def test_cache_perime_si_le_prompt_change(self):
        self.reponses_p55_p150()
        self.lancer("--pages", "55")
        self.lancer("--pages", "55")
        self.assertEqual(len(self.generations()), 1)                       # 2e passage : cache
        prompt = os.path.join(self.tmp, "prompt.txt")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("consigne différente")
        self.lancer("--pages", "55", "--prompt-fichier", prompt)
        self.assertEqual(len(self.generations()), 2)                       # prompt changé : recalcul
        self.assertIn("périmés", self.out)

    def test_comparaison_avec_ancien_csv(self):
        self.reponses_p55_p150()
        ancien = os.path.join(self.tmp, "ancien.csv")
        with open(ancien, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ID", "Prénom(s)", "Page (PDF)"])
            w.writerows([[1, "Adam / Adame", 55], [2, "Hawa / Haoua", 55], [3, "Chisse", 55], [4, "Idris", 55], [5, "Zorglub", 55]])
        self.lancer("--pages", "55", "--ancien", ancien)
        with open(os.path.join(self.tmp, "comparaison_ancien.csv"), encoding="utf-8-sig") as f:
            ligne = list(csv.reader(f))[1]
        self.assertEqual((ligne[0], ligne[1], ligne[2], ligne[3], ligne[4]), ("55", "7", "6", "zorglub", ""))

    def test_coupure_fixe_dans_un_blanc_sur_la_vraie_page(self):
        im = X.image_native(PDF, 55)
        net = X.nettoyer_page(im)
        haut, _ = X.decouper(net, im, 2)
        ligne_de_coupe = np.asarray(net)[haut.height]
        self.assertLessEqual(int(((255 - ligne_de_coupe.min(axis=1)) > 60).sum()), 2)     # aucune encre sur la ligne de coupe

    # --- pannes : le script doit s'en sortir seul
    def test_modele_sans_vision_arrete_tout_sans_exporter(self):
        FauxLMStudio.mode_global = "sans_vision"
        with self.assertRaises(SystemExit) as cm:
            self.lancer("--pages", "55,56,57")
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "prenoms_arabes.csv")))

    def test_une_page_en_echec_nempeche_pas_les_autres(self):
        self.reponses_p55_p150()
        FauxLMStudio.erreurs = {self.md5(55)}
        self.lancer("--pages", "55,150", "--essais", "2")
        self.assertEqual({r["page_pdf"] for r in self.lire()}, {150})
        self.assertEqual(sum(1 for g in self.generations() if g["cle"] == self.md5(55)), 2)      # 2 essais
        statuts = {r[0]: r[2] for r in self.pages_sheet()[1:]}
        self.assertEqual(statuts, {55: "échec", 150: "ok"})

    def test_reflexion_ignoree_bascule_en_mode_avec_reflexion(self):
        self.reponses_p55_p150()
        FauxLMStudio.honore_off = False
        FauxLMStudio.pensee = 600
        self.lancer("--pages", "55", "--reflexion", "non")
        gen = self.generations()
        self.assertEqual([g["corps"]["reasoning"] for g in gen], ["off", "on"])
        res = self.cache(55)
        self.assertEqual((res["penser"], res["tokens_reflexion"]), (True, 600))
        self.assertEqual(len(self.lire()), 6)

    def test_boucle_de_reflexion_abandonnee_puis_sans_reflexion(self):
        self.reponses_p55_p150()
        FauxLMStudio.sequence = ["boucle", ""]
        debut = time.time()
        self.lancer("--pages", "55", "--reflexion", "oui")
        self.assertLess(time.time() - debut, 20)
        gen = self.generations()
        self.assertEqual([g["corps"]["reasoning"] for g in gen], ["on", "off"])
        self.assertEqual(len(self.lire()), 6)
        for _ in range(40):                                                 # le serveur voit la connexion se fermer
            if FauxLMStudio.deconnexions:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(FauxLMStudio.deconnexions, 1)

    def test_silence_abandonne_puis_reessaye(self):
        self.reponses_p55_p150()
        FauxLMStudio.sequence = ["silence", ""]
        debut = time.time()
        self.lancer("--pages", "55", "--stall", "1")
        self.assertLess(time.time() - debut, 12)
        self.assertEqual(len(self.generations()), 2)
        self.assertEqual(len(self.lire()), 6)

    def test_aucune_reponse_avant_les_entetes_puis_reessaye(self):
        self.reponses_p55_p150()
        FauxLMStudio.sequence = ["avant_entetes", ""]
        self.lancer("--pages", "55", "--attente", "1")
        self.assertEqual(len(self.generations()), 2)
        self.assertEqual(len(self.lire()), 6)

    def test_reglage_de_reflexion_refuse_par_lm_studio(self):
        self.reponses_p55_p150()
        FauxLMStudio.mode_global = "reasoning_refuse"
        self.lancer("--pages", "55")
        gen = self.generations()
        self.assertEqual(len(gen), 2)
        self.assertIn("reasoning", gen[0]["corps"])
        self.assertNotIn("reasoning", gen[1]["corps"])                     # réglage retiré après le refus
        self.assertEqual(len(self.lire()), 6)

    def test_ctrl_c_exporte_et_rend_la_main(self):
        FauxLMStudio.sequence = ["silence_long"]

        def interrompre():
            for _ in range(200):
                if self.generations():
                    break
                time.sleep(0.05)
            time.sleep(0.5)
            _thread.interrupt_main()
        threading.Thread(target=interrompre, daemon=True).start()
        debut = time.time()
        code = self.lancer("--pages", "55", "--stall", "30", "--essais", "1")
        self.assertEqual(code, 130)
        self.assertLess(time.time() - debut, 15)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "prenoms_arabes.csv")))
        self.assertEqual(self.pages_sheet()[1][2], "à faire")

    def test_erreur_de_contexte_dans_le_flux_est_fatale(self):
        FauxLMStudio.sequence = ["erreur_contexte"]
        with self.assertRaises(SystemExit) as cm:
            self.lancer("--pages", "55")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("Contexte insuffisant", self.out)
        self.assertEqual(len(self.generations()), 1)                       # pas de nouvel essai inutile

    def test_authentification_exigee_message_explicite(self):
        FauxLMStudio.jeton = "secret"
        with self.assertRaises(SystemExit) as cm:
            self.lancer("--pages", "55")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--cle-api", self.out)
        self.out = ""
        FauxLMStudio.defaut = (json.dumps({"entrees": P55}, ensure_ascii=False), "")
        self.assertEqual(self.lancer("--pages", "55", "--cle-api", "secret"), 0)
        self.assertEqual(len(self.lire()), 6)

    def test_reponse_non_sse_est_signalee_clairement(self):
        FauxLMStudio.sequence = ["json_au_lieu_de_sse"] * 3
        self.lancer("--pages", "55", "--essais", "3")
        self.assertIn("aucun événement reconnu", self.out)
        self.assertEqual(self.pages_sheet()[1][2], "échec")

    def test_diagnostic_quand_la_reflexion_ne_se_coupe_pas(self):
        FauxLMStudio.defaut = ("ADAM 123", "")
        FauxLMStudio.honore_off = False
        FauxLMStudio.pensee = 400
        self.lancer("--diagnostic")
        self.assertIn("malgré la demande de la couper", self.out)
        self.assertIn("test non concluant", self.out)
        self.assertNotIn("la vision ne semble pas fonctionner", self.out)

    def test_journal_ecrit_pendant_une_extraction(self):
        self.reponses_p55_p150()
        self.lancer("--pages", "55,150", "--cle-api", "motdepasse-xyz")
        logs = sorted(glob.glob(os.path.join(self.tmp, "logs", "*.log")))
        self.assertEqual(len(logs), 1)
        with open(logs[0], encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("essai 1 : penser=False", txt)
        self.assertIn("finish=", txt)
        self.assertNotIn("motdepasse-xyz", txt)
        with open(logs[0][:-4] + ".jsonl", encoding="utf-8") as f:
            ev = [json.loads(ligne) for ligne in f]
        types = [e["type"] for e in ev]
        for t in ("demarrage", "cache", "modele", "calibrage", "requete", "page", "export"):
            self.assertIn(t, types)
        dem = next(e for e in ev if e["type"] == "demarrage")
        self.assertEqual(dem["config"]["cle_api"], "***")
        self.assertIn("python", dem["environnement"])
        req = next(e for e in ev if e["type"] == "requete")
        self.assertEqual((req["chunk"], req["essai"], req["penser"]), ("p055", 1, False))
        self.assertIn("Journal complet", self.out)

    def test_echec_journalise_avec_son_diagnostic(self):
        self.reponses_p55_p150()
        FauxLMStudio.erreurs = {self.md5(55)}
        self.lancer("--pages", "55", "--essais", "2")
        logs = glob.glob(os.path.join(self.tmp, "logs", "*.log"))
        with open(logs[0], encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("essai 1/2", txt)
        self.assertIn("WARN", txt)
        self.assertIn("Traceback", txt)                                    # trace complète dans le fichier...
        self.assertNotIn("Traceback", self.out)                            # ...mais pas sur la console
        with open(logs[0][:-4] + ".jsonl", encoding="utf-8") as f:
            types = [json.loads(ligne)["type"] for ligne in f]
        self.assertEqual(types.count("requete_erreur"), 2)
        self.assertIn("exception", types)

    def test_exception_inattendue_journalisee(self):
        with mock.patch.object(X, "run_pipeline", side_effect=RuntimeError("boum")):
            with self.assertRaises(SystemExit) as cm:
                self.lancer("--pages", "55")
        self.assertEqual(cm.exception.code, 1)
        logs = glob.glob(os.path.join(self.tmp, "logs", "*.log"))
        with open(logs[0], encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("RuntimeError: boum", txt)
        self.assertIn("Traceback", txt)
        self.assertIn("fichier de log", self.out)

    def test_le_controle_suit_l_extraction(self):
        self.reponses_p55_p150()
        FauxLMStudio.sequence = ["lent", "lent"]
        ctrl = X.Controle()
        cfg = X.Config(pdf=PDF, url=self.url, sortie=self.tmp, pages=[55, 150], tokens_image=0, suivi=0, suivi_ui=0.1, methode="vlm")
        instantanes = []

        def surveiller():
            while ctrl.etat not in ("termine", "interrompu", "erreur"):
                instantanes.append(ctrl.instantane())
                time.sleep(0.05)
        X.MUET = True
        try:
            t = threading.Thread(target=surveiller, daemon=True)
            t.start()
            rows, infos, avert = X.run_pipeline(cfg, ctrl)
            t.join(5)
        finally:
            X.MUET = False
        self.assertEqual((ctrl.etat, ctrl.total, ctrl.fait), ("termine", 2, 2))
        self.assertEqual(ctrl.actifs, {})
        self.assertEqual(len(rows), 18)
        self.assertTrue(any(s["etat"] == "en cours" for s in instantanes))
        self.assertTrue(any(a.get("tokens", 0) > 0 for s in instantanes for a in s["actifs"].values()))

    def test_arret_demande_par_le_controle(self):
        FauxLMStudio.sequence = ["silence_long"]
        ctrl = X.Controle()
        cfg = X.Config(pdf=PDF, url=self.url, sortie=self.tmp, pages=[55], tokens_image=0, suivi=0, stall=30, essais=1, methode="vlm")

        def travail():
            X.run_pipeline(cfg, ctrl)
        X.MUET = True
        try:
            t = threading.Thread(target=travail, daemon=True)
            t.start()
            for _ in range(200):
                if self.generations() and ctrl.actifs:
                    break
                time.sleep(0.05)
            debut = time.time()
            ctrl.stop.set()
            t.join(15)
        finally:
            X.MUET = False
        self.assertFalse(t.is_alive())
        self.assertLess(time.time() - debut, 10)
        self.assertTrue(cfg.interrompu)
        self.assertEqual(ctrl.etat, "interrompu")

    def test_diagnostic_prend_en_compte_les_pages(self):
        FauxLMStudio.defaut = ("ADAM 123", "")
        self.lancer("--diagnostic", "--pages", "150")
        self.assertIn("page 150", self.out)
        self.assertIn("préremplissage", self.out)

    @unittest.skipUnless(_tesseract_ok(), "Tesseract (langue française) introuvable")
    def test_hybride_une_seule_requete_par_page(self):
        self.lancer("--pages", "55", "--methode", "hybride", "--sauver-images")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "images", "p055_feuille.png")))
        gen = self.generations()
        self.assertEqual(len(gen), 1)                                                   # une feuille, pas la page entière
        self.assertEqual(gen[0]["corps"]["system_prompt"], LH.PROMPT_FEUILLE)
        self.assertEqual([i["type"] for i in gen[0]["corps"]["input"]], ["image", "text"])
        par = {r["prénom_fr"]: r for r in self.lire()}
        self.assertEqual(sorted(k.lower() for k in par), ["adam", "adame", "chisse", "haoua", "hawa", "idris"])
        self.assertEqual(par["Adame"]["prénom_arab_diacritiques"], "آدَم")              # une graphie empilée reprend l'arabe de son bloc
        self.assertEqual(par["Haoua"]["prénom_arab_diacritiques"], "حَوَّاء")
        self.assertEqual((par["Adam"]["genre"], par["Hawa"]["genre"], par["Idris"]["genre"]), ("MASCULIN", "FEMININ", "MASCULIN"))
        self.assertTrue(par["Adam"]["description_en_français"].startswith("Âdam (Adam) : premier homme"))
        self.assertTrue(par["Hawa"]["description_en_français"].startswith("Ḥawâ' (Ève) :"))
        c = self.cache(55)
        self.assertEqual(c["methode"], "hybride")
        self.assertLess(c["tokens"], 400)
        self.assertEqual(self.pages_sheet()[1][2], "ok")
        self.assertIn("méthode : HYBRIDE", self.out)

    @unittest.skipUnless(_tesseract_ok(), "Tesseract (langue française) introuvable")
    def test_hybride_repli_si_la_feuille_est_illisible(self):
        self.reponses_p55_p150()
        FauxLMStudio.feuille_invalide = True
        self.lancer("--pages", "55", "--methode", "hybride", "--essais", "2")
        self.assertEqual(len(self.generations()), 3)                                    # 2 essais sur la feuille + la page entière
        self.assertIn("repli sur la lecture de la page entière", self.out)
        self.assertEqual(len(self.lire()), 6)
        self.assertNotEqual(self.cache(55).get("methode"), "hybride")

    @unittest.skipUnless(_tesseract_ok(), "Tesseract (langue française) introuvable")
    def test_hybride_page_de_texte_sans_requete(self):
        self.lancer("--pages", "62", "--methode", "hybride")
        self.assertEqual(self.generations(), [])
        self.assertEqual(self.lire(), [])
        self.assertIn("texte courant", " ".join(self.cache(62)["avertissements"]))

    def test_methode_hybride_sans_tesseract_est_fatale_et_auto_se_replie(self):
        with mock.patch.object(LH, "disponible", return_value=(False, "Tesseract est introuvable")):
            with self.assertRaises(SystemExit) as cm:
                self.lancer("--pages", "55", "--methode", "hybride")
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("Tesseract est introuvable", self.out)
            self.out = ""
            self.reponses_p55_p150()
            self.lancer("--pages", "55", "--methode", "auto")
            self.assertIn("lecture par le modèle de vision seul", self.out)
            self.assertEqual(len(self.lire()), 6)

    def test_hybride_erreur_de_tesseract_repli_sur_le_modele(self):
        self.reponses_p55_p150()
        with mock.patch.object(LH, "disponible", return_value=(True, "Tesseract simulé")), \
                mock.patch.object(LH, "lire_page", side_effect=RuntimeError("tesseract a planté")):
            self.lancer("--pages", "55", "--methode", "hybride")
        self.assertEqual(len(self.generations()), 1)                                    # la page entière, lue par le modèle
        self.assertIn("repli sur la lecture de la page entière", self.out)
        self.assertEqual(len(self.lire()), 6)
        logs = glob.glob(os.path.join(self.tmp, "logs", "*.log"))
        with open(logs[0], encoding="utf-8") as f:
            self.assertIn("tesseract a planté", f.read())

    def test_flux_non_chunke_termine_par_la_fermeture(self):
        self.reponses_p55_p150()
        FauxLMStudio.chunked = False
        self.lancer("--pages", "55")
        self.assertEqual(len(self.lire()), 6)

    def test_reponse_tronquee_gardee_comme_partielle(self):
        complet = json.dumps({"entrees": P55}, ensure_ascii=False)
        coupe = complet[:complet.index('"Hawa"') + 12]                     # JSON coupé en plein milieu de la 2e entrée
        FauxLMStudio.reponses[self.md5(55)] = (coupe, "")
        self.lancer("--pages", "55", "--max-tokens", str(len(coupe) // 6 - 1))
        self.assertEqual(len(self.generations()), 3)                       # 3 essais, on garde le meilleur
        self.assertEqual(self.cache(55)["statut"], "partiel")
        self.assertEqual([r["prénom_fr"] for r in self.lire()], ["Adam", "Adame"])
        self.assertIn("page_partielle", self.lire()[0]["à_vérifier"])
        self.assertIn("pages partielles", self.out)

    def test_ligne_de_progression_pendant_une_requete_lente(self):
        self.reponses_p55_p150()
        FauxLMStudio.sequence = ["lent"]
        self.lancer("--pages", "55", "--suivi", "1")
        self.assertRegex(self.out, r"p055 en cours : \d+ jetons, [\d.]+ tok/s")

    def test_export_conserve_les_pages_d_une_autre_configuration(self):
        self.reponses_p55_p150()
        self.lancer("--pages", "55")
        tampon = io.StringIO()
        with contextlib.redirect_stdout(tampon):
            X.main(["--export", "--pages", "55", "--sortie", self.tmp, "--tokens-image", "1234"])
        self.assertEqual(len(self.lire()), 6)
        self.assertIn("autre configuration", tampon.getvalue())

    # --- contexte et image
    def test_calibrage_reduit_l_image(self):
        FauxLMStudio.defaut = (json.dumps({"entrees": P55}, ensure_ascii=False), "")
        self.lancer("--pages", "55", "--tokens-image", "2500")
        w, h = self.generations()[-1]["taille"]
        self.assertEqual(max(w, h), int(2872 * (2500 / (1816 * 2872 // 1024)) ** 0.5))
        self.assertLess(max(w, h), 2872)
        self.assertIn("image réduite", self.out)
        self.assertEqual(len(self.lire()), 6)

    def test_contexte_trop_court_est_fatal(self):
        FauxLMStudio.ctx = 6000
        with self.assertRaises(SystemExit) as cm:
            self.lancer("--pages", "55")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("Contexte trop court", self.out)
        self.assertEqual(self.generations(), [])

    def test_sortie_bornee_par_le_contexte(self):
        FauxLMStudio.ctx = 12000
        FauxLMStudio.defaut = (json.dumps({"entrees": P55}, ensure_ascii=False), "")
        self.lancer("--pages", "55")
        entree = 900 + 1816 * 2872 // 1024
        self.assertEqual(self.generations()[-1]["corps"]["max_output_tokens"], 12000 - entree - 128)
        self.assertIn("budget de sortie limité", self.out)

    def test_parallelisme_reduit_si_le_contexte_ne_suffit_pas(self):
        FauxLMStudio.ctx = 20000
        FauxLMStudio.defaut = (json.dumps({"entrees": P55}, ensure_ascii=False), "")
        self.lancer("--pages", "55,56,57", "--paralleles", "3")
        self.assertIn("2 à la fois", self.out)
        self.assertEqual(len(self.generations()), 3)

    # --- repli sur l'API compatible OpenAI (LM Studio < 0.4)
    def test_repli_openai_en_streaming(self):
        FauxLMStudio.natif = False
        FauxLMStudio.defaut = (json.dumps({"entrees": P55}, ensure_ascii=False), "réflexion fournie dans le champ dédié")
        self.lancer("--pages", "55")
        g = self.generations()[-1]
        self.assertEqual(g["chemin"], "/v1/chat/completions")
        self.assertEqual((g["corps"]["messages"][0]["role"], g["corps"]["stream"], g["corps"]["max_tokens"]), ("system", True, 8000))
        self.assertTrue(g["corps"]["stream_options"]["include_usage"])
        res = self.cache(55)
        self.assertEqual((res["penser"], res["tokens_entree"] > 900, res["tokens_reflexion"] > 0), (True, True, True))
        self.assertEqual(len(self.lire()), 6)

    def test_repli_openai_reflexion_dans_le_contenu(self):
        FauxLMStudio.natif = False
        FauxLMStudio.defaut = (json.dumps({"entrees": P55}, ensure_ascii=False), "")
        FauxLMStudio.sequence = ["think_inline"]
        self.lancer("--pages", "55")
        self.assertEqual(len(self.lire()), 6)
        self.assertGreater(self.cache(55)["tokens_reflexion"], 0)

    # --- reprise et visibilité
    def test_export_sans_llm_et_statut(self):
        self.reponses_p55_p150()
        self.lancer("--pages", "55,150")
        avant = len(self.lire())
        for f in ("prenoms_arabes.csv", "prenoms_arabes.json", "prenoms_arabes.xlsx"):
            os.remove(os.path.join(self.tmp, f))
        n = len(FauxLMStudio.requetes)
        tampon = io.StringIO()
        with contextlib.redirect_stdout(tampon):
            code = X.main(["--export", "--pages", "55,150", "--sortie", self.tmp, "--url", "http://127.0.0.1:9"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.lire()), avant)
        self.assertEqual(len(FauxLMStudio.requetes), n)                    # LM Studio n'a pas été contacté
        tampon = io.StringIO()
        with contextlib.redirect_stdout(tampon):
            X.main(["--statut", "--pages", "55,150,200", "--sortie", self.tmp, "--tokens-image", "0", "--methode", "vlm"])
        self.assertRegex(tampon.getvalue(), r"ok\s+:\s+2")
        self.assertRegex(tampon.getvalue(), r"à faire\s+:\s+1")

    def test_export_intermediaire_tous_les_n_pages(self):
        FauxLMStudio.defaut = (json.dumps({"entrees": P55}, ensure_ascii=False), "")
        appels = []
        vrai = X.exporter

        def espion(*a, **k):
            appels.append(1)
            return vrai(*a, **k)
        with mock.patch.object(X, "exporter", espion):
            self.lancer("--pages", "55,56,57", "--export-tous", "1")
        self.assertGreaterEqual(len(appels), 4)                            # 3 intermédiaires + l'export final

    def test_diagnostic(self):
        FauxLMStudio.defaut = ("ADAM 123", "")
        code = self.lancer("--diagnostic", "--tokens-image", "2500")
        self.assertEqual(code, 0)
        for attendu in ("VISION OK", "5/5", "lms load", "Max Concurrent Predictions", "jetons d'entrée"):
            self.assertIn(attendu, self.out)


if __name__ == "__main__":
    unittest.main()
