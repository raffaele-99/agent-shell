# Repository Guidelines

## Project Structure & Module Organization
This repository contains two Dockerized Codex agent profiles:
- `agent-daily-ubuntu/`: hardened daily profile (non-root user, no `sudo`).
- `agent-daily-ubuntu-sudo/`: autonomous profile with passwordless `sudo`.

Each profile includes:
- `Dockerfile`: image definition and Codex installation.
- `docker-compose.yml`: runtime settings, mounts, and environment variables.
- `README.md`: profile-specific usage notes.

Top-level `README.md` provides the entry point and profile selection guidance.

## Build, Test, and Development Commands
Run commands from the repository root:

```bash
export HOST_UID="$(id -u)" HOST_GID="$(id -g)" WORKSPACE_DIR="$(pwd)"
docker compose -f agent-daily-ubuntu/docker-compose.yml build
docker compose -f agent-daily-ubuntu/docker-compose.yml run --rm codex-agent-daily
```

```bash
export HOST_UID="$(id -u)" HOST_GID="$(id -g)" WORKSPACE_DIR="$(pwd)"
docker compose -f agent-daily-ubuntu-sudo/docker-compose.yml build
docker compose -f agent-daily-ubuntu-sudo/docker-compose.yml run --rm codex-agent-daily-sudo
```

Quick validation inside a container:
- `whoami`
- `pwd`
- `codex --version`

## Coding Style & Naming Conventions
- Use 2-space indentation for YAML and 4-space continuation indentation in Dockerfiles, matching existing files.
- Keep Dockerfile package lists alphabetized where practical.
- Prefer descriptive, kebab-case directory names (for example, `agent-daily-ubuntu-sudo`).
- Keep service names explicit (`codex-agent-daily`, `codex-agent-daily-sudo`).

## Testing Guidelines
There is no automated test suite yet. Validate changes with container smoke tests:
1. Build both profiles.
2. Start each container and verify user, working directory, and Codex availability.
3. For sudo profile, verify `sudo -n true`.

Document manual test results in PR descriptions.

## Commit & Pull Request Guidelines
- Use short, imperative commit subjects (example: `Add sudo profile smoke checks`).
- Keep commits focused to one profile or one cross-cutting change.
- PRs should include:
  - purpose and scope,
  - commands run for verification,
  - security-impact notes (mounts, privileges, capabilities),
  - linked issue (if applicable).

## Security & Configuration Tips
- Always set `WORKSPACE_DIR` to a minimal host path.
- Never mount home directories or secrets by default.
- Prefer the non-sudo profile unless runtime package installation is required.
