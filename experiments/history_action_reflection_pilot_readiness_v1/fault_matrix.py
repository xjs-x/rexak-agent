"""Persist actual SDK -> final transport fault receipts using mock responses only."""

from __future__ import annotations

import asyncio
import json

import httpx
import openai
from langchain_core.messages import HumanMessage

from .budget import BudgetStop, Ledger, request_scope
from .protocol import ROOT, write
from .provider_preflight import build
from .rehearsal import mock_response


async def main():
    results = []
    cases = {
        "normal": "complete",
        "provider_error": "provider_error",
        "429": "provider_error",
        "timeout": "unknown_response",
        "response_lost": "unknown_response",
        "usage_missing": "usage_missing",
        "invalid_response": "invalid_response",
        "forced_sdk_retry": "provider_error",
        "cancelled": "unknown_response",
    }
    for case, state in cases.items():
        root = ROOT / "results/faults" / case
        ledger = Ledger(root / "requests.sqlite")
        ledger.register(case)
        outgoing = []

        def handler(request, case=case, ledger=ledger, outgoing=outgoing):
            assert ledger.rows()[-1]["state"] == "possibly_sent"
            outgoing.append(json.loads(request.content))
            if case in {"429", "forced_sdk_retry", "provider_error"}:
                return httpx.Response(
                    500 if case == "provider_error" else 429,
                    json={"error": {"message": "offline injected error"}},
                )
            if case == "timeout":
                raise httpx.ReadTimeout("offline", request=request)
            if case == "response_lost":
                raise httpx.ReadError("offline sent response lost", request=request)
            if case == "cancelled":
                raise asyncio.CancelledError()
            value = mock_response(request).json()
            if case == "usage_missing":
                value.pop("usage")
            if case == "invalid_response":
                value["choices"] = []
            return httpx.Response(200, json=value)

        model, _, sync, asynchronous = build(ledger, handler)
        if case == "forced_sdk_retry":
            model.async_client = model.root_async_client.with_options(
                max_retries=1
            ).chat.completions
        exception = None
        try:
            with request_scope(case, "stable-decision-1"):
                await model.ainvoke([HumanMessage(content="offline fault injection only")])
        except (openai.APIError, BudgetStop, AttributeError, asyncio.CancelledError) as exc:
            exception = type(exc).__name__
            if isinstance(exc, AttributeError) and case != "cancelled":
                raise
        finally:
            sync.close()
            await asynchronous.aclose()
        assert len(outgoing) == 1 and ledger.rows()[0]["state"] == state
        write(
            root / "audit.json",
            {"case": case, "requests": outgoing, "receipts": ledger.rows(), "exception": exception},
        )
        results.append(
            {
                "case": case,
                "state": state,
                "physical_sends": len(outgoing),
                "exception": exception,
                "protocol": "PASS",
                "sdk_cancellation_semantics": "FAIL_EXISTING_LANGCHAIN"
                if case == "cancelled"
                else "N/A",
            }
        )
    write(
        ROOT / "results/fault-matrix.json",
        {
            "provider_requests": 0,
            "rows": results,
            "additional_cases": "pytest JUnit covers crash/resume, atomic shared budget, maintenance, token envelope, caps and missing scope",
        },
    )


if __name__ == "__main__":
    asyncio.run(main())
