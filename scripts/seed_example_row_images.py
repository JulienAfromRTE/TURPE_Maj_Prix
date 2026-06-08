"""Rattache de VRAIES captures des tableaux CRE aux 20 premieres lignes de la
campagne exemple « Fevrier 2025 ».

Chaque capture est un crop du tableau reel de la delibration PDF qui contient la
valeur de la cle (cf. docs/mapping_CRE_PDF.md), rendu via pdfplumber. Les images
sont stockees comme le ferait la route d'upload (rowimg_<rid>_<stamp>.png) et
inserees dans grid_row_images.

Idempotent : purge les captures rowimg_* existantes de la campagne avant de recreer.

Usage : python3 scripts/seed_example_row_images.py
"""
import glob
import os
import sqlite3

import pdfplumber

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
CRE_DIR = os.path.join(DATA_DIR, "Délibérations CRE")

CID = 2
AGENT = "Agent TMA"
TS = "2025-01-20T15:10:00"

HTB = os.path.join(CRE_DIR, "250115_2025-09_Evolution_TURPE_6_HTB.pdf")
HTABT = os.path.join(CRE_DIR, "250115__2025-08_Evolution_TURPE_6_HTA-BT.pdf")

# Cadrage horizontal commun (largeur utile de la page A4 ~595pt) + marges.
X0, X1, TOP_PAD, BOT_PAD = 52, 543, 50, 10

# sort_order de la ligne -> (pdf, page index, table index, libelle de la capture)
MAPPING = {
    1:  (HTB,   4, 0, "Composante annuelle de gestion HTB - delib. n°2025-09 (Tableau 1)"),
    2:  (HTB,   4, 0, "Composante annuelle de gestion HTB - delib. n°2025-09 (Tableau 1)"),
    3:  (HTB,   4, 0, "Composante annuelle de gestion HTB - delib. n°2025-09 (Tableau 1)"),
    4:  (HTABT, 5, 1, "Gestion y compris Rf et Ccard - HTA - delib. n°2025-08"),
    5:  (HTB,   4, 0, "Composante annuelle de gestion HTB - delib. n°2025-09 (Tableau 1)"),
    6:  (HTABT, 5, 1, "Gestion y compris Rf et Ccard - HTA - delib. n°2025-08"),
    7:  (HTB,   4, 1, "Composante annuelle de comptage HTB - delib. n°2025-09 (Tableau 2)"),
    8:  (HTABT, 8, 0, "Composante annuelle de comptage HTA - delib. n°2025-08"),
    9:  (HTB,   4, 1, "Composante annuelle de comptage HTB - delib. n°2025-09 (Tableau 2)"),
    10: (HTABT, 8, 0, "Composante annuelle de comptage HTA - delib. n°2025-08"),
    11: (HTB,   4, 3, "Soutirage HTB3 - coefficient c (c€/kWh) - delib. n°2025-09"),
    12: (HTB,   5, 0, "Soutirage HTB2 - courte utilisation - delib. n°2025-09"),
    13: (HTB,   5, 0, "Soutirage HTB2 - courte utilisation - delib. n°2025-09"),
    14: (HTB,   5, 0, "Soutirage HTB2 - courte utilisation - delib. n°2025-09"),
    15: (HTB,   5, 0, "Soutirage HTB2 - courte utilisation - delib. n°2025-09"),
    16: (HTB,   5, 0, "Soutirage HTB2 - courte utilisation - delib. n°2025-09"),
    17: (HTB,   5, 3, "Soutirage HTB2 - moyenne utilisation - delib. n°2025-09"),
    18: (HTB,   5, 3, "Soutirage HTB2 - moyenne utilisation - delib. n°2025-09"),
    19: (HTB,   5, 3, "Soutirage HTB2 - moyenne utilisation - delib. n°2025-09"),
    20: (HTB,   5, 3, "Soutirage HTB2 - moyenne utilisation - delib. n°2025-09"),
}


def crop_table(pdf, page_idx, table_idx, out_path):
    page = pdf.pages[page_idx]
    _, top, _, bottom = page.find_tables()[table_idx].bbox
    bbox = (X0, max(0, top - TOP_PAD), X1, min(page.height, bottom + BOT_PAD))
    page.crop(bbox).to_image(resolution=200).save(out_path)


def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cdir = os.path.join(UPLOAD_DIR, str(CID))
    os.makedirs(cdir, exist_ok=True)

    # Idempotence : purge des captures rowimg_* precedentes.
    con.execute("DELETE FROM grid_row_images WHERE campaign_id=?", (CID,))
    for f in glob.glob(os.path.join(cdir, "rowimg_*")):
        os.remove(f)

    pdfs = {HTB: pdfplumber.open(HTB), HTABT: pdfplumber.open(HTABT)}
    n = 0
    for sort_order, (pdf_path, page_idx, table_idx, caption) in sorted(MAPPING.items()):
        row = con.execute(
            "SELECT id, cle, operande FROM grid_rows WHERE campaign_id=? AND sort_order=?",
            (CID, sort_order),
        ).fetchone()
        rid = row["id"]
        stored = f"rowimg_{rid}_20250120_151000_{sort_order:02d}.png"
        crop_table(pdfs[pdf_path], page_idx, table_idx, os.path.join(cdir, stored))
        size = os.path.getsize(os.path.join(cdir, stored))
        original = f"capture_CRE_{row['cle'] or row['operande']}.png"
        con.execute(
            "INSERT INTO grid_row_images "
            "(campaign_id, row_id, original_name, stored_name, caption, size, uploaded_at, uploaded_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (CID, rid, original, stored, caption, size, TS, AGENT),
        )
        con.execute(
            "INSERT INTO audit_log (campaign_id, user_name, action, detail, created_at) VALUES (?,?,?,?,?)",
            (CID, AGENT, "ajout_capture_cre", f"Ligne {row['cle'] or row['operande']} : {original}", TS),
        )
        n += 1

    con.commit()
    con.close()
    print(f"{n} captures CRE rattachees aux 20 premieres lignes de la campagne #{CID}.")


if __name__ == "__main__":
    main()
