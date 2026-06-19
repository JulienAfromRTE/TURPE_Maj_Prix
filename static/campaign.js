"use strict";

const CID = document.body.getAttribute("data-campaign-id");
let CAMPAIGN = null;
let GRID = null;          // { period_label, periods, rows }
const FILE_KIND_LABELS = {
  cre_pdf_1: "Délibération CRE HTB", cre_pdf_2: "Délibération CRE HTA",
  ekdi: "Extract EKDI", epreih: "Extract EPREIH", export: "Export grille",
};

// Deux niveaux de validation ; chacun n'est cochable que par le profil associe.
const VALIDATION_LABELS = { tma: "TMA", rte: "Chef de projet DSIT" };
function canValidate(role) {
  return getProfile() === VALIDATION_LABELS[role];
}
function rowFullyValidated(r) {
  return !!(r.validated_tma && r.validated_rte);
}

// ---------------------------------------------------------------------------
// Navigation stepper
// ---------------------------------------------------------------------------
function showPanel(name) {
  document.querySelectorAll(".step-panel").forEach(function (p) {
    p.style.display = p.getAttribute("data-panel") === name ? "block" : "none";
  });
  document.querySelectorAll(".step").forEach(function (s) {
    s.classList.toggle("active", s.getAttribute("data-step") === name);
  });
  if (name === "grid" && !GRID) loadGrid();
  if (name === "scripts") loadScripts();
  if (name === "audit") { loadAudit(); renderVerifs(); }
}

// ---------------------------------------------------------------------------
// Campagne (entete + fichiers + verifs)
// ---------------------------------------------------------------------------
async function loadCampaign() {
  const resp = await apiFetch("../api/campaigns/" + CID);
  CAMPAIGN = await resp.json();
  document.getElementById("campTitle").textContent = CAMPAIGN.name;
  document.getElementById("campMeta").textContent =
    "Période " + CAMPAIGN.period_label + " · effet " + CAMPAIGN.effective_date +
    " · créée par " + CAMPAIGN.created_by;
  renderFiles();
  renderVerifs();
}

function renderFiles() {
  const body = document.getElementById("filesBody");
  body.innerHTML = "";
  CAMPAIGN.files.forEach(function (f) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      "<td>" + (FILE_KIND_LABELS[f.kind] || f.kind) + "</td>" +
      "<td>" + f.original_name + "</td>" +
      '<td class="num">' + fmtBytes(f.size) + "</td>" +
      "<td>" + f.uploaded_by + "</td>" +
      "<td>" + f.uploaded_at.replace("T", " ") + "</td>" +
      '<td><a class="link" href="../api/files/' + f.id + '/download">telecharger</a> ' +
      '<button class="link danger" data-del="' + f.id + '">suppr.</button></td>';
    body.appendChild(tr);
  });
  body.querySelectorAll("[data-del]").forEach(function (b) {
    b.addEventListener("click", function () { deleteFile(b.getAttribute("data-del")); });
  });
}

async function uploadFile(kind, input) {
  const file = input.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("kind", kind);
  fd.append("file", file);
  fd.append("user_name", getUser());
  const resp = await apiFetch("../api/campaigns/" + CID + "/files", { method: "POST", body: fd });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || "Échec upload."); return; }
  CAMPAIGN = data;
  input.value = "";
  renderFiles();
}

async function deleteFile(id) {
  if (!confirm("Supprimer ce fichier ?")) return;
  await apiFetch("../api/files/" + id, { method: "DELETE" });
  loadCampaign();
}

// ---------------------------------------------------------------------------
// Grille tarifaire
// ---------------------------------------------------------------------------
let VISIBLE_PERIODS = null;   // Set des periodes (annees) affichees
let ROW_MODAL_ID = null;      // ligne ouverte dans la modale detail

function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

async function loadGrid() {
  const resp = await apiFetch("../api/campaigns/" + CID + "/grid");
  GRID = await resp.json();
  VISIBLE_PERIODS = defaultVisiblePeriods();
  document.getElementById("newColLabel").textContent = "Valeur " + GRID.period_label;
  buildYearsBar();
  buildGridHead();
  renderGrid();
  loadCampComments();
}

// Commentaires libres au niveau de la campagne (sous la grille), distincts des
// commentaires rattaches a une ligne. Traces, nominatifs et exportes dans le xlsx.
async function loadCampComments() {
  const resp = await apiFetch("../api/campaigns/" + CID + "/grid-comments");
  const data = await resp.json();
  renderCampComments(data.comments);
}

async function addCampComment() {
  const inp = document.getElementById("campCmtInput");
  const txt = inp.value.trim();
  if (!txt) { inp.focus(); return; }
  const resp = await apiFetch("../api/campaigns/" + CID + "/grid-comments", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ comment: txt }),
  });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || "Erreur."); return; }
  inp.value = "";
  renderCampComments(data.comments);
}

function renderCampComments(comments) {
  const ul = document.getElementById("campCmtList");
  ul.innerHTML = "";
  if (!comments.length) { ul.innerHTML = '<li class="empty">Aucun commentaire.</li>'; return; }
  comments.forEach(function (c) {
    const who = esc(c.user_name) + (c.user_profile ? " (" + esc(c.user_profile) + ")" : "");
    const li = document.createElement("li");
    li.innerHTML =
      '<button class="cmt-del" data-del-cmt="' + c.id + '" title="Supprimer ce commentaire">Supprimer</button>' +
      '<div class="cmt-text">' + esc(c.comment) + "</div>" +
      '<div class="cmt-meta"><b>' + who + "</b> · " +
      '<span class="atime">' + esc(c.created_at.replace("T", " ")) + "</span></div>";
    ul.appendChild(li);
  });
  ul.querySelectorAll("[data-del-cmt]").forEach(function (b) {
    b.addEventListener("click", function () { deleteCampComment(b.getAttribute("data-del-cmt")); });
  });
}

async function deleteCampComment(commentId) {
  if (!confirm("Supprimer ce commentaire ?")) return;
  const resp = await apiFetch("../api/campaigns/" + CID + "/grid-comments/" + commentId, { method: "DELETE" });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || "Erreur."); return; }
  renderCampComments(data.comments);
}

// Par defaut on affiche les annees N-1 et N-2 par rapport a la campagne en cours
// (ex : campagne 08.2026 -> on montre les periodes de 2025 et 2024).
function periodYear(p) {
  const m = String(p).match(/(\d{4})/);
  return m ? parseInt(m[1], 10) : NaN;
}

function defaultVisiblePeriods() {
  const campaignYear = periodYear(GRID.period_label);
  if (isNaN(campaignYear)) return new Set(GRID.periods);
  const wanted = GRID.periods.filter(function (p) {
    const y = periodYear(p);
    return y === campaignYear - 1 || y === campaignYear - 2;
  });
  // Repli : si aucune periode ne correspond, on garde tout pour ne rien masquer a tort.
  return new Set(wanted.length ? wanted : GRID.periods);
}

