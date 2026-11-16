"""Bounded, in-memory log scans. File access and authentication stay in the Bridge.

No paths, user regexes, persistent cursors, or log-body replay cache live here.
"""

import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone

SEVERITIES = {
    "DEBUG",
    "INFO",
    "NOTICE",
    "WARNING",
    "ERROR",
    "FATAL",
    "SEVERE",
    "UNKNOWN",
}
TIME_PATTERN = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
# Fixed grammar, only at a physical line's start. The optional timestamp may be
# malformed; recognizing the severity must not depend on parsing its date.
HEADER = re.compile(
    rb"^(?:(?P<time>\d{4}-\S{1,24}?[ T]\S{1,24}|\d{1,2}:\S{1,24})\s+)?"
    rb"(?:T:\S{1,32}\s+)?(?P<severity>DEBUG|INFO|NOTICE|WARNING|ERROR|FATAL|SEVERE)"
    rb"\s*(?:<[^>\r\n]{0,128}>)?\s*:",
    re.IGNORECASE,
)


class ScanError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _local_time(value):
    if not isinstance(value, str) or re.fullmatch(TIME_PATTERN, value) is None:
        raise ValueError("local timestamp required")
    return datetime.fromisoformat(value)


def _query(payload):
    keys = {
        "logs",
        "severities",
        "text",
        "start_time",
        "end_time",
        "context_records",
        "max_matches",
        "cursor",
        "client_binding",
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ScanError("log_request_invalid")
    try:
        levels = payload["severities"]
        if (
            payload["logs"] not in ("current", "previous", "both")
            or not isinstance(levels, list)
            or not 1 <= len(levels) <= 8
            or any(not isinstance(s, str) or s not in SEVERITIES for s in levels)
            or len(set(levels)) != len(levels)
            or type(payload["context_records"]) is not int
            or not 0 <= payload["context_records"] <= 2
            or type(payload["max_matches"]) is not int
            or not 0 <= payload["max_matches"] <= 20
            or not isinstance(payload["client_binding"], str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["client_binding"]) is None
            or (
                payload["cursor"] is not None
                and (
                    not isinstance(payload["cursor"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", payload["cursor"]) is None
                )
            )
            or (
                payload["text"] is not None
                and (
                    not isinstance(payload["text"], str)
                    or not 1 <= len(payload["text"]) <= 256
                )
            )
        ):
            raise ValueError
        start, end = (
            _local_time(payload[k]) if payload[k] is not None else None
            for k in ("start_time", "end_time")
        )
        if start is not None and end is not None and start > end:
            raise ValueError
        if payload["text"] is not None:
            payload["text"].encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise ScanError("log_request_invalid") from None
    return {k: v for k, v in payload.items() if k != "cursor"}


class _Records:
    """Streaming logical records, including literal matches in oversized lines."""

    def __init__(self, file, scan):
        self.file, self.scan = file, scan
        self.query = scan.query
        self.needle = (self.query["text"] or "").encode("utf-8")
        self.start = (
            _local_time(self.query["start_time"]) if self.query["start_time"] else None
        )
        self.end = (
            _local_time(self.query["end_time"]) if self.query["end_time"] else None
        )
        self.position = self.line_start = 0
        self.head = b""
        self.line_started = False
        self.record = None
        self.previous = deque(maxlen=self.query["context_records"])
        self.pending = []

    def feed(self, raw):
        offset = 0
        while offset < len(raw):
            index = raw.find(b"\n", offset)
            end = len(raw) if index < 0 else index + 1
            self._piece(raw[offset:end], index >= 0)
            offset = end

    def _piece(self, data, eol):
        self.position += len(data)
        if not self.line_started:
            data = self.head + data
            self.head = b""
            if not eol and len(data) < 512:
                self.head = data
                return
            self.line_started = True
            header = HEADER.match(data[:512])
            if header:
                self._finish_record(self.line_start)
            if header or self.record is None:
                stamp = None
                if header and header["time"]:
                    with suppress(ValueError, UnicodeError):
                        stamp = _local_time(
                            header["time"].decode("ascii").replace(" ", "T")
                        )
                self.record = {
                    "start": self.line_start,
                    "severity": header["severity"].decode("ascii").upper()
                    if header
                    else "UNKNOWN",
                    "timestamp": stamp,
                    "excerpt": b"",
                    "length": 0,
                    "found": not self.needle,
                    "overlap": b"",
                }
        record = self.record
        record["length"] += len(data)
        record["excerpt"] += data[: max(0, 1024 - len(record["excerpt"]))]
        if not record["found"]:
            candidate = record["overlap"] + data
            record["found"] = self.needle in candidate
            record["overlap"] = (
                candidate[-(len(self.needle) - 1) :] if len(self.needle) > 1 else b""
            )
        if eol:
            self.line_start = self.position
            self.line_started = False

    def _finish_record(self, end):
        record = self.record
        if record is None:
            return
        self.record = None
        file = self.file
        file["records_scanned"] += 1
        stamp = record["timestamp"]
        if stamp is None:
            file["unknown_timestamp_records"] += 1
        snippet = record["excerpt"][:256].decode("utf-8", "replace")
        for match in self.pending:
            match["after"].append(snippet)
        self.pending = [
            m for m in self.pending if len(m["after"]) < self.query["context_records"]
        ]
        if (
            record["severity"] in self.query["severities"]
            and record["found"]
            and (self.start is None or (stamp is not None and stamp >= self.start))
            and (self.end is None or (stamp is not None and stamp <= self.end))
        ):
            file["matched_records"] += 1
            counts = file["severity_counts"]
            counts[record["severity"]] = counts.get(record["severity"], 0) + 1
            if len(self.scan.matches) < self.query["max_matches"]:
                match = {
                    "log": file["log"],
                    "start_byte": record["start"],
                    "end_byte": end,
                    "severity": record["severity"],
                    "timestamp": stamp.isoformat() if stamp else None,
                    "content": record["excerpt"].decode("utf-8", "replace"),
                    "content_truncated": record["length"] > 1024,
                    "before": list(self.previous),
                    "after": [],
                }
                self.scan.matches.append(match)
                if self.query["context_records"]:
                    self.pending.append(match)
                if match["content_truncated"]:
                    self.scan.truncated = True
            else:
                self.scan.truncated = True
        self.previous.append(snippet)

    def finish(self):
        if self.head:
            # _piece already accounted for the bytes retained in head.
            self._piece(b"", True)
        self._finish_record(self.position)
        self.pending.clear()
        self.previous.clear()


class _Scan:
    def __init__(self, query, binding, opener):
        self.query, self.binding = query, binding
        self.created = time.monotonic()
        self.matches = []
        self.truncated = False
        self.files = []
        self.work = []
        names = ("current", "previous") if query["logs"] == "both" else (query["logs"],)
        for log in names:
            file = {
                "log": log,
                "file_id": None,
                "observed_at": datetime.now(timezone.utc)  # noqa: UP017 - Kodi also runs Python 3.8.
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
                "boundary_bytes": 0,
                "scanned_bytes": 0,
                "verified_bytes": 0,
                "state": "scanning",
                "counts_exact": False,
                "records_scanned": 0,
                "matched_records": 0,
                "severity_counts": {},
                "unknown_timestamp_records": 0,
                "append_observed": False,
            }
            self.files.append(file)
            work = {
                "file": file,
                "digest": hashlib.sha256(),
                "verify_digest": hashlib.sha256(),
            }
            self.work.append(work)
            try:
                with opener(log) as (stream, observe):
                    identity, signature, size = observe()
                    file["file_id"] = hashlib.sha256(
                        _canonical([identity, signature, size])
                    ).hexdigest()
                    file["boundary_bytes"] = size
                    work.update(identity=identity, signature=signature, size=size)
                    # Keep the original inode alive across requests. Otherwise
                    # unlink/recreate can reuse it and masquerade as growth.
                    work["anchor"] = os.dup(stream.fileno())
                    work["parser"] = _Records(file, self)
            except FileNotFoundError:
                file["state"] = "missing"
                self._close_file(work)
            except (OSError, ValueError):
                file["state"] = "unavailable"
                self._close_file(work)

    @staticmethod
    def _close_file(work):
        work.pop("parser", None)
        anchor = work.pop("anchor", None)
        if anchor is not None:
            os.close(anchor)

    def close(self):
        for work in self.work:
            self._close_file(work)

    @staticmethod
    def _observe(work, observation):
        file = work["file"]
        identity, signature, size = observation
        if identity != work["identity"] or size < work["size"]:
            raise ValueError("changed")
        if signature != work["signature"]:
            if file["state"] in ("verifying", "complete") or size <= work["size"]:
                raise ValueError("changed")
            file["append_observed"] = True
            work["signature"], work["size"] = signature, size

    def step(self, opener, byte_limit, seconds, abort):
        started = time.monotonic()
        used, reason = 0, "byte_budget"
        for work in self.work:
            file = work["file"]
            if file["state"] not in ("scanning", "verifying"):
                continue
            try:
                with opener(file["log"]) as (stream, observe):
                    self._observe(work, observe())
                    while file["state"] in ("scanning", "verifying"):
                        if abort():
                            reason = "aborted"
                            break
                        if time.monotonic() - started >= seconds:
                            reason = "time_budget"
                            break
                        verifying = file["state"] == "verifying"
                        position = (
                            file["verified_bytes"]
                            if verifying
                            else file["scanned_bytes"]
                        )
                        remaining = file["boundary_bytes"] - position
                        if not remaining:
                            self._observe(work, observe())
                            if not verifying:
                                work["parser"].finish()
                            if not verifying and file["append_observed"]:
                                file["state"] = "verifying"
                                continue
                            if (
                                verifying
                                and work["digest"].digest()
                                != work["verify_digest"].digest()
                            ):
                                raise ValueError("changed")
                            file["state"], file["counts_exact"] = "complete", True
                            # Release all retained record fragments after completion.
                            self._close_file(work)
                            break
                        if used >= byte_limit:
                            break
                        stream.seek(position)
                        raw = stream.read(min(65536, remaining, byte_limit - used))
                        if not raw:
                            raise ValueError("changed")
                        used += len(raw)
                        if verifying:
                            work["verify_digest"].update(raw)
                            file["verified_bytes"] += len(raw)
                        else:
                            work["digest"].update(raw)
                            work["parser"].feed(raw)
                            file["scanned_bytes"] += len(raw)
                    self._observe(work, observe())
            except FileNotFoundError:
                file["state"] = "changed"
            except ValueError:
                file["state"] = "changed"
            except OSError:
                file["state"] = "unavailable"
            if file["state"] in ("changed", "unavailable"):
                file["counts_exact"] = False
                self._close_file(work)
            if used >= byte_limit or reason in ("time_budget", "aborted"):
                break
        return used, reason

    def result(self, token, used, reason):
        complete = all(f["state"] == "complete" for f in self.files)
        active = any(f["state"] in ("scanning", "verifying") for f in self.files)
        if complete:
            reason = "complete"
        elif not active:
            reason = (
                "file_changed"
                if any(f["state"] == "changed" for f in self.files)
                else "file_unavailable"
            )
        result = {
            "files": self.files,
            "matches": list(self.matches),
            "complete": complete,
            "counts_exact": complete,
            "excerpts_truncated": self.truncated,
            "reason": reason,
            "cursor": token if active else None,
            "bytes_read": used,
            "count_unit": "logical_records",
        }
        while len(_canonical(result)) > 32768 and result["matches"]:
            result["matches"].pop()
            result["excerpts_truncated"] = True
        # Do not let callers mutate live cursor state or serialize it concurrently.
        return json.loads(_canonical(result))


class ScanStore:
    """One concurrent scan slice; at most 16 states, absolute ten-minute expiry."""

    def __init__(self):
        self.lock = threading.Lock()
        self.states = {}
        self.byte_limit = 8 * 1024 * 1024
        self.seconds = 0.75
        self.ttl = 600
        self.capacity = 16
        self.closed = False

    def _prune(self):
        now = time.monotonic()
        for token, scan in list(self.states.items()):
            if self.closed or now - scan.created >= self.ttl:
                scan.close()
                del self.states[token]

    def prune(self):
        """Called by the existing HTTP service loop, including while idle."""
        while self.lock.acquire(blocking=False):
            try:
                self._prune()
            finally:
                self.lock.release()
            # Shutdown may have lost its nonblocking attempt just before release.
            if not self.closed or not self.states:
                return

    def shutdown(self):
        self.closed = True
        self.prune()

    def scan(self, payload, binding, opener, abort=lambda: False):
        query = _query(payload)
        if not self.lock.acquire(blocking=False):
            raise ScanError("log_scan_busy")
        try:
            self._prune()
            if self.closed:
                raise ScanError("log_scan_busy")
            token = payload["cursor"]
            if token is not None:
                scan = self.states.get(token)
                if scan is None or scan.binding != binding or scan.query != query:
                    raise ScanError("log_cursor_invalid")
            else:
                if len(self.states) >= self.capacity:
                    terminal = next(
                        (
                            k
                            for k, s in self.states.items()
                            if all(
                                f["state"] not in ("scanning", "verifying")
                                for f in s.files
                            )
                        ),
                        None,
                    )
                    if terminal is None:
                        raise ScanError("log_scan_busy")
                    self.states[terminal].close()
                    del self.states[terminal]
                token = secrets.token_hex(32)
                scan = _Scan(query, binding, opener)
                self.states[token] = scan
            used, reason = scan.step(opener, self.byte_limit, self.seconds, abort)
            return scan.result(token, used, reason)
        finally:
            self.lock.release()
            if self.closed:
                self.prune()
