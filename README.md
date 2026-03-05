# Docker Agent Setups

This repo contains Ubuntu-based Docker agent setups for autonomous Codex runs.

## Included
- `agent-daily-ubuntu/`: hardened daily-use profile (no sudo).
- `agent-daily-ubuntu-sudo/`: autonomous profile with passwordless sudo and runtime tool installs.

See each directory's `README.md` for build and run instructions.

## MVP: `agent-shell` (Python + Docker Sandboxes)
This repository now includes a Python `agent-shell` command that generates per-run Dockerfiles, builds local template images, and launches a sandbox immediately.

### Prerequisites
- Docker Desktop with **Sandboxes** enabled (`docker sandbox version` must work).
- API key in your shell:
  - Codex: `OPENAI_API_KEY`
  - Claude: `ANTHROPIC_API_KEY`

### Usage
```bash
./agent-shell -a codex -os ubuntu:24.04 -p ghidra radare2 -m . --allow-sudo
./agent-shell -a claude -os alpine:3.20 -p gdb strace -m .
```

### What it does
1. Generates a Dockerfile in `.agent-shell/dockerfiles/` for the selected `--os`.
2. Builds/reuses a local template image keyed by agent + OS + package set.
3. Installs requested packages with the image's native package manager.
4. Optionally enables passwordless `sudo` for user `agent`.
5. Adds agent auth adapter wiring (`~/.codex` or `~/.claude` mount + in-container symlink).
6. Starts a new sandbox and drops you into the selected agent.

### Current support
- Supported agents: `codex`, `claude`.
- `--os` supports these image families:
  - Debian/Ubuntu (`apt`)
  - Alpine (`apk`)
  - Fedora/RHEL family (`dnf`/`yum`)
  - Arch (`pacman`)
  - openSUSE/SLES (`zypper`)
- `-p/--package` values are passed directly to the selected package manager, so package names may differ across OS families.
