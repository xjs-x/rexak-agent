import pytest
from pydantic import ValidationError

from agent_workbench.config.settings import Settings
from agent_workbench.contracts.delegation import ExecutionMode
from agent_workbench.contracts.identity import SessionCreate
from agent_workbench.execution.a2a_worker import A2AWorkerClient, WorkerUnavailable
from agent_workbench.tools.registry import ToolSelection, build_tools


def test_execution_mode_defaults_and_strict_input():
    value = SessionCreate(title="fixture", initial_objective="local only")
    assert value.execution_mode is ExecutionMode.STANDARD
    assert value.model_copy(update={"execution_mode": ExecutionMode.ULTRA}).execution_mode == "ultra"
    with pytest.raises(ValidationError):
        SessionCreate.model_validate({"title": "x", "initial_objective": "x", "execution_mode": "ULTRA"})


def test_standard_toolset_does_not_register_delegation():
    assert not any(t.name == "delegate_task" for t in build_tools(ToolSelection()))
    names = {t.name for t in build_tools(ToolSelection(ultra=True))}
    assert {"delegate_task", "get_delegated_task", "wait_delegated_task", "cancel_delegated_task"} <= names


@pytest.mark.parametrize("url", ["http://example.com", "https://127.0.0.1", "http://127.0.0.1/evil", "http://a:b@127.0.0.1", "http://127.0.0.1?x=1"])
def test_model_cannot_redirect_worker_or_credentials(url):
    with pytest.raises(WorkerUnavailable):
        A2AWorkerClient(Settings(ultra_worker_url=url, ultra_worker_token="a" * 32))


def test_unconfigured_ultra_is_explicitly_unavailable(tmp_path):
    with pytest.raises(WorkerUnavailable):
        A2AWorkerClient(Settings(ultra_worker_token="", secret_store_path=tmp_path / "secrets.json"))


def test_worker_service_credentials_are_separate_and_not_overwritten(tmp_path):
    from agent_workbench.config.ultra_worker import read_worker_config, write_worker_config

    config = Settings(secret_store_path=tmp_path / "secrets.json")
    payload = {"token": "mock-service-token", "database_url": "mock-worker-only"}
    write_worker_config(config, payload)
    assert read_worker_config(config) == payload
    with pytest.raises(FileExistsError):
        write_worker_config(config, {"token": "overwrite"})
