from reuleauxcoder.domain.config.models import Config, ContextConfig
from reuleauxcoder.interfaces.entrypoint.dependencies import _default_create_agent


class FakeLLM:
    model = "fake-model"
    debug_trace = False


def test_default_create_agent_passes_context_config_to_context_manager() -> None:
    config = Config(
        api_key="key",
        max_context_tokens=12345,
        context=ContextConfig(
            snip_keep_recent_tools=2,
            snip_threshold_chars=321,
            snip_min_lines=4,
            summarize_keep_recent_turns=9,
            token_fudge_factor=1.25,
        ),
    )

    agent = _default_create_agent(FakeLLM(), [], config)

    assert agent.config is config
    assert agent.context.max_tokens == 12345
    assert agent.context._snip_keep_recent_tools == 2
    assert agent.context._snip_threshold_chars == 321
    assert agent.context._snip_min_lines == 4
    assert agent.context._summarize_keep_recent_turns == 9
    assert agent.context._token_fudge_factor == 1.25
