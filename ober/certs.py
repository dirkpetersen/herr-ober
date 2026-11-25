"""Certificate expiry check shared by `ober status`, `ober doctor` and `ober test`."""

import os
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

    ``path`` can be a directory (HAProxy ``crt <dir>``): each ``*.pem`` in it is checked and
    the one with the fewest days left (or that cannot be read) is reported.

    Returns:
        Dict with ``level`` ("ok", "warn" or "crit"), ``days_left`` (int or None)
        and ``message``.
    """
    if Path(path).is_dir():
        results = [
            {**r, "message": f"{pem.name}: {r['message']}"}
            for pem in sorted(Path(path).glob("*.pem"))
            for r in [check_cert_expiry(pem, now)]
        ]
        if not results:
            return {"level": "warn", "days_left": None, "message": f"No *.pem file in {path}"}
        return min(results, key=lambda r: -1e9 if r["days_left"] is None else r["days_left"])
    if Path(path).is_file() and not os.access(path, os.R_OK):
        # a non-root monitoring user cannot read the key file: not a certificate problem
        return {"level": "warn", "days_left": None, "message": f"Cannot read {path}, run as root"}
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
