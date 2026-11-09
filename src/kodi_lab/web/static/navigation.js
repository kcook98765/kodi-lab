"use strict";

(() => {
  // Fragments never reach the server. Replace the old entry in history so Back
  // returns to the previous page instead of redirecting through Home again.
  function redirectLegacyEntry() {
    const registrationFragment = ["#instances", "#managed-details"].includes(location.hash);
    const slotFragment = ["#managed-kodi-heading", "#matrix", "#nexus", "#omega", "#piers"].includes(location.hash);
    if (location.pathname === "/" || (location.pathname === "/kodi" && (registrationFragment || slotFragment))) {
      const destination = registrationFragment ? "/managed-instances" : slotFragment ? "/managed-kodi" : "/kodi";
      location.replace(destination + location.search + location.hash);
      return true;
    }
    return false;
  }
  if (redirectLegacyEntry()) return;
  window.addEventListener("hashchange", redirectLegacyEntry);
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
  navigation.addEventListener("click", event => {
    if (!narrow.matches || event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest("a[href]");
    if (!link) return;
    const destination = new URL(link.href, location.href);
    if (destination.origin !== location.origin || destination.pathname !== location.pathname ||
        destination.search !== location.search || !destination.hash) return;
    const heading = document.getElementById(destination.hash.slice(1));
    if (!heading) return;
    setOpen(false);
    if (!heading.hasAttribute("tabindex")) {
      heading.setAttribute("tabindex", "-1");
      heading.addEventListener("blur", () => heading.removeAttribute("tabindex"), {once: true});
    }
    // Keep the link's native fragment scrolling and Back/Forward history.
    heading.focus({preventScroll: true});
  });
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
