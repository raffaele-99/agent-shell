#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Iterable, Optional

import typer


DEFAULT_OS_IMAGE = "ubuntu:24.04"
CONFIG_REL_PATH = Path(".config/agent-shell/config.yml")
DEFAULT_ALLOW_SUDO = False
CACHE_FORMAT_VERSION = "3"


@dataclass(frozen=True)
class AgentAdapter:
    name: str
    auth_dirname: str
    env_var: str
    cli_binary: str

    def required_packages(self, os_family: str) -> list[str]:
        raise NotImplementedError

    def install_snippet(self, version: str | None = None) -> str:
        raise NotImplementedError

    def auto_args(self) -> list[str]:
        raise NotImplementedError

    def auth_target(self) -> str:
        return f"/home/agent/{self.auth_dirname}"


class CodexAdapter(AgentAdapter):
    def required_packages(self, os_family: str) -> list[str]:
        return ["ca-certificates", "curl", "tar", "gzip"]

    def auto_args(self) -> list[str]:
        return ["--full-auto"]

    def install_snippet(self, version: str | None = None) -> str:
        codex_version = version or "0.107.0"
        return textwrap.dedent(
            """\
            ARG CODEX_VERSION=__VERSION__
            RUN set -eux; \\
              arch="$(uname -m)"; \\
              case "${arch}" in \\
                x86_64) codex_target="x86_64-unknown-linux-musl" ;; \\
                aarch64|arm64) codex_target="aarch64-unknown-linux-musl" ;; \\
                *) echo "unsupported architecture for Codex install: ${arch}" >&2; exit 1 ;; \\
              esac; \\
              codex_url="https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${codex_target}.tar.gz"; \\
              tmpdir="$(mktemp -d)"; \\
              curl -fsSL "${codex_url}" -o "${tmpdir}/codex.tgz"; \\
              tar -xzf "${tmpdir}/codex.tgz" -C "${tmpdir}"; \\
              cp "${tmpdir}/codex-${codex_target}" /usr/local/bin/codex; \\
              chmod 0755 /usr/local/bin/codex; \\
              rm -rf "${tmpdir}"
            """
        ).strip().replace("__VERSION__", codex_version)


class ClaudeAdapter(AgentAdapter):
    def required_packages(self, os_family: str) -> list[str]:
        return ["nodejs", "npm", "ca-certificates"]

    def auto_args(self) -> list[str]:
        return ["--dangerously-skip-permissions"]

    def install_snippet(self, version: str | None = None) -> str:
        pkg = "@anthropic-ai/claude-code"
        if version:
            pkg = f"{pkg}@{version}"
        return f"RUN npm install -g {pkg}"


ADAPTERS: dict[str, AgentAdapter] = {
    "codex": CodexAdapter("codex", ".codex", "OPENAI_API_KEY", "codex"),
    "claude": ClaudeAdapter("claude", ".claude", "ANTHROPIC_API_KEY", "claude"),
    "claude-code": ClaudeAdapter("claude", ".claude", "ANTHROPIC_API_KEY", "claude"),
}


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True)


def normalize_agent(agent_name: str) -> AgentAdapter:
    adapter = ADAPTERS.get(agent_name.lower())
    if adapter is None:
        valid = ", ".join(sorted({"codex", "claude"}))
        raise ValueError(f"unsupported agent '{agent_name}' (supported: {valid})")
    return adapter


def infer_os_family(os_image: str) -> str:
    name = os_image.split("@", 1)[0]
    leaf = name.split("/")[-1]
    repo = leaf.split(":", 1)[0].lower()

    if repo in {"ubuntu", "debian", "kali", "linuxmint", "pop", "elementary"}:
        return "debian"
    if repo in {"alpine"}:
        return "alpine"
    if repo in {"fedora", "centos", "rockylinux", "almalinux", "oraclelinux", "rhel", "ubi"}:
        return "redhat"
    if repo in {"archlinux", "manjaro"}:
        return "arch"
    if repo in {"opensuse", "opensuse-leap", "opensuse-tumbleweed", "sles"}:
        return "suse"
    return "unknown"


