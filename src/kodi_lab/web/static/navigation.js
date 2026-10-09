"use strict";

(() => {
  const sidebar = document.querySelector("[data-admin-sidebar]");
  if (!sidebar) return;
  const managed = document.querySelector("[data-managed-deep-link]");
  let managedScriptLoaded = false;
  function openManagedDeepLink() {
    if (!managed || !["#instances", "#managed-details"].includes(location.hash)) return;
    managed.hidden = false;
    managed.open = true;
    if (!managedScriptLoaded) {
      const script = document.createElement("script");
      script.src = managed.dataset.managedScript;
      document.head.append(script);
      managedScriptLoaded = true;
    }
    document.querySelector(location.hash).scrollIntoView();
  }
  openManagedDeepLink();
  window.addEventListener("hashchange", openManagedDeepLink);
  sidebar.querySelector("[data-admin-view-form]")?.addEventListener("submit", event => {
    event.currentTarget.elements.return_to.value = location.pathname + location.search + location.hash;
  });
  const toggle = sidebar.querySelector("[data-menu-toggle]");
  const navigation = sidebar.querySelector("[data-admin-navigation]");
  const narrow = window.matchMedia("(max-width: 960px)");
  // CSS may hide the focused button before the media-query event arrives.
  let menuHadFocus = false;
  document.addEventListener("focusin", event => {
    menuHadFocus = event.target === toggle;
  });

  function setOpen(open, restoreFocus = false) {
    if (!open && (restoreFocus || navigation.contains(document.activeElement))) toggle.focus();
    navigation.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
  }

  toggle.hidden = false;
  setOpen(!narrow.matches);
  toggle.addEventListener("click", () => setOpen(navigation.hidden));
  sidebar.addEventListener("keydown", event => {
    if (event.key === "Escape" && narrow.matches && !navigation.hidden) {
      event.preventDefault();
      setOpen(false, true);
    }
  });
  document.addEventListener("click", event => {
    if (narrow.matches && !navigation.hidden && !sidebar.contains(event.target)) setOpen(false);
  });
  narrow.addEventListener("change", () => {
    setOpen(!narrow.matches);
    if (!narrow.matches && menuHadFocus) {
      const activeLink = navigation.querySelector('[aria-current="page"]') || navigation.querySelector("a");
      activeLink.focus();
    }
  });
})();
