"""Typer CLI — thin layer over the core library."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from .core.agents import delete_agent, list_agents, load_agent, save_agent
from .core.cleanup import (
    cleanup_orphans,
    find_all_cleanup_candidates,
    find_expired,
    find_idle,
    find_orphans,
)
from .core.config import GlobalConfig, config_dir
from .core.docker import is_docker_available, is_sandbox_feature_available
from .core.models import AgentProfile, SandboxTemplate
from .core.mount_allowlist import (
    add_to_allowlist,
    load_allowlist,
    remove_from_allowlist,
    validate_mount,
)
from .core.sandboxes import (
    create_sandbox,
    get_sandbox_stats,
    list_running_sandboxes,
    remove_sandbox,
    shell_into,
    snapshot_sandbox,
    stop_sandbox,
)
from .core.templates import (
    delete_template,
    list_templates,
    load_template,
    pull_template,
    push_template,
    save_template,
)

_ctx = {"help_option_names": ["--help", "-h"]}

app = typer.Typer(
    name="sandboxer",
    help="Manage Docker Sandbox environments for autonomous agents.",
    add_completion=False,
    rich_markup_mode="rich",
    invoke_without_command=True,
    context_settings=_ctx,
)


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


def _err(msg: str) -> None:
    typer.echo(msg, err=True)


def _check_docker() -> None:
    if not is_docker_available():
        _err("error: docker is not available. Install Docker Desktop and ensure it is running.")
        raise typer.Exit(1)
    if not is_sandbox_feature_available():
        _err("error: 'docker sandbox' is not available. Docker Desktop 4.58+ required.")
        raise typer.Exit(1)


# -- Sandbox commands --------------------------------------------------------

sandbox_app = typer.Typer(help="Manage sandboxes.", invoke_without_command=True, context_settings=_ctx)
app.add_typer(sandbox_app, name="sandbox")


@sandbox_app.callback(invoke_without_command=True)
def _sandbox_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


@sandbox_app.command("create")
def sandbox_create(
    template: Annotated[str, typer.Argument(help="Template name to use.")],
    agent: Annotated[str, typer.Argument(help="Agent profile name to use.")],
    workspace: Annotated[
        str,
        typer.Option("--workspace", "-w", help="Host directory to mount."),
    ] = ".",
    name: Annotated[
        Optional[str],
        typer.Option("--name", "-n", help="Sandbox name (auto-generated if omitted)."),
    ] = None,
    ttl: Annotated[
        Optional[int],
        typer.Option("--ttl", help="TTL in seconds before auto-cleanup."),
    ] = None,
    idle_timeout: Annotated[
        Optional[int],
        typer.Option("--idle-timeout", help="Idle timeout in seconds before auto-cleanup."),
    ] = None,
) -> None:
    """Create a new sandbox from a template and agent profile."""
    _check_docker()

    ok, reason = validate_mount(workspace)
    if not ok:
        _err(f"error: mount rejected — {reason}")
        raise typer.Exit(1)

    try:
        tmpl = load_template(template)
    except FileNotFoundError:
        _err(f"error: template not found: {template}")
        raise typer.Exit(1)

    try:
        ag = load_agent(agent)
    except FileNotFoundError:
        _err(f"error: agent profile not found: {agent}")
        raise typer.Exit(1)

    workspace_abs = str(Path(workspace).resolve())
    info = create_sandbox(
        tmpl, ag, workspace_abs,
        name=name,
        ttl_seconds=ttl,
        idle_timeout_seconds=idle_timeout,
    )
    typer.echo(f"Created sandbox: {info.name}")


@sandbox_app.command("ls")
def sandbox_ls() -> None:
    """List running sandboxer-managed sandboxes."""
    _check_docker()
    sandboxes = list_running_sandboxes()
    if not sandboxes:
        typer.echo("No sandboxes found.")
        return
    typer.echo(f"{'NAME':<40} {'STATUS':<15}")
    typer.echo("-" * 55)
    for s in sandboxes:
        typer.echo(f"{s.name:<40} {s.status:<15}")


@sandbox_app.command("shell")
def sandbox_shell(
    name: Annotated[str, typer.Argument(help="Sandbox name.")],
) -> None:
    """Open an interactive shell in a sandbox."""
    _check_docker()
    shell_into(name)


@sandbox_app.command("stop")
def sandbox_stop_cmd(
    name: Annotated[str, typer.Argument(help="Sandbox name.")],
) -> None:
    """Stop a running sandbox."""
    _check_docker()
    stop_sandbox(name)
    typer.echo(f"Stopped: {name}")


@sandbox_app.command("rm")
def sandbox_rm(
    name: Annotated[str, typer.Argument(help="Sandbox name.")],
) -> None:
    """Remove a sandbox."""
    _check_docker()
    remove_sandbox(name)
    typer.echo(f"Removed: {name}")


@sandbox_app.command("stats")
def sandbox_stats_cmd(
    name: Annotated[str, typer.Argument(help="Sandbox name.")],
) -> None:
    """Show resource usage for a sandbox."""
    _check_docker()
    stats = get_sandbox_stats(name)
    typer.echo(f"Name:       {stats.name}")
    typer.echo(f"CPU:        {stats.cpu_percent}")
    typer.echo(f"Memory:     {stats.mem_usage} ({stats.mem_percent})")
    typer.echo(f"Net I/O:    {stats.net_io}")
    typer.echo(f"Block I/O:  {stats.block_io}")
    typer.echo(f"PIDs:       {stats.pids}")


@sandbox_app.command("snapshot")
def sandbox_snapshot_cmd(
    name: Annotated[str, typer.Argument(help="Sandbox name.")],
    tag: Annotated[str, typer.Argument(help="Image tag for the snapshot.")],
    register: Annotated[
        bool,
        typer.Option("--register", help="Also register as a local template."),
    ] = False,
    local_name: Annotated[
        Optional[str],
        typer.Option("--as", help="Local template name (defaults to tag-derived name)."),
    ] = None,
) -> None:
    """Snapshot a sandbox as a Docker image."""
    _check_docker()
    snapshot_sandbox(name, tag)
    typer.echo(f"Snapshot saved: {tag}")

    if register:
        tmpl_name = local_name or tag.split("/")[-1].split(":")[0]
        tmpl = SandboxTemplate(name=tmpl_name, base_image=tag)
        save_template(tmpl)
        typer.echo(f"Registered template: {tmpl_name}")


# -- Template commands -------------------------------------------------------

template_app = typer.Typer(help="Manage sandbox templates.", invoke_without_command=True, context_settings=_ctx)
app.add_typer(template_app, name="template")


@template_app.callback(invoke_without_command=True)
def _template_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


@template_app.command("ls")
def template_ls() -> None:
    """List available templates."""
    templates = list_templates()
    if not templates:
        typer.echo("No templates found. Use 'sandboxer template create' to add one.")
        return
    typer.echo(f"{'NAME':<25} {'BASE IMAGE':<40} {'DESCRIPTION'}")
    typer.echo("-" * 80)
    for t in templates:
        typer.echo(f"{t.name:<25} {t.base_image:<40} {t.description}")


@template_app.command("create")
def template_create(
    name: Annotated[str, typer.Argument(help="Template name.")],
    base_image: Annotated[
        str,
        typer.Option("--base", "-b", help="Base Docker image."),
    ] = "docker/sandbox-templates:latest",
    description: Annotated[str, typer.Option("--desc", "-d")] = "",
    packages: Annotated[Optional[list[str]], typer.Option("--package", "-p")] = None,
    pip_packages: Annotated[Optional[list[str]], typer.Option("--pip")] = None,
    npm_packages: Annotated[Optional[list[str]], typer.Option("--npm")] = None,
    agent_type: Annotated[
        Optional[str],
        typer.Option("--agent-type", "-a", help="Agent type: claude, codex, gemini."),
    ] = None,
) -> None:
    """Create a new sandbox template."""
    tmpl = SandboxTemplate(
        name=name,
        description=description,
        base_image=base_image,
        packages=packages or [],
        pip_packages=pip_packages or [],
        npm_packages=npm_packages or [],
        agent_type=agent_type,
    )
    path = save_template(tmpl)
    typer.echo(f"Template saved: {path}")


@template_app.command("rm")
def template_rm(
    name: Annotated[str, typer.Argument(help="Template name to delete.")],
) -> None:
    """Delete a template."""
    delete_template(name)
    typer.echo(f"Deleted template: {name}")


@template_app.command("show")
def template_show(
    name: Annotated[str, typer.Argument(help="Template name.")],
) -> None:
    """Show template details."""
    try:
        tmpl = load_template(name)
    except FileNotFoundError:
        _err(f"error: template not found: {name}")
        raise typer.Exit(1)
    typer.echo(tmpl.model_dump_json(indent=2))


@template_app.command("push")
def template_push_cmd(
    name: Annotated[str, typer.Argument(help="Local template name.")],
    registry_tag: Annotated[str, typer.Argument(help="Registry tag (e.g. myregistry.io/sandbox:v1).")],
) -> None:
    """Push a template image to a registry."""
    _check_docker()
    try:
        push_template(name, registry_tag)
    except FileNotFoundError:
        _err(f"error: template not found: {name}")
        raise typer.Exit(1)
    typer.echo(f"Pushed: {registry_tag}")


@template_app.command("pull")
def template_pull_cmd(
    registry_tag: Annotated[str, typer.Argument(help="Registry tag to pull.")],
    local_name: Annotated[
        Optional[str],
        typer.Option("--as", help="Local template name."),
    ] = None,
) -> None:
    """Pull a template image from a registry."""
    _check_docker()
    tmpl = pull_template(registry_tag, local_name)
    typer.echo(f"Pulled and registered: {tmpl.name}")


# -- Agent commands ----------------------------------------------------------

agent_app = typer.Typer(help="Manage agent profiles.", invoke_without_command=True, context_settings=_ctx)
app.add_typer(agent_app, name="agent")


@agent_app.callback(invoke_without_command=True)
def _agent_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


@agent_app.command("ls")
def agent_ls() -> None:
    """List agent profiles."""
    profiles = list_agents()
    if not profiles:
        typer.echo("No agent profiles found. Use 'sandboxer agent create' to add one.")
        return
    typer.echo(f"{'NAME':<20} {'TYPE':<10} {'AUTH'}")
    typer.echo("-" * 50)
    for a in profiles:
        auth = a.api_key_env_var if a.api_key_env_var else f"auth_dir: {a.auth_dir}" if a.auth_dir else "none"
        typer.echo(f"{a.name:<20} {a.agent_type:<10} {auth}")


@agent_app.command("create")
def agent_create(
    name: Annotated[str, typer.Argument(help="Profile name.")],
    agent_type: Annotated[
        str,
        typer.Option("--type", "-t", help="Agent type: claude, codex, gemini."),
    ] = "claude",
    api_key_env_var: Annotated[
        str,
        typer.Option("--env-var", "-e", help="Environment variable holding the API key."),
    ] = "",
    auth_dir: Annotated[
        Optional[str],
        typer.Option(
            "--auth-dir",
            help="Host auth directory to mount into the sandbox (e.g. ~/.claude). "
            "WARNING: bypasses the credential proxy — the sandbox will have direct "
            "access to your auth tokens.",
        ),
    ] = None,
) -> None:
    """Create an agent profile."""
    # Default env var from agent type if not specified and no auth_dir provided.
    if not api_key_env_var and not auth_dir:
        defaults = {
            "claude": "ANTHROPIC_API_KEY",
            "codex": "OPENAI_API_KEY",
            "gemini": "GOOGLE_API_KEY",
        }
        api_key_env_var = defaults.get(agent_type, "")

    profile = AgentProfile(
        name=name,
        agent_type=agent_type,
        api_key_env_var=api_key_env_var,
        auth_dir=auth_dir,
    )
    path = save_agent(profile)
    typer.echo(f"Agent profile saved: {path}")


@agent_app.command("rm")
def agent_rm(
    name: Annotated[str, typer.Argument(help="Profile name to delete.")],
) -> None:
    """Delete an agent profile."""
    delete_agent(name)
    typer.echo(f"Deleted agent profile: {name}")


# -- Mount allowlist commands ------------------------------------------------

mount_app = typer.Typer(help="Manage mount allowlist.", invoke_without_command=True, context_settings=_ctx)
app.add_typer(mount_app, name="mount")


@mount_app.callback(invoke_without_command=True)
def _mount_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


@mount_app.command("ls")
def mount_ls() -> None:
    """List allowed mount paths."""
    paths = load_allowlist()
    if not paths:
        typer.echo("No allowlist configured (all non-blocked paths are allowed).")
        return
    for p in paths:
        typer.echo(p)


@mount_app.command("add")
def mount_add(
    path: Annotated[str, typer.Argument(help="Host path to allow.")],
) -> None:
    """Add a path to the mount allowlist."""
    paths = add_to_allowlist(path)
    typer.echo(f"Allowlist updated ({len(paths)} entries).")


@mount_app.command("rm")
def mount_rm(
    path: Annotated[str, typer.Argument(help="Host path to remove from allowlist.")],
) -> None:
    """Remove a path from the mount allowlist."""
    paths = remove_from_allowlist(path)
    typer.echo(f"Allowlist updated ({len(paths)} entries).")


# -- Cleanup commands --------------------------------------------------------

@app.command("cleanup")
def do_cleanup(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List candidates without removing."),
    ] = False,
    expired: Annotated[
        bool,
        typer.Option("--expired", help="Only clean up expired (TTL) sandboxes."),
    ] = False,
    idle: Annotated[
        bool,
        typer.Option("--idle", help="Only clean up idle sandboxes."),
    ] = False,
) -> None:
    """Find and remove orphaned, expired, or idle sandboxes."""
    _check_docker()

    # If specific flags given, only show those categories.
    if expired or idle:
        candidates: list[str] = []
        if expired:
            found = find_expired()
            if dry_run:
                if found:
                    typer.echo("Expired sandboxes:")
                    for n in found:
                        typer.echo(f"  {n}")
            candidates.extend(found)
        if idle:
            found = find_idle()
            if dry_run:
                if found:
                    typer.echo("Idle sandboxes:")
                    for n in found:
                        typer.echo(f"  {n}")
            candidates.extend(found)

        if not candidates:
            typer.echo("No matching sandboxes found.")
            return
        if dry_run:
            return
        removed = cleanup_orphans(candidates)
        typer.echo(f"Removed {len(removed)} sandbox(es).")
        return

    # Default: show all categories.
    all_candidates = find_all_cleanup_candidates()
    total = sum(len(v) for v in all_candidates.values())

    if total == 0:
        typer.echo("No cleanup candidates found.")
        return

    if dry_run:
        for category, names in all_candidates.items():
            if names:
                typer.echo(f"{category.capitalize()} sandboxes:")
                for n in names:
                    typer.echo(f"  {n}")
        return

    # Deduplicate across categories.
    all_names = list({n for names in all_candidates.values() for n in names})
    removed = cleanup_orphans(all_names)
    typer.echo(f"Removed {len(removed)} sandbox(es).")


# -- Serve command -----------------------------------------------------------

@app.command("serve")
def serve_cmd(
    host: Annotated[
        str,
        typer.Option("--host", "-H", help="Bind address."),
    ] = "0.0.0.0",
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Bind port."),
    ] = 8080,
    ssl_certfile: Annotated[
        Optional[str],
        typer.Option("--ssl-certfile", help="Path to SSL certificate for HTTPS."),
    ] = None,
    ssl_keyfile: Annotated[
        Optional[str],
        typer.Option("--ssl-keyfile", help="Path to SSL key for HTTPS."),
    ] = None,
) -> None:
    """Start the web UI server for remote sandbox control."""
    import secrets

    import uvicorn

    from .web import create_app

    token = secrets.token_urlsafe(32)
    app_instance = create_app(token=token)

    scheme = "https" if ssl_certfile else "http"
    display_host = "localhost" if host == "0.0.0.0" else host
    url = f"{scheme}://{display_host}:{port}/?token={token}"

    typer.echo(f"Sandboxer web UI starting on {scheme}://{host}:{port}")
    typer.echo(f"Auth token: {token}")
    typer.echo(f"Open: {url}")

    kwargs: dict[str, object] = {
        "host": host,
        "port": port,
        "log_level": "info",
    }
    if ssl_certfile:
        kwargs["ssl_certfile"] = ssl_certfile
    if ssl_keyfile:
        kwargs["ssl_keyfile"] = ssl_keyfile

    uvicorn.run(app_instance, **kwargs)  # type: ignore[arg-type]


# -- Config command ----------------------------------------------------------

@app.command("config")
def show_config() -> None:
    """Show current configuration."""
    cfg = GlobalConfig.load()
    typer.echo(f"Config dir: {config_dir()}")
    typer.echo(f"Default template: {cfg.default_template or '(none)'}")
    typer.echo(f"Default agent: {cfg.default_agent or '(none)'}")
    typer.echo(f"Credential proxy port: {cfg.credential_proxy_port}")
    typer.echo(f"Auto-cleanup orphans: {cfg.auto_cleanup_orphans}")
    typer.echo(f"Network mode: {cfg.network_mode}")
    typer.echo(f"Default TTL: {cfg.default_ttl_seconds or '(none)'}")
    typer.echo(f"Default idle timeout: {cfg.default_idle_timeout_seconds or '(none)'}")


# -- Entrypoint --------------------------------------------------------------

def entrypoint() -> None:
    app()
