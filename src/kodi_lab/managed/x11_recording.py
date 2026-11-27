"""Fixed read-only display sampling program executed in a managed Kodi container.

Only the broker supplies this source and its arguments. The wire format on
stdout is tightly packed BGR0 at 640 by 360. Stdin is the broker's short lease.
"""

import ctypes as c
import os
import select
import signal
import sys
from time import monotonic

WIDTH, HEIGHT = 640, 360
LEASE_SECONDS = 5
MAX_SECONDS = 1800


class XImage(c.Structure):
    _fields_ = [
        ("width", c.c_int),
        ("height", c.c_int),
        ("xoffset", c.c_int),
        ("format", c.c_int),
        ("data", c.c_void_p),
        ("byte_order", c.c_int),
        ("bitmap_unit", c.c_int),
        ("bitmap_bit_order", c.c_int),
        ("bitmap_pad", c.c_int),
        ("depth", c.c_int),
        ("stride", c.c_int),
        ("bits_per_pixel", c.c_int),
        ("red", c.c_ulong),
        ("green", c.c_ulong),
        ("blue", c.c_ulong),
    ]


def capture(rate: int, seconds: int) -> None:
    if rate not in {2, 5} or not 1 <= seconds <= MAX_SECONDS:
        raise ValueError("unsupported recording settings")
    signal.setitimer(signal.ITIMER_REAL, LEASE_SECONDS)
    x11 = c.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes = [c.c_char_p]
    x11.XOpenDisplay.restype = c.c_void_p
    x11.XDefaultRootWindow.argtypes = [c.c_void_p]
    x11.XDefaultRootWindow.restype = c.c_ulong
    x11.XDefaultScreen.argtypes = [c.c_void_p]
    x11.XDisplayWidth.argtypes = [c.c_void_p, c.c_int]
    x11.XDisplayHeight.argtypes = [c.c_void_p, c.c_int]
    x11.XGetImage.argtypes = [
        c.c_void_p,
        c.c_ulong,
        c.c_int,
        c.c_int,
        c.c_uint,
        c.c_uint,
        c.c_ulong,
        c.c_int,
    ]
    x11.XGetImage.restype = c.POINTER(XImage)
    x11.XDestroyImage.argtypes = [c.POINTER(XImage)]
    x11.XCloseDisplay.argtypes = [c.c_void_p]
    display = x11.XOpenDisplay(b":99")
    if not display:
        raise RuntimeError("recording display unavailable")
    try:
        screen = x11.XDefaultScreen(display)
        if (
            x11.XDisplayWidth(display, screen),
            x11.XDisplayHeight(display, screen),
        ) != (WIDTH, HEIGHT):
            raise RuntimeError("unsupported recording display size")
        root = x11.XDefaultRootWindow(display)
        pump(x11, display, root, rate, seconds)
    finally:
        x11.XCloseDisplay(display)


def pump(x11: c.CDLL, display: int, root: int, rate: int, seconds: int) -> None:
    deadline = monotonic() + seconds
    lease = monotonic() + LEASE_SECONDS
    next_frame = monotonic()
    while (now := monotonic()) < min(deadline, lease):
        readable, _, _ = select.select(
            [0], [], [], max(0, min(next_frame, deadline, lease) - now)
        )
        if readable:
            if not os.read(0, 4096):
                return
            lease = monotonic() + LEASE_SECONDS
            signal.setitimer(signal.ITIMER_REAL, LEASE_SECONDS)
        now = monotonic()
        if now < next_frame or now >= min(deadline, lease):
            continue
        image = x11.XGetImage(display, root, 0, 0, WIDTH, HEIGHT, -1, 2)
        if not image:
            raise RuntimeError("recording frame unavailable")
        try:
            data = image.contents
            if (
                data.byte_order != 0
                or data.bits_per_pixel != 32
                or data.stride != 4 * WIDTH
                or (data.red, data.green, data.blue) != (0xFF0000, 0x00FF00, 0x0000FF)
                or (data.width, data.height) != (WIDTH, HEIGHT)
                or not data.data
            ):
                raise RuntimeError("unsupported recording pixel format")
            sys.stdout.buffer.write(c.string_at(data.data, data.stride * HEIGHT))
            sys.stdout.buffer.flush()
        finally:
            x11.XDestroyImage(image)
        next_frame += 1 / rate
        if next_frame < now:
            next_frame = now + 1 / rate


if __name__ == "__main__":
    capture(int(sys.argv[1]), int(sys.argv[2]))
