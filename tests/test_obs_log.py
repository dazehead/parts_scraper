"""Structured logger emits one JSON object per line with the expected fields."""
import io
import json
import logging
import sys

import pytest


@pytest.fixture(autouse=True)
def reset_logging():
    # Each test wants a fresh root configuration.
    import obs.log as log_mod
    log_mod._configured = False  # type: ignore[attr-defined]
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    yield


def _capture_log_line(build_and_log) -> dict:
    """Run ``build_and_log()`` with stdout patched and return the parsed JSON.

    ``build_and_log`` must build the logger *inside* the patched block,
    because the StreamHandler binds to whatever ``sys.stdout`` was when
    the handler is created.
    """
    buf = io.StringIO()
    sys_stdout = sys.stdout
    sys.stdout = buf
    try:
        build_and_log()
    finally:
        sys.stdout = sys_stdout
    line = buf.getvalue().strip().splitlines()[-1]
    return json.loads(line)


def _emit(name="test", customer=None, **fields):
    """Helper that builds a logger inside the patched-stdout block."""
    def _inner():
        import os
        if customer is None:
            os.environ.pop("CUSTOMER", None)
        else:
            os.environ["CUSTOMER"] = customer
        from obs import get_logger
        get_logger(name).info(fields.pop("_msg", "hi"), **fields)
    return _inner


def test_basic_log_is_json():
    payload = _capture_log_line(_emit(name="test", _msg="hello world"))
    assert payload["msg"] == "hello world"
    assert payload["level"] == "info"
    assert payload["logger"] == "test"
    assert "ts" in payload


def test_extra_fields_make_it_through():
    payload = _capture_log_line(
        _emit(name="test", customer="acme", _msg="downloaded", part_number="AB123", n=3)
    )
    assert payload["msg"] == "downloaded"
    assert payload["part_number"] == "AB123"
    assert payload["n"] == 3
    assert payload["customer"] == "acme"


def test_bind_persists_across_calls():
    def build_and_log():
        from obs import get_logger
        bound = get_logger("test").bind(stage="scraper", shard="chunk_3.csv")
        bound.info("processing")

    payload = _capture_log_line(build_and_log)
    assert payload["stage"] == "scraper"
    assert payload["shard"] == "chunk_3.csv"


def test_reserved_fields_dont_collide():
    # ``module`` is a LogRecord reserved attribute. Passing it as a user
    # field would normally raise inside logging — the logger renames it
    # to ``_module`` so the caller gets to keep its data.
    def build_and_log():
        from obs import get_logger
        get_logger("test").info("hi", module="user-supplied-module")

    payload = _capture_log_line(build_and_log)
    assert payload["msg"] == "hi"
    assert payload.get("_module") == "user-supplied-module"


def test_unserialisable_fields_are_repr_d():
    class Weird:
        def __repr__(self):
            return "<Weird instance>"

    def build_and_log():
        from obs import get_logger
        get_logger("test").info("payload", thing=Weird())

    payload = _capture_log_line(build_and_log)
    assert payload["thing"] == "<Weird instance>"
