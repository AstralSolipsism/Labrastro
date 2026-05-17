from reuleauxcoder.domain.config.models import (
    Config,
    ModelProfileConfig,
    ProviderConfig,
    ProvidersConfig,
)
from reuleauxcoder.services.llm.factory import (
    build_llm_from_settings,
    model_binding_settings,
    resolve_model_runtime,
)


def test_resolve_model_runtime_uses_active_profile_and_provider() -> None:
    config = Config(
        providers=ProvidersConfig(
            items={
                "deepseek": ProviderConfig(
                    id="deepseek",
                    api_key="sk-test",
                    base_url="https://api.deepseek.com",
                )
            }
        ),
        model_profiles={
            "main": ModelProfileConfig(
                name="main",
                provider="deepseek",
                model="deepseek-v4-pro",
                max_tokens=384000,
                max_context_tokens=1000000,
                temperature=0.1,
            )
        },
        active_main_model_profile="main",
    )

    runtime = resolve_model_runtime(config)

    assert runtime.profile_name == "main"
    assert runtime.provider_id == "deepseek"
    assert runtime.api_key == "sk-test"
    assert runtime.base_url == "https://api.deepseek.com"
    assert runtime.model == "deepseek-v4-pro"
    assert runtime.max_tokens == 384000
    assert runtime.max_context_tokens == 1000000


def test_build_llm_from_settings_does_not_require_flat_config_fields() -> None:
    provider = ProviderConfig(id="openai", api_key="sk-test")
    profile = ModelProfileConfig(
        name="main",
        provider="openai",
        model="gpt-4.1",
        max_tokens=32768,
        max_context_tokens=128000,
    )

    llm = build_llm_from_settings(
        profile,
        providers=ProvidersConfig(items={"openai": provider}),
    )

    assert llm.model == "gpt-4.1"
    assert llm.api_key == "sk-test"
    assert llm.provider_id == "openai"


def test_model_binding_settings_uses_provider_config_and_parameter_overrides() -> None:
    config = Config(
        providers=ProvidersConfig(
            items={"deepseek": ProviderConfig(id="deepseek", api_key="sk-test")}
        ),
        model_profiles={
            "main": ModelProfileConfig(
                name="main",
                provider="deepseek",
                model="deepseek-chat",
                max_tokens=4096,
                max_context_tokens=128000,
                temperature=0.0,
            )
        },
        active_main_model_profile="main",
    )

    runtime = model_binding_settings(
        provider="deepseek",
        model="deepseek-v4-pro",
        parameters={"max_tokens": 384000, "temperature": 0.2},
        fallback=config,
    )

    assert runtime.model == "deepseek-v4-pro"
    assert runtime.api_key == "sk-test"
    assert runtime.max_tokens == 384000
    assert runtime.max_context_tokens == 128000
    assert runtime.temperature == 0.2
