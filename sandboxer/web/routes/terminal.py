"""Terminal page + WebSocket handler for interactive shell sessions."""
from __future__ import annotations

import asyncio
import json
import uuid

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ...core.adapters import get_adapter
from ...core.sandboxes import list_running_sandboxes


def _get_token(request: Request) -> str:
    """Extract auth token from cookie or query param."""
    return request.cookies.get("sandboxer_token", "") or request.query_params.get("token", "")


async def terminal_page(request: Request) -> HTMLResponse:
    """Render the full-screen terminal page."""
    name = request.path_params["name"]
    token = _get_token(request)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "terminal.html",
        {"sandbox_name": name, "ws_token": token, "mode": "shell"},
    )


async def agent_terminal_page(request: Request) -> HTMLResponse:
    """Render terminal page that launches the agent CLI."""
    name = request.path_params["name"]
    token = _get_token(request)

    # Look up the agent type for this sandbox to display in the UI.
    sandboxes = await asyncio.to_thread(list_running_sandboxes)
    sandbox = next((s for s in sandboxes if s.name == name), None)
    agent_type = sandbox.agent if sandbox else ""

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "terminal.html",
        {
            "sandbox_name": name,
            "ws_token": token,
            "mode": "agent",
            "agent_type": agent_type,
        },
    )


async def terminal_websocket(websocket: WebSocket) -> None:
    """Bridge a WebSocket to a PTY-backed docker exec session."""
    name = websocket.path_params["name"]
    session_mgr = websocket.app.state.session_manager

    # Determine command: agent mode launches the agent CLI, shell mode launches bash.
    mode = websocket.query_params.get("mode", "shell")
    command: list[str] | None = None
    env: dict[str, str] | None = None

    if mode == "agent":
        # Look up sandbox to find agent type.
        sandboxes = await asyncio.to_thread(list_running_sandboxes)
        sandbox = next((s for s in sandboxes if s.name == name), None)
        if sandbox and sandbox.agent:
            adapter = get_adapter(sandbox.agent)
            if adapter and adapter.cli_binary:
                command = [adapter.cli_binary] + list(adapter.auto_args)

        # Pass proxy env vars if available.
        try:
            from ...core.sandboxes import _proxy_env

            env = _proxy_env(name) or None
        except Exception:
            pass

    await websocket.accept()

    session_id = f"{name}-{mode}-{uuid.uuid4().hex[:8]}"
    try:
        session = session_mgr.create(
            session_id, name, command=command, env=env
        )
    except Exception as exc:
        import traceback
        traceback.print_exc()
        await websocket.send_text(f"\r\nError starting session: {exc}\r\n")
        await websocket.close()
        return

    async def pty_to_ws() -> None:
        """Forward PTY output to the WebSocket."""
        try:
            while session.alive:
                try:
                    data = await asyncio.wait_for(session.read(), timeout=0.5)
                    if data:
                        await websocket.send_bytes(data)
                except asyncio.TimeoutError:
                    continue
                except OSError:
                    break
        except Exception as exc:
            import traceback
            traceback.print_exc()

    async def ws_to_pty() -> None:
        """Forward WebSocket input to the PTY."""
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break

                data = message.get("bytes") or (message.get("text", "").encode())
                if not data:
                    continue

                # Check for JSON resize messages.
                try:
                    parsed = json.loads(data)
                    if isinstance(parsed, dict) and parsed.get("type") == "resize":
                        session.resize(parsed["rows"], parsed["cols"])
                        continue
                except (json.JSONDecodeError, ValueError, KeyError):
                    pass

                session.write(data)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            import traceback
            traceback.print_exc()

    try:
        await asyncio.gather(pty_to_ws(), ws_to_pty())
    finally:
        await session_mgr.close(session_id)


routes = [
    Route("/terminal/{name}", terminal_page),
    Route("/terminal/{name}/agent", agent_terminal_page),
    WebSocketRoute("/ws/terminal/{name}", terminal_websocket),
]
