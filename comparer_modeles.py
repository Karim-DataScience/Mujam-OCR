#!/usr/bin/env python3
"""Compare AUTOMATIQUEMENT plusieurs modèles de LM Studio sur ce projet : charge chaque modèle avec le bon contexte (CLI `lms`),
lance les tâches, le décharge, puis écrit un rapport comparatif (comparaison_modeles.md / .json).

Tâches (--taches, séparées par des virgules) :
  autotest   les 4 pages à vérité terrain (55, 56, 100, 150) en lecture hybride (et/ou --methodes vlm) : score + durée
  relecture  relecture ciblée AVEC réflexion des mots arabes incohérents d'un cache existant (--cache) : corrigés / restants
  chaine     (option --chainer) les modèles relisent le MÊME cache l'un après l'autre : ce que la combinaison corrige

Exemples (PowerShell) :
  python comparer_modeles.py lelivredesprenom0000geof.pdf --modeles "prism-ml/bonsai-27b,google/gemma-4-12b-qat"
  python comparer_modeles.py livre.pdf --modeles "prism-ml/bonsai-27b@16384,google/gemma-4-12b-qat@16384,ornith-1.0-9b@8192" --cache sortie_llm --chainer
  python comparer_modeles.py livre.pdf --modeles "grosmodele@8192#0.6" --simuler          # n'exécute rien, affiche les commandes

Syntaxe d'un modèle :  nom[@contexte][#gpu]   (contexte en jetons, défaut --ctx ; gpu = max | off | fraction 0-1, défaut max)
ATTENTION : chaque modèle est chargé après `lms unload --all` : les modèles déjà chargés dans LM Studio sont déchargés.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ICI = os.path.dirname(os.path.abspath(__file__))
MOTEUR = os.path.join(ICI, "extraire_prenoms_llm.py")
VERSION = "1.0.0"


# ----------------------------------------------------------------------------- spécification d'un modèle
def nom_fichier(nom: str) -> str:
    return re.sub(r"[^\w.-]+", "_", nom)


def lire_modele(spec: str, ctx_defaut: int, parallel: int = 1) -> dict:
    m = re.match(r"^\s*(?P<nom>[^@#\s]+)(?:@(?P<ctx>\d+))?(?:#(?P<gpu>max|off|[01](?:\.\d+)?))?\s*$", spec)
    if not m:
        raise ValueError(f"modèle illisible : « {spec} » (attendu : nom[@contexte][#gpu])")
    return {"nom": m["nom"], "ctx": int(m["ctx"] or ctx_defaut), "gpu": m["gpu"] or "max", "parallel": parallel}


def commande_chargement(lms: str, mdl: dict, cle: str) -> list[str]:
    return [lms, "load", cle, "--context-length", str(mdl["ctx"]), "--parallel", str(mdl["parallel"]),
            "--gpu", mdl["gpu"], "--identifier", mdl["nom"], "-y"]


# ----------------------------------------------------------------------------- exécution (remplaçable dans les tests)
class Executeur:
    def __init__(self, simuler: bool = False):
        self.simuler = simuler
        self.historique: list[list[str]] = []

    def lancer(self, cmd: list[str], sortie_fichier: str | None = None, timeout: int | None = None) -> tuple[int, str]:
        self.historique.append(cmd)
        print("  $ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
        if self.simuler:
            return 0, ""
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
        except FileNotFoundError as e:
            return 127, str(e)
        except subprocess.TimeoutExpired:
            return 124, "délai dépassé"
        sortie = (p.stdout or "") + (p.stderr or "")
        if sortie_fichier:
            os.makedirs(os.path.dirname(sortie_fichier) or ".", exist_ok=True)
            with open(sortie_fichier, "w", encoding="utf-8") as f:
                f.write(sortie)
        return p.returncode, sortie

    def attendre(self, s: float) -> None:
        if not self.simuler:
            time.sleep(s)


def trouver_lms() -> str:
    chemin = shutil.which("lms")
    if chemin:
        return chemin
    for c in (os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", "lms.exe"),
              os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", "lms"),
              os.path.join(os.path.expanduser("~"), ".cache", "lm-studio", "bin", "lms")):
        if os.path.isfile(c):
            return c
    return ""


def serveur_pret(url: str) -> bool:
    base = url.rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    try:
        with urllib.request.urlopen(base + "/v1/models", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def cle_modele(ex: Executeur, lms: str, nom: str) -> str:
    """Identifiant exact pour `lms load` : le nom donné, ou la clé du modèle téléchargé qui le contient (lms ls --json)."""
    code, out = ex.lancer([lms, "ls", "--json"])
    if code != 0 or ex.simuler:
        return nom
    try:
        data = json.loads(out[out.index("["):out.rindex("]") + 1])
    except ValueError:
        return nom
    cles = [str(d.get("modelKey") or d.get("path") or "") for d in data if isinstance(d, dict) and d.get("type", "llm") in ("llm", "vlm")]
    exact = [c for c in cles if c.lower() == nom.lower()]
    flou = [c for c in cles if nom.lower().split("/")[-1] in c.lower()]
    return (exact or flou or [nom])[0]


def charger(ex: Executeur, lms: str, mdl: dict, url: str, delai: int = 240) -> dict:
    """Décharge tout, charge `mdl` avec son contexte, attend qu'il soit servi. -> {'ok', 'duree_s', 'cle', 'message'}"""
    t = time.time()
    ex.lancer([lms, "unload", "--all"])
    cle = cle_modele(ex, lms, mdl["nom"])
    code, out = ex.lancer(commande_chargement(lms, mdl, cle), timeout=delai)
    if code != 0:
        return {"ok": False, "duree_s": round(time.time() - t, 1), "cle": cle,
                "message": f"`lms load` a échoué (code {code}) : {out.strip()[-300:]}"}
    fin = time.time() + 60
    while not ex.simuler and time.time() < fin:
        code, out = ex.lancer([lms, "ps"])
        if mdl["nom"].lower().split("/")[-1] in out.lower() or cle.lower().split("/")[-1] in out.lower():
            break
        ex.attendre(2)
    return {"ok": True, "duree_s": round(time.time() - t, 1), "cle": cle, "message": ""}


