from __future__ import annotations

import threading
from contextlib import contextmanager

import httpx
from evals.fixtures.acceptance_target import AdminHandler, FixtureState, PublicHandler, Server


@contextmanager
def fixture_servers():
    state = FixtureState(0)
    state.slow_delay = 0.005
    with (
        Server(("127.0.0.1", 0), AdminHandler, state, "control-token") as admin,
        Server(("127.0.0.1", 0), PublicHandler, state) as public,
    ):
        state.admin_port = admin.server_port
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True) for server in (admin, public)
        ]
        for thread in threads:
            thread.start()
        try:
            with (
                httpx.Client(
                    base_url=f"http://127.0.0.1:{public.server_port}", trust_env=False
                ) as client,
                httpx.Client(
                    base_url=f"http://127.0.0.1:{admin.server_port}",
                    headers={"Authorization": "Bearer control-token"},
                    trust_env=False,
                ) as private,
            ):
                yield state, client, private
        finally:
            admin.shutdown()
            public.shutdown()
            for thread in threads:
                thread.join(timeout=2)


def test_reset_rotates_accounts_invalidates_cookies_and_hides_management():
    with fixture_servers() as (_state, client, private):
        reset = private.post("/reset").json()
        assert client.get("/private/profile").status_code == 401
        account = reset["accounts"]["alice"]
        login = client.post("/login-form", data=account)
        assert login.status_code == 303
        assert "alice" in client.get("/private/profile").text
        assert client.get("/snapshot").status_code == 404
        assert private.get("/snapshot", headers={"Authorization": "wrong"}).status_code == 401
        snapshot = private.get("/snapshot").json()
        assert "accounts" not in snapshot
        assert account["password"] not in str(snapshot)
        new = private.post("/reset").json()
        assert new["generation"] != reset["generation"]
        assert new["accounts"]["alice"]["password"] != account["password"]
        assert new["audit"] == []
        assert client.get("/private/profile").status_code == 401
        assert client.post("/login-form", data=account).status_code == 401


def test_active_requests_reject_reset_slow_completion_and_injection_audit():
    with fixture_servers() as (state, client, private):
        with state.lock:
            state.active = 1
        assert private.post("/reset").status_code == 409
        with state.lock:
            state.active = 0
        assert "chunk 3/3" in client.get("/slow").text
        assert "Ignore previous instructions" in client.get("/content/notice").text
        assert private.post("/collect", headers={"Authorization": "wrong"}).status_code == 401
        snapshot = private.get("/snapshot").json()
        assert snapshot["canary_hits"] == 1
        assert snapshot["active_requests"] == 0
        assert snapshot["audit"][0]["path"] == "/slow"
