# -*- coding: utf-8 -*-
"""agent_link.swarm（Agent Swarm 联动）配置域 focused tests。

形状决策（对齐内置四键）：
- ``agent_link.swarm``：布尔总开关（apply_config / other_instances_use_agent /
  右键菜单都按内置键的布尔约定消费）；
- ``agent_link.swarm_config``：嵌套块 {server_url, workspaces}；
- apikey 绝不进 config.json（走 keyring，见 pet/chat/models.py SecretStore 同款
  服务名），测试断言清洗结果里没有任何 key/secret/token 字段。
"""
from __future__ import annotations

from pet.config import (
    _AGENT_LINK_BUILTIN_KEYS,
    _clean_agent_link_data,
    _clean_swarm_config,
)


class TestSwarmConfigClean:
    def test_default_is_disabled_with_empty_block(self):
        cleaned = _clean_agent_link_data({})
        assert cleaned["swarm"] is False
        assert cleaned["swarm_config"] == {"server_url": "", "workspaces": []}

    def test_swarm_is_builtin_key(self):
        # swarm 进入内置键名单：custom_agents 不得占用该 key
        assert "swarm" in _AGENT_LINK_BUILTIN_KEYS

    def test_clean_keeps_enabled_and_fields(self):
        cleaned = _clean_agent_link_data({
            "swarm": True,
            "swarm_config": {
                "server_url": " http://10.17.17.19:8700/ ",
                "workspaces": [" wid1 ", "wid2", "", 123, None],
            },
        })
        assert cleaned["swarm"] is True
        assert cleaned["swarm_config"]["server_url"] == "http://10.17.17.19:8700"
        # 空串/非字符串条目丢弃；其余去空白
        assert cleaned["swarm_config"]["workspaces"] == ["wid1", "wid2"]

    def test_workspaces_dedup_and_cap(self):
        cleaned = _clean_swarm_config({
            "workspaces": ["a", "a", "b"] + [f"w{i}" for i in range(20)],
        })
        # 去重 + 上限 8（与 custom_agents 上限纪律一致）
        assert cleaned["workspaces"] == ["a", "b"] + [f"w{i}" for i in range(6)]

    def test_non_dict_swarm_config_falls_back(self):
        cleaned = _clean_agent_link_data({"swarm_config": "junk"})
        assert cleaned["swarm_config"] == {"server_url": "", "workspaces": []}

    def test_no_secret_fields_survive(self):
        cleaned = _clean_agent_link_data({
            "swarm_config": {
                "server_url": "http://x",
                "api_key": "as_secret",
                "apikey": "as_secret",
                "token": "jwt",
                "secret": "s",
            },
        })
        cfg = cleaned["swarm_config"]
        assert set(cfg) == {"server_url", "workspaces"}
        assert "as_secret" not in str(cfg)

    def test_server_url_scheme_whitelist(self):
        # 只收 http/https；其他 scheme（file:/ws: 等）回退空串
        cleaned = _clean_swarm_config({"server_url": "ws://x"})
        assert cleaned["server_url"] == ""
        cleaned = _clean_swarm_config({"server_url": "https://swarm.example.com"})
        assert cleaned["server_url"] == "https://swarm.example.com"
