"""Rattache a chaque ligne SAISIE de la campagne « 1er aout 2026 » une capture du
tableau de la deliberation CRE 2026 contenant sa valeur, avec la cellule/valeur
**retenue surlignee en jaune** (pour identifier la source quand un tableau contient
plusieurs valeurs).

- crop par ligne (la cellule surlignee differe d'une ligne a l'autre) ;
- localisation par geometrie de cellule (pdfplumber find_tables().rows[..].cells) ;
  cellules multi-valeurs (liaisons aerienne/souterraine) : surlignage de la ligne
  de texte exacte ; reservation HTA (8,14) : table sans bordure -> recherche de mot.

Idempotent : purge les rowimg_* + grid_row_images existants de la campagne.
Usage : python3 scripts/seed_campaign_2026_row_images.py
"""
import glob
import os
import sqlite3

import pdfplumber
from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
CRE_DIR = os.path.join(DATA_DIR, "Délibérations CRE")

NAME = "1er aout 2026"
AGENT = "IA"
AGENT_PROFILE = "IA"
TS = "2026-06-08T10:15:00"
RES = 200
SCALE = RES / 72.0
X0, X1, TOP_PAD, BOT_PAD = 50, 545, 26, 8
YELLOW = (255, 238, 0, 110)
BORDER = (210, 160, 0, 255)

HTB = os.path.join(CRE_DIR, "260521_2026-104_Evolution_TURPE_7_HTB.pdf")
HTA = os.path.join(CRE_DIR, "260521_2026-105_Evolution_TURPE_7_HTA-BT.pdf")

# (pdf, page, table, caption)
T = {
    "cg_htb":   (HTB, 32, 0, "Composante annuelle de gestion HTB - delib. n°2026-104 (tab. 3.1)"),
    "cpt_htb":  (HTB, 32, 1, "Composante annuelle de comptage HTB - delib. n°2026-104 (tab. 3.2)"),
    "inj_htb":  (HTB, 32, 2, "Composante annuelle d'injection HTB - delib. n°2026-104 (tab. 3.3)"),
    "cs_htb3":  (HTB, 32, 3, "Soutirage HTB3 - coefficient c - delib. n°2026-104 (tab. 3.4)"),
    "htb2_cu":  (HTB, 33, 0, "Soutirage HTB2 courte utilisation (b,c) - delib. n°2026-104 (tab. 3.5)"),
    "htb2_mu":  (HTB, 33, 1, "Soutirage HTB2 moyenne utilisation (b,c) - delib. n°2026-104 (tab. 3.6)"),
    "htb2_lu":  (HTB, 33, 4, "Soutirage HTB2 longue utilisation (b,c) - delib. n°2026-104 (tab. 3.7)"),
    "htb1_cu":  (HTB, 34, 0, "Soutirage HTB1 courte utilisation (b,c) - delib. n°2026-104 (tab. 3.8)"),
    "htb1_mu":  (HTB, 34, 3, "Soutirage HTB1 moyenne utilisation (b,c) - delib. n°2026-104 (tab. 3.9)"),
    "htb1_lu":  (HTB, 34, 6, "Soutirage HTB1 longue utilisation (b,c) - delib. n°2026-104 (tab. 3.10)"),
    "cacs_htb": (HTB, 37, 0, "CACS - alimentations complementaires HTB - delib. n°2026-104 (tab. 3.14)"),
    "resa_htb": (HTB, 37, 1, "Alimentations de secours - reservation de puissance HTB (tab. 3.15)"),
    "reg_htb":  (HTB, 37, 5, "Composante de regroupement HTB - delib. n°2026-104 (tab. 3.17)"),
    "tr_htb":   (HTB, 38, 0, "Composante de transformation HTB - delib. n°2026-104 (tab. 3.18)"),
    "dpp_htb":  (HTB, 38, 1, "Depassements ponctuels programmes HTB - delib. n°2026-104 (tab. 3.19)"),
    "der_dist": (HTB, 38, 3, "Energie reactive entre deux GRD - delib. n°2026-104 (tab. 3.21)"),
    "cg_hta":   (HTA, 20, 1, "Composante de gestion HTA-BT y compris Rf/Ccard - delib. n°2026-105"),
    "cpt_hta":  (HTA, 23, 0, "Composante annuelle de comptage HTA-BT - delib. n°2026-105"),
    "inj_hta":  (HTA, 23, 2, "Composante annuelle des injections HTA-BT - delib. n°2026-105"),
    "fixe_c":   (HTA, 24, 0, "HTA 5 plages pointe fixe - courte utilisation - delib. n°2026-105"),
    "fixe_l":   (HTA, 24, 1, "HTA 5 plages pointe fixe - longue utilisation - delib. n°2026-105"),
    "mob_c":    (HTA, 25, 0, "HTA 5 plages pointe mobile - courte utilisation - delib. n°2026-105"),
    "mob_l":    (HTA, 25, 1, "HTA 5 plages pointe mobile - longue utilisation - delib. n°2026-105"),
    "cacs_hta": (HTA, 32, 0, "CACS - alimentations complementaires HTA - delib. n°2026-105"),
    "reg_hta":  (HTA, 32, 2, "Composante de regroupement HTA - delib. n°2026-105"),
    "tr_hta":   (HTA, 33, 0, "Composante de transformation BT->HTA - delib. n°2026-105"),
    "cer_hta":  (HTA, 33, 1, "Energie reactive HTA - flux de soutirage - delib. n°2026-105"),
    "sec_htb":  (HTB, 37, 2, "Secours - tarification du reseau permettant le secours HTB (tab. 3.16)"),
    "sec_hta":  (HTA, 32, 1, "Secours - tarification du reseau permettant le secours HTA (n°2026-105)"),
    "der_nondist": (HTB, 38, 2, "Composante annuelle de l'energie reactive d'electricite - delib. n°2026-104 (tab. 3.20)"),
    # reservation HTA (8,14) : pas une table bordee -> traitee par mot-cle
    "resa_hta": (HTA, 32, None, "Alimentations de secours - reservation de puissance HTA (n°2026-105)"),
}