# ----------------------------------------------------------------------------- tâches
def _lire_json(chemin: str) -> dict:
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def taux_incoherent(dossier_sortie: str) -> dict:
    """Part des lectures arabes incohérentes avec la translittération, calculée sur le cache d'un dossier de sortie."""
    sys.path.insert(0, ICI)
    import verif_arabe as VA
    from extraire_prenoms_llm import parse_description
    n = ko = 0
    cache = os.path.join(dossier_sortie, "cache")
    for nom in sorted(os.listdir(cache)) if os.path.isdir(cache) else []:
        d = _lire_json(os.path.join(cache, nom))
        for e in d.get("entrees", []):
            tr, _ = parse_description(e.get("description", ""))
            for b in e.get("prenoms", []):
                if tr and b.get("arabe"):
                    c, _i = VA.coherence(tr, b["arabe"])
                    if c >= 0:
                        n += 1
                        ko += c >= VA.SEUIL
    return {"evaluees": n, "incoherentes": ko, "pct": round(100 * ko / n, 1) if n else None}


def commande_moteur(a, mdl_nom: str, sortie: str, *options: str) -> list[str]:
    cmd = [sys.executable, MOTEUR, a.pdf, "--cli", "--url", a.url, "--modele", mdl_nom, "--sortie", sortie, *options]
    return cmd


