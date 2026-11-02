# -*- coding: utf-8 -*-
"""Analyse de la mise en page d'une page du dictionnaire par traitement d'image classique (sans modèle), ~0,6 s par page.

Une page = [titre courant] [bandeau + introduction éventuels] LÉGENDE « MASCULIN / FEMININ » puis des ENTRÉES :
    entrée = un ou deux blocs de prénom  +  UN paragraphe de définition
    bloc   = ligne de nom (français gras + arabe à droite) suivie d'orthographes empilées (français gras seul)
Le genre se lit à la COULEUR (noir = masculin, rouge = féminin). La structure se lit à la géométrie.

Validé sur les 286 pages du dictionnaire (53-338) : 271 pages d'entrées sans anomalie, 15 pages de texte courant
(introductions de chapitre, sans entrée), 0 page en échec ; 1 708 entrées, 2 253 blocs de prénoms, 939 orthographes empilées.
Cas gérés : pages sans légende (suites de rubrique), sous-titres à puce rouge, notes de bas de page (filet), arabe dont les
points/voyelles débordent sur la ligne voisine, nom français collé à son arabe, couples masculin/féminin sur une même ligne,
prénoms « mixtes » (deux colonnes), orthographes empilées sous un seul arabe.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image

ECART_FUSION = 14          # px : sous cet écart vertical, deux bandes font partie du même groupe (lignes d'un paragraphe, points de l'arabe)
FRAGMENT_H = 30            # px : un groupe plus bas et plus étroit que ça est un reste d'arabe (points, voyelles) rattaché au groupe voisin
ECART_AMAS = 50            # px : écart horizontal qui sépare le nom français de l'arabe
HAUT_MIN_BANDE = 12        # px : hauteur minimale d'une bande de texte
ECART_DEFINITIONS = 48     # px : deux groupes « définition » plus proches ne forment qu'un paragraphe
HAUT_MAX_NOM = 110         # px : hauteur maximale d'une ligne de nom (arabe avec points et voyelles compris ; 95 ratait « Ousfoura » p. 320, 97 px)
LARG_MAX_NOM = 1480        # px : largeur maximale d'une ligne de nom (deux blocs côte à côte compris) ; une ligne de paragraphe justifié fait ~1490


@dataclass
class Amas:
    x0: int
    x1: int
    rouge: float                      # part de pixels rouges dans l'amas (0..1)

    @property
    def genre(self) -> str:
        return "FEMININ" if self.rouge > 0.5 else "MASCULIN"


@dataclass
class Groupe:
    y0: int
    y1: int
    x0: int
    x1: int
    rouge: float
    epaisseur: float
    densite: float
    amas: list = field(default_factory=list)
    type: str = "?"

    @property
    def h(self) -> int:
        return self.y1 - self.y0

    @property
    def l(self) -> int:
        return self.x1 - self.x0


@dataclass
class Bloc:
    genre: str
    latin: tuple                      # (x0, x1) du nom français
    arabe: tuple | None               # (x0, x1) de l'arabe
    y: tuple                          # (y0, y1) de la ligne de nom
    orthographes: list = field(default_factory=list)      # [(x0, x1, y0, y1)] lignes empilées


@dataclass
class Entree:
    blocs: list = field(default_factory=list)
    definition: tuple | None = None                      # (x0, y0, x1, y1)
    suite_page_precedente: bool = False


@dataclass
class Mise:
    W: int
    H: int
    groupes: list
    entrees: list
    legende_trouvee: bool
    regle_y: int | None
    problemes: list = field(default_factory=list)
    textes_libres: list = field(default_factory=list)     # [(x0, y0, x1, y1)] paragraphes hors entrée (sous-titres, introductions)
    page_texte: bool = False                              # page de prose sans entrée (introduction de chapitre)
    legende_y: int | None = None
    sans_legende: bool = False                            # page de suite : entrées sans légende « MASCULIN / FEMININ »


def masques(net: Image.Image):
    a = np.asarray(net.convert("RGB")).astype(np.int16)
    rouge = (a[..., 0] - (a[..., 1] + a[..., 2]) / 2) > 70
    sombre = (a.mean(axis=2) < 150) & ~rouge
    return sombre, rouge


def retirer_filets(encre: np.ndarray, W: int):
    """Supprime les filets (traits fins et longs : soulignement de la légende, filet des notes) avant le groupement.
    Rend (encre sans filets, liste des filets (y0, y1, x0, x1))."""
    long_min = int(0.16 * W)
    horiz = cv2.morphologyEx(encre.astype(np.uint8), cv2.MORPH_OPEN, np.ones((1, long_min), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(horiz, connectivity=8)
    filets, retirer = [], np.zeros_like(encre)
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if h <= 9 and w >= long_min:
            filets.append((int(y), int(y + h), int(x), int(x + w)))
            retirer[max(0, y - 2): y + h + 2, x: x + w] = True
    return encre & ~retirer, filets


def _bandes(encre: np.ndarray, ecart: int) -> list[list[int]]:
    H, W = encre.shape
    prof = np.convolve(encre.sum(axis=1), np.ones(7) / 7, mode="same")
    actif = prof > max(3, 0.004 * W)
    out, d = [], None
    for y, a in enumerate(actif):
        if a and d is None:
            d = y
        if not a and d is not None:
            if y - d >= HAUT_MIN_BANDE:
                out.append([d, y])
            d = None
    fus: list[list[int]] = []
    for b in out:
        if fus and b[0] - fus[-1][1] < ecart:
            fus[-1][1] = b[1]
        else:
            fus.append(b)
    return fus


def _amas(encre, rouge, y0, y1, ecart) -> list[Amas]:
    zone = encre[y0:y1]
    xs = np.where(zone.any(axis=0))[0]
    if len(xs) == 0:
        return []
    paquets, debut, prec = [], xs[0], xs[0]
    for x in xs[1:]:
        if x - prec > ecart:
            paquets.append((debut, prec))
            debut = x
        prec = x
    paquets.append((debut, prec))
    out = []
    for x0, x1 in paquets:
        if x1 - x0 < 10:                                  # poussière ou point isolé : pas un mot
            continue
        n = int(zone[:, x0:x1 + 1].sum())
        out.append(Amas(int(x0), int(x1), int(rouge[y0:y1, x0:x1 + 1].sum()) / max(1, n)))
    return out


def _fabriquer(encre, rouge, dt, y0: int, y1: int) -> Groupe | None:
    zone = encre[y0:y1]
    xs = np.where(zone.any(axis=0))[0]
    if len(xs) == 0 or y1 - y0 < 10:
        return None
    n_r, n_s = int(rouge[y0:y1][zone].sum()), int(((~rouge[y0:y1]) & zone).sum())
    g = Groupe(y0=y0, y1=y1, x0=int(xs[0]), x1=int(xs[-1]), rouge=n_r / max(1, n_r + n_s), epaisseur=float(dt[y0:y1][zone].mean()),
               densite=float(zone.sum()) / max(1, (xs[-1] - xs[0] + 1) * (y1 - y0)))
    g.amas = _amas(encre, rouge, y0, y1, ECART_AMAS)
    return g


def _scinder_vertical(encre, rouge, dt, g: Groupe, profil_max: int = 260) -> list[Groupe]:
    """Un groupe haut (> 70 px) et pas trop large est coupé au vide qui sépare deux lignes DE FRANÇAIS (colonne du nom) :
    l'arabe de la ligne du dessus déborde parfois sur la ligne d'orthographes en dessous et les soude."""
    if g.h <= 70 or g.l > LARG_MAX_NOM or not g.amas:
        return [g]
    a = g.amas[0]
    x0, x1 = a.x0, min(a.x1, a.x0 + profil_max)
    prof = encre[g.y0:g.y1, x0:x1 + 1].sum(axis=1)
    vide, debut, coupes = prof <= 0, None, []
    for y, v in enumerate(vide):
        if v and debut is None:
            debut = y
        if not v and debut is not None:
            if y - debut >= 6 and debut >= 22 and (g.h - y) >= 22:
                coupes.append(g.y0 + (debut + y) // 2)
            debut = None
    if not coupes:
        return [g]
    bornes = [g.y0] + coupes + [g.y1]
    morceaux = [_fabriquer(encre, rouge, dt, b0, b1) for b0, b1 in zip(bornes, bornes[1:])]
    morceaux = [m for m in morceaux if m is not None]
    return morceaux or [g]


def _fusion_amas(amas: list[Amas], ecart: int = ECART_AMAS) -> list[Amas]:
    amas = sorted(amas, key=lambda a: a.x0)
    out: list[Amas] = []
    for a in amas:
        if out and a.x0 - out[-1].x1 <= ecart:
            p = out[-1]
            out[-1] = Amas(p.x0, max(p.x1, a.x1), (p.rouge + a.rouge) / 2)
        else:
            out.append(a)
    return out


def _absorber_fragments(groupes: list[Groupe]) -> list[Groupe]:
    """Points et voyelles de l'arabe parfois détachés de leur ligne : un petit groupe est fondu dans le suivant s'il est proche."""
    out: list[Groupe] = []
    i = 0
    while i < len(groupes):
        g = groupes[i]
        if g.h <= FRAGMENT_H and g.l <= 260 and i + 1 < len(groupes) and groupes[i + 1].y0 - g.y1 <= 40:
            n = groupes[i + 1]
            n.y0, n.x0, n.x1 = min(g.y0, n.y0), min(g.x0, n.x0), max(g.x1, n.x1)
            n.amas = _fusion_amas(g.amas + n.amas)
            i += 1
            continue
        out.append(g)
        i += 1
    return out


def analyser(net: Image.Image) -> Mise:
    sombre, rouge = masques(net)
    encre0 = sombre | rouge
    H, W = encre0.shape
    encre, filets = retirer_filets(encre0, W)
    dt = cv2.distanceTransform(encre.astype(np.uint8), cv2.DIST_L2, 3)
    groupes: list[Groupe] = []
    for y0, y1 in _bandes(encre, ECART_FUSION):
        zone = encre[y0:y1]
        xs = np.where(zone.any(axis=0))[0]
        n_r, n_s = int(rouge[y0:y1][zone].sum()), int(sombre[y0:y1][zone].sum())
        g = Groupe(y0=y0, y1=y1, x0=int(xs[0]), x1=int(xs[-1]), rouge=n_r / max(1, n_r + n_s),
                   epaisseur=float(dt[y0:y1][zone].mean()), densite=float(zone.sum()) / max(1, (xs[-1] - xs[0] + 1) * (y1 - y0)))
        g.amas = _amas(encre, rouge, y0, y1, ECART_AMAS)
        groupes.append(g)
    groupes = _absorber_fragments(groupes)
    groupes = [sous for g in groupes for sous in _scinder_vertical(encre, rouge, dt, g)]
    regle_y = min((f[0] for f in filets if f[0] > 0.55 * H), default=None)
    haut = [f[0] for f in filets if f[0] < 0.30 * H]
    mise = Mise(W=W, H=H, groupes=groupes, entrees=[], legende_trouvee=False, regle_y=regle_y, legende_y=min(haut, default=None))
    _classer(mise)
    for g in mise.groupes:
        if g.type == "nom":
            g.amas = _scinder_amas(encre, rouge, g)
    _grammaire(mise)
    return mise


def _scinder_amas(encre: np.ndarray, rouge: np.ndarray, g: Groupe, largeur_min: int = 330, vide_min: int = 18) -> list[Amas]:
    """Dans une ligne de nom, un amas large qui est seul de sa couleur (français + arabe collés) est coupé à son plus grand
    vide interne : à gauche le français, à droite l'arabe."""
    sortie: list[Amas] = []
    for i, am in enumerate(g.amas):
        voisins = [a for j, a in enumerate(g.amas) if j != i and a.genre == am.genre]
        if voisins or am.x1 - am.x0 < largeur_min:
            sortie.append(am)
            continue
        col = encre[g.y0:g.y1, am.x0:am.x1 + 1].any(axis=0)
        meilleur, debut = None, None
        for x, plein in enumerate(col):
            if not plein and debut is None:
                debut = x
            if plein and debut is not None:
                if x - debut >= vide_min and (meilleur is None or x - debut > meilleur[1] - meilleur[0]):
                    meilleur = (debut, x)
                debut = None
        if meilleur is None:
            sortie.append(am)
            continue
        xa, xb = am.x0 + meilleur[0], am.x0 + meilleur[1]
        for (a, b) in ((am.x0, xa - 1), (xb, am.x1)):
            n = int(encre[g.y0:g.y1, a:b + 1].sum())
            sortie.append(Amas(a, b, int(rouge[g.y0:g.y1, a:b + 1].sum()) / max(1, n)))
    return sortie


