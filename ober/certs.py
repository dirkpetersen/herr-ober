"""Certificate expiry check shared by `ober status`, `ober doctor` and `ober test`."""

import ssl
import time
from pathlib import Path
from typing import Any

CERT_WARN_DAYS = 21
CERT_FAIL_DAYS = 7


def cert_not_after(path: Path | str) -> float:
    """Return the notAfter time (unix seconds) of the first certificate in a PEM file.

    Raises:
        OSError or ssl.SSLError: if the file cannot be read or holds no certificate.
    """
    # shortcut: private stdlib decoder (no `cryptography` dependency); swap if it is removed.
    info = ssl._ssl._test_decode_cert(str(path))  # type: ignore[attr-defined]
    return float(ssl.cert_time_to_seconds(info["notAfter"]))


def check_cert_expiry(path: Path | str, now: float | None = None) -> dict[str, Any]:
    """Classify a certificate by days left.

    Returns:
        Dict with ``level`` ("ok", "warn" or "crit"), ``days_left`` (int or None)
        and ``message``.
    """
    try:
        not_after = cert_not_after(path)
    except (OSError, ssl.SSLError, KeyError, ValueError) as exc:
        return {"level": "crit", "days_left": None, "message": f"Cannot read {path}: {exc}"}
    days = int((not_after - (time.time() if now is None else now)) // 86400)
    expires = time.strftime("%Y-%m-%d", time.gmtime(not_after))
    if days < 0:
        return {"level": "crit", "days_left": days, "message": f"Expired on {expires}"}
    level = "crit" if days < CERT_FAIL_DAYS else "warn" if days < CERT_WARN_DAYS else "ok"
    return {"level": level, "days_left": days, "message": f"{days} days left (until {expires})"}
