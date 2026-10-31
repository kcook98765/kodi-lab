"""Bounded native packaging proof, separate from browser/performance acceptance."""

from __future__ import annotations

import json

from kodi_lab.runtime.display_capabilities import DisplayCapability

# Executed only by the fixed image validator in its disposable native container.
# No caller command, path, display or package is accepted.
_PROGRAM = r"""
import ctypes, hashlib, json, resource, runpy, stat, subprocess, tempfile
from pathlib import Path
def output(args):
    resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
    with tempfile.TemporaryFile() as stream:
        value = subprocess.run(
            args, stdout=stream, stderr=subprocess.STDOUT, timeout=3)
        stream.seek(0)
        value.stdout = stream.read(65537)
    if len(value.stdout) >= 65536:
        raise RuntimeError('native display probe exceeded bounds')
    return value
path = Path('/usr/local/lib/kodi-lab-novnc-view.py')
metadata = path.lstat()
assert stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0
assert stat.S_IMODE(metadata.st_mode) == 0o444
assert metadata.st_size <= 65536
helper_digest = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
assert helper_digest == EXPECTED_HELPER_DIGEST
helper = runpy.run_path(str(path), run_name='kodi_lab_display_validation')
help_text = output(['/usr/bin/X0tigervnc', '-help']).stdout.decode().lower()
for mode in ('view-only', 'interactive'):
    for option in helper['command']('/run/kodi/probe-unused.sock', 2, mode)[1:]:
        assert option.lstrip('-').split('=')[0].lower() in help_text
x11 = ctypes.CDLL('libX11.so.6'); ctypes.CDLL('libXtst.so.6')
helper['probe'](); helper['probe'](interactive=True)
package = output(['/usr/bin/dpkg-query', '-W',
    '-f=${Package}\t${Version}\t${Architecture}', 'tigervnc-scraping-server'])
assert package.returncode == 0
package = package.stdout.decode().strip().split('\t')
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
x11.XDefaultScreen.restype = ctypes.c_int
for name in ('XDisplayWidth', 'XDisplayHeight'):
    fn = getattr(x11, name)
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    fn.restype = ctypes.c_int
x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
display = x11.XOpenDisplay(b':99')
assert display
try:
    screen = x11.XDefaultScreen(display)
    width = x11.XDisplayWidth(display, screen)
    height = x11.XDisplayHeight(display, screen)
finally:
    x11.XCloseDisplay(display)
pids = output(['/usr/bin/pgrep', '-x', 'kodi.bin']).stdout.split()
assert len(pids) == 1
with open('/proc/'+pids[0].decode()+'/environ', 'rb') as stream:
    environment = stream.read(65537)
assert len(environment) <= 65536 and b'LP_NUM_THREADS=1' in environment.split(b'\0')
print(json.dumps({'schema':'reviewed-display-packaging-v2',
    'helper_integrity_before_execution':True, 'geometry_probe':'xlib',
    'helper_digest':helper_digest,
    'package':package, 'width':width, 'height':height,
    'lp_num_threads':1, 'helper_probe':True, 'neutral_input_probe':True,
    'options_supported':True, 'libraries':True}))
""".strip()


def display_validation_command(capability: DisplayCapability) -> tuple[str, ...]:
    program = _PROGRAM.replace(
        "EXPECTED_HELPER_DIGEST", json.dumps(capability.helper_digest)
    )
    return ("/usr/bin/python3", "-I", "-B", "-c", program)


def display_validation_matches(value: object, capability: DisplayCapability) -> bool:
    """Historical r4 needs no retroactively invented packaging receipt."""
    expected = {
        "schema": "reviewed-display-packaging-v2",
        "helper_integrity_before_execution": True,
        "geometry_probe": "xlib",
        "helper_digest": capability.helper_digest,
        "package": list(capability.package),
        "width": capability.width,
        "height": capability.height,
        "lp_num_threads": 1,
        "helper_probe": True,
        "neutral_input_probe": True,
        "options_supported": True,
        "libraries": True,
    }
    return (
        isinstance(value, dict)
        and value == expected
        and all(type(value[key]) is type(item) for key, item in expected.items())
    )


def parse_display_validation(
    raw: bytes, capability: DisplayCapability
) -> dict[str, object]:
    if len(raw) > 8192:
        raise ValueError("native display packaging proof exceeded bounds")
    value = json.loads(raw)
    if not display_validation_matches(value, capability):
        raise ValueError("native display packaging differs from reviewed capability")
    assert isinstance(value, dict)
    return value
