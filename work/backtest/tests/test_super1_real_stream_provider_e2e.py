from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import shutil
import sqlite3
import ssl
import subprocess
import sys
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from backtest.dukascopy_acquisition import AcquisitionCoordinator, ProviderResponseError, RateLimitController


ROOT = Path(__file__).resolve().parents[1]
HOST = "datafeed.dukascopy.com"
PROVIDER_URL = f"https://{HOST}/real-stream-test.bin"
MAX_BODY_BYTES = 16 * 1024 * 1024


DNS_PRELOAD = r'''
const dns = require("node:dns");
const originalLookup = dns.lookup.bind(dns);
dns.lookup = (hostname, options, callback) => {
  if (String(hostname).toLowerCase() !== "datafeed.dukascopy.com") return originalLookup(hostname, options, callback);
  if (typeof options === "function") { callback = options; options = {}; }
  if (options && options.all) return callback(null, [{ address: "127.0.0.1", family: 4 }]);
  return callback(null, "127.0.0.1", 4);
};
const originalPromisesLookup = dns.promises.lookup.bind(dns.promises);
dns.promises.lookup = async (hostname, options) => {
  if (String(hostname).toLowerCase() === "datafeed.dukascopy.com") {
    return options && options.all ? [{ address: "127.0.0.1", family: 4 }] : { address: "127.0.0.1", family: 4 };
  }
  return originalPromisesLookup(hostname, options);
};
const readerPrototype = globalThis.ReadableStreamDefaultReader?.prototype;
if (readerPrototype && process.env.CANCEL_MARKER) {
  const originalCancel = readerPrototype.cancel;
  readerPrototype.cancel = function(reason) {
    require("node:fs").writeFileSync(process.env.CANCEL_MARKER, "cancelled\n");
    return originalCancel.call(this, reason);
  };
}
'''


NODE_HARNESS = r'''
import { fetchWithLimits } from "__NODE_SCRIPT__";
import { promises as fs } from "node:fs";

const [url, stageRoot, timeoutMs] = process.argv.slice(1);
await fs.mkdir(stageRoot, { recursive: true });
const meta = [];
try {
  const body = await fetchWithLimits(url, stageRoot, meta, Number(timeoutMs));
  const files = await fs.readdir(stageRoot);
  process.stdout.write(JSON.stringify({
    outcome: "success",
    body_b64: Buffer.from(body).toString("base64"),
    meta_count: meta.length,
    stage_files: files,
  }));
} catch (error) {
  const files = await fs.readdir(stageRoot);
  process.stdout.write(JSON.stringify({
    outcome: "failure",
    error_name: String(error?.name || "Error"),
    error_message: String(error?.message || error),
    meta_count: meta.length,
    stage_files: files,
  }));
}
'''


