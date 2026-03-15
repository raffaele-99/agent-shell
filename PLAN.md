# Sandboxer — Refactor Plan

## Context

`agent-shell` currently generates ephemeral Docker containers from scratch: it
dynamically writes Dockerfiles, builds images, and runs hardened containers with
manual `docker run` flags. Docker Desktop 4.58+ ships **Docker Sandboxes** — a
first-party feature that provides isolated microVMs with private Docker daemons,
workspace syncing, persistence, and custom Dockerfile template support. It
handles most of what `agent-shell` does at the infrastructure level, but better
(microVM isolation vs plain containers, built-in persistence, `docker sandbox`
CLI).

The proposal is to **refactor `agent-shell` into `sandboxer`** — a sandbox
manager that sits on top of `docker sandbox`, providing a core library, TUI, and
web UI for managing sandbox templates, agent profiles, and running sandboxes.

## Feasibility Assessment

### What Docker Sandbox gives us for free

| Capability                    | Docker Sandbox support            |
| ----------------------------- | --------------------------------- |
| Isolated execution env        | microVMs with private daemons     |
| Workspace mounting (rw / ro)  | `docker sandbox run [PATH]`, `:ro`|
| Custom OS / packages          | Dockerfile templates extending base image |
| Persistence across sessions   | Sandboxes persist until `rm`      |
| Shell access                  | `docker sandbox exec -it NAME bash` |
| List / stop / remove          | `docker sandbox ls`, `rm`         |
| Save running env as template  | `docker sandbox save`             |
| Registry-based template share | Push/pull to any OCI registry     |
| Agent binary + tooling        | Included in base template image   |

### What we need to build

| Capability               | Complexity | Notes                                |
| ------------------------ | ---------- | ------------------------------------ |
| Core library (Python)    | Medium     | Wraps `docker sandbox` CLI, manages config |
| Template management      | Low        | CRUD for Dockerfile templates on disk |
| Agent profiles           | Low        | YAML/JSON store for API keys + agent config |
| TUI (Textual)            | Medium     | Dashboard, template editor, sandbox list |
| Web UI                   | Medium     | FastAPI + lightweight frontend       |
| Shell-in-browser         | Medium     | xterm.js + WebSocket to `docker sandbox exec` |

### Constraints / risks

- **Docker Sandbox is Docker Desktop only** — no Linux server / CI support
  (yet). This limits headless use cases. Monitor for engine-level support.
- **Templates must extend `docker/sandbox-templates:*` base images** — we can't
  use arbitrary base images (e.g., `alpine:3.19`). The base includes Ubuntu,
  Node, Python, Go, Git, and the agent binary.
- **Agent support is currently Claude-only** in the official base template. For
  Codex/Gemini we'd need to install their binaries in a custom template layer.
- **No programmatic API** — we're wrapping the `docker sandbox` CLI, which means
  parsing text output. If Docker ships a Go SDK or REST API later, we migrate.

**Verdict: fully doable.** The core value prop (template management + agent
profiles + unified UI) is all application-level logic. Docker sandbox handles
the hard infra.

## Patterns Borrowed from nanoclaw

