"""独立计数器失败不能被折叠成零次访问。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from evals import runner


async def test_failed_counter_job_is_not_an_empty_snapshot(monkeypatch, settings):
    class Tunnel:
        def __init__(self, _settings):
            pass

        async def close(self):
            pass

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            pass

        async def submit(self, _payload):
            return SimpleNamespace(job_id=uuid4())

        async def get_job(self, _job_id):
            return SimpleNamespace(state="failed")

        async def close(self):
            pass

    monkeypatch.setattr(runner, "SshTunnel", Tunnel)
    monkeypatch.setattr(runner, "HttpExecutorClient", Client)
    with pytest.raises(RuntimeError, match="独立证据不可用"):
        await runner.fetch_target_counters(settings)
