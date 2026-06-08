"""TURPE_Maj_Prix - Portail de gestion des mises a jour tarifaires TURPE.

Permet de suivre, au fil de l'eau, chaque campagne de mise a jour des prix (la MAJ
annuelle du 1er aout, mais aussi les MAJ intermediaires). Chaque campagne suit un
workflow trace de bout en bout :

  1. Upload des deliberations CRE (PDF), stockees sur le serveur et retelechargeables.
  2. Saisie en ligne de la grille tarifaire (instanciee avec l'historique des MAJ
     precedentes), exportable en .xlsx pour injection dans SAP.
  3. Verification post-injection : comparaison d'un extract EKDI (et/ou EA09) avec
     les valeurs saisies dans la grille.

Chaque campagne fait l'objet de verifications (TMA puis chef de projet DSIT), toutes
les actions sont tracees dans un journal (qui, quoi, quand). L'ecriture dans SAP
reste hors perimetre : l'application travaille en lecture/preparation uniquement.

Brique historique conservee : la reconciliation EKDI N / N-1 (triplet
Tarif/GrpValFix/Operande, gestion du decalage _P au 01.07).
"""
import io
import json
import os
import re
import sqlite3
from datetime import date, datetime

import pandas as pd
from flask import (Flask, abort, g, jsonify, render_template, request,
                   send_file, send_from_directory, url_for)

APP_NAME = "TURPE_Maj_Prix"
APP_VERSION = "2.3.0"
APP_DESCRIPTION = "Portail de gestion des mises a jour tarifaires TURPE"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
REFERENCE_GRID_PATH = os.path.join(DATA_DIR, "reference_grid.json")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 Mo (PDF CRE volumineux)

# ---------------------------------------------------------------------------
# Colonnes EKDI (libelles SAP exacts) -- utilisees par la reconciliation N/N-1
# et par la verification post-injection.
# ---------------------------------------------------------------------------
COL_TARIF = "Tarif"
COL_GRP = "Grpe ValFix pr tarif"
COL_OP = "Opérande"
COL_VALID_FROM = "Valide du"
COL_VALID_TO = "Fin de validité"
COL_VALUE = "Valeur de saisie"
REQUIRED_COLS = [COL_TARIF, COL_GRP, COL_OP, COL_VALID_FROM, COL_VALUE]

ABERRATION_THRESHOLD = 0.20  # 20 % -> alerte valeur potentiellement aberrante

# Etapes du workflow d'une campagne (ordre = progression)
WORKFLOW_STEPS = [
    ("pdf", "Deliberations CRE"),
    ("grid", "Saisie grille tarifaire"),
    ("verif", "Verification EKDI / EA09"),
]
# Roles de verification metier
VERIF_ROLES = [
    ("tma", "TMA"),
    ("rte", "Chef de projet DSIT"),
]
# Profils utilisateur saisis a l'ouverture, traces sur chaque action (audit).
USER_PROFILES = ["TMA", "Chef de projet DSIT"]
# Types de fichiers stockes par campagne
FILE_KINDS = {
    "cre_pdf_1": "Deliberation CRE HTB",
    "cre_pdf_2": "Deliberation CRE HTA",
    "ekdi": "Extract EKDI",
    "ea09": "Extract EA09",
    "export": "Export grille (.xlsx)",
}


# ---------------------------------------------------------------------------
# Base de donnees
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON;")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            period_label TEXT NOT NULL,
            effective_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pdf',
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS grid_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            sort_order INTEGER NOT NULL,
            section TEXT, tarif_label TEXT, tarif_ekdi TEXT,
            grp TEXT, operande TEXT, cle TEXT,
            history TEXT,          -- JSON {periode: valeur}
            new_value REAL,        -- valeur saisie pour cette campagne
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS campaign_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            size INTEGER,
            uploaded_at TEXT NOT NULL,
            uploaded_by TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            verdict TEXT NOT NULL,     -- ok | ko
            verifier TEXT NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER,
            user_name TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );

        -- Historique fin, ligne par ligne (exigence audit) : chaque modification de
        -- valeur ou de validation laisse une trace horodatee et nominative.
        CREATE TABLE IF NOT EXISTS grid_row_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            row_id INTEGER NOT NULL,
            field TEXT NOT NULL,          -- 'valeur' | 'validation'
            old_value TEXT,
            new_value TEXT,
            user_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
        );

        -- Captures d'ecran des tableaux CRE rattachees a une ligne (cle de prix),
        -- a l'image de l'onglet 1 du fichier Excel de suivi.
        CREATE TABLE IF NOT EXISTS grid_row_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            row_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            caption TEXT,
            size INTEGER,
            uploaded_at TEXT NOT NULL,
            uploaded_by TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
        );

        -- Commentaires libres rattaches a une ligne (cle de prix) : fil de notes
        -- horodatees et nominatives, distinct de l'historique des valeurs/validations.
        CREATE TABLE IF NOT EXISTS grid_row_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            row_id INTEGER NOT NULL,
            comment TEXT NOT NULL,
            user_name TEXT NOT NULL,
            user_profile TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_rowhist_row ON grid_row_history(row_id);
        CREATE INDEX IF NOT EXISTS idx_rowimg_row ON grid_row_images(row_id);
        CREATE INDEX IF NOT EXISTS idx_rowcmt_row ON grid_row_comments(row_id);
        """
    )
    # Migration : colonnes de validation sur grid_rows (bases anterieures a la v2.1).
    existing = {r[1] for r in con.execute("PRAGMA table_info(grid_rows)").fetchall()}
    for col, ddl in (
        ("validated", "INTEGER NOT NULL DEFAULT 0"),
        ("validated_by", "TEXT"),
        ("validated_at", "TEXT"),
        ("validated_profile", "TEXT"),
    ):
        if col not in existing:
            con.execute(f"ALTER TABLE grid_rows ADD COLUMN {col} {ddl}")
    # Migration : profil utilisateur (TMA | Chef de projet DSIT) trace sur les actions
    # (bases anterieures a la v2.2).
    for table, col in (
        ("audit_log", "user_profile"),
        ("grid_row_history", "user_profile"),
        ("verifications", "verifier_profile"),
    ):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    # Migration : corbeille (soft-delete). Une campagne n'est jamais supprimee
    # physiquement, seulement marquee comme mise a la corbeille (v2.3).
    camp_cols = {r[1] for r in con.execute("PRAGMA table_info(campaigns)").fetchall()}
    for col in ("deleted_at", "deleted_by", "deleted_profile"):
        if col not in camp_cols:
            con.execute(f"ALTER TABLE campaigns ADD COLUMN {col} TEXT")
    con.commit()
    con.close()


def current_user():
    """Nom de l'utilisateur courant (saisi a l'ouverture, envoye par le front)."""
    name = request.headers.get("X-User-Name") or request.form.get("user_name") or ""
    name = name.strip()
    return name or "Inconnu"


def current_profile():
    """Profil de l'utilisateur courant (TMA | Chef de projet DSIT), envoye par le front."""
    prof = request.headers.get("X-User-Profile") or request.form.get("user_profile") or ""
    return prof.strip()


