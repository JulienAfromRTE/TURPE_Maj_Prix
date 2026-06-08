"""Genere data/reference_grid.json a partir de l'Excel de suivi TURPE.

La feuille "2.Mise a jour et verif PROD" est tres irreguliere : certaines
composantes (Soutirage PV, Soutirage PF, echeance _P, regroupement, CACS, DER...)
disposent leurs operandes en **blocs cote a cote**. On aplatit tout en une grille a
plat : un enregistrement = (section, tarif, operande, cle de prix) + l'historique des
valeurs par periode.

Chaque section commence par une ligne d'entete (col 1 = "Tarif...", col 3 =
"Operande"). Les blocs se repetent ensuite tous les 8 colonnes a partir de la col 3 :
[Operande, Cle de prix, 6 valeurs].

Usage : python scripts/build_reference_grid.py
"""
import json
import os
import re

import openpyxl

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "data", "MAJ_Tarifaire__Aout_2025_23072025.xlsx")
SHEET = "2.Mise à jour et vérif PROD"
OUT = os.path.join(BASE, "data", "reference_grid.json")

BLOCK = 8          # largeur d'un bloc operande
FIRST_COL = 3      # 1re colonne "Operande" (index 0-based)
N_VALUES = 6       # nombre de colonnes de valeurs par bloc


def clean(v):
    return "" if v is None else re.sub(r"\s+", " ", str(v)).strip()


def to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    rows = [list(r) for r in wb[SHEET].iter_rows(values_only=True)]

    records = []
    section = periods = None
    order = 0
    for row in rows:
        c0, c3 = clean(row[0]), clean(row[3])
        # Entete de section
        if clean(row[1]).startswith("Tarif") and c3 == "Opérande":
            section = c0
            periods = [clean(row[FIRST_COL + 2 + k]).replace("Valeur", "").strip()
                       for k in range(N_VALUES)]
            continue
        if section is None or (not c0 and not c3):
            continue
        tarif_label, tarif_ekdi, grp = c0, clean(row[1]), clean(row[2])
        b = 0
        while True:
            base = FIRST_COL + b * BLOCK
            if base + 1 >= len(row):
                break
            op, cle = clean(row[base]), clean(row[base + 1])
            vals = [to_float(row[base + 2 + k]) if base + 2 + k < len(row) else None
                    for k in range(N_VALUES)]
            if op and (cle or any(v is not None for v in vals)):
                order += 1
                records.append({
                    "order": order, "section": section, "tarif_label": tarif_label,
                    "tarif_ekdi": tarif_ekdi, "grp": grp, "operande": op, "cle": cle,
                    "history": dict(zip(periods, vals)),
                })
            b += 1
            if base + BLOCK >= len(row):
                break

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"source": os.path.basename(SRC), "sheet": SHEET, "rows": records},
                  f, ensure_ascii=False, indent=1)
    print(f"{len(records)} lignes -> {OUT}")


if __name__ == "__main__":
    main()
