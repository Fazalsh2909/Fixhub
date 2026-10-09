"""Phase 3 provider registry: generic OpenAI-compatible architecture.

OpenAI and OpenRouter are presets on the SAME runtime path; users may enter
any arbitrary OpenAI-compatible base URL + model via the `custom` provider.
Native Anthropic (different API shape) is intentionally out of scope — the
`api_format` field reserves it so support can be added later without
redesigning BYOK.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    default_base_url: str
    default_model: str
    api_format: str = "openai_compatible"
    needs_base_url: bool = False


PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        label="OpenAI",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
    ),
    "openrouter": ProviderSpec(
        name="openrouter",
        label="OpenRouter",
        default_base_url="https://openrouter.ai/api/v1",
        default_model="openai/gpt-4o-mini",
    ),
    "custom": ProviderSpec(
        name="custom",
        label="Custom (OpenAI-compatible)",
        default_base_url="",
        default_model="",
        needs_base_url=True,
    ),
}


class UnknownProvider(ValueError):
    """Unsupported provider name."""


def get_provider(name: str) -> ProviderSpec:
    spec = PROVIDERS.get((name or "").strip().lower())
    if not spec:
        raise UnknownProvider(
            f"unsupported provider: {name!r} (choose from {sorted(PROVIDERS)})"
        )
    return spec


def resolve_endpoint(*, provider: str, base_url: str = "", model: str = "") -> dict:
    """Validate configuration and resolve the effective base_url + model.

    The user-provided model is authoritative; provider defaults apply only
    when the caller omits them. Raises UnknownProvider / ValueError.
    """
    spec = get_provider(provider)
    base = (base_url or "").strip() or spec.default_base_url
    if spec.needs_base_url and not base:
        raise ValueError("base_url is required for the custom provider")
    if base and not (base.startswith("http://") or base.startswith("https://")):
        raise ValueError("base_url must be an http(s) URL")
    resolved_model = (model or "").strip() or spec.default_model
    if not resolved_model:
        raise ValueError("model is required")
    return {
        "provider": spec.name,
        "base_url": base,
        "model": resolved_model,
        "api_format": spec.api_format,
    }
