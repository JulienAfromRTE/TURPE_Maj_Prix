"use strict";

// Identite utilisateur partagee (portail + campagne) : nom + profil (TMA | Chef de projet DSIT).
const USER_KEY = "turpe_user";
const PROFILE_KEY = "turpe_profile";

function getUser() {
  return localStorage.getItem(USER_KEY) || "";
}

function getProfile() {
  return localStorage.getItem(PROFILE_KEY) || "";
}

function renderIdentity() {
  const el = document.getElementById("userName");
  if (!el) return;
  const name = getUser();
  el.textContent = name ? (name + (getProfile() ? " · " + getProfile() : "")) : "—";
}

function setUser(name) {
  localStorage.setItem(USER_KEY, name);
  renderIdentity();
}

function setProfile(profile) {
  localStorage.setItem(PROFILE_KEY, profile);
  renderIdentity();
}

// fetch avec en-tetes X-User-Name / X-User-Profile systematiques.
async function apiFetch(url, options) {
  options = options || {};
  options.headers = Object.assign({}, options.headers, {
    "X-User-Name": getUser(),
    "X-User-Profile": getProfile(),
  });
  return fetch(url, options);
}

function openUserModal() {
  const modal = document.getElementById("userModal");
  const input = document.getElementById("userInput");
  if (!modal) return;
  input.value = getUser();
  const select = document.getElementById("userProfile");
  if (select) select.value = getProfile();
  modal.style.display = "flex";
  input.focus();
}

function initUser() {
  renderIdentity();
  const saveBtn = document.getElementById("btnSaveUser");
  const input = document.getElementById("userInput");
  const select = document.getElementById("userProfile");
  if (saveBtn) {
    saveBtn.addEventListener("click", function () {
      const name = (input.value || "").trim();
      const profile = select ? select.value : "";
      if (!name) { input.focus(); return; }
      if (!profile) { if (select) select.focus(); return; }
      setUser(name);
      setProfile(profile);
      document.getElementById("userModal").style.display = "none";
      document.dispatchEvent(new CustomEvent("user-ready"));
    });
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") saveBtn.click(); });
  }
  const changeBtn = document.getElementById("btnChangeUser");
  if (changeBtn) changeBtn.addEventListener("click", openUserModal);

  if (!getUser() || !getProfile()) {
    openUserModal();
    return false; // identite pas encore connue
  }
  return true;
}

// Formatage numerique FR.
function fmtNum(v) {
  if (v === null || v === undefined || v === "") return "";
  const abs = Math.abs(v);
  const dec = abs !== 0 && abs < 1 ? 5 : 2;
  return Number(v).toLocaleString("fr-FR", { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

function fmtPct(p) {
  if (p === null || p === undefined) return "";
  return (p * 100).toLocaleString("fr-FR", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + " %";
}

function fmtBytes(n) {
  if (!n) return "";
  if (n < 1024) return n + " o";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " Ko";
  return (n / 1024 / 1024).toFixed(1) + " Mo";
}
