"""Profils de documents (v5.0) : tout ce qui est propre à UN livre (pages du dictionnaire, décalage de pagination, mise en page décrite au modèle,
règles de genre et d'arabe, méthode de lecture) vit dans profils/<nom>.json ; le moteur est commun à tous les documents.

Un profil contient :
  nom, description, motifs_fichier (regex sur le nom du PDF, pour la détection automatique), langue,
  pages {debut, fin (0 = dernière page du PDF)}, decalage_page_imprimee (page PDF - page imprimée),
  methode ("hybride"/"auto" = spécifique au Livre des prénoms arabes ; "vlm" = le modèle lit la page entière), themes (bool),
  titre / mise_en_page / regle_genre / regle_arabe / exemple {orthographes, arabe, genre, description}  -> le prompt est construit à partir d'eux,
  ou prompt (texte complet, prioritaire),
  notes (limites connues, conseils).
"""
import glob
import json
import os
import re

ICI = os.path.dirname(os.path.abspath(__file__))
DOSSIER = os.path.join(ICI, "profils")
PROFIL_GENERIQUE = "generique"

BASE = """Tu transcris une page d'un dictionnaire de prénoms : « {titre} ». Réponds UNIQUEMENT par un objet JSON, sans texte avant ni après et sans analyse préalable.

MISE EN PAGE
{mise_en_page}

RÈGLES
- Une entrée = un prénom (avec ses variantes d'orthographe) et sa signification / définition. Recopie exactement les graphies (accents, apostrophes, tirets, majuscules) et la signification COMPLÈTE, sans résumer, traduire ni corriger. Recolle les mots coupés en fin de ligne.
- "orthographes" : le nom tel qu'imprimé, suivi de ses variantes d'orthographe éventuelles (une graphie par élément).
- "genre" : {regle_genre}
- "arabe" : {regle_arabe}
- "description" : la signification telle qu'imprimée (avec l'origine « (Persian) », « Ant., msc. »… si elle fait partie de l'entrée).
- IGNORE : titres courants, numéros de page, en-têtes et pieds de page, titres de section, notes de bas de page, illustrations, publicités, texte d'introduction.
- Haut de page : si elle commence par la fin d'une signification (aucun nom avant) : entrée avec "prenoms": [] et "suite_page_precedente": true.
- Bas de page : signification coupée, ou nom sans sa signification : "description_incomplete": true.
- Ces deux champs ne s'écrivent QUE lorsqu'ils valent true (sinon, ne les écris pas).
- Ne fusionne pas deux entrées, n'en invente aucune, n'en oublie aucune.

FORMAT (exemple, à ne pas recopier) :
{exemple_json}
Page sans entrée : {{"entrees":[]}}"""

REGLE_GENRE_DEFAUT = "\"MASCULIN\" ou \"FEMININ\" selon l'indication de la page ou de l'entrée (titre de section, symbole, mention) ; \"\" si rien ne l'indique. Un prénom mixte : un bloc par genre."
REGLE_ARABE_DEFAUT = "l'écriture arabe du nom, avec TOUTES les voyelles et signes imprimés (jamais ajoutés), sans les barres d'allongement (tatwil) ; \"\" si le document n'en donne pas ou si elle est illisible."


def liste_profils() -> list[dict]:
    res = []
    for f in sorted(glob.glob(os.path.join(DOSSIER, "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                p = json.load(fh)
            p.setdefault("nom", os.path.splitext(os.path.basename(f))[0])
            res.append(p)
        except (OSError, ValueError):
            continue
    return res


def charger(nom_ou_chemin: str) -> dict:
    """Profil par nom (profils/<nom>.json) ou par chemin de fichier .json. ValueError si introuvable ou illisible."""
    candidats = [nom_ou_chemin, os.path.join(DOSSIER, nom_ou_chemin), os.path.join(DOSSIER, nom_ou_chemin + ".json")]
    for c in candidats:
        if c.lower().endswith(".json") and os.path.isfile(c):
            try:
                with open(c, encoding="utf-8") as f:
                    p = json.load(f)
            except ValueError as e:
                raise ValueError(f"profil illisible ({c}) : {e}")
            p.setdefault("nom", os.path.splitext(os.path.basename(c))[0])
            return p
    dispo = ", ".join(p["nom"] for p in liste_profils()) or "aucun"
    raise ValueError(f"profil « {nom_ou_chemin} » introuvable (profils disponibles : {dispo})")


def detecter(chemin_pdf: str) -> dict | None:
    """Profil dont un motif correspond au nom du PDF, sinon None."""
    nom = os.path.basename(chemin_pdf or "").lower()
    for p in liste_profils():
        for motif in p.get("motifs_fichier", []):
            if re.search(motif, nom, re.I):
                return p
    return None


def exemple_json(profil: dict) -> str:
    ex = profil.get("exemple") or {"orthographes": ["Karim"], "arabe": "كَرِيم", "genre": "MASCULIN", "description": "généreux."}
    bloc = {"genre": ex.get("genre", ""), "orthographes": ex.get("orthographes", []), "arabe": ex.get("arabe", "")}
    return json.dumps({"entrees": [{"prenoms": [bloc], "description": ex.get("description", "")}]}, ensure_ascii=False, separators=(",", ":"))


def prompt_de(profil: dict) -> str | None:
    """Prompt système du profil : `prompt` s'il est donné, sinon construit sur le modèle commun ; None pour le profil historique (prompt du moteur)."""
    if profil.get("prompt"):
        return profil["prompt"]
    if not profil.get("mise_en_page"):
        return None
    return BASE.format(titre=profil.get("titre", profil.get("description", profil["nom"])), mise_en_page=profil["mise_en_page"],
                       regle_genre=profil.get("regle_genre", REGLE_GENRE_DEFAUT), regle_arabe=profil.get("regle_arabe", REGLE_ARABE_DEFAUT),
                       exemple_json=exemple_json(profil))


def explicite(args: list[str], *options: str) -> bool:
    """L'utilisateur a-t-il donné l'une de ces options en ligne de commande ?"""
    return any(a == o or a.startswith(o + "=") for a in args for o in options)
