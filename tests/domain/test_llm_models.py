from reuleauxcoder.domain.llm.models import LLMResponse, ToolCall


def test_llm_response_message_preserves_reasoning_content() -> None:
    response = LLMResponse(
        content="",
        reasoning_content="思考中",
        reasoning_details=[
            {
                "type": "reasoning.text",
                "text": "思考中",
                "signature": "sig-1",
            }
        ],
        tool_calls=[ToolCall(id="tool_1", name="shell", arguments={"command": "pwd"})],
    )

    message = response.message

    assert message["role"] == "assistant"
    assert message["content"] is None
    assert message["reasoning_content"] == "思考中"
    assert message["reasoning_details"][0]["signature"] == "sig-1"
    assert message["tool_calls"][0]["function"]["name"] == "shell"
