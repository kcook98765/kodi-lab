"""Installed-artifact entry point for the restricted Linux broker."""

from __future__ import annotations

import json
import logging
import signal
import sys
from threading import Event

from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.errors import (
    BrokerAuthorizationError,
    BrokerStartupError,
    BrokerUnavailableError,
)
from kodi_lab.broker.server import BrokerServer


class _AuditFormatter(logging.Formatter):
    """Keep the broker's bounded semantic audit fields in container logs."""

    def format(self, record: logging.LogRecord) -> str:
        fields = {
            key: value
            for key, value in vars(record).items()
            if key.startswith("broker_")
        }
        return json.dumps(
            {"event": record.getMessage(), **fields},
            sort_keys=True,
            separators=(",", ":"),
        )


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    audit = logging.getLogger("kodi_lab.broker.audit")
    audit.setLevel(logging.INFO)
    audit.propagate = False
    handler = logging.StreamHandler()
    handler.setFormatter(_AuditFormatter())
    audit.addHandler(handler)


def _healthcheck() -> int:
    """Prove a live listener responds, including its rejection of root."""

    try:
        BrokerClient().ping()
    except BrokerAuthorizationError as error:
        # The broker container runs the check as root. The exact-UID broker
        # must reject that peer after accepting and answering the connection.
        return 0 if error.code == "unauthorized_peer" else 1
    except BrokerUnavailableError:
        return 1
    except Exception:
        return 1
    # An accepted root request would mean the production identity gate drifted.
    return 1


def main() -> int:
    """Run the closed production broker until SIGTERM or SIGINT."""

    if sys.argv[1:] == ["--healthcheck"]:
        return _healthcheck()
    if sys.argv[1:]:
        print("usage: kodi-lab-broker [--healthcheck]", file=sys.stderr)
        return 2

    _configure_logging()
    stopped = Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server = BrokerServer.production()
        server.serve_forever(stopped)
    except BrokerStartupError as error:
        logging.getLogger(__name__).error("broker startup rejected: %s", error)
        return 1
    except Exception:
        logging.getLogger(__name__).exception("broker stopped unexpectedly")
        return 1
    return 0