def blocs_de_ligne(g: Groupe) -> list[Bloc]:
    """Une ligne de nom -> blocs : chaque série d'amas de même couleur = un bloc (le premier amas est le français, les suivants l'arabe)."""
    blocs: list[Bloc] = []
    prec_couleur, prec_bloc = None, None
    for am in g.amas:
        c = am.genre
        if prec_bloc is not None and c == prec_couleur:
            a = prec_bloc.arabe
            prec_bloc.arabe = (am.x0, am.x1) if a is None else (min(a[0], am.x0), max(a[1], am.x1))
        else:
            prec_bloc = Bloc(genre=c, latin=(am.x0, am.x1), arabe=None, y=(g.y0, g.y1))
            blocs.append(prec_bloc)
        prec_couleur = c
    return blocs


def _a_de_l_arabe(g: Groupe) -> bool:
    """Une ligne de nom porte du latin PUIS de l'arabe de la même couleur : au moins deux amas consécutifs de même couleur.
    Une ligne d'orthographes empilées (« Miske | Miske » : un noir, un rouge) n'en a pas."""
    return any(a.genre == b.genre for a, b in zip(g.amas, g.amas[1:]))


def _classer(m: Mise) -> None:
    W, H = m.W, m.H
    etat = "avant"                        # avant la légende : titre courant, bandeau, introduction
    prec_nom: Groupe | None = None
    # pré-passe : toutes les pages n'ont pas la légende (pages de suite d'une rubrique) ni de bandeau
    a_bandeau = any(g.rouge > 0.9 and g.epaisseur > 4 and g.l > 0.6 * W for g in m.groupes)
    a_legende = m.legende_y is not None or any(
        0.25 < g.rouge < 0.75 and g.l > 0.45 * W and len(g.amas) >= 2 and 30 <= g.h <= 85 and g.y1 < 0.3 * H for g in m.groupes)
    for g in m.groupes:
        if m.regle_y is not None and g.y0 >= m.regle_y - 4:
            g.type = "pied"
            continue
        if g.rouge > 0.9 and g.epaisseur > 4 and g.l > 0.6 * W:
            g.type, etat, prec_nom = "bandeau", "avant", None
            continue
        if etat == "avant":
            sous_filet = m.legende_y is not None and m.legende_y - 70 <= g.y1 <= m.legende_y + 25
            if g.l > 0.45 * W and len(g.amas) >= 2 and 30 <= g.h <= 85 and (0.25 < g.rouge < 0.75 or sous_filet):
                g.type, etat, m.legende_trouvee = "legende", "entrees", True
                continue
            elif g.y1 < 0.12 * H:
                g.type = "entete"
                continue
            elif not a_legende and not a_bandeau:
                etat, m.sans_legende = "entrees", True    # page de suite : les entrées commencent après le titre courant
            else:
                g.type = "intro"
                continue
        utiles = [am for am in g.amas if am.x1 - am.x0 >= 40]          # sans les traits d'arabe qui descendent de la ligne du dessus
        if (prec_nom is not None and g.y0 - prec_nom.y1 < 85 and 1 <= len(utiles) <= 2
                and not any(a.genre == b.genre for a, b in zip(utiles, utiles[1:]))
                and all(am.x1 - am.x0 < 560 for am in utiles) and g.epaisseur >= prec_nom.epaisseur - 0.25
                and all(any(abs(am.x0 - d.x0) <= 60 for d in prec_nom.amas) for am in utiles)):
            g.type, g.amas, prec_nom = "orthographe", utiles, g
        elif len(g.amas) >= 2 and g.h <= HAUT_MAX_NOM and g.l <= LARG_MAX_NOM and _a_de_l_arabe(g):
            g.type, prec_nom = "nom", g
        elif g.rouge > 0.85 and 330 <= g.l <= LARG_MAX_NOM and g.h <= HAUT_MAX_NOM:
            g.type, prec_nom = "nom", g                   # français et arabe rouges collés : le plus grand vide les séparera
        elif (prec_nom is None and len(g.amas) == 1 and g.rouge < 0.1 and g.epaisseur >= 1.6 and 560 <= g.l <= 900 and g.h <= HAUT_MAX_NOM):
            g.type, prec_nom = "nom", g                   # français et arabe NOIRS collés en un seul amas (« Abdelmoumine » + arabe allongé) : trait épais = ligne de nom
        else:
            g.type, prec_nom = "definition", None