def tache_autotest(ex: Executeur, a, mdl: dict, dossier: str, methode: str) -> dict:
    sortie = os.path.join(dossier, f"autotest_{methode}")
    t = time.time()
    code, _ = ex.lancer(commande_moteur(a, mdl["nom"], sortie, "--autotest", "--methode", methode, "--essais", str(a.essais)),
                        sortie_fichier=os.path.join(dossier, f"autotest_{methode}.txt"))
    res = _lire_json(os.path.join(sortie, "autotest", "score.json")) if not ex.simuler else {}
    return {"code": code, "duree_totale_s": round(time.time() - t, 1), **res}


def tache_relecture(ex: Executeur, a, mdl: dict, cache_source: str, dossier_sortie: str) -> dict:
    if not ex.simuler:
        shutil.copytree(cache_source, dossier_sortie, dirs_exist_ok=True, ignore=shutil.ignore_patterns("logs", "images", "*.tmp"))
    avant = taux_incoherent(dossier_sortie) if not ex.simuler else {}
    opts = ["--relire-arabe", "--essais", str(a.essais), "--budget-reflexion", str(a.budget_reflexion)]
    if a.pages:
        opts += ["--pages", a.pages]
    t = time.time()
    code, _ = ex.lancer(commande_moteur(a, mdl["nom"], dossier_sortie, *opts), sortie_fichier=dossier_sortie.rstrip("/\\") + "_relecture.txt")
    st = _lire_json(os.path.join(dossier_sortie, "relecture_stats.json")) if not ex.simuler else {}
    apres = taux_incoherent(dossier_sortie) if not ex.simuler else {}
    return {"code": code, "duree_totale_s": round(time.time() - t, 1), "stats": st, "avant": avant, "apres": apres}


# ----------------------------------------------------------------------------- orchestration
def comparer(a, ex: Executeur) -> dict:
    lms = a.lms or trouver_lms() or "lms"
    if not ex.simuler and not (a.lms or trouver_lms()):
        raise SystemExit("La commande `lms` (LM Studio) est introuvable. Dans LM Studio : Settings > Developer > « Install the lms CLI », "
                         "puis relance PowerShell. (Ou utilise --simuler pour voir les commandes.)")
    if not ex.simuler and not serveur_pret(a.url):
        print("Serveur LM Studio arrêté : `lms server start`")
        ex.lancer([lms, "server", "start"])
        for _ in range(15):
            if serveur_pret(a.url):
                break
            ex.attendre(2)
        if not serveur_pret(a.url):
            raise SystemExit(f"Le serveur LM Studio ne répond pas sur {a.url}.")
    modeles = [lire_modele(s, a.ctx) for s in a.modeles.split(",") if s.strip()]
    taches = {t.strip() for t in a.taches.split(",") if t.strip()}
    if "relecture" in taches or a.chainer:
        if not a.cache or not os.path.isdir(os.path.join(a.cache, "cache")):
            raise SystemExit("--cache : dossier de sortie existant (celui qui contient « cache/ »), requis pour la relecture / la chaîne.")
    rapport: dict = {"version": VERSION, "date": time.strftime("%Y-%m-%d %H:%M"), "pdf": a.pdf, "modeles": {}, "chaine": []}
    os.makedirs(a.sortie, exist_ok=True)
    try:
        for mdl in modeles:
            print(f"\n=== {mdl['nom']} (contexte {mdl['ctx']}, GPU {mdl['gpu']}) ===")
            d = os.path.join(a.sortie, nom_fichier(mdl["nom"]))
            os.makedirs(d, exist_ok=True)
            ch = charger(ex, lms, mdl, a.url)
            r: dict = {"chargement": ch, "spec": mdl}
            if not ch["ok"]:
                print("  ÉCHEC du chargement :", ch["message"])
                rapport["modeles"][mdl["nom"]] = r
                continue
            if "autotest" in taches:
                for meth in [m.strip() for m in a.methodes.split(",") if m.strip()]:
                    r[f"autotest_{meth}"] = tache_autotest(ex, a, mdl, d, meth)
            if "relecture" in taches:
                r["relecture"] = tache_relecture(ex, a, mdl, a.cache, os.path.join(d, "relecture"))
            rapport["modeles"][mdl["nom"]] = r
            if not a.garder_charge:
                ex.lancer([lms, "unload", "--all"])
        if a.chainer:
            print("\n=== CHAÎNE : les modèles relisent le même cache, l'un après l'autre ===")
            dc = os.path.join(a.sortie, "chaine")
            if not ex.simuler:
                shutil.copytree(a.cache, dc, dirs_exist_ok=True, ignore=shutil.ignore_patterns("logs", "images", "*.tmp"))
            rapport["chaine_avant"] = taux_incoherent(dc) if not ex.simuler else {}
            for mdl in modeles:
                ch = charger(ex, lms, mdl, a.url)
                if not ch["ok"]:
                    rapport["chaine"].append({"modele": mdl["nom"], "chargement": ch})
                    continue
                opts = ["--relire-arabe", "--essais", str(a.essais), "--budget-reflexion", str(a.budget_reflexion)]
                if a.pages:
                    opts += ["--pages", a.pages]
                code, _ = ex.lancer(commande_moteur(a, mdl["nom"], dc, *opts), sortie_fichier=os.path.join(a.sortie, "chaine_" + nom_fichier(mdl["nom"]) + ".txt"))
                rapport["chaine"].append({"modele": mdl["nom"], "code": code, "stats": _lire_json(os.path.join(dc, "relecture_stats.json")) if not ex.simuler else {},
                                          "apres": taux_incoherent(dc) if not ex.simuler else {}})
    finally:
        if not a.garder_charge and not ex.simuler:
            ex.lancer([lms, "unload", "--all"])
    ecrire_rapport(rapport, a.sortie)
    return rapport


