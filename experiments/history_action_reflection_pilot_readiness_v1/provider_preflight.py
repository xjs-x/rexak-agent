"""Production builder + actual LangChain/OpenAI SDK + guarded HTTPX mocks.

Dummy secret store and .invalid endpoint, no preferences/credentials access.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from agent_workbench.config.catalog import default_provider_profiles
from agent_workbench.models.provider import ModelBinding, build_chat_model

from .budget import AsyncGuardTransport, GuardTransport, Ledger


class DummySecrets:
    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "model:bailian"
        return "offline-placeholder-not-a-credential"

    def exists(self, secret_ref: str) -> bool:
        return secret_ref == "model:bailian"


def build(ledger: Ledger, handler: Any):
    profile = default_provider_profiles()[("bailian", "deepseek-v4.1-flash")].model_copy(
        update={
            "base_url": "https://provider.invalid/v1",
            "request_output_limit": 16384,
            "recovery_output_limit": 16384,
            "max_response_recoveries": 0,
        }
    )
    sync_client = httpx.Client(
        transport=GuardTransport(ledger, httpx.MockTransport(handler)), follow_redirects=False
    )
    async_client = httpx.AsyncClient(
        transport=AsyncGuardTransport(ledger, httpx.MockTransport(handler)), follow_redirects=False
    )
    binding = ModelBinding(
        profile=profile,
        temperature=1,
        max_retries=0,
        request_timeout_seconds=120,
        extra={
            "reasoning_effort": "high",
            "max_completion_tokens": 16384,
            "http_client": sync_client,
            "http_async_client": async_client,
        },
    )
    model = build_chat_model(binding, secret_store=DummySecrets(), streaming=False)
    return model, binding, sync_client, async_client


async def inspection(path: Path) -> dict:
    """Inspect contract locally without any invocation."""
    from langchain_core.messages import HumanMessage

    from .rehearsal import mock_response

    ledger = Ledger(path)
    model, binding, sync, asynchronous = build(ledger, mock_response)
    try:
        payload = model._get_request_payload([HumanMessage(content="offline serialization only")])
        return {
            "binding": binding.to_snapshot(),
            "payload": payload,
            "sync_transport": type(sync._transport).__name__,
            "async_transport": type(asynchronous._transport).__name__,
            "sdk_retries": model.max_retries,
            "local_compatibility": "PASS_SERIALIZATION_AND_MOCK_SDK_ONLY",
            "service_acceptance": "REQUIRES_LIVE_PROVIDER_VERIFICATION",
            "default_profile_output_limit": 393216,
            "important_override": "frozen experimental profile output=16384, recoveries=0; binding.extra alone is not persisted by ModelBinding.to_snapshot",
            "timeout": "HTTPX read=120/connect=15/write=30/pool=15, not end-to-end wall timeout",
            "full_stack": "NOT_VERIFIED; all future maintenance/compaction instances must use same guarded clients and stable scopes",
        }
    finally:
        sync.close()
        await asynchronous.aclose()
