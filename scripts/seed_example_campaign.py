"""Cree la campagne EXEMPLE « Fevrier 2025 », saisie proprement de bout en bout.

A partir des deux deliberations CRE du 15/01/2025 :
  - 250115__2025-08_Evolution_TURPE_6_HTA-BT.pdf
  - 250115_2025-09_Evolution_TURPE_6_HTB.pdf

La campagne est seedee comme si un agent l'avait remplie correctement :
  - les 2 PDF rattaches (cre_pdf_2 = HTA-BT, cre_pdf_1 = HTB) ;
  - la grille instanciee depuis reference_grid.json, chaque clé recevant sa valeur
    02.2025 (= valeurs lues dans les PDF, cf. docs/mapping_CRE_PDF.md) ;
  - une trace ligne par ligne (grid_row_history) pour chaque saisie et chaque
    validation (exigence audit v2.1) ;
  - validation de toutes les lignes servies par le chef de projet RTE ;
  - verifications metier TMA puis RTE (verdict OK) ;
  - journal d'audit coherent ; statut final 'cloture'.

Idempotent : supprime une eventuelle campagne exemple homonyme avant de recreer.

Usage : python3 scripts/seed_example_campaign.py
"""
import json
import os
import shutil
import sqlite3
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
REFERENCE_GRID_PATH = os.path.join(DATA_DIR, "reference_grid.json")
CRE_DIR = os.path.join(DATA_DIR, "Délibérations CRE")

PERIOD = "02.2025"
EFFECTIVE_DATE = "2025-02-01"
NAME = "Fevrier 2025 (exemple)"

# Acteurs fictifs mais plausibles pour la trace d'audit.
AGENT = "Agent TMA"
CDP = "Chef de projet RTE"

# Horodatages realistes du deroule de la campagne (process de janv/fev 2025).
TS_CREATE = "2025-01-16T09:12:00"
TS_UPLOAD = "2025-01-16T09:20:00"
TS_SAISIE = "2025-01-20T14:30:00"
TS_VERIF_TMA = "2025-01-22T10:05:00"
TS_VALIDATE = "2025-01-24T16:40:00"
TS_VERIF_RTE = "2025-01-27T11:15:00"
TS_CLOTURE = "2025-02-01T08:00:00"

PDFS = [
    # (kind, nom de fichier source)
    ("cre_pdf_2", "250115__2025-08_Evolution_TURPE_6_HTA-BT.pdf"),
    ("cre_pdf_1", "250115_2025-09_Evolution_TURPE_6_HTB.pdf"),
]


