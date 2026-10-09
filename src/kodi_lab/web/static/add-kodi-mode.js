(() => {
  "use strict";
  const root = document.querySelector("[data-unified-add-kodi]");
  if (!root) return;
  const controls = root.querySelector("[data-mode-controls]");
  const radios = Array.from(controls.querySelectorAll("input[type=radio]"));
  const panels = Array.from(root.querySelectorAll("[data-onboarding-mode]"));
  const help = root.querySelector("[data-mode-help]");
  const states = {home: {active:false, busy:false}, development: {active:false, busy:false}};
  let mode = root.dataset.initialMode === "development" ? "development" : "home";
  const select = (next) => {
    if (mode !== next) panels.find(panel => panel.dataset.onboardingMode === mode)
      .dispatchEvent(new CustomEvent("kodi-mode-leave"));
    mode = next;
    controls.hidden = root.hasAttribute("data-standard-view") && mode === "home";
    radios.forEach(radio => { radio.checked = radio.value === mode; });
    panels.forEach(panel => {
      panel.hidden = panel.dataset.onboardingMode !== mode;
      panel.inert = panel.hidden;
    });
  };
  const update = () => {
    const locked = Object.keys(states).find(key => states[key].active || states[key].busy);
    if (locked) select(locked);
    controls.disabled = Boolean(locked);
    help.textContent = locked
      ? `${locked === "home" ? "Home" : "Development"} connection in progress. Complete or resolve this saved connection before changing its use.`
      : root.hasAttribute("data-standard-view") && mode === "home"
        ? "Connect your Home Kodi. Choose the Development administrator view for add-on testing."
        : "Choose how you will use Kodi before connecting.";
  };
  root.addEventListener("kodi-onboarding-state", event => {
    if (!Object.hasOwn(states, event.detail.mode)) return;
    states[event.detail.mode] = {active:Boolean(event.detail.active), busy:Boolean(event.detail.busy)};
    update();
  });
  controls.addEventListener("change", event => {
    if (controls.disabled || !radios.includes(event.target)) return;
    select(event.target.value);
  });
  select(mode);
  update();
})();
