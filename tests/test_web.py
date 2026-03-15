"""Tests for the sandboxer web UI module."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from sandboxer.core.models import AgentProfile, SandboxInfo, SandboxTemplate
from sandboxer.web import create_app
from sandboxer.web.auth import TokenAuthMiddleware
from sandboxer.web.terminal import SessionManager, TerminalSession

TOKEN = "test-token-abc123"

# Common mocks for dashboard (which now imports templates, agents, cleanup)
_DASHBOARD_PATCHES = {
    "sandboxer.web.routes.dashboard.list_running_sandboxes": [],
    "sandboxer.web.routes.dashboard.list_templates": [],
    "sandboxer.web.routes.dashboard.list_agents": [],
    "sandboxer.web.routes.dashboard.find_all_cleanup_candidates": {
        "orphans": [],
        "expired": [],
        "idle": [],
    },
}


def _patch_dashboard(overrides=None):
    """Return a context manager that patches all dashboard dependencies."""
    import contextlib

    vals = dict(_DASHBOARD_PATCHES)
    if overrides:
        vals.update(overrides)
    return contextlib.ExitStack()


@pytest.fixture
def app():
    return create_app(token=TOKEN)


@pytest.fixture
def client(app):
    return TestClient(app)


def _dashboard_mocks(overrides=None):
    """Helper to build patch kwargs for dashboard route."""
    vals = dict(_DASHBOARD_PATCHES)
    if overrides:
        vals.update(overrides)
    return vals


# -- Auth middleware ---------------------------------------------------------


class TestAuth:
    def _patch_dashboard(self):
        """Patch all dashboard deps for auth tests."""
        import contextlib

        return contextlib.ExitStack()

    def test_unauthenticated_returns_401(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 401

    def test_bearer_token_authenticates(self, client):
        with (
            patch(
                "sandboxer.web.routes.dashboard.list_running_sandboxes",
                return_value=[],
            ),
            patch(
                "sandboxer.web.routes.dashboard.list_templates", return_value=[]
            ),
            patch(
                "sandboxer.web.routes.dashboard.list_agents", return_value=[]
            ),
            patch(
                "sandboxer.web.routes.dashboard.find_all_cleanup_candidates",
                return_value={"orphans": [], "expired": [], "idle": []},
            ),
        ):
            resp = client.get(
                "/", headers={"Authorization": f"Bearer {TOKEN}"}
            )
        assert resp.status_code == 200

    def test_query_param_authenticates_and_sets_cookie(self, client):
        with (
            patch(
                "sandboxer.web.routes.dashboard.list_running_sandboxes",
                return_value=[],
            ),
            patch(
                "sandboxer.web.routes.dashboard.list_templates", return_value=[]
            ),
            patch(
                "sandboxer.web.routes.dashboard.list_agents", return_value=[]
            ),
            patch(
                "sandboxer.web.routes.dashboard.find_all_cleanup_candidates",
                return_value={"orphans": [], "expired": [], "idle": []},
            ),
        ):
            resp = client.get(f"/?token={TOKEN}")
        assert resp.status_code == 200
        assert "sandboxer_token" in resp.cookies

    def test_cookie_authenticates(self, client):
        client.cookies.set("sandboxer_token", TOKEN)
        with (
            patch(
                "sandboxer.web.routes.dashboard.list_running_sandboxes",
                return_value=[],
            ),
            patch(
                "sandboxer.web.routes.dashboard.list_templates", return_value=[]
            ),
            patch(
                "sandboxer.web.routes.dashboard.list_agents", return_value=[]
            ),
            patch(
                "sandboxer.web.routes.dashboard.find_all_cleanup_candidates",
                return_value={"orphans": [], "expired": [], "idle": []},
            ),
        ):
            resp = client.get("/")
        assert resp.status_code == 200

    def test_wrong_token_returns_401(self, client):
        resp = client.get("/?token=wrong")
        assert resp.status_code == 401

    def test_static_files_exempt_from_auth(self, client):
        resp = client.get("/static/style.css")
        assert resp.status_code == 200


# -- Dashboard route ---------------------------------------------------------


class TestDashboard:
    def test_dashboard_renders_sandbox_list(self, client):
        sandboxes = [
            SandboxInfo(name="sandboxer-test-1", status="running"),
            SandboxInfo(name="sandboxer-test-2", status="stopped"),
        ]
        with (
            patch(
                "sandboxer.web.routes.dashboard.list_running_sandboxes",
                return_value=sandboxes,
            ),
            patch(
                "sandboxer.web.routes.dashboard.list_templates", return_value=[]
            ),
            patch(
                "sandboxer.web.routes.dashboard.list_agents", return_value=[]
            ),
            patch(
                "sandboxer.web.routes.dashboard.find_all_cleanup_candidates",
                return_value={"orphans": [], "expired": [], "idle": []},
            ),
        ):
            resp = client.get(
                "/", headers={"Authorization": f"Bearer {TOKEN}"}
            )
        assert resp.status_code == 200
        assert "sandboxer-test-1" in resp.text
        assert "sandboxer-test-2" in resp.text

    def test_dashboard_empty(self, client):
        with (
            patch(
                "sandboxer.web.routes.dashboard.list_running_sandboxes",
                return_value=[],
            ),
            patch(
                "sandboxer.web.routes.dashboard.list_templates", return_value=[]
            ),
            patch(
                "sandboxer.web.routes.dashboard.list_agents", return_value=[]
            ),
            patch(
                "sandboxer.web.routes.dashboard.find_all_cleanup_candidates",
                return_value={"orphans": [], "expired": [], "idle": []},
            ),
        ):
            resp = client.get(
                "/", headers={"Authorization": f"Bearer {TOKEN}"}
            )
        assert resp.status_code == 200
        assert "No sandboxes running" in resp.text

    def test_dashboard_shows_counts(self, client):
        templates = [SandboxTemplate(name="t1"), SandboxTemplate(name="t2")]
        agents = [AgentProfile(name="a1", agent_type="claude")]
        with (
            patch(
                "sandboxer.web.routes.dashboard.list_running_sandboxes",
                return_value=[],
            ),
            patch(
                "sandboxer.web.routes.dashboard.list_templates",
                return_value=templates,
            ),
            patch(
                "sandboxer.web.routes.dashboard.list_agents",
                return_value=agents,
            ),
            patch(
                "sandboxer.web.routes.dashboard.find_all_cleanup_candidates",
                return_value={
                    "orphans": ["x"],
                    "expired": [],
                    "idle": ["y"],
                },
            ),
        ):
            resp = client.get(
                "/", headers={"Authorization": f"Bearer {TOKEN}"}
            )
        assert resp.status_code == 200
        # Template count = 2, Agent count = 1, Cleanup = 2
        assert ">2<" in resp.text  # template or cleanup count
        assert ">1<" in resp.text  # agent count


# -- Sandbox CRUD routes ----------------------------------------------------


class TestSandboxRoutes:
    def test_sandbox_list_partial(self, client):
        sandboxes = [SandboxInfo(name="sandboxer-foo", status="running")]
        with patch(
            "sandboxer.web.routes.sandboxes.list_running_sandboxes",
            return_value=sandboxes,
        ):
            resp = client.get(
                "/api/sandboxes",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 200
        assert "sandboxer-foo" in resp.text

    def test_sandbox_list_page(self, client):
        with patch(
            "sandboxer.web.routes.sandboxes.list_running_sandboxes",
            return_value=[],
        ):
            resp = client.get(
                "/sandboxes/",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 200
        assert "Sandboxes" in resp.text

    def test_sandbox_create_page(self, client):
        with (
            patch(
                "sandboxer.web.routes.sandboxes.list_templates",
                return_value=[],
            ),
            patch(
                "sandboxer.web.routes.sandboxes.list_agents", return_value=[]
            ),
        ):
            resp = client.get(
                "/sandboxes/new",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 200
        assert "Create Sandbox" in resp.text

    def test_sandbox_stop(self, client):
        with (
            patch(
                "sandboxer.web.routes.sandboxes.stop_sandbox"
            ) as mock_stop,
            patch(
                "sandboxer.web.routes.sandboxes.list_running_sandboxes",
                return_value=[],
            ),
        ):
            resp = client.post(
                "/sandboxes/sandboxer-test/stop",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 200
        mock_stop.assert_called_once_with("sandboxer-test")

    def test_sandbox_remove(self, client):
        with (
            patch(
                "sandboxer.web.routes.sandboxes.remove_sandbox"
            ) as mock_rm,
            patch(
                "sandboxer.web.routes.sandboxes.list_running_sandboxes",
                return_value=[],
            ),
        ):
            resp = client.request(
                "DELETE",
                "/sandboxes/sandboxer-test/rm",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 200
        mock_rm.assert_called_once_with("sandboxer-test")

    def test_sandbox_detail_page(self, client):
        sandboxes = [SandboxInfo(name="sandboxer-test", status="running")]
        with patch(
            "sandboxer.web.routes.sandboxes.list_running_sandboxes",
            return_value=sandboxes,
        ):
            resp = client.get(
                "/sandboxes/sandboxer-test",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 200
        assert "sandboxer-test" in resp.text

    def test_sandbox_detail_not_found(self, client):
        with patch(
            "sandboxer.web.routes.sandboxes.list_running_sandboxes",
            return_value=[],
        ):
            resp = client.get(
                "/sandboxes/nonexistent",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 404


# -- Template CRUD routes ---------------------------------------------------


class TestTemplateRoutes:
    def test_template_list_page(self, client):
        with patch(
            "sandboxer.web.routes.templates.list_templates", return_value=[]
        ):
            resp = client.get(
                "/templates/",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 200
        assert "Templates" in resp.text

    def test_template_create_page(self, client):
        resp = client.get(
            "/templates/new",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status_code == 200
        assert "Create Template" in resp.text

    def test_template_detail_page(self, client):
        template = SandboxTemplate(
            name="test-tpl", description="A test", packages=["git"]
        )
        with (
            patch(
                "sandboxer.web.routes.templates.load_template",
                return_value=template,
            ),
            patch(
                "sandboxer.web.routes.templates.render_dockerfile",
                return_value="FROM ubuntu",
            ),
        ):
            resp = client.get(
                "/templates/test-tpl",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 200
        assert "test-tpl" in resp.text
        assert "FROM ubuntu" in resp.text

    def test_template_detail_not_found(self, client):
        with patch(
            "sandboxer.web.routes.templates.load_template",
            side_effect=FileNotFoundError,
        ):
            resp = client.get(
                "/templates/nonexistent",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 404

    def test_template_create_submission(self, client):
        with patch(
            "sandboxer.web.routes.templates.save_template"
        ) as mock_save:
            resp = client.post(
                "/templates/",
                headers={"Authorization": f"Bearer {TOKEN}"},
                data={"name": "new-tpl", "description": "desc"},
            )
        assert resp.status_code == 204
        assert resp.headers.get("HX-Redirect") == "/templates/new-tpl"
        mock_save.assert_called_once()

    def test_template_create_missing_name(self, client):
        resp = client.post(
            "/templates/",
            headers={"Authorization": f"Bearer {TOKEN}"},
            data={"name": ""},
        )
        assert resp.status_code == 200
        assert "Name is required" in resp.text

    def test_template_delete(self, client):
        with patch(
            "sandboxer.web.routes.templates.delete_template"
        ) as mock_del:
            resp = client.request(
                "DELETE",
                "/templates/test-tpl",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 204
        mock_del.assert_called_once_with("test-tpl")


# -- Agent CRUD routes ------------------------------------------------------


class TestAgentRoutes:
    def test_agent_list_page(self, client):
        with patch(
            "sandboxer.web.routes.agents.list_agents", return_value=[]
        ):
            resp = client.get(
                "/agents/",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 200
        assert "Agents" in resp.text

    def test_agent_create_page(self, client):
        resp = client.get(
            "/agents/new",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status_code == 200
        assert "Create Agent" in resp.text

    def test_agent_detail_page(self, client):
        agent = AgentProfile(
            name="test-agent",
            agent_type="claude",
            api_key_env_var="ANTHROPIC_API_KEY",
        )
        with patch(
            "sandboxer.web.routes.agents.load_agent", return_value=agent
        ):
            resp = client.get(
                "/agents/test-agent",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 200
        assert "test-agent" in resp.text
        assert "claude" in resp.text

    def test_agent_detail_not_found(self, client):
        with patch(
            "sandboxer.web.routes.agents.load_agent",
            side_effect=FileNotFoundError,
        ):
            resp = client.get(
                "/agents/nonexistent",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 404

    def test_agent_create_submission(self, client):
        with patch("sandboxer.web.routes.agents.save_agent") as mock_save:
            resp = client.post(
                "/agents/",
                headers={"Authorization": f"Bearer {TOKEN}"},
                data={
                    "name": "new-agent",
                    "agent_type": "claude",
                    "api_key_env_var": "ANTHROPIC_API_KEY",
                },
            )
        assert resp.status_code == 204
        assert resp.headers.get("HX-Redirect") == "/agents/new-agent"
        mock_save.assert_called_once()

    def test_agent_create_missing_fields(self, client):
        resp = client.post(
            "/agents/",
            headers={"Authorization": f"Bearer {TOKEN}"},
            data={"name": "", "agent_type": ""},
        )
        assert resp.status_code == 200
        assert "required" in resp.text

    def test_agent_delete(self, client):
        with patch(
            "sandboxer.web.routes.agents.delete_agent"
        ) as mock_del:
            resp = client.request(
                "DELETE",
                "/agents/test-agent",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        assert resp.status_code == 204
        mock_del.assert_called_once_with("test-agent")


# -- Terminal page -----------------------------------------------------------


class TestTerminalPage:
    def test_terminal_page_renders(self, client):
        resp = client.get(
            "/terminal/sandboxer-test",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status_code == 200
        assert "xterm" in resp.text
        assert "sandboxer-test" in resp.text


# -- Session manager ---------------------------------------------------------


class TestSessionManager:
    def test_create_and_get(self):
        mgr = SessionManager()
        with patch.object(TerminalSession, "start"):
            session = mgr.create("id1", "sandbox1")
        assert mgr.get("id1") is session

    def test_create_idempotent(self):
        mgr = SessionManager()
        with patch.object(TerminalSession, "start"):
            s1 = mgr.create("id1", "sandbox1")
            s2 = mgr.create("id1", "sandbox1")
        assert s1 is s2

    @pytest.mark.asyncio
    async def test_close(self):
        mgr = SessionManager()
        with patch.object(TerminalSession, "start"):
            mgr.create("id1", "sandbox1")
        with patch.object(TerminalSession, "close", new_callable=AsyncMock):
            await mgr.close("id1")
        assert mgr.get("id1") is None

    @pytest.mark.asyncio
    async def test_close_all(self):
        mgr = SessionManager()
        with patch.object(TerminalSession, "start"):
            mgr.create("id1", "sandbox1")
            mgr.create("id2", "sandbox2")
        with patch.object(TerminalSession, "close", new_callable=AsyncMock):
            await mgr.close_all()
        assert mgr.get("id1") is None
        assert mgr.get("id2") is None
