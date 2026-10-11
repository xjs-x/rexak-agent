"""自签名 HTTPS 的默认校验、单次关闭校验、身份隔离与范围检查。"""

import json
import os
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from uuid import uuid4

import pytest


def certificate_files(tmp_path):
    # Kali 的执行端运行时不需要 cryptography；演练可由 openssl 提供一次性 fixture 证书。
    supplied = os.environ.get("TLS_FIXTURE_CERT_DIR")
    if supplied:
        root = Path(supplied)
        return root / "cert.pem", root / "key.pem"
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.invalid")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def self_signed_server(tmp_path):
    cert_path, key_path = certificate_files(tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"cookie": self.headers.get("Cookie")}).encode("utf-8")
            self.send_response(302 if self.path == "/redirect" else 200)
            if self.path == "/redirect":
                self.send_header("Location", "https://outside.example.test/")
            if self.path == "/set":
                self.send_header("Set-Cookie", "fixture=one; Path=/; Secure")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def submit(client, headers, url, *, verify=None, identity="alice"):
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    payload = {"url": url, "identity_id": identity}
    if verify is not None:
        payload["verify_tls"] = verify
    response = client.post(
        "/jobs",
        headers=headers,
        json={
            "operation_id": str(uuid4()),
            "kind": "http_request",
            "request_digest": uuid4().hex,
            "scope_revision": 1,
            "scope": {"rules": [{"scheme": "https", "host": parts.hostname, "port": parts.port}]},
            "payload": payload,
        },
    )
    assert response.status_code in {200, 202}, response.text
    job_id = response.json()["job_id"]
    for _ in range(100):
        state = client.get(f"/jobs/{job_id}", headers=headers).json()
        if state["state"] not in {"received", "starting", "running"}:
            return state
        time.sleep(0.05)
    raise AssertionError("TLS fixture job did not terminate")


def test_self_signed_error_remains_real_error_and_override_is_per_request(
    client, auth_headers, self_signed_server
):
    url = self_signed_server
    checked = submit(client, auth_headers, url + "/")
    assert checked["state"] == "failed" and checked["error_code"] == "tls_certificate_error"
    assert "self-signed" in checked["error_message"]
    assert (
        client.get(
            f"/artifacts/{checked['job_id']}/response.meta.json", headers=auth_headers
        ).status_code
        == 404
    )
    allowed = submit(client, auth_headers, url + "/set", verify=False)
    assert allowed["state"] == "succeeded", allowed
    meta = client.get(
        f"/artifacts/{allowed['job_id']}/response.meta.json", headers=auth_headers
    ).json()
    assert meta["tls_verified"] is False and meta["tls_verification_enabled"] is False
    checked_again = submit(client, auth_headers, url + "/")
    assert checked_again["error_code"] == "tls_certificate_error", "单次关闭校验不能污染默认连接池"
    alice = submit(client, auth_headers, url + "/", verify=False)
    bob = submit(client, auth_headers, url + "/", verify=False, identity="bob")

    def cookie(item):
        return client.get(
            f"/artifacts/{item['job_id']}/response.body", headers=auth_headers
        ).json()["cookie"]

    assert cookie(alice) == "fixture=one" and cookie(bob) is None


def test_tls_override_does_not_disable_redirect_scope_checks(
    client, auth_headers, self_signed_server
):
    state = submit(client, auth_headers, self_signed_server + "/redirect", verify=False)
    assert state["state"] == "failed" and state["error_code"] == "scope_denied"
