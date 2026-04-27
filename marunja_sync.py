"""
Marunja Sync - Nautilus extension for OneDrive/SharePoint sync status column
and right-click context menu actions.

Features:
- "Sync" column in list view (✓ Synced / ⟳ Pending / ✗ Error / — Ignored)
- Emblem overlay on file/folder icons
- Right-click menu: Sync Now, Exclude from Sync

Install:
    cp marunja_sync.py ~/.local/share/nautilus-python/extensions/
    nautilus -q && nautilus &
"""

import os
import re
import sqlite3
import shutil
import subprocess
import threading
import time
import weakref
from gi.repository import Nautilus, GObject, GLib

# ---------------------------------------------------------------------------
# Profile definitions: add more SharePoint profiles here as needed
# ---------------------------------------------------------------------------
PROFILES = [
    {
        "name": "OneDrive",
        "db":       os.path.expanduser("~/.config/onedrive/items.sqlite3"),
        "sync_dir": os.path.expanduser("~/onedrive"),
        "confdir":  os.path.expanduser("~/.config/onedrive"),
        "service":  "onedrive",
    },
    {
        "name": "SharePoint/HERA",
        "db":       os.path.expanduser("~/.config/onedrive-sp-separationring/items.sqlite3"),
        "sync_dir": os.path.expanduser("~/sharepoint/HERA"),
        "confdir":  os.path.expanduser("~/.config/onedrive-sp-separationring"),
        "service":  "onedrive-sp-separationring",
    },
]

# How often (seconds) to reload the databases in background
REFRESH_INTERVAL = 30

# Status display strings
STATUS_SYNCED   = "✓ Synced"
STATUS_PENDING  = "⟳ Pending"
STATUS_ERROR    = "✗ Error"
STATUS_IGNORED  = "— Ignored"
STATUS_EXCLUDED = "⊘ Excluded"

# Emblem names from the system icon theme overlaid on file icons
_STATUS_EMBLEM = {
    STATUS_SYNCED:   "emblem-default",       # green checkmark
    STATUS_PENDING:  "emblem-synchronizing", # spinning arrows
    STATUS_ERROR:    "emblem-important",     # red/yellow warning
    STATUS_EXCLUDED: "emblem-important",     # same warning badge for excluded
    STATUS_IGNORED:  None,                   # no emblem
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


def _profile_for_path(abs_path: str):
    """Return the profile dict whose sync_dir contains abs_path, or None."""
    for profile in PROFILES:
        if abs_path == profile["sync_dir"] or abs_path.startswith(profile["sync_dir"] + "/"):
            return profile
    return None


# ---------------------------------------------------------------------------
# Background cache with periodic refresh
# ---------------------------------------------------------------------------

class _SyncCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict = _load_all_profiles()  # initial synchronous load
        self._excluded: set = set()
        self._overrides: dict = {}  # abs_path -> status, cleared on next reload
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            fresh = _load_all_profiles()
            with self._lock:
                self._data = fresh
                self._overrides.clear()  # DB is fresh, drop manual overrides
            time.sleep(REFRESH_INTERVAL)

    def get(self, abs_path: str):
        with self._lock:
            # Excluded takes priority
            for excl in self._excluded:
                if abs_path == excl or abs_path.startswith(excl + "/"):
                    return STATUS_EXCLUDED
            # Manual override (e.g. ⟳ Pending set by Sync Now)
            if abs_path in self._overrides:
                return self._overrides[abs_path]
            return self._data.get(abs_path)

    def exclude(self, abs_path: str):
        """Mark a path as excluded immediately, before the next DB reload."""
        with self._lock:
            self._excluded.add(abs_path)

    def set_pending(self, abs_path: str):
        """Show ⟳ Pending immediately while a sync is running."""
        with self._lock:
            self._overrides[abs_path] = STATUS_PENDING

    def force_reload(self):
        """Reload DB immediately (called after a sync completes)."""
        fresh = _load_all_profiles()
        with self._lock:
            self._data = fresh
            self._overrides.clear()


_cache = _SyncCache()


def _invalidate_by_uris(uris: list):
    """Ask Nautilus to re-query extension info for a list of URIs."""
    for uri in uris:
        f = Nautilus.FileInfo.lookup_for_uri(uri)
        if f:
            f.invalidate_extension_info()
    return False  # GLib.idle_add: don't repeat


# ---------------------------------------------------------------------------
# Config helpers for Exclude action
# ---------------------------------------------------------------------------

def _config_get(confdir: str, key: str) -> str:
    """Read a single key value from onedrive config file."""
    config_path = os.path.join(confdir, "config")
    if not os.path.exists(config_path):
        return ""
    with open(config_path) as f:
        for line in f:
            m = re.match(rf'^\s*{re.escape(key)}\s*=\s*"?([^"]*)"?\s*$', line)
            if m:
                return m.group(1).strip()
    return ""


def _config_set(confdir: str, key: str, value: str):
    """Set a key in onedrive config, appending if not present."""
    config_path = os.path.join(confdir, "config")
    lines = []
    found = False
    if os.path.exists(config_path):
        with open(config_path) as f:
            lines = f.readlines()
    new_lines = []
    for line in lines:
        if re.match(rf'^\s*{re.escape(key)}\s*=', line):
            new_lines.append(f'{key} = "{value}"\n')
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f'{key} = "{value}"\n')
    with open(config_path, "w") as f:
        f.writelines(new_lines)


