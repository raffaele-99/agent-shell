"""Chat interface — structured JSON bridge to agent CLIs."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
import uuid
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ...core.adapters import get_adapter
from ...core.config import config_dir
from ...core.docker import CONTAINER_WORKSPACE
from ...core.sandboxes import list_running_sandboxes


# ---------------------------------------------------------------------------
# Chat session persistence — multiple sessions per sandbox
# ---------------------------------------------------------------------------

def _sessions_dir(sandbox_name: str) -> Path:
    d = config_dir() / "chat_sessions" / sandbox_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_path(sandbox_name: str, session_id: str) -> Path:
    return _sessions_dir(sandbox_name) / f"{session_id}.json"


def _load_session(sandbox_name: str, session_id: str) -> dict:
    """Load a chat session: {id, agent_session_id, title, created_at, messages}."""
    path = _session_path(sandbox_name, session_id)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {
        "id": session_id,
        "agent_session_id": None,
        "title": "",
        "created_at": time.time(),
        "messages": [],
    }


def _save_session(sandbox_name: str, state: dict) -> None:
    _session_path(sandbox_name, state["id"]).write_text(json.dumps(state, indent=2))


def _append_message(sandbox_name: str, role: str, text: str, state: dict) -> None:
    state["messages"].append({"role": role, "text": text, "ts": time.time()})
    # Auto-title from first user message.
    if not state.get("title") and role == "user":
        state["title"] = text[:80]
    _save_session(sandbox_name, state)


def _list_sessions(sandbox_name: str) -> list[dict]:
    """List all sessions for a sandbox, newest first."""
    d = _sessions_dir(sandbox_name)
    sessions = []
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            sessions.append({
                "id": data.get("id", f.stem),
                "title": data.get("title", "Untitled"),
                "created_at": data.get("created_at", 0),
                "message_count": len(data.get("messages", [])),
            })
        except Exception:
            continue
    sessions.sort(key=lambda s: s["created_at"], reverse=True)
    return sessions


def _delete_session(sandbox_name: str, session_id: str) -> None:
    path = _session_path(sandbox_name, session_id)
    path.unlink(missing_ok=True)


def _migrate_old_session(sandbox_name: str) -> None:
    """Migrate old single-file session format to new multi-session format."""
    old_path = config_dir() / "chat_sessions" / f"{sandbox_name}.json"
    if not old_path.exists():
        return
    try:
        old_data = json.loads(old_path.read_text())
        if "messages" in old_data and old_data["messages"]:
            session_id = str(uuid.uuid4())[:8]
            new_state = {
                "id": session_id,
                "agent_session_id": old_data.get("session_id"),
                "title": "",
                "created_at": old_data["messages"][0].get("ts", time.time()),
                "messages": old_data["messages"],
            }
            # Title from first user message.
            for m in old_data["messages"]:
                if m.get("role") == "user":
                    new_state["title"] = m["text"][:80]
                    break
            _save_session(sandbox_name, new_state)
        old_path.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Agent command builders
# ---------------------------------------------------------------------------

def _build_agent_cmd(
    sandbox_name: str,
    agent_type: str,
    cli_binary: str,
    prompt: str,
    *,
    agent_session_id: str | None = None,
) -> list[str]:
    """Build the docker exec command for an agent invocation."""
    base = ["docker", "exec", "-w", CONTAINER_WORKSPACE, sandbox_name]

    if agent_type == "claude":
        cmd = [
            *base,
            cli_binary,
            "-p",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
        ]
        if agent_session_id:
            cmd.extend(["--resume", agent_session_id])
        cmd.append(prompt)
        return cmd
    elif agent_type == "codex":
        if agent_session_id:
            return [
                *base,
                cli_binary,
                "exec", "resume", agent_session_id,
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                prompt,
            ]
        return [
            *base,
            cli_binary,
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            prompt,
        ]
    elif agent_type == "gemini":
        return [*base, cli_binary, "-p", prompt]
    else:
        return [*base, cli_binary, prompt]


def _extract_session_id(agent_type: str, line_data: dict) -> str | None:
    """Try to extract a session/thread ID from a parsed JSON event."""
    if agent_type == "codex" and line_data.get("type") == "thread.started":
        return line_data.get("thread_id")
    if agent_type == "claude" and line_data.get("type") == "system":
        return line_data.get("session_id")
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def chat_page(request: Request) -> HTMLResponse:
    """Render the chat UI page."""
    name = request.path_params["name"]
    session_id = request.query_params.get("session", "")
    token = (
        request.cookies.get("sandboxer_token", "")
        or request.query_params.get("token", "")
    )

    sandboxes = await asyncio.to_thread(list_running_sandboxes)
    sandbox = next((s for s in sandboxes if s.name == name), None)
    agent_type = sandbox.agent if sandbox else ""

    # Migrate old format if needed.
    await asyncio.to_thread(_migrate_old_session, name)

    return request.app.state.templates.TemplateResponse(
        request,
        "chat.html",
        {
            "sandbox_name": name,
            "ws_token": token,
            "agent_type": agent_type,
            "session_id": session_id,
        },
    )


async def chat_sessions_list(request: Request) -> JSONResponse:
    """Return list of chat sessions for a sandbox."""
    name = request.path_params["name"]
    await asyncio.to_thread(_migrate_old_session, name)
    sessions = await asyncio.to_thread(_list_sessions, name)
    return JSONResponse(sessions)


async def chat_session_create(request: Request) -> JSONResponse:
    """Create a new chat session. Returns {id}."""
    name = request.path_params["name"]
    session_id = str(uuid.uuid4())[:8]
    state = {
        "id": session_id,
        "agent_session_id": None,
        "title": "",
        "created_at": time.time(),
        "messages": [],
    }
    await asyncio.to_thread(_save_session, name, state)
    return JSONResponse({"id": session_id})


async def chat_session_delete(request: Request) -> Response:
    """Delete a chat session."""
    name = request.path_params["name"]
    session_id = request.path_params["session_id"]
    await asyncio.to_thread(_delete_session, name, session_id)
    return Response(status_code=204)


async def chat_history(request: Request) -> JSONResponse:
    """Return the stored chat history for a specific session."""
    name = request.path_params["name"]
    session_id = request.query_params.get("session", "")
    if not session_id:
        return JSONResponse({"messages": []})
    state = await asyncio.to_thread(_load_session, name, session_id)
    return JSONResponse(state)


async def chat_websocket(websocket: WebSocket) -> None:
    """Bridge chat messages to/from the agent CLI via structured JSON."""
    name = websocket.path_params["name"]
    session_id = websocket.query_params.get("session", "")

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

    # Create session if needed.
    if not session_id:
        session_id = str(uuid.uuid4())[:8]
        await websocket.send_text(json.dumps({"type": "session.created", "session_id": session_id}))

    chat_state = await asyncio.to_thread(_load_session, name, session_id)

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
            _append_message(name, "user", user_text, chat_state)

            cmd = _build_agent_cmd(
                name, adapter.agent_type, adapter.cli_binary, user_text,
                agent_session_id=chat_state.get("agent_session_id"),
            )

            # Pass proxy env if available.
            try:
                from ...core.sandboxes import _proxy_env

                env = _proxy_env(name)
                if env:
                    idx = cmd.index(name)
                    for key, value in env.items():
                        cmd.insert(idx, f"{key}={value}")
                        cmd.insert(idx, "-e")
            except Exception:
                pass

            assistant_text_parts: list[str] = []

            try:
                proc = await asyncio.to_thread(
                    lambda: subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )

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
                        parsed = json.loads(line)

                        # Capture agent session ID from the first response.
                        sid = _extract_session_id(adapter.agent_type, parsed)
                        if sid and not chat_state.get("agent_session_id"):
                            chat_state["agent_session_id"] = sid
                            _save_session(name, chat_state)

                        # Collect assistant text for history.
                        if adapter.agent_type == "codex":
                            if parsed.get("type") == "item.completed":
                                item = parsed.get("item", {})
                                if item.get("type") == "agent_message" and item.get("text"):
                                    assistant_text_parts.append(item["text"])
                        elif adapter.agent_type == "claude":
                            if parsed.get("type") == "assistant":
                                content = (parsed.get("message") or {}).get("content", [])
                                for block in content:
                                    if block.get("type") == "text" and block.get("text"):
                                        assistant_text_parts.append(block["text"])
                            elif parsed.get("type") == "content_block_delta":
                                delta_text = (parsed.get("delta") or {}).get("text", "")
                                if delta_text:
                                    assistant_text_parts.append(delta_text)
                            elif parsed.get("type") == "result" and parsed.get("result") and not parsed.get("is_error"):
                                if not assistant_text_parts:
                                    assistant_text_parts.append(parsed["result"])

                        await websocket.send_text(line)
                    except json.JSONDecodeError:
                        assistant_text_parts.append(line)
                        await websocket.send_text(
                            json.dumps({
                                "type": "assistant",
                                "message": {
                                    "content": [{"type": "text", "text": line}],
                                },
                            })
                        )

                await asyncio.to_thread(proc.wait)

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

            # Save assistant response to history.
            if assistant_text_parts:
                full_text = "\n\n".join(assistant_text_parts)
                _append_message(name, "assistant", full_text, chat_state)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass


routes = [
    Route("/chat/{name}", chat_page),
    Route("/chat/{name}/sessions", chat_sessions_list),
    Route("/chat/{name}/sessions", chat_session_create, methods=["POST"]),
    Route("/chat/{name}/sessions/{session_id}", chat_session_delete, methods=["DELETE"]),
    Route("/chat/{name}/history", chat_history),
    WebSocketRoute("/ws/chat/{name}", chat_websocket),
]
