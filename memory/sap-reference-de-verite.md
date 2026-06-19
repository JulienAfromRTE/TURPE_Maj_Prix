---
name: sap-reference-de-verite
description: SAP (EKDI/EPREIH) fait foi pour la verification ; le typo PX_HTB3EA de l'Excel Aout 2025
metadata:
  type: project
---

Pour la vérification post-injection, **la base SAP est la référence de vérité**, pas le fichier Excel de suivi ni le tableau. Si l'outil signale un écart entre la grille (issue de l'Excel) et SAP, c'est la grille qui est suspecte.

Cas connu : dans `data/MAJ Tarifaire _Aout 2025_23072025.xlsx` (onglet *2.Mise à jour et vérif PROD*, ligne 21), la clé `PX_HTB3EA / COEFF_C_S / HTB3` a un **typo en colonne 08.2025 : `0,00041`** (un zéro de trop) au lieu de `0,0041`. SAP porte la bonne valeur `0,0041` (corrigée en production). La campagne 12 (seedée depuis cet Excel) a hérité du typo — c'est le seul écart du test de réconciliation 08.2025.

**Why:** valide que l'étape 3 [[verification-ekdi-epreih]] catche les vraies erreurs sans faux positifs.
**How to apply:** lors d'un test de non-régression, comparer les valeurs 08.2025 de la campagne 12 aux extracts SAP → tout doit matcher sauf ce typo connu.