# slot : sort_order -> (table_key, kind, arg)
#   kind 'cell'  : arg=(row_idx, col_idx)        -> surligne la cellule
#   kind 'line'  : arg=(row_idx, col_idx, token) -> surligne la ligne de texte (token) dans la cellule
#   kind 'coefb' : arg=col (1..5) -> derniere ligne (puissance b)
#   kind 'coefc' : arg=col (1..5) -> derniere ligne (energie c)
#   kind 'word'  : arg=token -> recherche du mot sur la page (table non bordee)
SLOT = {}
def coef(key, c_rows, b_rows, p_rows):
    for i, o in enumerate(c_rows):
        SLOT[o] = (key, "coefc", i + 1)
    for i, o in enumerate(b_rows):
        SLOT[o] = (key, "coefb", i + 1)
    for i, o in enumerate(p_rows):
        SLOT[o] = (key, "coefb", i + 1)

coef("htb2_cu", range(12, 17), range(72, 77), range(128, 133))
coef("htb2_mu", range(17, 22), range(77, 82), range(133, 138))
coef("htb2_lu", range(22, 27), range(82, 87), range(138, 143))
coef("htb1_cu", range(27, 32), range(87, 92), range(143, 148))
coef("htb1_mu", range(32, 37), range(92, 97), range(148, 153))
coef("htb1_lu", range(37, 42), range(97, 102), range(153, 158))
coef("fixe_c", range(42, 47), range(102, 107), range(158, 163))
coef("fixe_l", range(47, 52), range(107, 112), range(163, 168))
coef("mob_c", range(52, 57), range(112, 117), range(168, 173))
coef("mob_l", range(57, 62), range(117, 122), range(173, 178))

