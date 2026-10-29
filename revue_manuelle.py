"""Revue manuelle des mots arabes problématiques (v4.4).

1. `--exporter-problemes` : pour chaque mot signalé (incohérent avec la translittération, vide, non arabe, éventuellement sans voyelle),
   recadre le MOT ARABE et la TÊTE DE DÉFINITION sur la page du livre, puis écrit dans <sortie>/problemes/ :
     problemes.html   page autonome (images intégrées) : tu vois le mot, tu corriges, tu télécharges corrections.csv
     problemes.csv    la même liste, modifiable dans Excel (colonnes arabe_corrige, translit_corrigee, valide)
     images/          les recadrages (PNG)
2. `--appliquer-corrections corrections.csv` : écrit tes corrections dans le cache (les mots corrigés/validés ne sont plus signalés), puis ré-exporte.
Identifiant d'un mot : p058-e3-b1 = page PDF 58, 3ᵉ entrée de la page, 1ᵉʳ bloc de prénoms.
"""
import base64
import csv
import html
import io
import re
import time

CATEGORIES = {"incoherent": "incohérent avec la translittération", "vide": "aucune lecture", "non_arabe": "caractères non arabes",
              "sans_voyelles": "sans voyelles"}
OUI = {"1", "x", "oui", "o", "true", "vrai", "ok", "y", "yes"}


def identifiant(page: int, i: int, j: int) -> str:
    return f"p{page:03d}-e{i + 1}-b{j + 1}"


def lire_identifiant(ident: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"\s*p(\d+)-e(\d+)-b(\d+)\s*", ident or "")
    if not m:
        raise ValueError(f"identifiant illisible : « {ident} » (attendu : p058-e3-b1)")
    return int(m[1]), int(m[2]) - 1, int(m[3]) - 1


def classer(translit: str, arabe: str, VA, harakat: str, inclure_voyelles: bool) -> tuple[str, float, str] | None:
    """-> (catégorie, coût, indice) si le mot est problématique, sinon None."""
    if not arabe:
        return "vide", 9.0, f"aucune lecture ; translittération « {translit} »" if translit else "aucune lecture"
    if re.search(r"[A-Za-z0-9\u0400-\u04FF]", arabe):
        return "non_arabe", 8.0, "contient des caractères latins ou cyrilliques"
    if VA is not None and translit:
        c, indice = VA.coherence(translit, arabe)
        if c >= VA.SEUIL:
            return "incoherent", c, indice
    if inclure_voyelles and not re.search(f"[{harakat}]", arabe):
        return "sans_voyelles", 0.5, "aucune voyelle"
    return None


def lister_problemes(cache_pages: dict, parse_description, VA, harakat: str, inclure_voyelles: bool = False) -> list[dict]:
    """cache_pages : {page: contenu du cache} -> liste de problèmes (blocs déjà validés exclus), triée par page puis gravité."""
    res = []
    for page in sorted(cache_pages):
        for i, e in enumerate(cache_pages[page].get("entrees", [])):
            translit, _ = parse_description(e.get("description", ""))
            for j, b in enumerate(e.get("prenoms", [])):
                if b.get("valide"):
                    continue
                c = classer(translit, b.get("arabe", ""), VA, harakat, inclure_voyelles)
                if c:
                    res.append({"id": identifiant(page, i, j), "page": page, "i": i, "j": j, "categorie": c[0], "cout": c[1], "indice": c[2],
                                "prenom_fr": " / ".join(b.get("orthographes", [])), "translit": translit, "arabe": b.get("arabe", "")})
    return res


def _png(im, hauteur_min: int = 0) -> bytes:
    if hauteur_min and im.height < hauteur_min:
        f = hauteur_min / im.height
        im = im.resize((max(1, int(im.width * f)), hauteur_min))
    tampon = io.BytesIO()
    im.convert("L").save(tampon, "PNG", optimize=True)
    return tampon.getvalue()


