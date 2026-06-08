# Mapping délibérations CRE (PDF) → clés de prix de l'outil

Ce document décrit **comment sont structurés les PDF de délibération de la CRE** et
**comment chaque valeur d'un tableau PDF se rattache à une clé de prix** de l'outil
(triplet SAP `Tarif / GrpValFix / Opérande` + libellé « clé de prix » du fichier de
suivi). C'est la base de la future **Brique 2 — Extraction PDF CRE** : la « table de
correspondance figée » mentionnée dans [CLAUDE.md](../CLAUDE.md).

Établi à partir des 5 délibérations de `data/Délibérations CRE/` et de l'onglet
*1. Extraits Délibération CRE* (235 screenshots) + *2. Mise à jour et vérif PROD*
(valeurs saisies) du fichier `MAJ_Tarifaire__Aout_2025_23072025.xlsx`.

---

## 1. Deux PDF par campagne : HTA-BT et HTB

Une mise à jour TURPE = **deux délibérations** publiées le même jour :

| PDF | Délibération | Domaines couverts | Pages d'annexe tarifaire |
|---|---|---|---|
| `…Evolution_TURPE_6_HTA-BT.pdf` | n°2025-08 | HTA, BT > 36 kVA, BT ≤ 36 kVA | annexe à partir de la p.5 |
| `…Evolution_TURPE_6_HTB.pdf` | n°2025-09 | HTB1, HTB2, HTB3 | annexe à partir de la p.4 |

Dans l'outil ils sont stockés respectivement sous `cre_pdf_2` (« Délibération CRE HTA »)
et `cre_pdf_1` (« Délibération CRE HTB ») — cf. `FILE_KINDS` dans [app.py](../app.py).

---

## 2. Anatomie d'un PDF

Structure constante d'une année sur l'autre :

1. **Pages de contexte / décision** (≈ p.0 à 4) : prose réglementaire, formule
   d'indexation, paramètres `R_f`, `C_card`, coefficient `k`. **Aucune table à extraire**
   (`extract_tables()` renvoie 0 table).
2. **Annexe** : une **section par composante tarifaire**, chacune introduite par un
   titre en clair (« *Composante annuelle de gestion (CG)* », « *Composante de soutirage*
   … »), suivie d'un ou plusieurs **tableaux texte** (pas des images → extractibles avec
   `pdfplumber` / `camelot`).

Chaque tableau croise généralement :
- en **lignes** : un **domaine de tension** (HTA / BT > 36 / BT ≤ 36, ou HTB1/2/3) et/ou
  une **plage temporelle** (postes horosaisonniers `i = 1…5`, `i = 1…8`…) ;