SLOT[1] = SLOT[2] = SLOT[3] = ("cg_htb", "cell", (1, 1))
SLOT[5] = ("cg_htb", "cell", (1, 1))
SLOT[4] = SLOT[6] = ("cg_hta", "cell", (5, 1))          # HTA utilisateur incluant Ccard
SLOT[7] = ("cpt_htb", "cell", (2, 8))                   # GRD 3927
SLOT[9] = ("cpt_htb", "cell", (3, 8))                   # Utilisateur 705
SLOT[8] = SLOT[10] = ("cpt_hta", "cell", (6, 3))        # HTA 387,84
SLOT[11] = ("cs_htb3", "cell", (1, 1))                  # 0,42
SLOT[62] = ("inj_htb", "cell", (2, 1))                  # HTB2 37
SLOT[63] = ("inj_htb", "cell", (1, 1))                  # HTB3 37
SLOT[65] = ("inj_htb", "cell", (3, 1))                  # HTB1 0,00
SLOT[64] = ("inj_hta", "cell", (1, 1))                  # HTA 0
SLOT[193] = SLOT[194] = ("reg_htb", "cell", (1, 1))     # HTB3 7,37
SLOT[195] = ("reg_htb", "line", (2, 1, "19,18"))
SLOT[196] = ("reg_htb", "line", (2, 1, "73,73"))
SLOT[197] = SLOT[199] = ("reg_htb", "line", (3, 1, "97,35"))
SLOT[198] = SLOT[200] = ("reg_htb", "line", (3, 1, "171,10"))
SLOT[201] = ("reg_hta", "line", (4, 1, "0,65"))
SLOT[202] = ("reg_hta", "line", (4, 1, "0,95"))
SLOT[203] = ("cacs_htb", "cell", (2, 3))                # HTB3 cellule
SLOT[204] = SLOT[205] = ("cacs_htb", "cell", (2, 4))    # HTB3 liaison 12859,58
SLOT[206] = ("cacs_htb", "cell", (3, 3))                # HTB2 cellule
SLOT[207] = ("cacs_htb", "line", (3, 4, "198,39"))      # HTB2 liaison aer
SLOT[208] = ("cacs_htb", "line", (3, 4, "990,43"))      # HTB2 liaison sout
SLOT[209] = ("cacs_htb", "cell", (4, 3))                # HTB1 cellule
SLOT[210] = ("cacs_htb", "line", (4, 4, "864,75"))      # HTB1 liaison aer
SLOT[211] = ("cacs_htb", "line", (4, 4, "729,49"))      # HTB1 liaison sout
SLOT[212] = ("cacs_hta", "cell", (2, 1))                # HTA cellule 4169,01
SLOT[213] = ("cacs_hta", "line", (2, 2, "137,25"))      # HTA liaison aer
SLOT[214] = ("cacs_hta", "line", (2, 2, "705,87"))      # HTA liaison sout
SLOT[233] = ("resa_htb", "cell", (1, 1))                # HTB2 1,96
SLOT[234] = ("resa_htb", "cell", (2, 1))                # HTB1 3,78
SLOT[235] = ("resa_hta", "word", "8,14")               # HTA 8,14 (table non bordee)
SLOT[236] = ("tr_htb", "cell", (3, 6))                  # HTB1/HTA2->HTB2 4,96
SLOT[237] = ("tr_htb", "cell", (4, 6))                  # HTA1->HTB1 8,76
SLOT[239] = ("tr_htb", "cell", (2, 6))                  # HTB2->HTB3 2,30
SLOT[238] = ("tr_hta", "cell", (2, 2))                  # BT->HTA 10,86
SLOT[240] = ("dpp_htb", "cell", (1, 3))                 # HTB2 0,000143
SLOT[241] = ("dpp_htb", "cell", (2, 3))                 # HTB1 0,000090
SLOT[250] = ("cer_hta", "cell", (1, 2))                 # HTA SH 2,51
SLOT[270] = SLOT[272] = SLOT[274] = ("der_dist", "cell", (1, 1))  # absorbee 3,89
SLOT[271] = SLOT[273] = SLOT[275] = ("der_dist", "cell", (2, 1))  # fournie 2,21
# Secours - tarification du reseau permettant le secours
# HTB (sec_htb p37 t2) : r3 HTB3->HTB2, r4 HTB3->HTB1, r5 HTB2->HTB1 ;
#   col6 prime fixe, col7 part energie, col8 alpha
# HTA (sec_hta p32 t1) : r6 HTB2->HTA, r7 HTB1->HTA ; col2 part puiss, col3 energie, col4 alpha
SLOT[66] = ("sec_htb", "cell", (3, 7))    # HTB3->HTB2 energie 0,98
SLOT[67] = ("sec_hta", "cell", (6, 3))    # ->HTA energie 2,29
SLOT[68] = ("sec_hta", "cell", (7, 3))    # ->HTA energie 2,29
SLOT[69] = ("sec_hta", "cell", (6, 3))    # ->HTA energie 2,29
SLOT[70] = ("sec_htb", "cell", (4, 7))    # HTB3->HTB1 energie 1,66
SLOT[71] = ("sec_htb", "cell", (5, 7))    # HTB2->HTB1 energie 1,66
SLOT[122] = SLOT[178] = ("sec_htb", "cell", (3, 6))   # HTB3->HTB2 prime fixe 9,40
SLOT[123] = SLOT[179] = ("sec_hta", "cell", (6, 2))   # HTB2->HTA part puiss 10,56
SLOT[124] = SLOT[180] = ("sec_hta", "cell", (7, 2))   # HTB1->HTA part puiss 3,68
SLOT[125] = SLOT[181] = ("sec_hta", "cell", (7, 2))   # HTB1->HTA part puiss 3,68
SLOT[126] = SLOT[182] = ("sec_htb", "cell", (5, 6))   # HTB2->HTB1 prime fixe 2,02
SLOT[127] = SLOT[183] = ("sec_htb", "cell", (4, 6))   # HTB3->HTB1 prime fixe 6,91
SLOT[187] = ("sec_htb", "cell", (3, 8))   # HTB3->HTB2 alpha 39,83
SLOT[188] = ("sec_hta", "cell", (6, 4))   # HTB2->HTA alpha 84,76
SLOT[189] = ("sec_hta", "cell", (7, 4))   # HTB1->HTA alpha 30,10
SLOT[190] = ("sec_hta", "cell", (7, 4))   # HTB1->HTA alpha 30,10
SLOT[191] = ("sec_htb", "cell", (5, 8))   # HTB2->HTB1 alpha 8,86
SLOT[192] = ("sec_htb", "cell", (4, 8))   # HTB3->HTB1 alpha 29,49
# Miroirs secours : memes cellules CRE que les lignes principales
SLOT[215] = ("cacs_htb", "cell", (3, 3))
SLOT[216] = ("cacs_htb", "line", (3, 4, "198,39"))
SLOT[217] = ("cacs_htb", "line", (3, 4, "990,43"))
for o in (218, 221, 224): SLOT[o] = ("cacs_hta", "cell", (2, 1))
for o in (219, 222, 225): SLOT[o] = ("cacs_hta", "line", (2, 2, "137,25"))
for o in (220, 223, 226): SLOT[o] = ("cacs_hta", "line", (2, 2, "705,87"))
for o in (227, 230): SLOT[o] = ("cacs_htb", "cell", (4, 3))
for o in (228, 231): SLOT[o] = ("cacs_htb", "line", (4, 4, "864,75"))
for o in (229, 232): SLOT[o] = ("cacs_htb", "line", (4, 4, "729,49"))
for o in (259, 260, 261): SLOT[o] = ("cer_hta", "cell", (1, 2))   # HTA CER 2,51
for o in range(276, 288):
    SLOT[o] = ("der_dist", "cell", (1, 1) if (o - 276) % 2 == 0 else (2, 1))
