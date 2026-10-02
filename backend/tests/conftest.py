"""Test configuration: isolated data dir + a tiny local "agency" website served over HTTP."""

from __future__ import annotations

import os
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="leadforge-test-")
os.environ.setdefault("LF_DATA_DIR", _TMP)
os.environ.setdefault("LF_BROWSER_ENABLED", "false")
os.environ.setdefault("LF_SMTP_VERIFY", "false")
os.environ.setdefault("LF_PER_HOST_MIN_DELAY", "0")
os.environ.setdefault("LF_RESPECT_ROBOTS", "false")

FIXTURE_SITE = Path(__file__).parent / "fixtures" / "agency_site"


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D102
        pass


@pytest.fixture(scope="session")
def agency_site_url() -> str:
    handler = partial(_Quiet, directory=str(FIXTURE_SITE))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}/"
    server.shutdown()
