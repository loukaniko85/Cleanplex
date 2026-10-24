"""Single source of truth for Cleanplex's runtime data directory.

Everything persistent — the SQLite database, downloaded models, segment
thumbnails and the Plex poster cache — lives under this directory. In Docker it
is `/data`, which is the only path that needs a volume.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLEANPLEX_DATA", Path.home() / ".cleanplex"))


def ensure_data_dirs() -> None:
    """Create the data root and the cache directories used by the app.

    A clear message here saves a confusing SQLite "unable to open database file"
    when a bind-mounted /data is owned by the wrong UID.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("posters", "thumbnails", "models"):
            (DATA_DIR / name).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot write to CLEANPLEX_DATA={DATA_DIR}: {exc}. "
            "In Docker, the host folder mapped to /data must be writable by UID 1000 "
            "(sudo chown -R 1000:1000 <host-folder>), or use a named volume."
        ) from exc
