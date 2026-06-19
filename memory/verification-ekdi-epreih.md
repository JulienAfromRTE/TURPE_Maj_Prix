---
name: verification-ekdi-epreih
description: Etape 3 verifie EKDI (lignes sans cle) + EPREIH (lignes avec cle de prix)
metadata:
  type: project
---

L'étape 3 du workflow campagne (`verif`) confronte la grille saisie à **deux** extracts SAP complémentaires, selon l'origine de la valeur :

- **EKDI** → lignes **sans** clé de prix. Appariement du plus précis au plus large en tokenisant `tarif_ekdi` et `grp` (plusieurs tarifs/groupes ValFix espacés par ligne) : (Tarif, GrpValFix, Opérande) → (Tarif, Opérande) → (GrpValFix, Opérande) → (Opérande). Valeur = `Valeur de saisie`. Cibler le tarif évite l'ambiguïté sur un dump SAP complet.
- **EPREIH** → lignes **avec** clé de prix. Appariement sur la clé de prix (= colonne `Prix` de l'EPREIH). Valeur = `Montant de prix`.

Dans les deux cas, sélection de la valeur effective à la date d'effet (décalage `_P` au 01.07). Code : `_compare_ekdi` / `_compare_epreih` / `load_epreih` / `effective_value` dans `app.py`, route `POST /api/campaigns/<cid>/compare` (champ `kind` ∈ `ekdi`,`epreih`).

Après le contrôle auto, le **chef de projet DSIT** valide humainement chaque valeur en base via une case à cocher (table `compare_validations`, route `POST …/compare/validate` 403 si profil ≠ DSIT, hydratation au re-compare avec flag `stale`, 5e feuille d'export *Verification base*, trace `grid_row_history` champ `verif_bdd`). La base SAP fait foi.

L'ancienne notion **EA09 a été retirée** partout (remplacée par EPREIH). Voir [[sap-reference-de-verite]].