Several design patterns from [nanoclaw](https://github.com/qwibitai/nanoclaw)
are worth adopting:

### Credential proxy (high priority)

Instead of mounting API keys or passing env vars into sandboxes, run a
lightweight host-side HTTP proxy that transparently injects credentials into
outbound API requests. Sandboxes only know the proxy URL — they never see real
keys. Benefits:

- Keys never touch the sandbox filesystem or environment
- Credential rotation requires zero sandbox restarts
- Revocation is instant (stop the proxy)
- Works identically across all agent types

Implementation: `core/credential_proxy.py` — a small `asyncio` HTTP proxy
(or `mitmproxy` library) that intercepts requests to known AI API endpoints
and injects the appropriate `Authorization` header from the agent profile.

### Mount allowlist (high priority)

Store an explicit allowlist of host paths that sandboxes may access at
`~/.config/sandboxer/mount-allowlist.json` — **outside the project root** so
sandboxes cannot modify their own access rules. Block dangerous patterns by
default (`.ssh`, `.aws`, `.docker`, credential files, etc.). Validate host
path existence and resolve symlinks before mounting.

### Sandbox name prefixing + orphan cleanup (medium priority)

Prefix all sandbox names with `sandboxer-` so we can reliably identify ours.
On startup, sweep for orphaned sandboxes from crashed sessions and offer to
clean them up. This prevents resource leaks from ungraceful shutdowns.

### Platform-aware networking (medium priority)

Abstract macOS vs Linux vs WSL networking differences in the Docker wrapper
layer. macOS Docker Desktop routes `host.docker.internal` to loopback;
Linux uses the `docker0` bridge IP; WSL behaves like macOS. The credential
proxy bind address depends on this.

## Architecture

```
┌─────────────────────────────────────────────┐
│                   Users                     │
│         TUI (Textual)  /  Web UI            │
└──────────────┬──────────────┬───────────────┘
               │              │
               ▼              ▼
┌─────────────────────────────────────────────┐
│              sandboxer (core library)       │
│                                             │
│  ┌─────────────┐ ┌──────────┐ ┌──────────┐ │
│  │  Templates   │ │  Agents  │ │ Sandbox  │ │
│  │  Manager     │ │  Profiles│ │ Manager  │ │
│  └─────────────┘ └──────────┘ └──────────┘ │
│                                             │
│  ┌──────────────────┐ ┌──────────────────┐  │
│  │  Docker Sandbox  │ │   Credential     │  │
│  │  CLI Wrapper     │ │   Proxy          │  │
│  └──────────────────┘ └──────────────────┘  │
│                                             │
│  ┌──────────────────┐ ┌──────────────────┐  │
│  │  Mount           │ │   Orphan         │  │
│  │  Allowlist       │ │   Cleanup        │  │
│  └──────────────────┘ └──────────────────┘  │
└──────────────┬──────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────┐
│         docker sandbox (Docker Desktop)     │
│              microVM runtime                │
└─────────────────────────────────────────────┘
```

## Core Library Design

### Module layout

```
sandboxer/
├── __init__.py
├── core/
│   ├── __init__.py
│   ├── docker.py              # docker sandbox CLI wrapper (subprocess)
│   ├── templates.py           # template CRUD, Dockerfile generation
│   ├── agents.py              # agent profile management
│   ├── sandboxes.py           # sandbox lifecycle (create/start/stop/rm/exec)
│   ├── config.py              # global config, paths, defaults
│   ├── credential_proxy.py    # host-side HTTP proxy for API key injection
│   ├── mount_allowlist.py     # allowlist-based mount path validation
│   └── cleanup.py             # orphan sandbox detection + cleanup
├── tui/
│   ├── __init__.py
│   ├── app.py             # Textual app entry point
│   ├── screens/
│   │   ├── dashboard.py   # active sandboxes overview
│   │   ├── templates.py   # template list + editor
│   │   ├── agents.py      # agent profile management
│   │   └── sandbox.py     # single sandbox detail + shell
│   └── widgets/
│       └── terminal.py    # embedded terminal widget
├── web/
│   ├── __init__.py
│   ├── app.py             # FastAPI app
│   ├── routes/
│   │   ├── templates.py
│   │   ├── agents.py
│   │   └── sandboxes.py
│   └── frontend/          # static SPA (or server-rendered)
└── cli.py                 # typer CLI (thin layer over core)
```

### Key data models

```python
@dataclass
class SandboxTemplate:
    name: str                       # e.g. "python-dev"
    description: str
    base_image: str                 # e.g. "docker/sandbox-templates:claude-code"
    packages: list[str]             # apt-get packages
    pip_packages: list[str]
    npm_packages: list[str]
    custom_dockerfile_lines: list[str]  # escape hatch for arbitrary RUN lines
    allow_sudo: bool
    network: str                    # "none" | "bridge" | "host"
    read_only_workspace: bool

@dataclass
class AgentProfile:
    name: str                       # e.g. "claude-work"
    agent_type: str                 # "claude" | "codex" | "gemini"
    api_key: str                    # stored encrypted or via keyring
    auth_dir: Path | None           # optional auth directory to mount
    default_args: list[str]         # extra CLI args for the agent

@dataclass
class Sandbox:
    name: str
    template: str                   # template name used
    agent: str                      # agent profile name used
    workspace: Path
    status: str                     # "running" | "stopped" | "exited"
    created_at: datetime
```

### Docker CLI wrapper (core/docker.py)

Thin subprocess wrapper. Each method maps to a `docker sandbox` subcommand:

| Method                              | Wraps                                        |
| ----------------------------------- | -------------------------------------------- |
| `build_template(dockerfile, tag)`   | `docker build -t TAG .`                      |
| `create(template, workspace, name)` | `docker sandbox run -t TAG [PATH] --name N`  |
| `list_sandboxes()`                  | `docker sandbox ls` (parse output)            |
| `exec_shell(name)`                  | `docker sandbox exec -it NAME bash`           |
| `remove(name)`                      | `docker sandbox rm NAME`                      |
| `save_as_template(name, tag)`       | `docker sandbox save NAME TAG`                |
| `stop(name)`                        | `docker sandbox stop NAME`                    |

### Config / storage paths

```
~/.config/sandboxer/
├── config.yml              # global defaults
├── mount-allowlist.json    # allowed host paths (outside project root!)
├── templates/              # one YAML + Dockerfile per template
│   ├── python-dev.yml
│   ├── python-dev.Dockerfile
│   ├── node-full.yml
│   └── node-full.Dockerfile
└── agents/                 # one YAML per agent profile (keys via keyring)
    ├── claude-work.yml
    └── codex-personal.yml
```

## Implementation Phases

### Phase 1 — Core library + CLI

- [ ] Scaffold new package structure (`sandboxer/`)
- [ ] Implement `core/docker.py` — subprocess wrapper for `docker sandbox`
- [ ] Implement `core/templates.py` — template CRUD (YAML + Dockerfile on disk)
- [ ] Implement `core/agents.py` — agent profile CRUD (YAML on disk, keyring for secrets)
- [ ] Implement `core/sandboxes.py` — sandbox lifecycle orchestration
- [ ] Implement `core/credential_proxy.py` — host-side HTTP proxy for credential injection
- [ ] Implement `core/mount_allowlist.py` — allowlist validation with blocked patterns
- [ ] Implement `core/cleanup.py` — orphan sandbox detection on startup
- [ ] Implement `cli.py` — typer CLI exposing all core operations
- [ ] Tests for core modules (mock subprocess calls)
- [ ] Migrate README, update pyproject.toml (rename to `sandboxer`)

### Phase 2 — TUI (Textual)

- [ ] Dashboard screen — list active sandboxes with status, quick actions
- [ ] Templates screen — list, create, edit, delete templates
- [ ] Agents screen — manage agent profiles
- [ ] Sandbox detail screen — logs, shell access, stop/remove
- [ ] Embedded terminal widget for `docker sandbox exec`

### Phase 3 — Web UI

- [ ] FastAPI backend exposing core library as REST API
- [ ] WebSocket endpoint for interactive shell (xterm.js ↔ `docker sandbox exec`)
- [ ] Frontend: dashboard, template editor, agent manager, sandbox viewer
- [ ] Optional: auth layer if running on shared machine

### Phase 4 — Polish

- [ ] Template marketplace / sharing (push/pull from registries)
- [ ] Sandbox snapshots (save running sandbox as new template)
- [ ] Multi-agent support in single sandbox
- [ ] Resource monitoring (CPU/memory per sandbox)
- [ ] Auto-cleanup policies (TTL on idle sandboxes)

## Migration from agent-shell

The existing `agent-shell` code provides useful reference for:
- Agent adapter pattern (Codex/Claude install snippets, required packages)
- Security defaults (capability dropping, resource limits) — though Docker
  sandbox handles most of this at the microVM level now
- Config wizard UX

The refactor is a **rewrite**, not an incremental migration. The subprocess
wrapper approach is fundamentally different from the current direct
`docker build` + `docker run` approach. Old code stays on `main` as reference
until `sandboxer` is feature-complete.

## Dependencies

| Package         | Purpose                    |
| --------------- | -------------------------- |
| typer            | CLI framework (keep)      |
| textual          | TUI framework             |
| fastapi          | Web API                   |
| uvicorn          | ASGI server               |
| pyyaml           | Config/template storage   |
| keyring          | Secure API key storage    |
| pydantic         | Data models + validation  |

## Open Questions

1. **Docker sandbox JSON output?** — Need to check if `docker sandbox ls`
   supports `--format json` or if we parse table output.
2. **Programmatic exec?** — Can we get a PTY from `docker sandbox exec` piped
   through a WebSocket, or do we need to use `docker exec` on the inner
   container directly?
3. **Non-Claude agents** — The base template ships Claude. For Codex/Gemini, do
   we layer on top, or create separate base templates?
4. **Docker Desktop requirement** — Is this acceptable, or do we need a fallback
   path for Linux servers / CI?