function buildYearsBar() {
  const wrap = document.getElementById("yearsToggles");
  wrap.innerHTML = "";
  GRID.periods.forEach(function (p) {
    const b = document.createElement("button");
    b.className = "year-chip" + (VISIBLE_PERIODS.has(p) ? " on" : "");
    b.textContent = p;
    b.addEventListener("click", function () {
      if (VISIBLE_PERIODS.has(p)) VISIBLE_PERIODS.delete(p); else VISIBLE_PERIODS.add(p);
      b.classList.toggle("on");
      buildGridHead();
      renderGrid();
    });
    wrap.appendChild(b);
  });
}

function setAllYears(on) {
  VISIBLE_PERIODS = new Set(on ? GRID.periods : []);
  buildYearsBar();
  buildGridHead();
  renderGrid();
}

function visiblePeriods() {
  return GRID.periods.filter(function (p) { return VISIBLE_PERIODS.has(p); });
}

// GrpValFix (valeur EKDI) : un champ peut agreger plusieurs groupes ValFix
// separes par des espaces (ex "ZHTB2_CU ZHTB2_LU ZHTB2_MU" = 3 lignes SAP au
// meme prix). On les empile comme dans le fichier Excel de suivi.
function grpHtml(grp) {
  const tokens = (grp || "").split(/\s+/).filter(Boolean);
  return tokens.map(esc).join("<br>");
}

function buildGridHead() {
  const head = document.getElementById("gridHead");
  let h = '<th class="num">N&deg;</th><th>Section</th><th>Tarif</th><th>GrpValFix<br><span class="th-sub">(valeur EKDI)</span></th><th>Opérande</th><th>Clé</th>';
  visiblePeriods().forEach(function (p) { h += '<th class="num hist">' + p + "</th>"; });
  h += '<th class="num newcol">' + GRID.period_label + "</th>";
  h += '<th class="num">Évol.</th><th class="valcol">Présent Excel</th><th class="valcol">Val. TMA</th><th class="valcol">Val. DSIT</th><th>CRE</th>';
  head.innerHTML = h;
}

function updateGridProgress(shown) {
  const filled = GRID.rows.filter(function (r) { return r.new_value !== null && r.new_value !== undefined; }).length;
  const validated = GRID.rows.filter(rowFullyValidated).length;
  let txt = filled + " / " + GRID.rows.length + " saisies · " + validated + " validées";
  if (shown !== undefined) txt += " · " + shown + " affichées";
  document.getElementById("gridProgress").textContent = txt;
}

// Cellule de validation pour un niveau (tma|rte). Case desactivee si le profil
// courant ne correspond pas, avec une infobulle explicative.
function valCell(role, r) {
  const on = r["validated_" + role];
  const by = r["validated_" + role + "_by"];
  const allowed = canValidate(role);
  return '<td class="valcol"><input type="checkbox" class="gval-check" data-role="' + role +
    '" data-id="' + r.id + '"' + (on ? " checked" : "") + (allowed ? "" : " disabled") +
    (allowed ? "" : ' title="Réservé au profil ' + esc(VALIDATION_LABELS[role]) + '"') + ">" +
    (on && by ? '<div class="valmeta">' + esc(by) + "</div>" : "") + "</td>";
}

// « Present Excel » : coche reservee au chef de projet DSIT, signalant que la
// cle figure dans le fichier de suivi Excel. Independante des validations.
function canMarkExcel() {
  return getProfile() === VALIDATION_LABELS.rte;
}
function excelCell(r) {
  const on = r.excel_present;
  const by = r.excel_present_by;
  const allowed = canMarkExcel();
  return '<td class="valcol"><input type="checkbox" class="excel-check" data-id="' + r.id + '"' +
    (on ? " checked" : "") + (allowed ? "" : " disabled") +
    (allowed ? "" : ' title="Réservé au profil ' + esc(VALIDATION_LABELS.rte) + '"') + ">" +
    (on && by ? '<div class="valmeta">' + esc(by) + "</div>" : "") + "</td>";
}

// Interprete une saisie de filtre comme un nombre (virgule OU point en
// decimale, espaces de milliers eventuels). Retourne { value, decimals } ou
// null si la saisie n'est pas numerique. Permet de coller une valeur Excel
// type "11 545,32" et de retrouver les lignes par leur prix.
function parseFilterNumber(raw) {
  const norm = raw.replace(/\s/g, "").replace(",", ".");
  if (norm === "" || !/^-?\d+(\.\d+)?$/.test(norm)) return null;
  const dot = norm.indexOf(".");
  return { value: parseFloat(norm), decimals: dot === -1 ? 0 : norm.length - dot - 1 };
}

// Vrai si une des valeurs de prix de la ligne (historique + valeur saisie)
// egale le nombre cherche, arrondi au nombre de decimales du filtre.
function rowMatchesNumber(r, num) {
  const factor = Math.pow(10, num.decimals);
  const target = Math.round(num.value * factor);
  const vals = Object.values(r.history || {});
  if (r.new_value !== null && r.new_value !== undefined) vals.push(r.new_value);
  return vals.some(function (v) {
    return v !== null && v !== undefined && v !== "" && Math.round(v * factor) === target;
  });
}

function renderGrid() {
  const filter = (document.getElementById("gridFilter").value || "").toLowerCase().trim();
  const filterNum = filter ? parseFilterNumber(filter) : null;
  const onlyEmpty = document.getElementById("gridOnlyEmpty").checked;
  const onlyTodo = document.getElementById("gridOnlyTodo").checked;
  const onlyAbsentExcel = document.getElementById("gridOnlyAbsentExcel").checked;
  const periods = visiblePeriods();
  const body = document.getElementById("gridBody");
  body.innerHTML = "";
  let shown = 0;
  GRID.rows.forEach(function (r) {
    const hasVal = r.new_value !== null && r.new_value !== undefined;
    if (onlyEmpty && hasVal) return;
    if (onlyTodo && rowFullyValidated(r)) return;
    if (onlyAbsentExcel && r.excel_present) return;
    if (filter) {
      const hay = (r.section + " " + r.tarif_label + " " + r.operande + " " + r.cle + " " + r.grp).toLowerCase();
      const matchText = hay.indexOf(filter) !== -1;
      const matchNum = filterNum !== null && rowMatchesNumber(r, filterNum);
      if (!matchText && !matchNum) return;
    }
    shown++;
    const tr = document.createElement("tr");
    tr.setAttribute("data-row", r.id);
    if (r.aberrant) tr.classList.add("row-aberrant");
    if (rowFullyValidated(r)) tr.classList.add("row-validated");
    let cells =
      '<td class="num row-open">' + (r.sort_order || "") + "</td>" +
      '<td class="sec row-open">' + r.section + "</td>" +
      '<td class="row-open">' + (r.tarif_label || "") + "</td>" +
      '<td class="row-open grpcell">' + grpHtml(r.grp) + "</td>" +
      '<td class="row-open">' + r.operande + "</td>" +
      '<td class="row-open">' + (r.cle || "") + "</td>";
    periods.forEach(function (p) {
      const v = r.history[p];
      cells += '<td class="num hist">' + (v === null || v === undefined ? "" : fmtNum(v)) + "</td>";
    });
    const val = hasVal ? r.new_value : "";
    cells += '<td class="num newcol"><input class="gval" data-id="' + r.id + '" value="' + val + '"></td>';
    cells += '<td class="num evolcell">' + (r.aberrant ? "⚠ " : "") + fmtPct(r.pct) + "</td>";
    cells += excelCell(r) + valCell("tma", r) + valCell("rte", r);
    cells += '<td><span class="cre-badge' + (r.n_images ? " has" : "") + '">&#128206; ' + (r.n_images || 0) + "</span>" +
      (r.n_comments ? ' <span class="cmt-badge">&#128172; ' + r.n_comments + "</span>" : "") + "</td>";
    tr.innerHTML = cells;
    body.appendChild(tr);
  });
  updateGridProgress(shown);
  body.querySelectorAll(".gval").forEach(function (inp) {
    inp.addEventListener("change", function () { onGridEdit(inp); });
  });
  body.querySelectorAll(".gval-check").forEach(function (chk) {
    chk.addEventListener("change", function () { onValidate(chk); });
  });
  body.querySelectorAll(".excel-check").forEach(function (chk) {
    chk.addEventListener("change", function () { onExcelPresent(chk); });
  });
  body.querySelectorAll("tr").forEach(function (tr) {
    tr.addEventListener("click", function (e) {
      // Ne pas ouvrir la modale quand on edite la valeur ou coche la validation.
      if (e.target.closest("input, button")) return;
      openRowModal(parseInt(tr.getAttribute("data-row"), 10));
    });
  });
}

