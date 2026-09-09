"""Imagery fetch: state GIS services are flaky, so failures must be survivable."""
import urllib.error
import pytest

from curbside.sources.imagery import fetch


class _Resp:
    def __init__(self, data): self._d = data
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_successful_fetch_writes_the_file(tmp_path, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b"x" * 30000))
    out = tmp_path / "a.jpg"
    ok, msg = fetch(39.9, -86.1, out)
    assert ok and out.exists() and out.stat().st_size == 30000


def test_server_error_is_returned_not_raised(tmp_path, monkeypatch):
    """A 500 from the state service must cost one lead, not abort the scan."""
    import urllib.request

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 500, "Server Error", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    ok, msg = fetch(39.9, -86.1, tmp_path / "a.jpg", attempts=1)
    assert ok is False
    assert "500" in msg


def test_timeout_is_returned_not_raised(tmp_path, monkeypatch):
    import urllib.request

    def stall(*a, **k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(urllib.request, "urlopen", stall)
    ok, msg = fetch(39.9, -86.1, tmp_path / "a.jpg", attempts=1)
    assert ok is False and "TimeoutError" in msg


def test_transient_failure_is_retried(tmp_path, monkeypatch):
    """These services fail intermittently under load; one retry recovers most."""
    import urllib.request
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError("u", 500, "Server Error", {}, None)
        return _Resp(b"y" * 30000)

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ok, msg = fetch(39.9, -86.1, tmp_path / "a.jpg", attempts=2)
    assert ok is True and calls["n"] == 2


def test_blank_tile_means_outside_coverage(tmp_path, monkeypatch):
    """A tiny JPEG is the service saying 'nothing here', not an error."""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b"z" * 500))
    ok, msg = fetch(39.9, -86.1, tmp_path / "a.jpg")
    assert ok is False and "coverage" in msg
    assert not (tmp_path / "a.jpg").exists()
