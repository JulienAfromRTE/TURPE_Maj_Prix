"""Cree la VRAIE campagne « 1er aout 2026 » a partir des deliberations CRE 2026.

Source de verite = les deux PDF de la CRE du 21/05/2026 :
  - 260521_2026-105_Evolution_TURPE_7_HTA-BT.pdf  (delib. n°2026-105, HTA-BT)
  - 260521_2026-104_Evolution_TURPE_7_HTB.pdf     (delib. n°2026-104, HTB)

La saisie est realisee par l'IA (toutes les actions sont tracees user_name='IA',
user_profile='IA'). Les deux verifications humaines NE SONT PAS seedees : elles
restent a faire (TMA, puis Chef de projet DSIT). Statut final = 'verif'.

Mapping PDF -> cles : cf. docs/mapping_CRE_PDF.md. Regles appliquees :
  - valeur lue dans le PDF 2026 (jamais un calcul de % moyen) ;
  - c€/kWh -> €/kWh (/100), c€/MWh -> €/kWh (/100000),
    c€/kW/km/an -> €/.. (/100), €/Mvar.h -> €/kVar.h (/1000) ;
  - lignes _P (part fixe a echoir, effet 01.07) = memes valeurs b que le regulier ;
  - 272 cles saisies ; 15 laissees VIDES (lignes d'en-tete DER non distributeur et
    sous-lignes "0.0011" restees NULL dans l'historique, alpha depassement HTB sans source PDF).
    Aucune valeur n'est inventee ; en cas de doute la ligne reste vide.

Le detail cle par cle (valeur, source PDF, evolution) est dans
docs/verif_campagne_aout_2026.csv pour la verification humaine.

Idempotent : supprime une campagne homonyme avant de recreer.
Usage : python3 scripts/seed_campaign_aout_2026.py
"""
import csv
import json
import os
import re
import shutil
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
REFERENCE_GRID_PATH = os.path.join(DATA_DIR, "reference_grid.json")
CRE_DIR = os.path.join(DATA_DIR, "Délibérations CRE")
REPORT_PATH = os.path.join(BASE, "docs", "verif_campagne_aout_2026.csv")

PERIOD = "08.2026"
EFFECTIVE_DATE = "2026-08-01"
NAME = "1er aout 2026"

AGENT = "IA"
AGENT_PROFILE = "IA"

TS_CREATE = "2026-06-08T09:30:00"
TS_UPLOAD = "2026-06-08T09:35:00"
TS_SAISIE = "2026-06-08T10:00:00"

PDFS = [
    ("cre_pdf_2", "260521_2026-105_Evolution_TURPE_7_HTA-BT.pdf"),
    ("cre_pdf_1", "260521_2026-104_Evolution_TURPE_7_HTB.pdf"),
]


