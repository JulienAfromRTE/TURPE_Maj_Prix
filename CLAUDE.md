# TURPE_Maj_Prix — contexte et conventions

## Contexte métier

Projet RTE (Réseau de Transport d'Électricité). Chaque année au 1er août, RTE doit mettre à jour les prix du TURPE (Tarif d'Utilisation des Réseaux Publics d'Électricité).

**Process actuel (archaïque) :**
1. La CRE (Commission de Régulation de l'Énergie) publie des délibérations PDF avec les nouveaux tarifs
2. Les valeurs sont copiées manuellement depuis les PDF dans un fichier Excel de suivi (`MAJ Tarifaire _Aout AAAA_...xlsx`)
3. Le fichier Excel est validé, puis les valeurs sont injectées manuellement dans SAP
4. Vérification clé par clé dans SAP, en extrayant deux bases : **EKDI** (lignes sans clé de prix) et **EPREIH** (lignes avec clé de prix)

**Ce que cette application remplace en priorité :** la vérification post-injection (~150 clés vérifiées une par une dans SAP), qui est le maillon le plus chronophage et risqué.

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
3. **Vérification post-injection** — deux sources complémentaires selon l'origine de la valeur dans le fichier Excel de suivi : **EKDI** pour les lignes **sans clé de prix** (appariement sur `(GrpValFix, Opérande)` sinon `Opérande` seul, valeur = `Valeur de saisie`), **EPREIH** pour les lignes **avec clé de prix** (appariement sur la clé de prix = colonne `Prix`, valeur = `Montant de prix`). Dans les deux cas, sélection de la valeur effective à la date d'effet (décalage `_P` au 01.07).

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

## Double validation ligne par ligne & ajout de lignes (v2.4)

La validation ligne par ligne passe d'**un** niveau à **deux niveaux distincts** (`VALIDATION_ROLES` dans `app.py`) : *Val. TMA* et *Val. DSIT*. Chaque niveau a ses colonnes `grid_rows.validated_<role> / _by / _profile / _at` (`role` ∈ `tma`, `rte`). **Une case ne peut être cochée que par le profil correspondant** — contrôle côté serveur (`api_validate_row` renvoie 403 si `current_profile()` ≠ profil requis) *et* côté front (case désactivée + infobulle via `canValidate(role)`). Une ligne n'est *pleinement validée* (surbrillance verte, compteur « validées », filtre *Non validées*) que si les deux niveaux le sont (`rowFullyValidated`). La route `POST …/grid/validate` prend désormais un champ `role`. L'historique fin trace `validation_tma` / `validation_rte` ; l'export `.xlsx` a deux blocs de colonnes (*Validee TMA / par / le*, *Validee DSIT / par / le*).

Les deux cases sont aussi accessibles **depuis la modale détail ligne**, sous le champ *Valeur saisie* (mêmes contrôles de profil).

**Ajout manuel de lignes** : bouton *+ Ajouter une ligne* (modale `addRowModal`) → route `POST /api/campaigns/<cid>/rows` qui insère une `grid_rows` au prochain `sort_order` (opérande obligatoire, reste optionnel), tracée (`audit_log` + `grid_row_history` champ `creation`).

**Migrations v2.4 :** `init_db()` ajoute via `ALTER TABLE` les huit colonnes `validated_tma*` / `validated_rte*` si absentes, et **reprend** l'ancienne validation unique (`validated`) vers le bon niveau selon `validated_profile` (TMA → `tma`, sinon → `rte`). L'ancienne colonne `validated` est conservée (non supprimée) mais n'est plus alimentée. Les seeds (`seed_example_campaign.py`) écrivent désormais directement `validated_rte*`.

## Vérification EKDI / EPREIH & validation humaine du contrôle (v2.5)

**L'étape 3 (`verif`) confronte la grille à deux extracts SAP complémentaires** (la transaction EA09 n'est plus utilisée — terme retiré partout) :

- **EKDI** → lignes **sans clé de prix**. Appariement du plus précis au plus large, en tokenisant `tarif_ekdi` et `grp` (une ligne agrège souvent plusieurs tarifs/groupes ValFix séparés par des espaces) : `(Tarif, GrpValFix, Opérande)` → `(Tarif, Opérande)` → `(GrpValFix, Opérande)` → `(Opérande)`. Cibler le tarif **évite l'ambiguïté** sur un dump SAP complet (~30 000 lignes). Valeur comparée = `Valeur de saisie`.
- **EPREIH** → lignes **avec clé de prix**. Appariement sur la clé de prix = colonne **`Prix`** de l'EPREIH (`load_epreih`), valeur = **`Montant de prix`**. Colonnes pouvant être entourées d'apostrophes côté SAP (nettoyées au chargement).

Dans les deux cas, sélection de la **valeur effective à la date d'effet** (`effective_value`, décalage `_P` au 01.07). Code : `_compare_ekdi` / `_compare_epreih`, route `POST /api/campaigns/<cid>/compare` (champ `kind` ∈ `ekdi`, `epreih`), filtrant la grille sur la présence/absence de `cle`. Statuts : conforme / écart / absent / ambigu.

**Validation humaine du contrôle (la base SAP fait foi).** Après le contrôle automatique, le **chef de projet DSIT** confirme ligne par ligne la valeur trouvée en base via une **case à cocher** en regard de chaque valeur (la table affiche *Attendue (étape 2)* vs *En base (SAP)*). Table `compare_validations` (`UNIQUE(campaign_id, row_id)`) figeant `expected` / `found` / `status` au moment de la coche, avec `validated_by / _profile / _at`. Routes `POST …/compare/validate` (403 si `current_profile()` ≠ *Chef de projet DSIT*) et `GET …/compare/validations`. Chaque coche est tracée dans `grid_row_history` (champ `verif_bdd`) et l'`audit_log`. Au re-chargement d'un extract, `_merge_compare_validations` ré-hydrate les coches et signale `stale` (la valeur en base a changé depuis la validation → à revalider). Export `.xlsx` : 5e feuille *Verification base*. Front : `runCompare` / `onCmpValidate` / `cmpValCell` (`campaign.js`), gating profil via `canValidate("rte")`.

**Migrations v2.5 :** `init_db()` crée `compare_validations` via `CREATE TABLE IF NOT EXISTS` (aucune autre migration).

## Procédure de saisie SAP & Ordre de Transport (v2.6)

**Nouvelle étape `scripts` (libellé « Procedure de saisie SAP »), intercalée entre l'étape 2 (saisie grille) et l'étape 3 (vérification)** — c'est désormais la 3e étape, la vérification devient la 4e. L'étape est ajoutée dans `WORKFLOW_STEPS` (`app.py`) : le stepper du front (boucle Jinja sur `workflow_steps`) et l'ensemble des statuts valides (`api_set_status`) en dérivent, donc l'ajout se propage seul. La renumérotation des titres `<h2>` (« 3. Procédure… », « 4. Vérification… ») et les boutons *Passer à…* sont câblés dans `templates/campaign.html`. La clé technique de l'étape reste `scripts` (et les routes `…/scripts*`) bien que le livrable ne soit plus du SQL.

**Pourquoi pas du SQL :** retour TMA — les tarifs sont mis à jour **dans l'IHM SAP, pas en base**. De plus **toute modification SAP exige un Ordre de Transport (OT)**, récupéré dans **Ocas** (l'outil SAP ChaRM de suivi des OT) ; SAP réclame l'OT à *chaque* modification. L'étape produit donc une **procédure de saisie** (pas à pas, à exécuter manuellement) et gère l'OT.