def package_install_snippet(os_family: str, packages: Iterable[str]) -> str:
    package_list = [pkg for pkg in packages if pkg]
    if not package_list:
        return ""
    quoted = " ".join(shlex.quote(pkg) for pkg in package_list)

    snippets = {
        "debian": f"RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {quoted} && rm -rf /var/lib/apt/lists/*",
        "alpine": f"RUN apk add --no-cache {quoted}",
        "redhat": textwrap.dedent(
            f"""\
            RUN if command -v dnf >/dev/null 2>&1; then \\
                  dnf install -y {quoted} && dnf clean all; \\
                elif command -v yum >/dev/null 2>&1; then \\
                  yum install -y {quoted} && yum clean all; \\
                else \\
                  echo "missing dnf/yum package manager" >&2; exit 1; \\
                fi
            """
        ).strip(),
        "arch": f"RUN pacman -Sy --noconfirm --needed {quoted} && pacman -Scc --noconfirm",
        "suse": f"RUN zypper --non-interactive install --no-recommends {quoted} && zypper clean -a",
    }
    snippet = snippets.get(os_family)
    if snippet is None:
        raise ValueError(f"unsupported os family for package installation: {os_family}")
    return snippet


def user_setup_snippet(os_family: str) -> str:
    if os_family == "alpine":
        return textwrap.dedent(
            """\
            RUN set -eux; \\
              addgroup -S -g "${AGENT_GID}" agent 2>/dev/null || true; \\
              adduser -S -D -h /home/agent -u "${AGENT_UID}" -G agent agent 2>/dev/null || true; \\
              mkdir -p /home/agent /workspace; \\
              chown -R "${AGENT_UID}:${AGENT_GID}" /home/agent /workspace
            """
        ).strip()
    return textwrap.dedent(
        """\
        RUN set -eux; \\
          if ! id -u agent >/dev/null 2>&1; then \\
            groupadd --gid "${AGENT_GID}" agent 2>/dev/null || true; \\
            useradd --uid "${AGENT_UID}" --gid "${AGENT_GID}" -m -s /bin/bash agent 2>/dev/null || true; \\
          fi; \\
          mkdir -p /home/agent /workspace; \\
          chown -R "${AGENT_UID}:${AGENT_GID}" /home/agent /workspace
        """
    ).strip()


def sudo_snippet() -> str:
    return textwrap.dedent(
        """\
        RUN mkdir -p /etc/sudoers.d \
          && echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent \
          && chmod 0440 /etc/sudoers.d/agent
        """
    ).strip()


def sanitize_name(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.+-]+", "-", value).strip("-")
    return sanitized or "agent-shell"


def parse_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return None


def config_file_path() -> Path:
    return Path.home() / CONFIG_REL_PATH


def cache_root_path() -> Path:
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home) / "agent-shell"
    return Path.home() / ".cache" / "agent-shell"


def load_config(path: Path) -> dict[str, object]:
    config: dict[str, object] = {
        "default_agent": None,
        "default_allow_sudo": DEFAULT_ALLOW_SUDO,
        "default_network": "none",
        "default_auto": False,
        "default_read_only_workspace": False,
    }
    if not path.exists():
        return config

    for line in path.read_text(encoding="utf-8").splitlines():
        content = line.split("#", 1)[0].strip()
        if not content or ":" not in content:
            continue

        key, raw_value = content.split(":", 1)
        key = key.strip()
        raw = raw_value.strip().strip('"').strip("'")
        if key == "default_agent":
            if not raw or raw.lower() in {"none", "null", "~"}:
                config["default_agent"] = None
                continue
            candidate = raw.lower()
            if candidate == "claude-code":
                candidate = "claude"
            if candidate in {"codex", "claude"}:
                config["default_agent"] = candidate
            else:
                eprint(f"warning: ignoring unsupported default_agent in {path}: {raw}")
        elif key == "default_allow_sudo":
            parsed = parse_bool(raw)
            if parsed is None:
                eprint(f"warning: ignoring invalid default_allow_sudo in {path}: {raw}")
            else:
                config["default_allow_sudo"] = parsed
        elif key == "default_network":
            if raw:
                config["default_network"] = raw.lower()
        elif key == "default_auto":
            parsed = parse_bool(raw)
            if parsed is None:
                eprint(f"warning: ignoring invalid default_auto in {path}: {raw}")
            else:
                config["default_auto"] = parsed
        elif key == "default_read_only_workspace":
            parsed = parse_bool(raw)
            if parsed is None:
                eprint(f"warning: ignoring invalid default_read_only_workspace in {path}: {raw}")
            else:
                config["default_read_only_workspace"] = parsed

    return config