def _exclude_path(profile: dict, abs_path: str, is_dir: bool):
    """Add abs_path to skip_dir or skip_file in the profile config."""
    rel = os.path.relpath(abs_path, profile["sync_dir"])
    key = "skip_dir" if is_dir else "skip_file"
    current = _config_get(profile["confdir"], key)
    patterns = [p for p in current.split("|") if p] if current else []
    if rel not in patterns:
        patterns.append(rel)
        _config_set(profile["confdir"], key, "|".join(patterns))
    # Restart the systemd user service to pick up config change
    subprocess.Popen(
        ["systemctl", "--user", "restart", profile["service"]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


# ---------------------------------------------------------------------------
# Nautilus column + info provider
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

        emblem = _STATUS_EMBLEM.get(status)
        if emblem:
            file.add_emblem(emblem)


# ---------------------------------------------------------------------------
# Nautilus right-click menu provider
# ---------------------------------------------------------------------------

class MarunjaSyncMenuProvider(GObject.GObject, Nautilus.MenuProvider):

    def _build_menu_items(self, files):
        """Return menu items if all selected files belong to the same profile."""
        if not files:
            return []

        # All files must be local and inside a sync dir
        profiles_found = set()
        for f in files:
            if f.get_uri_scheme() != "file":
                return []
            p = _profile_for_path(f.get_location().get_path())
            if p is None:
                return []
            profiles_found.add(p["name"])

        if len(profiles_found) != 1:
            return []  # mixed profiles, skip

        profile = _profile_for_path(files[0].get_location().get_path())
        items = []

        # --- Sync Now ---
        sync_item = Nautilus.MenuItem(
            name="MarunjaSyncMenu::sync_now",
            label=f"Sync Now  [{profile['name']}]",
            tip="Force an immediate sync with OneDrive / SharePoint",
        )
        sync_item.connect("activate", self._on_sync_now, profile, files)
        items.append(sync_item)

        # --- Exclude from Sync (not shown on the root sync dir itself) ---
        non_root = [f for f in files if f.get_location().get_path() != profile["sync_dir"]]
        if non_root:
            excl_item = Nautilus.MenuItem(
                name="MarunjaSyncMenu::exclude",
                label="Exclude from Sync",
                tip="Stop syncing this item and restart the sync service",
            )
            excl_item.connect("activate", self._on_exclude, profile, non_root)
            items.append(excl_item)

        return items

    def get_file_items(self, *args):
        # Nautilus 3 passes (window, files), Nautilus 4 passes just (files,)
        files = args[-1]
        return self._build_menu_items(files)

    def get_background_items(self, *args):
        folder = args[-1]
        return self._build_menu_items([folder])

    # --- Handlers ---

    def _on_sync_now(self, menu, profile, files):
        # Immediately show ⟳ Pending on all selected files
        uris = []
        for f in files:
            abs_path = f.get_location().get_path()
            _cache.set_pending(abs_path)
            f.invalidate_extension_info()
            uris.append(f.get_uri())

        def _run():
            proc = subprocess.run(
                ["onedrive", "--sync", "--confdir", profile["confdir"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            # Reload cache after sync, then tell Nautilus to re-query
            _cache.force_reload()
            GLib.idle_add(_invalidate_by_uris, uris)

        threading.Thread(target=_run, daemon=True).start()

    def _on_exclude(self, menu, profile, files):
        for f in files:
            abs_path = f.get_location().get_path()
            is_dir = f.get_file_type().value_nick == "directory"
            # Update cache immediately so badge/column change right away
            _cache.exclude(abs_path)
            f.invalidate_extension_info()
            # Then persist to config and restart service
            _exclude_path(profile, abs_path, is_dir)