- en **colonnes** : la ou les **valeurs** (€/an, €/kVA/an, €/kW/an, c€/kWh…), parfois
  dédoublées (« contrat conclu par l'utilisateur » / « par le fournisseur »).

---

## 3. Principe du mapping

La **clé d'identification** d'une valeur dans le PDF est le triplet :

> **(section composante, tarif / domaine de tension, poste horosaisonnier ou sous-libellé)**

Ce triplet se résout vers une **clé de prix** de l'outil — un enregistrement de
[`data/reference_grid.json`](../data/reference_grid.json) :
`{ section, tarif_label, tarif_ekdi, grp, operande, cle, history }`.

Le rapprochement n'est donc **pas** un parsing littéral : c'est une **table figée**
section CRE → section outil, puis ligne/colonne du tableau → `operande` / `cle`. Cette
table change peu d'une année à l'autre ; seules les **valeurs** changent.

### Correspondance des sections

| Titre de section dans le PDF CRE | Section dans l'outil (`reference_grid.json`) |
|---|---|
| Composante annuelle de gestion (CG) | `Composante de gestion` |
| Composante annuelle de comptage | `Composante de Comptage` |
| Composantes annuelles de soutirage (CS) | `Composante de Soutirage PV` (part variable) |
| … part puissance / part fixe | `Composante de Soutirage PF` |
| … part fixe à échoir (préavis) | `Composante de Soutirage Part fixe à échoir` |
| Composante mensuelle des dépassements de puissance | `Dépassement puissance` |
| Composante de regroupement | `Composante regroupement` |
| Composante annuelle des soutirages complémentaires (CACS) | `CACS` |
| Réservation de puissance / alimentations de secours | `Réservation de puissance` |
| Composante de transformation | `Composante de transformation` |
| Composante des dépassements ponctuels programmés | `DPP` |
| Composante annuelle de l'énergie réactive (CER) | `DER non distributeur` / `DER distributeur` |

---

## 4. Subtilités de mapping à respecter

1. **Unités c€/kWh → €/kWh.** Les coefficients d'énergie sont publiés en **c€/kWh** dans
   le PDF mais stockés en **€/kWh** dans l'outil : **division par 100**.
   *Ex. : `c₁ = 8,05` c€/kWh → `0,0805` €/kWh.*
2. **Décalage `_P` (part fixe à échoir).** Les opérandes terminant par `_P` prennent
   effet au **01.07** (un mois avant le 01.08 standard) — déjà géré par l'app via le
   suffixe.
3. **Valeurs dédoublées utilisateur / fournisseur.** Certains tableaux de gestion
   donnent deux colonnes (contrat conclu par l'utilisateur vs par le fournisseur) ;
   seule la colonne pertinente est reprise selon la clé.
4. **Gestion « y compris R_f et C_card ».** La composante de gestion injectée est celle
   **incluant** `R_f` et `C_card` (≠ « hors R_f et C_card »). *Ex. HTA = 504,84 €/an,
   pas 262,17.*
5. **Pas d'application uniforme du % moyen.** Le PDF donne des valeurs absolues ; la
   hausse moyenne (+3,04 % HTA-BT, +3,34 % HTB en 08.2026) masque des composantes qui
   **baissent**. Toujours reprendre la valeur du tableau, jamais un calcul de %.
6. **Alerte ±20 %.** L'outil signale tout écart > ±20 % vs la période précédente : c'est
   un garde-fou de saisie, pas une règle métier.

---

## 5. Exemples vérifiés (délibérations de février 2025)

Valeurs lues dans les PDF n°2025-08 / n°2025-09 et retrouvées à l'identique dans la
colonne `02.2025` de la grille.

### a) Composante de gestion (PDF HTA-BT p.5, PDF HTB p.4)

| PDF — libellé | Valeur PDF | Section / tarif outil | `operande` | `cle` | Valeur outil |
|---|---|---|---|---|---|
| CG y compris R_f/C_card — **HTA** | 504,84 €/an | gestion / HTA | `PRX_FR_GST` | `HTA_FRGST` | 504,84 |
| Tableau 1 — CG **HTB** | 11 545,323 €/an | gestion / HTB1-3 | `PRX_FR_GST` | `HTB_FRGST` | 11 545,32 |

### b) Soutirage HTA 5 plages à pointe mobile — courte utilisation (PDF HTA-BT p.10)

Coefficients d'énergie `c₁…c₅` (c€/kWh dans le PDF, ÷100 dans l'outil) :

| PDF | Valeur PDF (c€/kWh) | `operande` | `cle` | Valeur outil (€/kWh) |
|---|---|---|---|---|
| `c₁` HP saison haute | 8,05 | `COEFF_C1` | `EAPTE_CUAM` | 0,0805 |
| `c₂` HC saison haute | 4,66 | `COEFF_C2` | `EAHPSHCUAM` | 0,0466 |
| `c₃` HP saison basse | 2,83 | `COEFF_C3` | `EAHCSH_CUA` | 0,0283 |
| `c₄` … | 0,82 | `COEFF_C4` | `EAHPSB_CUA` | 0,0082 |
| `c₅` … | 0,54 | `COEFF_C5` | `EAHCSB_CUA` | 0,0054 |

(Les coefficients de puissance `b₁…b₅` mappent symétriquement vers `COEFF_B1…B5`. La
clé de validation de [CLAUDE.md](../CLAUDE.md) `5CL_CAL01 / ZHTB1_CU / ZCOEF_B1` relève
du même schéma côté HTB.)

---

## 6. Couverture pour février 2025

275 des 287 clés de la grille reçoivent une valeur en 02.2025. Les 12 clés non servies
appartiennent toutes à la section `DER non distributeur` (composantes introduites
ultérieurement) — laissées vides, ce qui est le comportement correct.

---

## 7. Implémentation Brique 2 (piste)

Pour figer la table de correspondance de manière exploitable par l'extraction :
enrichir chaque enregistrement de `reference_grid.json` d'un bloc `cre_source`
décrivant **où** lire la valeur dans le PDF :

```json
"cre_source": {
  "pdf": "HTA-BT",
  "section": "Composante annuelle de gestion (CG)",
  "table_hint": "y compris Rf et Ccard",
  "row_label": "HTA",
  "col_label": "conclu par l'utilisateur incluant Ccard",
  "unit": "EUR_per_an",      // ou "cEUR_per_kWh" → ÷100
  "scale": 1
}
```

L'extraction `pdfplumber` localise alors la table par son titre de section, repère la
ligne via `row_label`, applique `scale` selon `unit`, et propose la valeur pour la clé —
à valider humainement (l'écriture SAP restant hors périmètre).
