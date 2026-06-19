"""TURPE_Maj_Prix - Portail de gestion des mises a jour tarifaires TURPE.

Permet de suivre, au fil de l'eau, chaque campagne de mise a jour des prix (la MAJ
annuelle du 1er aout, mais aussi les MAJ intermediaires). Chaque campagne suit un
workflow trace de bout en bout :

  1. Upload des deliberations CRE (PDF), stockees sur le serveur et retelechargeables.
  2. Saisie en ligne de la grille tarifaire (instanciee avec l'historique des MAJ
     precedentes), exportable en .xlsx pour injection dans SAP.
  3. Verification post-injection : comparaison des extracts EKDI (lignes sans cle
     de prix) et EPREIH (lignes avec cle de prix) avec les valeurs saisies.

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
APP_VERSION = "2.7.0"
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

# Colonnes EPREIH (libelles SAP exacts) -- verification des lignes portant une
# "Cle de prix". L'EPREIH est indexe par la colonne "Prix" (= cle de prix de la
# grille) ; la valeur est dans "Montant de prix".
COL_PRX_KEY = "Prix"
COL_PRX_VALUE = "Montant de prix"
COL_PRX_FROM = "Valide du"
REQUIRED_EPREIH_COLS = [COL_PRX_KEY, COL_PRX_VALUE, COL_PRX_FROM]

ABERRATION_THRESHOLD = 0.20  # 20 % -> alerte valeur potentiellement aberrante

# Etapes du workflow d'une campagne (ordre = progression). L'etape "scripts"
# (generation des scripts SQL de MAJ EKDI/EPREIH) s'intercale entre la saisie de
# la grille et la verification post-injection. Le stepper du front et l'ensemble
# des statuts valides (api_set_status) sont derives de cette liste : ajouter une
# etape ici suffit a l'exposer partout.
WORKFLOW_STEPS = [
    ("pdf", "Deliberations CRE"),
    ("grid", "Saisie grille tarifaire"),
    ("scripts", "Procedure de saisie SAP"),
    ("verif", "Verification EKDI / EPREIH"),
]
# Roles de verification metier
VERIF_ROLES = [
    ("tma", "TMA"),
    ("rte", "Chef de projet DSIT"),
]
# Validation ligne a ligne : deux niveaux distincts (TMA puis chef de projet DSIT).
# Une case ne peut etre cochee que par le profil correspondant (controle serveur).
VALIDATION_ROLES = {
    "tma": "TMA",
    "rte": "Chef de projet DSIT",
}
# Profils utilisateur saisis a l'ouverture, traces sur chaque action (audit).
USER_PROFILES = ["TMA", "Chef de projet DSIT"]
# Types de fichiers stockes par campagne
FILE_KINDS = {
    "cre_pdf_1": "Deliberation CRE HTB",
    "cre_pdf_2": "Deliberation CRE HTA",
    "ekdi": "Extract EKDI",
    "epreih": "Extract EPREIH",
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

        -- Commentaires libres au niveau de la campagne (sous la grille) : fil de
        -- notes horodatees et nominatives, non rattachees a une ligne precise.
        -- Traces et exportes dans le .xlsx (feuille dediee).
        CREATE TABLE IF NOT EXISTS campaign_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            comment TEXT NOT NULL,
            user_name TEXT NOT NULL,
            user_profile TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
        );

        -- Validation humaine du controle de l'etape 3 : le chef de projet DSIT
        -- confirme, ligne par ligne, la valeur trouvee en base (EKDI/EPREIH). On
        -- fige la valeur attendue (grille) et la valeur en base au moment de la
        -- validation, pour un controle auditable. Une validation par ligne.
        CREATE TABLE IF NOT EXISTS compare_validations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            row_id INTEGER NOT NULL,
            kind TEXT NOT NULL,            -- ekdi | epreih
            expected REAL,                 -- valeur attendue (grille, etape 2)
            found REAL,                    -- valeur en base (extract SAP)
            status TEXT,                   -- ok | diff | missing | ambiguous au moment de la validation
            validated_by TEXT NOT NULL,
            validated_profile TEXT,
            validated_at TEXT NOT NULL,
            UNIQUE(campaign_id, row_id),
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_rowhist_row ON grid_row_history(row_id);
        CREATE INDEX IF NOT EXISTS idx_rowimg_row ON grid_row_images(row_id);
        CREATE INDEX IF NOT EXISTS idx_rowcmt_row ON grid_row_comments(row_id);
        CREATE INDEX IF NOT EXISTS idx_campcmt_camp ON campaign_comments(campaign_id);
        CREATE INDEX IF NOT EXISTS idx_cmpval_camp ON compare_validations(campaign_id);
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
    # Migration : deux niveaux de validation distincts (TMA / chef de projet DSIT),
    # chacun avec son validateur, profil et horodatage (v2.4). On reprend l'ancienne
    # validation unique (`validated`) vers le niveau correspondant au profil du
    # validateur (a defaut : chef de projet DSIT = niveau rte).
    grid_cols = {r[1] for r in con.execute("PRAGMA table_info(grid_rows)").fetchall()}
    added_dual = "validated_rte" not in grid_cols
    for col, ddl in (
        ("validated_tma", "INTEGER NOT NULL DEFAULT 0"),
        ("validated_tma_by", "TEXT"),
        ("validated_tma_profile", "TEXT"),
        ("validated_tma_at", "TEXT"),
        ("validated_rte", "INTEGER NOT NULL DEFAULT 0"),
        ("validated_rte_by", "TEXT"),
        ("validated_rte_profile", "TEXT"),
        ("validated_rte_at", "TEXT"),
    ):
        if col not in grid_cols:
            con.execute(f"ALTER TABLE grid_rows ADD COLUMN {col} {ddl}")
    if added_dual:
        con.execute(
            "UPDATE grid_rows SET validated_tma=1, validated_tma_by=validated_by, "
            "validated_tma_profile=validated_profile, validated_tma_at=validated_at "
            "WHERE validated=1 AND validated_profile='TMA'"
        )
        con.execute(
            "UPDATE grid_rows SET validated_rte=1, validated_rte_by=validated_by, "
            "validated_rte_profile=validated_profile, validated_rte_at=validated_at "
            "WHERE validated=1 AND (validated_profile IS NULL OR validated_profile<>'TMA')"
        )
    # Migration : marqueur "present Excel" sur grid_rows, coche par le chef de
    # projet DSIT pour signaler les cles presentes dans le fichier de suivi Excel
    # (qui, quel profil, quand). Trace comme la validation, mais distinct d'elle.
    grid_cols = {r[1] for r in con.execute("PRAGMA table_info(grid_rows)").fetchall()}
    for col, ddl in (
        ("excel_present", "INTEGER NOT NULL DEFAULT 0"),
        ("excel_present_by", "TEXT"),
        ("excel_present_profile", "TEXT"),
        ("excel_present_at", "TEXT"),
    ):
        if col not in grid_cols:
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
    # Migration v2.6 : Ordre de Transport SAP (un OT par campagne, saisi
    # manuellement depuis Ocas / SAP ChaRM) repris sur la procedure de saisie.
    if "sap_ot" not in camp_cols:
        con.execute("ALTER TABLE campaigns ADD COLUMN sap_ot TEXT")
    con.commit()
    _migrate_split_grp(con)
    con.close()


# Colonnes de grid_rows recopiees telles quelles sur les lignes filles lors de
# l'eclatement d'un GrpValFix agrege (tout sauf id et grp).
_SPLIT_COPY_COLS = (
    "campaign_id", "sort_order", "section", "tarif_label", "tarif_ekdi",
    "operande", "cle", "history", "new_value",
    "validated", "validated_by", "validated_at", "validated_profile", "comment",
    "validated_tma", "validated_tma_by", "validated_tma_profile", "validated_tma_at",
    "validated_rte", "validated_rte_by", "validated_rte_profile", "validated_rte_at",
    "excel_present", "excel_present_by", "excel_present_profile", "excel_present_at",
)


def _migrate_split_grp(con):
    """Migration v2.7 : grille atomique (une ligne = un GrpValFix).

    Les lignes dont le champ `grp` agrege plusieurs groupes ValFix (p.ex.
    "ZHTB2_CU ZHTB2_LU ZHTB2_MU" = 3 lignes SAP au meme prix) sont eclatees en
    une ligne par GrpValFix, pour coller au fichier Excel de suivi et permettre
    une saisie / validation / verification ligne par ligne.

    NON DESTRUCTIF : la ligne existante est conservee (id, valeur, validations,
    present Excel, historique, captures et commentaires intacts) et recoit le 1er
    token ; les tokens suivants donnent de NOUVELLES lignes copiant les memes
    attributs. Idempotent (ne fait rien s'il ne reste aucun grp multi-valeurs) et
    limite aux campagnes NON cloturees (les campagnes audit/cloturees sont figees).
    """
    prev_factory = con.row_factory
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT gr.* FROM grid_rows gr JOIN campaigns c ON c.id = gr.campaign_id "
        "WHERE instr(trim(gr.grp), ' ') > 0 AND c.status <> 'cloture'"
    ).fetchall()
    if not rows:
        con.row_factory = prev_factory
        return
    ts = now_iso()
    cols_sql = ",".join(_SPLIT_COPY_COLS)
    ph_sql = ",".join("?" for _ in _SPLIT_COPY_COLS)
    added_per_camp = {}
    for r in rows:
        tokens = [t for t in re.split(r"\s+", (r["grp"] or "").strip()) if t]
        if len(tokens) < 2:
            continue
        # 1) la ligne existante garde le 1er GrpValFix (aucune donnee effacee).
        con.execute("UPDATE grid_rows SET grp=? WHERE id=?", (tokens[0], r["id"]))
        con.execute(
            "INSERT INTO grid_row_history (campaign_id, row_id, field, old_value, "
            "new_value, user_name, user_profile, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (r["campaign_id"], r["id"], "grp_split", r["grp"], tokens[0],
             "Migration v2.7", "systeme", ts),
        )
        # 2) une nouvelle ligne par GrpValFix suivant, copiant tous les attributs.
        for tok in tokens[1:]:
            new_id = con.execute(
                f"INSERT INTO grid_rows ({cols_sql}, grp) VALUES ({ph_sql}, ?)",
                [r[c] for c in _SPLIT_COPY_COLS] + [tok],
            ).lastrowid
            con.execute(
                "INSERT INTO grid_row_history (campaign_id, row_id, field, old_value, "
                "new_value, user_name, user_profile, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (r["campaign_id"], new_id, "creation", None,
                 "Eclatement GrpValFix " + tok + " (depuis ligne #" + str(r["id"]) + ")",
                 "Migration v2.7", "systeme", ts),
            )
        added_per_camp[r["campaign_id"]] = added_per_camp.get(r["campaign_id"], 0) + len(tokens) - 1
    for cid, n_added in added_per_camp.items():
        con.execute(
            "INSERT INTO audit_log (campaign_id, user_name, user_profile, action, "
            "detail, created_at) VALUES (?,?,?,?,?,?)",
            (cid, "Migration v2.7", "systeme", "migration_split_grp",
             "Eclatement des GrpValFix agreges : " + str(n_added) + " ligne(s) ajoutee(s)", ts),
        )
    con.commit()
    con.row_factory = prev_factory


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


def load_epreih(file_storage):
    """Charge un export EPREIH en DataFrame normalise (cles de prix).

    Cle = colonne "Prix" (correspond a la "Cle de prix" de la grille), valeur =
    "Montant de prix", date de debut = "Valide du". Leve ValueError si colonnes
    manquantes. Les noms peuvent etre entoures d'apostrophes cote SAP -> on les
    nettoie.
    """
    raw = file_storage.read()
    df = pd.read_excel(io.BytesIO(raw), sheet_name=0)
    df.columns = [str(c).strip().strip("'").strip() for c in df.columns]
    missing = [c for c in REQUIRED_EPREIH_COLS if c not in df.columns]
    if missing:
        raise ValueError("Colonnes manquantes dans l'export EPREIH : " + ", ".join(missing))
    df = df[REQUIRED_EPREIH_COLS].copy()
    df[COL_PRX_KEY] = df[COL_PRX_KEY].fillna("").astype(str).str.strip()
    df["valide_du"] = df[COL_PRX_FROM].apply(normalize_date)
    df["valeur"] = pd.to_numeric(df[COL_PRX_VALUE], errors="coerce")
    return df


def effective_value(periods, target):
    """Derniere valeur dont la date de debut <= date cible (logique SAP).

    `periods` : liste de (valide_du, valeur). Renvoie None si aucune periode
    n'est en vigueur a `target`.
    """
    best = None
    for vd, val in periods:
        if vd is not None and vd <= target and (best is None or vd > best[0]):
            best = (vd, val)
    return best[1] if best else None


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
        "sap_ot": row["sap_ot"],
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
            "validated_tma": bool(r["validated_tma"]),
            "validated_tma_by": r["validated_tma_by"], "validated_tma_at": r["validated_tma_at"],
            "validated_rte": bool(r["validated_rte"]),
            "validated_rte_by": r["validated_rte_by"], "validated_rte_at": r["validated_rte_at"],
            "excel_present": bool(r["excel_present"]),
            "excel_present_by": r["excel_present_by"], "excel_present_at": r["excel_present_at"],
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


@app.route("/api/campaigns/<int:cid>/rows", methods=["POST"])
def api_add_row(cid):
    """Ajoute manuellement une ligne (cle de prix) a la grille d'une campagne.

    Utile quand une nouvelle composante apparait en cours de campagne sans figurer
    dans la grille de reference. La creation est tracee (audit + historique fin).
    """
    get_campaign_or_404(cid)
    payload = request.get_json(silent=True) or {}
    operande = (payload.get("operande") or "").strip()
    if not operande:
        return jsonify({"error": "L'operande est obligatoire."}), 400
    fields = {
        "section": (payload.get("section") or "").strip(),
        "tarif_label": (payload.get("tarif_label") or "").strip(),
        "tarif_ekdi": (payload.get("tarif_ekdi") or "").strip(),
        "grp": (payload.get("grp") or "").strip(),
        "operande": operande,
        "cle": (payload.get("cle") or "").strip(),
    }
    db = get_db()
    next_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM grid_rows WHERE campaign_id=?", (cid,)
    ).fetchone()["n"]
    cur = db.execute(
        """INSERT INTO grid_rows (campaign_id, sort_order, section, tarif_label,
           tarif_ekdi, grp, operande, cle, history, new_value)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (cid, next_order, fields["section"], fields["tarif_label"], fields["tarif_ekdi"],
         fields["grp"], operande, fields["cle"], "{}", None),
    )
    rid = cur.lastrowid
    record_row_history(db, cid, rid, "creation", None, fields["cle"] or operande)
    db.commit()
    audit(cid, "ajout_ligne",
          f"Ligne #{rid} ajoutee ({fields['tarif_label'] or '-'} / {operande})")
    return jsonify({"id": rid, "sort_order": next_order}), 201


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


