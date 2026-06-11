from __future__ import annotations

import time

import pytest

from reuleauxcoder.domain.providers.models import ProviderRequest, ProviderResponse
from reuleauxcoder.services.providers.stream_supervisor import (
    ProviderStreamInterruptedError,
    StreamLivenessLimits,
    StreamSupervisor,
)
from reuleauxcoder.services.providers.tool_call_delta import emit_tool_call_delta


def test_stream_supervisor_interrupts_idle_stream_without_waiting_for_generator() -> None:
    def slow_stream():
        time.sleep(0.2)
        yield "late"

    supervisor = StreamSupervisor(
        provider_id="test",
        provider_type="test",
        params={},
        partial_response_factory=ProviderResponse,
        liveness_limits=StreamLivenessLimits(wall_time_sec=1.0, idle_time_sec=0.05),
    )

    started = time.time()
    with pytest.raises(ProviderStreamInterruptedError) as exc_info:
        supervisor.consume(slow_stream(), lambda _index, _chunk: None)

    assert time.time() - started < 0.2
    assert exc_info.value.interruption["code"] == "stream_idle_timeout"
    assert exc_info.value.partial_response.stream_status == "interrupted"


def test_tool_argument_delta_enforces_transport_size_limit() -> None:
    request = ProviderRequest(
        model="demo",
        messages=[],
        on_tool_call_delta=lambda _delta: None,
    )

    with pytest.raises(ValueError, match="128 KiB"):
        emit_tool_call_delta(
            request,
            index=0,
            tool_call_id="call-1",
            tool_name="apply_patch",
            arguments_delta="x" * (128 * 1024 + 1),
        )