def main():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys=ON;")
    con.row_factory = sqlite3.Row

    # --- Idempotence : purge d'une campagne exemple precedente -----------------
    old = con.execute("SELECT id FROM campaigns WHERE name=?", (NAME,)).fetchall()
    for r in old:
        oid = r["id"]
        con.execute("DELETE FROM campaigns WHERE id=?", (oid,))  # ON DELETE CASCADE
        shutil.rmtree(os.path.join(UPLOAD_DIR, str(oid)), ignore_errors=True)
        print(f"Ancienne campagne exemple #{oid} supprimee.")
    con.commit()

    # --- Campagne --------------------------------------------------------------
    cur = con.execute(
        "INSERT INTO campaigns (name, period_label, effective_date, status, created_at, created_by) "
        "VALUES (?,?,?,?,?,?)",
        (NAME, PERIOD, EFFECTIVE_DATE, "cloture", TS_CREATE, CDP),
    )
    cid = cur.lastrowid
    audit(con, cid, CDP, TS_CREATE, "creation_campagne",
          f"{NAME} (periode {PERIOD}, effet {EFFECTIVE_DATE})")

    # --- PDF rattaches ---------------------------------------------------------
    cdir = os.path.join(UPLOAD_DIR, str(cid))
    os.makedirs(cdir, exist_ok=True)
    for kind, fname in PDFS:
        src = os.path.join(CRE_DIR, fname)
        stored = f"{kind}_20250116_092000_{safe(fname)}"
        shutil.copy2(src, os.path.join(cdir, stored))
        size = os.path.getsize(src)
        con.execute(
            "INSERT INTO campaign_files (campaign_id, kind, original_name, stored_name, size, uploaded_at, uploaded_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, kind, fname, stored, size, TS_UPLOAD, AGENT),
        )
        audit(con, cid, AGENT, TS_UPLOAD, "upload_fichier", f"{kind} : {fname}")

    # --- Grille : instanciation + saisie 02.2025 -------------------------------
    with open(REFERENCE_GRID_PATH, encoding="utf-8") as f:
        ref = json.load(f)

    n_saisies = n_validees = 0
    for rrec in ref["rows"]:
        history = rrec["history"]
        val = history.get(PERIOD)
        rid = con.execute(
            "INSERT INTO grid_rows (campaign_id, sort_order, section, tarif_label, tarif_ekdi, "
            "grp, operande, cle, history, new_value, "
            "validated_rte, validated_rte_by, validated_rte_profile, validated_rte_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, rrec["order"], rrec["section"], rrec["tarif_label"], rrec["tarif_ekdi"],
             rrec["grp"], rrec["operande"], rrec["cle"],
             json.dumps(history, ensure_ascii=False),
             val,
             1 if val is not None else 0,
             CDP if val is not None else None,
             "Chef de projet DSIT" if val is not None else None,
             TS_VALIDATE if val is not None else None),
        ).lastrowid

        if val is not None:
            # Trace de saisie (None -> valeur) par l'agent TMA.
            row_history(con, cid, rid, "valeur", None, val, AGENT, TS_SAISIE)
            n_saisies += 1
            # Trace de validation DSIT (non validee -> validee) par le CDP.
            row_history(con, cid, rid, "validation_rte", "non validee", "validee", CDP, TS_VALIDATE)
            n_validees += 1

    audit(con, cid, AGENT, TS_SAISIE, "saisie_grille", f"{n_saisies} ligne(s) saisie(s)")
    audit(con, cid, CDP, TS_VALIDATE, "validation_ligne", f"{n_validees} ligne(s) validee(s)")

    # --- Verifications metier (TMA puis RTE) -----------------------------------
    add_verif(con, cid, "tma", "ok", AGENT, TS_VERIF_TMA,
              "Saisie conforme aux deliberations n°2025-08 et n°2025-09.")
    add_verif(con, cid, "rte", "ok", CDP, TS_VERIF_RTE,
              "Controle clé par clé OK, aucun ecart > 20% non justifie.")

    # --- Cloture ---------------------------------------------------------------
    audit(con, cid, CDP, TS_CLOTURE, "changement_etape", "cloture")

    con.commit()
    con.close()
    print(f"Campagne exemple #{cid} creee : {n_saisies} valeurs saisies, "
          f"{n_validees} lignes validees, 2 PDF rattaches, statut 'cloture'.")


# ---------------------------------------------------------------------------
def safe(name):
    import re
    name = os.path.basename(name or "fichier")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "fichier"


def audit(con, cid, user, ts, action, detail):
    con.execute(
        "INSERT INTO audit_log (campaign_id, user_name, action, detail, created_at) VALUES (?,?,?,?,?)",
        (cid, user, action, detail, ts),
    )


def row_history(con, cid, rid, field, old, new, user, ts):
    con.execute(
        "INSERT INTO grid_row_history (campaign_id, row_id, field, old_value, new_value, user_name, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (cid, rid, field, None if old is None else str(old),
         None if new is None else str(new), user, ts),
    )


def add_verif(con, cid, role, verdict, verifier, ts, comment):
    con.execute(
        "INSERT INTO verifications (campaign_id, role, verdict, verifier, comment, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (cid, role, verdict, verifier, comment, ts),
    )
    audit(con, cid, verifier, ts, "verification", f"{role.upper()} : {verdict.upper()} - {comment}")


if __name__ == "__main__":
    main()
