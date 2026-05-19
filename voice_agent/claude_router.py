"""
Claude router — find, focus, switch tab, and paste into an EXISTING Claude
session.

The whole point of this module is that Friday should NEVER open a fresh
Claude chat when an existing one is the right place for Sir's message.
Claude maintains conversation memory within a chat; spawning a new one
throws all of that away. Friday's job is to be the messenger to the
right window, not a stenographer who keeps walking into the wrong room.

**Architecture (corrected May 18 2026):**

Claude Desktop is a SINGLE Electron app that exposes three tabs at the
top-left of its window:

  - **Chat** — ordinary conversational chat. Default tab.
  - **Cowork** — Claude operates a sandboxed browser on Sir's behalf
    (the "Computer Use"-style agentic mode now baked into Desktop).
  - **Code** — in-app coding workspace with file tree, terminal, diff
    view. Distinct from the standalone `claude` CLI binary.

The earlier v1 of this module mistakenly treated those three as separate
Windows-level applications. They aren't. They're all the same
``claude.exe`` process; the tab is selected by clicking a Button inside
the window via Windows UI Automation. ``pywinauto`` does the heavy
lifting; we fall back to focus-only if UIA fails.

**Routing rules** (used by ``send_to_existing_claude``):

  1. Find the most-recently-focused Claude Desktop window.
  2. If a tab hint is given ("chat" / "cowork" / "code"), switch to that
     tab via UIA. Otherwise stay on whichever tab is currently active.
  3. Focus the window, paste into the clipboard, send Ctrl+V.
     **Do not press Enter** — Sir confirms.
  4. If no Claude window is running at all, return failure so the
     caller can decide whether to spawn one (and warn Sir, since a
     fresh chat loses context).

The standalone ``claude`` CLI (terminal-hosted) is treated separately:
that path lives in ``command_router._send_to_claude`` and is invoked
only when no Desktop window exists for the Code-style request.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Variant classification ──────────────────────────────────────────────────

class ClaudeVariant(str, Enum):
    """Which Claude surface this window represents.

    Note: Chat / Cowork / Code are **tabs inside the Desktop app**, not
    separate variants — the variant is always DESKTOP for the Electron
    app. The ``ClaudeTab`` enum below is what selects the tab.
    """
    DESKTOP = "desktop"          # Claude.exe — the Electron app
    COMPUTER_USE = "computer_use"  # Anthropic Docker beta on localhost
    CODE = "code"                # Terminal hosting `claude` CLI
    UNKNOWN = "unknown"          # Claude-titled but we can't tell


class ClaudeTab(str, Enum):
    """Which tab inside the Claude Desktop window to activate."""
    CHAT = "Chat"
    COWORK = "Cowork"
    CODE = "Code"


# Mapping from spoken hints to the tab name we should click.
# Order matters: more specific hints first.
_TAB_HINTS: tuple[tuple[tuple[str, ...], ClaudeTab], ...] = (
    (
        ("code", "coding", "refactor", "fix the bug", "write code",
         "implement", "debug", "build a", "add a function", "rewrite"),
        ClaudeTab.CODE,
    ),
    (
        ("cowork", "co-work", "co work", "computer use", "computer-use",
         "navigate to", "open the browser", "browse to", "in the browser",
         "post on linkedin", "post to linkedin", "send the email",
         "twitter", "click on", "fill in", "submit the form"),
        ClaudeTab.COWORK,
    ),
    (
        ("chat", "ask", "explain", "summarise", "summarize", "what is",
         "how do i", "draft a message", "draft an email", "brainstorm"),
        ClaudeTab.CHAT,
    ),
)


def _infer_tab_from_text(text: str) -> Optional[ClaudeTab]:
    """Best-effort: guess the right tab from the spoken message itself.

    Used when the caller doesn't pass an explicit hint. We err on the
    side of staying on the current tab (return None) when no keyword
    matches — the wrong tab is more annoying than no switch at all.
    """
    if not text:
        return None
    low = text.lower()
    for keywords, tab in _TAB_HINTS:
        for kw in keywords:
            if kw in low:
                return tab
    return None


@dataclass
class ClaudeWindow:
    hwnd: int
    title: str
    process_name: str
    variant: ClaudeVariant
    z_index: int   # 0 = topmost (most recently focused)

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return (
            f"ClaudeWindow(hwnd=0x{self.hwnd:08x}, variant={self.variant.value}, "
            f"z={self.z_index}, title={self.title!r}, proc={self.process_name!r})"
        )


# ─── Win32 plumbing ──────────────────────────────────────────────────────────
# We use ctypes directly instead of pywin32 so this module has zero
# extra runtime deps. All functions return None / [] on non-Windows
# so callers can no-op gracefully.

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    _user32.EnumWindows.argtypes = [EnumWindowsProc, wt.LPARAM]
    _user32.EnumWindows.restype = wt.BOOL
    _user32.IsWindowVisible.argtypes = [wt.HWND]
    _user32.IsWindowVisible.restype = wt.BOOL
    _user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wt.DWORD
    _user32.SetForegroundWindow.argtypes = [wt.HWND]
    _user32.SetForegroundWindow.restype = wt.BOOL
    _user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wt.BOOL
    _user32.BringWindowToTop.argtypes = [wt.HWND]
    _user32.BringWindowToTop.restype = wt.BOOL
    _user32.IsIconic.argtypes = [wt.HWND]
    _user32.IsIconic.restype = wt.BOOL
    _user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    _user32.AttachThreadInput.restype = wt.BOOL
    _user32.GetForegroundWindow.restype = wt.HWND

    _kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _kernel32.OpenProcess.restype = wt.HANDLE
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]
    _kernel32.CloseHandle.restype = wt.BOOL
    _kernel32.GetCurrentThreadId.restype = wt.DWORD

    _psapi.GetModuleBaseNameW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
    _psapi.GetModuleBaseNameW.restype = wt.DWORD

    _SW_RESTORE = 9
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _get_window_title(hwnd: int) -> str:
    if not _IS_WIN:
        return ""
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _get_process_name(hwnd: int) -> str:
    if not _IS_WIN:
        return ""
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        n = _psapi.GetModuleBaseNameW(h, None, buf, 260)
        return buf.value if n else ""
    finally:
        _kernel32.CloseHandle(h)


# ─── Variant detection ───────────────────────────────────────────────────────

# Window-title hints (case-insensitive substring match). Order matters
# — more specific patterns checked first.
_COMPUTER_USE_HINTS = (
    "computer use", "claude computer", "anthropic computer",
    "localhost:8080", "localhost:8501", "localhost:8443",
)
_CODE_HINTS = (
    "claude code", "claude cli", "claude-code",
)
_TERMINAL_PROCS = (
    "windowsterminal.exe", "conhost.exe", "cmd.exe",
    "powershell.exe", "pwsh.exe", "wezterm-gui.exe", "alacritty.exe",
)
_BROWSER_PROCS = (
    "msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "arc.exe",
    "opera.exe", "vivaldi.exe",
)


def _classify(title: str, process_name: str) -> ClaudeVariant:
    """Decide which Claude variant a window represents.

    The 'is this Claude at all?' question lives upstream in
    ``_is_claude_window``; this function assumes the answer is yes and
    just picks the variant.
    """
    t_low = title.lower().strip()
    p_low = process_name.lower()

    # Most specific first
    for hint in _CODE_HINTS:
        if hint in t_low:
            return ClaudeVariant.CODE
    for hint in _COMPUTER_USE_HINTS:
        if hint in t_low:
            return ClaudeVariant.COMPUTER_USE

    # Title is exactly "Claude" → the Desktop Electron app (covers MSIX
    # case where process_name is empty).
    if t_low == "claude" or t_low.startswith("claude — ") or t_low.startswith("claude - "):
        return ClaudeVariant.DESKTOP

    # Process-class hints
    if p_low in _TERMINAL_PROCS and "claude" in t_low:
        return ClaudeVariant.CODE
    if p_low in _BROWSER_PROCS and "claude" in t_low:
        # Browser tab with Claude in the title — could be claude.ai/chat
        # or a Computer Use embed. Default to DESKTOP-like (treat as
        # standard chat); the Computer Use hints above catch the
        # specific case.
        return ClaudeVariant.DESKTOP
    if p_low.startswith("claude") and p_low.endswith(".exe"):
        return ClaudeVariant.DESKTOP

    return ClaudeVariant.UNKNOWN


def _is_claude_window(title: str, process_name: str) -> bool:
    """Heuristic: does this window belong to a Claude surface?

    Notes on process_name being empty: Claude Desktop ships as an MSIX
    package on Windows. ``psapi.GetModuleBaseNameW`` returns an empty
    string for MSIX/AppContainer processes when the calling process
    isn't elevated. We can't rely on the process name for the Desktop
    app, so we treat any window titled exactly "Claude" (or starting
    with "Claude — " / "Claude - ") as the Desktop app.
    """
    t = title.lower().strip()
    p = process_name.lower()
    if not t:
        return False
    # The Desktop app: title is exactly "Claude" or "Claude — <chat title>"
    if t == "claude" or t.startswith("claude — ") or t.startswith("claude - "):
        return True
    # Strong signal: process is the Claude desktop app
    if p.startswith("claude") and p.endswith(".exe"):
        return True
    # Strong signal: title says Claude AND it's a browser or terminal
    if "claude" in t and (p in _TERMINAL_PROCS or p in _BROWSER_PROCS):
        return True
    # Computer Use browser tab specifically (Anthropic's Docker beta)
    for hint in _COMPUTER_USE_HINTS:
        if hint in t and p in _BROWSER_PROCS:
            return True
    return False


# ─── Enumeration ─────────────────────────────────────────────────────────────

def list_claude_windows() -> list[ClaudeWindow]:
    """Return every Claude window currently on the desktop, ordered by
    Z-index (index 0 = topmost / most-recently-focused).

    On non-Windows, returns []. On enumeration failure, returns [].
    """
    if not _IS_WIN:
        return []

    found: list[ClaudeWindow] = []
    z_counter = {"i": 0}  # mutable container for closure

    @EnumWindowsProc
    def _cb(hwnd, _lparam):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            title = _get_window_title(hwnd)
            if not title:
                return True
            proc = _get_process_name(hwnd)
            if not _is_claude_window(title, proc):
                return True
            variant = _classify(title, proc)
            found.append(ClaudeWindow(
                hwnd=int(hwnd),
                title=title,
                process_name=proc,
                variant=variant,
                z_index=z_counter["i"],
            ))
            z_counter["i"] += 1
        except Exception as exc:
            logger.debug("EnumWindows callback error: %s", exc)
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception as exc:
        logger.debug("EnumWindows failed: %s", exc)
        return []
    return found


def find_best_window(
    hint: Optional[str] = None,
) -> Optional[ClaudeWindow]:
    """Return the best Claude window to route to.

    ``hint`` (optional): "code", "computer use" / "computer-use" /
    "cu", "chat" / "desktop". When given, prefer that variant; if
    none of that variant is open, fall back to most-recent overall.
    """
    windows = list_claude_windows()
    if not windows:
        return None

    if hint:
        h = hint.lower().strip()
        target: Optional[ClaudeVariant] = None
        if h in ("code", "claude code", "cli"):
            target = ClaudeVariant.CODE
        elif h in ("computer use", "computer-use", "computer_use",
                   "co-work", "cowork", "co work", "cu"):
            target = ClaudeVariant.COMPUTER_USE
        elif h in ("chat", "desktop", "claude chat", "claude desktop"):
            target = ClaudeVariant.DESKTOP

        if target is not None:
            for w in windows:
                if w.variant == target:
                    return w
            # Fall through to most-recent overall

    # Default: most-recent (lowest z_index)
    return windows[0]


# ─── Focus + paste ───────────────────────────────────────────────────────────

def focus_window(hwnd: int) -> bool:
    """Bring ``hwnd`` to the foreground reliably.

    Windows is intentionally hostile to ``SetForegroundWindow`` calls
    that don't originate from a user-input context (it prevents apps
    stealing focus). The standard work-around — and the one Microsoft
    documents — is to attach to the current foreground thread, then
    call SetForegroundWindow, then detach. Most of the time this
    succeeds even from a background voice-agent thread.
    """
    if not _IS_WIN:
        return False
    try:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, _SW_RESTORE)

        fg_hwnd = _user32.GetForegroundWindow()
        fg_thread = _user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
        our_thread = _kernel32.GetCurrentThreadId()

        attached = False
        if fg_thread and fg_thread != our_thread:
            attached = bool(_user32.AttachThreadInput(our_thread, fg_thread, True))
        try:
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(our_thread, fg_thread, False)
        # Tiny settle delay — Windows needs a frame to repaint before
        # we start sending keystrokes, or the Ctrl+V can hit the wrong
        # window.
        time.sleep(0.08)
        return _user32.GetForegroundWindow() == hwnd
    except Exception as exc:
        logger.debug("focus_window failed: %s", exc)
        return False


def _set_clipboard_text(text: str) -> bool:
    """Place ``text`` on the Windows clipboard as CF_UNICODETEXT."""
    if not _IS_WIN:
        return False
    try:
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        _user32.OpenClipboard.argtypes = [wt.HWND]
        _user32.OpenClipboard.restype = wt.BOOL
        _user32.EmptyClipboard.restype = wt.BOOL
        _user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
        _user32.SetClipboardData.restype = wt.HANDLE
        _user32.CloseClipboard.restype = wt.BOOL
        _kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
        _kernel32.GlobalAlloc.restype = wt.HGLOBAL
        _kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
        _kernel32.GlobalLock.restype = ctypes.c_void_p
        _kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
        _kernel32.GlobalUnlock.restype = wt.BOOL

        if not _user32.OpenClipboard(None):
            return False
        try:
            _user32.EmptyClipboard()
            buf = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(buf)
            h_mem = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not h_mem:
                return False
            ptr = _kernel32.GlobalLock(h_mem)
            ctypes.memmove(ptr, buf, size)
            _kernel32.GlobalUnlock(h_mem)
            if not _user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                return False
            return True
        finally:
            _user32.CloseClipboard()
    except Exception as exc:
        logger.debug("_set_clipboard_text failed: %s", exc)
        return False


def _send_ctrl_v() -> bool:
    """Press Ctrl+V using SendInput so it lands in the focused window.

    SendInput is preferred over keybd_event because it's the documented,
    composable, future-proof primitive. We synthesize four input events:
    Ctrl-down, V-down, V-up, Ctrl-up.
    """
    if not _IS_WIN:
        return False
    try:
        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        VK_CONTROL = 0x11
        VK_V = 0x56

        ULONG_PTR = ctypes.c_size_t

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wt.WORD),
                ("wScan", wt.WORD),
                ("dwFlags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class _UNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [
                ("type", wt.DWORD),
                ("u", _UNION),
            ]

        def _key(vk, up=False):
            i = INPUT()
            i.type = INPUT_KEYBOARD
            i.ki.wVk = vk
            i.ki.wScan = 0
            i.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
            i.ki.time = 0
            i.ki.dwExtraInfo = 0
            return i

        events = (INPUT * 4)(
            _key(VK_CONTROL),
            _key(VK_V),
            _key(VK_V, up=True),
            _key(VK_CONTROL, up=True),
        )
        _user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        _user32.SendInput.restype = wt.UINT
        sent = _user32.SendInput(4, events, ctypes.sizeof(INPUT))
        return sent == 4
    except Exception as exc:
        logger.debug("_send_ctrl_v failed: %s", exc)
        return False


# ─── Public entry point ──────────────────────────────────────────────────────

@dataclass
class RouteResult:
    success: bool
    window: Optional[ClaudeWindow]
    note: str = ""


def switch_claude_tab(hwnd: int, tab: ClaudeTab) -> bool:
    """Click the Chat / Cowork / Code tab inside the Claude Desktop window.

    We use ``pywinauto`` with the UIA backend because Claude Desktop is
    an Electron app and Electron exposes the full UIA tree. The tab
    buttons are plain ``Button`` controls (named exactly "Chat",
    "Cowork", "Code") nested inside a ``Group`` named "Mode".

    Returns True if the click landed, False otherwise. On non-Windows,
    when pywinauto is missing, or when the named button isn't found, we
    return False so the caller can decide whether to proceed anyway
    (often the right call — most messages work on any tab).
    """
    if not _IS_WIN:
        return False
    try:
        from pywinauto import Application  # type: ignore
    except Exception as exc:
        logger.debug("pywinauto unavailable: %s", exc)
        return False
    try:
        app = Application(backend="uia").connect(handle=hwnd, timeout=2)
        win = app.window(handle=hwnd)
        btn = win.child_window(title=tab.value, control_type="Button")
        if not btn.exists(timeout=1):
            logger.debug("Tab button %r not found in window 0x%x", tab.value, hwnd)
            return False
        btn.click_input()
        # Small settle delay; Claude's tab-change animation runs ~150 ms.
        time.sleep(0.20)
        logger.info("Switched Claude tab to %s", tab.value)
        return True
    except Exception as exc:
        logger.debug("switch_claude_tab(%s) failed: %s", tab.value, exc)
        return False


def _resolve_tab(
    hint: Optional[str],
    message: Optional[str],
) -> Optional[ClaudeTab]:
    """Coerce a hint string or message into a ClaudeTab.

    Priority: explicit hint > content-based inference > None (stay on
    current tab).
    """
    if hint:
        h = hint.lower().strip()
        if h in ("chat", "desktop", "claude chat", "ask", "claude desktop"):
            return ClaudeTab.CHAT
        if h in ("cowork", "co-work", "co work", "computer use",
                 "computer-use", "cu", "browser"):
            return ClaudeTab.COWORK
        if h in ("code", "claude code", "cli", "coding"):
            return ClaudeTab.CODE
    if message:
        return _infer_tab_from_text(message)
    return None


def send_to_existing_claude(
    message: str,
    *,
    hint: Optional[str] = None,
    tab: Optional[ClaudeTab] = None,
    switch_tab: bool = True,
) -> RouteResult:
    """Find Claude Desktop, switch to the right tab, paste ``message``.

    Pipeline:
      1. Find the most-recently-focused Claude window.
      2. Pick a tab from ``tab`` (explicit) / ``hint`` (string) /
         message-content inference, in that order.
      3. Click the tab button if needed (and ``switch_tab=True``).
      4. Re-focus the window (UIA click can shift focus).
      5. Copy ``message`` to clipboard, send Ctrl+V.

    Sir confirms with Enter — we deliberately don't send it.

    Returns ``RouteResult(success=False, window=None)`` when no Claude
    window is open. The caller decides whether to open a fresh chat
    (and should warn Sir that context will be lost).
    """
    if not _IS_WIN:
        return RouteResult(False, None, note="not_windows")
    if not message or not message.strip():
        return RouteResult(False, None, note="empty_message")

    win = find_best_window(hint=hint)
    if win is None:
        return RouteResult(False, None, note="no_claude_window")

    target_tab = tab or _resolve_tab(hint, message)
    logger.info(
        "Routing to Claude: variant=%s tab=%s title=%r",
        win.variant.value,
        target_tab.value if target_tab else "<keep current>",
        win.title[:80],
    )

    if not focus_window(win.hwnd):
        logger.warning("Could not focus %s — paste may go to wrong window",
                       win.title[:60])
        return RouteResult(False, win, note="focus_failed")

    # Switch tab BEFORE focusing the input. Tab clicks via UIA can move
    # focus to the tab button; we re-focus the window afterwards so
    # Ctrl+V lands in the chat input.
    tab_note = ""
    if switch_tab and target_tab is not None and win.variant == ClaudeVariant.DESKTOP:
        if switch_claude_tab(win.hwnd, target_tab):
            tab_note = f" tab={target_tab.value}"
            focus_window(win.hwnd)
            time.sleep(0.10)
        else:
            tab_note = f" tab=keep(could_not_switch_to_{target_tab.value})"

    if not _set_clipboard_text(message):
        return RouteResult(False, win, note="clipboard_set_failed")

    if not _send_ctrl_v():
        return RouteResult(False, win, note="paste_failed")

    return RouteResult(True, win, note=f"pasted{tab_note}; awaiting Sir's Enter")


# ─── Standalone debug ────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("Looking for Claude windows...")
    wins = list_claude_windows()
    if not wins:
        print("  (none found — open Claude Desktop / Computer Use / Claude Code first)")
    for w in wins:
        print(f"  z={w.z_index} variant={w.variant.value:<14} pid_proc={w.process_name:<25} title={w.title[:80]!r}")
    print()
    best = find_best_window()
    if best:
        print(f"Best target: {best!r}")