def _grammaire(m: Mise) -> None:
    """Entrées = blocs de noms puis une définition. Valide la structure et note les problèmes."""
    courant: Entree | None = None
    derniere_ligne: list[Bloc] = []
    for g in m.groupes:
        if g.type == "nom":
            if courant is not None and courant.definition is not None:
                m.entrees.append(courant)
                courant = None
            if courant is None:
                courant = Entree()
            derniere_ligne = blocs_de_ligne(g)
            courant.blocs.extend(derniere_ligne)
        elif g.type == "orthographe":
            if courant is None or not derniere_ligne:
                m.problemes.append(f"orthographe sans nom (y={g.y0})")
                continue
            for am in g.amas:
                cible = min(derniere_ligne, key=lambda b: abs(b.latin[0] - am.x0))
                cible.orthographes.append((am.x0, am.x1, g.y0, g.y1))
        elif g.type == "definition":
            if courant is None:
                prec = m.entrees[-1].definition if m.entrees else None
                if not m.entrees:
                    m.entrees.append(Entree(suite_page_precedente=True, definition=(g.x0, g.y0, g.x1, g.y1)))
                elif prec is not None and g.y0 - prec[3] < ECART_DEFINITIONS:
                    m.entrees[-1].definition = (min(prec[0], g.x0), prec[1], max(prec[2], g.x1), g.y1)
                else:
                    m.textes_libres.append((g.x0, g.y0, g.x1, g.y1))        # sous-titre ou introduction : hors entrée
                continue
            if courant.definition is None:
                courant.definition = (g.x0, g.y0, g.x1, g.y1)
            elif g.y0 - courant.definition[3] < ECART_DEFINITIONS:
                prec = courant.definition
                courant.definition = (min(prec[0], g.x0), prec[1], max(prec[2], g.x1), g.y1)
            else:
                m.textes_libres.append((g.x0, g.y0, g.x1, g.y1))            # sous-titre (« • SES FILLES ») ou introduction
    if courant is not None:
        m.entrees.append(courant)
        if courant.definition is None:
            m.problemes.append("dernière entrée sans définition (suite sur la page suivante ?)")
    for i, e in enumerate(m.entrees):
        for b in e.blocs:
            if b.arabe is None:
                m.problemes.append(f"entrée {i + 1} : bloc « {b.genre} » sans arabe détecté (y={b.y[0]})")
    if not m.legende_trouvee and not m.sans_legende:
        candidats = [g for g in m.groupes if len(g.amas) >= 2 and g.h <= HAUT_MAX_NOM and g.l <= LARG_MAX_NOM and _a_de_l_arabe(g)]
        if candidats:
            m.problemes.append("légende « MASCULIN / FEMININ » introuvable alors que la page semble contenir des noms")
        else:
            m.page_texte = True
