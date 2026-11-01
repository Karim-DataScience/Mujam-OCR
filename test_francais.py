"""Tests de la correction du français (verif_francais) et de la relecture du corps des définitions (v4.5)."""
import json
import shutil
import tempfile
import unittest
from unittest import mock

import extraire_prenoms_llm as X
import lecture_hybride as LH
import verif_francais as VF

CONNUS = {"serviteur", "lumière", "femme", "bonheur", "vivant", "le", "est", "dieu", "une", "belle", "pour", "celui", "nom", "avec", "très", "aimé", "porte", "parole"}


class FauxDico(set):
    """Dictionnaire minimal : « mot in dico »."""


class TestFusionConservatrice(unittest.TestCase):
    def setUp(self):
        self.dico = FauxDico(CONNUS)
        self.freq = VF.Counter()

    def test_remplace_un_mot_suspect_par_un_mot_connu_proche(self):
        nouveau, ch = VF.fusionner("Le seroiteur du Vivant.", "Le serviteur du Vivant.", self.dico, self.freq)
        self.assertEqual(nouveau, "Le serviteur du Vivant.")
        self.assertEqual(ch, [("seroiteur", "serviteur")])

    def test_garde_la_ponctuation_de_tesseract(self):
        nouveau, ch = VF.fusionner("une lunnière, belle", "une lumière belle", self.dico, self.freq)
        self.assertIn("lumière,", nouveau)

    def test_ne_touche_pas_un_mot_non_suspect(self):
        # « porte » est connu : même si le modèle propose autre chose, on garde Tesseract
        nouveau, ch = VF.fusionner("il porte le nom", "il comte le nom", self.dico, self.freq)
        self.assertEqual((nouveau, ch), ("il porte le nom", []))

    def test_refuse_un_mot_inconnu_ou_trop_different(self):
        nouveau, ch = VF.fusionner("un fennne belle", "un zorglub belle", self.dico, self.freq)          # inconnu
        self.assertEqual(ch, [])
        nouveau, ch = VF.fusionner("un fennne belle", "un serviteur belle", self.dico, self.freq)         # connu mais trop éloigné
        self.assertEqual(ch, [])

    def test_mot_frequent_du_corpus_est_legitime(self):
        freq = VF.Counter({"sourate": 21})
        self.assertFalse(VF.est_suspect("sourate", self.dico, freq))
        self.assertTrue(VF.est_suspect("seroiteur", self.dico, freq))

    def test_modele_qui_reecrit_tout_n_a_aucun_effet_hors_mots_suspects(self):
        nouveau, ch = VF.fusionner("Le seroiteur est très aimé", "Le grand serviteur est fort aimé", self.dico, self.freq)
        self.assertEqual(nouveau, "Le seroiteur est très aimé")                                          # insertion / remplacements non alignés : rien

    def test_corps_et_tete(self):
        self.assertEqual(VF.corps("Khalîl-Allâh : l'ami de Dieu."), ("Khalîl-Allâh", "l'ami de Dieu."))
        self.assertEqual(VF.corps("Pas de deux points")[0], "")


class TestRelireCorps(unittest.TestCase):
    def test_relecture_avec_faux_modele(self):
        from PIL import Image
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = X.Config(pdf="faux.pdf", sortie=tmp, pages=[60], essais=2)
        ent = lambda nom, desc: {"prenoms": [{"genre": "MASCULIN", "orthographes": [nom], "arabe": "عَبْد"}], "description": desc, "description_incomplete": False}
        d = {"page": 60, "bande": 1, "decoupe": 1, "empreinte": X.empreinte(cfg), "statut": "ok", "avertissements": [],
             "entrees": [ent("Abdelbadi", "Abd al-Badî‘ : le seroiteur du Vivant, une femme très belle."), ent("Nour", "Nûr : lumière pour le Vivant.")]}
        X.ecrire_atomique(X.chemin_cache(cfg, 60, 1), json.dumps(d, ensure_ascii=False))
        mk = lambda nom, desc: LH.EntreeLue(blocs=[LH.BlocLu(genre="MASCULIN", orthographes=[nom], arabe=(0, 0, 10, 10))], description=desc, def_box=(0, 0, 200, 40))
        lec = LH.PageLue(entrees=[mk("Abdelbadi", d["entrees"][0]["description"]), mk("Nour", d["entrees"][1]["description"])], problemes=[], page_texte=False)

        def faux_llm(cfg_, info, png, systeme, texte, **kw):
            return {"content": json.dumps({"D1": "Abd al-Badî‘ : le serviteur du Vivant, une femme très belle."}, ensure_ascii=False), "reasoning": "",
                    "abandon": "", "finish": "stop", "tokens": 30, "tokens_reflexion": 0, "tokens_entree": 400, "tps": 30.0, "ttft": 0.2, "erreur": ""}

        dico = FauxDico(CONNUS | {"abd", "badî", "nûr"})
        with mock.patch.object(VF, "charger_dictionnaire", return_value=dico), mock.patch.object(X, "resoudre_modele", return_value=X.InfoModele(id="faux")), \
                mock.patch.object(X, "afficher_info"), mock.patch.object(X, "image_native", return_value=Image.new("RGB", (50, 50), "white")), \
                mock.patch.object(X, "nettoyer_page", side_effect=lambda im, *a, **k: im), mock.patch.object(LH, "lire_page", return_value=lec), \
                mock.patch.object(LH, "disponible", return_value=(True, "ok")), mock.patch.object(X, "appeler_llm", side_effect=faux_llm):
            st = X.relire_corps(cfg)
        self.assertEqual(st["entrees"], 1)                       # « femme » est connu : seule la 1re définition a un mot suspect (« seroiteur »)
        self.assertGreaterEqual(st["mots_corriges"], 1)
        with open(X.chemin_cache(cfg, 60, 1), encoding="utf-8") as f:
            apres = json.load(f)
        self.assertIn("serviteur", apres["entrees"][0]["description"])
        self.assertTrue(apres["entrees"][0]["description"].startswith("Abd al-Badî‘ : "))           # la tête n'est pas touchée
        self.assertEqual(apres["entrees"][1]["description"], "Nûr : lumière pour le Vivant.")


