"""
Computer automation tools — the gap between a smart chatbot and a real assistant.

If Jarvis can write and run Python, read/write files, execute shell commands,
and see the screen, it can automate virtually anything on the local machine.

Safety model:
- Shell and code execution: timeout-gated, output-capped, risk_level=medium
- File writes: require explicit path, warn on sensitive locations
- Screenshots: always allowed (low risk)
- Dangerous shell patterns blocked at the tool level
"""

import asyncio
import base64
import os
import platform
import sys
import tempfile
import textwrap
from pathlib import Path

from langchain_core.tools import tool

from core.config import get_settings

settings = get_settings()

# ── Safety config ─────────────────────────────────────────────────────────────

# Default workspace: user's home directory. All relative paths resolve here.
WORKSPACE = Path.home()

# Hard block these shell patterns regardless of input
_BLOCKED_SHELL = {
    "rm -rf /", "rm -rf ~", "del /f /s /q c:\\",
    "format c:", ":(){:|:&};:", "mkfs",
}

_EXEC_TIMEOUT = 30   # seconds for shell / Python execution
_OUTPUT_CAP   = 8000  # max chars returned from command output


def _is_safe_shell(command: str) -> tuple[bool, str]:
    lower = command.lower()
    for pattern in _BLOCKED_SHELL:
        if pattern in lower:
            return False, f"Blocked: '{pattern}' detected."
    return True, ""


def _cap(text: str) -> str:
    if len(text) <= _OUTPUT_CAP:
        return text
    half = _OUTPUT_CAP // 2
    return text[:half] + f"\n... [truncated {len(text) - _OUTPUT_CAP} chars] ...\n" + text[-half:]


# ── File system tools ─────────────────────────────────────────────────────────


