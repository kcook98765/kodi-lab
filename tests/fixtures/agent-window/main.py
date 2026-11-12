"""Inert native acceptance window: no network, backend, settings or file writes."""

from __future__ import annotations

from typing import Protocol

import xbmc  # type: ignore[import-not-found]
import xbmcaddon  # type: ignore[import-not-found]
import xbmcgui  # type: ignore[import-not-found]


class Action(Protocol):
    def getId(self) -> int: ...


def evidence(value: str) -> None:
    xbmc.log("KODILAB_AGENT_FIXTURE:" + value, xbmc.LOGINFO)


class Fixture(xbmcgui.WindowXML):  # type: ignore[misc]
    def onInit(self) -> None:
        self.selections = 0
        self.getControl(200).setLabel("Ready: select the fixture button")
        self.setFocusId(101)
        evidence("READY")

    def onClick(self, control_id: int) -> None:
        if control_id == 101:
            self.selections += 1
            self.getControl(200).setLabel("Selected: " + str(self.selections))
            evidence("SELECT:" + str(self.selections))
        elif control_id == 102:
            evidence("CLOSE_BUTTON")
            self.close()

    def onAction(self, action: Action) -> None:
        if action.getId() in (9, 10, 92):
            evidence("BACK")
            self.close()


evidence("START")
# Manifest-selected library launch uses RunScript(file), so Kodi does not
# populate an implicit add-on context. Keep identity explicit in this fixture.
addon = xbmcaddon.Addon("script.kodilab.agentfixture")
window = Fixture("agent.xml", addon.getAddonInfo("path"), "Default", "720p")
window.doModal()
evidence("CLOSED")
del window