def _write_test_certificate(root: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now.replace(microsecond=0))
        .not_valid_after(now.replace(microsecond=0).replace(year=now.year + 1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path = root / "stream-test-key.pem"
    cert_path = root / "stream-test-cert.pem"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


class _RealStreamServer:
    def __init__(self, root: Path, scenario: str) -> None:
        self.scenario = scenario
        self.state: dict[str, object] = {"requests": 0, "headers_sent": False, "chunks_sent": 0, "disconnects": 0, "server_closed": False}
        self.lock = threading.Lock()
        cert_path, key_path = _write_test_certificate(root)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def handle(self) -> None:
                try:
                    super().handle()
                except (ConnectionAbortedError, ConnectionResetError, OSError):
                    with owner.lock:
                        owner.state["disconnects"] = int(owner.state["disconnects"]) + 1

            def do_GET(self) -> None:
                if self.path != f"/{owner.scenario}.bin":
                    self.send_error(404)
                    return
                with owner.lock:
                    owner.state["requests"] = int(owner.state["requests"]) + 1
                if owner.scenario == "fragmented":
                    chunks = [b"fragment-", b"stream-", b"body"]
                    declared = sum(map(len, chunks))
                elif owner.scenario == "delayed":
                    chunks = [b"delayed-body"]
                    declared = len(chunks[0])
                elif owner.scenario in {"half-body", "connection-drop"}:
                    body = b"0123456789abcdef0123456789abcdef"
                    chunks = [body[: len(body) // 2]] if owner.scenario == "half-body" else [body[: len(body) // 3]]
                    declared = len(body)
                elif owner.scenario == "oversized":
                    chunks = [b"x"]
                    declared = MAX_BODY_BYTES + 1
                else:
                    raise AssertionError(owner.scenario)
                self.send_response(200)
                self.send_header("Content-Length", str(declared))
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                with owner.lock:
                    owner.state["headers_sent"] = True
                if owner.scenario == "delayed":
                    time.sleep(1.0)
                try:
                    for index, chunk in enumerate(chunks):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        with owner.lock:
                            owner.state["chunks_sent"] = int(owner.state["chunks_sent"]) + 1
                        if owner.scenario == "fragmented" and index + 1 < len(chunks):
                            time.sleep(0.02)
                    if owner.scenario in {"half-body", "connection-drop"}:
                        with owner.lock:
                            owner.state["server_closed"] = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                    elif owner.scenario == "oversized":
                        time.sleep(0.1)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    with owner.lock:
                        owner.state["disconnects"] = int(owner.state["disconnects"]) + 1

        server = ThreadingHTTPServer(("127.0.0.1", 443), Handler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self.server = server
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "_RealStreamServer":
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)


def _run_real_fetch(url: str, stage_root: Path, cert_path: Path, marker: Path, timeout_ms: int) -> dict[str, object]:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the real stream acceptance"
    preload = stage_root.parent / "dns-and-reader-instrumentation.cjs"
    preload.write_text(DNS_PRELOAD, encoding="utf-8", newline="\n")
    harness = NODE_HARNESS.replace("__NODE_SCRIPT__", (ROOT / "tools/dukascopy-downloader/acquire_v5.mjs").as_uri())
    completed = subprocess.run(
        [node, "--require", str(preload), "--input-type=module", "-e", harness, url, str(stage_root), str(timeout_ms)],
        cwd=ROOT,
        env={**__import__("os").environ, "NODE_EXTRA_CA_CERTS": str(cert_path), "CANCEL_MARKER": str(marker)},
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip(), completed.stderr
    result = json.loads(completed.stdout.strip())
    result["returncode"] = completed.returncode
    return result


@pytest.mark.parametrize(
    ("scenario", "timeout_ms", "expected"),
    [
        ("fragmented", 1000, "success"),
        ("delayed", 500, "timeout"),
        ("half-body", 1000, "incomplete"),
        ("connection-drop", 1000, "incomplete"),
        ("oversized", 1000, "oversized"),
    ],
)
def test_real_socket_stream_is_bound_to_coordinator_and_never_commits_partial_body(tmp_path: Path, scenario: str, timeout_ms: int, expected: str) -> None:
    scenario_root = tmp_path / scenario
    scenario_root.mkdir()
    stage_root = scenario_root / "stage"
    marker = scenario_root / "reader-cancelled.txt"
    observed: dict[str, object] = {}

    with _RealStreamServer(scenario_root, scenario) as stream:
        cert_path = scenario_root / "stream-test-cert.pem"

        def provider(item: dict[str, object]) -> dict[str, object]:
            result = _run_real_fetch(f"https://{HOST}/{scenario}.bin", stage_root, cert_path, marker, timeout_ms)
            observed["node"] = result
            if expected == "success":
                assert result["outcome"] == "success"
                body = base64.b64decode(str(result["body_b64"]))
                body_sha = sha256(body).hexdigest()
                return {
                    "status": 200,
                    "url": str(item["url"]),
                    "headers": {"content-length": str(len(body)), "content-type": "application/octet-stream"},
                    "body": body,
                    "body_sha256": body_sha,
                    "body_byte_count": len(body),
                    "provenance": {"provider": "Dukascopy", "status": 200, "url_sha256": sha256(str(item["url"]).encode()).hexdigest(), "body_sha256": body_sha, "body_byte_count": len(body)},
                }
            assert result["outcome"] == "failure"
            if expected == "timeout":
                raise TimeoutError(str(result["error_message"]))
            if expected == "oversized":
                raise ValueError(str(result["error_message"]))
            raise ConnectionError(str(result["error_message"]))

        controller = RateLimitController(scenario_root / "state.json", rng=lambda: 0)
        coordinator = AcquisitionCoordinator(
            fixture_root=None,
            cas_root=scenario_root / "cas",
            ledger_path=scenario_root / "ledger.sqlite3",
            provider=provider,
            mode="provider",
            rate_controller=controller,
        )
        try:
            if expected == "success":
                result = coordinator.run(run_id="e" * 64, source_commit="a" * 40, source_tree_sha256="b" * 40, artifacts=[{"artifact_id": "stream-artifact", "url": PROVIDER_URL}])
                assert result[0]["state"] == "COMMITTED"
            else:
                with pytest.raises(ProviderResponseError, match="provider callback failed"):
                    coordinator.run(run_id="e" * 64, source_commit="a" * 40, source_tree_sha256="b" * 40, artifacts=[{"artifact_id": "stream-artifact", "url": PROVIDER_URL}])
        finally:
            coordinator.close()

    with sqlite3.connect(scenario_root / "ledger.sqlite3") as db:
        ledger_state = db.execute("SELECT state FROM acquisition_artifacts WHERE artifact_id='stream-artifact'").fetchone()[0]
    cas_files = list((scenario_root / "cas").rglob("*.bi5"))
    cas_parts = list((scenario_root / "cas").rglob("*.part.*"))
    stage_files = [path for path in stage_root.rglob("*") if path.is_file()]
    node_result = observed["node"]
    assert isinstance(node_result, dict)
    assert int(stream.state["requests"]) == 1
    if expected == "success":
        assert ledger_state == "COMMITTED" and len(cas_files) == 1 and not cas_parts
        assert node_result["meta_count"] == 1 and len(stage_files) == 1
        assert int(stream.state["chunks_sent"]) == 3
    else:
        assert ledger_state == "IN_PROGRESS" and not cas_files and not cas_parts and not stage_files
        assert node_result["meta_count"] == 0 and node_result["stage_files"] == []
        assert json.loads((scenario_root / "state.json").read_text(encoding="utf-8"))["request_events"][0]["terminal"] is True
        if expected in {"timeout", "incomplete"}:
            assert marker.read_text(encoding="utf-8") == "cancelled\n"
        if expected == "timeout":
            assert stream.state["headers_sent"] is True
        if expected == "incomplete":
            assert stream.state["server_closed"] is True
