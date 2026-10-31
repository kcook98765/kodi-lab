"""Bounded native packaging proof, separate from browser/performance acceptance."""

from __future__ import annotations

import json

from kodi_lab.runtime.display_capabilities import DisplayCapability

# Executed only by the fixed image validator in its disposable native container.
# No caller command, path, display or package is accepted.
_PIERS_PROCESS_SOURCE = r"""
def matches_piers_process(cmdline, executable, uid):
    expected = (
        b'/opt/fdo/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2',
        b'--library-path',
        b'/app/lib:/opt/mesa/lib:/opt/fdo/lib/x86_64-linux-gnu:/opt/fdo/lib/x86_64-linux-gnu/pulseaudio:/opt/fdo/lib',
        b'/app/lib/kodi/kodi.bin',
        b'--standalone', b'--windowing=x11', b'--audio-backend=alsa',
    )
    return (type(cmdline) is bytes and cmdline == b'\0'.join(expected)+b'\0'
            and executable == expected[0].decode() and type(uid) is int and uid == 1000)
""".strip()

_PROGRAM = (
    _PIERS_PROCESS_SOURCE
    + "\n"
    + r"""
import ctypes, hashlib, json, os, resource, runpy, stat, subprocess, tempfile
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
clipboard_policy = EXPECTED_CLIPBOARD_POLICY
clipboard_flags = ('AcceptCutText', 'SendCutText', 'SetPrimary', 'SendPrimary')
if clipboard_policy == 'unimplemented':
    assert helper.get('CLIPBOARD_POLICY') == 'unimplemented'
    assert all(flag.lower() not in help_text for flag in clipboard_flags)
else:
    for mode in ('view-only', 'interactive'):
        options = helper['command']('/run/kodi/probe-unused.sock', 2, mode)
        assert all('-'+flag+'=0' in options for flag in clipboard_flags)
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
process_probe = EXPECTED_PROCESS_PROBE
if process_probe == 'piers-loader':
    pids = []
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            with process.joinpath('cmdline').open('rb') as stream:
                cmdline = stream.read(8193)
            if len(cmdline) > 8192:
                continue
            if matches_piers_process(
                cmdline, os.readlink(process/'exe'), process.stat().st_uid
            ):
                pids.append(process.name.encode())
        except (FileNotFoundError, ProcessLookupError):
            pass
else:
    pids = output(['/usr/bin/pgrep', '-x', 'kodi.bin']).stdout.split()
assert len(pids) == 1
with open('/proc/'+pids[0].decode()+'/environ', 'rb') as stream:
    environment = stream.read(65537)
assert len(environment) <= 65536 and b'LP_NUM_THREADS=1' in environment.split(b'\0')
print(json.dumps({'schema':'reviewed-display-packaging-v2',
    'helper_integrity_before_execution':True, 'geometry_probe':'xlib',
    **({'clipboard_policy':clipboard_policy}
       if clipboard_policy == 'unimplemented' else {}),
    **({'process_probe':process_probe} if process_probe == 'piers-loader' else {}),
    'helper_digest':helper_digest,
    'package':package, 'width':width, 'height':height,
    'lp_num_threads':1, 'helper_probe':True, 'neutral_input_probe':True,
    'options_supported':True, 'libraries':True}))
""".strip()
)


def display_validation_command(capability: DisplayCapability) -> tuple[str, ...]:
    program = _PROGRAM.replace(
        "EXPECTED_HELPER_DIGEST", json.dumps(capability.helper_digest)
    )
    program = program.replace(
        "EXPECTED_CLIPBOARD_POLICY", json.dumps(capability.clipboard_policy)
    )
    program = program.replace(
        "EXPECTED_PROCESS_PROBE", json.dumps(capability.process_probe)
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
    if capability.clipboard_policy == "unimplemented":
        expected["clipboard_policy"] = "unimplemented"
    if capability.process_probe == "piers-loader":
        expected["process_probe"] = "piers-loader"
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
