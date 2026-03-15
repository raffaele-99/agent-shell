"""Dashboard route — overview page with summary cards."""
from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from ...core.agents import list_agents
from ...core.cleanup import find_all_cleanup_candidates
from ...core.sandboxes import list_running_sandboxes
from ...core.templates import list_templates


async def dashboard(request: Request) -> HTMLResponse:
    try:
        sandboxes, templates, agents, cleanup = await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(list_running_sandboxes),
                asyncio.to_thread(list_templates),
                asyncio.to_thread(list_agents),
                asyncio.to_thread(find_all_cleanup_candidates),
            ),
            timeout=10,
        )
    except asyncio.TimeoutError:
        sandboxes, templates, agents, cleanup = [], [], [], {}
    cleanup_total = sum(len(v) for v in cleanup.values())
    return request.app.state.templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "sandboxes": sandboxes,
            "template_count": len(templates),
            "agent_count": len(agents),
            "cleanup_total": cleanup_total,
        },
    )


routes = [
    Route("/", dashboard),
]
