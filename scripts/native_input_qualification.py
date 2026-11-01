"""Finite read-only X11 input observer for isolated native qualification.

Sample inside the runtime so Docker-exec and JSON-RPC round trips cannot create
false coverage gaps. This grants no input authority and changes no viewer code.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

NATIVE_OBSERVER = r"""
import ctypes,json,signal,time
signal.alarm(12)
x=ctypes.CDLL('libX11.so.6');d=ctypes.c_void_p
x.XOpenDisplay.argtypes=[ctypes.c_char_p];x.XOpenDisplay.restype=d
x.XDefaultRootWindow.argtypes=[d];x.XDefaultRootWindow.restype=ctypes.c_ulong
x.XQueryKeymap.argtypes=[d,ctypes.c_void_p]
x.XQueryPointer.argtypes=[d,ctypes.c_ulong,ctypes.POINTER(ctypes.c_ulong),ctypes.POINTER(ctypes.c_ulong),ctypes.POINTER(ctypes.c_int),ctypes.POINTER(ctypes.c_int),ctypes.POINTER(ctypes.c_int),ctypes.POINTER(ctypes.c_int),ctypes.POINTER(ctypes.c_uint)]
x.XCloseDisplay.argtypes=[d]
p=x.XOpenDisplay(b':99');assert p
root=x.XDefaultRootWindow(p)
def sample():
 began=time.time();keys=(ctypes.c_ubyte*32)();x.XQueryKeymap(p,keys)
 roots=[ctypes.c_ulong(),ctypes.c_ulong()]
 coords=[ctypes.c_int() for _ in range(4)];mask=ctypes.c_uint()
 assert x.XQueryPointer(p,root,*[ctypes.byref(v) for v in (*roots,*coords,mask)])
 state={'keys':[k for k in range(8,256) if keys[k//8]&(1<<(k%8))],
        'root_xy':[coords[0].value,coords[1].value],'mask':mask.value}
 return {'began':began,'at':time.time(),'mono':time.monotonic(),'state':state}
try:
 print(json.dumps({'armed':sample()}),flush=True)
 deadline=time.monotonic()+8;scheduled=time.monotonic()
 while time.monotonic()<deadline:
  print(json.dumps(sample()),flush=True);scheduled+=.05;time.sleep(max(0,scheduled-time.monotonic()))
 print(json.dumps({'complete':True}),flush=True)
finally:x.XCloseDisplay(p)
"""


def validate_refusal(
    rows: list[dict[str, Any]],
    before: dict[str, Any],
    begin: float,
    end: float,
    *,
    max_gap: float = 0.5,
) -> dict[str, float | int]:
    """Require full finite coverage and no key/pointer effect, including edges."""
    if end <= begin or max_gap <= 0:
        raise ValueError("Invalid observation interval")
    ordered = sorted(rows, key=lambda row: row["at"])
    left = [r for r in ordered if r["at"] <= begin]
    right = [r for r in ordered if r["at"] >= end]
    if not left or not right:
        raise ValueError("Missing observation interval boundary")
    relevant = [left[-1], *[r for r in ordered if begin < r["at"] < end], right[0]]
    if len(relevant) < 3:
        raise ValueError("Insufficient observations")
    gaps = [b["at"] - a["at"] for a, b in pairwise(relevant)]
    if max(gaps) > max_gap or any(r["at"] - r["began"] > max_gap for r in relevant):
        raise ValueError("Native observation gap exceeds bound")
    if any(r["state"] != before for r in relevant):
        raise ValueError("Unauthorized native input effect")
    return {
        "samples": len(relevant),
        "max_gap_seconds": max(gaps),
        "seconds": end - begin,
    }


def compound_latency(baseline: float, current: float) -> dict[str, float | bool]:
    """The existing rule fails only when both increases exceed their limits."""
    if baseline <= 0 or current < 0:
        raise ValueError("Invalid latency measurement")
    delta = current - baseline
    growth = delta / baseline
    return {
        "delta_ms": delta,
        "growth_ratio": growth,
        "passed": not (delta > 100 and growth > 0.2),
    }
