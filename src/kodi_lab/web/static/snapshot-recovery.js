"use strict";
const preview = document.querySelector("#recovery-preview");
const confirm = document.querySelector("#recovery-confirm");
const output = document.querySelector("#recovery-proof");
const receipt = document.querySelector("#recovery-receipt");
const observe = document.querySelector("#recovery-observe");
let reviewed;
let requestId;
let saved = sessionStorage.getItem("snapshot-recovery-receipt");
function receiptLink(path) {
  if (!/^\/api\/snapshot-recovery\/[0-9a-f-]{36}$/.test(path || "")) return;
  saved = path;
  receipt.href = path;
  receipt.hidden = observe.hidden = false;
  sessionStorage.setItem("snapshot-recovery-receipt", path);
}
receiptLink(saved);
async function readStatus() {
  try {
    const response = await fetch(saved, {credentials: "same-origin"});
    const value = await response.json();
    if (!response.ok) throw new Error(JSON.stringify(value));
    output.textContent = JSON.stringify(value, null, 2);
    return value.state;
  } catch (error) { output.textContent = error.message; return "unavailable"; }
}
observe.addEventListener("click", readStatus);
async function post(path, body) {
  const response = await fetch(path, {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-Kodi-Lab-CSRF": document.querySelector('meta[name="kodi-lab-csrf"]').content},
    body: JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(JSON.stringify(value));
  return value;
}
preview.addEventListener("click", async () => {
  confirm.hidden = true;
  try {
    reviewed = await post("/api/snapshot-recovery/reviews", {});
    requestId = reviewed.confirmation_request_id;
    output.textContent = JSON.stringify(reviewed, null, 2);
    confirm.hidden = false;
  } catch (error) { output.textContent = error.message; }
});
confirm.addEventListener("click", async () => {
  confirm.disabled = true;
  receiptLink(reviewed.status_url);
  try {
    const result = await post(`/api/snapshot-recovery/${reviewed.recovery_id}/confirm`,
      {fingerprint: reviewed.fingerprint, request_id: requestId, confirmed: true});
    output.textContent = JSON.stringify(result, null, 2);
    confirm.hidden = true;
    for (let i = 0; i < 5; i++) {
      const state = await readStatus();
      if (["succeeded", "reconciliation_required", "unavailable"].includes(state)) break;
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  } catch (error) { output.textContent = error.message; }
  confirm.disabled = false;
});