def _campaign_comments(db, cid):
    """Liste des commentaires de campagne (sous la grille), du plus recent au plus ancien."""
    rows = db.execute(
        """SELECT id, comment, user_name, user_profile, created_at
           FROM campaign_comments WHERE campaign_id=? ORDER BY id DESC""",
        (cid,),
    ).fetchall()
    return [dict(r) for r in rows]


@app.route("/api/campaigns/<int:cid>/grid-comments", methods=["GET"])
def api_list_campaign_comments(cid):
    get_campaign_or_404(cid)
    return jsonify({"comments": _campaign_comments(get_db(), cid)})


@app.route("/api/campaigns/<int:cid>/grid-comments", methods=["POST"])
def api_add_campaign_comment(cid):
    """Ajoute un commentaire libre au niveau de la campagne (sous la grille). Append-only, nominatif."""
    get_campaign_or_404(cid)
    payload = request.get_json(silent=True) or {}
    text = (payload.get("comment") or "").strip()
    if not text:
        return jsonify({"error": "Commentaire vide."}), 400
    db = get_db()
    db.execute(
        """INSERT INTO campaign_comments (campaign_id, comment, user_name, user_profile, created_at)
           VALUES (?,?,?,?,?)""",
        (cid, text, current_user(), current_profile(), now_iso()),
    )
    db.commit()
    audit(cid, "commentaire_campagne", "Commentaire ajoute")
    comments = _campaign_comments(db, cid)
    return jsonify({"comments": comments, "n_comments": len(comments)})