def _b64(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


COLONNES = ["id", "page_pdf", "page_imprimee", "prenom_fr", "translittération", "arabe_actuel", "categorie", "indice",
            "image_arabe", "image_tete", "arabe_corrige", "translit_corrigee", "valide"]


def ecrire_csv(chemin: str, lignes: list[dict]) -> None:
    with open(chemin, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLONNES, extrasaction="ignore")
        w.writeheader()
        w.writerows(lignes)


def page_html(lignes: list[dict], titre: str) -> str:
    cartes = []
    for r in lignes:
        i = html.escape(r["id"])
        img_a = f'<img class="ar" src="{r["_b64_arabe"]}" alt="mot arabe">' if r.get("_b64_arabe") else '<div class="aucune">recadrage indisponible (mise en page différente du cache) : voir la page {} du PDF</div>'.format(r["page_pdf"])
        img_t = f'<img class="tete" src="{r["_b64_tete"]}" alt="tête">' if r.get("_b64_tete") else ""
        cartes.append(f'''<section class="carte" data-id="{i}">
<header><b>{i}</b> · page PDF {r["page_pdf"]} (imprimée {r["page_imprimee"]}) · <span class="cat {html.escape(r["categorie"])}">{html.escape(CATEGORIES.get(r["categorie"], r["categorie"]))}</span></header>
<div class="images">{img_a}{img_t}</div>
<div class="infos"><div>Prénom : <b>{html.escape(r["prenom_fr"])}</b> · translittération lue : <b>{html.escape(r["translittération"])}</b></div>
<div class="indice">{html.escape(r["indice"])}</div>
<div>Arabe actuel : <span class="actuel" dir="rtl">{html.escape(r["arabe_actuel"]) or "—"}</span></div></div>
<div class="champs"><label>Arabe corrigé <input class="ac" dir="rtl" lang="ar" value="" placeholder="laisser vide si inchangé"></label>
<label>Translittération corrigée <input class="tc" value="" placeholder="si la tête est mal lue"></label>
<label class="v"><input class="va" type="checkbox"> l'arabe actuel est correct</label></div></section>''')
    return f'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(titre)}</title><style>
:root{{color-scheme:light dark;--bg:#fafafa;--fg:#222;--carte:#fff;--bord:#ccc;--acc:#b00020}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1b1b1b;--fg:#eee;--carte:#262626;--bord:#444;--acc:#ff6b81}}}}
body{{font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:0 12px 80px}}
h1{{font-size:1.2rem}} .carte{{background:var(--carte);border:1px solid var(--bord);border-radius:8px;margin:12px 0;padding:10px}}
header{{font-size:.9rem;margin-bottom:6px}} .cat{{color:var(--acc);font-weight:600}}
.images{{display:flex;flex-wrap:wrap;gap:12px;align-items:center;background:#fff;border-radius:6px;padding:6px}} img{{max-width:100%;background:#fff}}
img.ar{{max-height:130px}} img.tete{{max-height:60px}} .aucune{{color:#777}}
.infos{{margin:6px 0;font-size:.95rem}} .indice{{font-size:.85rem;opacity:.8}} .actuel{{font-size:1.5rem}}
.champs{{display:flex;flex-wrap:wrap;gap:10px;align-items:end}} label{{display:flex;flex-direction:column;font-size:.85rem}}
input[type=text],input:not([type]){{font-size:1.3rem;padding:4px;min-width:14rem}} .v{{flex-direction:row;gap:6px;align-items:center}}
.barre{{position:fixed;left:0;right:0;bottom:0;background:var(--carte);border-top:1px solid var(--bord);padding:8px 12px;display:flex;gap:10px;align-items:center}}
button{{font-size:1rem;padding:6px 12px}} .fait{{opacity:.55}}
</style></head><body>
<h1>{html.escape(titre)} — {len(lignes)} mot(s)</h1>
<p>Pour chaque mot : écris l'arabe correct (avec les voyelles visibles) <b>ou</b> coche « l'arabe actuel est correct ». Si c'est la <i>translittération</i> (tête) qui est mal lue, corrige-la aussi.
Puis « Télécharger corrections.csv » et donne le fichier à <code>--appliquer-corrections</code>. Ton avancement est gardé dans le navigateur.</p>
{"".join(cartes)}
<div class="barre"><button id="dl">Télécharger corrections.csv</button><span id="cpt"></span></div>
<script>
const CLE="revue-"+document.title;
const cartes=[...document.querySelectorAll(".carte")];
function lire(){{try{{return JSON.parse(localStorage.getItem(CLE)||"{{}}")}}catch(e){{return {{}}}}}}
function sauver(){{const o={{}};cartes.forEach(c=>{{o[c.dataset.id]=[c.querySelector(".ac").value,c.querySelector(".tc").value,c.querySelector(".va").checked]}});try{{localStorage.setItem(CLE,JSON.stringify(o))}}catch(e){{}};compter()}}
function compter(){{let n=0;cartes.forEach(c=>{{const f=c.querySelector(".ac").value.trim()||c.querySelector(".tc").value.trim()||c.querySelector(".va").checked;c.classList.toggle("fait",!!f);if(f)n++}});document.getElementById("cpt").textContent=n+" / "+cartes.length+" traité(s)"}}
const d=lire();cartes.forEach(c=>{{const v=d[c.dataset.id];if(v){{c.querySelector(".ac").value=v[0];c.querySelector(".tc").value=v[1];c.querySelector(".va").checked=v[2]}}c.addEventListener("input",sauver)}});compter();
function q(s){{return '"'+String(s).replace(/"/g,'""')+'"'}}
document.getElementById("dl").onclick=()=>{{const L=["id,arabe_corrige,translit_corrigee,valide"];cartes.forEach(c=>{{const a=c.querySelector(".ac").value.trim(),t=c.querySelector(".tc").value.trim(),v=c.querySelector(".va").checked;if(a||t||v)L.push([q(c.dataset.id),q(a),q(t),v?"1":""].join(","))}});
const b=new Blob(["\\ufeff"+L.join("\\r\\n")+"\\r\\n"],{{type:"text/csv;charset=utf-8"}});const u=URL.createObjectURL(b);const a=document.createElement("a");a.href=u;a.download="corrections.csv";a.click();URL.revokeObjectURL(u)}};
</script></body></html>'''


def appliquer(cache_pages: dict, lignes: list[dict], parse_description, nettoyer) -> tuple[dict, dict]:
    """Applique les corrections (liste de dicts id / arabe_corrige / translit_corrigee / valide) aux contenus de cache.
    -> ({page: contenu modifié}, statistiques)."""
    st = {"lignes": 0, "arabe_corrige": 0, "translit_corrigee": 0, "valides": 0, "ignorees": 0}
    modifies: dict = {}
    for L in lignes:
        ident = (L.get("id") or "").strip()
        if not ident:
            continue
        st["lignes"] += 1
        try:
            page, i, j = lire_identifiant(ident)
        except ValueError:
            st["ignorees"] += 1
            continue
        d = cache_pages.get(page)
        if not d or i >= len(d.get("entrees", [])) or j >= len(d["entrees"][i].get("prenoms", [])):
            st["ignorees"] += 1
            continue
        e = d["entrees"][i]
        b = e["prenoms"][j]
        ar = nettoyer((L.get("arabe_corrige") or "").strip())
        tc = (L.get("translit_corrigee") or L.get("translittération_corrigee") or "").strip()
        ok = (L.get("valide") or "").strip().lower() in OUI
        if not (ar or tc or ok):
            continue
        journal = e.setdefault("corrections_manuelles", [])
        if ar and ar != b.get("arabe"):
            journal.append({"bloc": j + 1, "champ": "arabe", "avant": b.get("arabe", ""), "apres": ar, "date": time.strftime("%Y-%m-%d")})
            b["arabe"] = ar
            st["arabe_corrige"] += 1
        if tc:
            ancien, _ = parse_description(e.get("description", ""))
            desc = e.get("description", "")
            if ancien and desc.startswith(ancien):
                e["description"] = tc + desc[len(ancien):]
            elif ancien and ancien in desc:
                e["description"] = desc.replace(ancien, tc, 1)
            else:
                e["description"] = tc + " : " + desc
            journal.append({"bloc": j + 1, "champ": "translittération", "avant": ancien, "apres": tc, "date": time.strftime("%Y-%m-%d")})
            st["translit_corrigee"] += 1
        b["valide"] = True                      # n'est plus signalé « arabe_* » : validé par un humain
        st["valides"] += 1
        modifies[page] = d
    return modifies, st


def lire_corrections(chemin: str) -> list[dict]:
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        echantillon = f.read(4096)
        f.seek(0)
        delim = ";" if echantillon.count(";") > echantillon.count(",") else ","
        return list(csv.DictReader(f, delimiter=delim))
