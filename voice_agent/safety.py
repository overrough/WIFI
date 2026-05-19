"""
Safety primitives — the floor every autonomous action stands on.

Friday is moving from a reactive command router to an agent that takes
actions on Sir's behalf. Before any of those actions land, we install
four guard rails — together they form what Sir asked for: "highly safe
and secure" autonomy.

  1. **Action tiers** — every dispatched action is classified Tier 0-3
     (see ``classify_tier``). Tier 0 is free. Tier 3 requires an
     interactive confirmation we won't suppress, ever.

  2. **Audit log** — ``~/.friday/audit.log``. JSON-lines, append-only,
     timestamped, includes the tier and outcome of every action. Tail
     it from any terminal to see exactly what Friday has done.

  3. **Voice confirmation** — ``request_confirmation(prompt)`` blocks
     for up to 5 s waiting for Sir to say "yes" (or a recognised
     synonym). Used for Tier 2/3 actions. main.py registers the
     recorder + STT bridge at startup; this module stays decoupled.

  4. **Kill switch** — a separate hotkey wired in main.py
     (``Ctrl+Shift+End``) shuts Friday down immediately. Auditing of
     that event happens here so the post-mortem is always available.

The module is intentionally callback-driven: nothing here grabs the mic
or the asyncio loop directly. Set hooks at startup with
``set_confirmation_handler`` and ``set_speak_handler`` and the safety
layer slots in over whatever audio stack the host runs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Callable, Optional, Union

logger = logging.getLogger(__name__)

_FRIDAY_HOME = Path(os.path.expanduser("~")) / ".friday"
_AUDIT_FILE = _FRIDAY_HOME / "audit.log"

# Affirmatives we accept for voice confirmation. Kept short — anything
# longer adds latency at the worst moment ("am I sure I want to shut
# down my computer?"). The matcher does substring containment so
# longer responses ("yes I'm sure, Sir") still work.
_AFFIRMATIVES = (
    "yes", "yeah", "yep", "yup", "yes sir", "yes please", "confirm",
    "confirmed", "do it", "go ahead", "proceed", "authorize", "authorise",
    "i authorize", "i authorise", "affirmative", "absolutely",
)
_NEGATIVES = (
    "no", "nope", "cancel", "stop", "abort", "negative", "don't",
    "do not", "never mind", "nevermind", "wait",
)


class Tier(IntEnum):
    """Action severity. The higher the tier, the more friction before run."""
    SAFE = 0          # open app, search web, read briefing
    MILD = 1          # send WhatsApp text, post Twitter draft, edit a file
    DESTRUCTIVE = 2   # delete files, send external email, post LinkedIn
    SYSTEM = 3        # shut down Friday/PC, change OS settings, install


# Compact action → tier registry. Anything not listed defaults to
# ``Tier.SAFE`` — the rule of thumb is "if it can't undo itself in 5
# seconds, it should be in here." Add entries here, NOT inline at
# call-sites, so the audit is single-source-of-truth.
_TIER_REGISTRY: dict[str, Tier] = {
    # Tier 0 (safe)
    "open_app": Tier.SAFE,
    "open_website": Tier.SAFE,
    "web_search": Tier.SAFE,
    "speak_briefing": Tier.SAFE,
    "refresh_app_index": Tier.SAFE,
    "claude_focus_existing": Tier.SAFE,   # focus + paste only; Sir presses Enter
    "claude_query_cli": Tier.SAFE,        # CLI question; no side-effects yet

    # Tier 1 (mild)
    "send_whatsapp_text": Tier.MILD,
    "post_twitter_draft": Tier.MILD,
    "edit_local_file": Tier.MILD,
    "run_routine": Tier.MILD,
    "claude_open_new_chat": Tier.MILD,    # fresh chat = lost context; warn

    # Tier 2 (destructive)
    "delete_file": Tier.DESTRUCTIVE,
    "send_email_external": Tier.DESTRUCTIVE,
    "post_linkedin": Tier.DESTRUCTIVE,
    "run_arbitrary_script": Tier.DESTRUCTIVE,

    # Tier 3 (system / non-recoverable)
    "shutdown_friday": Tier.SYSTEM,
    "shutdown_pc": Tier.SYSTEM,
    "restart_pc": Tier.SYSTEM,
    "change_os_setting": Tier.SYSTEM,
    "install_software": Tier.SYSTEM,
    "kill_switch_invoked": Tier.SYSTEM,
}


def classify_tier(action: str) -> Tier:
    """Return the registered tier for ``action`` (default Tier.SAFE).

    Unknown actions default to SAFE on purpose — Friday's expanding
    surface area means new low-risk actions will keep landing, and the
    cost of a missing classification on something high-risk is a single
    audit-log entry that flags it for the next developer.
    """
    return _TIER_REGISTRY.get(action, Tier.SAFE)


# ─── Audit log ───────────────────────────────────────────────────────────────

_audit_lock = threading.Lock()


@dataclass
class AuditEvent:
    """One row of the audit log. Persisted as JSONL."""
    ts: float
    action: str
    tier: int
    params: dict = field(default_factory=dict)
    result: str = "ok"
    note: str = ""

    def to_jsonl(self) -> str:
        return json.dumps({
            "ts": self.ts,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts)),
            "action": self.action,
            "tier": self.tier,
            "params": self.params,
            "result": self.result,
            "note": self.note,
        }, ensure_ascii=False)


def audit(action: str, *, result: str = "ok", note: str = "", **params) -> None:
    """Append a single audit event. Never raises — safety logging must
    never become the thing that breaks Friday.
    """
    try:
        ev = AuditEvent(
            ts=time.time(),
            action=action,
            tier=int(classify_tier(action)),
            params=params,
            result=result,
            note=note,
        )
        with _audit_lock:
            _FRIDAY_HOME.mkdir(parents=True, exist_ok=True)
            with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(ev.to_jsonl() + "\n")
    except Exception as exc:
        logger.debug("audit() failed: %s", exc)


# ─── Voice confirmation ──────────────────────────────────────────────────────
# main.py registers the recorder/STT bridge at startup. We just expose
# a synchronous wait-for-yes/no primitive that ANY thread can call.

_CONFIRM_HANDLER: Optional[Callable[[str, float], str]] = None
_SPEAK_HANDLER: Optional[Callable[[str], None]] = None


def set_confirmation_handler(fn: Optional[Callable[[str, float], str]]) -> None:
    """Register a function that:
       - takes (prompt_text, timeout_s)
       - speaks the prompt
       - records & transcribes Sir's reply
       - returns the raw transcript (or "" on timeout/error)
    """
    global _CONFIRM_HANDLER
    _CONFIRM_HANDLER = fn


def set_speak_handler(fn: Optional[Callable[[str], None]]) -> None:
    """Register a simple speak(text) callback used for safety-side
    narration (e.g. "Cancelled, Sir."). Decoupled from the confirmation
    handler so a host that lacks STT can still get TTS feedback.
    """
    global _SPEAK_HANDLER
    _SPEAK_HANDLER = fn


def _classify_reply(text: str) -> Optional[bool]:
    """Map a raw transcript to True / False / None (unclear)."""
    t = (text or "").lower().strip().rstrip(".!?")
    if not t:
        return None
    for word in _AFFIRMATIVES:
        if word in t:
            return True
    for word in _NEGATIVES:
        if word in t:
            return False
    return None


def request_confirmation(
    prompt: str,
    *,
    timeout_s: float = 5.0,
    action: str = "generic_confirm",
) -> bool:
    """Block until Sir confirms (or denies / times out). Returns True
    only on an explicit affirmative. Every outcome is audited.
    """
    if _CONFIRM_HANDLER is None:
        # No bridge wired — fail closed. Better to refuse the action
        # than auto-approve it because the host forgot to install the
        # hook.
        logger.warning(
            "request_confirmation called but no handler registered "
            "(action=%s) — denying.", action,
        )
        audit(action, result="denied", note="no_confirm_handler")
        return False

    try:
        reply = _CONFIRM_HANDLER(prompt, timeout_s) or ""
    except Exception as exc:
        logger.error("Confirmation handler crashed: %s", exc)
        audit(action, result="denied", note=f"handler_error:{exc}")
        return False

    verdict = _classify_reply(reply)
    if verdict is True:
        audit(action, result="confirmed", note=f"reply={reply!r}")
        return True
    if verdict is False:
        if _SPEAK_HANDLER:
            try:
                _SPEAK_HANDLER("Cancelled, Sir.")
            except Exception:
                pass
        audit(action, result="cancelled", note=f"reply={reply!r}")
        return False
    # Ambiguous or empty
    if _SPEAK_HANDLER:
        try:
            _SPEAK_HANDLER("I didn't get a clear answer, Sir. Cancelling.")
        except Exception:
            pass
    audit(action, result="timeout", note=f"reply={reply!r}")
    return False


# ─── Decorator helper for tier-gated actions ─────────────────────────────────

def guarded(action: str, *, confirm_prompt: Optional[str] = None):
    """Decorator: classify the wrapped function as ``action``, audit it,
    and (for Tier ≥ DESTRUCTIVE) request voice confirmation first.

    Wrapped function MUST return a JSON-serialisable result, or a tuple
    ``(result, note)`` if it wants to add a free-text note to the audit
    row.
    """
    def deco(fn):
        def wrapper(*args, **kwargs):
            tier = classify_tier(action)
            if tier >= Tier.DESTRUCTIVE:
                prompt = confirm_prompt or f"Confirm {action.replace('_', ' ')}, Sir?"
                if not request_confirmation(prompt, action=action):
                    return None  # caller treats None as "denied"
            try:
                out = fn(*args, **kwargs)
                if isinstance(out, tuple) and len(out) == 2:
                    result, note = out
                else:
                    result, note = out, ""
                audit(action, result="ok",
                      note=str(note) if note else "",
                      args=_safe_repr(args), kwargs=_safe_repr(kwargs))
                return result
            except Exception as exc:
                audit(action, result="error", note=f"{type(exc).__name__}: {exc}")
                raise
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return deco


def _safe_repr(x, max_len: int = 200) -> str:
    """repr() with a length cap so a giant prompt doesn't bloat audit.log."""
    try:
        s = repr(x)
    except Exception:
        return "<unrepresentable>"
    return s if len(s) <= max_len else s[:max_len] + "…"


