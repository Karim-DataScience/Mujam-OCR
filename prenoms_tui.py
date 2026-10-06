# -*- coding: utf-8 -*-
"""
prenoms_tui.py — interface plein écran (Textual) pour tout gérer
================================================================
Lancer :   python extraire_prenoms_llm.py        (sans argument, dans un terminal)
      ou   python prenoms_tui.py

Onglets  F1 Accueil · F2 Extraction · F3 Pages · F4 Résultats · F5 Réglages · F6 Journal
Touches  F7 lancer / reprendre · F8 arrêter · Q quitter · Espace (onglet Pages) cocher une page

Tout passe par le moteur (extraire_prenoms_llm.py) : les mêmes options, le même cache, le même journal. Les réglages
sont enregistrés dans prenoms_config.json ; la ligne de commande reste utilisable en parallèle.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time

import extraire_prenoms_llm as X
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, ProgressBar, RichLog, Select, Static, Switch,
                             TabbedContent, TabPane)

# réglages éditables : (clé, libellé, genre, options)
CHAMPS = [
    ("pdf", "Fichier PDF du livre", "texte", None),
    ("url", "URL de LM Studio", "texte", None),
    ("modele", "Modèle (nom ou identifiant)", "texte", None),
    ("api", "API", "choix", ("auto", "natif", "openai")),
    ("cle_api", "Jeton d'API (si l'authentification est activée)", "secret", None),
    ("sortie", "Dossier de sortie (vide = sortie_llm à côté du script)", "texte", None),
    ("pages", "Pages (vide = 53-338 ; ex. 55,56,100-104)", "texte", None),
    ("reflexion", "Réflexion du modèle", "choix", ("auto", "non", "oui")),
    ("budget_reflexion", "Budget de réflexion en jetons (0 = automatique)", "entier", None),
    ("tokens_image", "Jetons d'image visés (0 = image native)", "entier", None),
    ("max_cote", "Plafond manuel du côté de l'image, en px (0 = automatique)", "entier", None),
    ("decoupe", "Découpe de page (1 = entière, 2 = deux moitiés)", "choix", ("1", "2")),
    ("paralleles", "Requêtes simultanées", "entier", None),
    ("max_tokens", "Jetons de sortie max par requête", "entier", None),
    ("timeout", "Durée max d'une requête (s)", "entier", None),
    ("stall", "Silence max entre deux jetons (s)", "entier", None),
    ("attente", "Attente max du préremplissage (s)", "entier", None),
    ("essais", "Tentatives par page", "entier", None),
    ("export_tous", "Export intermédiaire tous les N pages (0 = à la fin seulement)", "entier", None),
    ("nettoyage", "Nettoyage des fantômes du verso", "choix", ("oui", "non")),
    ("ancien", "CSV de l'ancienne extraction (comparaison)", "texte", None),
    ("log_niveau", "Niveau écrit dans le fichier de log", "choix", ("DEBUG", "INFO")),
]
COULEURS_STATUT = {"ok": "green", "partiel": "yellow", "périmé": "magenta", "à faire": "grey62", "blanche": "grey50", "échec": "red bold"}
LIMITE_RESULTATS = 2500


class LigneJournal(Message):
    """Une ligne du journal, postée depuis n'importe quel fil vers l'interface."""

    def __init__(self, niveau: str, texte: str) -> None:
        super().__init__()
        self.niveau, self.texte = niveau, texte


class Confirmer(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "non", "Non")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="cadre-conf"):
            yield Label(self.question)
            with Horizontal(classes="boutons"):
                yield Button("Oui", id="oui", variant="error")
                yield Button("Non", id="non", variant="primary")

    @on(Button.Pressed)
    def choisir(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id == "oui")

    def action_non(self) -> None:
        self.dismiss(False)


class VueTexte(ModalScreen[None]):
    BINDINGS = [Binding("escape", "fermer", "Fermer")]

    def __init__(self, titre: str, contenu: str) -> None:
        super().__init__()
        self.titre, self.contenu = titre, contenu

    def compose(self) -> ComposeResult:
        with Vertical(id="cadre-vue"):
            yield Label(self.titre)
            with VerticalScroll():
                yield Static(Text(self.contenu), markup=False)
            yield Button("Fermer (Échap)", id="fermer", variant="primary")

    @on(Button.Pressed, "#fermer")
    def fermer(self) -> None:
        self.dismiss(None)

    def action_fermer(self) -> None:
        self.dismiss(None)


class Onglets(TabbedContent):
    """TabbedContent dont un changement d'onglet demandé par un bouton n'est pas annulé après coup.

    Textual réactive l'onglet d'un widget qui prend le focus. Quand on clique un bouton (qui prend le focus) puis que son
    gestionnaire change d'onglet, l'événement « focus » encore en file d'attente ramenait l'onglet précédent. Un événement
    est donc ignoré si le widget qui a le focus n'est plus dans cet onglet."""

    def _on_tab_pane_focused(self, event) -> None:
        focus = self.screen.focused if self.screen is not None else None
        if focus is not None and event.tab_pane in focus.ancestors_with_self:
            super()._on_tab_pane_focused(event)
        else:
            event.stop()


class TablePages(DataTable):
    BINDINGS = [Binding("space", "basculer", "Cocher / décocher")]

    def action_basculer(self) -> None:
        self.app.basculer_selection()                                  # type: ignore[attr-defined]