// Met a jour la valeur d'une ligne (modele + cellule grille). Retourne false si
// la saisie n'est pas numerique. Source unique pour la grille et la modale.
function setRowValue(id, raw) {
  const row = GRID.rows.find(function (r) { return r.id === id; });
  if (!row) return true;
  raw = (raw || "").trim();
  if (raw === "") {
    row.new_value = null; row.pct = null; row.aberrant = false;
  } else {
    const v = parseFloat(raw.replace(",", ".").replace(/\s/g, ""));
    if (isNaN(v)) return false;
    row.new_value = v;
    if (row.prev_value !== null && row.prev_value !== 0) {
      row.pct = (v - row.prev_value) / Math.abs(row.prev_value);
      row.aberrant = Math.abs(row.pct) > 0.20;
    } else { row.pct = null; row.aberrant = false; }
  }
  const tr = document.querySelector('#gridBody tr[data-row="' + id + '"]');
  if (tr) {
    const inp = tr.querySelector(".gval");
    if (inp) { inp.value = (row.new_value === null ? "" : raw); inp.classList.remove("bad"); }
    tr.classList.toggle("row-aberrant", !!row.aberrant);
    const ev = tr.querySelector(".evolcell");
    if (ev) ev.innerHTML = (row.aberrant ? "⚠ " : "") + fmtPct(row.pct);
  }
  updateGridProgress();
  return true;
}

function onGridEdit(inp) {
  const id = parseInt(inp.getAttribute("data-id"), 10);
  inp.classList.toggle("bad", !setRowValue(id, inp.value));
}

// Repercute la reponse serveur sur le modele de la ligne pour un niveau donne.
function applyValidation(row, role, data) {
  row["validated_" + role] = data.validated;
  row["validated_" + role + "_by"] = data.by;
  row["validated_" + role + "_at"] = data.at;
}

// Resynchronise les cases (et la surbrillance) d'une ligne dans la grille.
function syncGridRowValidation(id) {
  const row = GRID.rows.find(function (r) { return r.id === id; });
  const tr = document.querySelector('#gridBody tr[data-row="' + id + '"]');
  if (!tr || !row) return;
  tr.querySelectorAll(".gval-check").forEach(function (chk) {
    const role = chk.getAttribute("data-role");
    chk.checked = !!row["validated_" + role];
    const cell = chk.closest("td");
    let meta = cell.querySelector(".valmeta");
    const by = row["validated_" + role + "_by"];
    if (chk.checked && by) {
      if (!meta) { meta = document.createElement("div"); meta.className = "valmeta"; cell.appendChild(meta); }
      meta.textContent = by;
    } else if (meta) { meta.remove(); }
  });
  tr.classList.toggle("row-validated", rowFullyValidated(row));
  updateGridProgress();
}

// Envoie la (de)validation d'un niveau au serveur (controle de profil cote serveur).
// Retourne true si la requete a abouti.
async function postValidation(id, role, validated) {
  const resp = await apiFetch("../api/campaigns/" + CID + "/grid/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: id, role: role, validated: validated }),
  });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || "Erreur."); return false; }
  const row = GRID.rows.find(function (r) { return r.id === id; });
  applyValidation(row, role, data);
  return true;
}

async function onValidate(chk) {
  const id = parseInt(chk.getAttribute("data-id"), 10);
  const role = chk.getAttribute("data-role");
  const validated = chk.checked;
  if (!(await postValidation(id, role, validated))) { chk.checked = !validated; return; }
  syncGridRowValidation(id);
  if (ROW_MODAL_ID === id) renderModalValidation(GRID.rows.find(function (r) { return r.id === id; }));
}

// (De)marque une ligne « Présent Excel » (controle de profil cote serveur).
async function onExcelPresent(chk) {
  const id = parseInt(chk.getAttribute("data-id"), 10);
  const present = chk.checked;
  const resp = await apiFetch("../api/campaigns/" + CID + "/grid/excel-present", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: id, present: present }),
  });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || "Erreur."); chk.checked = !present; return; }
  const row = GRID.rows.find(function (r) { return r.id === id; });
  if (row) { row.excel_present = data.present; row.excel_present_by = data.by; row.excel_present_at = data.at; }
  const cell = chk.closest("td");
  let meta = cell.querySelector(".valmeta");
  if (data.present && data.by) {
    if (!meta) { meta = document.createElement("div"); meta.className = "valmeta"; cell.appendChild(meta); }
    meta.textContent = data.by;
  } else if (meta) { meta.remove(); }
}

// ---------------------------------------------------------------------------
// Modale detail ligne : captures CRE + historique des modifications
// ---------------------------------------------------------------------------
async function openRowModal(id) {
  ROW_MODAL_ID = id;
  const row = GRID.rows.find(function (r) { return r.id === id; });
  document.getElementById("rowModalTitle").textContent = (row.tarif_label || "") + " · " + row.operande;
  document.getElementById("rowModalTarif").textContent = row.tarif_label || "—";
  document.getElementById("rowModalOperande").textContent = row.operande || "—";
  document.getElementById("rowModalCle").textContent = row.cle || "—";
  document.getElementById("rowModalMeta").textContent =
    "Section " + row.section + (row.grp ? " · GrpValFix " + row.grp : "");
  const vinp = document.getElementById("rowModalValue");
  vinp.value = (row.new_value === null || row.new_value === undefined) ? "" : row.new_value;
  vinp.classList.remove("bad");
  setModalEditable(false);
  setIdentityEditable(false);
  document.getElementById("rowIdStatus").textContent = "";
  document.getElementById("rowModalSaveStatus").textContent = "";
  updateModalEvol(row);
  renderModalValidation(row);
  document.getElementById("rowModalComment").value = "";
  document.getElementById("rowModalCommentStatus").textContent = "";
  document.getElementById("rowImgInput").value = "";
  document.getElementById("rowImgCaption").value = "";
  document.getElementById("rowModal").style.display = "flex";
  loadRowImages(id);
  loadRowHistory(id);
  loadRowComments(id);
}