# ─── File-system & registry policy ───────────────────────────────────────────
# This is the "Sir's hard rules" enforcement layer. Sir's instruction:
#
#   • Never violate anything on the computer.
#   • Always ask before touching Windows settings.
#   • Never read personal files without explicit permission.
#
# We implement that as three sets of regex patterns. Any file/registry/
# process access goes through ``check_path_access`` which returns the
# Tier required to proceed. Tier.SAFE = go ahead. Tier.SYSTEM means
# voice confirmation is mandatory; Tier.MILD/DESTRUCTIVE sit in between.
# A small ``BANNED_PATHS`` set is absolute: Friday refuses no matter
# what Sir confirms, because those are the categories where one wrong
# move is catastrophic.

# Substring patterns of paths Friday must NEVER touch (read OR write),
# even with Sir's voice confirmation. The bar to add to this list is
# "could leak a credential, key, or token that compromises Sir's
# identity if it escaped the machine."
BANNED_PATH_PATTERNS: tuple[str, ...] = (
    r"\.ssh[\\/]",                       # SSH keys
    r"\.aws[\\/]credentials",            # AWS credentials
    r"\.gnupg[\\/]",                     # GPG private keys
    r"AppData[\\/]Roaming[\\/]Microsoft[\\/]Crypto[\\/]",  # Windows crypto keys
    r"AppData[\\/]Local[\\/]Microsoft[\\/]Credentials[\\/]",
    r"AppData[\\/]Roaming[\\/]Mozilla[\\/]Firefox[\\/]Profiles",  # browser key3.db
    r"AppData[\\/]Local[\\/](Google|Microsoft|BraveSoftware)[\\/](Chrome|Edge|Brave)[\\/]User Data[\\/]Default[\\/]Login Data",
    r"AppData[\\/]Local[\\/](Google|Microsoft|BraveSoftware)[\\/](Chrome|Edge|Brave)[\\/]User Data[\\/]Default[\\/]Cookies",
    r"Windows[\\/]System32[\\/]config[\\/]",   # SAM, SYSTEM, SECURITY hives
    r"Windows[\\/]ServiceProfiles[\\/]",
    r"BitLocker",                        # disk-encryption keys
    r"hiberfil\.sys",                    # hibernation file (memory dump)
    r"pagefile\.sys",
    r"swapfile\.sys",
)

