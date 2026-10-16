"""Lossless TGA -> PNG re-encoding, with a pixel-identity proof for every file converted.

Why this is safe for Ironwail: Image_LoadImage (image.c) opens "<name>.png", then "<name>.tga", then "<name>.jpg" and decodes
whichever exists with stbi_load_from_file(..., req_comp = 4), i.e. always to RGBA. So a PNG that decodes to the same RGBA as the
TGA is indistinguishable to the engine for every texture and skybox that is looked up by name.

Correctness does not rely on Pillow's TGA reader (stb_image and Pillow disagree on some variants). The TGA is decoded here, following
stb_image.h stbi__tga_load for the supported subset; Pillow is used only as a PNG writer, and every PNG is decoded again and compared
byte for byte with the pixels decoded from the TGA. Anything outside the subset is left untouched and reported.

Supported: uncompressed (type 2) and RLE (type 10) truecolor, 24 or 32 bits per pixel, no colour map, left-to-right, not interleaved.
Skipped (kept as TGA): colour-mapped, greyscale, 15/16-bit (stb_image's 5-bit expansion differs from common decoders), right-to-left
or interleaved, truncated or over-long RLE data, oversized images, files whose PNG would not be smaller.
"""
from __future__ import annotations

import io
import struct

MAX_PIXELS = 16_777_216          # 4096 x 4096; larger images are left alone (bounds memory)
MIN_SAVING = 0.99                # PNG must be at most 99% of the TGA to be used


class Skip(Exception):
    """The file is deliberately left as it is. `reason` is a short stable code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def decode_tga(data: bytes) -> tuple[int, int, int, bytes]:
    """Return (width, height, channels, pixels). Pixels are top-down, RGB or RGBA, exactly as stb_image would return them."""
    if len(data) < 18:
        raise Skip("truncated")
    id_len, cmap_type, image_type = data[0], data[1], data[2]
    width, height = struct.unpack("<HH", data[12:16])
    bpp, desc = data[16], data[17]
    if cmap_type != 0:
        raise Skip("colour-mapped")
    if image_type not in (2, 10):
        raise Skip("unsupported-type")
    if bpp not in (24, 32):
        raise Skip(f"unsupported-depth-{bpp}")
    if desc & 0x10 or desc & 0xC0:
        raise Skip("unsupported-orientation")
    if width == 0 or height == 0:
        raise Skip("empty")
    if width * height > MAX_PIXELS:
        raise Skip("too-large")
    spp = bpp // 8
    total = width * height * spp
    pos = 18 + id_len
    if image_type == 2:
        raw = data[pos:pos + total]
        if len(raw) != total:
            raise Skip("truncated")
    else:
        raw_buf = bytearray()
        n = len(data)
        npix = width * height
        got = 0
        while got < npix:
            if pos >= n:
                raise Skip("truncated")
            head = data[pos]
            pos += 1
            count = (head & 0x7F) + 1
            if got + count > npix:
                raise Skip("rle-overrun")
            if head & 0x80:
                pixel = data[pos:pos + spp]
                if len(pixel) != spp:
                    raise Skip("truncated")
                pos += spp
                raw_buf += pixel * count
            else:
                chunk = data[pos:pos + count * spp]
                if len(chunk) != count * spp:
                    raise Skip("truncated")
                pos += count * spp
                raw_buf += chunk
            got += count
        raw = bytes(raw_buf)
    out = bytearray(total)
    out[0::spp] = raw[2::spp]       # BGR(A) -> RGB(A)
    out[1::spp] = raw[1::spp]
    out[2::spp] = raw[0::spp]
    if spp == 4:
        out[3::4] = raw[3::4]
    if not desc & 0x20:             # bit 5 clear = bottom-left origin; stb_image flips it to top-down
        row = width * spp
        out = b"".join(bytes(out[i * row:(i + 1) * row]) for i in range(height - 1, -1, -1))
    return width, height, spp, bytes(out)


def _png_module():
    try:
        from PIL import Image
    except ImportError as exc:      # Pillow is a declared dependency; fail with a clear message if it is missing
        raise RuntimeError("Lossless image re-encoding needs the Pillow package, which is not installed") from exc
    return Image


def tga_to_png(data: bytes, compress_level: int = 9) -> tuple[bytes, dict]:
    """Return (png bytes, info). Raises Skip when the file should stay a TGA."""
    Image = _png_module()
    width, height, spp, pixels = decode_tga(data)
    mode = "RGBA" if spp == 4 else "RGB"
    buf = io.BytesIO()
    Image.frombytes(mode, (width, height), pixels).save(buf, format="PNG", compress_level=compress_level)
    png = buf.getvalue()
    # Proof: decode the PNG we just wrote and compare every byte with the TGA's pixels.
    with Image.open(io.BytesIO(png)) as check:
        check.load()
        if check.mode != mode or check.size != (width, height) or check.tobytes() != pixels:
            raise Skip("verification-failed")
    if len(png) > len(data) * MIN_SAVING:
        raise Skip("not-smaller")
    return png, {"width": width, "height": height, "channels": spp, "tga_bytes": len(data), "png_bytes": len(png)}


def versions() -> dict[str, str]:
    """Library versions that influence the PNG bytes (recorded so a rebuild can be explained)."""
    import zlib
    try:
        import PIL
        pillow = PIL.__version__
    except ImportError:
        pillow = "missing"
    return {"pillow": pillow, "zlib": zlib.ZLIB_VERSION}
