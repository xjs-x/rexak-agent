"""Isolated cross-language callback server with the ordinary execution kernel and Fake Executor."""

import asyncio
import os
from pathlib import Path
from typing import cast

import uvicorn

from agent_workbench.api import worker_tools
from agent_workbench.api.app import create_app
from agent_workbench.execution.client import ExecutorClient
from agent_workbench.platform_compat import configure_event_loop_policy
from tests.support.pi_executor import PiFakeExecutor


def main():
    configure_event_loop_policy()
    original = worker_tools.WorkerToolService
    executor = PiFakeExecutor(Path(os.environ["PI_FAKE_ROOT"]))
    if os.environ.get("PI_REAL_EXECUTOR") != "true":
        worker_tools.WorkerToolService = lambda db, settings: original(db, settings, cast(ExecutorClient, executor))

    async def serve():
        server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=int(os.environ["WORKBENCH_API_PORT"])))
        await server.serve()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