def write_config(path: Path, config: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    default_agent = config.get("default_agent")
    agent_value = "null" if default_agent is None else str(default_agent)
    allow_sudo = bool(config.get("default_allow_sudo", False))
    network = config.get("default_network", "none")
    auto = bool(config.get("default_auto", False))
    ro_workspace = bool(config.get("default_read_only_workspace", False))
    rendered = "\n".join(
        [
            "# agent-shell defaults",
            f"default_agent: {agent_value}",
            f"default_allow_sudo: {'true' if allow_sudo else 'false'}",
            f"default_network: {network}",
            f"default_auto: {'true' if auto else 'false'}",
            f"default_read_only_workspace: {'true' if ro_workspace else 'false'}",
            "",
        ]
    )
    path.write_text(rendered, encoding="utf-8")


def run_config_wizard(path: Path, existing_config: dict[str, object]) -> None:
    print(f"Config path: {path}")
    print("Press Enter to keep the current value.")

    current_agent = existing_config.get("default_agent")
    current_agent_label = "none" if current_agent is None else str(current_agent)
    try:
        while True:
            user_value = input(
                f"Default agent [codex/claude/none] ({current_agent_label}): "
            ).strip().lower()
            if not user_value:
                selected_agent = current_agent
                break
            if user_value in {"none", "null", "~"}:
                selected_agent = None
                break
            if user_value == "claude-code":
                user_value = "claude"
            if user_value in {"codex", "claude"}:
                selected_agent = user_value
                break
            print("Invalid value. Enter codex, claude, or none.")

        current_sudo = bool(existing_config.get("default_allow_sudo", DEFAULT_ALLOW_SUDO))
        current_sudo_label = "y" if current_sudo else "n"
        while True:
            user_value = input(
                f"Default allow sudo [y/n] ({current_sudo_label}): "
            ).strip()
            if not user_value:
                selected_sudo = current_sudo
                break
            parsed = parse_bool(user_value)
            if parsed is None:
                print("Invalid value. Enter y or n.")
                continue
            selected_sudo = parsed
            break
        current_network = str(existing_config.get("default_network", "none"))
        while True:
            user_value = input(
                f"Default network mode [none/bridge/host] ({current_network}): "
            ).strip().lower()
            if not user_value:
                selected_network = current_network
                break
            if user_value in {"none", "bridge", "host"}:
                selected_network = user_value
                break
            print("Invalid value. Enter none, bridge, or host.")

        current_auto = bool(existing_config.get("default_auto", False))
        current_auto_label = "y" if current_auto else "n"
        while True:
            user_value = input(
                f"Default auto mode [y/n] ({current_auto_label}): "
            ).strip()
            if not user_value:
                selected_auto = current_auto
                break
            parsed = parse_bool(user_value)
            if parsed is None:
                print("Invalid value. Enter y or n.")
                continue
            selected_auto = parsed
            break

        current_ro = bool(existing_config.get("default_read_only_workspace", False))
        current_ro_label = "y" if current_ro else "n"
        while True:
            user_value = input(
                f"Default read-only workspace [y/n] ({current_ro_label}): "
            ).strip()
            if not user_value:
                selected_ro = current_ro
                break
            parsed = parse_bool(user_value)
            if parsed is None:
                print("Invalid value. Enter y or n.")
                continue
            selected_ro = parsed
            break

    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        raise typer.Exit(1)

    new_config = {
        "default_agent": selected_agent,
        "default_allow_sudo": selected_sudo,
        "default_network": selected_network,
        "default_auto": selected_auto,
        "default_read_only_workspace": selected_ro,
    }
    write_config(path, new_config)
    print("Saved.")


def prompt_snippet() -> str:
    return textwrap.dedent(
        """\
        RUN { \
          echo 'if [ -n "$BASH_VERSION" ]; then'; \
          echo '  export PS1="\\[\\e[1;36m\\]\\u@\\h\\[\\e[0m\\]:\\[\\e[1;33m\\]\\w\\[\\e[0m\\]\\\\$ "'; \
          echo 'fi'; \
        } > /etc/profile.d/agent-shell-prompt.sh
        """
    ).strip()


def generate_dockerfile(
    os_image: str,
    os_family: str,
    adapter: AgentAdapter,
    packages: list[str],
    allow_sudo: bool,
    agent_version: str | None = None,
) -> str:
    base_packages = ["bash", "curl", "git", "ca-certificates"]
    if os_family != "alpine":
        base_packages.append("procps")

    all_packages: list[str] = []
    seen: set[str] = set()
    for pkg in [*base_packages, *adapter.required_packages(os_family), *packages]:
        if pkg not in seen:
            seen.add(pkg)
            all_packages.append(pkg)

    if allow_sudo and "sudo" not in seen:
        all_packages.append("sudo")

    install_packages = package_install_snippet(os_family, all_packages)
    user_setup = user_setup_snippet(os_family)

    parts = [
        f"FROM {os_image}",
        "",
        "ARG AGENT_UID=1000",
        "ARG AGENT_GID=1000",
        "",
    ]

    if install_packages:
        parts.extend([install_packages, ""])

    parts.extend([user_setup, "", adapter.install_snippet(version=agent_version), ""])

    if allow_sudo:
        parts.extend([sudo_snippet(), ""])

    parts.extend([prompt_snippet(), ""])

    parts.extend(
        [
            "ENV HOME=/home/agent",
            "WORKDIR /workspace",
            "USER agent",
            "",
        ]
    )

    return "\n".join(parts).strip() + "\n"


def ensure_docker_engine() -> None:
    check = subprocess.run(
        ["docker", "info"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if check.returncode != 0:
        message = (check.stderr or "").strip()
        eprint(
            f"error: docker engine is not accessible "
            f"({message or 'unknown docker error'})."
        )
        raise typer.Exit(1)


def do_prune() -> None:
    cache_root = cache_root_path()
    dockerfile_dir = cache_root / "dockerfiles"
    removed_files = 0
    if dockerfile_dir.is_dir():
        for f in dockerfile_dir.iterdir():
            if f.suffix == ".Dockerfile":
                f.unlink()
                removed_files += 1
    print(f"Removed {removed_files} cached Dockerfile(s).")

    result = subprocess.run(
        ["docker", "images", "--filter=reference=agent-shell/*", "--format", "{{.Repository}}:{{.Tag}}"],
        text=True,
        capture_output=True,
    )
    images = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if images:
        subprocess.run(["docker", "rmi", *images], text=True)
        print(f"Removed {len(images)} Docker image(s).")
    else:
        print("No agent-shell Docker images found.")


# -- Typer app ---------------------------------------------------------------

app = typer.Typer(
    name="agent-shell",
    help=(
        "Build a Docker image for an agent and open an interactive "
        "container with your workspace mounted."
    ),
    add_completion=False,
    rich_markup_mode="rich",
)

# Store passthrough args (everything after --) since typer/click can't
# natively forward them.
_agent_passthrough_args: list[str] = []


@app.command()
def main(
    # -- agent selection --
    agent: Annotated[
        Optional[str],
        typer.Argument(help="Agent to run: [bold]codex[/bold] | [bold]claude[/bold]."),
    ] = None,
    agent_flag: Annotated[
        Optional[str],
        typer.Option("--agent", "-a", help="Agent to run (alternative to positional)."),
    ] = None,
    # -- image & packages --
    os_image: Annotated[
        str,
        typer.Option("--os", "-o", help="Base OS image."),
    ] = DEFAULT_OS_IMAGE,
    packages: Annotated[
        Optional[list[str]],
        typer.Option("--package", "-p", help="OS package to install (repeatable)."),
    ] = None,
    agent_version: Annotated[
        Optional[str],
        typer.Option("--agent-version", help="Override the agent CLI version to install."),
    ] = None,
    # -- workspace --
    mount: Annotated[
        str,
        typer.Option("--mount", "-m", help="Workspace directory to mount to /workspace."),
    ] = ".",
    read_only_workspace: Annotated[
        bool,
        typer.Option("--read-only-workspace", help="Mount the workspace as read-only."),
    ] = False,
    # -- container security --
    allow_sudo: Annotated[
        Optional[bool],
        typer.Option("--allow-sudo/--no-allow-sudo", help="Enable/disable passwordless sudo."),
    ] = None,
    network: Annotated[
        Optional[str],
        typer.Option("--network", help="Docker network mode (none, bridge, host)."),
    ] = None,
    allow_network: Annotated[
        bool,
        typer.Option("--allow-network", help="Allow network access (shorthand for --network bridge)."),
    ] = False,
    # -- run mode --
    auto: Annotated[
        bool,
        typer.Option("--auto", help="Launch the agent in fully autonomous mode."),
    ] = False,
    name: Annotated[
        Optional[str],
        typer.Option("--name", help="Container name (auto-generated if omitted)."),
    ] = None,
    rebuild: Annotated[
        bool,
        typer.Option("--rebuild", help="Rebuild image even if cached."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print Dockerfile and docker run command without executing."),
    ] = False,
    # -- management --
    config: Annotated[
        bool,
        typer.Option("--config", help="Open interactive configuration wizard."),
    ] = False,
    prune: Annotated[
        bool,
        typer.Option("--prune", help="Remove cached Dockerfiles and agent-shell Docker images."),
    ] = False,
) -> None:
    config_path = config_file_path()
    cfg = load_config(config_path)

    if config:
        run_config_wizard(config_path, cfg)
        return

    if prune:
        do_prune()
        return

    # -- validate network flags --
    if network and allow_network:
        eprint("error: --network and --allow-network are mutually exclusive.")
        raise typer.Exit(1)
    if allow_network:
        network = "bridge"

    # -- resolve agent --
    selected_agent = agent_flag or agent or cfg.get("default_agent")
    if agent and agent_flag and agent != agent_flag:
        eprint(
            f"error: conflicting agent values: "
            f"positional '{agent}' vs --agent '{agent_flag}'"
        )
        raise typer.Exit(1)
    if not selected_agent:
        eprint(
            "error: agent is required (use positional `agent-shell codex`, "
            "-a codex, or set default_agent in ~/.config/agent-shell/config.yml)"
        )
        raise typer.Exit(1)

    try:
        adapter = normalize_agent(selected_agent)
    except ValueError as err:
        eprint(f"error: {err}")
        raise typer.Exit(1)

    workspace = Path(mount).expanduser().resolve()
    if not workspace.is_dir():
        eprint(f"error: mount path does not exist or is not a directory: {workspace}")
        raise typer.Exit(1)

    os_family = infer_os_family(os_image)
    if os_family == "unknown":
        eprint(
            "error: unable to infer package manager for --os image. "
            "Supported families: debian/ubuntu, alpine, fedora/rhel, arch, opensuse."
        )
        raise typer.Exit(1)

    pkg_list = list(packages) if packages else []
    resolved_allow_sudo = (
        bool(cfg.get("default_allow_sudo", DEFAULT_ALLOW_SUDO))
        if allow_sudo is None
        else allow_sudo
    )
    if resolved_allow_sudo and allow_sudo is None:
        eprint(
            "warning: sudo enabled via config default. "
            "Use --no-allow-sudo to disable."
        )

    host_home = Path.home().resolve()
    auth_path = host_home / adapter.auth_dirname
    has_auth_dir = auth_path.exists()
    has_env_auth = adapter.env_var in os.environ

    if not has_auth_dir:
        eprint(
            f"warning: expected auth dir does not exist: {auth_path}\n"
            f"         {adapter.name} may require authentication inside the container."
        )

    if not has_env_auth and not has_auth_dir:
        eprint(
            f"warning: neither {adapter.env_var} nor {auth_path} is available; "
            f"{adapter.name} will likely require authentication."
        )

    ensure_docker_engine()

    cache_key = "|".join(
        [
            f"format={CACHE_FORMAT_VERSION}",
            f"agent={adapter.name}",
            f"agent_version={agent_version or 'default'}",
            f"os={os_image}",
            f"os_family={os_family}",
            f"sudo={int(resolved_allow_sudo)}",
            f"packages={' '.join(pkg_list)}",
            f"uid={os.getuid()}",
            f"gid={os.getgid()}",
        ]
    )
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:12]

    cache_root = cache_root_path()
    dockerfile_dir = cache_root / "dockerfiles"
    dockerfile_dir.mkdir(parents=True, exist_ok=True)
    dockerfile_path = dockerfile_dir / f"{adapter.name}-{digest}.Dockerfile"

    dockerfile_content = generate_dockerfile(
        os_image=os_image,
        os_family=os_family,
        adapter=adapter,
        packages=pkg_list,
        allow_sudo=resolved_allow_sudo,
        agent_version=agent_version,
    )
    dockerfile_path.write_text(dockerfile_content, encoding="utf-8")

    image_tag = f"agent-shell/{adapter.name}:{digest}"
    if rebuild:
        build_needed = True
    else:
        inspect = subprocess.run(
            ["docker", "image", "inspect", image_tag],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        build_needed = inspect.returncode != 0

    if build_needed:
        print(f"Building image {image_tag}")
        build_cmd = [
            "docker",
            "build",
            "-t",
            image_tag,
            "--build-arg",
            f"AGENT_UID={os.getuid()}",
            "--build-arg",
            f"AGENT_GID={os.getgid()}",
            "-f",
            str(dockerfile_path),
            str(dockerfile_path.parent),
        ]
        try:
            run(build_cmd)
        except subprocess.CalledProcessError as exc:
            cmd_text = " ".join(shlex.quote(part) for part in build_cmd)
            eprint(f"error: docker build failed ({exc.returncode}): {cmd_text}")
            raise typer.Exit(exc.returncode or 1)
    else:
        print(f"Using cached image {image_tag}")

    container_name = name
    if not container_name:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        container_name = sanitize_name(f"agent-shell-{adapter.name}-{timestamp}")

    resolved_network = network or str(cfg.get("default_network", "none"))
    resolved_ro = read_only_workspace or bool(cfg.get("default_read_only_workspace", False))

    run_cmd = [
        "docker",
        "run",
        "--rm",
        "-it",
        "--name",
        container_name,
        "-w",
        "/workspace",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=512",
        "--memory=4g",
        "--cpus=2",
        f"--network={resolved_network}",
        "-v",
        f"{workspace}:/workspace{':ro' if resolved_ro else ''}",
    ]

    if has_auth_dir:
        run_cmd.extend(["-v", f"{auth_path}:{adapter.auth_target()}:ro"])

    if has_env_auth:
        run_cmd.extend(["-e", f"{adapter.env_var}={os.environ[adapter.env_var]}"])

    run_cmd.append(image_tag)
    if _agent_passthrough_args:
        run_cmd.extend([adapter.cli_binary, *_agent_passthrough_args])
    elif auto or cfg.get("default_auto", False):
        run_cmd.extend([adapter.cli_binary, *adapter.auto_args()])
    else:
        run_cmd.extend(["/bin/bash", "-l"])

    if dry_run:
        print(f"# Dockerfile: {dockerfile_path}")
        print(dockerfile_content)
        print("# Run command:")
        safe_cmd = []
        for part in run_cmd:
            if part.startswith(f"{adapter.env_var}="):
                safe_cmd.append(f"{adapter.env_var}=***")
            else:
                safe_cmd.append(part)
        print(" ".join(shlex.quote(p) for p in safe_cmd))
        return

    print(f"Generated Dockerfile: {dockerfile_path}")
    print(f"Launching container {container_name}")
    print(f"  Sandbox: cap_drop=ALL, no-new-privileges, pids_limit=512, memory=4g, cpus=2")
    print(f"  Network: {resolved_network}")
    ws_mode = "read-only" if resolved_ro else "read-write"
    print(f"  Workspace: {workspace} -> /workspace ({ws_mode})")
    print(f"  Sudo: {'enabled' if resolved_allow_sudo else 'disabled'}")
    try:
        result = subprocess.run(run_cmd)
        raise typer.Exit(result.returncode)
    except OSError as exc:
        eprint(f"error: failed to start docker run: {exc}")
        raise typer.Exit(1)


def entrypoint() -> None:
    global _agent_passthrough_args
    argv = sys.argv[1:]
    if "--" in argv:
        idx = argv.index("--")
        _agent_passthrough_args = argv[idx + 1 :]
        sys.argv = [sys.argv[0], *argv[:idx]]
    app()


if __name__ == "__main__":
    entrypoint()