def ouvrir_avec_le_systeme(chemin: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(chemin)                                           # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", chemin])
    else:
        subprocess.Popen(["xdg-open", chemin])


def fmt_duree(s) -> str:
    return X.fmt_duree(s) if s is not None else "?"


def fixer_select(widget: Select, valeur, options, defaut=None) -> None:
    """Affecte une valeur à un Select sans jamais lever d'erreur si elle n'est pas dans les options (ex. configuration à la main)."""
    v = str(valeur)
    widget.value = v if v in options else (defaut if defaut in options else options[0])


class PrenomsApp(App):
    TITLE = "Prénoms arabes — extraction (LM Studio)"
    CSS = """
    .carte { border: round $primary; padding: 0 1; margin: 0 0 1 0; height: auto; }
    .boutons { height: auto; margin: 1 0; overflow-x: auto; }
    .boutons Input { width: 40; }
    .boutons Static { width: auto; }
    .boutons Button { margin-right: 1; }
    #form-extraction { width: 40; padding: 0 1; }
    #suivi-extraction { width: 1fr; padding: 0 1; }
    #e-actifs { height: 8; }
    #log-extraction { height: 1fr; }
    #log-accueil { height: 12; }
    #journal { height: 1fr; }
    #p-table, #r-table { height: 1fr; }
    RichLog { border: round $secondary; }
    Confirmer, VueTexte { align: center middle; }
    #cadre-conf { width: 60; height: auto; border: thick $error; background: $surface; padding: 1 2; }
    #cadre-vue { width: 92%; height: 92%; border: thick $primary; background: $surface; padding: 1 2; }
    """
    BINDINGS = [
        Binding("f1", "onglet('accueil')", "Accueil"),
        Binding("f2", "onglet('extraction')", "Extraction"),
        Binding("f3", "onglet('pages')", "Pages"),
        Binding("f4", "onglet('resultats')", "Résultats"),
        Binding("f5", "onglet('reglages')", "Réglages"),
        Binding("f6", "onglet('journal')", "Journal"),
        Binding("f7", "lancer", "Lancer/reprendre"),
        Binding("f8", "arreter", "Arrêter"),
        Binding("q", "quitter", "Quitter"),
    ]

    def __init__(self, chemin_config: str = "", auto_test: bool = True, **kw) -> None:
        super().__init__(**kw)
        self.chemin_config = chemin_config or X.CONFIG_DEFAUT
        self.reglages: dict = X.valeurs_effectives(self.chemin_config)
        self.auto_test = auto_test
        self.controle: X.Controle | None = None
        self.en_cours = False
        self.selection: set[tuple[int, int]] = set()
        self.infos_derniere: dict = {}
        self.lignes_resultats: list[dict] = []
        self.derniere_issue = ""

    def vers_ui(self, fonction, *args, **kw) -> None:
        """Appel depuis un fil d'exécution vers l'interface ; sans effet (et sans erreur) si l'application est déjà fermée."""
        try:
            self.call_from_thread(fonction, *args, **kw)
        except Exception:                                              # noqa: BLE001
            pass

    def q(self, selecteur, type_=None):
        """query_one sur l'écran de BASE : une fenêtre modale (confirmation, sortie brute) ne doit pas masquer l'interface."""
        piles = self.screen_stack
        racine = piles[0] if piles else self.screen
        return racine.query_one(selecteur, type_) if type_ is not None else racine.query_one(selecteur)

    # ------------------------------------------------------------------ construction de l'écran
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Onglets(initial="accueil", id="onglets"):
            with TabPane("Accueil", id="accueil"):
                with VerticalScroll():
                    yield Static(Text("Étapes : 1) Réglages (F5) : PDF, URL de LM Studio, modèle · 2) « Tester LM Studio » puis « Diagnostic complet » · "
                                      "3) « Lancer / reprendre » (reprise automatique) · 4) Pages (F3) et Résultats (F4) · "
                                      "En cas de souci : « Rapport de debug » crée un zip à envoyer."), id="carte-guide", classes="carte", markup=False)
                    with Horizontal(classes="boutons"):
                        yield Button("Tester LM Studio", id="b-tester", variant="primary")
                        yield Button("Diagnostic complet", id="b-diag")
                        yield Button("Lancer / reprendre", id="b-lancer", variant="success")
                        yield Button("Exporter", id="b-export")
                        yield Button("Rapport de debug", id="b-rapport")
                    yield Static(Text("LM Studio : pas encore testé — bouton « Tester LM Studio »."), id="carte-lm", classes="carte", markup=False)
                    yield Static(id="carte-cache", classes="carte", markup=False)
                    yield Static(id="carte-chemins", classes="carte", markup=False)
                    yield RichLog(id="log-accueil", wrap=True, markup=False, highlight=False, max_lines=500)
            with TabPane("Extraction", id="extraction"):
                with Horizontal():
                    with Vertical(id="form-extraction"):
                        yield Label("Pages (53-338, 55,56, 100-104…)")
                        yield Input(id="e-pages", placeholder="vide = réglages")
                        yield Label("Réflexion")
                        yield Select([("auto", "auto"), ("non (rapide)", "non"), ("oui (bornée)", "oui")], value="auto",
                                     allow_blank=False, id="e-reflexion")
                        yield Label("Découpe")
                        yield Select([("1 · page entière", "1"), ("2 · deux moitiés", "2")], value="1", allow_blank=False, id="e-decoupe")
                        yield Label("Requêtes simultanées")
                        yield Input(id="e-paralleles", value="1")
                        with Horizontal(classes="boutons"):
                            yield Switch(id="e-refaire", value=False)
                            yield Label(" Refaire même si en cache")
                        yield Button("▶ Lancer / reprendre", id="e-lancer", variant="success")
                        yield Button("■ Arrêter", id="e-arreter", variant="error")
                    with Vertical(id="suivi-extraction"):
                        yield Static("Aucune extraction en cours.", id="e-resume", markup=False)
                        yield ProgressBar(total=100, id="e-barre", show_eta=False)
                        yield Static("", id="e-eta", markup=False)
                        yield DataTable(id="e-actifs", cursor_type="none")
                        yield RichLog(id="log-extraction", wrap=True, markup=False, highlight=False, max_lines=1000)
            with TabPane("Pages", id="pages"):
                with Horizontal(classes="boutons"):
                    yield Button("Actualiser", id="p-actualiser")
                    yield Button("Cocher à refaire", id="p-selectionner")
                    yield Button("Refaire sans réfl.", id="p-refaire-non", variant="primary")
                    yield Button("Refaire avec réfl.", id="p-refaire-oui", variant="warning")
                    yield Button("Sortie brute", id="p-brut")
                    yield Button("Image", id="p-image")
                yield Static("Espace : cocher / décocher la page sélectionnée. « Cocher à refaire » : échecs, partielles, périmées, à faire.",
                             id="p-aide", markup=False)
                yield TablePages(id="p-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Résultats", id="resultats"):
                with Horizontal(classes="boutons"):
                    yield Input(id="r-filtre", placeholder="Filtrer : prénom, arabe, description…")
                    yield Switch(id="r-flags", value=False)
                    yield Label(" seulement « à vérifier »")
                    yield Button("Recharger", id="r-recharger")
                    yield Button("Ouvrir le dossier", id="r-dossier")
                yield Static("", id="r-stats", markup=False)
                yield DataTable(id="r-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Réglages", id="reglages"):
                with VerticalScroll():
                    with Horizontal(classes="boutons"):
                        yield Button("Enregistrer", id="s-enregistrer", variant="success")
                        yield Button("Valeurs par défaut", id="s-defauts")
                        yield Static("", id="s-info", markup=False)
                    for cle, libelle, genre, options in CHAMPS:
                        yield Label(libelle)
                        if genre == "choix":
                            yield Select([(o, o) for o in options], value=options[0], allow_blank=False, id=f"s-{cle}")
                        else:
                            yield Input(id=f"s-{cle}", password=(genre == "secret"))
            with TabPane("Journal", id="journal-tab"):
                with Horizontal(classes="boutons"):
                    yield Button("Ouvrir le log", id="j-ouvrir")
                    yield Button("Rapport de debug", id="j-rapport", variant="primary")
                    yield Button("Effacer l'affichage", id="j-effacer")
                yield Static("", id="j-chemin", markup=False)
                yield RichLog(id="journal", wrap=True, markup=False, highlight=False, max_lines=5000)
        yield Footer()

    # ------------------------------------------------------------------ cycle de vie
    def on_mount(self) -> None:
        X.MUET = True
        X.ECOUTEURS.append(self._ecouteur)
        X.installer_hooks()
        try:
            cfg = X.construire_cfg(argparse.Namespace(**self.reglages))
            X.JOURNAL.ouvrir(os.path.join(cfg.sortie, "logs"), "tui", cfg.log_niveau)
        except (ValueError, OSError) as e:
            self.notify(f"Journal indisponible : {e}", severity="warning")
        self.q("#j-chemin", Static).update(Text(f"Fichier de log : {X.JOURNAL.chemin or '(non ouvert)'}"))
        self.remplir_reglages()
        self.preparer_tables()
        self.set_interval(0.5, self.rafraichir_extraction)
        self.set_interval(5.0, self.rafraichir_lent)
        X.log(f"interface prête — version {X.VERSION} — journal : {X.JOURNAL.chemin}")
        try:
            cfg0 = X.construire_cfg(argparse.Namespace(**self.reglages))
            self.run_worker(lambda: X.journaliser_demarrage("tui", sys.argv[1:], cfg0), thread=True, name="env", exit_on_error=False)
        except (ValueError, OSError):
            pass
        self.rafraichir_accueil()
        self.charger_pages()
        self.charger_resultats()
        if self.auto_test:
            self.run_worker(self._tester_lm, thread=True, name="test-lm", exit_on_error=False)

    def on_unmount(self) -> None:
        X.MUET = False
        if self._ecouteur in X.ECOUTEURS:
            X.ECOUTEURS.remove(self._ecouteur)
        if self.controle is not None:
            self.controle.stop.set()
        X.JOURNAL.fermer()

    def _ecouteur(self, niveau: str, ligne: str) -> None:
        self.post_message(LigneJournal(niveau, ligne))

    def on_ligne_journal(self, message: LigneJournal) -> None:
        style = {"WARN": "yellow", "ERROR": "bold red"}.get(message.niveau, "")
        t = Text(message.texte, style=style)
        for ident in ("#journal", "#log-accueil", "#log-extraction"):
            try:
                self.q(ident, RichLog).write(t)
            except Exception:                                          # noqa: BLE001 : l'écran peut être en cours de démontage
                pass

    # ------------------------------------------------------------------ réglages <-> configuration
    def champ(self, cle: str):
        return self.q(f"#s-{cle}")

    def remplir_reglages(self) -> None:
        for cle, _, genre, options in CHAMPS:
            v = self.reglages.get(cle)
            w = self.champ(cle)
            if genre == "choix":
                fixer_select(w, v, options)
            else:
                w.value = "" if v is None else str(v)
        self.remplir_extraction()

    def remplir_extraction(self) -> None:
        self.q("#e-pages", Input).value = str(self.reglages.get("pages") or "")
        fixer_select(self.q("#e-reflexion", Select), self.reglages.get("reflexion", "auto"), ("auto", "non", "oui"))
        fixer_select(self.q("#e-decoupe", Select), self.reglages.get("decoupe", 1), ("1", "2"))
        self.q("#e-paralleles", Input).value = str(self.reglages.get("paralleles", 1))

    def lire_reglages(self) -> dict:
        d = dict(self.reglages)
        for cle, libelle, genre, _ in CHAMPS:
            w = self.champ(cle)
            if genre == "entier":
                try:
                    d[cle] = int(str(w.value).strip() or 0)
                except ValueError:
                    raise ValueError(f"« {libelle} » : un nombre entier est attendu")
            elif genre == "choix":
                d[cle] = int(w.value) if cle == "decoupe" else str(w.value)
            else:
                d[cle] = str(w.value).strip()
        return d

    @on(Button.Pressed, "#s-enregistrer")
    def enregistrer_reglages(self) -> None:
        try:
            d = self.lire_reglages()
            X.construire_cfg(argparse.Namespace(**d))                    # valide l'ensemble avant d'écrire
        except (ValueError, OSError) as e:
            self.notify(str(e), severity="error", timeout=8)
            return
        self.reglages = d
        X.ecrire_config(self.chemin_config, d)
        X.log(f"réglages enregistrés : {self.chemin_config}")
        self.q("#s-info", Static).update(Text(f"Enregistré : {self.chemin_config}"))
        self.notify(f"Réglages enregistrés dans {self.chemin_config}")
        self.remplir_extraction()
        self.rafraichir_accueil()
        self.charger_pages()
        self.charger_resultats()

    @on(Button.Pressed, "#s-defauts")
    def valeurs_par_defaut(self) -> None:
        self.reglages = X.valeurs_effectives(os.path.join(tempfile.gettempdir(), "prenoms_aucune_config.json"))
        self.remplir_reglages()
        self.notify("Valeurs par défaut chargées (pas encore enregistrées).")

    def cfg_courante(self, **surcharges) -> X.Config:
        """Réglages enregistrés, puis champs de l'onglet Extraction, puis surcharges explicites."""
        d = dict(self.reglages)
        pages = self.q("#e-pages", Input).value.strip()
        if pages:
            d["pages"] = pages
        d["reflexion"] = str(self.q("#e-reflexion", Select).value)
        d["decoupe"] = int(self.q("#e-decoupe", Select).value)
        para = self.q("#e-paralleles", Input).value.strip()
        if para:
            d["paralleles"] = int(para)
        d["refaire"] = bool(self.q("#e-refaire", Switch).value)
        d.update(surcharges)
        cfg = X.construire_cfg(argparse.Namespace(**d))
        cfg.suivi_ui = 1.0
        return cfg

    def cfg_ou_alerte(self, **surcharges):
        try:
            return self.cfg_courante(**surcharges)
        except (ValueError, OSError) as e:
            self.notify(str(e), severity="error", timeout=8)
            return None

    # ------------------------------------------------------------------ navigation
    def action_onglet(self, nom: str) -> None:
        self.q(TabbedContent).active = "journal-tab" if nom == "journal" else nom
        self.screen.set_focus(None)                                    # le bouton cliqué ne doit pas ramener l'ancien onglet
        if nom == "pages":
            self.charger_pages()
        elif nom == "resultats":
            self.charger_resultats()
        elif nom == "accueil":
            self.rafraichir_accueil()

    # ------------------------------------------------------------------ accueil
    def rafraichir_accueil(self) -> None:
        cfg = self.cfg_ou_alerte()
        if cfg is None:
            return
        e = X.etat_cache(cfg)
        c = e["cats"]
        lignes = [f"Avancement — pages {cfg.pages[0]}–{cfg.pages[-1]} ({len(cfg.pages)} pages, découpe {cfg.decoupe})" if cfg.pages else "Aucune page",
                  f"  ok {len(c['ok'])}   partielles {len(c['partiel'])}   périmées {len(c['périmé'])}   à faire {len(c['à faire'])}   blanches {len(c['blanche'])}"]
        if e["moyenne"] and e["reste"]:
            lignes.append(f"  durée moyenne {e['moyenne']:.0f} s par page → reste ≈ {fmt_duree(e['reste_s'])}")
        self.q("#carte-cache", Static).update(Text("\n".join(lignes)))
        pdf_ok = "✔" if cfg.pdf and os.path.exists(cfg.pdf) else "✘ à renseigner dans Réglages (F5)"
        chemins = [f"PDF : {cfg.pdf or '—'}  {pdf_ok}", f"Sortie : {cfg.sortie}", f"Cache : {os.path.join(cfg.sortie, 'cache')}",
                   f"Journal : {X.JOURNAL.chemin or '—'}", f"Configuration : {self.chemin_config}"]
        self.q("#carte-chemins", Static).update(Text("\n".join(chemins)))

    def afficher_lm(self, info, erreur: str = "") -> None:
        if info is None:
            texte = f"LM Studio : ✘ {erreur}"
        else:
            texte = (f"LM Studio : ✔ {info.id}  ({info.quant or '?'}, {info.taille_go or '?'} Go, {info.format or '?'})\n"
                     f"  API {'native' if info.natif else 'OpenAI'} — contexte {info.ctx or '?'} — requêtes simultanées {info.parallel or '?'} — "
                     f"taille de lot {info.batch or '?'}\n"
                     f"  Flash Attention {X._oui_non(info.flash)} — KV sur GPU {X._oui_non(info.kv_gpu)} — vision {X._oui_non(info.vision)} — "
                     f"réflexion pilotable : {info.options_reflexion or 'non'}")
        self.q("#carte-lm", Static).update(Text(texte))

    def _tester_lm(self) -> None:
        try:
            cfg = X.construire_cfg(argparse.Namespace(**self.reglages))
            info = X.resoudre_modele(cfg)
            X.evenement("modele", **X.asdict(info))
            self.vers_ui(self.afficher_lm, info)
        except (X.ErreurFatale, X.ErreurLLM, ValueError, OSError) as e:
            self.vers_ui(self.afficher_lm, None, str(e))

    @on(Button.Pressed, "#b-tester")
    def tester_lm(self) -> None:
        self.q("#carte-lm", Static).update(Text("LM Studio : test en cours…"))
        self.run_worker(self._tester_lm, thread=True, name="test-lm", exit_on_error=False)

    @on(Button.Pressed, "#b-diag")
    def diagnostic(self) -> None:
        cfg = self.cfg_ou_alerte()
        if cfg is None:
            return
        self.notify("Diagnostic en cours (30 s à quelques minutes) ; résultats dans le journal.")
        self.action_onglet("journal")

        def travail() -> None:
            try:
                X.diagnostic(cfg)
            except (X.ErreurFatale, X.ErreurLLM) as e:
                X.log(f"ERREUR : {e}", "ERROR")
            except Exception as e:                                     # noqa: BLE001
                X.log_exception("diagnostic", e)
            self.vers_ui(self.notify, "Diagnostic terminé : voir le journal (F6).")
        self.run_worker(travail, thread=True, name="diagnostic", exit_on_error=False)

    @on(Button.Pressed, "#b-lancer")
    def lancer_depuis_accueil(self) -> None:
        self.lancer_extraction()

    @on(Button.Pressed, "#b-export")
    def exporter_maintenant(self) -> None:
        cfg = self.cfg_ou_alerte(export=True)
        if cfg is None:
            return

        def travail() -> None:
            try:
                rows, infos, avert = X.run_pipeline(cfg)
                ch = X.exporter(rows, infos, cfg.sortie)
                X.evenement("export", intermediaire=False, lignes=len(rows), **ch)
                X.log(f"export : {len(rows)} lignes -> " + " | ".join(ch.values()))
                self.vers_ui(self.notify, f"Export : {len(rows)} lignes → {ch.get('csv')}")
            except Exception as e:                                     # noqa: BLE001
                X.log_exception("export", e)
                self.vers_ui(self.notify, f"Export impossible : {e}", severity="error")
        self.run_worker(travail, thread=True, name="export", exit_on_error=False)

    @on(Button.Pressed, "#b-rapport")
    @on(Button.Pressed, "#j-rapport")
    def rapport(self) -> None:
        cfg = self.cfg_ou_alerte()
        if cfg is None:
            return

        def travail() -> None:
            try:
                chemin = X.creer_rapport(cfg, self.chemin_config)
                X.log(f"rapport de debug : {chemin}")
                self.vers_ui(self.notify, f"Rapport créé : {chemin}", timeout=15)
            except Exception as e:                                     # noqa: BLE001
                X.log_exception("rapport de debug", e)
                self.vers_ui(self.notify, f"Rapport impossible : {e}", severity="error")
        self.run_worker(travail, thread=True, name="rapport", exit_on_error=False)

    # ------------------------------------------------------------------ extraction
    @on(Button.Pressed, "#e-lancer")
    def action_lancer(self) -> None:
        self.lancer_extraction()

    @on(Button.Pressed, "#e-arreter")
    def action_arreter(self) -> None:
        if self.en_cours and self.controle is not None:
            self.controle.stop.set()
            X.log("arrêt demandé depuis l'interface")
            self.notify("Arrêt demandé : les pages terminées sont conservées.")
        else:
            self.notify("Aucune extraction en cours.")

    def lancer_extraction(self, basculer: bool = True, **surcharges) -> None:
        if self.en_cours:
            self.notify("Une extraction est déjà en cours.", severity="warning")
            return
        cfg = self.cfg_ou_alerte(**surcharges)
        if cfg is None:
            return
        if not cfg.sans_llm and not (cfg.pdf and os.path.exists(cfg.pdf)):
            self.notify("Indique d'abord le fichier PDF dans l'onglet Réglages (F5).", severity="error", timeout=8)
            if basculer:
                self.action_onglet("reglages")
            return
        controle = X.Controle()
        self.controle, self.en_cours, self.derniere_issue = controle, True, ""
        if basculer:
            self.action_onglet("extraction")
        X.log(f"extraction demandée : {len(cfg.pages)} page(s), découpe {cfg.decoupe}, réflexion {cfg.reflexion}, "
              f"parallèle {cfg.paralleles}{', refaire' if cfg.refaire else ''}")
        self.run_worker(lambda: self._executer_extraction(cfg, controle), thread=True, group="extraction", name="extraction",
                        exit_on_error=False)

    def _executer_extraction(self, cfg: X.Config, controle: X.Controle) -> None:
        issue = ("termine", "")
        try:
            rows, infos, avert = X.run_pipeline(cfg, controle)
            self.infos_derniere = {i["page_pdf"]: i for i in infos}
            if cfg.fatal and not rows:
                issue = ("erreur", cfg.fatal)
            else:
                ch = X.exporter(rows, infos, cfg.sortie)
                X.evenement("export", intermediaire=False, lignes=len(rows), **ch)
                X.resume(rows, infos, avert)
                if cfg.ancien:
                    X.comparer_ancien(rows, cfg.ancien, cfg.pages, cfg.sortie)
                X.log("Fichiers : " + " | ".join(ch.values()))
                issue = ("erreur", cfg.fatal) if cfg.fatal else (("interrompu", "") if cfg.interrompu else ("termine", ""))
        except (X.ErreurFatale, X.ErreurLLM) as e:
            X.log(f"ERREUR : {e}", "ERROR")
            X.evenement("fatal", message=str(e))
            issue = ("erreur", str(e))
        except Exception as e:                                         # noqa: BLE001
            X.log_exception("erreur inattendue", e)
            issue = ("erreur", f"{type(e).__name__}: {e} (détail dans le journal)")
        self.vers_ui(self.fin_extraction, issue)

    def fin_extraction(self, issue: tuple) -> None:
        self.en_cours = False
        self.derniere_issue = issue[0]
        self.selection.clear()
        etat, message = issue
        if etat == "termine":
            self.notify("Extraction terminée.", title="Prénoms")
        elif etat == "interrompu":
            self.notify("Extraction interrompue : les pages terminées sont conservées (relance pour reprendre).", severity="warning")
        else:
            self.notify(message or "Erreur (voir le journal).", severity="error", timeout=12)
        self.rafraichir_extraction()
        self.rafraichir_accueil()
        self.charger_pages()
        self.charger_resultats()

    def rafraichir_extraction(self) -> None:
        try:
            self._rafraichir_extraction()
        except (NoMatches, IndexError):                                # écran en cours de démontage
            pass

    def _rafraichir_extraction(self) -> None:
        c = self.controle
        if c is None:
            return
        s = c.instantane()
        total = max(1, s["total"])
        self.q("#e-barre", ProgressBar).update(total=total, progress=s["fait"])
        etat = {"attente": "en attente", "calibrage": "calibrage…", "en cours": "en cours", "termine": "terminée",
                "interrompu": "interrompue", "erreur": "erreur"}.get(s["etat"], s["etat"])
        resume = f"État : {etat} — {s['fait']}/{s['total']} pages ({100 * s['fait'] / total:.0f} %) — échecs : {s['echecs']}"
        if s["message"]:
            resume += f"\n{s['message']}"
        self.q("#e-resume", Static).update(Text(resume))
        ecoule = time.time() - s["debut"] if s["debut"] else 0
        eta = []
        if s["fait"] and ecoule > 1:
            eta.append(f"{s['fait'] / (ecoule / 60):.2f} page/min")
        if s["reste_s"] is not None and s["etat"] == "en cours":
            eta.append(f"reste ≈ {fmt_duree(s['reste_s'])}")
        g = s["gpu"]
        if g:
            eta.append(f"GPU {g.get('util') if g.get('util') is not None else '?'} % · VRAM {g.get('mem') or '?'}/{g.get('mem_tot') or '?'} Mo"
                       + (f" · {g['pw']:.0f} W" if g.get("pw") else ""))
        self.q("#e-eta", Static).update(Text(" · ".join(eta)))
        table = self.q("#e-actifs", DataTable)
        table.clear()
        for ident, a in sorted(s["actifs"].items()):
            tokens, ref, prefill = a.get("tokens", 0), a.get("tokens_reflexion", 0), a.get("prefill", 0.0)
            phase = f"préremplissage {prefill * 100:.0f} %" if tokens == 0 and prefill < 1 else ("réflexion" if tokens and ref >= tokens else "génération")
            table.add_row(ident, phase, str(tokens), str(ref), f"{a.get('tps') or 0:.1f}", f"{time.time() - a.get('debut', time.time()):.0f} s")
        self.sub_title = f"extraction {etat} {s['fait']}/{s['total']}" if self.en_cours else ""

    def rafraichir_lent(self) -> None:
        """Pendant une extraction : met à jour l'onglet visible (Accueil ou Pages) sans que l'utilisateur ait à le demander."""
        if not self.en_cours:
            return
        try:
            actif = self.q(TabbedContent).active
        except (NoMatches, IndexError):
            return
        if actif == "accueil":
            self.rafraichir_accueil()
        elif actif == "pages":
            self.charger_pages()

    # ------------------------------------------------------------------ pages
    def preparer_tables(self) -> None:
        t = self.q("#e-actifs", DataTable)
        t.add_columns("Page", "Phase", "Jetons", "Réflexion", "tok/s", "Durée")
        t = self.q("#p-table", DataTable)
        t.add_column("", key="sel", width=3)
        for libelle, cle, largeur in (("Page", "page", 6), ("Imp.", "imp", 5), ("Statut", "statut", 9), ("Entrées", "entrees", 8),
                                      ("Durée", "duree", 8), ("tok/s", "tps", 7), ("Réfl.", "refl", 7), ("Remarque", "remarque", 60)):
            t.add_column(libelle, key=cle, width=largeur)
        t = self.q("#r-table", DataTable)
        for libelle, largeur in (("Prénom", 20), ("Arabe", 18), ("Genre", 9), ("Page", 5), ("Description", 70), ("À vérifier", 30)):
            t.add_column(libelle, width=largeur)

    def statut_page(self, cfg: X.Config, p: int, b: int) -> tuple[str, dict, str]:
        d, perime = X.lire_cache(cfg, p, b, tolerer=True)
        info = self.infos_derniere.get(p)
        if d is None:
            if info and info["statut"] in ("échec", "incomplet"):
                return "échec", {}, info["remarque"]
            return "à faire", {}, ""
        if perime:
            return "périmé", d, "calculé avec une autre configuration"
        if d.get("blanche"):
            return "blanche", d, "page sans encre : aucune requête"
        return ("partiel" if d.get("statut") == "partiel" else "ok"), d, " | ".join(d.get("avertissements") or [])

    def charger_pages(self) -> None:
        cfg = self.cfg_ou_alerte()
        if cfg is None:
            return
        table = self.q("#p-table", DataTable)
        ligne = table.cursor_row
        table.clear()
        for p in cfg.pages:
            for b in range(1, cfg.decoupe + 1):
                statut, d, remarque = self.statut_page(cfg, p, b)
                nom = f"{p}" if cfg.decoupe == 1 else f"{p}.{b}"
                table.add_row("☑" if (p, b) in self.selection else "☐", nom, str(p - X.PAGE_OFFSET),
                              Text(statut, style=COULEURS_STATUT.get(statut, "")), str(len(d.get("entrees") or [])) if d else "",
                              f"{d.get('duree'):.0f} s" if d and d.get("duree") else "", f"{d.get('tps')}" if d and d.get("tps") else "",
                              str(d.get("tokens_reflexion") or "") if d else "", remarque[:200], key=f"{p}:{b}")
        if table.row_count:
            table.move_cursor(row=min(ligne, table.row_count - 1))

    def ligne_courante(self) -> tuple[int, int] | None:
        table = self.q("#p-table", DataTable)
        if not table.row_count:
            return None
        try:
            cle = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            p, b = str(cle).split(":")
            return int(p), int(b)
        except Exception:                                              # noqa: BLE001
            return None

    def basculer_selection(self) -> None:
        cle = self.ligne_courante()
        if cle is None:
            return
        self.selection.symmetric_difference_update({cle})
        table = self.q("#p-table", DataTable)
        table.update_cell(f"{cle[0]}:{cle[1]}", "sel", "☑" if cle in self.selection else "☐")

    @on(Button.Pressed, "#p-actualiser")
    def actualiser_pages(self) -> None:
        self.charger_pages()

    @on(Button.Pressed, "#p-selectionner")
    def cocher_a_refaire(self) -> None:
        cfg = self.cfg_ou_alerte()
        if cfg is None:
            return
        self.selection = {(p, b) for p in cfg.pages for b in range(1, cfg.decoupe + 1)
                          if self.statut_page(cfg, p, b)[0] in ("échec", "partiel", "périmé", "à faire")}
        self.charger_pages()
        self.notify(f"{len(self.selection)} page(s) cochée(s).")

    def refaire_selection(self, reflexion: str) -> None:
        if not self.selection:
            self.notify("Coche d'abord des pages (Espace), ou « Cocher à refaire ».", severity="warning")
            return
        pages = ",".join(str(p) for p in sorted({p for p, _ in self.selection}))
        self.lancer_extraction(pages=pages, reflexion=reflexion, refaire=True)

    @on(Button.Pressed, "#p-refaire-non")
    def refaire_sans(self) -> None:
        self.refaire_selection("non")

    @on(Button.Pressed, "#p-refaire-oui")
    def refaire_avec(self) -> None:
        self.refaire_selection("oui")

    @on(Button.Pressed, "#p-brut")
    def voir_brut(self) -> None:
        cle = self.ligne_courante()
        cfg = self.cfg_ou_alerte()
        if cle is None or cfg is None:
            return
        ident = X.id_chunk(cle[0], cle[1], cfg.decoupe)
        dossier = os.path.join(cfg.sortie, "cache", "brut")
        noms = sorted(n for n in os.listdir(dossier) if n.startswith(ident)) if os.path.isdir(dossier) else []
        parties = []
        for n in noms:
            try:
                with open(os.path.join(dossier, n), encoding="utf-8", errors="replace") as f:
                    parties.append(f"===== {n} =====\n{f.read()[:20000]}")
            except OSError:
                pass
        self.push_screen(VueTexte(f"Sortie brute — {ident}", "\n\n".join(parties) or "Aucune sortie brute enregistrée pour cette page."))

    @on(Button.Pressed, "#p-image")
    def ouvrir_image(self) -> None:
        cle = self.ligne_courante()
        cfg = self.cfg_ou_alerte()
        if cle is None or cfg is None:
            return
        if not (cfg.pdf and os.path.exists(cfg.pdf)):
            self.notify("Renseigne le PDF dans Réglages (F5).", severity="warning")
            return
        try:
            chemin = os.path.join(tempfile.gettempdir(), f"prenoms_page_{cle[0]:03d}.png")
            X.image_native(cfg.pdf, cle[0]).save(chemin)
            ouvrir_avec_le_systeme(chemin)
            self.notify(f"Image ouverte : {chemin}")
        except Exception as e:                                         # noqa: BLE001
            X.log_exception("ouverture de l'image", e)
            self.notify(f"Impossible d'ouvrir l'image : {e}", severity="error")

    # ------------------------------------------------------------------ résultats
    def charger_resultats(self) -> None:
        cfg = self.cfg_ou_alerte()
        if cfg is None:
            return
        try:
            rows, _, _ = X.construire(cfg, X.resultats_du_cache(cfg))
        except Exception as e:                                         # noqa: BLE001
            X.log_exception("lecture des résultats", e)
            return
        self.lignes_resultats = rows
        self.appliquer_filtre()

    def appliquer_filtre(self) -> None:
        q = X.norm(self.q("#r-filtre", Input).value)
        seulement = bool(self.q("#r-flags", Switch).value)
        table = self.q("#r-table", DataTable)
        table.clear()
        affichees = 0
        correspondantes = 0
        for r in self.lignes_resultats:
            if seulement and not X.a_verifier(r):
                continue
            if q and q not in X.norm(f"{r['prénom_fr']} {r['prénom_arab_diacritiques']} {r['description_en_français']} {r['prénoms_équivalents']}"):
                continue
            correspondantes += 1
            if affichees < LIMITE_RESULTATS:
                table.add_row(r["prénom_fr"], r["prénom_arab_diacritiques"], r["genre"], str(r["page_pdf"]),
                              r["description_en_français"][:160], r["à_vérifier"])
                affichees += 1
        rows = self.lignes_resultats
        stats = (f"{len(rows)} lignes au total — {sum(r['genre'] == 'MASCULIN' for r in rows)} masculins, "
                 f"{sum(r['genre'] == 'FEMININ' for r in rows)} féminins — {sum(X.a_verifier(r) for r in rows)} à vérifier — "
                 f"{correspondantes} correspondent au filtre" + (f" (les {LIMITE_RESULTATS} premières affichées)" if correspondantes > affichees else ""))
        self.q("#r-stats", Static).update(Text(stats))

    @on(Input.Changed, "#r-filtre")
    @on(Switch.Changed, "#r-flags")
    def filtre_change(self) -> None:
        self.appliquer_filtre()

    @on(Button.Pressed, "#r-recharger")
    def recharger_resultats(self) -> None:
        self.charger_resultats()

    @on(Button.Pressed, "#r-dossier")
    def ouvrir_dossier(self) -> None:
        cfg = self.cfg_ou_alerte()
        if cfg is None:
            return
        try:
            os.makedirs(cfg.sortie, exist_ok=True)
            ouvrir_avec_le_systeme(cfg.sortie)
        except Exception as e:                                         # noqa: BLE001
            self.notify(f"Impossible d'ouvrir le dossier : {e}", severity="error")

    # ------------------------------------------------------------------ journal
    @on(Button.Pressed, "#j-ouvrir")
    def ouvrir_log(self) -> None:
        if not X.JOURNAL.chemin:
            self.notify("Aucun fichier de log ouvert.", severity="warning")
            return
        try:
            ouvrir_avec_le_systeme(X.JOURNAL.chemin)
        except Exception as e:                                         # noqa: BLE001
            self.notify(f"Impossible d'ouvrir le fichier : {e}  (chemin : {X.JOURNAL.chemin})", severity="error", timeout=12)

    @on(Button.Pressed, "#j-effacer")
    def effacer_affichage(self) -> None:
        self.q("#journal", RichLog).clear()

    # ------------------------------------------------------------------ sortie
    def action_quitter(self) -> None:
        if self.en_cours:
            def apres(reponse: bool | None) -> None:
                if reponse:
                    if self.controle is not None:
                        self.controle.stop.set()
                    self.exit()
            self.push_screen(Confirmer("Une extraction est en cours. L'arrêter et quitter ?\n(les pages terminées sont conservées)"), apres)
        else:
            self.exit()


def lancer(chemin_config: str = "") -> int:
    app = PrenomsApp(chemin_config)
    app.run()
    sys.stdout.flush()
    if any(t is not threading.main_thread() and t.is_alive() and not t.daemon for t in threading.enumerate()):
        os._exit(0)                  # une requête encore en vol (extraction arrêtée) ne doit pas retarder la sortie
    return 0


if __name__ == "__main__":
    sys.exit(lancer())
