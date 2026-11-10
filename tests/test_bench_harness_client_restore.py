"""``_count_model_calls`` must leave the shared Ollama client as it found it.

Restoring by assigning the bound originals back onto the instance left
``generate`` / ``chat_completions`` as instance attributes, which shadow the
class. Later tests that patch ``OllamaClient.generate`` at class level then
missed the singleton and reached the real host, so on the serial main sweep
``test_exclusion_enrichment_leak[path]`` failed after the bench tests ran.
"""
from __future__ import annotations

from bench import harness
from palinode.core.ollama_client import OllamaClient, get_ollama_client


def test_count_model_calls_leaves_no_instance_attributes(monkeypatch):
    client = get_ollama_client()
    before = {n: n in vars(client) for n in ("generate", "chat_completions")}

    with harness._count_model_calls():
        assert "generate" in vars(client)

    for name, had in before.items():
        assert (name in vars(client)) is had

    calls = []
    monkeypatch.setattr(OllamaClient, "generate", lambda self, *a, **k: calls.append(a) or {"response": ""})
    get_ollama_client().generate("probe")
    assert calls == [("probe",)]
