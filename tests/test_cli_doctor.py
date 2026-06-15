"""`python -m zhub doctor` — install/environment check output.

The brain-credential section is derived from the adapter REGISTRY rather
than a hardcoded key list, so adding a new adapter can never silently drop
its env var from the doctor report. These tests pin that contract.
"""

import pytest

from zhub import cli_doctor
from zhub.brains import REGISTRY


@pytest.fixture
def doctor_output(monkeypatch, capsys):
    """Run `doctor` with network probing stubbed out, return captured stdout."""
    # Keep REGISTRY real (that's what we're testing) but avoid the live
    # detection probes so the test stays offline and fast.
    monkeypatch.setattr("zhub.brains.list_available", lambda: [])

    def run(env=None):
        for cls in REGISTRY:
            for key in cls.env_keys:
                monkeypatch.delenv(key, raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        cli_doctor.run([])
        return capsys.readouterr().out

    return run


def test_lists_every_adapter_env_key(doctor_output):
    out = doctor_output()
    for cls in REGISTRY:
        for key in cls.env_keys:
            assert key in out, f"{key} ({cls.name}) missing from doctor report"


def test_includes_previously_dropped_keys(doctor_output):
    # Regression guard: these four belonged to adapters added after the
    # original hardcoded 4-key list and were silently absent from the report.
    out = doctor_output()
    for key in ("ANTHROPIC_API_KEY", "TOGETHER_API_KEY",
                "MISTRAL_API_KEY", "COHERE_API_KEY"):
        assert key in out


def test_set_vs_unset_marking(doctor_output):
    out = doctor_output({"ANTHROPIC_API_KEY": "sk-test"})
    lines = {ln.split()[1]: ln for ln in out.splitlines()
             if "API_KEY" in ln or "OLLAMA_HOST" in ln}
    assert "✓" in lines["ANTHROPIC_API_KEY"]
    assert "set" in lines["ANTHROPIC_API_KEY"]
    assert "✗" in lines["GROQ_API_KEY"]
    assert "not set" in lines["GROQ_API_KEY"]


def test_each_env_key_reported_once(doctor_output):
    out = doctor_output()
    for cls in REGISTRY:
        for key in cls.env_keys:
            # one line per key in the brain-availability section
            assert out.count(f" {key}") == 1, f"{key} reported more than once"
