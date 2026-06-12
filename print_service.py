"""
print_service.py — Silent direct-to-printer service using ESC/POS.

Sends raw ESC/POS bytes straight to the printer, completely bypassing
any browser print dialog.

Printer detection order:
  Windows → pywin32 (win32print) → default printer
  Linux   → /dev/usb/lp0, /dev/usb/lp1, /dev/lp0 → lp command fallback
"""

import logging
import os
import platform
import subprocess
import tempfile
from datetime import datetime

logger = logging.getLogger("print_service")

# ── ESC/POS command bytes ─────────────────────────────────────────────────────
ESC = b'\x1b'
GS  = b'\x1d'

INIT         = ESC + b'@'           # Initialize / reset printer
CENTER       = ESC + b'a\x01'       # Center align
LEFT         = ESC + b'a\x00'       # Left align
BOLD_ON      = ESC + b'E\x01'       # Bold on
BOLD_OFF     = ESC + b'E\x00'       # Bold off
SIZE_BIG     = ESC + b'!\x11'       # Double height + bold
SIZE_NORMAL  = ESC + b'!\x00'       # Normal size
CUT          = GS  + b'V\x42\x04'  # Feed 4 lines then full cut
LF           = b'\n'
DIVIDER      = b'================================\n'


def _enc(text: str) -> bytes:
    """Encode text to bytes. Tries UTF-8, falls back to latin-1."""
    try:
        return text.encode('utf-8')
    except Exception:
        return text.encode('latin-1', errors='replace')


def build_ticket(product_name: str, order_id: int, price: str,
                 shop_name: str = "Coffee 24H") -> bytes:
    """
    Build ESC/POS bytes for a single-item kitchen/bar ticket.
    Produces one ticket per call — caller loops for multi-item orders.
    """
    now = datetime.now().strftime("%d/%m/%Y  %H:%M")

    pkt  = INIT
    # ── Header ────────────────────────────────────────────────────────────────
    pkt += CENTER + BOLD_ON + SIZE_BIG
    pkt += _enc(shop_name) + LF
    pkt += SIZE_NORMAL + BOLD_OFF
    pkt += DIVIDER
    # ── Product (big) ─────────────────────────────────────────────────────────
    pkt += CENTER + BOLD_ON + SIZE_BIG
    pkt += _enc(product_name) + LF
    pkt += SIZE_NORMAL + BOLD_OFF
    pkt += DIVIDER
    # ── Order details ─────────────────────────────────────────────────────────
    pkt += LEFT
    pkt += _enc(f"Order : #{order_id}") + LF
    pkt += _enc(f"Price : {price}") + LF
    pkt += _enc(f"Time  : {now}") + LF
    pkt += DIVIDER
    pkt += CENTER
    pkt += _enc("--- Ticket ---") + LF
    # ── Feed & cut ────────────────────────────────────────────────────────────
    pkt += LF + LF + LF
    pkt += CUT
    return pkt


def print_ticket(data: bytes, printer_name: str = None) -> bool:
    """
    Send raw ESC/POS bytes to the printer.
    Returns True on success, False on failure.
    """
    system = platform.system()
    if system == "Windows":
        return _print_windows(data, printer_name)
    else:
        return _print_linux(data)


# ── Windows ───────────────────────────────────────────────────────────────────

def _print_windows(data: bytes, printer_name: str = None) -> bool:
    try:
        import win32print  # type: ignore
        pname = printer_name or win32print.GetDefaultPrinter()
        logger.info("Sending ticket to Windows printer: %s", pname)
        h = win32print.OpenPrinter(pname)
        try:
            win32print.StartDocPrinter(h, 1, ("Ticket", None, "RAW"))
            try:
                win32print.StartPagePrinter(h)
                win32print.WritePrinter(h, data)
                win32print.EndPagePrinter(h)
            finally:
                win32print.EndDocPrinter(h)
        finally:
            win32print.ClosePrinter(h)
        logger.info("Ticket sent successfully.")
        return True

    except ImportError:
        logger.warning("pywin32 not installed — trying temp-file fallback.")
        return _print_windows_file(data)
    except Exception as exc:
        logger.error("Windows print error: %s", exc)
        return False


def _print_windows_file(data: bytes) -> bool:
    """Fallback: dump raw bytes to a .prn temp file and COPY to the printer port."""
    try:
        fd, path = tempfile.mkstemp(suffix=".prn")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # Send to default printer via Windows COPY /B command
        result = subprocess.run(
            f'copy /B "{path}" /D PRN',
            shell=True, capture_output=True
        )
        os.unlink(path)
        return result.returncode == 0
    except Exception as exc:
        logger.error("File-fallback print failed: %s", exc)
        return False


# ── Linux ─────────────────────────────────────────────────────────────────────

def _print_linux(data: bytes) -> bool:
    # 1. Try writing directly to the USB thermal printer device
    for dev in ("/dev/usb/lp0", "/dev/usb/lp1", "/dev/lp0"):
        if os.path.exists(dev):
            try:
                with open(dev, "wb") as f:
                    f.write(data)
                logger.info("Ticket sent to %s", dev)
                return True
            except PermissionError:
                logger.warning(
                    "Permission denied on %s. Run: sudo chmod 666 %s", dev, dev
                )
            except Exception as exc:
                logger.error("Write to %s failed: %s", dev, exc)

    # 2. Fallback: lp -o raw
    try:
        proc = subprocess.run(
            ["lp", "-o", "raw", "-"],
            input=data, capture_output=True, timeout=5
        )
        if proc.returncode == 0:
            logger.info("Ticket sent via lp command.")
            return True
        logger.error("lp command failed: %s", proc.stderr.decode())
    except FileNotFoundError:
        logger.error("lp not found. Install CUPS: sudo apt install cups")
    except Exception as exc:
        logger.error("lp fallback failed: %s", exc)

    return False


# ── List printers (for settings page) ────────────────────────────────────────

def list_printers() -> list[str]:
    """Return a list of available printer names."""
    try:
        if platform.system() == "Windows":
            import win32print  # type: ignore
            return [p[2] for p in win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            )]
    except Exception:
        pass
    return []


def get_default_printer() -> str:
    """Return the default printer name."""
    try:
        if platform.system() == "Windows":
            import win32print  # type: ignore
            return win32print.GetDefaultPrinter()
    except Exception:
        pass
    return "Default"
