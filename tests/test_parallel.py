import pytest

from hermes_control import parallel


DEFAULTS = {"max_tasks": 1, "per_engine": {"claude": 1, "codex": 1}}


def test_defaults_and_partial_settings():
    assert parallel.parse_parallel(None) == DEFAULTS
    assert parallel.parse_parallel({}) == DEFAULTS
    assert parallel.parse_parallel({"max_tasks": 2}) == {
        "max_tasks": 2, "per_engine": {"claude": 2, "codex": 2}}
    assert parallel.parse_parallel({"max_tasks": 3, "per_engine": {"codex": 1}}) == {
        "max_tasks": 3, "per_engine": {"claude": 3, "codex": 1}}
    assert parallel.parse_parallel({"max_tasks": 8, "per_engine": {"codex": 12}}) == {
        "max_tasks": 8, "per_engine": {"claude": 8, "codex": 12}}
    parallel.parse_parallel(None)["per_engine"]["codex"] = 9
    assert parallel.parse_parallel(None) == DEFAULTS


@pytest.mark.parametrize("raw,key", [
    ([], "parallel"), ({"extra": 2}, "extra"),
    *[({"max_tasks": v}, "max_tasks") for v in [0, -1, 9, True, 2.0, "2", None]],
    ({"per_engine": []}, "per_engine"), ({"per_engine": None}, "per_engine"),
    ({"per_engine": {"other": 1}}, "other"),
    *[({"per_engine": {"codex": v}}, "codex") for v in [0, -1, True, 2.0, "2", None]],
])
def test_invalid_settings_name_the_key(raw, key):
    with pytest.raises(ValueError, match=key):
        parallel.parse_parallel(raw)


def test_read_uses_plugin_settings(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "plugins": {"entries": {"hermes_control": {"settings": {
            "parallel": {"max_tasks": 2, "per_engine": {"codex": 2}}}}}}})
    assert parallel.read_parallel() == {
        "max_tasks": 2, "per_engine": {"claude": 2, "codex": 2}}


def test_invalid_settings_warn_and_default(monkeypatch, caplog):
    from hermes_control.dispatch import _parallel

    def invalid():
        raise ValueError("parallel.oops")

    monkeypatch.setattr(parallel, "read_parallel", invalid)
    assert _parallel() == (DEFAULTS, "ValueError: parallel.oops")
    assert "parallel settings are invalid" in caplog.text
