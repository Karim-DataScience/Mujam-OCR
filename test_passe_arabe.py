"""Tests de la deuxième passe arabe (--completer-arabe) : modèle simulé, cache temporaire, aucun LM Studio.
Lancer : python -m unittest test_passe_arabe"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import extraire_prenoms_llm as X
import profils as PR

NOMS = {"Abd-ul-Ahad": "عبد الاحد", "Ahdaf": "أهداف", "Ahmad": "أحمد", "Ahsab": "أحسب", "Ajwad": "أجود", "Aali": "عالي"}


def page(p, noms, arabe_deja=(), methode=None, blanche=False):
    entrees = [{"prenoms": [{"genre": "MASCULIN", "orthographes": [n], "arabe": NOMS[n] if n in arabe_deja else ""}],
                "description": f"sens de {n}", "description_incomplete": False} for n in noms]
    d = {"page": p, "bande": 1, "statut": "ok", "entrees": entrees, "avertissements": [], "penser": False}
    if methode:
        d["methode"] = methode
    if blanche:
        d["blanche"] = True
        d["entrees"] = []
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = X.Config(sortie=self.tmp, pdf="faux.pdf", pages=[8], essais=2)
        self.requetes = []
        self.lectures = dict(NOMS)                         # ce que le « modèle » lit pour chaque nom latin
        self.patches = [mock.patch.object(X, "resoudre_modele", return_value=X.InfoModele(id="faux")), mock.patch.object(X, "afficher_info"),
                        mock.patch.object(X, "preparer_image", return_value=(b"png", 0.5)), mock.patch.object(X, "_journaliser_essai"),
                        mock.patch.object(X, "appeler_llm", side_effect=self.faux_llm)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        X.reinitialiser_profil()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def faux_llm(self, cfg, info, png, systeme, texte, **kw):
        self.requetes.append({"systeme": systeme, "texte": texte, **kw})
        sortie = []
        for ligne in texte.splitlines():
            if ligne[:1].isdigit() and ". " in ligne:
                k, nom = ligne.split(". ", 1)
                if nom in self.lectures:
                    sortie.append(f"{k}. {self.lectures[nom]}")
        return {"content": "\n".join(sortie), "reasoning": "", "finish": "stop", "abandon": "", "erreur": "", "tokens": 50, "tokens_reflexion": 0}

    def ecrire(self, p, d):
        X.ecrire_atomique(X.chemin_cache(self.cfg, p, 1), json.dumps({**d, "empreinte": X.empreinte(self.cfg)}, ensure_ascii=False))

    def lire(self, p):
        with open(X.chemin_cache(self.cfg, p, 1), encoding="utf-8") as f:
            return json.load(f)

    def arabes(self, p):
        return [e["prenoms"][0]["arabe"] for e in self.lire(p)["entrees"]]


class TestAnalyseDeLignes(unittest.TestCase):
    def test_formats_acceptes(self):
        t = "```\n1. أحمد\n2) أكبر\n3 - -\n[4]. «عالي»\nbla bla\n5: **أجود**\n1. doublon ignoré\n```"
        self.assertEqual(X.lignes_numerotees(t), {1: "أحمد", 2: "أكبر", 3: "-", 4: "عالي", 5: "أجود"})
        self.assertEqual(X.lignes_numerotees(""), {})
        self.assertEqual(X.lignes_numerotees(None), {})

    def test_texte_numerote_avec_les_noms_latins(self):
        t = X.texte_passe_arabe(["Ahmad", "Abd-ul-Ahad / Abdul Ahad"])
        self.assertIn("1. Ahmad", t)
        self.assertIn("2. Abd-ul-Ahad / Abdul Ahad", t)
        self.assertIn("numéro. -", t)


class TestPasse(Base):
    def test_page_entierement_vide_est_completee(self):
        self.ecrire(8, page(8, list(NOMS)))
        st = X.completer_arabe(self.cfg)
        self.assertEqual((st["adoptes"], st["refuses"], st["pages"], st["requetes"]), (6, 0, 1, 1))
        self.assertEqual(self.arabes(8), list(NOMS.values()))
        d = self.lire(8)
        self.assertEqual(len(d["passe_arabe"]), 6)
        self.assertEqual(d["empreinte"], X.empreinte(self.cfg))                 # le cache reste valide
        self.assertIn("passe arabe", d["avertissements"][-1])

    def test_conditions_de_la_requete(self):
        self.ecrire(8, page(8, list(NOMS)))
        X.completer_arabe(self.cfg)
        r = self.requetes[0]
        self.assertIs(r["penser"], False)                                       # sans réflexion
        self.assertIn("RECOPIES", r["systeme"])
        self.assertIn("3. Ahmad", r["texte"])                                   # liste numérotée de TOUS les noms de la page
        self.assertGreaterEqual(r["max_tokens"], 300)

    def test_lecture_incoherente_refusee_et_gardee_en_proposition(self):
        self.lectures["Ajwad"] = "أجرد"                                         # cas réel du benchmark : د remplacé par ر
        self.ecrire(8, page(8, list(NOMS)))
        st = X.completer_arabe(self.cfg)
        self.assertEqual((st["adoptes"], st["refuses"]), (5, 1))
        self.assertEqual(self.arabes(8)[4], "")                                 # plutôt vide que faux
        prop = self.lire(8)["propositions_arabe"]
        self.assertEqual((prop[0]["nom"], prop[0]["arabe"]), ("Ajwad", "أجرد"))

    def test_csv_de_revue(self):
        import csv
        self.lectures["Ajwad"] = "أجرد"
        self.ecrire(8, page(8, list(NOMS)))
        st = X.completer_arabe(self.cfg)
        with open(st["csv"], encoding="utf-8-sig", newline="") as f:
            lignes = list(csv.DictReader(f, delimiter=";"))
        self.assertEqual(len(lignes), 6)
        refusee = [r for r in lignes if r["decision"].startswith("refus")]
        self.assertEqual((len(refusee), refusee[0]["nom"], refusee[0]["arabe"], refusee[0]["page"]), (1, "Ajwad", "أجرد", "8"))
        self.assertEqual(sum(r["decision"] == "adoptée" for r in lignes), 5)

    def test_pas_de_csv_sans_lecture(self):
        self.lectures.clear()
        self.ecrire(8, page(8, list(NOMS)))
        st = X.completer_arabe(self.cfg)
        self.assertNotIn("csv", st)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "passe_arabe.csv")))

    def test_seuil_parametrable(self):
        self.lectures["Abd-ul-Ahad"] = "عبد الواحد"                              # plausible mais faux : coût 0,25
        self.ecrire(8, page(8, ["Abd-ul-Ahad"]))
        self.assertEqual(X.completer_arabe(self.cfg)["adoptes"], 0)             # seuil par défaut 0,2 : refusée
        self.cfg.seuil_passe2 = 0.3
        self.assertEqual(X.completer_arabe(self.cfg)["adoptes"], 1)             # seuil plus permissif : adoptée

    def test_lectures_non_arabes_ou_absentes_ignorees(self):
        self.lectures.update({"Ahmad": "Ahmad", "Ahsab": "-", "Aali": ""})
        self.ecrire(8, page(8, list(NOMS)))
        st = X.completer_arabe(self.cfg)
        self.assertEqual(st["adoptes"], 3)
        self.assertEqual(st["non_lus"], 3)
        self.assertEqual(self.arabes(8)[2], "")

    def test_page_partielle_seuls_les_blocs_vides_sont_remplis(self):
        deja = ("Abd-ul-Ahad", "Ahdaf", "Ahmad")
        self.ecrire(8, page(8, list(NOMS), arabe_deja=deja))
        st = X.completer_arabe(self.cfg)
        self.assertEqual(st["blocs_vides"], 3)
        self.assertEqual(self.arabes(8), list(NOMS.values()))

    def test_controle_d_ancrage_page_douteuse(self):
        """Si la nouvelle lecture contredit les blocs DÉJÀ lus, l'alignement est suspect : rien n'est écrit."""
        for n in ("Abd-ul-Ahad", "Ahdaf", "Ahmad"):
            self.lectures[n] = "ظظظظ"
        self.ecrire(8, page(8, list(NOMS), arabe_deja=("Abd-ul-Ahad", "Ahdaf", "Ahmad")))
        avant = self.lire(8)
        st = X.completer_arabe(self.cfg)
        self.assertEqual((st["pages_douteuses"], st["adoptes"]), (1, 0))
        self.assertEqual(self.lire(8), avant)

    def test_pages_ignorees(self):
        self.cfg.pages = [8, 9, 10, 11]
        self.ecrire(8, page(8, ["Ahmad"], arabe_deja=("Ahmad",)))               # rien à compléter
        self.ecrire(9, page(9, ["Ahmad"], methode="hybride"))                   # lecture hybride : c'est --relire-arabe
        self.ecrire(10, page(10, [], blanche=True))
        X.completer_arabe(self.cfg)                                              # aucune page éligible : le modèle n'est même pas sollicité
        self.assertEqual(self.requetes, [])
        X.resoudre_modele.assert_not_called()
        self.assertEqual(self.arabes(9), [""])                                  # (11 : pas de cache du tout)

    def test_bloc_valide_a_la_main_jamais_ecrase(self):
        d = page(8, ["Ahmad", "Aali"])
        d["entrees"][0]["prenoms"][0].update({"arabe": "", "valide": True})
        self.ecrire(8, d)
        X.completer_arabe(self.cfg)
        self.assertEqual(self.arabes(8), ["", "عالي"])

    def test_profil_sans_arabe_attendu(self):
        X.ARABE_ATTENDU = False
        self.ecrire(8, page(8, list(NOMS)))
        st = X.completer_arabe(self.cfg)
        self.assertEqual((st["requetes"], st["pages"]), (0, 0))

    def test_erreur_du_modele_ne_plante_pas(self):
        X.appeler_llm.side_effect = X.ErreurLLM("HTTP 500")
        self.ecrire(8, page(8, list(NOMS)))
        st = X.completer_arabe(self.cfg)
        self.assertEqual((st["adoptes"], st["requetes"]), (0, 0))
        self.assertEqual(self.arabes(8), [""] * 6)

    def test_deuxieme_essai_complete_les_numeros_manquants(self):
        appels = {"n": 0}
        base = self.faux_llm

        def une_ligne_sur_deux(cfg, info, png, systeme, texte, **kw):
            appels["n"] += 1
            r = base(cfg, info, png, systeme, texte, **kw)
            if appels["n"] == 1:
                r["content"] = "\n".join(r["content"].splitlines()[:3])           # 1er essai : réponse tronquée
            return r
        X.appeler_llm.side_effect = une_ligne_sur_deux
        self.ecrire(8, page(8, list(NOMS)))
        st = X.completer_arabe(self.cfg)
        self.assertEqual((st["requetes"], st["adoptes"]), (2, 6))

    def test_silence_repete_arrete_la_passe(self):
        X.appeler_llm.side_effect = lambda *a, **k: {"content": "", "reasoning": "", "finish": "", "abandon": "silence", "erreur": "", "tokens": 0, "tokens_reflexion": 0}
        self.cfg.pages = [8, 9, 10]
        for p in (8, 9, 10):
            self.ecrire(p, page(p, list(NOMS)))
        with self.assertRaises(X.ErreurFatale):
            X.completer_arabe(self.cfg)


class TestProfilEtCLI(unittest.TestCase):
    def tearDown(self):
        X.reinitialiser_profil()

    def test_drapeau_de_profil(self):
        p = PR.charger("abdur_rahman_dictionary_of_muslim_names")
        a = mock.Mock(pages="", debut=1, fin=1, methode="auto", sortie="", pdf="x.pdf")
        X.appliquer_profil({**p, "arabe_attendu": False}, a, [])
        self.assertFalse(X.ARABE_ATTENDU)
        X.appliquer_profil(p, a, [])
        self.assertTrue(X.ARABE_ATTENDU)                                         # chaque profil repart de la valeur par défaut
        X.appliquer_profil({**p, "arabe_attendu": False}, a, [])
        X.reinitialiser_profil()
        self.assertTrue(X.ARABE_ATTENDU)

    def test_options_de_ligne_de_commande(self):
        import io
        import contextlib
        with mock.patch.object(X.sys, "argv", ["x", "--help"]), contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            X.main()
        self.assertIn("--completer-arabe", out.getvalue())
        self.assertIn("--seuil-passe2", out.getvalue())

    def test_seuil_transmis_a_la_config(self):
        a = X.valeurs_effectives(os.path.join(tempfile.gettempdir(), "prenoms_aucune_config.json"))
        self.assertEqual(a["seuil_passe2"], 0.2)
        import argparse
        a["seuil_passe2"] = 0.35
        self.assertEqual(X.construire_cfg(argparse.Namespace(**a)).seuil_passe2, 0.35)


if __name__ == "__main__":
    unittest.main()
