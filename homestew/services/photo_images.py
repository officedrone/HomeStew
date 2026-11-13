"""Pure-stdlib image validation for device photo uploads.

The browser downscales every photo before upload (frontend canvas helper),
so the server never re-encodes: adding an image library would put Pillow's
recurring CVE stream into the runtime image, which 0.2.3 deliberately worked
to shrink. What the server DOES need is to know what it is being handed -
real MIME type and pixel dimensions read straight from the file header - so
it can refuse a mislabeled or oversized image instead of silently storing it.

Supported formats are exactly the ones the chat pipeline already accepts
(PNG/JPEG/WebP) plus GIF; anything else is rejected before touching disk.
"""
import struct
from typing import Optional, Tuple

# Extension-less sniffing: (magic bytes -> canonical mime). WebP needs a
# second look at the RIFF subtype, handled in sniff_image().
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")

SUPPORTED_MIME_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")


def sniff_image(data: bytes) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    """Identify an image payload and read its pixel dimensions.

    Returns ``(mime_type, width, height)``; width/height are None only if
    the container is recognized but its header is too truncated to parse
    (the caller stores it with unknown dimensions - still a valid image).
    Returns None when the bytes are not a supported image at all.
    """
    if data.startswith(_PNG_MAGIC):
        return ("image/png", *_png_size(data))
    if data.startswith(b"\xff\xd8\xff"):
        return ("image/jpeg", *_jpeg_size(data))
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ("image/webp", *_webp_size(data))
    if data[:6] in _GIF_MAGICS:
        return _gif_size(data)
    return None


def _png_size(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    """IHDR (the first chunk) holds width/height as big-endian u32."""
    if len(data) >= 24 and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return (w or None, h or None)
    return (None, None)


def _jpeg_size(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    """Walk the marker segments to the first SOFn frame.

    Baseline/progressive/etc. frames all encode height then width as
    big-endian u16 after the 2-byte segment length. SOF markers are C0-CF
    except C4 (DHT), C8 (JPG) and CC (DAC); E0-EE is where EXIF/JFIF live,
    so scanning stops at the first frame marker past them.
    """
    i = 2  # skip SOI
    n = len(data)
    while i < n - 1:
        if data[i] != 0xFF:
            i += 1  # resync on stray padding bytes
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2  # standalone markers carry no length
            continue
        if marker == 0xD9:  # EOI - malformed to keep scanning
            break
        if len(data) < i + 4:
            break
        (seg_len,) = struct.unpack(">H", data[i + 2 : i + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if len(data) >= i + 9:
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return (w or None, h or None)
            return (None, None)
        i += 2 + seg_len
    return (None, None)


def _webp_size(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    """Parse the VP8/VP8L/VP8X chunk that follows the RIFF header."""
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        # Canvas size is stored as (n-1) in 3-byte little-endian fields.
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return (w, h)
    if chunk == b"VP8L" and len(data) >= 25:
        # Lossless: 14-bit width-1 then 14-bit height-1 packed in 4 bytes.
        bits = int.from_bytes(data[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8 " and len(data) >= 30:
        # Lossy keyframe: after the 3-byte frame tag, sync code 0x9d 0x01
        # 0x2a precedes 16-bit LE width/height (14 significant bits each).
        if data[26:29] == b"\x9d\x01\x2a":
            w = struct.unpack("<H", data[29:31])[0] & 0x3FFF
            h = struct.unpack("<H", data[31:33])[0] & 0x3FFF
            return (w or None, h or None)
    return (None, None)


def _gif_size(data: bytes) -> Tuple[str, Optional[int], Optional[int]]:
    """Logical screen descriptor: two little-endian u16 after the 6-byte magic."""
    if len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return ("image/gif", w or None, h or None)
    return ("image/gif", None, None)