# ----------------------------------------------------------------------------- rapport
def _cellule(v) -> str:
    return "—" if v in (None, "", {}) else str(v)


def rapport_markdown(r: dict) -> str:
    L = [f"# Comparaison de modèles — {r['date']}", "", f"PDF : `{os.path.basename(r['pdf'])}`", ""]
    cles = ["vedettes", "parasites", "genre", "arabe_lu", "arabe_exact", "arabe_sim_moy", "arabe_voyelles"]
    for meth in sorted({k for m in r["modeles"].values() for k in m if k.startswith("autotest_")}):
        L += [f"## Autotest ({meth.split('_', 1)[1]}) — 4 pages lues à l'œil", "",
              "| Modèle | Chargement | " + " | ".join(cles) + " | s/page | tok/s |", "|---|---|" + "---|" * (len(cles) + 2)]
        for nom, m in r["modeles"].items():
            t = m.get(meth)
            if not m["chargement"]["ok"]:
                L.append(f"| {nom} | ÉCHEC | " + " | ".join("—" for _ in cles) + " | — | — |")
                continue
            sc = (t or {}).get("score", {})
            L.append(f"| {nom} | {m['chargement']['duree_s']} s | " + " | ".join(_cellule(sc.get(k)) for k in cles)
                     + f" | {_cellule((t or {}).get('duree_moy_s'))} | {_cellule((t or {}).get('tok_s_moy'))} |")
        L.append("")
    if any("relecture" in m for m in r["modeles"].values()):
        L += ["## Relecture ciblée de l'arabe (même cache de départ pour tous)", "",
              "| Modèle | Suspects | Corrigés | Restent | Incohérents avant → après | Jetons de réflexion | Durée |", "|---|---|---|---|---|---|---|"]
        for nom, m in r["modeles"].items():
            t = m.get("relecture")
            if not t:
                continue
            st, av, ap = t.get("stats", {}), t.get("avant", {}), t.get("apres", {})
            L.append(f"| {nom} | {_cellule(st.get('cibles'))} | {_cellule(st.get('corrigees'))} | {_cellule(st.get('restantes'))} | "
                     f"{_cellule(av.get('pct'))} % → {_cellule(ap.get('pct'))} % | {_cellule(st.get('jetons_reflexion'))} | {_cellule(st.get('duree_s'))} s |")
        L.append("")
    if r.get("chaine"):
        L += ["## Chaîne (modèles enchaînés sur le même cache)", "", f"Départ : {_cellule((r.get('chaine_avant') or {}).get('pct'))} % incohérents.", "",
              "| Étape | Modèle | Corrigés | Restent | Incohérents après |", "|---|---|---|---|---|"]
        for i, e in enumerate(r["chaine"], 1):
            st = e.get("stats", {})
            L.append(f"| {i} | {e['modele']} | {_cellule(st.get('corrigees'))} | {_cellule(st.get('restantes'))} | {_cellule((e.get('apres') or {}).get('pct'))} % |")
        L.append("")
    L += ["_Lecture_ : sur 4 pages l'autotest est un indicateur, pas une preuve ; la relecture (mêmes mots pour tous) est le test discriminant. "
          "Le vérificateur ne voit pas toutes les erreurs : « incohérents après » est une borne basse du taux d'erreur réel."]
    return "\n".join(L) + "\n"