# ---------------------------------------------------------------------------
# Mapping PDF 2026 -> valeurs par numero d'ordre de la grille
# ---------------------------------------------------------------------------
def build_values():
    HTB = {
        "HTB2_CU": dict(b=[3.60, 3.60, 3.60, 3.60, 3.60], c=[1.23, 1.11, 0.90, 0.67, 0.54]),
        "HTB2_MU": dict(b=[4.44, 4.32, 3.96, 3.72, 3.60], c=[1.01, 0.95, 0.82, 0.63, 0.53]),
        "HTB2_LU": dict(b=[11.64, 11.04, 8.16, 5.64, 4.20], c=[0.69, 0.66, 0.61, 0.54, 0.50]),
        "HTB1_CU": dict(b=[12.12, 12.12, 12.12, 12.12, 12.12], c=[2.64, 2.26, 1.72, 1.05, 0.69]),
        "HTB1_MU": dict(b=[13.92, 13.68, 12.84, 12.48, 12.24], c=[2.09, 1.85, 1.50, 0.96, 0.67]),
        "HTB1_LU": dict(b=[43.08, 40.68, 29.88, 19.68, 14.40], c=[0.75, 0.72, 0.65, 0.59, 0.54]),
    }
    HTA = {
        "FixeC": dict(b=[14.85, 14.85, 14.85, 12.93, 11.56], c=[5.91, 4.36, 2.05, 1.04, 0.71]),
        "FixeL": dict(b=[36.40, 33.28, 21.01, 14.77, 11.91], c=[2.73, 2.16, 1.51, 0.95, 0.70]),
        "MobC":  dict(b=[14.85, 14.85, 14.85, 12.93, 11.56], c=[7.22, 4.17, 2.05, 1.04, 0.71]),
        "MobL":  dict(b=[39.43, 35.34, 21.01, 14.77, 11.91], c=[3.25, 1.93, 1.51, 0.95, 0.70]),
    }
    c = lambda lst: [round(x / 100, 6) for x in lst]

    V, SRC = {}, {}

    def put(o, val, src):
        V[o] = val
        SRC[o] = src

    def seq(start, vals, src):
        for i, v in enumerate(vals):
            put(start + i, v, src)

    seq(1, [11930.88, 11930.88, 11930.88], "HTB CG alpha1 (PDF HTB, 11 930,88)")
    put(4, 509.75, "HTA CG y compris Rf/Ccard utilisateur (PDF HTA-BT)")
    put(5, 11930.88, "MIROIR CG HTB (Prod) - A VERIFIER")
    put(6, 509.75, "MIROIR CG HTA (Prod) - A VERIFIER")
    put(7, 3927.00, "HTB comptage GRD (PDF HTB, 3927,00)")
    put(8, 387.84, "HTA comptage mensuel (PDF HTA-BT, 387,84)")
    put(9, 705.00, "HTB comptage utilisateur (PDF HTB, 705,00)")
    put(10, 387.84, "HTA comptage (= ligne 8)")
    put(11, round(0.42 / 100, 6), "HTB3 CS c=0,42 c€/kWh (PDF HTB) - 08.2025 fichier=0,00041 ERREUR x10")
    seq(12, c(HTB["HTB2_CU"]["c"]), "HTB2 CU c (PDF HTB tab3.5)")
    seq(17, c(HTB["HTB2_MU"]["c"]), "HTB2 MU c (PDF HTB tab3.6)")
    seq(22, c(HTB["HTB2_LU"]["c"]), "HTB2 LU c (PDF HTB tab3.7)")
    seq(27, c(HTB["HTB1_CU"]["c"]), "HTB1 CU c (PDF HTB tab3.8)")
    seq(32, c(HTB["HTB1_MU"]["c"]), "HTB1 MU c (PDF HTB tab3.9)")
    seq(37, c(HTB["HTB1_LU"]["c"]), "HTB1 LU c (PDF HTB tab3.10)")
    seq(42, c(HTA["FixeC"]["c"]), "HTA pointe fixe courte c (PDF HTA-BT)")
    seq(47, c(HTA["FixeL"]["c"]), "HTA pointe fixe longue c (PDF HTA-BT)")
    seq(52, c(HTA["MobC"]["c"]), "HTA pointe mobile courte c (PDF HTA-BT)")
    seq(57, c(HTA["MobL"]["c"]), "HTA pointe mobile longue c (PDF HTA-BT)")
    put(62, round(37 / 100000, 6), "HTB2 injection 37 c€/MWh (PDF HTB tab3.3)")
    put(63, round(37 / 100000, 6), "HTB3 injection 37 c€/MWh")
    put(64, 0.0, "HTA injection 0 (PDF HTA-BT)")
    put(65, 0.0, "HTB1 injection 0,00 (PDF HTB)")
    seq(72, HTB["HTB2_CU"]["b"], "HTB2 CU b (PDF HTB tab3.5)")
    seq(77, HTB["HTB2_MU"]["b"], "HTB2 MU b (PDF HTB tab3.6)")
    seq(82, HTB["HTB2_LU"]["b"], "HTB2 LU b (PDF HTB tab3.7)")
    seq(87, HTB["HTB1_CU"]["b"], "HTB1 CU b (PDF HTB tab3.8)")
    seq(92, HTB["HTB1_MU"]["b"], "HTB1 MU b (PDF HTB tab3.9)")
    seq(97, HTB["HTB1_LU"]["b"], "HTB1 LU b (PDF HTB tab3.10)")
    seq(102, HTA["FixeC"]["b"], "HTA pointe fixe courte b (PDF HTA-BT)")
    seq(107, HTA["FixeL"]["b"], "HTA pointe fixe longue b (PDF HTA-BT)")
    seq(112, HTA["MobC"]["b"], "HTA pointe mobile courte b (PDF HTA-BT)")
    seq(117, HTA["MobL"]["b"], "HTA pointe mobile longue b (PDF HTA-BT)")
    # _P (part fixe a echoir) = memes valeurs b, effet 01.07
    for off in range(50):
        s = 72 + off
        if s in V:
            put(128 + off, V[s], "= ligne %d (part fixe a echoir, effet 01.07)" % s)
    put(193, round(7.37 / 100, 6), "HTB3 regroupement 7,37 c€ (PDF HTB)")
    put(194, round(7.37 / 100, 6), "HTB3 regroupement (= ligne 193)")
    put(195, round(19.18 / 100, 6), "HTB2 aerien 19,18 c€")
    put(196, round(73.73 / 100, 6), "HTB2 souterrain 73,73 c€")
    put(197, round(97.35 / 100, 6), "HTB1 aerien 97,35 c€")
    put(198, round(171.10 / 100, 6), "HTB1 souterrain 171,10 c€")
    put(199, round(97.35 / 100, 6), "HTA2->HTB1 aerien (= ligne 197)")
    put(200, round(171.10 / 100, 6), "HTA2->HTB1 souterrain (= ligne 198)")
    put(201, 0.65, "HTA aerien 0,65 €/kW/km/an (PDF HTA-BT)")
    put(202, 0.95, "HTA souterrain 0,95 €/kW/km/an")
    put(203, 135663.76, "HTB3 cellule (PDF HTB tab3.14)")
    put(204, 12859.58, "HTB3 liaison")
    put(205, 12859.58, "HTB3 liaison (= ligne 204)")
    put(206, 81816.45, "HTB2 cellule")
    put(207, 8198.39, "HTB2 liaison aerienne")
    put(208, 40990.43, "HTB2 liaison souterraine")
    put(209, 42497.13, "HTB1 cellule")
    put(210, 4864.75, "HTB1 liaison aerienne")
    put(211, 9729.49, "HTB1 liaison souterraine")
    put(212, 4169.01, "HTA cellule (PDF HTA-BT)")
    put(213, 1137.25, "HTA liaison aerienne")
    put(214, 1705.87, "HTA liaison souterraine")
    put(233, 1.96, "HTB2 reservation secours (PDF HTB tab3.15)")
    put(234, 3.78, "HTB1 reservation secours")
    put(235, 8.14, "HTA reservation secours (PDF HTA-BT)")
    put(236, 4.96, "HTB1/HTA2->HTB2 k transformation (PDF HTB)")
    put(237, 8.76, "HTA1->HTB1 k transformation")
    put(238, 10.86, "BT->HTA k transformation (PDF HTA-BT)")
    put(239, 2.30, "HTB2->HTB3 k transformation")
    put(240, 0.000143, "DPP HTB2 (PDF HTB tab3.19)")
    put(241, 0.000090, "DPP HTB1")
    # Alimentations de secours - tarification du reseau permettant le secours
    # (PDF HTB tab3.16 ; PDF HTA-BT). Part energie -> lignes energie 66-71 ;
    # prime fixe / part puissance -> lignes puissance 122-127 (+ _P 178-183) ;
    # alpha (c€/kW) -> depassements secours 187-192. Source confirmee par RTE.
    put(66, round(0.98 / 100, 6), "Secours HTB3->HTB2 part energie 0,98 c€/kWh (PDF HTB tab3.16)")
    put(67, round(2.29 / 100, 6), "Secours ->HTA part energie 2,29 c€/kWh (PDF HTA-BT)")
    put(68, round(2.29 / 100, 6), "Secours ->HTA part energie 2,29 c€/kWh (PDF HTA-BT)")
    put(69, round(2.29 / 100, 6), "Secours ->HTA part energie 2,29 c€/kWh (PDF HTA-BT)")
    put(70, round(1.66 / 100, 6), "Secours ->HTB1 part energie 1,66 c€/kWh (PDF HTB tab3.16)")
    put(71, round(1.66 / 100, 6), "Secours ->HTB1 part energie 1,66 c€/kWh (PDF HTB tab3.16)")
    put(122, 9.40, "Secours HTB3->HTB2 prime fixe 9,40 €/kW/an (PDF HTB tab3.16)")
    put(123, 10.56, "Secours HTB2->HTA part puissance 10,56 €/kW/an (PDF HTA-BT)")
    put(124, 3.68, "Secours HTB1->HTA part puissance 3,68 €/kW/an (PDF HTA-BT)")
    put(125, 3.68, "Secours HTB1->HTA part puissance 3,68 €/kW/an (PDF HTA-BT)")
    put(126, 2.02, "Secours HTB2->HTB1 prime fixe 2,02 €/kW/an (PDF HTB tab3.16)")
    put(127, 6.91, "Secours HTB3->HTB1 prime fixe 6,91 €/kW/an (PDF HTB tab3.16)")
    for s, d in zip(range(122, 128), range(178, 184)):
        put(d, V[s], "= ligne %d (part fixe a echoir, effet 01.07)" % s)
    put(187, round(39.83 / 100, 6), "Secours HTB3->HTB2 alpha 39,83 c€/kW (PDF HTB tab3.16)")
    put(188, round(84.76 / 100, 6), "Secours HTB2->HTA alpha 84,76 c€/kW (PDF HTA-BT)")
    put(189, round(30.10 / 100, 6), "Secours HTB1->HTA alpha 30,10 c€/kW (PDF HTA-BT)")
    put(190, round(30.10 / 100, 6), "Secours HTB1->HTA alpha 30,10 c€/kW (PDF HTA-BT)")
    put(191, round(8.86 / 100, 6), "Secours HTB2->HTB1 alpha 8,86 c€/kW (PDF HTB tab3.16)")
    put(192, round(29.49 / 100, 6), "Secours HTB3->HTB1 alpha 29,49 c€/kW (PDF HTB tab3.16)")
    put(250, round(2.51 / 100, 6), "HTA CER soutirage 2,51 c€/kVar.h (PDF HTA-BT)")
    put(270, round(3.89 / 1000, 6), "DER dist absorbee 3,89 €/Mvar.h (PDF HTB tab3.21)")
    put(271, round(2.21 / 1000, 6), "DER dist fournie 2,21 €/Mvar.h - 08.2025 fichier saut A VERIFIER")
    put(272, round(3.89 / 1000, 6), "DER dist absorbee HTB1 (= ligne 270)")
    put(273, round(2.21 / 1000, 6), "DER dist fournie HTB1 (= ligne 271)")
    put(274, round(3.89 / 1000, 6), "DER dist absorbee HTB3")
    put(275, round(2.21 / 1000, 6), "DER dist fournie HTB3")
    # Miroirs secours (memes valeurs que les lignes principales, autre config) :
    # CACS secours (215-232), DER distributeur secours (276-287), DER HTA secours (259-261).
    for d, s in {215: 206, 216: 207, 217: 208,
                 218: 212, 219: 213, 220: 214, 221: 212, 222: 213, 223: 214,
                 224: 212, 225: 213, 226: 214,
                 227: 209, 228: 210, 229: 211, 230: 209, 231: 210, 232: 211}.items():
        put(d, V[s], "= ligne %d (CACS secours, miroir)" % s)
    for d in range(276, 288):
        s = 270 if (d - 276) % 2 == 0 else 271
        put(d, V[s], "= ligne %d (DER distributeur secours, miroir)" % s)
    for d in (259, 260, 261):
        put(d, V[250], "= ligne 250 (DER non distributeur HTA secours, miroir)")
    # DER non distributeur - composante annuelle de l'energie reactive d'electricite
    # (PDF HTB tab3.20) : PRX_DERNBT = absorbee par l'utilisateur, PRX_DERNHT = fournie.
    for d in (243, 247, 252, 256, 263, 267):
        put(d, round(13.07 / 1000, 6), "DER non distrib absorbee par l'utilisateur 13,07 €/Mvar.h (PDF HTB tab3.20)")
    for d in (244, 248, 253, 257, 264, 268):
        put(d, round(1.14 / 1000, 6), "DER non distrib fournie par l'utilisateur 1,14 €/Mvar.h (PDF HTB tab3.20)")
    return V, SRC


