"""The Bombadil fixture server's ready signal (tests/bombadil/serve.py)."""

import importlib.util
import socket
from pathlib import Path


def _serve():
    path = Path(__file__).parent / "bombadil" / "serve.py"
    spec = importlib.util.spec_from_file_location("bombadil_serve", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_socket_is_listening_when_the_ready_file_appears(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    ready = out / "ready.txt"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        url = _serve().bind_and_announce(sock, str(ready))
        assert ready.read_text(encoding="utf-8") == url == f"http://127.0.0.1:{sock.getsockname()[1]}/"
        assert [p.name for p in out.iterdir()] == ["ready.txt"]  # written whole, no leftovers
        # Nothing accepts yet, as before uvicorn starts: the connection waits in the backlog.
        with socket.create_connection(sock.getsockname(), timeout=2) as client:
            assert client.getpeername() == sock.getsockname()
