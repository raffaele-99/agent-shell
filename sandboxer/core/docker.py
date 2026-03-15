"""Thin subprocess wrapper around the ``docker sandbox`` CLI."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class SandboxRow:
    """One row from ``docker sandbox ls``."""

    name: str
    status: str
    image: str
    agent: str = ""
    workspace: str = ""


class DockerSandboxError(Exception):
    """Raised when a docker sandbox command fails."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"docker sandbox failed (rc={returncode}): {stderr}")


def _run(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "sandbox", *args]
    result = subprocess.run(
        cmd,
        text=True,
        capture_output=capture,
    )
    if check and result.returncode != 0:
        raise DockerSandboxError(result.returncode, result.stderr or "")
    return result


def _run_docker(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a plain ``docker`` command (not ``docker sandbox``)."""
    cmd = ["docker", *args]
    result = subprocess.run(cmd, text=True, capture_output=capture)
    if check and result.returncode != 0:
        raise DockerSandboxError(result.returncode, result.stderr or "")
    return result


# -- Template / image operations ---------------------------------------------

def build_template(dockerfile: str, tag: str, context_dir: str = ".") -> None:
    """Build a Docker image from a Dockerfile string."""
    _run_docker(
        ["build", "-t", tag, "-f", dockerfile, context_dir],
    )


def tag_image(source: str, target: str) -> None:
    """Tag a Docker image."""
    _run_docker(["tag", source, target])


def push_image(tag: str) -> None:
    """Push a Docker image to a registry."""
    _run_docker(["image", "push", tag])


def pull_image(tag: str) -> None:
    """Pull a Docker image from a registry."""
    _run_docker(["image", "pull", tag])


# -- Sandbox lifecycle -------------------------------------------------------

def create(
    agent: str,
    workspace: str | None = None,
    *,
    template: str | None = None,
    name: str | None = None,
    read_only: bool = False,
    extra_workspaces: list[str] | None = None,
) -> str:
    """Create a sandbox without starting the agent.  Returns the sandbox name.

    Usage: ``docker sandbox create [--name NAME] [-t IMAGE] AGENT WORKSPACE``

    The *agent* arg is one of the built-in agents (claude, codex, shell, …).
    ``-t`` overrides the base image.  Use ``docker sandbox exec`` to run
    commands inside the created sandbox.

    *extra_workspaces* are additional host paths to mount (e.g. auth
    directories).  They are passed as extra positional args after the
    primary workspace.
    """
    args = ["create"]
    if name:
        args.extend(["--name", name])
    if template:
        args.extend(["-t", template])
    args.append(agent)
    if workspace:
        ws = f"{workspace}:ro" if read_only else workspace
        args.append(ws)
    for ew in extra_workspaces or []:
        args.append(ew)
    result = _run(args)
    return (result.stdout or "").strip() or (name or "")


def list_sandboxes() -> list[SandboxRow]:
    """List sandboxes, returning parsed rows."""
    result = _run(["ls"], check=False)
    if result.returncode != 0:
        return []

    rows: list[SandboxRow] = []
    lines = (result.stdout or "").strip().splitlines()
    if len(lines) < 2:
        return rows

    # Parse the header to find column positions.
    header = lines[0]
    col_names = re.split(r"\s{2,}", header.strip())
    col_starts = [header.index(c) for c in col_names]

    # Build a lookup so we can extract by column name regardless of order.
    def _col_index(name: str) -> int | None:
        for i, c in enumerate(col_names):
            if c.upper() == name.upper():
                return i
        return None

    name_idx = _col_index("SANDBOX") or _col_index("NAME") or 0
    status_idx = _col_index("STATUS")
    image_idx = _col_index("IMAGE")
    agent_idx = _col_index("AGENT")
    workspace_idx = _col_index("WORKSPACE")

    def _get(parts: list[str], idx: int | None) -> str:
        return parts[idx] if idx is not None and idx < len(parts) else ""

    for line in lines[1:]:
        if not line.strip():
            continue
        parts: list[str] = []
        for i, start in enumerate(col_starts):
            end = col_starts[i + 1] if i + 1 < len(col_starts) else len(line)
            parts.append(line[start:end].strip())
        while len(parts) < len(col_names):
            parts.append("")
        rows.append(SandboxRow(
            name=_get(parts, name_idx) or parts[0],
            status=_get(parts, status_idx),
            image=_get(parts, image_idx),
            agent=_get(parts, agent_idx),
            workspace=_get(parts, workspace_idx),
        ))

    return rows


def exec_shell(
    name: str,
    command: str = "bash",
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Exec an interactive shell inside a running sandbox (foreground).

    ``docker sandbox exec`` supports ``-e`` for env vars and ``-it`` for
    interactive TTY.
    """
    cmd = ["docker", "sandbox", "exec", "-it"]
    if env:
        for key, value in env.items():
            cmd.extend(["-e", f"{key}={value}"])
    cmd.extend([name, command])
    subprocess.run(cmd)


def exec_command(name: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    """Exec a non-interactive command inside a sandbox."""
    return _run(["exec", name, *command])


def stop(name: str) -> None:
    _run(["stop", name])


def remove(name: str) -> None:
    _run(["rm", name])


def save_as_template(name: str, tag: str) -> None:
    _run(["save", name, tag])


def sandbox_stats(name: str) -> dict[str, str]:
    """Return resource usage for a sandbox via ``docker stats``."""
    result = _run_docker(
        ["stats", "--no-stream", "--format", "{{json .}}", name],
    )
    raw = json.loads((result.stdout or "").strip())
    return {
        "name": raw.get("Name", name),
        "cpu_percent": raw.get("CPUPerc", ""),
        "mem_usage": raw.get("MemUsage", ""),
        "mem_percent": raw.get("MemPerc", ""),
        "net_io": raw.get("NetIO", ""),
        "block_io": raw.get("BlockIO", ""),
        "pids": raw.get("PIDs", ""),
    }


# -- Utility -----------------------------------------------------------------

def sandbox_exists(name: str) -> bool:
    """Check whether a sandbox with the given name exists."""
    for row in list_sandboxes():
        if row.name == name:
            return True
    return False


def is_docker_available() -> bool:
    """Return True if the docker CLI is reachable."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            text=True,
            capture_output=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def is_sandbox_feature_available() -> bool:
    """Return True if ``docker sandbox`` subcommand exists."""
    try:
        result = subprocess.run(
            ["docker", "sandbox", "--help"],
            text=True,
            capture_output=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False
