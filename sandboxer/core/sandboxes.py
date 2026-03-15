"""Sandbox lifecycle orchestration — ties together docker, templates, and agents."""
from __future__ import annotations

from datetime import datetime

from .config import SANDBOX_NAME_PREFIX, GlobalConfig
from .docker import (
    create as docker_create,
    exec_shell as docker_exec_shell,
    list_sandboxes as docker_list,
    remove as docker_remove,
    sandbox_stats as docker_sandbox_stats,
    save_as_template as docker_save_as_template,
    stop as docker_stop,
)
from .models import AgentProfile, SandboxInfo, SandboxStats, SandboxTemplate


def _sandbox_name(template: str, agent: str) -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{SANDBOX_NAME_PREFIX}{template}-{agent}-{ts}"


def create_sandbox(
    template: SandboxTemplate,
    agent: AgentProfile,
    workspace: str,
    *,
    name: str | None = None,
    config: GlobalConfig | None = None,
    ttl_seconds: int | None = None,
    idle_timeout_seconds: int | None = None,
) -> SandboxInfo:
    """Create a new sandbox from a template + agent profile."""
    config = config or GlobalConfig.load()
    sandbox_name = name or _sandbox_name(template.name, agent.name)

    # Create the sandbox.  docker sandbox run only accepts --name,
    # -t/--template, and positional AGENT WORKSPACE args.  Env vars
    # must be set via docker sandbox exec.
    use_template = template.base_image if template.base_image != "docker/sandbox-templates:latest" else None
    extra_workspaces: list[str] = []
    if agent.auth_dir:
        from pathlib import Path

        resolved = str(Path(agent.auth_dir).expanduser().resolve())
        extra_workspaces.append(resolved)
    docker_create(
        agent=agent.agent_type,
        workspace=workspace,
        template=use_template,
        name=sandbox_name,
        read_only=template.read_only_workspace,
        extra_workspaces=extra_workspaces or None,
    )

    # Start credential proxy (best-effort).  The proxy URL is stored in
    # SandboxInfo and metadata so it can be passed as env vars to exec.
    proxy_url: str | None = None
    if agent.api_key_env_var:
        try:
            from .proxy_manager import get_proxy_manager

            pm = get_proxy_manager()
            proxy_url = pm.start_proxy(
                sandbox_name, [agent], port=config.credential_proxy_port
            )
        except Exception:
            pass  # proxy is best-effort

    # Metadata for auto-cleanup.
    resolved_ttl = ttl_seconds if ttl_seconds is not None else config.default_ttl_seconds
    resolved_idle = (
        idle_timeout_seconds
        if idle_timeout_seconds is not None
        else config.default_idle_timeout_seconds
    )
    if resolved_ttl is not None or resolved_idle is not None:
        try:
            from .metadata import SandboxMetadata, save_metadata

            now = datetime.now()
            meta = SandboxMetadata(
                name=sandbox_name,
                created_at=now,
                last_activity=now,
                ttl_seconds=resolved_ttl,
                idle_timeout_seconds=resolved_idle,
            )
            save_metadata(meta)
        except Exception:
            pass

    return SandboxInfo(
        name=sandbox_name,
        template=template.name,
        agent=agent.name,
        workspace=workspace,
        status="running",
        created_at=datetime.now(),
        credential_proxy_url=proxy_url,
    )


def list_running_sandboxes() -> list[SandboxInfo]:
    """List sandboxes managed by sandboxer (filtered by name prefix)."""
    rows = docker_list()
    results: list[SandboxInfo] = []
    for row in rows:
        if row.name.startswith(SANDBOX_NAME_PREFIX):
            results.append(
                SandboxInfo(
                    name=row.name,
                    status=row.status,
                    agent=row.agent,
                    workspace=row.workspace,
                )
            )
    return results


def stop_sandbox(name: str) -> None:
    docker_stop(name)
    try:
        from .proxy_manager import get_proxy_manager
        get_proxy_manager().stop_proxy(name)
    except Exception:
        pass


def remove_sandbox(name: str) -> None:
    docker_remove(name)
    try:
        from .proxy_manager import get_proxy_manager
        get_proxy_manager().stop_proxy(name)
    except Exception:
        pass
    try:
        from .metadata import delete_metadata
        delete_metadata(name)
    except Exception:
        pass


def _proxy_env(name: str) -> dict[str, str]:
    """Build proxy env vars for a sandbox if a proxy is running."""
    try:
        from .proxy_manager import get_proxy_manager
        pm = get_proxy_manager()
        url = pm.get_proxy_url(name)
        if url:
            return {"HTTP_PROXY": url, "HTTPS_PROXY": url}
    except Exception:
        pass
    return {}


def shell_into(name: str) -> None:
    try:
        from .metadata import touch_activity
        touch_activity(name)
    except Exception:
        pass
    env = _proxy_env(name)
    docker_exec_shell(name, env=env if env else None)


def get_sandbox_stats(name: str) -> SandboxStats:
    """Get resource usage stats for a sandbox."""
    data = docker_sandbox_stats(name)
    return SandboxStats(**data)


def snapshot_sandbox(name: str, tag: str) -> None:
    """Commit the current state of a sandbox as a Docker image."""
    docker_save_as_template(name, tag)
