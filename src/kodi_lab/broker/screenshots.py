"""Fixed screenshot probes used for reviewed runtime validation."""

from kodi_lab.runtime.display_capabilities import EXPERIMENTAL_OMEGA_DISPLAY

CAPTURE_DEADLINE_SECONDS = 5.0
_FIXED_X11_CAPTURE_PROGRAM = r"""
import socket, struct, sys
def exact(sock, count):
    result = bytearray()
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise SystemExit(21)
        result.extend(chunk)
    return bytes(result)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(4.0)
s.connect('/tmp/.X11-unix/X99')
s.sendall(struct.pack('<BBHHHHH', ord('l'), 0, 11, 0, 0, 0, 0))
setup = exact(s, 8)
status, _reason, _major, _minor, words = struct.unpack('<BBHHH', setup)
body = exact(s, words * 4)
if status != 1 or len(body) < 40:
    raise SystemExit(22)
vendor_length = struct.unpack_from('<H', body, 16)[0]
root_count, format_count = body[20], body[21]
image_byte_order = body[22]
formats_offset = 32 + ((vendor_length + 3) & ~3)
screen_offset = formats_offset + (format_count * 8)
if root_count < 1 or screen_offset + 40 > len(body):
    raise SystemExit(23)
root = struct.unpack_from('<I', body, screen_offset)[0]
width, height = struct.unpack_from('<HH', body, screen_offset + 20)
root_visual = struct.unpack_from('<I', body, screen_offset + 32)[0]
root_depth = body[screen_offset + 38]
depth_count = body[screen_offset + 39]
if (width, height) != (1280, 720):
    raise SystemExit(24)
bits_per_pixel = scanline_pad = None
for index in range(format_count):
    item = formats_offset + (index * 8)
    if item + 8 > len(body):
        raise SystemExit(23)
    depth, bpp, pad = body[item], body[item + 1], body[item + 2]
    if depth == root_depth:
        bits_per_pixel, scanline_pad = bpp, pad
visual_class = bits_per_rgb = None
red_mask = green_mask = blue_mask = None
depth_offset = screen_offset + 40
for _ in range(depth_count):
    if depth_offset + 8 > len(body):
        raise SystemExit(23)
    depth = body[depth_offset]
    visual_count = struct.unpack_from('<H', body, depth_offset + 2)[0]
    visual_offset = depth_offset + 8
    for index in range(visual_count):
        item = visual_offset + (index * 24)
        if item + 24 > len(body):
            raise SystemExit(23)
        visual_id = struct.unpack_from('<I', body, item)[0]
        if depth == root_depth and visual_id == root_visual:
            visual_class = body[item + 4]
            bits_per_rgb = body[item + 5]
            red_mask, green_mask, blue_mask = struct.unpack_from('<III', body, item + 8)
    depth_offset = visual_offset + (visual_count * 24)
if not (
    root_depth == 24
    and bits_per_pixel == 32
    and image_byte_order in (0, 1)
    and scanline_pad == 32
    and visual_class == 4
    and bits_per_rgb == 8
    and red_mask == 0x00ff0000
    and green_mask == 0x0000ff00
    and blue_mask == 0x000000ff
):
    raise SystemExit(26)
bytes_per_line = (
    (width * bits_per_pixel + scanline_pad - 1) // scanline_pad
) * (scanline_pad // 8)
s.sendall(struct.pack('<BBHIhhHHI', 73, 2, 5, root, 0, 0, width, height, 0xffffffff))
reply = exact(s, 32)
kind, depth, _sequence, length, _visual = struct.unpack_from('<BBHII', reply)
if kind != 1 or depth != root_depth or length * 4 != bytes_per_line * height:
    raise SystemExit(25)
pixels = exact(s, length * 4)
sys.stdout.buffer.write(struct.pack(
    '>8s12I', b'KLSX11V1', width, height, root_depth, bits_per_pixel,
    image_byte_order, scanline_pad, bytes_per_line, visual_class, bits_per_rgb,
    red_mask, green_mask, blue_mask,
))
sys.stdout.buffer.write(pixels)
""".strip()
FIXED_SCREENSHOT_COMMAND = (
    "/usr/bin/python3",
    "-I",
    "-c",
    _FIXED_X11_CAPTURE_PROGRAM,
)


_EXPERIMENTAL_X11_CAPTURE_PROGRAM = (
    _FIXED_X11_CAPTURE_PROGRAM.replace(
        "(width, height) != (1280, 720)",
        f"(width, height) != ({EXPERIMENTAL_OMEGA_DISPLAY.width}, "
        f"{EXPERIMENTAL_OMEGA_DISPLAY.height})",
    )
    .replace(
        "reply = exact(s, 32)\nkind, depth",
        "reply = exact(s, 32)\nmapping_events = 0\n"
        "while reply[0] == 34:\n"
        "    mapping_events += 1\n"
        "    if mapping_events > 16:\n"
        "        raise SystemExit(25)\n"
        "    reply = exact(s, 32)\n"
        "kind, depth",
    )
    .replace(
        "sys.stdout.buffer.write(struct.pack(",
        "rows=[]\nfor y in range(height):\n"
        "    row=pixels[y*bytes_per_line:(y+1)*bytes_per_line]\n"
        "    scaled=b''.join(row[x:x+4]*2 for x in range(0,len(row),4))\n"
        "    rows.extend((scaled,scaled))\n"
        "pixels=b''.join(rows)\nwidth,height=1280,720\nbytes_per_line=width*4\n"
        "sys.stdout.buffer.write(struct.pack(",
    )
)