**Ordre de Transport (OT) :** **un OT par campagne**, **saisi manuellement** (pas d'intégration Ocas — l'app reste en lecture seule). Stocké dans `campaigns.sap_ot` (migration `ALTER TABLE` dans `init_db`), exposé par `campaign_to_dict`, modifié via `POST /api/campaigns/<cid>/ot` (tracé `audit_log` action `saisie_ot`). L'OT est rappelé sur **chaque étape** de la procédure ; s'il manque, un marqueur explicite `<OT manquant : a recuperer dans Ocas (SAP ChaRM)>` s'affiche.

**Ce que l'étape produit :** deux procédures texte générées **automatiquement à partir des seules valeurs saisies** (`grid_rows.new_value IS NOT NULL`), une par table SAP :

- **EKDI** → lignes **sans clé de prix**. Clé `(Tarif, GrpValFix, Opérande)`. Comme `tarif_ekdi`/`grp` agrègent souvent plusieurs tarifs/groupes séparés par des espaces (une ligne de grille couvre p.ex. `ZHTB2_CU/LU/MU`), on **développe le produit cartésien des tokens** → une étape de saisie par clé SAP réelle.
- **EPREIH** → lignes **avec clé de prix**. Clé = colonne `Prix`, valeur = `Montant de prix`.

