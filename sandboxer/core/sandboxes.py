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

    extra_args: list[str] = []

    # Credential proxy integration.
    proxy_url: str | None = None
    if agent.api_key_env_var:
        try:
            from .proxy_manager import get_proxy_manager

            pm = get_proxy_manager()
            proxy_url = pm.start_proxy(
                sandbox_name, [agent], port=config.credential_proxy_port
            )
            extra_args.extend([
                "--env", f"HTTP_PROXY={proxy_url}",
                "--env", f"HTTPS_PROXY={proxy_url}",
            ])
        except Exception:
            pass  # proxy is best-effort

    docker_create(
        template=template.base_image,
        workspace=workspace,
        name=sandbox_name,
        read_only=template.read_only_workspace,
        extra_args=extra_args if extra_args else None,
    )

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


def shell_into(name: str) -> None:
    try:
        from .metadata import touch_activity
        touch_activity(name)
    except Exception:
        pass
    docker_exec_shell(name)


def get_sandbox_stats(name: str) -> SandboxStats:
    """Get resource usage stats for a sandbox."""
    data = docker_sandbox_stats(name)
    return SandboxStats(**data)


def snapshot_sandbox(name: str, tag: str) -> None:
    """Commit the current state of a sandbox as a Docker image."""
    docker_save_as_template(name, tag)