// Bascule la valeur entre lecture seule et edition (bouton "Modifier").
// A l'ouverture la valeur est verrouillee pour eviter les modifications accidentelles.
function setModalEditable(on) {
  const inp = document.getElementById("rowModalValue");
  const edit = document.getElementById("rowModalEdit");
  const save = document.getElementById("rowModalSave");
  inp.readOnly = !on;
  inp.classList.toggle("editing", on);
  edit.style.display = on ? "none" : "";
  save.style.display = on ? "" : "none";
  if (on) inp.focus();
}

// Bascule les 3 blocs (Tarif / Operande / Cle) entre affichage et edition.
// Le triplet forme la cle SAP : correction possible mais tracee champ par champ.
const ID_FIELDS = [
  { col: "tarif_label", val: "rowModalTarif", inp: "rowEditTarif" },
  { col: "operande", val: "rowModalOperande", inp: "rowEditOperande" },
  { col: "cle", val: "rowModalCle", inp: "rowEditCle" },
];

function setIdentityEditable(on) {
  const row = GRID.rows.find(function (r) { return r.id === ROW_MODAL_ID; });
  ID_FIELDS.forEach(function (f) {
    const inp = document.getElementById(f.inp);
    if (on && row) inp.value = row[f.col] || "";
    inp.style.display = on ? "" : "none";
    document.getElementById(f.val).style.display = on ? "none" : "";
  });
  document.getElementById("rowIdEdit").style.display = on ? "none" : "";
  document.getElementById("rowIdSave").style.display = on ? "" : "none";
  document.getElementById("rowIdCancel").style.display = on ? "" : "none";
  if (on) document.getElementById("rowEditTarif").focus();
}

async function saveRowIdentity() {
  const id = ROW_MODAL_ID;
  const status = document.getElementById("rowIdStatus");
  const body = {};
  ID_FIELDS.forEach(function (f) {
    body[f.col] = document.getElementById(f.inp).value.trim();
  });
  status.textContent = "Enregistrement...";
  const resp = await apiFetch("../api/campaigns/" + CID + "/rows/" + id + "/identity", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) { status.textContent = data.error || "Erreur."; return; }
  // Repercute sur le modele + la grille puis rafraichit l'affichage de la modale.
  const row = GRID.rows.find(function (r) { return r.id === id; });
  if (row) {
    row.tarif_label = data.row.tarif_label;
    row.operande = data.row.operande;
    row.cle = data.row.cle;
  }
  setIdentityEditable(false);
  document.getElementById("rowModalTitle").textContent = (data.row.tarif_label || "") + " · " + data.row.operande;
  document.getElementById("rowModalTarif").textContent = data.row.tarif_label || "—";
  document.getElementById("rowModalOperande").textContent = data.row.operande || "—";
  document.getElementById("rowModalCle").textContent = data.row.cle || "—";
  status.textContent = data.updated.length
    ? "Enregistré à " + new Date().toLocaleTimeString("fr-FR")
    : "Aucune modification.";
  if (typeof renderGrid === "function" && GRID) renderGrid();
  loadRowHistory(id);
}

// Reflete l'etat des deux validations dans la modale (cases gateees par profil).
const MODAL_VAL_FIELDS = [
  { role: "tma", chk: "rowValTma", meta: "rowValTmaMeta" },
  { role: "rte", chk: "rowValRte", meta: "rowValRteMeta" },
];

function renderModalValidation(row) {
  MODAL_VAL_FIELDS.forEach(function (f) {
    const chk = document.getElementById(f.chk);
    const meta = document.getElementById(f.meta);
    const allowed = canValidate(f.role);
    chk.checked = !!row["validated_" + f.role];
    chk.disabled = !allowed;
    chk.title = allowed ? "" : "Réservé au profil " + VALIDATION_LABELS[f.role];
    const by = row["validated_" + f.role + "_by"];
    meta.textContent = (chk.checked && by) ? "par " + by : "";
  });
}

async function onModalValidate(chk) {
  const id = ROW_MODAL_ID;
  const role = chk.getAttribute("data-role");
  const validated = chk.checked;
  if (!(await postValidation(id, role, validated))) { chk.checked = !validated; return; }
  const row = GRID.rows.find(function (r) { return r.id === id; });
  renderModalValidation(row);
  syncGridRowValidation(id);
  loadRowHistory(id);
}

// Affiche la valeur N-1 de reference + l'evolution % (avec alerte aberrante).
function updateModalEvol(row) {
  const el = document.getElementById("rowModalEvol");
  const prev = (row.prev_value === null || row.prev_value === undefined)
    ? "—" : fmtNum(row.prev_value);
  let txt = "Précédente : " + prev;
  if (row.pct !== null && row.pct !== undefined) {
    txt += " · évolution " + (row.aberrant ? "⚠ " : "") + fmtPct(row.pct);
  }
  el.textContent = txt;
  el.classList.toggle("aberrant", !!row.aberrant);
}

function onModalValueEdit() {
  const inp = document.getElementById("rowModalValue");
  const ok = setRowValue(ROW_MODAL_ID, inp.value);
  inp.classList.toggle("bad", !ok);
  if (ok) {
    const row = GRID.rows.find(function (r) { return r.id === ROW_MODAL_ID; });
    updateModalEvol(row);
  }
  document.getElementById("rowModalSaveStatus").textContent = "";
}

async function saveRowValue() {
  const id = ROW_MODAL_ID;
  const inp = document.getElementById("rowModalValue");
  if (!setRowValue(id, inp.value)) {
    inp.classList.add("bad");
    document.getElementById("rowModalSaveStatus").textContent = "Valeur non numérique.";
    return;
  }
  const row = GRID.rows.find(function (r) { return r.id === id; });
  const status = document.getElementById("rowModalSaveStatus");
  status.textContent = "Enregistrement...";
  const resp = await apiFetch("../api/campaigns/" + CID + "/grid", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates: [{ id: id, new_value: row.new_value }] }),
  });
  const data = await resp.json();
  if (!resp.ok) { status.textContent = data.error || "Erreur."; return; }
  status.textContent = "Enregistré à " + new Date().toLocaleTimeString("fr-FR");
  setModalEditable(false);
  loadRowHistory(id);
}

async function addRowComment() {
  const id = ROW_MODAL_ID;
  const inp = document.getElementById("rowModalComment");
  const txt = inp.value.trim();
  const status = document.getElementById("rowModalCommentStatus");
  if (!txt) { status.textContent = "Saisissez un commentaire."; return; }
  status.textContent = "Ajout...";
  const resp = await apiFetch("../api/campaigns/" + CID + "/rows/" + id + "/comments", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ comment: txt }),
  });
  const data = await resp.json();
  if (!resp.ok) { status.textContent = data.error || "Erreur."; return; }
  inp.value = "";
  status.textContent = "Ajouté à " + new Date().toLocaleTimeString("fr-FR");
  renderComments(data.comments, id);
  setCommentCount(id, data.n_comments);
}

