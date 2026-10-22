# -*- coding: utf-8 -*-
"""dualvm.images — tach tu ctf-brain tools.py (pre-split), verbatim.
Xem TODO[coupling] cho cac cho can truyen tham so thay cho ws/reasoner."""
import os, base64, re
from . import remote


# --- Seeing things: images, PDFs, and real GUIs -----------------------------------
# Claude resizes anything whose long edge exceeds ~1568px, and a full-screen capture
# costs ~1.1-1.6k input tokens either way, so shrink BEFORE sending: cheaper and no
# quality lost to a server-side resize we don't control.
IMAGE_MAX_EDGE = 1568


IMAGE_MAX_BYTES = 4_000_000


_VIEWABLE = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "gif": "image/gif", "webp": "image/webp"}


_CONVERTIBLE = {"bmp", "ico", "tif", "tiff", "ppm", "pgm", "pnm", "tga", "dib"}


def _prepare_image(raw, ext):
    """(media_type, base64, note) or (None, None, reason). Converts and downscales
    with Pillow when the HOST has it; falls back to ImageMagick on Kali; and finally
    to sending the bytes untouched when the format is already viewable."""
    ext = (ext or "").lower().lstrip(".")
    note = ""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        w, h = im.size
        changed = False
        if max(w, h) > IMAGE_MAX_EDGE:
            sc = IMAGE_MAX_EDGE / float(max(w, h))
            im = im.resize((max(1, int(w * sc)), max(1, int(h * sc))), Image.LANCZOS)
            note = f" (downscaled {w}x{h} -> {im.size[0]}x{im.size[1]})"
            changed = True
        if ext not in _VIEWABLE or changed:
            buf = io.BytesIO()
            im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(buf, "PNG")
            return "image/png", base64.b64encode(buf.getvalue()).decode(), note or " (converted to png)"
        return _VIEWABLE[ext], base64.b64encode(raw).decode(), f" ({w}x{h})"
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 - a corrupt/odd image is a normal result
        return None, None, f"could not decode as an image ({e.__class__.__name__}: {e})"
    # no Pillow on the host: ImageMagick on Kali can still convert
    if ext in _CONVERTIBLE:
        try:
            remote.write_remote("samples/_viewconv_in", raw, host="kali")
            remote.ssh_exec("cd samples && convert _viewconv_in -resize "
                            f"{IMAGE_MAX_EDGE}x{IMAGE_MAX_EDGE}\\> _viewconv_out.png",
                            host="kali", read_timeout=40)
            png = remote.read_bytes("samples/_viewconv_out.png", host="kali")
            return "image/png", base64.b64encode(png).decode(), " (converted on Kali)"
        except Exception as e:  # noqa: BLE001
            return None, None, f"format .{ext} needs conversion and it failed ({e})"
    if ext in _VIEWABLE:
        if len(raw) > IMAGE_MAX_BYTES:
            return None, None, f"{len(raw)} bytes is over the {IMAGE_MAX_BYTES}-byte cap"
        return _VIEWABLE[ext], base64.b64encode(raw).decode(), " (sent as-is)"
    return None, None, f"format .{ext} is not viewable"
