# -*- coding: utf-8 -*-
"""回归：_agent_link_wanted 必须认识 swarm 键（漏检导致 Manager 永不创建，
swarm 联动完全空转——2026-09-25 简报链路排查一天的实际根因）。"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


def test_agent_link_wanted_recognizes_swarm(tmp_path):
    from pet.window_optional_services import WindowFeatureGateMixin

    host = WindowFeatureGateMixin.__new__(WindowFeatureGateMixin)
    host.cfg = {"agent_link": {"swarm": True}}
    assert host._agent_link_wanted() is True


def test_agent_link_wanted_builtin_keys_still_work(tmp_path):
    from pet.window_optional_services import WindowFeatureGateMixin

    host = WindowFeatureGateMixin.__new__(WindowFeatureGateMixin)
    for key in ("dsh", "claude", "cursor", "opencode"):
        host.cfg = {"agent_link": {key: True}}
        assert host._agent_link_wanted() is True, key
    host.cfg = {"agent_link": {"custom_agents": [{"key": "g", "name": "g", "path": "p"}]}}
    assert host._agent_link_wanted() is True


def test_agent_link_wanted_all_off_is_false(tmp_path):
    from pet.window_optional_services import WindowFeatureGateMixin

    host = WindowFeatureGateMixin.__new__(WindowFeatureGateMixin)
    host.cfg = {"agent_link": {"dsh": False, "swarm": False, "custom_agents": []}}
    assert host._agent_link_wanted() is False
