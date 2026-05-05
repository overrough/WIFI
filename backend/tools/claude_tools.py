"""
Claude integration tools — bridge Jarvis to Claude Desktop and Claude Code.

Three capabilities:
  1. open_claude()           — launch Claude Desktop app via URL scheme
  2. send_to_claude(task)    — execute a task via Claude Code CLI (`claude -p "..."`)
  3. claude_cowork(task, folder) — start Claude Code with auto-accept on a project

The `claude://` URL scheme is handled natively by the Claude Desktop app on Windows.
Claude Code CLI (`claude`) must be installed separately: npm install -g @anthropic-ai/claude-code

Safety:
  - open_claude: risk_level=low (just opens the app)
  - send_to_claude: risk_level=medium (executes a task in Claude Code)
  - claude_cowork: risk_level=medium (runs Claude Code with auto-accept)
"""

import asyncio
import logging
import os
import platform
import subprocess
import urllib.parse

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_CLAUDE_CLI_TIMEOUT = 120  # seconds — Claude Code tasks can take a while
_OUTPUT_CAP = 8000


def _cap(text: str) -> str:
    if len(text) <= _OUTPUT_CAP:
        return text
    half = _OUTPUT_CAP // 2
    return text[:half] + f"\n... [truncated {len(text) - _OUTPUT_CAP} chars] ...\n" + text[-half:]


def make_claude_tools():

    @tool
    async def open_claude(prompt: str = "") -> str:
        """
        Open the Claude Desktop application.
        If a prompt is provided, it opens Claude with that prompt pre-filled in a new chat.
        If no prompt, it simply opens the Claude Desktop app.

        Examples:
          open_claude()                                → opens Claude Desktop
          open_claude("Summarize this document")       → opens Claude with a prompt
        """
        try:
            if platform.system() == "Windows":
                if prompt:
                    encoded = urllib.parse.quote(prompt)
                    url = f"claude://claude.ai/new?q={encoded}"
                    os.startfile(url)
                    return f"Opened Claude Desktop with prompt: {prompt[:100]}..."
                else:
                    # Try URL scheme first, fall back to direct app launch
                    try:
                        os.startfile("claude://claude.ai/new")
                        return "Claude Desktop opened."
                    except OSError:
                        # Fallback: try to launch the app directly
                        for path in [
                            os.path.expandvars(r"%LOCALAPPDATA%\Programs\claude\Claude.exe"),
                            os.path.expandvars(r"%LOCALAPPDATA%\Claude\Claude.exe"),
                            os.path.expandvars(r"%PROGRAMFILES%\Claude\Claude.exe"),
                        ]:
                            if os.path.exists(path):
                                subprocess.Popen([path], shell=False)
                                return f"Claude Desktop launched from: {path}"
                        return (
                            "Could not find Claude Desktop. "
                            "Install it from https://claude.ai/download"
                        )
            elif platform.system() == "Darwin":
                if prompt:
                    encoded = urllib.parse.quote(prompt)
                    subprocess.run(["open", f"claude://claude.ai/new?q={encoded}"])
                else:
                    subprocess.run(["open", "-a", "Claude"])
                return "Claude Desktop opened."
            else:
                return "Claude Desktop launch is only supported on Windows and macOS."

        except Exception as exc:
            return f"Failed to open Claude: {exc}"

    @tool
    async def send_to_claude(
        task: str,
        working_directory: str = "",
    ) -> str:
        """
        Send a task to Claude Code (CLI) for execution.
        Claude Code will process the task and return the result.
        Requires Claude Code CLI installed: npm install -g @anthropic-ai/claude-code

        task: the instruction/prompt to send to Claude Code
        working_directory: optional folder path where Claude should work
                          (defaults to user's home directory)

        Examples:
          send_to_claude("Explain what this project does")
          send_to_claude("Fix the bug in utils.py", working_directory="D:/projects/myapp")
          send_to_claude("Write unit tests for the API routes")
        """
        try:
            cwd = working_directory if working_directory else str(os.path.expanduser("~"))
            if not os.path.isdir(cwd):
                return f"Directory not found: {cwd}"

            # Use 'claude' CLI with print mode (-p) for non-interactive execution
            cmd = ["claude", "-p", task]

            logger.info("Sending task to Claude Code: %s (cwd=%s)", task[:80], cwd)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )

            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=_CLAUDE_CLI_TIMEOUT
                )
            except asyncio.TimeoutError:
                proc.kill()
                return f"Claude Code timed out after {_CLAUDE_CLI_TIMEOUT}s for task: {task[:80]}..."

            output = stdout.decode("utf-8", errors="replace")
            exit_code = proc.returncode

            if exit_code != 0:
                return f"Claude Code finished with exit code {exit_code}:\n{_cap(output)}"

            return f"Claude Code result:\n{_cap(output)}"

        except FileNotFoundError:
            return (
                "Claude Code CLI not found. Install it with:\n"
                "  npm install -g @anthropic-ai/claude-code\n"
                "Then run 'claude' once to authenticate."
            )
        except Exception as exc:
            return f"Error running Claude Code: {exc}"

    @tool
    async def claude_cowork(
        task: str,
        folder: str = "",
        auto_accept: bool = True,
    ) -> str:
        """
        Start a Claude Code coworking session — Claude works on a task in a project folder
        with tool permissions auto-accepted (so it can read/write files, run commands, etc.)

        This is ideal for coding tasks: "fix this bug", "add a new feature",
        "refactor this module", "write tests".

        task: what Claude should work on
        folder: project folder (required for coding tasks)
        auto_accept: if True, auto-accepts all tool permission requests (default: True)

        Examples:
          claude_cowork("Fix the login page styling", folder="D:/projects/myapp")
          claude_cowork("Add error handling to all API routes", folder="D:/antigravity/friday/backend")
          claude_cowork("Review the code and suggest improvements", folder=".")
        """
        try:
            cwd = folder if folder else str(os.path.expanduser("~"))
            if not os.path.isdir(cwd):
                return f"Folder not found: {cwd}"

            # Build the command with --dangerously-skip-permissions for auto-accept
            cmd = ["claude", "-p", task]
            if auto_accept:
                cmd.append("--dangerously-skip-permissions")

            logger.info(
                "Starting Claude cowork: task=%s, folder=%s, auto_accept=%s",
                task[:80], cwd, auto_accept,
            )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )

            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=_CLAUDE_CLI_TIMEOUT * 2  # longer timeout for cowork
                )
            except asyncio.TimeoutError:
                proc.kill()
                return f"Claude cowork session timed out after {_CLAUDE_CLI_TIMEOUT * 2}s."

            output = stdout.decode("utf-8", errors="replace")
            exit_code = proc.returncode

            status = "completed" if exit_code == 0 else f"finished with exit code {exit_code}"
            return (
                f"Claude cowork session {status}.\n"
                f"Task: {task}\n"
                f"Folder: {cwd}\n"
                f"Output:\n{_cap(output)}"
            )

        except FileNotFoundError:
            return (
                "Claude Code CLI not found. Install with:\n"
                "  npm install -g @anthropic-ai/claude-code"
            )
        except Exception as exc:
            return f"Claude cowork error: {exc}"

    return [open_claude, send_to_claude, claude_cowork]
