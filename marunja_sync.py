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
import urllib.parse
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
# Config helpers
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


def _get_skip_patterns(confdir: str) -> set:
    """Read skip_dir and skip_file patterns from onedrive config."""
    patterns = set()
    for key in ("skip_dir", "skip_file"):
        val = _config_get(confdir, key)
        if val:
            for p in val.split("|"):
                p = p.strip()
                if p:
                    patterns.add(p)
    return patterns


def _is_excluded(rel_path: str, skip_patterns: set) -> bool:
    """Check if a relative path matches any skip pattern."""
    parts = rel_path.split("/")
    # Check each component and each prefix
    for i in range(len(parts)):
        segment = parts[i]
        prefix = "/".join(parts[: i + 1])
        if segment in skip_patterns or prefix in skip_patterns:
            return True
    return False


def _load_profile(profile: dict) -> dict:
    """Return {absolute_path: status_string} for one profile."""
    db_path = profile["db"]
    sync_dir = profile["sync_dir"].rstrip("/")
    result = {}

    if not os.path.exists(db_path):
        return result

    skip_patterns = _get_skip_patterns(profile["confdir"])

    # Unique temp file per thread to avoid cross-thread file conflicts
    tmp = f"/tmp/marunja_{os.path.basename(db_path)}_{threading.get_ident()}.tmp"
    try:
        shutil.copy2(db_path, tmp)
        con = sqlite3.connect(tmp)
        for rel_path, sync_status in con.execute(_REBUILD_SQL):
            abs_path = sync_dir + "/" + rel_path
            if _is_excluded(rel_path, skip_patterns):
                status = STATUS_EXCLUDED
            elif sync_status == "Y":
                status = STATUS_SYNCED
            elif sync_status is None:
                status = STATUS_PENDING
            else:
                status = STATUS_ERROR
            result[abs_path] = status
        con.close()
    except Exception as e:
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
            for excl in self._excluded:
                if abs_path == excl or abs_path.startswith(excl + "/"):
                    return STATUS_EXCLUDED
            if abs_path in self._overrides:
                return self._overrides[abs_path]
            return self._data.get(abs_path)

    def exclude(self, abs_path: str):
        """Mark a path as excluded immediately, before the next DB reload."""
        with self._lock:
            self._excluded.add(abs_path)

    def set_pending(self, abs_path: str):
        """Show ⟳ Pending immediately while a sync is running.
        Removes the path AND any excluded children from the excluded set."""
        with self._lock:
            # Remove exact match and any children (e.g. syncing parent re-includes subs)
            self._excluded = {
                e for e in self._excluded
                if e != abs_path and not e.startswith(abs_path + "/")
            }
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