@app.route("/api/campaigns/<int:cid>/grid-comments/<int:comment_id>", methods=["DELETE"])
def api_delete_campaign_comment(cid, comment_id):
    get_campaign_or_404(cid)
    db = get_db()
    row = db.execute(
        "SELECT id FROM campaign_comments WHERE id=? AND campaign_id=?",
        (comment_id, cid),
    ).fetchone()
    if row is None:
        return jsonify({"error": "Commentaire introuvable."}), 404
    db.execute("DELETE FROM campaign_comments WHERE id=? AND campaign_id=?", (comment_id, cid))
    db.commit()
    audit(cid, "commentaire_campagne", "Commentaire supprime")
    comments = _campaign_comments(db, cid)
    return jsonify({"comments": comments, "n_comments": len(comments)})


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
               + [f"Valeur {period}",
                  "Present Excel", "Present Excel par", "Present Excel le",
                  "Validee TMA", "Validee TMA par", "Validee TMA le",
                  "Validee DSIT", "Validee DSIT par", "Validee DSIT le",
                  "Nb captures CRE", "Nb commentaires"])
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
        rec["Present Excel"] = "Oui" if r["excel_present"] else "Non"
        rec["Present Excel par"] = r["excel_present_by"]
        rec["Present Excel le"] = (r["excel_present_at"] or "").replace("T", " ")
        rec["Validee TMA"] = "Oui" if r["validated_tma"] else "Non"
        rec["Validee TMA par"] = r["validated_tma_by"]
        rec["Validee TMA le"] = (r["validated_tma_at"] or "").replace("T", " ")
        rec["Validee DSIT"] = "Oui" if r["validated_rte"] else "Non"
        rec["Validee DSIT par"] = r["validated_rte_by"]
        rec["Validee DSIT le"] = (r["validated_rte_at"] or "").replace("T", " ")
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

    # Feuille 5 : validation humaine du controle en base (EKDI/EPREIH) -- audit.
    cv_rows = db.execute(
        """SELECT v.kind, v.expected, v.found, v.status, v.validated_by, v.validated_profile,
                  v.validated_at, g.section, g.tarif_label, g.operande, g.cle
           FROM compare_validations v JOIN grid_rows g ON g.id = v.row_id
           WHERE v.campaign_id=? ORDER BY v.id""",
        (cid,),
    ).fetchall()
    cv_df = pd.DataFrame(
        [{
            "Source": (v["kind"] or "").upper(),
            "Section": v["section"], "Tarif": v["tarif_label"], "Operande": v["operande"],
            "Cle de prix": v["cle"],
            "Valeur attendue (grille)": v["expected"], "Valeur en base (SAP)": v["found"],
            "Statut controle": v["status"], "Validee par": v["validated_by"],
            "Profil": v["validated_profile"], "Validee le": (v["validated_at"] or "").replace("T", " "),
        } for v in cv_rows],
        columns=["Source", "Section", "Tarif", "Operande", "Cle de prix",
                 "Valeur attendue (grille)", "Valeur en base (SAP)", "Statut controle",
                 "Validee par", "Profil", "Validee le"],
    )

    # Feuille 6 : commentaires libres de campagne (sous la grille) -- audit.
    ccmt_rows = db.execute(
        """SELECT created_at, user_name, user_profile, comment
           FROM campaign_comments WHERE campaign_id=? ORDER BY id""",
        (cid,),
    ).fetchall()
    ccmt_df = pd.DataFrame(
        [{
            "Date": c["created_at"].replace("T", " "), "Auteur": c["user_name"],
            "Profil": c["user_profile"], "Commentaire": c["comment"],
        } for c in ccmt_rows],
        columns=["Date", "Auteur", "Profil", "Commentaire"],
    )

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="Grille TURPE")
        hist_df.to_excel(xw, index=False, sheet_name="Historique modifications")
        img_df.to_excel(xw, index=False, sheet_name="Captures CRE")
        cmt_df.to_excel(xw, index=False, sheet_name="Commentaires")
        cv_df.to_excel(xw, index=False, sheet_name="Verification base")
        ccmt_df.to_excel(xw, index=False, sheet_name="Commentaires campagne")
    buf.seek(0)
    audit(cid, "export_grille",
          f"Export .xlsx grille periode {period} (+ historique {len(hist_rows)} traces, "
          f"{len(img_rows)} captures, {len(cmt_rows)} commentaires, "
          f"{len(cv_rows)} validations base, {len(ccmt_rows)} commentaires campagne)")
    return send_file(
        buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=f"grille_TURPE_{_safe_name(period)}.xlsx",
    )