for o in (243, 247, 252, 256, 263, 267): SLOT[o] = ("der_nondist", "cell", (1, 1))  # absorbee 13,07
for o in (244, 248, 253, 257, 264, 268): SLOT[o] = ("der_nondist", "cell", (2, 1))  # fournie 1,14


# ---------------------------------------------------------------------------
_pdfcache = {}
def page_of(path, pi):
    key = (path, pi)
    if key not in _pdfcache:
        _pdfcache[key] = pdfplumber.open(path).pages[pi]
    return _pdfcache[key]

_imgcache = {}
def base_image(path, pi):
    key = (path, pi)
    if key not in _imgcache:
        _imgcache[key] = page_of(path, pi).to_image(resolution=RES).original.convert("RGBA")
    return _imgcache[key]


SUB = {1: "₁", 2: "₂", 3: "₃", 4: "₄", 5: "₅"}
def find_cell(page, t, token):
    """bbox de la cellule dont le texte contient token (ex. 'b₁')."""
    for row in t.rows:
        for cb in row.cells:
            if not cb:
                continue
            txt = (page.crop(cb).extract_text() or "").replace(" ", "")
            if token in txt:
                return cb
    return None


def line_bbox(page, cell, token):
    """bbox de la ligne de texte contenant token, a l'interieur de la cellule."""
    cx0, ct, cx1, cb = cell
    ws = [w for w in page.extract_words()
          if w["x0"] >= cx0 - 1 and w["x1"] <= cx1 + 1 and w["top"] >= ct - 1 and w["bottom"] <= cb + 1]
    hit = [w for w in ws if token in w["text"]]
    if not hit:
        return None
    ty = hit[0]["top"]
    line = [w for w in ws if abs(w["top"] - ty) < 4]
    return (min(w["x0"] for w in line), min(w["top"] for w in line),
            max(w["x1"] for w in line), max(w["bottom"] for w in line))