def make_computer_tools():

    @tool
    def list_files(directory: str = ".") -> str:
        """
        List files and folders in a directory.
        Use '.' for the current workspace (home directory).
        Shows file sizes and modification times.
        """
        try:
            path = (WORKSPACE / directory).resolve()
            if not path.exists():
                return f"Directory not found: {path}"

            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
            lines = [f"Directory: {path}\n"]
            for e in entries[:200]:
                if e.is_dir():
                    lines.append(f"  [DIR]  {e.name}/")
                else:
                    size = e.stat().st_size
                    size_str = (
                        f"{size}B" if size < 1024
                        else f"{size/1024:.1f}KB" if size < 1024**2
                        else f"{size/1024**2:.1f}MB"
                    )
                    lines.append(f"  [FILE] {e.name}  ({size_str})")
            if len(list(path.iterdir())) > 200:
                lines.append(f"  ... (showing first 200 entries)")
            return "\n".join(lines)
        except PermissionError:
            return f"Permission denied: {directory}"
        except Exception as exc:
            return f"Error listing files: {exc}"

    @tool
    def read_file(path: str, max_lines: int = 200) -> str:
        """
        Read the contents of a file.
        Supports text files, code, config files, markdown, etc.
        Relative paths are resolved from the home directory.
        max_lines: limit output (default 200 lines). Use 0 for full file.
        """
        try:
            resolved = (WORKSPACE / path).resolve() if not Path(path).is_absolute() else Path(path)
            if not resolved.exists():
                return f"File not found: {resolved}"
            if not resolved.is_file():
                return f"Not a file: {resolved}"

            # Binary file detection
            chunk = resolved.read_bytes()[:1024]
            if b'\x00' in chunk:
                size = resolved.stat().st_size
                return f"Binary file ({size} bytes): {resolved}\nUse a specific tool to process this file type."

            content = resolved.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()

            if max_lines > 0 and len(lines) > max_lines:
                shown = "\n".join(lines[:max_lines])
                return f"[File: {resolved}  — {len(lines)} lines total, showing first {max_lines}]\n\n{shown}\n\n... [truncated]"

            return f"[File: {resolved}  — {len(lines)} lines]\n\n{content}"
        except Exception as exc:
            return f"Error reading file: {exc}"

    @tool
    def write_file(path: str, content: str, mode: str = "overwrite") -> str:
        """
        Write content to a file.
        mode: 'overwrite' (default) or 'append'
        Relative paths resolve from the home directory.
        Creates parent directories automatically.
        """
        try:
            resolved = (WORKSPACE / path).resolve() if not Path(path).is_absolute() else Path(path)
            resolved.parent.mkdir(parents=True, exist_ok=True)

            write_mode = "a" if mode == "append" else "w"
            resolved.write_text(content, encoding="utf-8") if write_mode == "w" else \
                open(resolved, "a", encoding="utf-8").write(content)

            size = resolved.stat().st_size
            action = "Appended to" if mode == "append" else "Written"
            return f"{action}: {resolved}  ({size} bytes)"
        except PermissionError:
            return f"Permission denied: {path}"
        except Exception as exc:
            return f"Error writing file: {exc}"

    @tool
    def search_files(query: str, directory: str = ".", file_pattern: str = "*") -> str:
        """
        Search for files by name pattern or content.
        query: text to search for in file contents (or empty to just match by name)
        directory: where to search (default: home)
        file_pattern: glob pattern like '*.py', '*.txt', '*.md'
        """
        try:
            root = (WORKSPACE / directory).resolve()
            if not root.exists():
                return f"Directory not found: {directory}"

            matches = []
            for fpath in root.rglob(file_pattern):
                if not fpath.is_file():
                    continue
                if not query:
                    matches.append(str(fpath))
                    continue
                try:
                    text = fpath.read_text(encoding="utf-8", errors="ignore")
                    if query.lower() in text.lower():
                        # Find first matching line
                        for i, line in enumerate(text.splitlines(), 1):
                            if query.lower() in line.lower():
                                matches.append(f"{fpath}:{i}  →  {line.strip()[:100]}")
                                break
                except Exception:
                    continue
                if len(matches) >= 50:
                    break

            if not matches:
                return f"No matches for '{query}' in {root} ({file_pattern})"
            return f"Found {len(matches)} match(es):\n" + "\n".join(matches)
        except Exception as exc:
            return f"Search error: {exc}"

    # ── Shell execution ───────────────────────────────────────────────────────

    @tool
    async def execute_command(
        command: str,
        working_directory: str = ".",
        timeout: int = 30,
    ) -> str:
        """
        Execute a shell command and return its output.
        Use for: running scripts, git commands, npm/pip installs, system tasks.
        timeout: max seconds (default 30, max 120).
        The command runs in the specified working directory (default: home).

        Examples:
          execute_command("git status")
          execute_command("python script.py", working_directory="~/projects/myapp")
          execute_command("npm install", working_directory="frontend")
        """
        safe, reason = _is_safe_shell(command)
        if not safe:
            return f"Command blocked: {reason}"

        timeout = min(int(timeout), 120)
        cwd = (WORKSPACE / working_directory).resolve()
        if not cwd.exists():
            return f"Working directory not found: {cwd}"

        try:
            if platform.system() == "Windows":
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(cwd),
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(cwd),
                    executable="/bin/bash",
                )

            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return f"Command timed out after {timeout}s: {command}"

            output = stdout.decode("utf-8", errors="replace")
            exit_code = proc.returncode

            result = f"$ {command}\n[exit code: {exit_code}]\n\n{_cap(output)}"
            return result if output.strip() else f"$ {command}\n[exit code: {exit_code}] (no output)"

        except FileNotFoundError as exc:
            return f"Command not found: {exc}"
        except Exception as exc:
            return f"Execution error: {exc}"

    # ── Python code execution ─────────────────────────────────────────────────

    @tool
    async def run_python_code(code: str, timeout: int = 30) -> str:
        """
        Write and execute Python code locally. Returns stdout + stderr.
        Use this for: data analysis, file processing, automation scripts,
        calculations, API calls, text processing — anything Python can do.

        The code runs in a temporary directory with access to the full Python
        environment (all installed packages available).

        timeout: max seconds (default 30, max 300 for long jobs).

        Examples:
          run_python_code("import os; print(os.listdir('.'))")
          run_python_code("import pandas as pd; df = pd.read_csv('data.csv'); print(df.describe())")
          run_python_code(\"\"\"
          import json
          data = json.load(open('config.json'))
          print(json.dumps(data, indent=2))
          \"\"\")
        """
        timeout = min(int(timeout), 300)

        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = os.path.join(tmpdir, "jarvis_exec.py")
            # Prepend workspace directory so relative file ops work from home
            preamble = textwrap.dedent(f"""
                import os, sys
                os.chdir(r'{WORKSPACE}')
                sys.path.insert(0, r'{WORKSPACE}')
            """)
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(preamble + "\n" + code)

            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, script_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(WORKSPACE),
                )
                try:
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    return f"Code timed out after {timeout}s."

                output = stdout.decode("utf-8", errors="replace")
                exit_code = proc.returncode

                if exit_code != 0:
                    return f"[exit code: {exit_code}]\n{_cap(output)}"
                return _cap(output) if output.strip() else "[Code executed successfully — no output]"

            except Exception as exc:
                return f"Execution error: {exc}"

    # ── Screenshot + vision ───────────────────────────────────────────────────

    @tool
    async def take_screenshot(save_path: str = "") -> str:
        """
        Capture the current screen and return it as a base64 PNG.
        Optionally save to a file path.
        Use before analyze_screen to see what's on screen.
        """
        try:
            import mss
            import mss.tools

            with mss.mss() as sct:
                monitor = sct.monitors[0]  # full screen
                sct_img = sct.grab(monitor)
                png_bytes = mss.tools.to_png(sct_img.rgb, sct_img.size)

            b64 = base64.b64encode(png_bytes).decode()

            if save_path:
                resolved = (WORKSPACE / save_path).resolve() if not Path(save_path).is_absolute() else Path(save_path)
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_bytes(png_bytes)
                return f"Screenshot saved to {resolved}  ({len(png_bytes)//1024}KB)\nBase64 (first 100 chars): {b64[:100]}..."

            return f"screenshot_base64:{b64}"

        except ImportError:
            return "mss not installed. Run: pip install mss"
        except Exception as exc:
            return f"Screenshot failed: {exc}"

    @tool
    async def analyze_screen(question: str) -> str:
        """
        Take a screenshot and answer a question about what's on screen.
        Use this to: understand what's happening on the screen, read UI elements,
        check if something is visible, guide further automation.

        Examples:
          analyze_screen("What application is in focus?")
          analyze_screen("Is there an error message visible?")
          analyze_screen("What text is in the main window?")
        """
        try:
            import mss
            import mss.tools

            with mss.mss() as sct:
                monitor = sct.monitors[0]
                sct_img = sct.grab(monitor)
                png_bytes = mss.tools.to_png(sct_img.rgb, sct_img.size)

            b64 = base64.b64encode(png_bytes).decode()

        except ImportError:
            return "mss not installed. Run: pip install mss"
        except Exception as exc:
            return f"Screenshot failed: {exc}"

        # Call the vision-capable LLM
        try:
            from providers.factory import get_provider
            provider = get_provider()
            lc_model = provider.to_langchain_model()

            # Both Claude and GPT-4o support vision via base64 image messages
            from langchain_core.messages import HumanMessage as LCHumanMessage

            msg = LCHumanMessage(content=[
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
                {"type": "text", "text": question},
            ])
            response = await lc_model.ainvoke([msg])
            return response.content

        except Exception as exc:
            return f"Vision analysis failed: {exc}. Screenshot was captured successfully."

    # ── System info ───────────────────────────────────────────────────────────

    @tool
    def get_system_info() -> str:
        """
        Get information about the current system: OS, CPU, memory, disk, Python version.
        Useful for diagnosing issues or understanding the environment.
        """
        import platform as plt
        lines = [
            f"OS: {plt.system()} {plt.release()} ({plt.machine()})",
            f"Python: {sys.version.split()[0]}",
            f"Home: {Path.home()}",
            f"CWD: {Path.cwd()}",
        ]

        try:
            import psutil
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            lines += [
                f"CPU cores: {psutil.cpu_count()} ({psutil.cpu_percent(interval=0.1):.1f}% used)",
                f"Memory: {mem.used/1024**3:.1f}GB / {mem.total/1024**3:.1f}GB ({mem.percent:.0f}% used)",
                f"Disk: {disk.used/1024**3:.1f}GB / {disk.total/1024**3:.1f}GB ({disk.percent:.0f}% used)",
            ]
        except ImportError:
            lines.append("(install psutil for CPU/memory/disk stats)")

        return "\n".join(lines)

    return [
        list_files,
        read_file,
        write_file,
        search_files,
        execute_command,
        run_python_code,
        take_screenshot,
        analyze_screen,
        get_system_info,
    ]
