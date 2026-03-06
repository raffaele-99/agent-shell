#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
from typing import Iterable


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

    def install_snippet(self) -> str:
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

    def install_snippet(self) -> str:
        return textwrap.dedent(
            """\
            ARG CODEX_VERSION=0.107.0
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
        ).strip()


class ClaudeAdapter(AgentAdapter):
    def required_packages(self, os_family: str) -> list[str]:
        return ["nodejs", "npm", "ca-certificates"]

    def auto_args(self) -> list[str]:
        return ["--dangerously-skip-permissions"]

    def install_snippet(self) -> str:
        return "RUN npm install -g @anthropic-ai/claude-code"


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

    return config


def write_config(path: Path, config: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    default_agent = config.get("default_agent")
    agent_value = "null" if default_agent is None else str(default_agent)
    allow_sudo = bool(config.get("default_allow_sudo", False))
    rendered = "\n".join(
        [
            "# agent-shell defaults",
            f"default_agent: {agent_value}",
            f"default_allow_sudo: {'true' if allow_sudo else 'false'}",
            "",
        ]
    )
    path.write_text(rendered, encoding="utf-8")


def run_config_wizard(path: Path, existing_config: dict[str, object]) -> int:
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
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1

    new_config = {
        "default_agent": selected_agent,
        "default_allow_sudo": selected_sudo,
    }
    write_config(path, new_config)
    print("Saved.")
    return 0


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


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-shell",
        description=(
            "Build a Docker image for an agent and open an interactive container "
            "with your workspace mounted."
        ),
    )
    parser.add_argument(
        "agent",
        nargs="?",
        help="Agent to run positionally: codex | claude",
    )
    parser.add_argument(
        "-a",
        "--agent",
        dest="agent_flag",
        help="Agent to run (optional if provided positionally).",
    )
    parser.add_argument(
        "-o",
        "-os",
        "--os",
        dest="os_image",
        default=DEFAULT_OS_IMAGE,
        help=f"Base OS image (default: {DEFAULT_OS_IMAGE})",
    )
    parser.add_argument(
        "-p",
        "--package",
        dest="packages",
        nargs="+",
        action="append",
        default=[],
        help="One or more OS packages to install (repeatable).",
    )
    parser.add_argument(
        "-m",
        "--mount",
        default=".",
        help="Workspace directory to mount to /workspace (default: current directory).",
    )
    sudo_group = parser.add_mutually_exclusive_group()
    sudo_group.add_argument(
        "--allow-sudo",
        dest="allow_sudo",
        action="store_true",
        help="Enable passwordless sudo for user agent.",
    )
    sudo_group.add_argument(
        "--no-allow-sudo",
        dest="allow_sudo",
        action="store_false",
        help="Disable sudo even if config default enables it.",
    )
    parser.set_defaults(allow_sudo=None)
    parser.add_argument("--name", help="Container name (default: auto-generated).")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild image even if cached.")
    net_group = parser.add_mutually_exclusive_group()
    net_group.add_argument(
        "--network",
        dest="network",
        default=None,
        help="Docker network mode (e.g. none, bridge, host). Default: none.",
    )
    net_group.add_argument(
        "--allow-network",
        dest="network",
        action="store_const",
        const="bridge",
        help="Allow network access (shorthand for --network bridge).",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Launch the agent in fully autonomous mode instead of opening a shell.",
    )
    parser.add_argument(
        "--config",
        action="store_true",
        help="Open interactive configuration for defaults at ~/.config/agent-shell/config.yml.",
    )
    return parser


def split_agent_args(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" not in argv:
        return argv, []
    idx = argv.index("--")
    return argv[:idx], argv[idx + 1 :]


def generate_dockerfile(
    os_image: str,
    os_family: str,
    adapter: AgentAdapter,
    packages: list[str],
    allow_sudo: bool,
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

    parts.extend([user_setup, "", adapter.install_snippet(), ""])

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
        raise RuntimeError(
            f"docker engine is not accessible ({message or 'unknown docker error'})."
        )


def main(argv: list[str]) -> int:
    cli_args, agent_args = split_agent_args(argv)
    parser = make_parser()
    args = parser.parse_args(cli_args)

    config_path = config_file_path()
    config = load_config(config_path)
    if args.config:
        return run_config_wizard(config_path, config)

    selected_agent = args.agent_flag or args.agent or config.get("default_agent")
    if args.agent and args.agent_flag and args.agent != args.agent_flag:
        parser.error(
            f"conflicting agent values: positional '{args.agent}' vs --agent '{args.agent_flag}'"
        )
    if not selected_agent:
        parser.error(
            "agent is required (use positional `agent-shell codex`, -a codex, "
            "or set default_agent in ~/.config/agent-shell/config.yml)"
        )

    try:
        adapter = normalize_agent(selected_agent)
    except ValueError as err:
        parser.error(str(err))

    workspace = Path(args.mount).expanduser().resolve()
    if not workspace.is_dir():
        parser.error(f"mount path does not exist or is not a directory: {workspace}")

    os_family = infer_os_family(args.os_image)
    if os_family == "unknown":
        parser.error(
            "unable to infer package manager for --os image. Supported families: "
            "debian/ubuntu, alpine, fedora/rhel, arch, opensuse."
        )

    package_groups = args.packages if args.packages else []
    packages = [pkg for group in package_groups for pkg in group]
    resolved_allow_sudo = (
        bool(config.get("default_allow_sudo", DEFAULT_ALLOW_SUDO))
        if args.allow_sudo is None
        else args.allow_sudo
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

    try:
        ensure_docker_engine()
    except RuntimeError as err:
        parser.error(str(err))

    cache_key = "|".join(
        [
            f"format={CACHE_FORMAT_VERSION}",
            f"agent={adapter.name}",
            f"os={args.os_image}",
            f"os_family={os_family}",
            f"sudo={int(resolved_allow_sudo)}",
            f"packages={' '.join(packages)}",
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
        os_image=args.os_image,
        os_family=os_family,
        adapter=adapter,
        packages=packages,
        allow_sudo=resolved_allow_sudo,
    )
    dockerfile_path.write_text(dockerfile_content, encoding="utf-8")

    image_tag = f"agent-shell/{adapter.name}:{digest}"
    if args.rebuild:
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
            return exc.returncode or 1
    else:
        print(f"Using cached image {image_tag}")

    container_name = args.name
    if not container_name:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        container_name = sanitize_name(f"agent-shell-{adapter.name}-{timestamp}")

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
        f"--network={args.network or 'none'}",
        "-v",
        f"{workspace}:/workspace",
    ]

    if has_auth_dir:
        run_cmd.extend(["-v", f"{auth_path}:{adapter.auth_target()}:ro"])

    if has_env_auth:
        run_cmd.extend(["-e", f"{adapter.env_var}={os.environ[adapter.env_var]}"])

    run_cmd.append(image_tag)
    if agent_args:
        run_cmd.extend([adapter.cli_binary, *agent_args])
    elif args.auto:
        run_cmd.extend([adapter.cli_binary, *adapter.auto_args()])
    else:
        run_cmd.extend(["/bin/bash", "-l"])

    print(f"Generated Dockerfile: {dockerfile_path}")
    print(f"Launching container {container_name}")
    try:
        os.execvp(run_cmd[0], run_cmd)
    except OSError as exc:
        eprint(f"error: failed to start docker run: {exc}")
        return 1


def entrypoint() -> None:
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    entrypoint()
