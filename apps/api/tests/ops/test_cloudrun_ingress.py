"""Cloud Run 用 Caddy の隔離試験。Cloud Run や IAP の動作は証明しない。"""

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
CONFIG = ROOT / "infra/cloudrun/Caddyfile"
VM_CONFIG = ROOT / "infra/proxy/Caddyfile"
LIMIT = 12_000_000  # Caddy の 12MB は decimal MB


class EchoHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        remaining = int(self.headers.get("Content-Length", "0"))
        while remaining:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)
        self.respond()

    def respond(self):
        data = json.dumps({"path": self.path, "headers": dict(self.headers)}).encode()
        self.send_response(200)
        self.send_header("Server", "test-upstream")
        self.send_header("X-Powered-By", "test-upstream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def caddy_binary():
    binary = os.environ.get("CADDY_BINARY") or shutil.which("caddy")
    if not binary:
        pytest.skip("BLOCKED: Caddy 2.10 is unavailable")
    version = subprocess.run([binary, "version"], capture_output=True, text=True, check=True).stdout
    assert re.search(r"\bv2\.10\.\d+\b", version), version
    return binary


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def ingress(caddy_binary, tmp_path_factory):
    try:
        upstream = ThreadingHTTPServer(("127.0.0.1", 3000), EchoHandler)
    except OSError as exc:
        pytest.skip(f"BLOCKED: test upstream port 3000 unavailable: {exc}")
    upstream.daemon_threads = True
    worker = threading.Thread(target=upstream.serve_forever, daemon=True)
    worker.start()
    port = free_port()
    env = {**os.environ, "PORT": str(port)}
    log_path = tmp_path_factory.mktemp("caddy") / "caddy.log"
    with log_path.open("w+") as log:
        process = subprocess.Popen(
            [caddy_binary, "run", "--config", str(CONFIG), "--adapter", "caddyfile"],
            env=env, stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    log.flush()
                    pytest.fail(f"Caddy exited during startup: {log_path.read_text()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        pytest.fail("Caddy startup timed out")
                    time.sleep(0.05)
            yield port, upstream, log_path
        finally:
            process.terminate()
            process.wait(timeout=10)
            upstream.shutdown()
            upstream.server_close()
            worker.join(timeout=10)


def request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_syntax(caddy_binary):
    env = {**os.environ, "PORT": "18080"}
    result = subprocess.run([caddy_binary, "validate", "--config", str(CONFIG), "--adapter", "caddyfile"],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    env.pop("PORT")
    missing = subprocess.run([caddy_binary, "validate", "--config", str(CONFIG), "--adapter", "caddyfile"],
                             capture_output=True, text=True, env=env)
    assert missing.returncode != 0


def test_cloudrun_configuration_and_vm_untouched():
    config = CONFIG.read_text()
    assert "http://:{$PORT:missing}" in config
    assert "bind 0.0.0.0" in config
    assert "reverse_proxy 127.0.0.1:3000" in config
    assert "max_size 12MB" in config
    assert not re.search(r"^\s*log(?:\s|\{|$)", config, re.M)
    assert "trusted_proxies" not in config
    assert "reverse_proxy 127.0.0.1:8000" not in config
    committed = subprocess.run(["git", "show", "HEAD:infra/proxy/Caddyfile"], cwd=ROOT,
                               capture_output=True, check=True).stdout
    assert VM_CONFIG.read_bytes() == committed


def test_headers_and_untrusted_forwarding(ingress):
    port, _, _ = ingress
    status, headers, body = request(port, "GET", "/api/v1/invites/example-test-token", headers={
        "X-Forwarded-For": "198.51.100.11, 203.0.113.22",
        "X-Real-IP": "198.51.100.33",
        "Forwarded": "for=198.51.100.44",
    })
    assert status == 200
    assert json.loads(body)["path"] == "/api/v1/invites/example-test-token"
    upstream_headers = {k.lower(): v for k, v in json.loads(body)["headers"].items()}
    assert "x-forwarded-for" not in upstream_headers
    assert "x-real-ip" not in upstream_headers
    assert "forwarded" not in upstream_headers
    status2, _, body2 = request(port, "GET", "/api/v1/identity/me", headers={
        "X-Forwarded-For": "203.0.113.88", "X-Real-IP": "203.0.113.99",
    })
    assert status2 == 200
    headers2 = {k.lower(): v for k, v in json.loads(body2)["headers"].items()}
    assert "x-forwarded-for" not in headers2
    assert "x-real-ip" not in headers2
    h = {k.lower(): v for k, v in headers.items()}
    assert h["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert h["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert "server" not in h
    assert "x-powered-by" not in h


@pytest.mark.parametrize("size,expected", [(LIMIT, 200), (LIMIT + 1, 413)])
def test_request_size_boundary(ingress, size, expected):
    status, _, _ = request(ingress[0], "POST", "/boundary", body=b"x" * size)
    assert status == expected


def test_access_log_is_disabled(ingress):
    port, _, path = ingress
    token = "example-local-token-never-real"
    assert request(port, "GET", f"/join/{token}")[0] == 200
    assert token not in path.read_text()


def test_upstream_failure_is_not_success(ingress):
    port, upstream, _ = ingress
    upstream.shutdown()
    upstream.server_close()
    status, _, _ = request(port, "GET", "/unavailable")
    assert status == 502
