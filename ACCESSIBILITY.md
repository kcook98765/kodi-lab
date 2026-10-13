# Accessibility

Kodi Lab aims to make its Web administrator usable with a keyboard, clear labels,
visible focus and layouts that adapt to smaller screens. Accessibility barriers
are bugs worth reporting, whether you are a home Kodi user or a developer.

## Current interaction support

The Web administrator includes a skip-to-content link, focus indicators, labeled
form controls and navigation that identifies the current page. Selected menus
and disclosures have been checked with Tab, Enter, Space and Escape. Responsive
layouts are tested at desktop, tablet and mobile widths.

The [Kodi views acceptance record](docs/WEB-KODI-VIEWS-6A.md) documents keyboard,
responsive, dark-mode and no-JavaScript checks, plus automated axe scans of
selected fixture-driven page states. [Add Kodi acceptance](docs/WEB-UNIFIED-ADD-KODI.md)
records related form and navigation checks. These are evidence for those tested
pages and states, not a formal accessibility audit or a claim of WCAG conformance.

## Known limits

- Complete screen-reader and assistive-technology coverage has not been qualified.
- Some administration actions require JavaScript. No-JavaScript checks cover
  selected information and navigation paths, not every workflow.
- Kodi screenshots are images of Kodi's interface. Native and Bridge UI-state
  inspection can supply text context where supported, but they do not make every
  Kodi window or add-on accessible.
- Browser, zoom, device and add-on combinations outside the recorded tests may
  expose additional barriers.

## Report a barrier

Use the [bug report form](https://github.com/kcook98765/kodi-lab/issues/new?template=bug_report.yml).
Describe the page and task, what you expected, what happened, and your browser,
operating system and relevant assistive technology or zoom setting. A short
reproduction helps; you do not need to diagnose the cause.

Share only sanitized logs or screenshots. See [CONTRIBUTING.md](CONTRIBUTING.md)
and [SECURITY.md](SECURITY.md) before sharing sensitive information.
