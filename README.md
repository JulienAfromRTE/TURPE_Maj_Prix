# TURPE_Maj_Prix

Application Projectix (Flask) d'aide a la mise a jour tarifaire du TURPE.

Depuis la v2, l'application est un **portail de gestion des mises a jour de prix au
fil de l'eau** : chaque campagne (la MAJ annuelle du 1er aout, ou une MAJ
intermediaire) est suivie de bout en bout via un workflow trace.

## Le portail

Page d'accueil = liste des campagnes (avec avancement de la saisie, etat du
workflow, validations TMA/RTE). A l'ouverture, l'utilisateur saisit son nom : il est
associe a **chaque action** (saisie, upload, verification) dans un journal d'audit.

### Workflow d'une campagne (3 etapes)

1. **Deliberations CRE** — depot des 2 PDF de la deliberation CRE. Les fichiers sont
   stockes sur le serveur (`data/uploads/<campagne>/`) et retelechargeables.
2. **Saisie de la grille tarifaire** — au lieu de remplir l'Excel de suivi, on
   remplit un tableau en ligne **instancie avec l'historique des MAJ precedentes**
   (287 lignes tarifaires extraites de `MAJ_Tarifaire__Aout_2025_*.xlsx`). L'evolution
   par rapport a la derniere valeur connue est calculee en direct, avec **alerte
   au-dela de +/-20 %**. La grille est **exportable en .xlsx** pour injection SAP.
3. **Verification post-injection (EKDI / EA09)** — apres injection dans SAP, on depose
   un extract (EKDI et/ou EA09) ; l'outil le confronte automatiquement aux valeurs
   saisies dans la grille. L'appariement se fait sur (GrpValFix, Operande), sinon sur
   l'Operande seul ; la valeur **effective a la date d'effet** est selectionnee dans
   l'extract (operandes en `_P` au 01.07). Statuts : conforme / ecart / absent /
   ambigu.

### Validations metier et tracabilite

Chaque campagne fait l'objet de validations par la **TMA** puis par le **chef de
projet RTE** (verdict OK/KO + commentaire). Toutes les actions sont consignees dans
un **journal d'audit** consultable dans l'onglet « Journal & verifs ».

L'ecriture dans SAP reste volontairement **hors perimetre** : l'application travaille
en preparation / verification uniquement.

## Outil reconciliation EKDI N / N-1 (brique 1, conservee)

Accessible depuis le portail (`/reconcile`). Compare deux exports EKDI et met en
evidence, cle par cle, les ecarts sur le triplet **(Tarif, GrpValFix, Operande)** :
modifiees / nouvelles / supprimees / inchangees, evolution en %, alerte > +/-20 %,
gestion du decalage `_P` (01.07), export Excel des ecarts.

## Lancer en local

```bash
pip install flask openpyxl pandas
python app.py
# http://localhost:9998
```

## Donnees & stockage

- `data/app.db` — base SQLite (WAL) : campagnes, grilles, fichiers, verifications, audit.
- `data/uploads/<id>/` — fichiers deposes par campagne (PDF CRE, extracts).
- `data/reference_grid.json` — grille de reference figee (287 lignes), regeneree depuis
  l'Excel de suivi ; sert a instancier chaque nouvelle campagne.

## Conventions Projectix respectees

- variables de metadonnees `APP_*`, route `/health` (+ `/api/health`)
- base SQLite en `data/app.db`, mode WAL
- chemins JS relatifs, `url_for()` dans Jinja2, aucun identifiant JS accentue
- `debug=False`
