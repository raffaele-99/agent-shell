"""Template CRUD routes."""
from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from ...core.models import SandboxTemplate
from ...core.templates import (
    delete_template,
    list_templates,
    load_template,
    render_dockerfile,
    save_template,
)


async def template_list_page(request: Request) -> HTMLResponse:
    """Full-page template list."""
    templates = await asyncio.to_thread(list_templates)
    return request.app.state.templates.TemplateResponse(
        request,
        "templates/list.html",
        {"templates": templates},
    )


async def template_create_page(request: Request) -> HTMLResponse:
    """Template creation form."""
    return request.app.state.templates.TemplateResponse(
        request,
        "templates/create.html",
        {"error": None},
    )


async def template_create(request: Request) -> Response:
    """Handle template creation form submission."""
    form = await request.form()
    name = form.get("name", "").strip()

    if not name:
        return request.app.state.templates.TemplateResponse(
            request,
            "templates/create.html",
            {"error": "Name is required."},
        )

    def _split(val: str) -> list[str]:
        return [line.strip() for line in val.split("\n") if line.strip()]

    template = SandboxTemplate(
        name=name,
        description=form.get("description", "").strip(),
        base_image=form.get("base_image", "").strip()
        or "docker/sandbox-templates:latest",
        agent_type=form.get("agent_type", "").strip() or None,
        packages=_split(form.get("packages", "")),
        pip_packages=_split(form.get("pip_packages", "")),
        npm_packages=_split(form.get("npm_packages", "")),
        network=form.get("network", "bridge").strip(),
        allow_sudo="allow_sudo" in form,
        read_only_workspace="read_only_workspace" in form,
    )

    try:
        await asyncio.to_thread(save_template, template)
    except Exception as exc:
        return request.app.state.templates.TemplateResponse(
            request,
            "templates/create.html",
            {"error": str(exc)},
        )

    response = Response(status_code=204)
    response.headers["HX-Redirect"] = f"/templates/{name}"
    return response


async def template_detail_page(request: Request) -> HTMLResponse:
    """Template detail view with Dockerfile preview."""
    name = request.path_params["name"]
    try:
        template = await asyncio.to_thread(load_template, name)
    except FileNotFoundError:
        return HTMLResponse("Template not found", status_code=404)
    dockerfile = render_dockerfile(template)
    return request.app.state.templates.TemplateResponse(
        request,
        "templates/detail.html",
        {"template": template, "dockerfile": dockerfile},
    )


async def template_delete(request: Request) -> Response:
    """Delete a template."""
    name = request.path_params["name"]
    try:
        await asyncio.to_thread(delete_template, name)
    except Exception as exc:
        response = Response(status_code=422)
        response.headers["HX-Trigger"] = (
            '{"showToast": {"message": "Delete failed: '
            + str(exc).replace('"', '\\"')
            + '", "level": "error"}}'
        )
        return response
    response = Response(status_code=204)
    response.headers["HX-Redirect"] = "/templates/"
    return response


async def template_list_partial(request: Request) -> HTMLResponse:
    """HTMX partial — template card grid."""
    templates = await asyncio.to_thread(list_templates)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/template_list.html",
        {"templates": templates},
    )


routes = [
    Route("/templates/", template_list_page),
    Route("/templates/new", template_create_page),
    Route("/templates/", template_create, methods=["POST"]),
    Route("/templates/{name}", template_detail_page),
    Route("/templates/{name}", template_delete, methods=["DELETE"]),
    Route("/api/templates", template_list_partial),
]
