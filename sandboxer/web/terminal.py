"""PTY-based terminal sessions bridged to WebSockets."""
from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class TerminalSession:
    """Spawn ``docker sandbox exec -it <name> <command>`` with a real PTY.

    The slave fd is connected to the subprocess stdin/stdout/stderr.
    The master fd is exposed for async read/write from the WebSocket handler.
    """

    def __init__(
        self,
        sandbox_name: str,
        *,
        command: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.sandbox_name = sandbox_name
        self._master_fd: int | None = None
        self._process: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._command = command or ["bash"]
        self._env = env

    def start(self) -> None:
        master, slave = pty.openpty()
        self._master_fd = master

        cmd = ["docker", "sandbox", "exec", "-it"]
        if self._env:
            for key, value in self._env.items():
                cmd.extend(["-e", f"{key}={value}"])
        cmd.append(self.sandbox_name)
        cmd.extend(self._command)

        self._process = subprocess.Popen(
            cmd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=os.setsid,
        )
        # Close slave in the parent — the child owns it now.
        os.close(slave)

    @property
    def master_fd(self) -> int:
        if self._master_fd is None:
            raise RuntimeError("Session not started")
        return self._master_fd

    async def read(self, size: int = 4096) -> bytes:
        """Read from the PTY master fd (non-blocking via asyncio)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, os.read, self.master_fd, size)

    def write(self, data: bytes) -> None:
        """Write to the PTY master fd."""
        os.write(self.master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        """Send TIOCSWINSZ to resize the PTY."""
        if self._master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
            # Signal the process group so the shell picks up the new size.
            if self._process and self._process.poll() is None:
                os.killpg(os.getpgid(self._process.pid), signal.SIGWINCH)

    async def close(self) -> None:
        """Tear down the PTY and subprocess."""
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

        if self._process is not None:
            try:
                self._process.terminate()
                await asyncio.to_thread(self._process.wait, timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None


class SessionManager:
    """Track active terminal sessions by id."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}

    def create(
        self,
        session_id: str,
        sandbox_name: str,
        *,
        command: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> TerminalSession:
        if session_id in self._sessions:
            return self._sessions[session_id]
        session = TerminalSession(sandbox_name, command=command, env=env)
        session.start()
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> TerminalSession | None:
        return self._sessions.get(session_id)

    async def close(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            await session.close()

    async def close_all(self) -> None:
        for sid in list(self._sessions):
            await self.close(sid)
