"""OS-boundary and destination-filter tests with no model generation."""
import json
import platform
import shutil
import socket
import subprocess

import pytest

from experiments.model_matrix.runtime import build_profile
from experiments.model_matrix.transport import TransportProxy


def test_gateway_rejects_nonprovider_destination():
    with TransportProxy("openai") as proxy:
        with socket.create_connection(("127.0.0.1", proxy.port)) as connection:
            connection.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
            assert connection.recv(200).startswith(b"HTTP/1.1 403")
        assert proxy.audit()["denied_connections"] == {"unapproved_destination": 1}
        assert proxy.audit()["tls_interception"] is False


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS-specific evaluated boundary")
def test_single_os_boundary_allows_workspace_gateway_only(tmp_path):
    workspace, runtime = tmp_path / "workspace", tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    (tmp_path / "protected.txt").write_text("PRIVATE_TEST_VALUE")
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen(2)
        with TransportProxy("openai") as proxy:
            profile = runtime / "profile.sb"
            profile.write_text(build_profile(workspace, runtime, tmp_path, proxy.port))
            program = f'''
import json, socket
from pathlib import Path
result = {{}}
Path("allowed.txt").write_text("ok")
result["canonicalized"] = str(Path({str(runtime)!r}).resolve())
try:
    Path("../protected.txt").read_text()
    result["read_denied"] = False
except PermissionError:
    result["read_denied"] = True
try:
    socket.create_connection(("127.0.0.1", {other.getsockname()[1]}), timeout=2)
    result["network_denied"] = False
except PermissionError:
    result["network_denied"] = True
with socket.create_connection(("127.0.0.1", {proxy.port}), timeout=2) as s:
    s.sendall(b"CONNECT example.com:443 HTTP/1.1\\r\\n\\r\\n")
    result["gateway_only"] = s.recv(200).startswith(b"HTTP/1.1 403")
print(json.dumps(result))
'''
            python = shutil.which("python3", path="/opt/homebrew/bin:/usr/bin")
            result = subprocess.run(["sandbox-exec", "-f", str(profile), python, "-I", "-c", program],
                                    cwd=workspace, capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, result.stderr
            observed = json.loads(result.stdout)
            assert observed["read_denied"] and observed["network_denied"] and observed["gateway_only"]
            assert (workspace / "allowed.txt").read_text() == "ok"