# ---------------------------------------------------------------------------
# Etape intermediaire -- generation des procedures de saisie SAP
# ---------------------------------------------------------------------------
# A partir des valeurs *saisies* dans la grille (etape 2), on genere deux
# PROCEDURES DE SAISIE SAP (et non du SQL : les tarifs sont mis a jour dans
# l'IHM SAP, pas en base), une par table :
#   - EKDI   : lignes SANS cle de prix  -> cle (Tarif, GrpValFix, Operande) ;
#   - EPREIH : lignes AVEC cle de prix  -> cle = colonne "Prix".
# Chaque modification porte sa date d'effet (decalage _P au 01.07, sinon date
# d'effet de la campagne) et l'Ordre de Transport (OT) a affecter.
#
# CONTRAINTE METIER (retour TMA) : toute modification dans SAP exige un Ordre de
# Transport (OT), recupere dans Ocas (l'outil SAP ChaRM de suivi des OT). SAP le
# reclame a CHAQUE modification -> la procedure rappelle l'OT a affecter sur
# chaque etape. L'OT est saisi manuellement au niveau de la campagne (un OT par
# campagne), pas d'integration Ocas (l'application reste en lecture seule et
# n'ecrit jamais dans SAP, cf. CLAUDE.md).
SAP_TABLE_EKDI = "EKDI"
SAP_TABLE_EPREIH = "EPREIH"


def _fmt_num_fr(value):
    """Valeur numerique en notation francaise (virgule), sans bruit flottant.

    Entiers rendus sans decimale ; le reste via repr() (plus courte chaine qui se
    relit a l'identique en Python 3), point remplace par une virgule (ex: 11,76).
    """
    if value is None:
        return ""
    v = float(value)
    s = str(int(v)) if v.is_integer() else repr(v)
    return s.replace(".", ",")


def _row_effective_date(operande, ref_date, p_ref_date):
    """Date de validite a appliquer a la ligne : 01.07 pour les operandes en _P,
    sinon la date d'effet de la campagne."""
    return p_ref_date if str(operande or "").endswith("_P") else ref_date


def _ot_label(ot):
    """Libelle de l'OT a afficher (ou marqueur explicite si absent)."""
    return ot or "<OT manquant : a recuperer dans Ocas (SAP ChaRM)>"