async function loadRowComments(id) {
  const resp = await apiFetch("../api/campaigns/" + CID + "/rows/" + id + "/comments");
  const data = await resp.json();
  renderComments(data.comments, id);
  setCommentCount(id, data.comments.length);
}

function renderComments(comments, id) {
  const ul = document.getElementById("rowComments");
  ul.innerHTML = "";
  if (!comments.length) { ul.innerHTML = '<li class="empty">Aucun commentaire.</li>'; return; }
  comments.forEach(function (c) {
    const li = document.createElement("li");
    li.innerHTML =
      '<button class="cmt-del" data-del-cmt="' + c.id + '" title="Supprimer ce commentaire">Supprimer</button>' +
      '<div class="cmt-text">' + esc(c.comment) + "</div>" +
      '<div class="cmt-meta"><b>' + esc(c.user_name) + "</b> · " +
      '<span class="atime">' + esc(c.created_at.replace("T", " ")) + "</span></div>";
    ul.appendChild(li);
  });
  ul.querySelectorAll("[data-del-cmt]").forEach(function (b) {
    b.addEventListener("click", function () { deleteRowComment(b.getAttribute("data-del-cmt"), id); });
  });
}

async function deleteRowComment(commentId, rowId) {
  if (!confirm("Supprimer ce commentaire ?")) return;
  const resp = await apiFetch("../api/campaigns/" + CID + "/comments/" + commentId, { method: "DELETE" });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || "Erreur."); return; }
  renderComments(data.comments, rowId);
  setCommentCount(rowId, data.n_comments);
}

// Met a jour l'indicateur commentaire (compteur) dans la ligne de grille.
function setCommentCount(id, n) {
  const row = GRID.rows.find(function (r) { return r.id === id; });
  if (row) row.n_comments = n;
  const tr = document.querySelector('#gridBody tr[data-row="' + id + '"]');
  if (!tr) return;
  const cell = tr.querySelector(".cre-badge").parentNode;
  let badge = cell.querySelector(".cmt-badge");
  if (n > 0) {
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "cmt-badge";
      cell.appendChild(document.createTextNode(" "));
      cell.appendChild(badge);
    }
    badge.innerHTML = "&#128172; " + n;
  } else if (badge) {
    badge.remove();
  }
}

async function fetchRowImages(id) {
  const resp = await apiFetch("../api/campaigns/" + CID + "/rows/" + id + "/images");
  return (await resp.json()).images;
}

async function loadRowImages(id) {
  const images = await fetchRowImages(id);
  renderGallery(images, id);
  bumpImageCount(id, images.length);
}

function renderGallery(images, id) {
  const g = document.getElementById("rowGallery");
  g.innerHTML = "";
  if (!images.length) { g.innerHTML = '<p class="empty">Aucune capture rattachée.</p>'; return; }
  images.forEach(function (im) {
    const url = "../api/rows/images/" + im.id;
    const div = document.createElement("div");
    div.className = "thumb";
    div.innerHTML =
      '<div class="thumb-img">' +
      '<img src="' + url + '" data-url="' + url + '">' +
      '<button class="thumb-del" data-del="' + im.id + '" title="Supprimer la capture" aria-label="Supprimer la capture">&times;</button>' +
      "</div>" +
      '<div class="cap">' + esc(im.caption || im.original_name) + "</div>" +
      '<div class="meta">' + esc(im.uploaded_by) + " · " + esc(im.uploaded_at.replace("T", " ")) + "</div>";
    g.appendChild(div);
  });
  g.querySelectorAll("img").forEach(function (img) {
    img.addEventListener("click", function () { openViewer(img.getAttribute("data-url")); });
  });
  g.querySelectorAll("[data-del]").forEach(function (b) {
    b.addEventListener("click", function () { deleteRowImage(b.getAttribute("data-del"), id); });
  });
}

function bumpImageCount(id, n) {
  const row = GRID.rows.find(function (r) { return r.id === id; });
  if (row) row.n_images = n;
  const tr = document.querySelector('#gridBody tr[data-row="' + id + '"]');
  if (tr) {
    const badge = tr.querySelector(".cre-badge");
    if (badge) { badge.innerHTML = "&#128206; " + n; badge.classList.toggle("has", n > 0); }
  }
}

async function uploadRowImage() {
  const input = document.getElementById("rowImgInput");
  const file = input.files[0];
  if (!file) { alert("Sélectionnez une image ou collez une capture (Ctrl-V)."); return; }
  await sendRowImage(file);
}

async function sendRowImage(file) {
  const id = ROW_MODAL_ID;
  if (!id) return;
  const fd = new FormData();
  fd.append("file", file);
  fd.append("caption", document.getElementById("rowImgCaption").value.trim());
  fd.append("user_name", getUser());
  const resp = await apiFetch("../api/campaigns/" + CID + "/rows/" + id + "/images", { method: "POST", body: fd });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || "Échec de l'ajout."); return; }
  document.getElementById("rowImgInput").value = "";
  document.getElementById("rowImgCaption").value = "";
  renderGallery(data.images, id);
  bumpImageCount(id, data.images.length);
}

async function deleteRowImage(imgId, rowId) {
  if (!confirm("Supprimer cette capture ?")) return;
  await apiFetch("../api/rows/images/" + imgId, { method: "DELETE" });
  loadRowImages(rowId);
}

async function loadRowHistory(id) {
  const resp = await apiFetch("../api/campaigns/" + CID + "/rows/" + id + "/history");
  const data = await resp.json();
  const ul = document.getElementById("rowHistory");
  ul.innerHTML = "";
  if (!data.entries.length) { ul.innerHTML = '<li class="empty">Aucune modification tracée.</li>'; return; }
  data.entries.forEach(function (e) {
    const li = document.createElement("li");
    const isValidation = e.field.indexOf("validation") === 0;
    const change = isValidation
      ? esc(e.new_value)
      : (fmtHistVal(e.old_value) + " &rarr; " + fmtHistVal(e.new_value));
    const who = esc(e.user_name) + (e.user_profile ? " (" + esc(e.user_profile) + ")" : "");
    li.innerHTML = '<span class="atime">' + esc(e.created_at.replace("T", " ")) + "</span> " +
      "<b>" + who + "</b> · <i>" + esc(histFieldLabel(e.field)) + "</i> — " + change;
    ul.appendChild(li);
  });
}

function fmtHistVal(v) {
  return (v === null || v === "" || v === undefined) ? "(vide)" : esc(v);
}

// Libelle lisible du champ trace dans l'historique fin.
const HIST_FIELD_LABELS = {
  valeur: "valeur", validation_tma: "validation TMA", validation_rte: "validation DSIT",
  excel_present: "présent Excel",
  creation: "création", tarif: "tarif", operande: "opérande", cle: "clé",
};
function histFieldLabel(field) {
  return HIST_FIELD_LABELS[field] || field;
}

