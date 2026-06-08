"""TURPE_Maj_Prix - Reconciliation des exports EKDI (SAP) pour la MAJ tarifaire annuelle.

Brique 1 : compare deux exports EKDI (annee N et N-1) et met en evidence les ecarts,
clef par clef, sur le triplet (Tarif, GrpValFix, Operande). Gere nativement le decalage
de validite des operandes en "_P" (valides au 01.07 au lieu du 01.08).
"""
import io
import os
import sqlite3
from datetime import date, datetime

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file, url_for

APP_NAME = "TURPE_Maj_Prix"
APP_VERSION = "1.0.0"
APP_DESCRIPTION = "Reconciliation des grilles tarifaires TURPE entre deux exports EKDI"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 Mo

# Colonnes attendues dans un export EKDI (les libelles SAP exacts)
COL_TARIF = "Tarif"
COL_GRP = "Grpe ValFix pr tarif"
COL_OP = "Opérande"
COL_VALID_FROM = "Valide du"
COL_VALID_TO = "Fin de validité"
COL_VALUE = "Valeur de saisie"
REQUIRED_COLS = [COL_TARIF, COL_GRP, COL_OP, COL_VALID_FROM, COL_VALUE]

# Seuil d'alerte sur l'evolution annuelle (au-dela => valeur potentiellement aberrante)
ABERRATION_THRESHOLD = 0.20  # 20 %


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            ref_date TEXT NOT NULL,
            n_lines INTEGER,
            n_new INTEGER,
            n_removed INTEGER,
            n_changed INTEGER,
            n_aberrant INTEGER
        )"""
    )
    con.commit()
    con.close()


def excel_serial_to_date(value):
    """Convertit une serie de date Excel/SAP en date Python. Tolere les datetime deja parses."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (datetime, date)):
        return value if isinstance(value, date) and not isinstance(value, datetime) else value.date()
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    # 2958465 = 31/12/9999 (validite illimitee SAP)
    base = date(1899, 12, 30)
    try:
        return base + pd.Timedelta(days=n).to_pytimedelta()
    except (OverflowError, ValueError):
        return None


def normalize_date(value):
    d = excel_serial_to_date(value)
    if d is None:
        return None
    if isinstance(d, datetime):
        d = d.date()
    return d


def load_ekdi(file_storage):
    """Charge un export EKDI en DataFrame normalise. Leve ValueError si colonnes manquantes."""
    raw = file_storage.read()
    df = pd.read_excel(io.BytesIO(raw), sheet_name=0)
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            "Colonnes manquantes dans l'export EKDI : " + ", ".join(missing)
        )
    df = df[[c for c in [COL_TARIF, COL_GRP, COL_OP, COL_VALID_FROM, COL_VALID_TO, COL_VALUE] if c in df.columns]].copy()
    for c in [COL_TARIF, COL_GRP, COL_OP]:
        df[c] = df[c].fillna("").astype(str).str.strip()
    df["valide_du"] = df[COL_VALID_FROM].apply(normalize_date)
    df["valeur"] = pd.to_numeric(df[COL_VALUE], errors="coerce")
    return df


def select_effective_rows(df, ref_date, p_ref_date):
    """Pour chaque triplet (Tarif,GrpValFix,Operande), retient la ligne valide a la
    date d'effet voulue. Les operandes en _P utilisent p_ref_date (un mois plus tot)."""
    rows = {}
    for _, r in df.iterrows():
        op = r[COL_OP]
        key = (r[COL_TARIF], r[COL_GRP], op)
        target = p_ref_date if op.endswith("_P") else ref_date
        vd = r["valide_du"]
        if vd is None:
            continue
        # On garde la ligne dont 'valide du' est <= date cible et la plus recente
        if vd <= target:
            prev = rows.get(key)
            if prev is None or vd > prev["valide_du"]:
                rows[key] = {"valide_du": vd, "valeur": r["valeur"]}
    return rows