def _build_ekdi_procedure(camp, rows, ref_date, p_ref_date, ot):
    """Construit la procedure de saisie EKDI (lignes sans cle de prix).

    Depuis la v2.7 la grille est atomique : une ligne = un GrpValFix (les lignes
    qui en agregaient plusieurs, p.ex. ZHTB2_CU/LU/MU, sont desormais eclatees a
    la source dans reference_grid.json). On emet donc une etape de saisie par
    ligne. Le produit cartesien (Tarif x GrpValFix) est conserve comme filet de
    securite : il reste correct pour les campagnes anterieures (dont les
    grid_rows agregent encore grp) et un tarif_ekdi multi-valeurs (variantes
    d'ecriture SAP, p.ex. "5CL_CAL01 Z5CL_CAL01").
    """
    steps, n_step = [], 0
    for r in rows:
        op = (r["operande"] or "").strip()
        if not op:
            continue
        val = _fmt_num_fr(r["new_value"])
        eff = _row_effective_date(op, ref_date, p_ref_date).strftime("%d.%m.%Y")
        tarifs = [t for t in re.split(r"\s+", (r["tarif_ekdi"] or "").strip()) if t] or [""]
        grps = [grp for grp in re.split(r"\s+", (r["grp"] or "").strip()) if grp] or [""]
        for tarif in tarifs:
            for grp in grps:
                n_step += 1
                steps.append("\n".join([
                    "[{}] {}  --  {}".format(n_step, r["section"] or "-", r["tarif_label"] or "-"),
                    "     Tarif        : {}".format(tarif or "-"),
                    "     GrpValFix    : {}".format(grp or "-"),
                    "     Operande     : {}".format(op),
                    "     Valide du    : {}".format(eff),
                    "     Valeur       : {}".format(val),
                    "     OT a saisir  : {}".format(_ot_label(ot)),
                ]))
    body = ("\n\n".join(steps) if steps
            else "(Aucune ligne sans cle de prix saisie a l'etape 2.)") + "\n"
    return _procedure_header(camp, SAP_TABLE_EKDI, "lignes SANS cle de prix "
                             "(cle = Tarif / GrpValFix / Operande)", len(rows), n_step, ot) \
        + body, len(rows), n_step


def _build_epreih_procedure(camp, rows, ref_date, p_ref_date, ot):
    """Construit la procedure de saisie EPREIH (lignes avec cle de prix).

    Appariement sur la cle de prix = colonne "Prix" ; valeur = "Montant de prix".
    """
    steps, n_step = [], 0
    for r in rows:
        cle = (r["cle"] or "").strip()
        if not cle:
            continue
        n_step += 1
        val = _fmt_num_fr(r["new_value"])
        eff = _row_effective_date(r["operande"], ref_date, p_ref_date).strftime("%d.%m.%Y")
        steps.append("\n".join([
            "[{}] {}  --  {}".format(n_step, r["section"] or "-", r["tarif_label"] or "-"),
            "     Cle de prix  : {}".format(cle),
            "     Valide du    : {}".format(eff),
            "     Montant      : {}".format(val),
            "     OT a saisir  : {}".format(_ot_label(ot)),
        ]))
    body = ("\n\n".join(steps) if steps
            else "(Aucune ligne avec cle de prix saisie a l'etape 2.)") + "\n"
    return _procedure_header(camp, SAP_TABLE_EPREIH, "lignes AVEC cle de prix "
                             "(cle = colonne Prix)", len(rows), n_step, ot) \
        + body, len(rows), n_step


def _procedure_header(camp, table, scope, n_rows, n_step, ot):
    """En-tete d'une procedure (contexte campagne + OT + rappel metier)."""
    return (
        "============================================================\n"
        "Procedure de saisie SAP -- table {table}\n"
        "Campagne : {name} (periode {period}, date d'effet {eff})\n"
        "Ordre de Transport (OT) : {ot}\n"
        "Transaction SAP : (a preciser selon la composante)\n"
        "Perimetre : {scope}\n"
        "{n_rows} ligne(s) saisie(s) -> {n_step} modification(s) SAP\n"
        "Genere le {ts} par {app} v{ver}\n"
        "------------------------------------------------------------\n"
        "IMPORTANT : a chaque modification, SAP reclame un Ordre de Transport.\n"
        "  Affecter l'OT ci-dessus (recupere dans Ocas / SAP ChaRM).\n"
        "  Saisie MANUELLE dans SAP : l'application n'ecrit jamais en base.\n"
        "============================================================\n\n"
    ).format(
        table=table, name=camp["name"], period=camp["period_label"],
        eff=camp["effective_date"], ot=_ot_label(ot), scope=scope,
        n_rows=n_rows, n_step=n_step, ts=now_iso().replace("T", " "),
        app=APP_NAME, ver=APP_VERSION,
    )


def _generate_procedures(cid):
    """Genere les deux procedures (EKDI, EPREIH) pour une campagne.

    Retourne {ot, ekdi: {text, n_rows, n_steps}, epreih: {...}} a partir des
    seules lignes de grille ayant une valeur saisie (new_value non nul).
    """
    camp = get_campaign_or_404(cid)
    ot = (camp["sap_ot"] or "").strip()
    ref_date = datetime.strptime(camp["effective_date"], "%Y-%m-%d").date()
    p_ref_date = (date(ref_date.year, ref_date.month - 1, 1)
                  if ref_date.month > 1 else date(ref_date.year - 1, 12, 1))
    db = get_db()
    rows = db.execute(
        "SELECT * FROM grid_rows WHERE campaign_id=? AND new_value IS NOT NULL "
        "ORDER BY sort_order", (cid,)).fetchall()
    ekdi_rows = [r for r in rows if not (r["cle"] or "").strip()]
    epreih_rows = [r for r in rows if (r["cle"] or "").strip()]
    e_txt, e_nrows, e_nsteps = _build_ekdi_procedure(camp, ekdi_rows, ref_date, p_ref_date, ot)
    p_txt, p_nrows, p_nsteps = _build_epreih_procedure(camp, epreih_rows, ref_date, p_ref_date, ot)
    return {
        "ot": ot,
        "ekdi": {"text": e_txt, "n_rows": e_nrows, "n_steps": e_nsteps},
        "epreih": {"text": p_txt, "n_rows": p_nrows, "n_steps": p_nsteps},
    }


