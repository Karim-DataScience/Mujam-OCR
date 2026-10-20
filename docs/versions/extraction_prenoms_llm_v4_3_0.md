# v4.3.0 — Vérification de l'arabe par la translittération

> **Date de la version : mardi 20 oct. 2026, 22:48 (heure de Paris)** · archive `extraction_prenoms_llm_v4_3_0.zip` · 7ᵉ version sur 23

## Ce qui change

- `verif_arabe.py` : squelette arabe attendu d'après la translittération de la tête, alignement pondéré, drapeau `arabe_incoherent_translit` (seuil 0,5).
- Retrait du tanwin final (hallucination du modèle) ; indice de translittération ajouté à chaque recadrage arabe.
- 137 tests moteur + 23 TUI (REPRISE).

## Résultats et mesures

- Sur les 1 116 lectures évaluables du run 4.2 : **24 % signalées**, en grande majorité de vraies erreurs (Mukhtâr→`زُبَيْدَة`, ʿImâd ad-Dîn→`فَيْصَل`, س au lieu de ص…).
- Dossier de sortie : 1 050 lignes (run partiel).

## Limites et à retenir

- Le vérificateur ne corrige rien, et ne voit pas ش lu pour س+ح ni un و mal placé (coût 0,4 < seuil 0,5).
- Le premier zip « v4_3_0 » (sans `--relire-arabe`) et le second portaient le même nom : la 4.3.1 est la bonne livraison.

## Chiffres du code (calculés sur l'archive)

| | |
|---|---|
| Fichiers | 13 (+1) |
| Modules Python (hors tests) | 6 (+1) |
| Lignes de code (hors tests) | 4 779 (+216) |
| Tests (`def test_`) | 137 (+5) |
| Taille de l'archive | 121 Ko |

_Sources : `REPRISE.md`, `AUDIT.md`, `README.md` de la version la plus récente, et dossiers de sortie du projet. Date = date de dernière modification des fichiers de l'archive, lue comme UTC (cohérente avec l'heure d'enregistrement des archives sur la machine de l'utilisateur)._
