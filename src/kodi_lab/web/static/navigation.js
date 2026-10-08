"use strict";

(() => {
  const sidebar = document.querySelector("[data-admin-sidebar]");
  if (!sidebar) return;
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