@app.route("/api/campaigns/<int:cid>/ot", methods=["POST"])
def api_set_ot(cid):
    """Enregistre l'Ordre de Transport (OT) de la campagne (saisi depuis Ocas).

    Un seul OT par campagne, repris sur toutes les etapes de la procedure SAP.
    Toute modification est tracee dans le journal d'audit.
    """
    row = get_campaign_or_404(cid)
    payload = request.get_json(silent=True) or {}
    ot = (payload.get("ot") or "").strip()
    old = (row["sap_ot"] or "").strip()
    db = get_db()
    db.execute("UPDATE campaigns SET sap_ot=? WHERE id=?", (ot or None, cid))
    db.commit()
    if old != ot:
        audit(cid, "saisie_ot", f"OT : {old or '(vide)'} -> {ot or '(vide)'}")
    return jsonify({"sap_ot": ot})


@app.route("/api/campaigns/<int:cid>/scripts", methods=["GET"])
def api_get_scripts(cid):
    """Apercu JSON des deux procedures de saisie SAP (pour l'etape 'scripts')."""
    return jsonify(_generate_procedures(cid))


@app.route("/api/campaigns/<int:cid>/scripts/download", methods=["GET"])
def api_download_script(cid):
    """Telechargement d'une procedure .txt (kind = ekdi | epreih), trace dans l'audit."""
    camp = get_campaign_or_404(cid)
    kind = (request.args.get("kind") or "").strip().lower()
    if kind not in ("ekdi", "epreih"):
        return jsonify({"error": "Type de procedure invalide (ekdi/epreih)."}), 400
    block = _generate_procedures(cid)[kind]
    audit(cid, "generation_procedure_sap",
          f"{kind.upper()} : {block['n_rows']} ligne(s) -> {block['n_steps']} modification(s) SAP")
    buf = io.BytesIO(block["text"].encode("utf-8"))
    fname = f"Procedure_SAP_{kind.upper()}_{_safe_name(camp['period_label'])}.txt"
    return send_file(buf, mimetype="text/plain; charset=utf-8", as_attachment=True,
                     download_name=fname)


# ---------------------------------------------------------------------------
# Routes -- validation ligne a ligne (deux niveaux : TMA / chef de projet DSIT)
# ---------------------------------------------------------------------------
@app.route("/api/campaigns/<int:cid>/grid/validate", methods=["POST"])
def api_validate_row(cid):
    get_campaign_or_404(cid)
    payload = request.get_json(silent=True) or {}
    rid = payload.get("id")
    role = (payload.get("role") or "rte").strip()
    validated = bool(payload.get("validated"))
    if role not in VALIDATION_ROLES:
        return jsonify({"error": "Niveau de validation invalide."}), 400
    # Seul le profil correspondant peut (de)valider a ce niveau (tracabilite audit).
    required = VALIDATION_ROLES[role]
    if current_profile() != required:
        return jsonify({"error": f"Seul le profil « {required} » peut cocher cette validation."}), 403
    col = "validated_" + role
    by_col, prof_col, at_col = col + "_by", col + "_profile", col + "_at"
    db = get_db()
    row = db.execute(
        f"SELECT {col} AS v FROM grid_rows WHERE id=? AND campaign_id=?", (rid, cid)
    ).fetchone()
    if row is None:
        return jsonify({"error": "Ligne introuvable."}), 404
    if bool(row["v"]) == validated:
        return jsonify({"role": role, "validated": validated, "by": None, "at": None})
    who = current_user()
    prof = current_profile()
    when = now_iso() if validated else None
    db.execute(
        f"UPDATE grid_rows SET {col}=?, {by_col}=?, {prof_col}=?, {at_col}=? WHERE id=? AND campaign_id=?",
        (1 if validated else 0, who if validated else None,
         prof if validated else None, when, rid, cid),
    )
    record_row_history(db, cid, rid, "validation_" + role,
                       "validee" if row["v"] else "non validee",
                       "validee" if validated else "non validee")
    db.commit()
    audit(cid, "validation_ligne",
          f"Ligne #{rid} {required} {'validee' if validated else 'devalidee'}")
    return jsonify({"role": role, "validated": validated,
                    "by": who if validated else None, "at": when})


@app.route("/api/campaigns/<int:cid>/grid/excel-present", methods=["POST"])
def api_excel_present(cid):
    """Marque (ou demarque) une ligne comme presente dans le fichier de suivi
    Excel. Reserve au chef de projet DSIT, trace pour l'audit."""
    get_campaign_or_404(cid)
    payload = request.get_json(silent=True) or {}
    rid = payload.get("id")
    present = bool(payload.get("present"))
    required = VALIDATION_ROLES["rte"]
    if current_profile() != required:
        return jsonify({"error": f"Seul le profil « {required} » peut cocher « Présent Excel »."}), 403
    db = get_db()
    row = db.execute(
        "SELECT excel_present AS v FROM grid_rows WHERE id=? AND campaign_id=?", (rid, cid)
    ).fetchone()
    if row is None:
        return jsonify({"error": "Ligne introuvable."}), 404
    if bool(row["v"]) == present:
        return jsonify({"present": present, "by": None, "at": None})
    who = current_user()
    prof = current_profile()
    when = now_iso() if present else None
    db.execute(
        "UPDATE grid_rows SET excel_present=?, excel_present_by=?, excel_present_profile=?, "
        "excel_present_at=? WHERE id=? AND campaign_id=?",
        (1 if present else 0, who if present else None, prof if present else None, when, rid, cid),
    )
    record_row_history(db, cid, rid, "excel_present",
                       "present" if row["v"] else "absent",
                       "present" if present else "absent")
    db.commit()
    audit(cid, "present_excel",
          f"Ligne #{rid} marquee {'presente' if present else 'absente'} du fichier Excel")
    return jsonify({"present": present, "by": who if present else None, "at": when})


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
# Routes -- verification post-injection (EKDI / EPREIH vs grille saisie)
# ---------------------------------------------------------------------------
def _norm_key(s):
    return re.sub(r"\s+", "", str(s or "")).upper()


