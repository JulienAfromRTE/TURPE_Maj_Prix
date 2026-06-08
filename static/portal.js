"use strict";

const STEP_LABELS = {
  pdf: "Délibérations CRE",
  grid: "Saisie grille",
  verif: "Vérification",
  injecte: "Injecté SAP",
  cloture: "Clôturée",
};

function statusBadge(status) {
  const label = STEP_LABELS[status] || status;
  return '<span class="badge badge-status badge-' + status + '">' + label + "</span>";
}

function verifSummary(c) {
  const out = [];
  ["tma", "rte"].forEach(function (role) {
    const last = c.verifications.filter(function (v) { return v.role === role; }).slice(-1)[0];
    const lbl = role === "tma" ? "TMA" : "DSIT";
    if (!last) out.push('<span class="vchip vchip-pending">' + lbl + " : en attente</span>");
    else out.push('<span class="vchip vchip-' + last.verdict + '">' + lbl + " : " + (last.verdict === "ok" ? "validé" : "rejeté") + "</span>");
  });
  return out.join(" ");
}

function campaignInner(c) {
  const pct = c.n_rows ? Math.round((c.n_filled / c.n_rows) * 100) : 0;
  return (
    '<div class="camp-top">' +
      '<div class="camp-name">' + c.name + "</div>" +
      statusBadge(c.status) +
    "</div>" +
    '<div class="camp-meta">Période <b>' + c.period_label + "</b> &middot; effet " + c.effective_date + "</div>" +
    '<div class="camp-bar"><span style="width:' + pct + '%"></span></div>' +
    '<div class="camp-sub">' + c.n_filled + " / " + c.n_rows + " lignes saisies &middot; " + c.files.length + " fichier(s)</div>" +
    '<div class="camp-verifs">' + verifSummary(c) + "</div>"
  );
}

async function loadCampaigns() {
  const resp = await apiFetch("api/campaigns");
  const data = await resp.json();
  const list = document.getElementById("campaignList");
  const empty = document.getElementById("emptyState");
  list.innerHTML = "";
  if (!data.campaigns.length) { empty.style.display = "block"; }
  else { empty.style.display = "none"; }
  data.campaigns.forEach(function (c) {
    const card = document.createElement("a");
    card.className = "camp-card";
    card.href = "campaign/" + c.id;
    card.innerHTML = campaignInner(c) +
      '<div class="camp-foot"><span>Créée par ' + c.created_by + " le " + c.created_at.slice(0, 10) + "</span>" +
        '<button type="button" class="camp-del" title="Mettre à la corbeille">Corbeille</button></div>';
    card.querySelector(".camp-del").addEventListener("click", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      startDelete(c);
    });
    list.appendChild(card);
  });
  refreshTrashCount();
}

// ---------------------------------------------------------------------------
// Corbeille (soft-delete : aucune campagne n'est jamais supprimee physiquement)
// ---------------------------------------------------------------------------
async function fetchTrash() {
  const resp = await apiFetch("api/campaigns/trash");
  const data = await resp.json();
  return data.campaigns || [];
}

async function refreshTrashCount() {
  const items = await fetchTrash();
  const badge = document.getElementById("trashCount");
  badge.textContent = items.length;
  badge.style.display = items.length ? "inline-block" : "none";
  return items;
}

async function loadTrash() {
  const items = await refreshTrashCount();
  const list = document.getElementById("trashList");
  const empty = document.getElementById("trashEmpty");
  list.innerHTML = "";
  empty.style.display = items.length ? "none" : "block";
  items.forEach(function (c) {
    const card = document.createElement("div");
    card.className = "camp-card camp-card-trashed";
    card.innerHTML = campaignInner(c) +
      '<div class="camp-foot"><span>Corbeille par ' + (c.deleted_by || "?") +
        " le " + (c.deleted_at ? c.deleted_at.slice(0, 10) : "") + "</span>" +
        '<button type="button" class="camp-restore" title="Restaurer la campagne">Restaurer</button></div>';
    card.querySelector(".camp-restore").addEventListener("click", function () {
      restoreCampaign(c);
    });
    list.appendChild(card);
  });
}

function showTrash() {
  document.getElementById("trashSection").style.display = "block";
  loadTrash();
}

function hideTrash() {
  document.getElementById("trashSection").style.display = "none";
}