# Substring patterns of "Sir's personal data" — Tier.DESTRUCTIVE
# confirmation required to READ, same to write. Includes documents,
# pictures, videos, downloads, desktop, and any user-named folder.
# These can be unblocked per-call with voice confirmation but the
# default policy says "do not snoop."
PERSONAL_PATH_PATTERNS: tuple[str, ...] = (
    r"[\\/]Documents[\\/]",
    r"[\\/]Pictures[\\/]",
    r"[\\/]Videos[\\/]",
    r"[\\/]Music[\\/]",
    r"[\\/]Desktop[\\/]",
    r"[\\/]Downloads[\\/]",
    r"[\\/]OneDrive[\\/]",
    r"[\\/]iCloud[\\/]",
    r"[\\/]Dropbox[\\/]",
)

# Substring patterns of system-critical locations — Tier.SYSTEM
# confirmation required. Writing to these can break Sir's machine; we
# require an explicit "yes, Sir" before any write.
SYSTEM_PATH_PATTERNS: tuple[str, ...] = (
    r"^[A-Z]:[\\/]Windows[\\/]",
    r"^[A-Z]:[\\/]Program Files",        # 'Program Files' and 'Program Files (x86)'
    r"^[A-Z]:[\\/]ProgramData[\\/]",
    r"^[A-Z]:[\\/]Boot[\\/]",
    r"^[A-Z]:[\\/]Recovery[\\/]",
    r"^[A-Z]:[\\/]System Volume Information",
    r"AppData[\\/]Roaming[\\/]Microsoft[\\/]Windows[\\/]Start Menu[\\/]Programs[\\/]Startup",
    r"AppData[\\/]Local[\\/]Microsoft[\\/]Windows[\\/]Start Menu[\\/]Programs[\\/]Startup",
)

