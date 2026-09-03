"""Small explicit test-side bridge to the plugin-owned checkpoint API."""

from __future__ import annotations

from typing import Any


def checkpoint_if_enabled(request: Any):
    recorder = getattr(request.config, "_super1_observation_recorder", None)
    if recorder is None or not recorder.is_behavior(str(request.node.nodeid)):
        return None
    return recorder.fixture(str(request.node.nodeid)).checkpoint()


def record_if_enabled(request: Any, token: Any) -> None:
    if token is None:
        return
    recorder = request.config._super1_observation_recorder
    recorder.fixture(str(request.node.nodeid)).record_actual(checkpoint=token)