def reconcile(df_new, df_old, ref_date):
    p_ref_date = date(ref_date.year, ref_date.month - 1, 1) if ref_date.month > 1 else date(ref_date.year - 1, 12, 1)
    new_rows = select_effective_rows(df_new, ref_date, p_ref_date)
    # Pour l'ancien export, on prend simplement la valeur la plus recente disponible par cle
    old_rows = {}
    for _, r in df_old.iterrows():
        key = (r[COL_TARIF], r[COL_GRP], r[COL_OP])
        vd = r["valide_du"]
        if vd is None:
            continue
        prev = old_rows.get(key)
        if prev is None or vd > prev["valide_du"]:
            old_rows[key] = {"valide_du": vd, "valeur": r["valeur"]}

    all_keys = sorted(set(new_rows) | set(old_rows))
    results = []
    counts = {"new": 0, "removed": 0, "changed": 0, "unchanged": 0, "aberrant": 0}
    for key in all_keys:
        n = new_rows.get(key)
        o = old_rows.get(key)
        tarif, grp, op = key
        nv = n["valeur"] if n else None
        ov = o["valeur"] if o else None
        if n and not o:
            status = "new"
        elif o and not n:
            status = "removed"
        elif nv is not None and ov is not None and abs(nv - ov) > 1e-9:
            status = "changed"
        else:
            status = "unchanged"
        counts[status] += 1
        pct = None
        aberrant = False
        if status == "changed" and ov not in (None, 0):
            pct = (nv - ov) / abs(ov)
            if abs(pct) > ABERRATION_THRESHOLD:
                aberrant = True
                counts["aberrant"] += 1
        results.append(
            {
                "tarif": tarif,
                "grp": grp,
                "operande": op,
                "is_p": op.endswith("_P"),
                "valeur_old": ov,
                "valeur_new": nv,
                "valide_du": n["valide_du"].isoformat() if n else (o["valide_du"].isoformat() if o else None),
                "status": status,
                "pct": pct,
                "aberrant": aberrant,
            }
        )
    return results, counts, p_ref_date


@app.route("/health")
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": APP_NAME, "version": APP_VERSION})


@app.route("/")
def index():
    return render_template("index.html", app_name=APP_NAME, app_version=APP_VERSION,
                           app_description=APP_DESCRIPTION)


@app.route("/api/reconcile", methods=["POST"])
def api_reconcile():
    if "file_new" not in request.files or "file_old" not in request.files:
        return jsonify({"error": "Deux fichiers EKDI sont requis (annee N et N-1)."}), 400
    ref_str = request.form.get("ref_date", "")
    try:
        ref_date = datetime.strptime(ref_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Date d'effet invalide (format AAAA-MM-JJ)."}), 400
    try:
        df_new = load_ekdi(request.files["file_new"])
        df_old = load_ekdi(request.files["file_old"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Lecture impossible : verifier que les fichiers sont des exports EKDI .xlsx valides."}), 400

    results, counts, p_ref = reconcile(df_new, df_old, ref_date)

    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO runs (created_at, ref_date, n_lines, n_new, n_removed, n_changed, n_aberrant) VALUES (?,?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), ref_date.isoformat(), len(results),
         counts["new"], counts["removed"], counts["changed"], counts["aberrant"]),
    )
    con.commit()
    con.close()

    return jsonify(
        {
            "ref_date": ref_date.isoformat(),
            "p_ref_date": p_ref.isoformat(),
            "counts": counts,
            "results": results,
        }
    )


@app.route("/api/export", methods=["POST"])
def api_export():
    """Reconstruit la reconciliation et renvoie un .xlsx des ecarts."""
    payload = request.get_json(silent=True) or {}
    rows = payload.get("results", [])
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df[["tarif", "grp", "operande", "valide_du", "valeur_old", "valeur_new", "pct", "status", "aberrant"]]
        df.columns = ["Tarif", "GrpValFix", "Operande", "Valide du", "Valeur N-1", "Valeur N", "Evolution", "Statut", "Aberrant"]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="Ecarts EKDI")
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="ecarts_EKDI.xlsx",
    )


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9998, debug=False)