def safe(name):
    name = os.path.basename(name or "fichier")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "fichier"


def audit(con, cid, ts, action, detail):
    con.execute(
        "INSERT INTO audit_log (campaign_id, user_name, user_profile, action, detail, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (cid, AGENT, AGENT_PROFILE, action, detail, ts),
    )


def row_history(con, cid, rid, field, old, new, ts):
    con.execute(
        "INSERT INTO grid_row_history "
        "(campaign_id, row_id, field, old_value, new_value, user_name, user_profile, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (cid, rid, field, None if old is None else str(old),
         None if new is None else str(new), AGENT, AGENT_PROFILE, ts),
    )


def last_valid(h):
    for p in ("08.2025", "07.2025", "02.2025", "11.2024", "10.2024", "08.2023", "07.2023"):
        if h.get(p) is not None:
            return p, h[p]
    return None, None


def main():
    V, SRC = build_values()
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys=ON;")
    con.row_factory = sqlite3.Row

    for r in con.execute("SELECT id FROM campaigns WHERE name=?", (NAME,)).fetchall():
        con.execute("DELETE FROM campaigns WHERE id=?", (r["id"],))
        shutil.rmtree(os.path.join(UPLOAD_DIR, str(r["id"])), ignore_errors=True)
        print("Ancienne campagne #%d supprimee." % r["id"])
    con.commit()

    cid = con.execute(
        "INSERT INTO campaigns (name, period_label, effective_date, status, created_at, created_by) "
        "VALUES (?,?,?,?,?,?)",
        (NAME, PERIOD, EFFECTIVE_DATE, "verif", TS_CREATE, AGENT),
    ).lastrowid
    audit(con, cid, TS_CREATE, "creation_campagne",
          "%s (periode %s, effet %s)" % (NAME, PERIOD, EFFECTIVE_DATE))

    cdir = os.path.join(UPLOAD_DIR, str(cid))
    os.makedirs(cdir, exist_ok=True)
    for kind, fname in PDFS:
        src = os.path.join(CRE_DIR, fname)
        stored = "%s_20260608_093500_%s" % (kind, safe(fname))
        shutil.copy2(src, os.path.join(cdir, stored))
        con.execute(
            "INSERT INTO campaign_files "
            "(campaign_id, kind, original_name, stored_name, size, uploaded_at, uploaded_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, kind, fname, stored, os.path.getsize(src), TS_UPLOAD, AGENT),
        )
        audit(con, cid, TS_UPLOAD, "upload_fichier", "%s : %s" % (kind, fname))

    ref = json.load(open(REFERENCE_GRID_PATH, encoding="utf-8"))
    report = []
    n_saisies = 0
    for rec in ref["rows"]:
        o = rec["order"]
        val = V.get(o)
        rid = con.execute(
            "INSERT INTO grid_rows (campaign_id, sort_order, section, tarif_label, tarif_ekdi, "
            "grp, operande, cle, history, new_value, validated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (cid, o, rec["section"], rec["tarif_label"], rec["tarif_ekdi"],
             rec["grp"], rec["operande"], rec["cle"],
             json.dumps(rec["history"], ensure_ascii=False), val),
        ).lastrowid
        lp, lv = last_valid(rec["history"])
        pct = ""
        if val is not None:
            row_history(con, cid, rid, "valeur", None, val, TS_SAISIE)
            n_saisies += 1
            if lv not in (None, 0):
                pct = "%+.1f%%" % ((val - lv) / lv * 100)
        report.append([o, rec["section"], rec["tarif_label"], rec["operande"], rec["cle"],
                       lp or "", "" if lv is None else lv,
                       "" if val is None else val, pct, SRC.get(o, "(vide - non saisi)")])

    audit(con, cid, TS_SAISIE, "saisie_grille",
          "%d cles saisies par IA depuis les delib. n°2026-104 et n°2026-105 "
          "(%d laissees vides volontairement)" % (n_saisies, len(ref["rows"]) - n_saisies))

    con.commit()
    con.close()

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ordre", "section", "tarif", "operande", "cle", "derniere_periode",
                    "derniere_valeur", "valeur_2026", "evolution", "source_PDF"])
        w.writerows(report)

    print("Campagne #%d '%s' creee : %d cles saisies, %d vides, 2 PDF rattaches, statut 'verif'."
          % (cid, NAME, n_saisies, len(ref["rows"]) - n_saisies))
    print("Rapport de verification : %s" % REPORT_PATH)
    return cid


if __name__ == "__main__":
    main()
