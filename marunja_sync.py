"""
Marunja Sync - Nautilus extension for OneDrive/SharePoint sync status column.

Shows a "Sync" column in Nautilus list view reflecting the state from
abraunegg/onedrive SQLite databases.

Install:
    cp marunja_sync.py ~/.local/share/nautilus-python/extensions/
    nautilus -q && nautilus &
"""

import os
import sqlite3
import shutil
import threading
import time
from gi.repository import Nautilus, GObject

# ---------------------------------------------------------------------------
# Profile definitions: add more SharePoint profiles here as needed
# ---------------------------------------------------------------------------
PROFILES = [
    {
        "name": "OneDrive",
        "db": os.path.expanduser("~/.config/onedrive/items.sqlite3"),
        "sync_dir": os.path.expanduser("~/onedrive"),
    },
    {
        "name": "SharePoint/HERA",
        "db": os.path.expanduser("~/.config/onedrive-sp-separationring/items.sqlite3"),
        "sync_dir": os.path.expanduser("~/sharepoint/HERA"),
    },
]

# How often (seconds) to reload the databases in background
REFRESH_INTERVAL = 30

# Status display strings
STATUS_SYNCED  = "✓ Synced"
STATUS_PENDING = "⟳ Pending"
STATUS_ERROR   = "✗ Error"
STATUS_IGNORED = "— Ignored"

# Emblem names from the system icon theme overlaid on file icons
_STATUS_EMBLEM = {
    STATUS_SYNCED:  "emblem-default",       # green checkmark
    STATUS_PENDING: "emblem-synchronizing", # spinning arrows
    STATUS_ERROR:   "emblem-important",     # red/yellow warning
    STATUS_IGNORED: None,                   # no emblem
}

# ---------------------------------------------------------------------------
# DB loader: builds a dict of absolute_path -> status_string
# ---------------------------------------------------------------------------

_REBUILD_SQL = """
WITH RECURSIVE path_cte(id, driveId, type, syncStatus, parentId, full_path) AS (
    SELECT id, driveId, type, syncStatus, parentId, ''
    FROM item WHERE type = 'root'
    UNION ALL
    SELECT i.id, i.driveId, i.type, i.syncStatus, i.parentId,
           CASE WHEN p.full_path = '' THEN i.name
                ELSE p.full_path || '/' || i.name END
    FROM item i
    JOIN path_cte p ON i.parentId = p.id AND i.driveId = p.driveId
)
SELECT full_path, syncStatus FROM path_cte WHERE full_path != ''
"""


def _load_profile(profile: dict) -> dict:
    """Return {absolute_path: status_string} for one profile."""
    db_path = profile["db"]
    sync_dir = profile["sync_dir"].rstrip("/")
    result = {}

    if not os.path.exists(db_path):
        return result

    # Copy to temp to avoid "database is locked"
    tmp = db_path + ".marunja_tmp"
    try:
        shutil.copy2(db_path, tmp)
        con = sqlite3.connect(tmp)
        for rel_path, sync_status in con.execute(_REBUILD_SQL):
            abs_path = sync_dir + "/" + rel_path
            if sync_status == "Y":
                status = STATUS_SYNCED
            elif sync_status is None:
                status = STATUS_PENDING
            else:
                status = STATUS_ERROR
            result[abs_path] = status
        con.close()
    except Exception:
        pass
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    return result


def _load_all_profiles() -> dict:
    combined = {}
    for profile in PROFILES:
        combined.update(_load_profile(profile))
    return combined


# ---------------------------------------------------------------------------
# Background cache with periodic refresh
# ---------------------------------------------------------------------------

class _SyncCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict = _load_all_profiles()  # initial synchronous load
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            fresh = _load_all_profiles()
            with self._lock:
                self._data = fresh
            time.sleep(REFRESH_INTERVAL)

    def get(self, abs_path: str):
        with self._lock:
            return self._data.get(abs_path)


_cache = _SyncCache()


# ---------------------------------------------------------------------------
# Nautilus extension
# ---------------------------------------------------------------------------

class MarunjaSyncProvider(GObject.GObject, Nautilus.ColumnProvider, Nautilus.InfoProvider):

    def get_columns(self):
        return [
            Nautilus.Column(
                name="MarunjaSyncProvider::sync_status",
                attribute="sync_status",
                label="Sync",
                description="OneDrive / SharePoint sync status",
            )
        ]

    def update_file_info(self, file):
        if file.get_uri_scheme() != "file":
            return

        abs_path = file.get_location().get_path()
        status = _cache.get(abs_path)

        if status is None:
            for profile in PROFILES:
                if abs_path == profile["sync_dir"]:
                    status = STATUS_SYNCED
                    break
                elif abs_path.startswith(profile["sync_dir"] + "/"):
                    status = STATUS_IGNORED
                    break

        file.add_string_attribute("sync_status", status or "")

        # Emblem overlay on the file icon
        emblem = _STATUS_EMBLEM.get(status)
        if emblem:
            file.add_emblem(emblem)