class TestDisjoncteurSilence(unittest.TestCase):
    def test_deux_silences_de_suite_arretent_la_passe(self):
        from PIL import Image
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = X.Config(pdf="faux.pdf", sortie=tmp, pages=[60, 61], essais=3)
        ent = lambda nom, desc: {"prenoms": [{"genre": "MASCULIN", "orthographes": [nom], "arabe": "عَبْد"}], "description": desc, "description_incomplete": False}
        for p in (60, 61):
            d = {"page": p, "bande": 1, "decoupe": 1, "empreinte": X.empreinte(cfg), "statut": "ok", "avertissements": [],
                 "entrees": [ent("Nom", "Nûr : le seroiteur du Vivant.")]}
            X.ecrire_atomique(X.chemin_cache(cfg, p, 1), json.dumps(d, ensure_ascii=False))
        mk = lambda: LH.PageLue(entrees=[LH.EntreeLue(blocs=[LH.BlocLu(genre="MASCULIN", orthographes=["Nom"], arabe=(0, 0, 10, 10))],
                                                      description="Nûr : le seroiteur du Vivant.", def_box=(0, 0, 200, 40))], problemes=[], page_texte=False)
        appels = []

        def muet(cfg_, info, png, systeme, texte, **kw):
            appels.append(1)
            return {"content": "", "reasoning": "", "abandon": "silence", "finish": "", "tokens": 0, "tokens_reflexion": 0, "tokens_entree": 0,
                    "tps": 0.0, "ttft": 0.0, "erreur": ""}

        dico = FauxDico(CONNUS | {"nûr"})
        with mock.patch.object(VF, "charger_dictionnaire", return_value=dico), mock.patch.object(X, "resoudre_modele", return_value=X.InfoModele(id="faux")), \
                mock.patch.object(X, "afficher_info"), mock.patch.object(X, "image_native", return_value=Image.new("RGB", (50, 50), "white")), \
                mock.patch.object(X, "nettoyer_page", side_effect=lambda im, *a, **k: im), mock.patch.object(LH, "lire_page", side_effect=lambda net: mk()), \
                mock.patch.object(LH, "disponible", return_value=(True, "ok")), mock.patch.object(X, "appeler_llm", side_effect=muet):
            with self.assertRaises(X.ErreurFatale) as ctx:
                X.relire_corps(cfg)
        self.assertEqual(len(appels), 2)                           # arrêt dès le 2e silence, pas 3 essais × N pages
        self.assertIn("ne répond plus", str(ctx.exception))

    def test_apostrophe_et_mots_courts_non_remplaces(self):
        dico = FauxDico(CONNUS | {"aujourd", "hui", "son"})
        nouveau, ch = VF.fusionner("le sen d’aujourd’hui", "le son d'aujourd'hui", dico, VF.Counter())
        self.assertEqual(ch, [])


class TestMixte(unittest.TestCase):
    def test_colonne_mixte_et_drapeau_de_genre(self):
        e = {"id": 1, "page": 84, "partiel": False, "description_incomplete": False,
             "prenoms": [{"genre": "MASCULIN", "orthographes": ["Tawba"], "arabe": "تَوْبَةُ"}], "description": "Tawba (mixte) : repentir, contrition."}
        r = X.exploser([e])[0]
        self.assertEqual(r["mixte"], "oui")
        self.assertNotIn("genre_a_verifier", r["à_vérifier"])
        e["description"] = "Tawba : repentir, contrition."
        r = X.exploser([e])[0]
        self.assertEqual(r["mixte"], "")
        self.assertIn("genre_a_verifier", r["à_vérifier"])


if __name__ == "__main__":
    unittest.main()
