"""Source-specific credential masking and conservative literal script linkage."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_workbench.domain.execution_provenance import safe_source, script_refs


@pytest.mark.parametrize("source,secret", [
    ("API_KEY='hidden with spaces' python3 cli.py", "hidden with spaces"),
    ('ACCESS_TOKEN="hidden" ./run.sh', "hidden"),
    ('curl --token hidden https://example.invalid', "hidden"),
    ('curl -H "Authorization: Bearer hidden"', "hidden"),
    ('-----BEGIN RSA PRIVATE KEY-----\nhidden\n-----END RSA PRIVATE KEY-----', "hidden"),
    ({"env": {"SERVICE_API_KEY": "hidden"}}, "hidden"),
    ({"argv": ["curl", "--token", "hidden"]}, "hidden"),
    ("DATABASE_URL='postgresql://alice:hidden@localhost/workbench'", "hidden"),
])
def test_masks_shell_and_structured_secrets(source, secret):
    assert secret not in json.dumps(safe_source(source))
    assert safe_source("OPENSSL_MODULES=/nonexistent ./easytl") == "OPENSSL_MODULES=/nonexistent ./easytl"


def test_literal_script_link_uses_latest_record_before_execution():
    calls = {str(n): {"sequence": n, "id": str(n), "name": "bash", "arguments": {
        "command": f"cd /tmp/et && cat > run.sh <<'EOF'\necho version-{n}\nEOF\n"}} for n in (8, 3, 1)}
    operations = {key: SimpleNamespace(operation_id=uuid4(), execution_context={}) for key in calls}
    refs = script_refs("cd /tmp/et && ./run.sh", calls, operations, 4)
    assert len(refs) == 1 and refs[0]["sequence"] == 3
    assert refs[0]["version_status"] == "historical_write_not_execution_snapshot"


def test_same_basename_in_other_directory_is_not_a_source():
    call = {"sequence": 1, "id": "one", "name": "bash", "arguments": {
        "command": "cd /tmp/other && cat > run.sh <<'EOF'\necho wrong\nEOF\n"}}
    refs = script_refs("cd /tmp/et && ./run.sh", {"one": call},
        {"one": SimpleNamespace(operation_id=uuid4(), execution_context={})}, 2)
    assert refs == [{"path": "/tmp/et/run.sh", "version_status": "execution_version_unavailable"}]
