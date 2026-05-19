"""
Installed-app index — Windows app discovery & smart matching.

Discovers every app the Windows Start Menu can launch (UWP / MSIX, classic
Win32 with shortcuts, Microsoft Store, Steam, anything `Get-StartApps`
returns) and provides token-aware matching so:

    "open microsoft edge"  → Microsoft Edge       (NOT Microsoft Excel)
    "open whatsapp"        → WhatsApp desktop app (NOT web.whatsapp.com)
    "open whatsapp beta"   → WhatsApp Beta        (matches the longer name)
    "open youtube"         → YouTube Store app    (NOT youtube.com fallback)

Launch path: `explorer.exe shell:AppsFolder\\<AppID>`. That's the canonical
Windows mechanism — it works for every app type the Start Menu can launch,
so we don't have to maintain separate code paths for UWP vs. Win32 vs.
URI-protocol handlers.

The index is rebuilt on first import if no cache exists, and again if the
cache is older than ``_MAX_AGE_HOURS``. Sir can also force a rebuild with
the voice command "refresh my apps" (wired up in command_router).

Cache location: ``%USERPROFILE%\\.friday\\app_index.json``.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(os.path.expanduser("~")) / ".friday"
_CACHE_FILE = _CACHE_DIR / "app_index.json"
_MAX_AGE_HOURS = 24


@dataclass
class App:
    """One installed application: human name + AppsFolder identifier."""
    name: str
    app_id: str

    @property
    def is_uwp(self) -> bool:
        """UWP / MSIX AppIDs contain '!' (e.g. ``Pub.Name_hash!Tag``)."""
        return "!" in self.app_id


# ─── Discovery ───────────────────────────────────────────────────────────────

def _powershell_get_start_apps() -> list[App]:
    """Run ``Get-StartApps`` and parse the JSON output.

    Falls back to an empty list on any failure — caller decides whether
    to ask the user, log a warning, or just skip the index lookup.
    """
    if sys.platform != "win32":
        return []
    cmd = [
        "powershell", "-NoProfile", "-NonInteractive",
        "-Command", "Get-StartApps | ConvertTo-Json -Compress",
    ]
    try:
        flags = 0
        if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
            creationflags=flags,
        )
        if result.returncode != 0:
            logger.warning(
                "Get-StartApps failed (rc=%d): %s",
                result.returncode, result.stderr[:200],
            )
            return []
        data = json.loads(result.stdout or "[]")
        # PowerShell ConvertTo-Json emits an object (not array) when there's
        # only one result. Normalise both shapes.
        if isinstance(data, dict):
            data = [data]
        apps: list[App] = []
        for item in data:
            try:
                name = str(item["Name"]).strip()
                app_id = str(item["AppID"]).strip()
                if name and app_id:
                    apps.append(App(name=name, app_id=app_id))
            except (KeyError, TypeError):
                continue
        return apps
    except Exception as exc:
        logger.warning("Failed to enumerate Start Menu apps: %s", exc)
        return []


# ─── Cache ───────────────────────────────────────────────────────────────────

def _save_cache(apps: list[App]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "apps": [asdict(a) for a in apps],
        }
        _CACHE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("Failed to save app index cache: %s", exc)


def _load_cache() -> Optional[list[App]]:
    try:
        if not _CACHE_FILE.exists():
            return None
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        age_hours = (time.time() - float(data.get("saved_at", 0))) / 3600.0
        if age_hours > _MAX_AGE_HOURS:
            logger.debug("App index cache stale (%.1fh) — will rebuild", age_hours)
            return None
        return [App(**a) for a in data.get("apps", [])]
    except Exception as exc:
        logger.debug("Failed to load app index cache: %s", exc)
        return None


_INDEX: list[App] = []


def _ensure_loaded() -> None:
    """Lazy-load the index (cache → rebuild) on first use."""
    global _INDEX
    if _INDEX:
        return
    cached = _load_cache()
    if cached is not None:
        _INDEX = cached
        logger.debug("Loaded %d apps from cache", len(_INDEX))
        return
    refresh()


def refresh() -> int:
    """Re-enumerate the Start Menu and rebuild the cache.

    Returns the number of apps discovered (0 on failure — old cache, if any,
    stays in memory).
    """
    global _INDEX
    apps = _powershell_get_start_apps()
    if apps:
        _INDEX = apps
        _save_cache(apps)
        logger.info("App index rebuilt: %d apps", len(apps))
    elif not _INDEX:
        logger.warning("App index empty — Get-StartApps returned nothing")
    return len(_INDEX)


# ─── Matching ────────────────────────────────────────────────────────────────

# Words we strip from both query and app name before comparison. "The Edge"
# and "Edge" should match identically; "WhatsApp App" and "WhatsApp" too.
_STOPWORDS = frozenset({"the", "app", "application", "a", "an"})

# Sir often says these as synonyms; map them to the canonical form before
# scoring so "vs code" matches "Visual Studio Code" cleanly.
_QUERY_ALIASES = {
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "wispr": "wispr flow",
    "whisper flow": "wispr flow",
    "whisper": "wispr flow",
    "claude desktop": "claude",
    "gpt": "chatgpt",
    "chat gpt": "chatgpt",
    "ms edge": "microsoft edge",
    "ms word": "word",
    "ms excel": "excel",
    "ms powerpoint": "powerpoint",
    "discord app": "discord",
    "whats app": "whatsapp",
    "what's app": "whatsapp",
}


def _tokenize(s: str) -> list[str]:
    """Lowercase, drop punctuation, split on word boundaries, drop stopwords."""
    tokens = re.findall(r"[a-z0-9]+", s.lower())
    return [t for t in tokens if t not in _STOPWORDS]


def _apply_aliases(query: str) -> str:
    q = query.lower().strip().rstrip(".")
    return _QUERY_ALIASES.get(q, q)


def _score(query_tokens: list[str], query_joined: str, app: App) -> float:
    """Score how well an installed app matches the query.

    Priority bands (higher = better):
        1000   exact name match
         800   query is a prefix of the name AND all query tokens present
         600   every query token appears in the name (token containment)
         100×J Jaccard overlap on token sets (partial match)

    Within a band, shorter names win — so "edge" prefers "Microsoft Edge"
    over "Microsoft Edge Beta" if both exist.
    """
    name_lower = app.name.lower()
    if name_lower == query_joined:
        return 1000.0

    app_tokens = _tokenize(app.name)
    if not app_tokens:
        return 0.0
    app_set = set(app_tokens)
    query_set = set(query_tokens)

    if query_set.issubset(app_set):
        if name_lower.startswith(query_joined):
            base = 800.0
        else:
            base = 600.0
        # Slight penalty per extra token in the app name so the most
        # specific match wins. e.g. for query "edge": "Edge" beats
        # "Microsoft Edge" beats "Microsoft Edge Dev".
        extra = len(app_set) - len(query_set)
        return base - 2.0 * extra

    overlap = query_set & app_set
    if not overlap:
        return 0.0
    jaccard = len(overlap) / max(1, len(query_set | app_set))
    return 100.0 * jaccard


def find_app(query: str, *, min_score: float = 100.0) -> Optional[App]:
    """Return the installed app that best matches ``query``, or None.

    Examples (with the apps Sir has installed today):
        find_app("edge")            → Microsoft Edge
        find_app("microsoft edge")  → Microsoft Edge (NOT Excel)
        find_app("whatsapp")        → WhatsApp
        find_app("whatsapp beta")   → WhatsApp Beta
        find_app("youtube")         → YouTube
        find_app("excel")           → Excel
        find_app("vs code")         → Visual Studio Code (via alias)
        find_app("nonexistent")     → None
    """
    _ensure_loaded()
    if not _INDEX:
        return None

    normalised = _apply_aliases(query)
    query_tokens = _tokenize(normalised)
    if not query_tokens:
        return None
    query_joined = " ".join(query_tokens)

    best: Optional[App] = None
    best_score = float(min_score) - 1.0
    for app in _INDEX:
        s = _score(query_tokens, query_joined, app)
        if s > best_score:
            best_score = s
            best = app

    if best is not None:
        logger.info(
            "Matched %r → %r (AppID=%s, score=%.1f)",
            query, best.name, best.app_id, best_score,
        )
    return best


def all_apps() -> list[App]:
    """Return a copy of the current index (for /apps endpoints or debugging)."""
    _ensure_loaded()
    return list(_INDEX)


# ─── Launch ──────────────────────────────────────────────────────────────────

def launch(app: App) -> bool:
    """Launch an installed app via ``shell:AppsFolder``.

    Works uniformly for UWP, MSIX, Win32 shortcuts, Steam, and anything else
    in Get-StartApps. Returns True on success.
    """
    if sys.platform != "win32":
        logger.warning("launch() is Windows-only")
        return False
    target = f"shell:AppsFolder\\{app.app_id}"
    try:
        # explorer.exe is the canonical handler for shell:* paths.
        # shell=False + a fixed argv avoids any quoting issues with the
        # exclamation-mark in UWP AppIDs.
        subprocess.Popen(["explorer.exe", target], shell=False)
        logger.info("Launched %r via %s", app.name, target)
        return True
    except Exception as exc:
        logger.error("Failed to launch %r (%s): %s", app.name, target, exc)
        return False


def find_and_launch(query: str) -> Optional[App]:
    """Convenience: find_app(query) → launch() → return the App that ran."""
    app = find_app(query)
    if app is None:
        return None
    return app if launch(app) else None


# ─── Standalone smoke test ───────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    n = refresh()
    print(f"Discovered {n} apps")
    print()
    for q in [
        "edge", "microsoft edge", "whatsapp", "whatsapp beta",
        "youtube", "excel", "claude", "chrome", "google chrome",
        "wispr flow", "wispr", "vs code", "nonexistent app xyz",
    ]:
        app = find_app(q)
        if app:
            print(f"  {q!r:25} → {app.name!r:25} ({app.app_id})")
        else:
            print(f"  {q!r:25} → (no match)")
