"use strict";

(() => {
  const copy = JSON.parse(document.getElementById("state-copy")?.content.textContent || "{}");
  const state = value => copy.states?.[value] || `Unrecognized state (${value})`;
  const error = code => copy.errors?.[code] || "The action needs review. Check the technical details before continuing.";
  function render(node, value) {
    node.dataset.uiState = value;
    node.textContent = state(value);
    node.title = value;
  }
  window.KodiLabPresentation = Object.freeze({state, error, render});
  for (const node of document.querySelectorAll("[data-ui-state]")) render(node, node.dataset.uiState);
})();
