# TURPE_Maj_Prix — contexte et conventions

## Contexte métier

Projet RTE (Réseau de Transport d'Électricité). Chaque année au 1er août, RTE doit mettre à jour les prix du TURPE (Tarif d'Utilisation des Réseaux Publics d'Électricité).

**Process actuel (archaïque) :**
1. La CRE (Commission de Régulation de l'Énergie) publie des délibérations PDF avec les nouveaux tarifs
2. Les valeurs sont copiées manuellement depuis les PDF dans un fichier Excel de suivi (`MAJ Tarifaire _Aout AAAA_...xlsx`)
3. Le fichier Excel est validé, puis les valeurs sont injectées manuellement dans SAP
4. Vérification clé par clé dans SAP via la transaction EA09, et en extrayant la table EKDI

**Ce que cette application remplace en priorité :** la vérification post-injection (~150 clés vérifiées une par une dans EA09), qui est le maillon le plus chronophage et risqué.

## Architecture de solution (trois briques)

### Brique 1 — Réconciliation EKDI N / N-1 (livrée, `app.py`)
Compare deux exports de la table SAP EKDI (année N et N-1) et met en évidence les écarts clé par clé sur le triplet **(Tarif, GrpValFix, Opérande)**.

- Statuts : modifiée, nouvelle, supprimée, inchangée
- Évolution annuelle en % par clé
- Alerte automatique au-delà de ±20 % (valeur potentiellement aberrante)
- Gestion du décalage `_P` : les opérandes terminant par `_P` sont valides au 01.07 (un mois avant le 01.08 standard)
- Export Excel des écarts

### Brique 2 — Extraction PDF CRE (non livrée ; mapping documenté)
Extraction des tableaux des délibérations PDF CRE avec `pdfplumber` ou `camelot` (les tableaux sont du texte, pas des images). Nécessite une **table de correspondance figée** : libellé CRE → clé de prix SAP (cette table change peu d'une année sur l'autre et doit être calée manuellement une bonne fois).

**Mapping documenté : [`docs/mapping_CRE_PDF.md`](docs/mapping_CRE_PDF.md).** Anatomie des PDF (pages de contexte sans table, puis une *Annexe* avec une section par composante = tableaux texte extractibles), correspondance section CRE → section de `reference_grid.json`, et exemples vérifiés. **Une campagne = deux PDF** : HTA-BT et HTB. Subtilités de mapping : unités **c€/kWh → €/kWh (÷100)**, gestion *incluant* R_f/C_card (≠ hors), colonnes utilisateur/fournisseur, décalage `_P`. Piste d'implémentation : enrichir chaque clé de `reference_grid.json` d'un bloc `cre_source` (pdf, section, row/col label, unit, scale).

### Brique 3 — Génération de la colonne historisée (non livrée)
Ajout automatique de la nouvelle colonne `Valeur 08.AAAA` dans le fichier de suivi Excel, avec calcul des % d'évolution pour repérer les valeurs aberrantes.

## Portail multi-campagnes (livré, v2)

Au-dessus des briques, `app.py` expose un **portail de campagnes** (la MAJ annuelle du 1er août, mais aussi les MAJ intermédiaires). Chaque campagne suit un workflow tracé de bout en bout (`WORKFLOW_STEPS`) :

1. **Délibérations CRE** — upload des PDF, stockés sous `data/uploads/<campaign_id>/`, retéléchargeables.
2. **Saisie grille tarifaire** — grille instanciée depuis `data/reference_grid.json` (table `grid_rows`), avec l'historique des MAJ précédentes par clé, % d'évolution et alerte ±20 %. Export `.xlsx` pour injection SAP.
3. **Vérification post-injection** — comparaison d'un extract EKDI / EA09 avec les valeurs saisies (appariement sur `(GrpValFix, Opérande)` sinon `Opérande` seul).

Vérifications métier (TMA puis chef de projet DSIT, `VERIF_ROLES`) et journal d'audit global (`audit_log` : qui / quoi / quand). Front : `templates/campaign.html` + `static/campaign.js` (campagne), `templates/index.html` + `static/portal.js` (liste), `static/common.js` (identité utilisateur via `X-User-Name` + `X-User-Profile`).

## Traçabilité fine & audit (v2.1)

**Contexte : l'outil est audité (commissaires aux comptes) — tout doit être traçable.** Au-delà du journal global, la grille est tracée ligne par ligne :

- **Validation ligne par ligne** — colonne *Validée* (case à cocher) destinée au chef de projet DSIT. Colonnes `grid_rows.validated / validated_by / validated_profile / validated_at` (qui, quel profil, quand). Route `POST /api/campaigns/<cid>/grid/validate`.
- **Historique fin par ligne** — table `grid_row_history` (`field` = `valeur` | `validation`, `old_value`, `new_value`, `user_name`, `user_profile`, `created_at`). Le save ne trace **que les changements réels** (pas de bruit sur ré-enregistrement à l'identique). Route `GET /api/campaigns/<cid>/rows/<rid>/history`.
- **Captures CRE rattachées à la ligne** — images (PNG/JPG/GIF/WEBP) attachées à une **clé de prix** précise, à l'image de l'onglet 1 du fichier Excel de suivi. Table `grid_row_images`, fichiers `data/uploads/<cid>/rowimg_*`. Routes `…/rows/<rid>/images` (GET/POST), `/api/rows/images/<id>` (GET inline / DELETE). Front : clic sur une ligne → modale (captures + historique).
- **Masquage des années** dans la grille (chips période, défaut : toutes visibles) + filtres *À saisir uniquement* / *Non validées*. La grille déborde du conteneur central (`.step-panel.wide`) pour limiter le scroll horizontal.
- **Export `.xlsx` auditable à 3 feuilles** : *Grille TURPE* (+ colonnes Validée / Validée par / Profil validateur / Validée le / Nb captures), *Historique modifications* (toutes les traces ligne par ligne, avec colonne *Profil*), *Captures CRE* (inventaire des pièces rattachées).

**Migrations :** `init_db()` crée les tables manquantes (`CREATE TABLE IF NOT EXISTS`) et ajoute les colonnes de validation à `grid_rows` via `ALTER TABLE` si absentes (compatibilité bases antérieures à v2.1).

## Profil utilisateur (v2.2)

À l'ouverture, la modale d'identité demande le **nom** *et* le **profil** : `TMA` ou `Chef de projet DSIT` (`USER_PROFILES` dans `app.py`). Le profil est stocké côté front (`localStorage` clé `turpe_profile`) et envoyé sur chaque appel via l'en-tête `X-User-Profile` (côté serveur : `current_profile()`), à côté de `X-User-Name`.

**Le profil n'a qu'un rôle de traçabilité — il ne restreint aucune action** (un TMA peut tout faire, on enregistre juste qui, avec quel profil). Il est persisté dans `audit_log.user_profile`, `grid_row_history.user_profile`, `grid_rows.validated_profile` et `verifications.verifier_profile`, affiché dans l'UI (historique, journal, validations sous forme `Nom (Profil)`) et exporté.

**Renommage v2.2 :** l'affichage « Chef de projet RTE » devient partout « Chef de projet DSIT » ; la clé technique du rôle de vérification reste `rte` (inchangée pour ne rien casser : `VERIF_ROLES`, `portal.js`, `campaign.js`).

**Migrations v2.2 :** `init_db()` ajoute via `ALTER TABLE` les colonnes `user_profile` (`audit_log`, `grid_row_history`), `verifier_profile` (`verifications`) et `validated_profile` (`grid_rows`) si absentes.

## Décisions techniques structurantes

**L'écriture dans SAP est hors périmètre.** L'application travaille uniquement en lecture seule (extraction et vérification). Automatiser une écriture en prod sur des tarifs réglementaires représente un risque sans commune mesure avec le gain.

**Stack : Flask (Projectix) + pandas + openpyxl + SQLite WAL.**

## Conventions Projectix (obligatoires)

- Variables de métadonnées `APP_NAME`, `APP_VERSION`, `APP_DESCRIPTION` en tête de `app.py`
- Route `/health` (et alias `/api/health`) retournant `{"status": "ok", "app": ..., "version": ...}`
- Base SQLite dans `data/app.db`, mode WAL (`PRAGMA journal_mode=WAL`)
- Chemins JS en relatifs, `url_for()` dans les templates Jinja2
- Aucun identifiant JS accentué (remplacer `é`, `à`, etc. par des équivalents sans accent)
- `debug=False` en production

## Structure des données EKDI

Un export EKDI est un `.xlsx` avec ces colonnes SAP exactes :

| Colonne | Rôle |
|---|---|
| `Tarif` | Code tarif SAP (ex: `5CL_CAL01`) |
| `Grpe ValFix pr tarif` | Groupe de valeur fixe (ex: `ZHTB1_CU`) |
| `Opérande` | Opérande tarifaire (ex: `ZCOEF_B1`) |
| `Valide du` | Date de début de validité (série Excel, ex: `45870` = 01/08/2025) |
| `Fin de validité` | Date de fin (série Excel, `2958465` = 31/12/9999 = illimité) |
| `Valeur de saisie` | Valeur numérique du tarif |

**Clé d'identification d'une ligne SAP** = triplet `(Tarif, GrpValFix, Opérande)`.

**Conversion des séries de dates Excel :** base `1899-12-30`, `45870` → 01/08/2025, `45839` → 01/07/2025.

**Subtilité `_P` :** les opérandes terminant par `_P` (part fixe à échoir) entrent en vigueur au 01.07 et non au 01.08. L'app sélectionne automatiquement la bonne date selon le suffixe.

## Repères de validation métier

- Le TURPE 7 HTA-BT évolue de **+3,04 %** au 1er août 2026 (formule : IPC +0,39 % − X 0,35 % + k 3 %)
- Le HTB évolue de **+3,34 %**
- Ces chiffres sont des moyennes : **ne pas appliquer +3,04 % uniformément**, certaines composantes baissent
- La gestion HTA par exemple passe de 504,84 à une valeur inférieure
- Clé de test de validation : `5CL_CAL01 / ZHTB1_CU / ZCOEF_B1` → 10,44 (11.2024) → 11,76 (08.2025)

## Avertissement sur les exports partiels

Un export EKDI partiel (ex : 133 lignes) comparé à un export complet (536 lignes) génère ~136 "nouvelles" clés en faux positif. Ce n'est pas un bug : toujours utiliser deux exports **de périmètre identique** (même sélection de tarifs, deux années complètes) pour que la réconciliation soit exploitable.

## Campagne exemple « Février 2025 » (seedée)

Campagne de démonstration saisie proprement de bout en bout, recréable via deux scripts idempotents :

- [`scripts/seed_example_campaign.py`](scripts/seed_example_campaign.py) — crée la campagne `Fevrier 2025 (exemple)` (période `02.2025`, effet `2025-02-01`, statut `cloture`) : rattache les 2 PDF (HTA-BT `cre_pdf_2`, HTB `cre_pdf_1`), saisit **275/287 clés** (valeurs = colonne `02.2025` de la grille = valeurs lues dans les PDF ; les 12 vides sont des `DER non distributeur` introduites plus tard), trace chaque saisie et validation dans `grid_row_history`, valide toutes les lignes servies, ajoute les vérifications métier (TMA puis chef de projet), journal d'audit cohérent. Horodatages réalistes (janv–fév 2025).
- [`scripts/seed_example_row_images.py`](scripts/seed_example_row_images.py) — rattache aux **20 premières lignes** de vraies captures des tableaux CRE (crop du bbox `pdfplumber` + titre, 200 dpi) dans `grid_row_images`.

NB : la grille affiche `02.2025` à la fois en historique et comme valeur saisie (doublon assumé pour une campagne rétroactive).

## Fichiers de référence

Les délibérations CRE de test sont dans `data/Délibérations CRE/` (5 PDF : HTA-BT + HTB pour 2023, 2024, et fév. 2025). Les deux de fév. 2025 (n°2025-08 HTA-BT, n°2025-09 HTB) alimentent la campagne exemple.

Les vrais exports EKDI sont dans `data/Extracts EKDI/` :

- `EKDI_01112024.xlsx` — 132 lignes, extrait partiel, date d'effet 01/11/2024 (N-1 de test)
- `EKDI_valeurs2025.xlsx` — 536 lignes, export complet, dates d'effet 2025-01, 2025-02, 2025-07, 2025-08 (N de test)

Ces fichiers servent de jeu de référence pour valider la logique. Sur un appel réel, utiliser deux exports **de périmètre identique** (même sélection de tarifs) pour éviter les faux positifs "nouvelle clé".

## Lancer l'application

```bash
pip3 install flask openpyxl pandas pdfplumber  # pdfplumber : extraction/captures des tableaux CRE
python3 app.py   # `python` = Python 2 sur ce poste -> SyntaxError sur les accents
# http://localhost:9998
```
