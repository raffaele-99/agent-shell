# Docker Agent Setups

This repo contains Ubuntu-based Docker agent setups for autonomous Codex runs.

## Included
- `agent-daily-ubuntu/`: hardened daily-use profile (no sudo).
- `agent-daily-ubuntu-sudo/`: autonomous profile with passwordless sudo and runtime tool installs.

See each directory's `README.md` for build and run instructions.

## MVP: `agent-shell` (Python + Docker)
This repository includes a Python `agent-shell` command that generates per-run Dockerfiles, builds local images, and launches an interactive container immediately.

### Install
With `uv`:
```bash
uv tool install --from . agent-shell
```

Upgrade after pulling new commits:
```bash
uv tool upgrade --from . agent-shell
```

### Prerequisites
- Docker engine available from your shell (`docker info` must work).
- API key in your shell:
  - Codex: `OPENAI_API_KEY`
  - Claude: `ANTHROPIC_API_KEY`

### Usage
```bash
# Interactive shell
agent-shell codex -o ubuntu:24.04 -p ghidra -p radare2 -m . --allow-sudo
agent-shell claude -o alpine:3.20 -p gdb -p strace -m .

# Fully autonomous mode (no shell, agent runs directly)
agent-shell codex -m . --auto --allow-network
agent-shell claude -m . --auto

# Preview without executing
agent-shell codex -m . --dry-run

# Configuration and maintenance
agent-shell --config
agent-shell --prune
```

### What it does
1. Generates a Dockerfile in `~/.cache/agent-shell/dockerfiles/` (or `$XDG_CACHE_HOME/agent-shell/dockerfiles/`) for the selected `--os`.
2. Builds/reuses a local Docker image keyed by agent + OS + package set.
3. Installs requested packages with the image's native package manager.
4. Optionally enables passwordless `sudo` for user `agent`.
5. Mounts agent auth (`~/.codex` or `~/.claude`) read-only into `/home/agent`.
6. Sets a colored Bash prompt (`user@host:path`) for interactive shells.
7. Starts a hardened `docker run --rm -it` container and drops you into `/workspace`.

### Container security defaults
Every container runs with these hardening measures:
- `--cap-drop=ALL` — no Linux capabilities
- `--security-opt=no-new-privileges:true` — no privilege escalation
- `--pids-limit=512` — fork bomb protection
- `--memory=4g`, `--cpus=2` — resource limits
- `--network=none` — no network access (override with `--allow-network` or `--network bridge`)
- Auth directories mounted read-only

A sandbox summary is printed on each launch so you can verify the boundaries.

### Config defaults
Run `./agent-shell --config` to open an interactive setup wizard.  
Config file path: `~/.config/agent-shell/config.yml`

Supported keys:
- `default_agent`: `codex`, `claude`, or `null`
- `default_allow_sudo`: `true` or `false`
- `default_network`: `none`, `bridge`, or `host` (default: `none`)
- `default_auto`: `true` or `false` (default: `false`)
- `default_read_only_workspace`: `true` or `false` (default: `false`)

### Current support
- Supported agents: `codex`, `claude`.
- `--os` supports these image families:
  - Debian/Ubuntu (`apt`)
  - Alpine (`apk`)
  - Fedora/RHEL family (`dnf`/`yum`)
  - Arch (`pacman`)
  - openSUSE/SLES (`zypper`)
- `-p/--package` values are passed directly to the selected package manager; repeat the flag for each package (`-p pkg1 -p pkg2`). Package names may differ across OS families.
- `--auto` launches the agent in autonomous mode (Codex: `--full-auto`, Claude: `--dangerously-skip-permissions`).
- `--allow-network` enables network access; `--network <mode>` for explicit control.
- `--read-only-workspace` mounts the workspace as read-only.
- `--agent-version <ver>` overrides the agent CLI version installed in the image.
- `--dry-run` prints the Dockerfile and docker run command without executing.
- `--prune` removes all cached Dockerfiles and agent-shell Docker images.
