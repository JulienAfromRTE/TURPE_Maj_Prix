"use strict";

let lastResults = [];

function fmtNum(v) {
  if (v === null || v === undefined) return "";
  const abs = Math.abs(v);
  const dec = abs !== 0 && abs < 1 ? 5 : 2;
  return Number(v).toLocaleString("fr-FR", { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

function fmtPct(p) {
  if (p === null || p === undefined) return "";
  return (p * 100).toLocaleString("fr-FR", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + " %";
}

function activeStatuses() {
  const out = [];
  document.querySelectorAll(".fStatus").forEach(function (c) {
    if (c.checked) out.push(c.value);
  });
  return out;
}

function render() {
  const tbody = document.querySelector("#tbl tbody");
  tbody.innerHTML = "";
  const statuses = activeStatuses();
  const onlyAberrant = document.getElementById("fAberrant").checked;
  const labels = { changed: "Modifiee", new: "Nouvelle", removed: "Supprimee", unchanged: "Inchangee" };

  lastResults
    .filter(function (r) {
      if (onlyAberrant && !r.aberrant) return false;
      return statuses.indexOf(r.status) !== -1;
    })
    .forEach(function (r) {
      const tr = document.createElement("tr");
      tr.className = "row-" + r.status + (r.aberrant ? " row-aberrant" : "");
      tr.innerHTML =
        "<td>" + r.tarif + "</td>" +
        "<td>" + r.grp + "</td>" +
        "<td>" + r.operande + (r.is_p ? ' <span class="tag">_P 01.07</span>' : "") + "</td>" +
        '<td class="num">' + fmtNum(r.valeur_old) + "</td>" +
        '<td class="num">' + fmtNum(r.valeur_new) + "</td>" +
        '<td class="num">' + (r.aberrant ? "&#9888; " : "") + fmtPct(r.pct) + "</td>" +
        "<td>" + (r.valide_du || "") + "</td>" +
        '<td><span class="badge badge-' + r.status + '">' + labels[r.status] + "</span></td>";
      tbody.appendChild(tr);
    });
}

async function runReconcile() {
  const fNew = document.getElementById("fileNew").files[0];
  const fOld = document.getElementById("fileOld").files[0];
  const ref = document.getElementById("refDate").value;
  const status = document.getElementById("status");
  if (!fNew || !fOld) {
    status.textContent = "Selectionnez les deux exports EKDI.";
    return;
  }
  status.textContent = "Calcul en cours...";
  const fd = new FormData();
  fd.append("file_new", fNew);
  fd.append("file_old", fOld);
  fd.append("ref_date", ref);
  try {
    const resp = await fetch("api/reconcile", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) {
      status.textContent = data.error || "Erreur.";
      return;
    }
    lastResults = data.results;
    document.getElementById("cChanged").textContent = data.counts.changed;
    document.getElementById("cNew").textContent = data.counts.new;
    document.getElementById("cRemoved").textContent = data.counts.removed;
    document.getElementById("cUnchanged").textContent = data.counts.unchanged;
    document.getElementById("cAberrant").textContent = data.counts.aberrant;
    document.getElementById("summary").style.display = "block";
    status.textContent = data.results.length + " cles comparees (date d'effet " + data.ref_date + ", _P au " + data.p_ref_date + ").";
    render();
  } catch (e) {
    status.textContent = "Erreur reseau ou fichier illisible.";
  }
}

async function exportXlsx() {
  const resp = await fetch("api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ results: lastResults.filter(function (r) { return r.status !== "unchanged"; }) }),
  });
  const blob = await resp.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "ecarts_EKDI.xlsx";
  a.click();
  URL.revokeObjectURL(a.href);
}

document.getElementById("btnRun").addEventListener("click", runReconcile);
document.getElementById("btnExport").addEventListener("click", exportXlsx);
document.querySelectorAll(".fStatus").forEach(function (c) { c.addEventListener("change", render); });
document.getElementById("fAberrant").addEventListener("change", render);
