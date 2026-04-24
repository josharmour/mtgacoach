"""Screen capture helpers that survive DirectX / Unity windows.

`PIL.ImageGrab.grab(bbox=...)` uses GDI `BitBlt` which reads the DWM
compositor bitmap — and that's usually empty for Unity's DirectX back
buffer. MTGA then shows up as a solid black rectangle in the captured
PNG, which is why "Visual Analysis" was failing.

`PrintWindow` with the `PW_RENDERFULLCONTENT` (0x2, Win 8.1+) flag asks
Windows to render the window's current frame into a device context,
including DirectX content. It's the reliable path for Unity games.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import io
import logging
import sys
from typing import Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

PW_RENDERFULLCONTENT = 0x00000002


def _import_pil():
    from PIL import Image, ImageGrab
    return Image, ImageGrab


def capture_window_via_printwindow(hwnd: int) -> Optional["Image.Image"]:
    """Return a PIL.Image of the window contents using PrintWindow.

    Works for DirectX / Unity windows where GDI BitBlt returns black.
    Returns None if the capture fails or PIL isn't available.
    """
    if not _IS_WINDOWS or not hwnd:
        return None

    try:
        Image, _ = _import_pil()
    except Exception as e:
        logger.debug(f"PIL import failed in capture_window_via_printwindow: {e}")
        return None

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = ctypes.wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    hdc_window = user32.GetDC(hwnd)
    if not hdc_window:
        return None

    hdc_mem = 0
    hbitmap = 0
    try:
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        if not hdc_mem:
            return None
        hbitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
        if not hbitmap:
            return None
        old_obj = gdi32.SelectObject(hdc_mem, hbitmap)

        ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
        if not ok:
            # Fall back to the (less DirectX-friendly) 0 flag just in case
            ok = user32.PrintWindow(hwnd, hdc_mem, 0)
        gdi32.SelectObject(hdc_mem, old_obj)
        if not ok:
            return None

        bmp_info = _make_bmp_info(width, height)
        buf_size = width * height * 4
        buf = (ctypes.c_ubyte * buf_size)()

        got = gdi32.GetDIBits(
            hdc_mem,
            hbitmap,
            0,
            height,
            buf,
            ctypes.byref(bmp_info),
            0,  # DIB_RGB_COLORS
        )
        if not got:
            return None

        img = Image.frombuffer(
            "RGB",
            (width, height),
            bytes(buf),
            "raw",
            "BGRX",
            0,
            1,
        )
        return img
    except Exception as e:
        logger.debug(f"PrintWindow capture failed: {e}")
        return None
    finally:
        if hbitmap:
            try:
                gdi32.DeleteObject(hbitmap)
            except Exception:
                pass
        if hdc_mem:
            try:
                gdi32.DeleteDC(hdc_mem)
            except Exception:
                pass
        try:
            user32.ReleaseDC(hwnd, hdc_window)
        except Exception:
            pass


def _make_bmp_info(width: int, height: int):
    """BITMAPINFO with a BITMAPINFOHEADER set to 32bpp top-down."""
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32),
            ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32),
            ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16),
            ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", BITMAPINFOHEADER),
            ("bmiColors", ctypes.c_uint32 * 3),
        ]

    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    # Negative height = top-down DIB so the buffer matches PIL's default
    info.bmiHeader.biHeight = -height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0  # BI_RGB
    return info


def is_mostly_black(img, threshold: float = 0.98) -> bool:
    """Return True if at least `threshold` fraction of pixels are near-black.

    Used to detect a failed DirectX capture that returned a black frame.
    Samples the image rather than walking every pixel.
    """
    if img is None:
        return True
    try:
        small = img.copy()
        small.thumbnail((128, 128))
        pixels = list(small.convert("L").getdata())
        if not pixels:
            return True
        dark = sum(1 for p in pixels if p < 8)
        return (dark / len(pixels)) >= threshold
    except Exception:
        return False


def capture_mtga_png(
    hwnd: Optional[int],
    bbox: Optional[tuple[int, int, int, int]] = None,
) -> Optional[bytes]:
    """Capture MTGA as PNG bytes, surviving DirectX back-buffers.

    Strategy:
      1. If we have an `hwnd`, try PrintWindow first — it's the only
         method that reliably works for DirectX/Unity content.
      2. Fall back to ImageGrab.grab (GDI) if PrintWindow fails or PIL
         surprises us with a non-Windows host.
      3. If the result is still a mostly-black frame, log and return
         whatever we have so the caller can react.
    """
    try:
        Image, ImageGrab = _import_pil()
    except Exception as e:
        logger.error(f"PIL unavailable for screenshot capture: {e}")
        return None

    img = None
    if _IS_WINDOWS and hwnd:
        img = capture_window_via_printwindow(hwnd)
        if img is not None and is_mostly_black(img):
            logger.info("PrintWindow capture was mostly black, retrying ImageGrab")
            img = None

    if img is None and bbox is not None:
        try:
            left, top, right, bottom = bbox
            img = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        except TypeError:
            # older PIL without all_screens kwarg
            img = ImageGrab.grab(bbox=(bbox[0], bbox[1], bbox[2], bbox[3]))
        except Exception as e:
            logger.debug(f"ImageGrab.grab(bbox) failed: {e}")
            img = None

    if img is None:
        try:
            img = ImageGrab.grab()
        except Exception as e:
            logger.error(f"All screenshot methods failed: {e}")
            return None

    if is_mostly_black(img):
        logger.warning(
            "MTGA screenshot appears mostly black — DirectX capture likely "
            "failed. The coach will warn the user instead of hallucinating."
        )

    try:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Saving screenshot PNG failed: {e}")
        return None
