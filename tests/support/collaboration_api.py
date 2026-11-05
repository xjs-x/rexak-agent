"""Synthetic gate in a Fake Executor, no shell/target calls."""

import asyncio

from tests.support import pi_api
from tests.support.pi_executor import PiFakeExecutor


class GatedExecutor(PiFakeExecutor):
    async def execution_submit(self, envelope):
        if "stage4-wait" in envelope.get("arguments", {}).get("command", ""):
            await asyncio.sleep(2)
        return await super().execution_submit(envelope)


if __name__ == "__main__":
    pi_api.PiFakeExecutor = GatedExecutor
    pi_api.main()
