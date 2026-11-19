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


def test_seeded_snapshots_stop_at_the_injected_day(tmp_path):
    import sqlite3
    from datetime import date

    path = tmp_path / "metrics.sqlite"
    _serve().seed_metrics(path, today=date(2026, 1, 1))  # in the past: the wall clock must not decide
    conn = sqlite3.connect(path)
    count, latest = conn.execute("SELECT count(*), max(fetched_at) FROM post_metrics").fetchone()
    conn.close()
    assert count > 0 and latest < "2026-01-02"


def test_seeded_clips_cover_every_panel_state(tmp_path):
    from argus_herald import clip_panel
    from argus_herald.config import PerformanceSettings
    from argus_herald.store import Store

    _serve().seed_clips(tmp_path / "s.sqlite", tmp_path / "metrics.sqlite")
    store = Store(tmp_path / "s.sqlite")
    panel = clip_panel.load(store, tmp_path / "metrics.sqlite", PerformanceSettings())
    store.close()
    rows = [(r.id, r.freshness, r.best, r.rerecord_first, r.age) for r in panel.rows]
    assert rows == [
        ("atlas-styles", "redo", "strong", True, ""),
        ("proof-checkpoints", "check", "no data", False, "as of 3d"),
        ("lens-heatmap", "fresh", "typical", False, ""),
        ("pipeline-tour", "not checked", "no data", False, ""),
        ("quarry-loop", "not checked", "no data", False, ""),
    ]
    assert panel.rows[0].last_url.startswith("https://") and panel.missing == ("quarry",)