def audit(campaign_id, action, detail=""):
    db = get_db()
    db.execute(
        "INSERT INTO audit_log (campaign_id, user_name, user_profile, action, detail, created_at) VALUES (?,?,?,?,?,?)",
        (campaign_id, current_user(), current_profile(), action, detail, now_iso()),
    )
    db.commit()


def record_row_history(db, campaign_id, row_id, field, old_value, new_value):
    """Trace une modification d'une ligne de grille (audit fin)."""
    db.execute(
        """INSERT INTO grid_row_history
           (campaign_id, row_id, field, old_value, new_value, user_name, user_profile, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (campaign_id, row_id, field,
         None if old_value is None else str(old_value),
         None if new_value is None else str(new_value),
         current_user(), current_profile(), now_iso()),
    )


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Conversion des dates Excel/SAP (reprises de la brique 1)
# ---------------------------------------------------------------------------
def excel_serial_to_date(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (datetime, date)):
        return value if isinstance(value, date) and not isinstance(value, datetime) else value.date()
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    base = date(1899, 12, 30)
    try:
        return base + pd.Timedelta(days=n).to_pytimedelta()
    except (OverflowError, ValueError):
        return None


def normalize_date(value):
    d = excel_serial_to_date(value)
    if d is None:
        return None
    return d.date() if isinstance(d, datetime) else d


def load_ekdi(file_storage):
    """Charge un export EKDI en DataFrame normalise. Leve ValueError si colonnes manquantes."""
    raw = file_storage.read()
    df = pd.read_excel(io.BytesIO(raw), sheet_name=0)
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError("Colonnes manquantes dans l'export EKDI : " + ", ".join(missing))
    keep = [c for c in [COL_TARIF, COL_GRP, COL_OP, COL_VALID_FROM, COL_VALID_TO, COL_VALUE] if c in df.columns]
    df = df[keep].copy()
    for c in [COL_TARIF, COL_GRP, COL_OP]:
        df[c] = df[c].fillna("").astype(str).str.strip()
    df["valide_du"] = df[COL_VALID_FROM].apply(normalize_date)
    df["valeur"] = pd.to_numeric(df[COL_VALUE], errors="coerce")
    return df


def select_effective_rows(df, ref_date, p_ref_date):
    rows = {}
    for _, r in df.iterrows():
        op = r[COL_OP]
        key = (r[COL_TARIF], r[COL_GRP], op)
        target = p_ref_date if op.endswith("_P") else ref_date
        vd = r["valide_du"]
        if vd is None or vd > target:
            continue
        prev = rows.get(key)
        if prev is None or vd > prev["valide_du"]:
            rows[key] = {"valide_du": vd, "valeur": r["valeur"]}
    return rows


def reconcile(df_new, df_old, ref_date):
    p_ref_date = date(ref_date.year, ref_date.month - 1, 1) if ref_date.month > 1 else date(ref_date.year - 1, 12, 1)
    new_rows = select_effective_rows(df_new, ref_date, p_ref_date)
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
        results.append({
            "tarif": tarif, "grp": grp, "operande": op, "is_p": op.endswith("_P"),
            "valeur_old": ov, "valeur_new": nv,
            "valide_du": n["valide_du"].isoformat() if n else (o["valide_du"].isoformat() if o else None),
            "status": status, "pct": pct, "aberrant": aberrant,
        })
    return results, counts, p_ref_date


# ---------------------------------------------------------------------------
# Grille de reference (instanciation des nouvelles campagnes)
# ---------------------------------------------------------------------------
def load_reference_grid():
    with open(REFERENCE_GRID_PATH, encoding="utf-8") as f:
        return json.load(f)


def latest_history_value(history):
    """Derniere valeur connue (periode la plus recente) dans l'historique."""
    if not history:
        return None
    best_key, best_val = None, None
    for period, val in history.items():
        if val is None:
            continue
        k = _period_sort_key(period)
        if best_key is None or k > best_key:
            best_key, best_val = k, val
    return best_val


def _period_sort_key(period):
    """'08.2025' -> (2025, 8). Tolere les libelles bruites."""
    m = re.search(r"(\d{2})\.(\d{4})", period)
    if m:
        return (int(m.group(2)), int(m.group(1)))
    return (0, 0)


def instantiate_grid(campaign_id):
    ref = load_reference_grid()
    db = get_db()
    for r in ref["rows"]:
        db.execute(
            """INSERT INTO grid_rows (campaign_id, sort_order, section, tarif_label,
               tarif_ekdi, grp, operande, cle, history, new_value)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (campaign_id, r["order"], r["section"], r["tarif_label"], r["tarif_ekdi"],
             r["grp"], r["operande"], r["cle"], json.dumps(r["history"], ensure_ascii=False), None),
        )
    db.commit()


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def campaign_to_dict(row, db):
    cid = row["id"]
    files = db.execute(
        "SELECT id, kind, original_name, size, uploaded_at, uploaded_by FROM campaign_files WHERE campaign_id=? ORDER BY uploaded_at",
        (cid,),
    ).fetchall()
    verifs = db.execute(
        "SELECT id, role, verdict, verifier, verifier_profile, comment, created_at FROM verifications WHERE campaign_id=? ORDER BY created_at",
        (cid,),
    ).fetchall()
    n_rows = db.execute("SELECT COUNT(*) FROM grid_rows WHERE campaign_id=?", (cid,)).fetchone()[0]
    n_filled = db.execute(
        "SELECT COUNT(*) FROM grid_rows WHERE campaign_id=? AND new_value IS NOT NULL", (cid,)
    ).fetchone()[0]
    return {
        "id": cid,
        "name": row["name"],
        "period_label": row["period_label"],
        "effective_date": row["effective_date"],
        "status": row["status"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "n_rows": n_rows,
        "n_filled": n_filled,
        "files": [dict(f) for f in files],
        "verifications": [dict(v) for v in verifs],
        "deleted_at": row["deleted_at"],
        "deleted_by": row["deleted_by"],
    }


def get_campaign_or_404(cid):
    db = get_db()
    row = db.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if row is None:
        abort(404, description="Campagne introuvable.")
    return row


# ---------------------------------------------------------------------------
# Routes -- pages
# ---------------------------------------------------------------------------
@app.route("/health")
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": APP_NAME, "version": APP_VERSION})


@app.route("/")
def index():
    return render_template("index.html", app_name=APP_NAME, app_version=APP_VERSION,
                           app_description=APP_DESCRIPTION)


@app.route("/campaign/<int:cid>")
def campaign_page(cid):
    get_campaign_or_404(cid)
    return render_template("campaign.html", app_name=APP_NAME, app_version=APP_VERSION,
                           campaign_id=cid, workflow_steps=WORKFLOW_STEPS,
                           verif_roles=VERIF_ROLES)


@app.route("/reconcile")
def reconcile_page():
    return render_template("reconcile.html", app_name=APP_NAME, app_version=APP_VERSION,
                           app_description=APP_DESCRIPTION)


# ---------------------------------------------------------------------------
# Routes -- campagnes
# ---------------------------------------------------------------------------
@app.route("/api/campaigns", methods=["GET"])
def api_list_campaigns():
    db = get_db()
    rows = db.execute("SELECT * FROM campaigns WHERE deleted_at IS NULL ORDER BY effective_date DESC, id DESC").fetchall()
    return jsonify({"campaigns": [campaign_to_dict(r, db) for r in rows]})


@app.route("/api/campaigns", methods=["POST"])
def api_create_campaign():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    period = (payload.get("period_label") or "").strip()
    eff = (payload.get("effective_date") or "").strip()
    if not name or not period or not eff:
        return jsonify({"error": "Nom, periode et date d'effet sont requis."}), 400
    try:
        datetime.strptime(eff, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Date d'effet invalide (AAAA-MM-JJ)."}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO campaigns (name, period_label, effective_date, status, created_at, created_by) VALUES (?,?,?,?,?,?)",
        (name, period, eff, "pdf", now_iso(), current_user()),
    )
    cid = cur.lastrowid
    db.commit()
    instantiate_grid(cid)
    audit(cid, "creation_campagne", f"{name} (periode {period}, effet {eff})")
    row = db.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    return jsonify(campaign_to_dict(row, db)), 201


@app.route("/api/campaigns/<int:cid>", methods=["GET"])
def api_get_campaign(cid):
    row = get_campaign_or_404(cid)
    return jsonify(campaign_to_dict(row, get_db()))


@app.route("/api/campaigns/<int:cid>/status", methods=["POST"])
def api_set_status(cid):
    get_campaign_or_404(cid)
    payload = request.get_json(silent=True) or {}
    status = (payload.get("status") or "").strip()
    valid = {k for k, _ in WORKFLOW_STEPS} | {"injecte", "cloture"}
    if status not in valid:
        return jsonify({"error": "Statut invalide."}), 400
    db = get_db()
    db.execute("UPDATE campaigns SET status=? WHERE id=?", (status, cid))
    db.commit()
    audit(cid, "changement_etape", status)
    return jsonify({"status": status})


@app.route("/api/campaigns/trash", methods=["GET"])
def api_list_trash():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM campaigns WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC, id DESC"
    ).fetchall()
    return jsonify({"campaigns": [campaign_to_dict(r, db) for r in rows]})


@app.route("/api/campaigns/<int:cid>", methods=["DELETE"])
def api_trash_campaign(cid):
    """Mise a la corbeille (soft-delete). La campagne n'est jamais supprimee
    physiquement : toutes ses donnees et son audit restent intacts, elle est
    seulement masquee de la liste et peut etre restauree."""
    row = get_campaign_or_404(cid)
    if row["deleted_at"] is not None:
        return jsonify({"error": "Campagne deja dans la corbeille."}), 400
    payload = request.get_json(silent=True) or {}
    confirm = (payload.get("confirm_name") or "").strip()
    if confirm != row["name"]:
        return jsonify({"error": "Le nom saisi ne correspond pas a la campagne."}), 400
    db = get_db()
    db.execute(
        "UPDATE campaigns SET deleted_at=?, deleted_by=?, deleted_profile=? WHERE id=?",
        (now_iso(), current_user(), current_profile(), cid),
    )
    db.commit()
    audit(cid, "mise_corbeille", row["name"])
    fresh = get_campaign_or_404(cid)
    return jsonify(campaign_to_dict(fresh, db))


@app.route("/api/campaigns/<int:cid>/restore", methods=["POST"])
def api_restore_campaign(cid):
    row = get_campaign_or_404(cid)
    if row["deleted_at"] is None:
        return jsonify({"error": "Campagne deja active."}), 400
    db = get_db()
    db.execute(
        "UPDATE campaigns SET deleted_at=NULL, deleted_by=NULL, deleted_profile=NULL WHERE id=?",
        (cid,),
    )
    db.commit()
    audit(cid, "restauration_corbeille", row["name"])
    fresh = get_campaign_or_404(cid)
    return jsonify(campaign_to_dict(fresh, db))


# ---------------------------------------------------------------------------
# Routes -- fichiers
# ---------------------------------------------------------------------------
def _safe_name(name):
    name = os.path.basename(name or "fichier")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "fichier"


@app.route("/api/campaigns/<int:cid>/files", methods=["POST"])
def api_upload_file(cid):
    get_campaign_or_404(cid)
    kind = request.form.get("kind", "")
    if kind not in FILE_KINDS:
        return jsonify({"error": "Type de fichier inconnu."}), 400
    if "file" not in request.files or request.files["file"].filename == "":
        return jsonify({"error": "Aucun fichier fourni."}), 400
    f = request.files["file"]
    cdir = os.path.join(UPLOAD_DIR, str(cid))
    os.makedirs(cdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stored = f"{kind}_{stamp}_{_safe_name(f.filename)}"
    path = os.path.join(cdir, stored)
    f.save(path)
    size = os.path.getsize(path)
    db = get_db()
    db.execute(
        """INSERT INTO campaign_files (campaign_id, kind, original_name, stored_name, size, uploaded_at, uploaded_by)
           VALUES (?,?,?,?,?,?,?)""",
        (cid, kind, f.filename, stored, size, now_iso(), current_user()),
    )
    db.commit()
    audit(cid, "upload_fichier", f"{FILE_KINDS[kind]} : {f.filename} ({size} octets)")
    row = get_campaign_or_404(cid)
    return jsonify(campaign_to_dict(row, db)), 201


@app.route("/api/files/<int:file_id>/download")
def api_download_file(file_id):
    db = get_db()
    f = db.execute("SELECT * FROM campaign_files WHERE id=?", (file_id,)).fetchone()
    if f is None:
        abort(404)
    cdir = os.path.join(UPLOAD_DIR, str(f["campaign_id"]))
    return send_from_directory(cdir, f["stored_name"], as_attachment=True,
                               download_name=f["original_name"])


@app.route("/api/files/<int:file_id>", methods=["DELETE"])
def api_delete_file(file_id):
    db = get_db()
    f = db.execute("SELECT * FROM campaign_files WHERE id=?", (file_id,)).fetchone()
    if f is None:
        abort(404)
    path = os.path.join(UPLOAD_DIR, str(f["campaign_id"]), f["stored_name"])
    if os.path.exists(path):
        os.remove(path)
    db.execute("DELETE FROM campaign_files WHERE id=?", (file_id,))
    db.commit()
    audit(f["campaign_id"], "suppression_fichier", f["original_name"])
    return jsonify({"deleted": file_id})


# ---------------------------------------------------------------------------
# Routes -- grille tarifaire
# ---------------------------------------------------------------------------
@app.route("/api/campaigns/<int:cid>/grid", methods=["GET"])
def api_get_grid(cid):
    row = get_campaign_or_404(cid)
    db = get_db()
    rows = db.execute(
        "SELECT * FROM grid_rows WHERE campaign_id=? ORDER BY sort_order", (cid,)
    ).fetchall()
    # Nombre de captures CRE par ligne (une seule requete groupee).
    img_counts = {
        r["row_id"]: r["n"] for r in db.execute(
            "SELECT row_id, COUNT(*) AS n FROM grid_row_images WHERE campaign_id=? GROUP BY row_id",
            (cid,),
        ).fetchall()
    }
    cmt_counts = {
        r["row_id"]: r["n"] for r in db.execute(
            "SELECT row_id, COUNT(*) AS n FROM grid_row_comments WHERE campaign_id=? GROUP BY row_id",
            (cid,),
        ).fetchall()
    }
    out = []
    periods_order = []
    for r in rows:
        history = json.loads(r["history"] or "{}")
        for p in history:
            if p not in periods_order:
                periods_order.append(p)
        prev = latest_history_value(history)
        nv = r["new_value"]
        pct = None
        aberrant = False
        if nv is not None and prev not in (None, 0):
            pct = (nv - prev) / abs(prev)
            aberrant = abs(pct) > ABERRATION_THRESHOLD
        out.append({
            "id": r["id"], "sort_order": r["sort_order"],
            "section": r["section"], "tarif_label": r["tarif_label"],
            "tarif_ekdi": r["tarif_ekdi"], "grp": r["grp"], "operande": r["operande"],
            "cle": r["cle"], "history": history, "prev_value": prev,
            "new_value": nv, "pct": pct, "aberrant": aberrant,
            "validated": bool(r["validated"]),
            "validated_by": r["validated_by"], "validated_at": r["validated_at"],
            "n_comments": cmt_counts.get(r["id"], 0),
            "n_images": img_counts.get(r["id"], 0),
        })
    periods_order.sort(key=_period_sort_key)
    return jsonify({
        "period_label": row["period_label"],
        "periods": periods_order,
        "rows": out,
    })


@app.route("/api/campaigns/<int:cid>/grid", methods=["POST"])
def api_save_grid(cid):
    get_campaign_or_404(cid)
    payload = request.get_json(silent=True) or {}
    updates = payload.get("updates", [])
    db = get_db()
    changed = 0
    for u in updates:
        rid = u.get("id")
        raw = u.get("new_value")
        if raw in ("", None):
            val = None
        else:
            try:
                val = float(str(raw).replace(",", ".").replace(" ", ""))
            except ValueError:
                return jsonify({"error": f"Valeur non numerique pour la ligne {rid}."}), 400
        existing = db.execute(
            "SELECT new_value FROM grid_rows WHERE id=? AND campaign_id=?", (rid, cid)
        ).fetchone()
        if existing is None:
            continue
        old = existing["new_value"]
        # Modification reelle uniquement (evite de polluer l'historique a chaque save).
        same = (old is None and val is None) or (
            old is not None and val is not None and abs(old - val) <= 1e-9)
        if same:
            continue
        db.execute(
            "UPDATE grid_rows SET new_value=? WHERE id=? AND campaign_id=?", (val, rid, cid)
        )
        record_row_history(db, cid, rid, "valeur", old, val)
        changed += 1
    db.commit()
    if changed:
        audit(cid, "saisie_grille", f"{changed} ligne(s) modifiee(s)")
    return jsonify({"updated": changed})


# Champs d'identite editables d'une ligne (Tarif / Operande / Cle). La cle SAP
# repose sur ce triplet : toute correction est tracee champ par champ (audit).
IDENTITY_FIELDS = (("tarif_label", "tarif"), ("operande", "operande"), ("cle", "cle"))


@app.route("/api/campaigns/<int:cid>/rows/<int:rid>/identity", methods=["POST"])
def api_save_row_identity(cid, rid):
    get_campaign_or_404(cid)
    payload = request.get_json(silent=True) or {}
    db = get_db()
    existing = db.execute(
        "SELECT tarif_label, operande, cle FROM grid_rows WHERE id=? AND campaign_id=?",
        (rid, cid),
    ).fetchone()
    if existing is None:
        return jsonify({"error": "Ligne introuvable."}), 404
    changed = []
    for col, field in IDENTITY_FIELDS:
        if col not in payload:
            continue
        new = (payload.get(col) or "").strip()
        old = existing[col] or ""
        if new == old:
            continue
        if col == "operande" and not new:
            return jsonify({"error": "L'operande ne peut pas etre vide."}), 400
        db.execute(
            f"UPDATE grid_rows SET {col}=? WHERE id=? AND campaign_id=?", (new, rid, cid)
        )
        record_row_history(db, cid, rid, field, old or None, new or None)
        changed.append(field)
    db.commit()
    if changed:
        audit(cid, "edition_identifiant",
              f"Ligne #{rid} : {', '.join(changed)} modifie(s)")
    row = db.execute(
        "SELECT tarif_label, operande, cle FROM grid_rows WHERE id=? AND campaign_id=?",
        (rid, cid),
    ).fetchone()
    return jsonify({"updated": changed, "row": dict(row)})


def _row_comments(db, cid, rid):
    """Liste des commentaires d'une ligne, du plus recent au plus ancien."""
    rows = db.execute(
        """SELECT id, comment, user_name, user_profile, created_at
           FROM grid_row_comments WHERE campaign_id=? AND row_id=? ORDER BY id DESC""",
        (cid, rid),
    ).fetchall()
    return [dict(r) for r in rows]


@app.route("/api/campaigns/<int:cid>/rows/<int:rid>/comments", methods=["GET"])
def api_list_row_comments(cid, rid):
    get_campaign_or_404(cid)
    return jsonify({"comments": _row_comments(get_db(), cid, rid)})


@app.route("/api/campaigns/<int:cid>/rows/<int:rid>/comments", methods=["POST"])
def api_add_row_comment(cid, rid):
    """Ajoute un commentaire (fil de notes) a une ligne. Append-only, nominatif."""
    get_campaign_or_404(cid)
    payload = request.get_json(silent=True) or {}
    text = (payload.get("comment") or "").strip()
    if not text:
        return jsonify({"error": "Commentaire vide."}), 400
    db = get_db()
    exists = db.execute(
        "SELECT 1 FROM grid_rows WHERE id=? AND campaign_id=?", (rid, cid)
    ).fetchone()
    if exists is None:
        return jsonify({"error": "Ligne introuvable."}), 404
    db.execute(
        """INSERT INTO grid_row_comments (campaign_id, row_id, comment, user_name, user_profile, created_at)
           VALUES (?,?,?,?,?,?)""",
        (cid, rid, text, current_user(), current_profile(), now_iso()),
    )
    db.commit()
    audit(cid, "commentaire_ligne", f"Ligne #{rid} commentaire ajoute")
    comments = _row_comments(db, cid, rid)
    return jsonify({"comments": comments, "n_comments": len(comments)})


@app.route("/api/campaigns/<int:cid>/comments/<int:comment_id>", methods=["DELETE"])
def api_delete_row_comment(cid, comment_id):
    get_campaign_or_404(cid)
    db = get_db()
    row = db.execute(
        "SELECT row_id FROM grid_row_comments WHERE id=? AND campaign_id=?",
        (comment_id, cid),
    ).fetchone()
    if row is None:
        return jsonify({"error": "Commentaire introuvable."}), 404
    rid = row["row_id"]
    db.execute("DELETE FROM grid_row_comments WHERE id=? AND campaign_id=?", (comment_id, cid))
    db.commit()
    audit(cid, "commentaire_ligne", f"Ligne #{rid} commentaire supprime")
    comments = _row_comments(db, cid, rid)
    return jsonify({"row_id": rid, "comments": comments, "n_comments": len(comments)})


@app.route("/api/campaigns/<int:cid>/grid/export", methods=["GET"])
def api_export_grid(cid):
    row = get_campaign_or_404(cid)
    db = get_db()
    rows = db.execute(
        "SELECT * FROM grid_rows WHERE campaign_id=? ORDER BY sort_order", (cid,)
    ).fetchall()
    period = row["period_label"]
    all_periods = set()
    parsed = []
    for r in rows:
        history = json.loads(r["history"] or "{}")
        all_periods.update(history)
        parsed.append((r, history))
    ordered_periods = sorted(all_periods, key=_period_sort_key)
    base_cols = ["Section", "Tarif (EKDI)", "GrpValFix (EKDI)", "Operande", "Cle de prix"]
    columns = (base_cols + [f"Valeur {p}" for p in ordered_periods]
               + [f"Valeur {period}", "Validee", "Validee par",
                  "Profil validateur", "Validee le", "Nb captures CRE", "Nb commentaires"])
    img_counts = {
        x["row_id"]: x["n"] for x in db.execute(
            "SELECT row_id, COUNT(*) AS n FROM grid_row_images WHERE campaign_id=? GROUP BY row_id",
            (cid,),
        ).fetchall()
    }
    cmt_counts = {
        x["row_id"]: x["n"] for x in db.execute(
            "SELECT row_id, COUNT(*) AS n FROM grid_row_comments WHERE campaign_id=? GROUP BY row_id",
            (cid,),
        ).fetchall()
    }
    records = []
    for r, history in parsed:
        rec = {
            "Section": r["section"], "Tarif (EKDI)": r["tarif_ekdi"],
            "GrpValFix (EKDI)": r["grp"], "Operande": r["operande"],
            "Cle de prix": r["cle"],
        }
        for p in ordered_periods:
            rec[f"Valeur {p}"] = history.get(p)
        rec[f"Valeur {period}"] = r["new_value"]
        rec["Validee"] = "Oui" if r["validated"] else "Non"
        rec["Validee par"] = r["validated_by"]
        rec["Profil validateur"] = r["validated_profile"]
        rec["Validee le"] = (r["validated_at"] or "").replace("T", " ")
        rec["Nb captures CRE"] = img_counts.get(r["id"], 0)
        rec["Nb commentaires"] = cmt_counts.get(r["id"], 0)
        records.append(rec)
    df = pd.DataFrame(records, columns=columns)

    # Feuille 2 : historique fin ligne par ligne (audit commissaires aux comptes).
    hist_rows = db.execute(
        """SELECT h.created_at, h.user_name, h.user_profile, h.field, h.old_value, h.new_value,
                  g.section, g.tarif_label, g.operande, g.cle
           FROM grid_row_history h JOIN grid_rows g ON g.id = h.row_id
           WHERE h.campaign_id=? ORDER BY h.id""",
        (cid,),
    ).fetchall()
    hist_df = pd.DataFrame(
        [{
            "Date": h["created_at"].replace("T", " "), "Utilisateur": h["user_name"],
            "Profil": h["user_profile"],
            "Section": h["section"], "Tarif": h["tarif_label"], "Operande": h["operande"],
            "Cle de prix": h["cle"], "Champ": h["field"],
            "Ancienne valeur": h["old_value"], "Nouvelle valeur": h["new_value"],
        } for h in hist_rows],
        columns=["Date", "Utilisateur", "Profil", "Section", "Tarif", "Operande", "Cle de prix",
                 "Champ", "Ancienne valeur", "Nouvelle valeur"],
    )

    # Feuille 3 : inventaire des captures CRE rattachees.
    img_rows = db.execute(
        """SELECT i.uploaded_at, i.uploaded_by, i.original_name, i.caption,
                  g.section, g.tarif_label, g.operande, g.cle
           FROM grid_row_images i JOIN grid_rows g ON g.id = i.row_id
           WHERE i.campaign_id=? ORDER BY i.id""",
        (cid,),
    ).fetchall()
    img_df = pd.DataFrame(
        [{
            "Section": i["section"], "Tarif": i["tarif_label"], "Operande": i["operande"],
            "Cle de prix": i["cle"], "Fichier": i["original_name"], "Legende": i["caption"],
            "Depose par": i["uploaded_by"], "Depose le": i["uploaded_at"].replace("T", " "),
        } for i in img_rows],
        columns=["Section", "Tarif", "Operande", "Cle de prix", "Fichier", "Legende",
                 "Depose par", "Depose le"],
    )

    # Feuille 4 : fil des commentaires rattaches aux lignes (audit).
    cmt_rows = db.execute(
        """SELECT c.created_at, c.user_name, c.user_profile, c.comment,
                  g.section, g.tarif_label, g.operande, g.cle
           FROM grid_row_comments c JOIN grid_rows g ON g.id = c.row_id
           WHERE c.campaign_id=? ORDER BY c.id""",
        (cid,),
    ).fetchall()
    cmt_df = pd.DataFrame(
        [{
            "Date": c["created_at"].replace("T", " "), "Auteur": c["user_name"],
            "Profil": c["user_profile"],
            "Section": c["section"], "Tarif": c["tarif_label"], "Operande": c["operande"],
            "Cle de prix": c["cle"], "Commentaire": c["comment"],
        } for c in cmt_rows],
        columns=["Date", "Auteur", "Profil", "Section", "Tarif", "Operande", "Cle de prix",
                 "Commentaire"],
    )

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="Grille TURPE")
        hist_df.to_excel(xw, index=False, sheet_name="Historique modifications")
        img_df.to_excel(xw, index=False, sheet_name="Captures CRE")
        cmt_df.to_excel(xw, index=False, sheet_name="Commentaires")
    buf.seek(0)
    audit(cid, "export_grille",
          f"Export .xlsx grille periode {period} (+ historique {len(hist_rows)} traces, "
          f"{len(img_rows)} captures, {len(cmt_rows)} commentaires)")
    return send_file(
        buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=f"grille_TURPE_{_safe_name(period)}.xlsx",
    )


# ---------------------------------------------------------------------------
# Routes -- validation ligne a ligne (chef de projet RTE)
# ---------------------------------------------------------------------------
@app.route("/api/campaigns/<int:cid>/grid/validate", methods=["POST"])
def api_validate_row(cid):
    get_campaign_or_404(cid)
    payload = request.get_json(silent=True) or {}
    rid = payload.get("id")
    validated = bool(payload.get("validated"))
    db = get_db()
    row = db.execute(
        "SELECT validated FROM grid_rows WHERE id=? AND campaign_id=?", (rid, cid)
    ).fetchone()
    if row is None:
        return jsonify({"error": "Ligne introuvable."}), 404
    if bool(row["validated"]) == validated:
        return jsonify({"validated": validated, "validated_by": None,
                        "validated_profile": None, "validated_at": None})
    who = current_user()
    prof = current_profile()
    when = now_iso() if validated else None
    db.execute(
        "UPDATE grid_rows SET validated=?, validated_by=?, validated_profile=?, validated_at=? WHERE id=? AND campaign_id=?",
        (1 if validated else 0, who if validated else None,
         prof if validated else None, when, rid, cid),
    )
    record_row_history(db, cid, rid, "validation",
                       "validee" if row["validated"] else "non validee",
                       "validee" if validated else "non validee")
    db.commit()
    audit(cid, "validation_ligne",
          f"Ligne #{rid} {'validee' if validated else 'devalidee'}")
    return jsonify({"validated": validated, "validated_by": who if validated else None,
                    "validated_profile": prof if validated else None, "validated_at": when})


# ---------------------------------------------------------------------------
# Routes -- historique fin d'une ligne
# ---------------------------------------------------------------------------
@app.route("/api/campaigns/<int:cid>/rows/<int:rid>/history", methods=["GET"])
def api_row_history(cid, rid):
    get_campaign_or_404(cid)
    db = get_db()
    rows = db.execute(
        """SELECT field, old_value, new_value, user_name, user_profile, created_at
           FROM grid_row_history WHERE campaign_id=? AND row_id=? ORDER BY id DESC""",
        (cid, rid),
    ).fetchall()
    return jsonify({"entries": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Routes -- captures CRE rattachees a une ligne (cle de prix)
# ---------------------------------------------------------------------------
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


@app.route("/api/campaigns/<int:cid>/rows/<int:rid>/images", methods=["GET"])
def api_list_row_images(cid, rid):
    get_campaign_or_404(cid)
    db = get_db()
    rows = db.execute(
        """SELECT id, original_name, caption, size, uploaded_at, uploaded_by
           FROM grid_row_images WHERE campaign_id=? AND row_id=? ORDER BY id""",
        (cid, rid),
    ).fetchall()
    return jsonify({"images": [dict(r) for r in rows]})


@app.route("/api/campaigns/<int:cid>/rows/<int:rid>/images", methods=["POST"])
def api_upload_row_image(cid, rid):
    get_campaign_or_404(cid)
    db = get_db()
    row = db.execute(
        "SELECT cle, operande FROM grid_rows WHERE id=? AND campaign_id=?", (rid, cid)
    ).fetchone()
    if row is None:
        return jsonify({"error": "Ligne introuvable."}), 404
    if "file" not in request.files or request.files["file"].filename == "":
        return jsonify({"error": "Aucune image fournie."}), 400
    f = request.files["file"]
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in IMAGE_EXTS:
        return jsonify({"error": "Format image attendu (PNG, JPG, GIF, WEBP)."}), 400
    caption = (request.form.get("caption") or "").strip()
    cdir = os.path.join(UPLOAD_DIR, str(cid))
    os.makedirs(cdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stored = f"rowimg_{rid}_{stamp}{ext}"
    path = os.path.join(cdir, stored)
    f.save(path)
    size = os.path.getsize(path)
    db.execute(
        """INSERT INTO grid_row_images
           (campaign_id, row_id, original_name, stored_name, caption, size, uploaded_at, uploaded_by)
           VALUES (?,?,?,?,?,?,?,?)""",
        (cid, rid, f.filename, stored, caption, size, now_iso(), current_user()),
    )
    db.commit()
    audit(cid, "ajout_capture_cre",
          f"Ligne {row['cle'] or row['operande']} : {f.filename}")
    return api_list_row_images(cid, rid)


@app.route("/api/rows/images/<int:img_id>")
def api_serve_row_image(img_id):
    db = get_db()
    img = db.execute("SELECT * FROM grid_row_images WHERE id=?", (img_id,)).fetchone()
    if img is None:
        abort(404)
    cdir = os.path.join(UPLOAD_DIR, str(img["campaign_id"]))
    return send_from_directory(cdir, img["stored_name"], as_attachment=False,
                               download_name=img["original_name"])


@app.route("/api/rows/images/<int:img_id>", methods=["DELETE"])
def api_delete_row_image(img_id):
    db = get_db()
    img = db.execute("SELECT * FROM grid_row_images WHERE id=?", (img_id,)).fetchone()
    if img is None:
        abort(404)
    path = os.path.join(UPLOAD_DIR, str(img["campaign_id"]), img["stored_name"])
    if os.path.exists(path):
        os.remove(path)
    db.execute("DELETE FROM grid_row_images WHERE id=?", (img_id,))
    db.commit()
    audit(img["campaign_id"], "suppression_capture_cre", img["original_name"])
    return jsonify({"deleted": img_id})


# ---------------------------------------------------------------------------
# Routes -- verification post-injection (EKDI / EA09 vs grille saisie)
# ---------------------------------------------------------------------------
def _norm_key(s):
    return re.sub(r"\s+", "", str(s or "")).upper()


@app.route("/api/campaigns/<int:cid>/compare", methods=["POST"])
def api_compare(cid):
    """Compare un extract (EKDI ou EA09) aux valeurs saisies dans la grille.

    L'appariement se fait sur (GrpValFix, Operande) quand la grille les renseigne,
    sinon sur l'Operande seul. On compare la 'Valeur de saisie' de l'extract a la
    valeur saisie dans la grille pour cette campagne. On retient pour chaque cle de
    l'extract la ligne effective a la date d'effet de la campagne (les operandes en
    _P au 01.07), conformement a la logique de la transaction EA09.
    """
    camp = get_campaign_or_404(cid)
    if "file" not in request.files:
        return jsonify({"error": "Un extract .xlsx est requis."}), 400
    kind = request.form.get("kind", "ekdi")
    try:
        df = load_ekdi(request.files["file"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Lecture impossible : verifier que le fichier est un extract .xlsx valide."}), 400

    ref_date = datetime.strptime(camp["effective_date"], "%Y-%m-%d").date()
    p_ref_date = date(ref_date.year, ref_date.month - 1, 1) if ref_date.month > 1 else date(ref_date.year - 1, 12, 1)
    effective = select_effective_rows(df, ref_date, p_ref_date)

    # Index des valeurs effectives : par (grp, operande) et par operande.
    by_pair = {}
    by_op = {}
    for (tarif, grp, op), info in effective.items():
        val = info["valeur"]
        if pd.isna(val):
            continue
        by_pair.setdefault((_norm_key(grp), _norm_key(op)), []).append(val)
        by_op.setdefault(_norm_key(op), []).append(val)

    db = get_db()
    rows = db.execute(
        "SELECT * FROM grid_rows WHERE campaign_id=? AND new_value IS NOT NULL ORDER BY sort_order",
        (cid,),
    ).fetchall()

    results = []
    counts = {"ok": 0, "diff": 0, "missing": 0, "ambiguous": 0}
    for r in rows:
        op = _norm_key(r["operande"])
        grp = _norm_key(r["grp"])
        saisie = r["new_value"]
        cands = by_pair.get((grp, op)) if grp else None
        match_on = "grp+operande"
        if not cands:
            cands = by_op.get(op)
            match_on = "operande"
        if not cands:
            status = "missing"
            extract_val = None
        elif len(set(round(v, 8) for v in cands)) > 1:
            status = "ambiguous"
            extract_val = None
        else:
            extract_val = cands[0]
            status = "ok" if abs(extract_val - saisie) <= 1e-9 else "diff"
        counts[status] += 1
        results.append({
            "section": r["section"], "tarif_label": r["tarif_label"], "grp": r["grp"],
            "operande": r["operande"], "cle": r["cle"], "saisie": saisie,
            "extract": extract_val, "status": status, "match_on": match_on,
        })

    audit(cid, "comparaison_extract", f"{FILE_KINDS.get(kind, kind)} : "
          f"{counts['ok']} OK, {counts['diff']} ecarts, {counts['missing']} absents, {counts['ambiguous']} ambigus")
    return jsonify({"kind": kind, "counts": counts, "results": results})


# ---------------------------------------------------------------------------
# Routes -- verifications metier (TMA / RTE)
# ---------------------------------------------------------------------------
@app.route("/api/campaigns/<int:cid>/verifications", methods=["POST"])
def api_add_verification(cid):
    get_campaign_or_404(cid)
    payload = request.get_json(silent=True) or {}
    role = (payload.get("role") or "").strip()
    verdict = (payload.get("verdict") or "").strip()
    comment = (payload.get("comment") or "").strip()
    if role not in {k for k, _ in VERIF_ROLES}:
        return jsonify({"error": "Role de verification invalide."}), 400
    if verdict not in ("ok", "ko"):
        return jsonify({"error": "Verdict invalide (ok/ko)."}), 400
    db = get_db()
    db.execute(
        "INSERT INTO verifications (campaign_id, role, verdict, verifier, verifier_profile, comment, created_at) VALUES (?,?,?,?,?,?,?)",
        (cid, role, verdict, current_user(), current_profile(), comment, now_iso()),
    )
    db.commit()
    label = dict(VERIF_ROLES)[role]
    audit(cid, "verification", f"{label} : {verdict.upper()}" + (f" - {comment}" if comment else ""))
    row = get_campaign_or_404(cid)
    return jsonify(campaign_to_dict(row, db)), 201


# ---------------------------------------------------------------------------
# Routes -- journal d'audit
# ---------------------------------------------------------------------------
@app.route("/api/campaigns/<int:cid>/audit", methods=["GET"])
def api_get_audit(cid):
    get_campaign_or_404(cid)
    db = get_db()
    rows = db.execute(
        "SELECT user_name, user_profile, action, detail, created_at FROM audit_log WHERE campaign_id=? ORDER BY id DESC",
        (cid,),
    ).fetchall()
    return jsonify({"entries": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Routes -- reconciliation N/N-1 (brique 1 historique)
# ---------------------------------------------------------------------------
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
    return jsonify({
        "ref_date": ref_date.isoformat(), "p_ref_date": p_ref.isoformat(),
        "counts": counts, "results": results,
    })


@app.route("/api/export", methods=["POST"])
def api_export():
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
        buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name="ecarts_EKDI.xlsx",
    )


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9998, debug=False)