function openViewer(url) {
  document.getElementById("imgViewerImg").src = url;
  document.getElementById("imgViewer").style.display = "flex";
}

async function saveGrid() {
  const updates = GRID.rows.map(function (r) { return { id: r.id, new_value: r.new_value }; });
  const status = document.getElementById("gridStatus");
  status.textContent = "Enregistrement...";
  const resp = await apiFetch("../api/campaigns/" + CID + "/grid", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates: updates }),
  });
  const data = await resp.json();
  if (!resp.ok) { status.textContent = data.error || "Erreur."; return; }
  status.textContent = data.updated + " ligne(s) enregistrée(s) à " + new Date().toLocaleTimeString("fr-FR");
}

function exportGrid() {
  window.location.href = "../api/campaigns/" + CID + "/grid/export";
}

// ---------------------------------------------------------------------------
// Etape 3 : procedure de saisie SAP (EKDI / EPREIH)
// ---------------------------------------------------------------------------
// Apercu genere a la volee a partir des valeurs saisies a l'etape 2. La saisie
// dans SAP est manuelle ; l'app n'ecrit jamais en base. Le telechargement (.txt)
// est trace cote serveur. Chaque etape rappelle l'OT (recupere dans Ocas).
async function loadScripts() {
  const status = document.getElementById("scriptsStatus");
  status.textContent = "Génération…";
  // Pre-remplit le champ OT avec la valeur enregistree sur la campagne.
  if (CAMPAIGN) document.getElementById("campOt").value = CAMPAIGN.sap_ot || "";
  const resp = await apiFetch("../api/campaigns/" + CID + "/scripts");
  const data = await resp.json();
  if (!resp.ok) { status.textContent = data.error || "Erreur."; return; }
  renderScript("scrEkdiSql", "scrEkdiCount", data.ekdi);
  renderScript("scrEpreihSql", "scrEpreihCount", data.epreih);
  status.textContent = (data.ot ? "OT " + data.ot + " · " : "OT non renseigné · ")
    + "à jour avec la grille saisie · " + new Date().toLocaleTimeString("fr-FR");
}

function renderScript(preId, countId, block) {
  document.getElementById(preId).textContent = block.text;
  document.getElementById(countId).textContent =
    block.n_rows + " ligne(s) · " + block.n_steps + " modif. SAP";
}

function downloadScript(kind) {
  window.location.href = "../api/campaigns/" + CID + "/scripts/download?kind=" + kind;
}

// Enregistre l'OT de la campagne (saisi depuis Ocas) puis regenere les procedures.
async function saveOt() {
  const ot = document.getElementById("campOt").value.trim();
  const status = document.getElementById("otStatus");
  status.textContent = "Enregistrement…";
  const resp = await apiFetch("../api/campaigns/" + CID + "/ot", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ot: ot }),
  });
  const data = await resp.json();
  if (!resp.ok) { status.textContent = data.error || "Erreur."; return; }
  if (CAMPAIGN) CAMPAIGN.sap_ot = data.sap_ot;
  status.textContent = data.sap_ot ? "OT enregistré." : "OT effacé.";
  loadScripts();
}

async function copyScript(targetId, btn) {
  const txt = document.getElementById(targetId).textContent;
  try {
    await navigator.clipboard.writeText(txt);
    const old = btn.textContent;
    btn.textContent = "Copié ✓";
    setTimeout(function () { btn.textContent = old; }, 1500);
  } catch (e) {
    alert("Copie impossible : sélectionnez le texte manuellement.");
  }
}

// ---------------------------------------------------------------------------
// Ajout manuel d'une ligne
// ---------------------------------------------------------------------------
const ADD_ROW_FIELDS = {
  arSection: "section", arTarif: "tarif_label", arTarifEkdi: "tarif_ekdi",
  arGrp: "grp", arOperande: "operande", arCle: "cle",
};

function openAddRow() {
  Object.keys(ADD_ROW_FIELDS).forEach(function (id) { document.getElementById(id).value = ""; });
  document.getElementById("addRowStatus").textContent = "";
  document.getElementById("addRowModal").style.display = "flex";
  document.getElementById("arOperande").focus();
}

async function saveNewRow() {
  const body = {};
  Object.keys(ADD_ROW_FIELDS).forEach(function (id) {
    body[ADD_ROW_FIELDS[id]] = document.getElementById(id).value.trim();
  });
  const status = document.getElementById("addRowStatus");
  if (!body.operande) { status.textContent = "L'opérande est obligatoire."; return; }
  status.textContent = "Ajout...";
  const resp = await apiFetch("../api/campaigns/" + CID + "/rows", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) { status.textContent = data.error || "Erreur."; return; }
  document.getElementById("addRowModal").style.display = "none";
  await loadGrid();
}

// ---------------------------------------------------------------------------
// Corbeille de la grille (soft-delete ligne par ligne)
// ---------------------------------------------------------------------------
async function trashRow() {
  const id = ROW_MODAL_ID;
  const row = GRID.rows.find(function (r) { return r.id === id; });
  if (!row) return;
  const label = (row.tarif_label || "") + " · " + (row.operande || "") + (row.cle ? " · " + row.cle : "");
  if (!confirm("Mettre cette ligne à la corbeille ?\n\n" + label +
    "\n\nElle sera masquée de la grille (et des étapes 3/4 et de l'export) mais conservée et restaurable.")) return;
  const status = document.getElementById("rowTrashStatus");
  status.textContent = "Mise à la corbeille...";
  const resp = await apiFetch("../api/campaigns/" + CID + "/rows/" + id, { method: "DELETE" });
  const data = await resp.json();
  if (!resp.ok) { status.textContent = data.error || "Erreur."; return; }
  document.getElementById("rowModal").style.display = "none";
  await loadGrid();
}

async function openTrash() {
  document.getElementById("trashModal").style.display = "flex";
  await renderTrash();
}

async function renderTrash() {
  const body = document.getElementById("trashBody");
  body.innerHTML = '<tr><td colspan="7" class="hint">Chargement…</td></tr>';
  const resp = await apiFetch("../api/campaigns/" + CID + "/rows/trash");
  const data = await resp.json();
  const rows = (data && data.rows) || [];
  body.innerHTML = "";
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="7" class="hint">La corbeille est vide.</td></tr>';
    return;
  }
  rows.forEach(function (r) {
    const tr = document.createElement("tr");
    let who = esc(r.deleted_by || "—");
    if (r.deleted_profile) who += " (" + esc(r.deleted_profile) + ")";
    if (r.deleted_at) who += " · " + esc(r.deleted_at.replace("T", " ").slice(0, 16));
    tr.innerHTML =
      '<td class="num">' + (r.sort_order || "") + "</td>" +
      "<td>" + esc(r.section || "") + "</td>" +
      "<td>" + esc(r.tarif_label || "") + "</td>" +
      "<td>" + esc(r.operande || "") + "</td>" +
      "<td>" + esc(r.cle || "") + "</td>" +
      "<td>" + who + "</td>" +
      '<td><button class="secondary" data-restore="' + r.id + '">Restaurer</button></td>';
    body.appendChild(tr);
  });
  body.querySelectorAll("[data-restore]").forEach(function (b) {
    b.addEventListener("click", function () { restoreRow(parseInt(b.getAttribute("data-restore"), 10)); });
  });
}

