"""Phase E4 — ARIA's provider keys in the pipeline's config shape.

What the code established, pinned here:

  * There is one OpenAI-compatible provider ("openai_compat"); a
    hosted service is a catalog entry over it, stored as that
    provider's creds {base_url, model, api_key, catalog}. No new
    plumbing was needed — provider_setup is a setup surface in
    gmail_setup's pattern, writing the panel's own shape.
  * Groq and OpenRouter presets already existed. Ollama Cloud did
    not — ARIA's first provider is Ollama's HOSTED endpoint, not
    the local server — so it was added (preset + catalog entry),
    as a cloud entry: no tool budget, per the providers self-test's
    own invariant.
  * After provider_setup with a model, the stored config satisfies
    the engine's own completeness check (registry.missing_fields)
    and the pipeline's LLM slot names the provider.
"""
from pathlib import Path

import pytest

import actions.provider_setup as ps
from memory import config_manager as cm

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", path)
    return path


def test_groq_key_lands_in_the_engines_shape(cfg):
    result = ps.provider_setup({"service": "groq", "api_key": "gsk-x",
                                "model": "llama-3.3-70b-versatile"})
    assert result.startswith("status=done"), result
    creds = cm.get_provider_config("openai_compat")
    assert creds["base_url"] == "https://api.groq.com/openai/v1"
    assert creds["catalog"] == "groq"
    assert creds["api_key"] == "gsk-x"
    assert creds["model"] == "llama-3.3-70b-versatile"
    assert cm.get_pipeline().get("llm") == "openai_compat"

    from providers import registry
    assert registry.missing_fields("openai_compat", creds) == []


def test_openrouter_and_mistral_presets(cfg):
    ps.provider_setup({"service": "OpenRouter", "api_key": "sk-or"})
    creds = cm.get_provider_config("openai_compat")
    assert creds["base_url"] == "https://openrouter.ai/api/v1"
    assert creds["catalog"] == "openrouter"
    ps.provider_setup({"service": "mistral", "api_key": "m-key"})
    creds = cm.get_provider_config("openai_compat")
    assert creds["base_url"] == "https://api.mistral.ai/v1"
    assert creds["catalog"] == "mistral"


def test_ollama_cloud_is_a_cloud_catalog_entry(cfg):
    from providers.llm_openai import CATALOG, PRESETS

    entry = next(e for e in CATALOG if e["id"] == "ollama_cloud")
    assert entry["local"] is False
    assert not entry["values"].get("max_tools")     # cloud: no budget
    assert entry["values"]["base_url"] == PRESETS["Ollama Cloud"]
    assert PRESETS["Ollama Cloud"] == "https://ollama.com/v1"

    result = ps.provider_setup({"service": "ollama_cloud",
                                "api_key": "oc-key", "model": "gpt-oss:120b"})
    assert result.startswith("status=done"), result
    creds = cm.get_provider_config("openai_compat")
    assert creds["catalog"] == "ollama_cloud"
    assert creds["base_url"] == "https://ollama.com/v1"


def test_no_model_yet_is_said_out_loud(cfg):
    result = ps.provider_setup({"service": "groq", "api_key": "gsk-x"})
    assert result.startswith("status=done"), result
    assert "still needs one" in result
    from providers import registry
    creds = cm.get_provider_config("openai_compat")
    assert "Model" in registry.missing_fields("openai_compat", creds)


def test_unknown_service_and_missing_key_fail(cfg):
    assert ps.provider_setup({"service": "skynet", "api_key": "x"}) \
        .startswith("status=failed")
    assert ps.provider_setup({"service": "groq"}) \
        .startswith("status=failed")
    assert cm.get_provider_config("openai_compat") == {}


def test_registry_lists_provider_setup():
    import main
    from core.action_loader import discover_actions

    reserved = {t["name"] for t in main.TOOL_DECLARATIONS}
    actions = discover_actions(ROOT / "actions", reserved_names=reserved,
                               logger=lambda msg: None)
    assert actions.has("provider_setup")