def _restart_service(profile: dict):
    subprocess.Popen(
        ["systemctl", "--user", "restart", profile["service"]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _exclude_path(profile: dict, abs_path: str, is_dir: bool):
    """Add abs_path to skip_dir or skip_file in the profile config."""
    rel = os.path.relpath(abs_path, profile["sync_dir"])
    key = "skip_dir" if is_dir else "skip_file"
    current = _config_get(profile["confdir"], key)
    patterns = [p for p in current.split("|") if p] if current else []
    if rel not in patterns:
        patterns.append(rel)
        _config_set(profile["confdir"], key, "|".join(patterns))
    _restart_service(profile)


def _reinclude_path(profile: dict, abs_path: str, is_dir: bool):
    """Remove abs_path from skip_dir or skip_file in the profile config."""
    rel = os.path.relpath(abs_path, profile["sync_dir"])
    key = "skip_dir" if is_dir else "skip_file"
    current = _config_get(profile["confdir"], key)
    patterns = [p for p in current.split("|") if p] if current else []
    patterns = [p for p in patterns if p != rel]
    _config_set(profile["confdir"], key, "|".join(patterns))
    _restart_service(profile)


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

        # While pending, re-check every 2s until status changes
        if status == STATUS_PENDING:
            _file_ref = [file]  # prevent GC
            def _recheck():
                if _cache.get(abs_path) == STATUS_PENDING:
                    return True
                try:
                    _file_ref[0].invalidate_extension_info()
                except Exception:
                    pass
                _file_ref.clear()
                return False
            GLib.timeout_add(2000, _recheck)

        if status is None:
            for profile in PROFILES:
                if abs_path == profile["sync_dir"]:
                    status = STATUS_SYNCED
                    break
                elif abs_path.startswith(profile["sync_dir"] + "/"):
                    status = STATUS_IGNORED
                    # The DB might not yet know about this path (e.g. just
                    # added/synced after the last cache reload). Re-check a
                    # few times so a freshly-synced item stops showing Ignored.
                    _file_ref = [file]
                    _attempts = [0]
                    # Generous cap: covers cases where the onedrive service
                    # is down and needs manual recovery (--resync etc.)
                    _MAX_ATTEMPTS = (30 * 60) // 5  # 30 min at 5s
                    def _recheck_ignored():
                        _attempts[0] += 1
                        new_status = _cache.get(abs_path)
                        if new_status is None:
                            if _attempts[0] >= _MAX_ATTEMPTS:
                                _file_ref.clear()
                                return False
                            return True
                        try:
                            _file_ref[0].invalidate_extension_info()
                        except Exception:
                            pass
                        _file_ref.clear()
                        return False
                    GLib.timeout_add(5000, _recheck_ignored)
                    break

        file.add_string_attribute("sync_status", status or "")

        emblem = _STATUS_EMBLEM.get(status)
        if emblem:
            file.add_emblem(emblem)


# ---------------------------------------------------------------------------
# Nautilus right-click menu provider
# ---------------------------------------------------------------------------

class MarunjaSyncMenuProvider(GObject.GObject, Nautilus.MenuProvider):

    def _extract_infos(self, files):
        """Extract stable string data from GObject file refs before they're GC'd."""
        return [
            {
                "abs_path": f.get_location().get_path(),
                "uri":      f.get_uri(),
                "is_dir":   f.get_file_type().value_nick == "directory",
            }
            for f in files
            if f.get_uri_scheme() == "file"
        ]

    def get_file_items(self, *args):
        """Right-click on selected files/folders."""
        files = args[-1]
        if not files:
            return []

        infos = self._extract_infos(files)
        profile_names = {_profile_for_path(fi["abs_path"])["name"]
                         for fi in infos
                         if _profile_for_path(fi["abs_path"]) is not None}
        if len(profile_names) != 1:
            return []

        profile = _profile_for_path(infos[0]["abs_path"])
        items = []

        # If selection IS the root sync dir → only Sync Now
        root_selected = any(fi["abs_path"] == profile["sync_dir"] for fi in infos)
        if root_selected:
            item = Nautilus.MenuItem(
                name="MarunjaSyncMenu::sync_now_file",
                label=f"Sync Now  [{profile['name']}]",
                tip="Force an immediate sync with OneDrive / SharePoint",
            )
            uri = infos[0]["uri"]
            item.connect("activate", self._on_sync_now, profile, uri)
            return [item]

        # Normal items: Exclude or Re-include
        excluded = [fi for fi in infos if _cache.get(fi["abs_path"]) == STATUS_EXCLUDED]
        included = [fi for fi in infos if _cache.get(fi["abs_path"]) != STATUS_EXCLUDED]

        if included:
            item = Nautilus.MenuItem(
                name="MarunjaSyncMenu::exclude",
                label="Exclude from Sync",
                tip="Stop syncing this item",
            )
            item.connect("activate", self._on_exclude, profile, included)
            items.append(item)

        if excluded:
            item = Nautilus.MenuItem(
                name="MarunjaSyncMenu::reinclude",
                label="Re-include in Sync",
                tip="Resume syncing this item",
            )
            item.connect("activate", self._on_reinclude, profile, excluded)
            items.append(item)

        return items

    def get_background_items(self, *args):
        """Right-click on folder background: Sync Now."""
        folder = args[-1]
        if not folder or folder.get_uri_scheme() != "file":
            return []

        abs_path = folder.get_location().get_path()
        profile = _profile_for_path(abs_path)
        if not profile:
            return []

        item = Nautilus.MenuItem(
            name="MarunjaSyncMenu::sync_now",
            label=f"Sync Now  [{profile['name']}]",
            tip="Force an immediate sync with OneDrive / SharePoint",
        )
        uri = folder.get_uri()
        item.connect("activate", self._on_sync_now, profile, uri)
        return [item]

    # --- Handlers ---

    def _on_sync_now(self, menu, profile, folder_uri):
        GLib.idle_add(_invalidate_by_uris, [folder_uri])

        def _run():
            subprocess.run(
                ["onedrive", "--sync", "--confdir", profile["confdir"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            fresh = _load_all_profiles()
            with _cache._lock:
                _cache._data = fresh
                _cache._overrides.clear()

            # Build URI list for the folder + its immediate children so
            # the column refreshes instantly, no need to wait for the
            # background timer to tick.
            uris = [folder_uri]
            folder_path = urllib.parse.unquote(folder_uri[7:]) if folder_uri.startswith("file://") else None
            if folder_path and os.path.isdir(folder_path):
                try:
                    for name in os.listdir(folder_path):
                        uris.append(folder_uri + "/" + urllib.parse.quote(name))
                except OSError:
                    pass
            GLib.idle_add(_invalidate_by_uris, uris)

        threading.Thread(target=_run, daemon=True).start()

    def _on_exclude(self, menu, profile, file_infos):
        uris = [fi["uri"] for fi in file_infos]
        for fi in file_infos:
            _cache.exclude(fi["abs_path"])
            _exclude_path(profile, fi["abs_path"], fi["is_dir"])
        GLib.idle_add(_invalidate_by_uris, uris)

    def _on_reinclude(self, menu, profile, file_infos):
        uris = [fi["uri"] for fi in file_infos]
        for fi in file_infos:
            _cache.set_pending(fi["abs_path"])
            _reinclude_path(profile, fi["abs_path"], fi["is_dir"])
        GLib.idle_add(_invalidate_by_uris, uris)
