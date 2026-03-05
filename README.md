# Docker Agent Setups

This repo contains Ubuntu-based Docker agent setups for autonomous Codex runs.

## Included
- `agent-daily-ubuntu/`: hardened daily-use profile (no sudo).
- `agent-daily-ubuntu-sudo/`: autonomous profile with passwordless sudo and runtime tool installs.

See each directory's `README.md` for build and run instructions.

## MVP: `agent-shell` (Python + Docker)
This repository includes a Python `agent-shell` command that generates per-run Dockerfiles, builds local images, and launches an interactive container immediately.

### Prerequisites
- Docker engine available from your shell (`docker info` must work).
- API key in your shell:
  - Codex: `OPENAI_API_KEY`
  - Claude: `ANTHROPIC_API_KEY`

### Usage
```bash
./agent-shell codex -os ubuntu:24.04 -p ghidra radare2 -m . --allow-sudo
./agent-shell claude -os alpine:3.20 -p gdb strace -m .
./agent-shell -m . -p ripgrep -a codex
```

### What it does
1. Generates a Dockerfile in `.agent-shell/dockerfiles/` for the selected `--os`.
2. Builds/reuses a local Docker image keyed by agent + OS + package set.
3. Installs requested packages with the image's native package manager.
4. Optionally enables passwordless `sudo` for user `agent`.
5. Mounts agent auth (`~/.codex` or `~/.claude`) directly into `/home/agent`.
6. Starts `docker run --rm -it` and drops you into `/workspace`.

### Current support
- Supported agents: `codex`, `claude`.
- `--os` supports these image families:
  - Debian/Ubuntu (`apt`)
  - Alpine (`apk`)
  - Fedora/RHEL family (`dnf`/`yum`)
  - Arch (`pacman`)
  - openSUSE/SLES (`zypper`)
- `-p/--package` values are passed directly to the selected package manager, so package names may differ across OS families.