def ecrire_rapport(r: dict, dossier: str) -> None:
    with open(os.path.join(dossier, "comparaison_modeles.json"), "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    with open(os.path.join(dossier, "comparaison_modeles.md"), "w", encoding="utf-8") as f:
        f.write(rapport_markdown(r))
    print(f"\nRapport : {os.path.join(dossier, 'comparaison_modeles.md')}")


def creer_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("pdf", nargs="?", default="", help="chemin du PDF (détecté automatiquement s'il est dans le dossier)")
    ap.add_argument("--modeles", default="prism-ml/bonsai-27b,google/gemma-4-12b-qat", help="liste séparée par des virgules : nom[@contexte][#gpu]")
    ap.add_argument("--ctx", type=int, default=16384, help="contexte par défaut (16384 suffit pour la lecture hybride, la page entière et la relecture avec réflexion)")
    ap.add_argument("--taches", default="autotest,relecture", help="autotest, relecture (virgules)")
    ap.add_argument("--methodes", default="hybride", help="méthodes de l'autotest : hybride, vlm ou hybride,vlm")
    ap.add_argument("--cache", default="", help="dossier de sortie existant (contenant cache/) servant de base à la relecture")
    ap.add_argument("--chainer", action="store_true", help="en plus : les modèles relisent le même cache à la suite (effet de la combinaison)")
    ap.add_argument("--pages", default="", help="restreindre la relecture à ces pages (ex. 55-120)")
    ap.add_argument("--essais", type=int, default=3)
    ap.add_argument("--budget-reflexion", type=int, default=3000, dest="budget_reflexion")
    ap.add_argument("--url", default="http://localhost:1234")
    ap.add_argument("--sortie", default="comparaison", help="dossier des résultats et du rapport")
    ap.add_argument("--garder-charge", action="store_true", help="ne pas décharger le dernier modèle à la fin")
    ap.add_argument("--lms", default="", help="chemin de la commande lms (défaut : automatique)")
    ap.add_argument("--simuler", action="store_true", help="affiche les commandes sans rien exécuter")
    ap.add_argument("--version", action="version", version=f"comparer_modeles {VERSION}")
    return ap


def main(argv=None, ex: Executeur | None = None) -> int:
    a = creer_parser().parse_args(argv)
    sys.path.insert(0, ICI)
    from extraire_prenoms_llm import trouver_pdf
    a.pdf = trouver_pdf(a.pdf)
    if not os.path.isfile(a.pdf) and not a.simuler:
        print(f"PDF introuvable : « {a.pdf} »", file=sys.stderr)
        return 2
    try:
        r = comparer(a, ex or Executeur(a.simuler))
    except KeyboardInterrupt:
        print("interruption : décharge des modèles…")
        return 130
    return 0 if r else 1


if __name__ == "__main__":
    sys.exit(main())