def _compare_ekdi(file_storage, rows, ref_date, p_ref_date):
    """Verifie les lignes SANS cle de prix contre un extract EKDI.

    Appariement du plus precis au plus large, en exploitant le ou les tarifs que
    la grille renseigne (champ `tarif_ekdi`, plusieurs tarifs/groupes ValFix
    espaces) : (Tarif, GrpValFix, Operande) -> (Tarif, Operande) ->
    (GrpValFix, Operande) -> (Operande). Sur un dump SAP complet (tous tarifs),
    cibler le tarif evite l'ambiguite entre lignes partageant le meme operande.
    On retient pour chaque cle la ligne effective a la date d'effet (operandes en
    _P au 01.07).
    """
    df = load_ekdi(file_storage)
    effective = select_effective_rows(df, ref_date, p_ref_date)
    by_tgo, by_to, by_pair, by_op = {}, {}, {}, {}
    for (tarif, grp, op), info in effective.items():
        val = info["valeur"]
        if pd.isna(val):
            continue
        t, g, o = _norm_key(tarif), _norm_key(grp), _norm_key(op)
        by_tgo.setdefault((t, g, o), []).append(val)
        by_to.setdefault((t, o), []).append(val)
        by_pair.setdefault((g, o), []).append(val)
        by_op.setdefault(o, []).append(val)

    results = []
    counts = {"ok": 0, "diff": 0, "missing": 0, "ambiguous": 0}
    for r in rows:
        op = _norm_key(r["operande"])
        # Grille atomique depuis la v2.7 (une ligne = un GrpValFix), mais on
        # tokenise toujours : tarif_ekdi peut agreger des variantes d'ecriture
        # (p.ex. "5CL_CAL01 Z5CL_CAL01") et les campagnes anterieures peuvent
        # encore avoir un grp multi-valeurs.
        tokens = [_norm_key(t) for t in re.split(r"\s+", r["tarif_ekdi"] or "") if t.strip()]
        grps = [_norm_key(g) for g in re.split(r"\s+", r["grp"] or "") if g.strip()]
        saisie = r["new_value"]

        cands, match_on = None, "operande"
        if tokens and grps:
            agg = [v for t in tokens for g in grps for v in by_tgo.get((t, g, op), [])]
            if agg:
                cands, match_on = agg, "tarif+grp+operande"
        if not cands and tokens:
            agg = [v for t in tokens for v in by_to.get((t, op), [])]
            if agg:
                cands, match_on = agg, "tarif+operande"
        if not cands and grps:
            agg = [v for g in grps for v in by_pair.get((g, op), [])]
            if agg:
                cands, match_on = agg, "grp+operande"
        if not cands:
            cands = by_op.get(op)
            match_on = "operande"

        if not cands:
            status, extract_val = "missing", None
        elif len(set(round(v, 8) for v in cands)) > 1:
            status, extract_val = "ambiguous", None
        else:
            extract_val = cands[0]
            status = "ok" if abs(extract_val - saisie) <= 1e-9 else "diff"
        counts[status] += 1
        results.append({
            "row_id": r["id"],
            "section": r["section"], "tarif_label": r["tarif_label"], "grp": r["grp"],
            "operande": r["operande"], "cle": r["cle"], "saisie": saisie,
            "extract": extract_val, "status": status, "match_on": match_on,
        })
    return counts, results


def _compare_epreih(file_storage, rows, ref_date, p_ref_date):
    """Verifie les lignes AVEC cle de prix contre un extract EPREIH.

    Appariement sur la cle de prix (colonne 'Prix' de l'EPREIH). On compare le
    'Montant de prix' effectif a la date d'effet de la campagne (cle en _P au
    01.07) a la valeur saisie dans la grille.
    """
    df = load_epreih(file_storage)
    periods = {}
    for _, rr in df.iterrows():
        key = _norm_key(rr[COL_PRX_KEY])
        vd, val = rr["valide_du"], rr["valeur"]
        if not key or vd is None or pd.isna(val):
            continue
        periods.setdefault(key, []).append((vd, val))

    results = []
    counts = {"ok": 0, "diff": 0, "missing": 0, "ambiguous": 0}
    for r in rows:
        saisie = r["new_value"]
        target = p_ref_date if str(r["operande"] or "").endswith("_P") else ref_date
        cand = periods.get(_norm_key(r["cle"]))
        extract_val = effective_value(cand, target) if cand else None
        if extract_val is None:
            status = "missing"
        else:
            status = "ok" if abs(extract_val - saisie) <= 1e-9 else "diff"
        counts[status] += 1
        results.append({
            "row_id": r["id"],
            "section": r["section"], "tarif_label": r["tarif_label"], "grp": r["grp"],
            "operande": r["operande"], "cle": r["cle"], "saisie": saisie,
            "extract": extract_val, "status": status, "match_on": "cle de prix",
        })
    return counts, results


