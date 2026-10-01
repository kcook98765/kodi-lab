"""Local Kodi settings action for pairing codes and confirmed pairing reset."""

import math
import time
from datetime import datetime

import xbmcaddon
import xbmcgui
from narrow_bridge import (
    ADDON_ID,
    _local_pairing_status,
    _onboarding_store,
    _reset_pairing,
)
from onboarding import OnboardingError


def _message(view, now):
    status = view["status"]
    if status == "Pairing code active":
        remaining = max(0, math.ceil(view["expires_at"] - now))
        minutes, seconds = divmod(remaining, 60)
        local_expiry = datetime.fromtimestamp(view["expires_at"]).strftime("%H:%M:%S")
        return (
            f"Pairing code active\n\n{view['code']}\n\n"
            f"Expires at {local_expiry} local time "
            f"(about {minutes}:{seconds:02d} remaining). "
            "Enter this code in Kodi Lab Web."
        )
    if status == "Pairing code expired":
        return "Pairing code expired. Generate a new code to pair Kodi Lab."
    if status == "Pairing temporarily locked":
        return "Pairing temporarily locked. Wait before generating a new code."
    return "Not paired. Generate a code to pair Kodi Lab."


def main():
    dialog = xbmcgui.Dialog()
    addon = xbmcaddon.Addon(ADDON_ID)
    try:
        if _local_pairing_status():
            if dialog.yesno(
                "Pair Kodi Lab",
                "Reset pairing? This removes the current Kodi Lab connection. "
                "You can pair this bridge again with a new code.",
                nolabel="Cancel",
                yeslabel="Reset pairing",
            ):
                if _reset_pairing():
                    dialog.ok(
                        "Pair Kodi Lab",
                        "Pairing reset. Generate a new code to pair Kodi Lab.",
                    )
                else:
                    dialog.ok(
                        "Pair Kodi Lab",
                        "Already unpaired. Generate a new code to pair Kodi Lab.",
                    )
            return
        if not addon.getSettingBool("enabled"):
            dialog.ok("Pair Kodi Lab", "Enable bridge in its settings first.")
            return
        store = _onboarding_store()
        view = store.status(paired=False)
        replace = dialog.yesno(
            "Pair Kodi Lab",
            _message(view, time.time()),
            nolabel="Close",
            yeslabel="Generate new code",
        )
        if replace:
            store.generate_new_code(paired=False)
            dialog.ok(
                "Pair Kodi Lab", _message(store.status(paired=False), time.time())
            )
    except OnboardingError as error:
        if str(error) == "generation_cooldown":
            dialog.ok(
                "Pair Kodi Lab", "Wait 30 seconds before generating another code."
            )
        elif str(error) == "pairing_temporarily_locked":
            dialog.ok("Pair Kodi Lab", "Pairing temporarily locked. Try later.")
        elif str(error) == "pairing_in_progress":
            dialog.ok("Pair Kodi Lab", "A pairing transaction is in progress.")
        else:
            dialog.ok("Pair Kodi Lab", "Pairing state unavailable. Check Kodi logs.")
    except (OSError, RuntimeError):
        dialog.ok("Pair Kodi Lab", "Pairing state unavailable. Check Kodi logs.")


if __name__ == "__main__":
    main()