async function restoreRow(id) {
  const resp = await apiFetch("../api/campaigns/" + CID + "/rows/" + id + "/restore", { method: "POST" });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || "Erreur."); return; }
  await loadGrid();
  await renderTrash();
}

// ---------------------------------------------------------------------------
// Comparaison EKDI / EPREIH
// ---------------------------------------------------------------------------
const CMP_LABELS = { ok: "Conforme", diff: "Écart", missing: "Absent extract", ambiguous: "Ambigu" };
let CMP_RESULTS = [];   // dernier resultat de comparaison (pour la validation humaine)
let CMP_KIND = "";

// Cellule de validation humaine (chef de projet DSIT) d'une valeur en base.
function cmpValCell(r) {
  const allowed = canValidate("rte");
  let meta = "";
  if (r.validated && r.validated_by) {
    meta = '<div class="valmeta">' + esc(r.validated_by) +
      (r.validated_at ? " · " + esc(r.validated_at.replace("T", " ").slice(0, 16)) : "") + "</div>";
    if (r.stale) {
      meta += '<div class="valstale" title="La valeur en base a changé depuis la validation">' +
        "⚠ revalider (validé : " + fmtNum(r.validated_found) + ")</div>";
    }
  }
  return '<td class="valcol"><input type="checkbox" class="cmp-check" data-id="' + r.row_id + '"' +
    (r.validated ? " checked" : "") + (allowed ? "" : " disabled") +
    (allowed ? "" : ' title="Réservé au profil Chef de projet DSIT"') + ">" + meta + "</td>";
}

function cmpRowHtml(r) {
  return '<td class="sec">' + r.section + "</td>" +
    "<td>" + (r.tarif_label || "") + "</td>" +
    "<td>" + esc(r.operande) + (r.cle ? ' <span class="cmp-cle">' + esc(r.cle) + "</span>" : "") + "</td>" +
    '<td class="num">' + fmtNum(r.saisie) + "</td>" +
    '<td class="num">' + (r.extract === null || r.extract === undefined ? "—" : fmtNum(r.extract)) + "</td>" +
    "<td>" + r.match_on + "</td>" +
    '<td><span class="badge badge-cmp-' + r.status + '">' + CMP_LABELS[r.status] + "</span></td>" +
    cmpValCell(r);
}

function renderCmpCounts() {
  const validated = CMP_RESULTS.filter(function (r) { return r.validated; }).length;
  document.getElementById("cmpValidated").textContent = validated;
  document.getElementById("cmpTotal").textContent = CMP_RESULTS.length;
}

async function runCompare(kind, inputId) {
  const input = document.getElementById(inputId);
  const file = input.files[0];
  if (!file) { alert("Sélectionnez d'abord un extract."); return; }
  // On stocke aussi l'extract cote serveur pour tracabilite.
  const up = new FormData();
  up.append("kind", kind); up.append("file", file); up.append("user_name", getUser());
  await apiFetch("../api/campaigns/" + CID + "/files", { method: "POST", body: up });

  const fd = new FormData();
  fd.append("kind", kind); fd.append("file", file);
  const resp = await apiFetch("../api/campaigns/" + CID + "/compare", { method: "POST", body: fd });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || "Erreur."); return; }
  CMP_KIND = data.kind;
  document.getElementById("cmpSummary").style.display = "flex";
  document.getElementById("cmpValHint").style.display = "block";
  document.getElementById("cmpOk").textContent = data.counts.ok;
  document.getElementById("cmpDiff").textContent = data.counts.diff;
  document.getElementById("cmpMissing").textContent = data.counts.missing;
  document.getElementById("cmpAmbig").textContent = data.counts.ambiguous;
  data.results.sort(function (a, b) {
    const order = { diff: 0, missing: 1, ambiguous: 2, ok: 3 };
    return order[a.status] - order[b.status];
  });
  CMP_RESULTS = data.results;
  const body = document.getElementById("cmpBody");
  body.innerHTML = "";
  CMP_RESULTS.forEach(function (r) {
    const tr = document.createElement("tr");
    tr.className = "cmp-" + r.status + (r.validated ? " cmp-validated" : "");
    tr.setAttribute("data-row", r.row_id);
    tr.innerHTML = cmpRowHtml(r);
    body.appendChild(tr);
  });
  body.querySelectorAll(".cmp-check").forEach(function (chk) {
    chk.addEventListener("change", function () { onCmpValidate(chk); });
  });
  renderCmpCounts();
  loadCampaign();
}

async function onCmpValidate(chk) {
  const rid = parseInt(chk.getAttribute("data-id"), 10);
  const validated = chk.checked;
  const r = CMP_RESULTS.find(function (x) { return x.row_id === rid; });
  const resp = await apiFetch("../api/campaigns/" + CID + "/compare/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      row_id: rid, kind: CMP_KIND, validated: validated,
      expected: r ? r.saisie : null, found: r ? r.extract : null,
      status: r ? r.status : null,
    }),
  });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || "Erreur."); chk.checked = !validated; return; }
  if (r) {
    r.validated = validated;
    r.validated_by = data.by || null;
    r.validated_at = data.at || null;
    r.validated_found = validated ? r.extract : undefined;
    r.stale = false;
  }
  const tr = document.querySelector('#cmpBody tr[data-row="' + rid + '"]');
  if (tr) {
    tr.classList.toggle("cmp-validated", validated);
    tr.innerHTML = cmpRowHtml(r);
    tr.querySelector(".cmp-check").addEventListener("change", function () {
      onCmpValidate(tr.querySelector(".cmp-check"));
    });
  }
  renderCmpCounts();
  loadCampaign();
}

// ---------------------------------------------------------------------------
// Verifications metier
// ---------------------------------------------------------------------------
function renderVerifs() {
  if (!CAMPAIGN) return;
  const ul = document.getElementById("verifList");
  if (!ul) return;
  ul.innerHTML = "";
  CAMPAIGN.verifications.slice().reverse().forEach(function (v) {
    const li = document.createElement("li");
    li.className = "verif-item verif-" + v.verdict;
    const role = v.role === "tma" ? "TMA" : "Chef de projet DSIT";
    const who = v.verifier + (v.verifier_profile ? " (" + v.verifier_profile + ")" : "");
    li.innerHTML = '<span class="vbadge vbadge-' + v.verdict + '">' + (v.verdict === "ok" ? "VALIDÉ" : "REJETÉ") + "</span> " +
      "<b>" + role + "</b> — " + who + " le " + v.created_at.replace("T", " ") +
      (v.comment ? '<div class="vcomment">' + v.comment + "</div>" : "");
    ul.appendChild(li);
  });
}

async function submitVerif(role, verdict, comment) {
  const resp = await apiFetch("../api/campaigns/" + CID + "/verifications", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role: role, verdict: verdict, comment: comment }),
  });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || "Erreur."); return; }
  CAMPAIGN = data;
  renderVerifs();
}