def word_bbox(page, token):
    hit = [w for w in page.extract_words() if w["text"].strip() == token]
    if not hit:
        hit = [w for w in page.extract_words() if token in w["text"]]
    return hit[0] if hit else None


def render_row(key, kind, arg, out_path):
    path, pi, ti, _ = T[key]
    page = page_of(path, pi)
    base = base_image(path, pi).copy()
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    if kind == "word":
        w = word_bbox(page, arg)
        if w is None:
            raise RuntimeError("mot %r introuvable p%d" % (arg, pi))
        hl = (w["x0"], w["top"], w["x1"], w["bottom"])
        crop = (X0, max(0, w["top"] - 82), X1, w["bottom"] + 52)
    else:
        t = page.find_tables()[ti]
        if kind in ("coefb", "coefc"):
            token = ("b" if kind == "coefb" else "c") + SUB[arg]
            cell = find_cell(page, t, token)
            if cell is None:
                raise RuntimeError("coefficient %r introuvable %s" % (token, key))
        elif kind == "cell":
            cell = t.rows[arg[0]].cells[arg[1]]
        elif kind == "line":
            cell = t.rows[arg[0]].cells[arg[1]]
        hl = cell if kind != "line" else line_bbox(page, cell, arg[2])
        if hl is None:
            raise RuntimeError("token %r introuvable %s" % (arg, key))
        _, top, _, bot = t.bbox
        crop = (X0, max(0, top - TOP_PAD), X1, min(page.height, bot + BOT_PAD))

    d.rectangle([c * SCALE for c in hl], fill=YELLOW, outline=BORDER, width=2)
    img = Image.alpha_composite(base, overlay).convert("RGB")
    img.crop(tuple(int(c * SCALE) for c in crop)).save(out_path)


def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cid = con.execute("SELECT id FROM campaigns WHERE name=?", (NAME,)).fetchone()["id"]
    cdir = os.path.join(UPLOAD_DIR, str(cid))
    os.makedirs(cdir, exist_ok=True)

    con.execute("DELETE FROM grid_row_images WHERE campaign_id=?", (cid,))
    for f in glob.glob(os.path.join(cdir, "rowimg_*")):
        os.remove(f)

    n = 0
    for o in sorted(SLOT):
        row = con.execute(
            "SELECT id, cle, operande, new_value FROM grid_rows WHERE campaign_id=? AND sort_order=?",
            (cid, o)).fetchone()
        if row is None or row["new_value"] is None:
            continue
        key, kind, arg = SLOT[o]
        stored = "rowimg_%d_20260608_101500_%03d.png" % (row["id"], o)
        render_row(key, kind, arg, os.path.join(cdir, stored))
        label = row["cle"] or row["operande"]
        cap = T[key][3] + " - valeur retenue surlignee"
        con.execute(
            "INSERT INTO grid_row_images "
            "(campaign_id, row_id, original_name, stored_name, caption, size, uploaded_at, uploaded_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cid, row["id"], "capture_CRE_%s.png" % label, stored, cap,
             os.path.getsize(os.path.join(cdir, stored)), TS, AGENT),
        )
        con.execute(
            "INSERT INTO audit_log (campaign_id, user_name, user_profile, action, detail, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (cid, AGENT, AGENT_PROFILE, "ajout_capture_cre",
             "Ligne %s : %s (valeur surlignee)" % (label, T[key][3]), TS),
        )
        n += 1

    con.commit()
    con.close()
    print("%d captures CRE (valeur surlignee) rattachees a la campagne #%d." % (n, cid))


if __name__ == "__main__":
    main()
