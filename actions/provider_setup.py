"""
actions/provider_setup.py — wire an ARIA provider key into the pipeline.

What E4 established from the code: this build's provider config
already has the shape ARIA's keys need. There is ONE OpenAI-compatible
provider (id "openai_compat"); a service — Groq, OpenRouter, Mistral,
Ollama Cloud — is a *catalog entry* over it, and its configuration is
stored as that provider's creds: {base_url, model, api_key, catalog}.
The engine builds whatever the pipeline's LLM slot names from exactly
those creds. So there was no plumbing to add — this action is the
setup surface, mirroring gmail_setup: the user hands over a key by
voice and it lands in the config in the panel's own shape.

Two honest limits, stated in the outcome text rather than discovered
later:

  * This configures the PIPELINE engine's model. Gemini Live remains
    the conversation until the user switches modes in ⚙ ENGINE &
    PROVIDERS; a key saved here does not change her brain by itself.
  * A model name is still required before the pipeline can run
    (the panel's LIST button reads the service's own list). Pass
    `model` here, or choose it there.

providers/ is imported lazily, inside the call: a default install
never executes the providers package, and an action module must not
break that at discovery time.
"""

from __future__ import annotations

from core import outcome
from memory import config_manager

# service id (what the user says) -> (catalog id, PRESETS label)
_SERVICES = {
    "groq": ("groq", "Groq"),
    "openrouter": ("openrouter", "OpenRouter"),
    "ollama_cloud": ("ollama_cloud", "Ollama Cloud"),
    "mistral": ("mistral", "Mistral"),
}


def _normalise_service(raw: str) -> str:
    s = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"open_router": "openrouter"}
    return aliases.get(s, s)


def provider_setup(parameters: dict = None, player=None,
                   session_memory=None) -> str:
    params = parameters or {}
    service = _normalise_service(params.get("service") or "")
    api_key = (params.get("api_key") or "").strip()
    model = (params.get("model") or "").strip()
    if service not in _SERVICES:
        return outcome.failed(
            "provider_setup",
            "that service is not one I can wire a key for — supported: "
            + ", ".join(sorted(_SERVICES)))
    if not api_key:
        return outcome.failed("provider_setup",
                              "no API key was given")

    catalog_id, preset_label = _SERVICES[service]
    from providers.llm_openai import PRESETS
    values = {"base_url": PRESETS[preset_label],
              "catalog": catalog_id,
              "api_key": api_key}
    if model:
        values["model"] = model
    config_manager.save_provider_config("openai_compat", values)
    # Naming her as the pipeline's LLM costs nothing in Gemini mode
    # (the slots are only read by the pipeline engine) and makes the
    # mode switch in the panel work with nothing left to type.
    config_manager.save_pipeline(llm="openai_compat")

    note = (f" The pipeline model is set to '{model}'."
            if model else
            " No model was named — the pipeline still needs one: "
            "choose it in the providers panel (LIST reads the "
            "service's models) or call provider_setup again with "
            "model.")
    return outcome.done("provider_setup", service=catalog_id,
                        base_url=values["base_url"]) + (
        f"\n{preset_label} is wired as the pipeline engine's model "
        f"provider.{note} Gemini Live still holds the conversation "
        f"until the user switches the engine mode in the providers "
        f"panel.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "provider_setup",
    "description": (
        "Wire an API key for a hosted model service into the "
        "pipeline engine: service is one of groq, openrouter, "
        "ollama_cloud, mistral. Stores the key with the service's "
        "preset address as the OpenAI-compatible provider config and "
        "names it the pipeline's LLM. This does NOT switch the "
        "conversation off Gemini Live — that is the user's mode "
        "choice in the providers panel. Pass model if known; the "
        "pipeline cannot run until a model is chosen."
    ),
    "brief": "Wire a model-service key ('service', 'api_key', 'model').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "service": {
                "type": "STRING",
                "description": "groq | openrouter | ollama_cloud | mistral",
            },
            "api_key": {
                "type": "STRING",
                "description": "The service's API key",
            },
            "model": {
                "type": "STRING",
                "description": "Model name at that service (optional; can be chosen later in the panel)",
            },
        },
        "required": ["service", "api_key"],
    },
    "handler": provider_setup,
}