# Friday's OWN home — always Tier.SAFE for read/write.
FRIDAY_HOME_PATTERNS: tuple[str, ...] = (
    r"[\\/]\.friday[\\/]",
    r"[\\/]\.jarvis[\\/]",
    r"[\\/]antigravity[\\/]friday[\\/]",  # project root
)


def _match_any(path: str, patterns: tuple[str, ...]) -> bool:
    """Case-insensitive substring/regex match against a tuple of patterns."""
    return any(re.search(p, path, re.IGNORECASE) for p in patterns)


class PathAccessError(PermissionError):
    """Raised when Friday tried to touch a banned path. Inherits from
    PermissionError so callers can catch with the standard idiom.
    """


def check_path_access(
    path: Union[str, os.PathLike],
    mode: str = "read",
) -> Tier:
    """Decide what tier of approval is required to touch ``path``.

    Returns one of:
      - ``Tier.SAFE``         — go ahead, audit only.
      - ``Tier.MILD``         — log it; no voice confirmation needed.
      - ``Tier.DESTRUCTIVE``  — voice confirmation required (personal).
      - ``Tier.SYSTEM``       — voice confirmation required (system).

    Raises ``PathAccessError`` if the path is in ``BANNED_PATH_PATTERNS``
    — Friday refuses absolutely, no matter what Sir says, because those
    paths leak credentials / encryption material.

    ``mode`` is one of "read", "write", "delete". Write/delete escalate
    Tier.MILD → DESTRUCTIVE for personal paths (writing to Documents is
    worse than reading from it).
    """
    p = str(path).replace("/", "\\")
    if _match_any(p, BANNED_PATH_PATTERNS):
        audit(
            "path_access_denied", result="banned",
            path=_safe_repr(p), mode=mode,
        )
        raise PathAccessError(
            f"Path is in Friday's hard refusal list: {p!r}"
        )
    if _match_any(p, FRIDAY_HOME_PATTERNS):
        return Tier.SAFE
    if _match_any(p, SYSTEM_PATH_PATTERNS):
        return Tier.SYSTEM
    if _match_any(p, PERSONAL_PATH_PATTERNS):
        # Reading Sir's personal files is destructive of his privacy
        # even if it isn't destructive of bits on disk.
        return Tier.DESTRUCTIVE
    return Tier.SAFE


