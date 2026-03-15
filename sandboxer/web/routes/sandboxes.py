"""Sandbox CRUD routes — full pages and HTMX partials."""
from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from ...core.agents import list_agents, load_agent
from ...core.sandboxes import (
    create_sandbox,
    get_sandbox_stats,
    list_running_sandboxes,
    remove_sandbox,
    snapshot_sandbox,
    stop_sandbox,
)
from ...core.templates import list_templates, load_template


# ---------------------------------------------------------------------------
# Full pages
# ---------------------------------------------------------------------------


async def sandbox_list_page(request: Request) -> HTMLResponse:
    """Full-page sandbox list."""
    sandboxes = await asyncio.to_thread(list_running_sandboxes)
    return request.app.state.templates.TemplateResponse(
        request,
        "sandboxes/list.html",
        {"sandboxes": sandboxes},
    )


async def sandbox_create_page(request: Request) -> HTMLResponse:
    """Sandbox creation form."""
    templates, agents = await asyncio.gather(
        asyncio.to_thread(list_templates),
        asyncio.to_thread(list_agents),
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "sandboxes/create.html",
        {"templates": templates, "agents": agents, "error": None},
    )


async def sandbox_create(request: Request) -> Response:
    """Handle sandbox creation form submission."""
    form = await request.form()
    template_name = form.get("template", "").strip()
    agent_name = form.get("agent", "").strip()
    workspace = form.get("workspace", ".").strip() or "."
    name = form.get("name", "").strip() or None
    ttl = form.get("ttl", "").strip()
    idle_timeout = form.get("idle_timeout", "").strip()

    if not template_name or not agent_name:
        templates, agents = await asyncio.gather(
            asyncio.to_thread(list_templates),
            asyncio.to_thread(list_agents),
        )
        return request.app.state.templates.TemplateResponse(
            request,
            "sandboxes/create.html",
            {
                "templates": templates,
                "agents": agents,
                "error": "Template and agent are required.",
            },
        )

    try:
        template = await asyncio.to_thread(load_template, template_name)
        agent = await asyncio.to_thread(load_agent, agent_name)
        kwargs: dict = {"workspace": workspace}
        if name:
            kwargs["name"] = name
        if ttl:
            kwargs["ttl_seconds"] = int(ttl)
        if idle_timeout:
            kwargs["idle_timeout_seconds"] = int(idle_timeout)
        info = await asyncio.to_thread(
            create_sandbox, template, agent, **kwargs
        )
    except Exception as exc:
        templates, agents = await asyncio.gather(
            asyncio.to_thread(list_templates),
            asyncio.to_thread(list_agents),
        )
        return request.app.state.templates.TemplateResponse(
            request,
            "sandboxes/create.html",
            {
                "templates": templates,
                "agents": agents,
                "error": str(exc),
            },
        )

    response = Response(status_code=204)
    response.headers["HX-Redirect"] = f"/sandboxes/{info.name}"
    return response


async def sandbox_detail_page(request: Request) -> HTMLResponse:
    """Sandbox detail view."""
    name = request.path_params["name"]
    sandboxes = await asyncio.to_thread(list_running_sandboxes)
    sandbox = next((s for s in sandboxes if s.name == name), None)
    if sandbox is None:
        return HTMLResponse("Sandbox not found", status_code=404)
    return request.app.state.templates.TemplateResponse(
        request,
        "sandboxes/detail.html",
        {"sandbox": sandbox},
    )


# ---------------------------------------------------------------------------
# HTMX partials / actions
# ---------------------------------------------------------------------------


async def sandbox_list_partial(request: Request) -> HTMLResponse:
    """Return the sandbox card grid as an HTMX partial."""
    sandboxes = await asyncio.to_thread(list_running_sandboxes)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/sandbox_list.html",
        {"sandboxes": sandboxes},
    )


async def sandbox_stats_partial(request: Request) -> HTMLResponse:
    """Return sandbox stats as an HTMX partial."""
    name = request.path_params["name"]
    try:
        stats = await asyncio.to_thread(get_sandbox_stats, name)
    except Exception:
        return HTMLResponse(
            '<div class="text-sm text-slate-500">Stats unavailable</div>'
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/sandbox_stats.html",
        {"stats": stats},
    )


async def sandbox_stop(request: Request) -> HTMLResponse:
    """Stop a sandbox and return the updated list."""
    name = request.path_params["name"]
    await asyncio.to_thread(stop_sandbox, name)
    sandboxes = await asyncio.to_thread(list_running_sandboxes)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/sandbox_list.html",
        {"sandboxes": sandboxes},
    )


async def sandbox_remove(request: Request) -> HTMLResponse:
    """Remove a sandbox and return the updated list."""
    name = request.path_params["name"]
    await asyncio.to_thread(remove_sandbox, name)
    sandboxes = await asyncio.to_thread(list_running_sandboxes)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/sandbox_list.html",
        {"sandboxes": sandboxes},
    )


async def sandbox_snapshot(request: Request) -> Response:
    """Snapshot a sandbox."""
    name = request.path_params["name"]
    tag = f"{name}:snapshot"
    try:
        await asyncio.to_thread(snapshot_sandbox, name, tag)
    except Exception as exc:
        response = Response(status_code=422)
        response.headers["HX-Trigger"] = (
            '{"showToast": {"message": "Snapshot failed: '
            + str(exc).replace('"', '\\"')
            + '", "level": "error"}}'
        )
        return response
    response = Response(status_code=204)
    response.headers["HX-Trigger"] = (
        '{"showToast": {"message": "Snapshot created: ' + tag + '", "level": "success"}}'
    )
    return response


routes = [
    Route("/sandboxes/", sandbox_list_page),
    Route("/sandboxes/new", sandbox_create_page),
    Route("/sandboxes/", sandbox_create, methods=["POST"]),
    Route("/sandboxes/{name}", sandbox_detail_page),
    Route("/sandboxes/{name}/stats", sandbox_stats_partial),
    Route("/sandboxes/{name}/stop", sandbox_stop, methods=["POST"]),
    Route("/sandboxes/{name}/rm", sandbox_remove, methods=["DELETE"]),
    Route("/sandboxes/{name}/snapshot", sandbox_snapshot, methods=["POST"]),
    Route("/api/sandboxes", sandbox_list_partial),
]