Chaque étape liste Tarif / GrpValFix / Opérande (ou Clé de prix), **date de validité** (`_row_effective_date` : décalage `_P` au 01.07, sinon date d'effet — format `JJ.MM.AAAA`), valeur (`_fmt_num_fr` : virgule décimale, pas de bruit flottant) et **OT à saisir**. En-tête `_procedure_header` : campagne, période, date d'effet, OT, transaction (placeholder à préciser), compteurs, horodatage et rappel métier (OT obligatoire, saisie manuelle, aucune écriture SAP).

Code : `_generate_procedures` / `_build_ekdi_procedure` / `_build_epreih_procedure` (`app.py`). Routes : `GET /api/campaigns/<cid>/scripts` (aperçu JSON `{ot, ekdi:{text,n_rows,n_steps}, epreih:{…}}`), `GET /api/campaigns/<cid>/scripts/download?kind=ekdi|epreih` (téléchargement `.txt`, **tracé** action `generation_procedure_sap`). Front : `loadScripts` / `renderScript` / `downloadScript` / `copyScript` / `saveOt` (`campaign.js`), panneau `data-panel="scripts"` avec barre OT (`.ot-bar`), aperçu `<pre>` + boutons Copier / Télécharger / Rafraîchir.

**C'est un livrable de _préparation_, pas d'écriture.** Conformément aux décisions structurantes, l'application **ne se connecte à aucune base / à SAP et n'exécute jamais** la procédure : elle est relue puis appliquée manuellement dans SAP.

**Migrations v2.6 :** `init_db()` ajoute `campaigns.sap_ot` (`ALTER TABLE … ADD COLUMN`) si absente. Le statut `scripts` est une valeur supplémentaire de `campaigns.status`, déjà tolérée par la dérivation depuis `WORKFLOW_STEPS`.

## Grille atomique par GrpValFix (v2.7)

**Une ligne de grille = un seul `GrpValFix`.** Auparavant, certaines lignes agrégeaient plusieurs groupes ValFix dans le champ `grp` séparés par des espaces (p.ex. `ZHTB2_CU ZHTB2_LU ZHTB2_MU`), à l'image d'une cellule unique du fichier Excel ; ces groupes sont en réalité **plusieurs lignes SAP distinctes au même prix** (confirmé en base EKDI : `ZHTB2_CU` / `_LU` / `_MU` × tarifs `Z5CL_CAL01` / `Z5CL_CAL02`, toutes à la même valeur). Pour coller à l'Excel et permettre une **saisie / validation / vérification ligne par ligne**, ces lignes sont désormais **éclatées à la source** dans `reference_grid.json` (281 → 290 lignes, `order` renumérotés). Les 5 lignes concernées : *Composante de transformation* HTB2/HTB1/HTA (`ZCOEF_KPON`) et *DPP* HTB2/HTB1 (`ZCO_ALPDPP`).

- **Affichage** — colonne **`GrpValFix (valeur EKDI)`** ajoutée à la grille (entre *Tarif* et *Opérande*, ordre de l'Excel), tokens empilés (`grpHtml` dans `campaign.js`). La donnée `grp` était déjà servie par l'API (recherche, sous-titre de la modale détail), seul l'affichage colonne manquait.
- **Étape 3 (procédure SAP) / étape 4 (vérification)** — `_build_ekdi_procedure` et `_compare_ekdi` tokenisent toujours `grp` : sur des lignes atomiques c'est un no-op, mais **conservé en filet de sécurité** pour les campagnes antérieures (grid_rows encore agrégés) et le `tarif_ekdi` multi-valeurs (variantes d'écriture SAP, p.ex. `5CL_CAL01 Z5CL_CAL01`, ≠ tarifs distincts comme `Z5CL_CAL01 Z5CL_CAL02`). Chaque ligne atomique produit donc sa propre étape de saisie et son propre statut de vérification (par GrpValFix), ce qui est plus fin.

**Migration v2.7 (`_migrate_split_grp`, exécutée par `init_db()` au lancement) :** éclate **en base** les `grid_rows` au `grp` multi-valeurs des campagnes **non clôturées** (les campagnes clôturées/auditées restent figées). **Non destructive** : la ligne existante est conservée (id, valeur saisie, validations, *présent Excel*, historique, captures, commentaires intacts) et reçoit le 1er token ; les tokens suivants donnent de **nouvelles** lignes copiant tous les attributs (mêmes `sort_order` → affichage groupé). Tracée : `grid_row_history` (`field` = `grp_split` sur la ligne mère, `creation` avec provenance `(depuis ligne #id)` sur les filles) et `audit_log` (action `migration_split_grp`, acteur `Migration v2.7` / profil `systeme`). **Idempotente** : ne fait rien s'il ne reste aucun `grp` multi-valeurs.

## Filtre « Absent Excel » & commentaires de campagne (v2.8)

Deux ajouts à l'étape 2 (saisie grille) :

- **Filtre *Absent Excel*** — case à cocher (`gridOnlyAbsentExcel` dans `templates/campaign.html`) en regard de *À saisir uniquement* / *Non validées*. Affiche **uniquement les lignes où *Présent Excel* n'est pas coché** (`renderGrid` filtre sur `!r.excel_present`, `campaign.js`). Complète le marqueur *Présent Excel* (v2.1) pour repérer d'un coup les clés à rattacher au fichier de suivi.
- **Commentaires de campagne** — fil de notes libres **sous la grille** (`#gridComments`), distinct des commentaires rattachés à une ligne (`grid_row_comments`). Table `campaign_comments` (`campaign_id`, `comment`, `user_name`, `user_profile`, `created_at`), routes `GET/POST /api/campaigns/<cid>/grid-comments` et `DELETE …/grid-comments/<id>`. **Append-only, nominatif**, tracé (`audit_log` action `commentaire_campagne`) et **exporté** dans le `.xlsx` (6e feuille *Commentaires campagne* : Date / Auteur / Profil / Commentaire). Front : `loadCampComments` / `addCampComment` / `renderCampComments` / `deleteCampComment` (`campaign.js`).

**Migration v2.8 :** `init_db()` crée `campaign_comments` via `CREATE TABLE IF NOT EXISTS` (aucune autre migration).

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
