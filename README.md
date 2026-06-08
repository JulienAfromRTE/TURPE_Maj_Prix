# TURPE_Maj_Prix

Application Projectix (Flask) d'aide a la mise a jour tarifaire annuelle du TURPE.

## Brique 1 (livree) : reconciliation EKDI N / N-1

Compare deux exports EKDI (table SAP) et met en evidence, cle par cle, les ecarts
sur le triplet **(Tarif, GrpValFix, Operande)** :

- valeurs **modifiees**, **nouvelles**, **supprimees**, **inchangees** ;
- calcul de l'**evolution** annuelle en % par cle ;
- **alerte** automatique au-dela de +/-20 % (valeur potentiellement erronee) ;
- gestion native du **decalage `_P`** : les operandes en `_P` (part fixe a echoir)
  sont compares a la date d'effet **moins un mois** (01.07 au lieu du 01.08),
  conformement au mode operatoire de verification ;
- **export Excel** des ecarts.

Objectif : remplacer la verification manuelle cle par cle dans EA09 (~150 cles)
par une lecture des seules lignes en ecart.

## Lancer en local

```bash
pip install flask openpyxl pandas
python app.py
# http://localhost:9998
```

## Conventions Projectix respectees

- variables de metadonnees `APP_*`
- route `/health` (et alias `/api/health`)
- base SQLite en `data/app.db`, mode WAL
- chemins JS relatifs, `url_for()` dans Jinja2
- aucun identifiant JS accentue
- `debug=False`

## Suites possibles (non livrees)

- **Brique 2** : extraction des tableaux des PDF CRE + table de correspondance
  libelle CRE -> cle de prix SAP.
- **Brique 3** : generation automatique de la nouvelle colonne historisee
  (`Valeur 08.AAAA`) dans le fichier de suivi.

L'ecriture dans SAP reste volontairement **hors perimetre** : l'app travaille en
lecture seule (extraction et verification).
