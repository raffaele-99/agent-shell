"""Starlette application factory."""
from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .auth import TokenAuthMiddleware
from .routes import agents, chat, dashboard, sandboxes, templates, terminal
from .terminal import SessionManager

_WEB_DIR = Path(__file__).parent


def create_app(*, token: str) -> Starlette:
    """Build the Starlette app with routes, templates, and auth."""
    routes = [
        *dashboard.routes,
        *sandboxes.routes,
        *templates.routes,
        *agents.routes,
        *chat.routes,
        *terminal.routes,
        Mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static"),
    ]

    app = Starlette(routes=routes)
    app.add_middleware(TokenAuthMiddleware, token=token)

    app.state.templates = Jinja2Templates(directory=_WEB_DIR / "templates")
    app.state.session_manager = SessionManager()

    return app
