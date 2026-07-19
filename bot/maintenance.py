"""Git + dependency maintenance helpers used by the TUI hub.

All functions are async and shell out via ``asyncio.create_subprocess_exec`` so
the UI never blocks. Output is streamed line-by-line to an optional ``on_line``
callback (the TUI pipes this into a log panel).
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent

LineSink = Callable[[str], None] | None


@dataclass
class CommandResult:
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run(args: list[str], on_line: LineSink = None, cwd: Path | None = None) -> CommandResult:
    """Run a command, streaming combined stdout/stderr to ``on_line``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd or REPO_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as e:
        if on_line:
            on_line(f"[error] {e}")
        return CommandResult(127, str(e))

    collected: list[str] = []
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip("\n")
        collected.append(line)
        if on_line:
            on_line(line)
    rc = await proc.wait()
    return CommandResult(rc, "\n".join(collected))


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #


async def git_current_branch(on_line: LineSink = None) -> str:
    res = await run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return res.output.strip() if res.ok else ""


async def git_has_upstream() -> bool:
    res = await run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    return res.ok and bool(res.output.strip())


async def git_fetch(on_line: LineSink = None) -> CommandResult:
    return await run(["git", "fetch", "--all", "--prune"], on_line)


@dataclass
class UpdateStatus:
    has_upstream: bool
    behind: int
    ahead: int
    local_rev: str
    remote_rev: str
    branch: str
    error: str = ""

    @property
    def update_available(self) -> bool:
        return self.has_upstream and self.behind > 0


async def check_for_updates(on_line: LineSink = None) -> UpdateStatus:
    """Fetch and report how many commits behind/ahead the upstream we are."""
    branch = await git_current_branch()
    fetch = await git_fetch(on_line)
    if not fetch.ok:
        return UpdateStatus(False, 0, 0, "", "", branch, error="git fetch failed")

    if not await git_has_upstream():
        return UpdateStatus(False, 0, 0, "", "", branch, error="no upstream tracking branch")

    counts = await run(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"])
    behind = ahead = 0
    if counts.ok and counts.output.strip():
        parts = counts.output.split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
    local = (await run(["git", "rev-parse", "--short", "HEAD"])).output.strip()
    remote = (await run(["git", "rev-parse", "--short", "@{u}"])).output.strip()
    return UpdateStatus(True, behind, ahead, local, remote, branch)


async def git_pull(on_line: LineSink = None) -> CommandResult:
    # Fast-forward only: never create surprise merge commits under the user.
    return await run(["git", "pull", "--ff-only"], on_line)


async def git_head() -> str:
    """Full SHA of the current HEAD (empty string on failure)."""
    return (await run(["git", "rev-parse", "HEAD"])).output.strip()


async def git_log_between(old: str, new: str) -> list[str]:
    """Commit subject lines in ``old..new`` (newest first), merges excluded.

    Returns an empty list if either rev is missing or they're identical.
    """
    if not old or not new or old == new:
        return []
    res = await run(["git", "log", "--no-merges", "--pretty=format:%s", f"{old}..{new}"])
    if not res.ok:
        return []
    return [ln.strip() for ln in res.output.splitlines() if ln.strip()]


def top_changelog_section(text: str) -> str:
    """Extract the first version section from a Keep-a-Changelog-style file.

    Returns the first ``## `` heading and its body, stopping before the next
    ``## `` heading. Pure and I/O-free so it's directly unit-testable. Any
    leading preamble before the first ``## `` heading is skipped.
    """
    lines = text.splitlines()
    out: list[str] = []
    started = False
    for ln in lines:
        if ln.startswith("## "):
            if started:
                break
            started = True
            out.append(ln)
            continue
        if started:
            out.append(ln)
    return "\n".join(out).strip()


def read_changelog_section(root: Path | None = None) -> str:
    """Read CHANGELOG.md (if present) and return its top section. '' if absent."""
    path = (root or REPO_ROOT) / "CHANGELOG.md"
    try:
        return top_changelog_section(path.read_text(encoding="utf-8"))
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


async def install_dependencies(on_line: LineSink = None, upgrade: bool = False) -> CommandResult:
    args = [sys.executable, "-m", "pip", "install", "-r", str(REPO_ROOT / "requirements.txt")]
    if upgrade:
        args.append("--upgrade")
    return await run(args, on_line)


async def reinstall_dependencies(on_line: LineSink = None) -> CommandResult:
    """Force-reinstall every dependency."""
    args = [
        sys.executable, "-m", "pip", "install",
        "--force-reinstall", "--no-cache-dir",
        "-r", str(REPO_ROOT / "requirements.txt"),
    ]
    return await run(args, on_line)


@dataclass
class UpdateReport:
    """Result of a pull-and-install cycle, including what changed."""

    pulled: bool
    deps_ok: bool
    old_rev: str = ""
    new_rev: str = ""
    commits: list[str] = field(default_factory=list)  # subjects, newest first

    @property
    def changed(self) -> bool:
        return self.pulled and bool(self.commits)


async def pull_and_install(on_line: LineSink = None) -> UpdateReport:
    """Pull (ff-only), then install deps, reporting the commits that landed."""
    old = await git_head()
    pull = await git_pull(on_line)
    if not pull.ok:
        return UpdateReport(pulled=False, deps_ok=False, old_rev=old, new_rev=old)
    new = await git_head()
    deps = await install_dependencies(on_line)
    commits = await git_log_between(old, new)
    return UpdateReport(pulled=True, deps_ok=deps.ok, old_rev=old, new_rev=new, commits=commits)