@app.route("/api/campaigns/<int:cid>/compare", methods=["POST"])
def api_compare(cid):
    """Compare un extract aux valeurs saisies dans la grille.

    Deux sources complementaires, selon l'origine de la valeur dans le fichier de
    suivi Excel :
      - EKDI   -> lignes SANS cle de prix (appariement sur le triplet) ;
      - EPREIH -> lignes AVEC cle de prix (appariement sur la cle de prix).
    On retient la valeur effective a la date d'effet de la campagne (operandes en
    _P au 01.07).
    """
    camp = get_campaign_or_404(cid)
    if "file" not in request.files:
        return jsonify({"error": "Un extract .xlsx est requis."}), 400
    kind = request.form.get("kind", "ekdi")
    if kind not in ("ekdi", "epreih"):
        return jsonify({"error": "Type d'extract invalide (ekdi/epreih)."}), 400

    ref_date = datetime.strptime(camp["effective_date"], "%Y-%m-%d").date()
    p_ref_date = date(ref_date.year, ref_date.month - 1, 1) if ref_date.month > 1 else date(ref_date.year - 1, 12, 1)

    db = get_db()
    if kind == "epreih":
        rows = db.execute(
            "SELECT * FROM grid_rows WHERE campaign_id=? AND new_value IS NOT NULL "
            "AND cle IS NOT NULL AND cle!='' ORDER BY sort_order", (cid,)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM grid_rows WHERE campaign_id=? AND new_value IS NOT NULL "
            "AND (cle IS NULL OR cle='') ORDER BY sort_order", (cid,)).fetchall()

    try:
        if kind == "epreih":
            counts, results = _compare_epreih(request.files["file"], rows, ref_date, p_ref_date)
        else:
            counts, results = _compare_ekdi(request.files["file"], rows, ref_date, p_ref_date)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Lecture impossible : verifier que le fichier est un extract .xlsx valide."}), 400

    _merge_compare_validations(db, cid, results)
    audit(cid, "comparaison_extract", f"{FILE_KINDS.get(kind, kind)} : "
          f"{counts['ok']} OK, {counts['diff']} ecarts, {counts['missing']} absents, {counts['ambiguous']} ambigus")
    return jsonify({"kind": kind, "counts": counts, "results": results})


def _merge_compare_validations(db, cid, results):
    """Hydrate chaque resultat avec sa validation humaine persistee (si elle existe).

    On expose aussi `stale` = la valeur en base a change depuis la validation
    (l'extract courant ne donne plus la valeur validee) -> a revalider.
    """
    vals = {
        v["row_id"]: v
        for v in db.execute(
            "SELECT row_id, found, validated_by, validated_profile, validated_at "
            "FROM compare_validations WHERE campaign_id=?", (cid,)).fetchall()
    }
    for r in results:
        v = vals.get(r["row_id"])
        if not v:
            r["validated"] = False
            continue
        cur = r.get("extract")
        stale = cur is None or v["found"] is None or abs(cur - v["found"]) > 1e-9
        r["validated"] = True
        r["validated_by"] = v["validated_by"]
        r["validated_profile"] = v["validated_profile"]
        r["validated_at"] = v["validated_at"]
        r["validated_found"] = v["found"]
        r["stale"] = stale


@app.route("/api/campaigns/<int:cid>/compare/validate", methods=["POST"])
def api_validate_compare(cid):
    """Validation humaine (chef de projet DSIT) d'une valeur trouvee en base.

    Confirme, ligne par ligne, la valeur de l'extract SAP face a la valeur de la
    grille. Trace dans `compare_validations`, le journal d'audit et l'historique
    fin de la ligne (champ `verif_bdd`).
    """
    get_campaign_or_404(cid)
    payload = request.get_json(silent=True) or {}
    rid = payload.get("row_id")
    kind = (payload.get("kind") or "").strip()
    validated = bool(payload.get("validated"))
    required = VALIDATION_ROLES["rte"]
    if current_profile() != required:
        return jsonify({"error": f"Seul le profil « {required} » peut valider la verification en base."}), 403
    db = get_db()
    row = db.execute("SELECT id, cle, operande FROM grid_rows WHERE id=? AND campaign_id=?",
                     (rid, cid)).fetchone()
    if row is None:
        return jsonify({"error": "Ligne introuvable."}), 404

    existing = db.execute(
        "SELECT id FROM compare_validations WHERE campaign_id=? AND row_id=?", (cid, rid)).fetchone()
    if not validated:
        if existing:
            db.execute("DELETE FROM compare_validations WHERE id=?", (existing["id"],))
            record_row_history(db, cid, rid, "verif_bdd", "validee", "non validee")
            db.commit()
            audit(cid, "validation_verif_bdd", f"Ligne #{rid} verification en base devalidee")
        return jsonify({"row_id": rid, "validated": False, "by": None, "at": None})

    expected = payload.get("expected")
    found = payload.get("found")
    status = (payload.get("status") or "").strip()
    who, prof, when = current_user(), current_profile(), now_iso()
    if existing:
        db.execute(
            "UPDATE compare_validations SET kind=?, expected=?, found=?, status=?, "
            "validated_by=?, validated_profile=?, validated_at=? WHERE id=?",
            (kind, expected, found, status, who, prof, when, existing["id"]))
    else:
        db.execute(
            "INSERT INTO compare_validations (campaign_id, row_id, kind, expected, found, "
            "status, validated_by, validated_profile, validated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, rid, kind, expected, found, status, who, prof, when))
    record_row_history(db, cid, rid, "verif_bdd", "non validee",
                       f"validee (base={found})")
    db.commit()
    audit(cid, "validation_verif_bdd",
          f"Ligne #{rid} valeur en base validee : attendue={expected} base={found} [{status}]")
    return jsonify({"row_id": rid, "validated": True, "by": who, "profile": prof, "at": when})


@app.route("/api/campaigns/<int:cid>/compare/validations", methods=["GET"])
def api_list_compare_validations(cid):
    get_campaign_or_404(cid)
    db = get_db()
    rows = db.execute(
        "SELECT row_id, kind, expected, found, status, validated_by, validated_profile, "
        "validated_at FROM compare_validations WHERE campaign_id=?", (cid,)).fetchall()
    return jsonify({"validations": [dict(r) for r in rows]})


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