def require_path_access(
    path: Union[str, os.PathLike],
    mode: str = "read",
    *,
    reason: str = "",
) -> bool:
    """Convenience: ``check_path_access`` + voice confirmation when
    required. Returns True if the access may proceed.

    Use this as a one-liner at the top of any function that opens a file
    on Sir's behalf::

        if not safety.require_path_access(path, "read", reason="Reading inbox"):
            return  # cancelled

    Audits the outcome regardless.
    """
    try:
        tier = check_path_access(path, mode=mode)
    except PathAccessError as exc:
        if _SPEAK_HANDLER:
            try:
                _SPEAK_HANDLER(
                    "I won't touch that path, Sir — it's on my hard "
                    "refusal list."
                )
            except Exception:
                pass
        logger.warning("require_path_access denied (banned): %s", exc)
        return False

    if tier <= Tier.MILD:
        audit("path_access", result="allowed", path=str(path), mode=mode,
              tier=int(tier), reason=reason)
        return True

    # Tier 2 or 3 → voice confirmation
    label = "Sir's personal files" if tier == Tier.DESTRUCTIVE else "Windows system area"
    prompt = (
        f"I need to {mode} something in {label}, Sir."
        + (f" {reason}" if reason else "")
        + f" Path: {os.path.basename(str(path)) or str(path)}."
        " Say yes to allow."
    )
    confirmed = request_confirmation(
        prompt, action="path_access", timeout_s=8.0,
    )
    audit(
        "path_access",
        result="confirmed" if confirmed else "denied",
        path=str(path), mode=mode, tier=int(tier), reason=reason,
    )
    return confirmed


# ─── Read access for tooling ─────────────────────────────────────────────────

def tail_audit(n: int = 20) -> list[dict]:
    """Return the last `n` audit events parsed back from disk. Used by
    the (forthcoming) ``"Friday, what did you do today"`` voice command
    and any debug UI.
    """
    if not _AUDIT_FILE.exists():
        return []
    try:
        with open(_AUDIT_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception as exc:
        logger.debug("tail_audit failed: %s", exc)
        return []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("Tier registry:")
    for k, v in _TIER_REGISTRY.items():
        print(f"  {k:30} tier={int(v)}")
    print()
    audit("open_app", result="ok", app="WhatsApp")
    audit("shutdown_friday", result="confirmed", note="test")
    audit("delete_file", result="cancelled", path="/tmp/oops.txt")
    for ev in tail_audit(5):
        print(ev)