// ---------------------------------------------------------------------------
// Journal d'audit
// ---------------------------------------------------------------------------
async function loadAudit() {
  const resp = await apiFetch("../api/campaigns/" + CID + "/audit");
  const data = await resp.json();
  const ul = document.getElementById("auditList");
  ul.innerHTML = "";
  data.entries.forEach(function (e) {
    const li = document.createElement("li");
    const who = e.user_name + (e.user_profile ? " (" + e.user_profile + ")" : "");
    li.innerHTML = '<span class="atime">' + e.created_at.replace("T", " ") + "</span> " +
      "<b>" + who + "</b> · <i>" + e.action + "</i>" +
      (e.detail ? " — " + e.detail : "");
    ul.appendChild(li);
  });
}

async function setStatus(status) {
  const resp = await apiFetch("../api/campaigns/" + CID + "/status", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: status }),
  });
  if (resp.ok) loadCampaign();
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", function () {
  const ready = initUser();
  function boot() { loadCampaign(); showPanel("pdf"); }
  if (ready) boot();
  document.addEventListener("user-ready", boot);

  document.querySelectorAll(".step").forEach(function (s) {
    s.addEventListener("click", function () { showPanel(s.getAttribute("data-step")); });
  });
  document.querySelectorAll("input[type=file][data-kind]").forEach(function (inp) {
    inp.addEventListener("change", function () { uploadFile(inp.getAttribute("data-kind"), inp); });
  });
  document.querySelectorAll(".advance").forEach(function (b) {
    b.addEventListener("click", function () {
      if (b.getAttribute("data-next")) { setStatus(b.getAttribute("data-next")); showPanel(b.getAttribute("data-next")); }
      if (b.getAttribute("data-status")) setStatus(b.getAttribute("data-status"));
    });
  });
  document.getElementById("btnSaveGrid").addEventListener("click", saveGrid);
  document.getElementById("btnExportGrid").addEventListener("click", exportGrid);
  document.getElementById("btnRefreshScripts").addEventListener("click", loadScripts);
  document.getElementById("btnSaveOt").addEventListener("click", saveOt);
  document.getElementById("campOt").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); saveOt(); }
  });
  document.querySelectorAll(".script-dl").forEach(function (b) {
    b.addEventListener("click", function () { downloadScript(b.getAttribute("data-kind")); });
  });
  document.querySelectorAll(".script-copy").forEach(function (b) {
    b.addEventListener("click", function () { copyScript(b.getAttribute("data-target"), b); });
  });
  document.getElementById("btnAddRow").addEventListener("click", openAddRow);
  document.getElementById("addRowSave").addEventListener("click", saveNewRow);
  document.getElementById("addRowClose").addEventListener("click", function () {
    document.getElementById("addRowModal").style.display = "none";
  });
  document.getElementById("addRowModal").addEventListener("click", function (e) {
    if (e.target.id === "addRowModal") e.target.style.display = "none";
  });
  document.getElementById("gridFilter").addEventListener("input", function () { if (GRID) renderGrid(); });
  document.getElementById("gridOnlyEmpty").addEventListener("change", function () { if (GRID) renderGrid(); });
  document.getElementById("gridOnlyTodo").addEventListener("change", function () { if (GRID) renderGrid(); });
  document.getElementById("gridOnlyAbsentExcel").addEventListener("change", function () { if (GRID) renderGrid(); });
  document.getElementById("btnTrash").addEventListener("click", openTrash);
  document.getElementById("trashClose").addEventListener("click", function () {
    document.getElementById("trashModal").style.display = "none";
  });
  document.getElementById("trashModal").addEventListener("click", function (e) {
    if (e.target.id === "trashModal") e.target.style.display = "none";
  });
  document.getElementById("rowTrashBtn").addEventListener("click", trashRow);
  document.getElementById("btnAddCampCmt").addEventListener("click", addCampComment);
  document.getElementById("btnYearsAll").addEventListener("click", function () { if (GRID) setAllYears(true); });
  document.getElementById("btnYearsNone").addEventListener("click", function () { if (GRID) setAllYears(false); });

  // Modale detail ligne
  document.getElementById("rowModalValue").addEventListener("input", onModalValueEdit);
  document.getElementById("rowModalValue").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); saveRowValue(); }
  });
  document.getElementById("rowModalSave").addEventListener("click", saveRowValue);
  document.getElementById("rowModalEdit").addEventListener("click", function () { setModalEditable(true); });
  MODAL_VAL_FIELDS.forEach(function (f) {
    document.getElementById(f.chk).addEventListener("change", function () { onModalValidate(this); });
  });
  document.getElementById("rowIdEdit").addEventListener("click", function () { setIdentityEditable(true); });
  document.getElementById("rowIdCancel").addEventListener("click", function () {
    setIdentityEditable(false);
    document.getElementById("rowIdStatus").textContent = "";
  });
  document.getElementById("rowIdSave").addEventListener("click", saveRowIdentity);
  document.getElementById("rowModalCommentSave").addEventListener("click", addRowComment);
  document.getElementById("rowImgUpload").addEventListener("click", uploadRowImage);
  document.getElementById("rowModalClose").addEventListener("click", function () {
    document.getElementById("rowModal").style.display = "none";
  });
  document.getElementById("rowModal").addEventListener("click", function (e) {
    if (e.target.id === "rowModal") e.target.style.display = "none";
  });
  document.getElementById("imgViewerClose").addEventListener("click", function () {
    document.getElementById("imgViewer").style.display = "none";
  });
  document.getElementById("imgViewer").addEventListener("click", function (e) {
    if (e.target.id === "imgViewer") e.target.style.display = "none";
  });
  // Echap ferme la modale ouverte (visionneuse d'abord, sinon detail ligne).
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    const viewer = document.getElementById("imgViewer");
    const modal = document.getElementById("rowModal");
    const addModal = document.getElementById("addRowModal");
    if (viewer.style.display === "flex") { viewer.style.display = "none"; }
    else if (addModal.style.display === "flex") { addModal.style.display = "none"; }
    else if (modal.style.display === "flex") { modal.style.display = "none"; }
  });
  // Ctrl-V : coller une capture d'ecran directement dans la modale detail ligne.
  document.addEventListener("paste", function (e) {
    if (document.getElementById("rowModal").style.display !== "flex") return;
    const items = (e.clipboardData || {}).items || [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].type && items[i].type.indexOf("image") === 0) {
        const file = items[i].getAsFile();
        if (file) { e.preventDefault(); sendRowImage(file); }
        return;
      }
    }
  });
  document.querySelectorAll(".cmp-btn").forEach(function (b) {
    b.addEventListener("click", function () { runCompare(b.getAttribute("data-kind"), b.getAttribute("data-input")); });
  });
  document.querySelectorAll(".verif-form").forEach(function (form) {
    const role = form.getAttribute("data-role");
    form.querySelectorAll("button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const comment = form.querySelector(".verif-comment").value.trim();
        submitVerif(role, btn.getAttribute("data-verdict"), comment);
        form.querySelector(".verif-comment").value = "";
      });
    });
  });
});
