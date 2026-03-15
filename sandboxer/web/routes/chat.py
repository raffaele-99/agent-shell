"""Chat interface — structured JSON bridge to agent CLIs."""
from __future__ import annotations

import asyncio
import json
import subprocess
import uuid

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ...core.adapters import get_adapter
from ...core.sandboxes import list_running_sandboxes


async def chat_page(request: Request) -> HTMLResponse:
    """Render the chat UI page."""
    name = request.path_params["name"]
    token = (
        request.cookies.get("sandboxer_token", "")
        or request.query_params.get("token", "")
    )

    sandboxes = await asyncio.to_thread(list_running_sandboxes)
    sandbox = next((s for s in sandboxes if s.name == name), None)
    agent_type = sandbox.agent if sandbox else ""

    return request.app.state.templates.TemplateResponse(
        request,
        "chat.html",
        {
            "sandbox_name": name,
            "ws_token": token,
            "agent_type": agent_type,
        },
    )


async def chat_websocket(websocket: WebSocket) -> None:
    """Bridge chat messages to/from the agent CLI via stream-json."""
    name = websocket.path_params["name"]

    # Look up agent type.
    sandboxes = await asyncio.to_thread(list_running_sandboxes)
    sandbox = next((s for s in sandboxes if s.name == name), None)
    if not sandbox or not sandbox.agent:
        await websocket.accept()
        await websocket.send_text(
            json.dumps({"type": "result", "is_error": True, "result": "No agent configured for this sandbox"})
        )
        await websocket.close()
        return

    adapter = get_adapter(sandbox.agent)
    if not adapter or not adapter.cli_binary:
        await websocket.accept()
        await websocket.send_text(
            json.dumps({"type": "result", "is_error": True, "result": f"Unknown agent type: {sandbox.agent}"})
        )
        await websocket.close()
        return

    await websocket.accept()

    # We'll run one-shot `claude -p` commands for each user message.
    # This is simpler and more reliable than maintaining a persistent
    # stream-json session (which requires careful stdin/stdout management).

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") != "user" or not msg.get("message"):
                continue

            user_text = msg["message"]

            # Build the command.
            cmd = [
                "docker", "sandbox", "exec", name,
                adapter.cli_binary,
                "-p",
                "--dangerously-skip-permissions",
                "--output-format", "stream-json",
                "--verbose",
                user_text,
            ]

            # Pass proxy env if available.
            try:
                from ...core.sandboxes import _proxy_env

                env = _proxy_env(name)
                if env:
                    # Prepend -e flags before the sandbox name.
                    idx = cmd.index(name)
                    for key, value in env.items():
                        cmd.insert(idx, f"{key}={value}")
                        cmd.insert(idx, "-e")
            except Exception:
                pass

            # Run the command and stream output lines.
            try:
                proc = await asyncio.to_thread(
                    lambda: subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )

                # Stream stdout lines to the client.
                async def stream_output() -> None:
                    loop = asyncio.get_running_loop()
                    while True:
                        line = await loop.run_in_executor(
                            None, proc.stdout.readline  # type: ignore[union-attr]
                        )
                        if not line:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            # Validate it's JSON before sending.
                            json.loads(line)
                            await websocket.send_text(line)
                        except json.JSONDecodeError:
                            pass

                await stream_output()

                # Wait for process to finish.
                await asyncio.to_thread(proc.wait)

                # Check for errors on stderr.
                stderr = await asyncio.to_thread(
                    lambda: proc.stderr.read()  # type: ignore[union-attr]
                )
                if proc.returncode != 0 and stderr:
                    await websocket.send_text(
                        json.dumps({
                            "type": "result",
                            "is_error": True,
                            "result": stderr.strip(),
                        })
                    )

            except Exception as exc:
                await websocket.send_text(
                    json.dumps({
                        "type": "result",
                        "is_error": True,
                        "result": f"Error: {exc}",
                    })
                )

    except WebSocketDisconnect:
        pass
    except Exception:
        pass


routes = [
    Route("/chat/{name}", chat_page),
    WebSocketRoute("/ws/chat/{name}", chat_websocket),
]