async function restoreCampaign(c) {
  if (!window.confirm("Restaurer la campagne « " + c.name + " » dans la liste active ?")) {
    return;
  }
  const resp = await apiFetch("api/campaigns/" + c.id + "/restore", { method: "POST" });
  if (!resp.ok) {
    const data = await resp.json().catch(function () { return {}; });
    window.alert(data.error || "Erreur lors de la restauration.");
    return;
  }
  loadTrash();
  loadCampaigns();
}

// ---------------------------------------------------------------------------
// Mise a la corbeille (modale avec recopie de l'intitule + double confirmation)
// ---------------------------------------------------------------------------
let delTarget = null;

function refreshDeleteState() {
  const typed = document.getElementById("delConfirmInput").value.trim();
  const nameOk = delTarget && typed === delTarget.name;
  const ack = document.getElementById("delAck1").checked;
  document.getElementById("btnConfirmDelete").disabled = !(nameOk && ack);
}

function startDelete(c) {
  // 1er garde-fou : confirmation native avant meme d'ouvrir la modale detaillee.
  if (!window.confirm("Mettre la campagne « " + c.name + " » à la corbeille ?\n\nElle sera retirée de la liste active mais restera récupérable depuis la corbeille.")) {
    return;
  }
  delTarget = c;
  document.getElementById("delCampName").textContent = c.name;
  document.getElementById("delCampMeta").textContent =
    "(période " + c.period_label + ", effet " + c.effective_date + ", " + c.n_filled + " lignes saisies)";
  document.getElementById("delConfirmInput").value = "";
  document.getElementById("delAck1").checked = false;
  document.getElementById("delStatus").textContent = "";
  refreshDeleteState();
  document.getElementById("deleteModal").style.display = "flex";
  document.getElementById("delConfirmInput").focus();
}

function closeDelete() {
  document.getElementById("deleteModal").style.display = "none";
  delTarget = null;
}

async function confirmDelete() {
  if (!delTarget) return;
  const typed = document.getElementById("delConfirmInput").value.trim();
  if (typed !== delTarget.name) {
    document.getElementById("delStatus").textContent = "L'intitulé recopié ne correspond pas.";
    return;
  }
  // Dernier garde-fou avant l'appel reseau.
  if (!window.confirm("Confirmer la mise a la corbeille de « " + delTarget.name + " » ?")) {
    return;
  }
  document.getElementById("delStatus").textContent = "Mise a la corbeille en cours...";
  const resp = await apiFetch("api/campaigns/" + delTarget.id, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm_name: typed }),
  });
  const data = await resp.json().catch(function () { return {}; });
  if (!resp.ok) {
    document.getElementById("delStatus").textContent = data.error || "Erreur lors de la mise a la corbeille.";
    return;
  }
  closeDelete();
  loadCampaigns();
  if (document.getElementById("trashSection").style.display !== "none") loadTrash();
}

function openNewModal() {
  document.getElementById("newModal").style.display = "flex";
  document.getElementById("ncName").focus();
}

async function createCampaign() {
  const name = document.getElementById("ncName").value.trim();
  const period = document.getElementById("ncPeriod").value.trim();
  const eff = document.getElementById("ncDate").value;
  const status = document.getElementById("ncStatus");
  if (!name || !period || !eff) { status.textContent = "Tous les champs sont requis."; return; }
  status.textContent = "Creation en cours...";
  const resp = await apiFetch("api/campaigns", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name, period_label: period, effective_date: eff }),
  });
  const data = await resp.json();
  if (!resp.ok) { status.textContent = data.error || "Erreur."; return; }
  window.location.href = "campaign/" + data.id;
}

document.addEventListener("DOMContentLoaded", function () {
  const ready = initUser();
  if (ready) loadCampaigns();
  document.addEventListener("user-ready", loadCampaigns);

  document.getElementById("btnNew").addEventListener("click", openNewModal);
  document.getElementById("btnCancelNew").addEventListener("click", function () {
    document.getElementById("newModal").style.display = "none";
  });
  document.getElementById("btnCreateNew").addEventListener("click", createCampaign);

  document.getElementById("btnTrash").addEventListener("click", showTrash);
  document.getElementById("btnHideTrash").addEventListener("click", hideTrash);

  document.getElementById("btnCancelDelete").addEventListener("click", closeDelete);
  document.getElementById("btnConfirmDelete").addEventListener("click", confirmDelete);
  document.getElementById("delConfirmInput").addEventListener("input", refreshDeleteState);
  document.getElementById("delAck1").addEventListener("change", refreshDeleteState);
});
