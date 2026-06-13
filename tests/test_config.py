"""配置占位空值解析与旧版默认值迁移测试。"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SDK_ROOT = _ROOT.parent / "maibot-plugin-sdk"
for path in (_ROOT, _SDK_ROOT):
    normalized = str(path)
    if path.is_dir() and normalized not in sys.path:
        sys.path.insert(0, normalized)

from plugin import (  # noqa: E402
    CURRENT_CONFIG_VERSION,
    DEFAULT_ESCALATE_CONSECUTIVE_VETOES,
    DEFAULT_MAX_CONSECUTIVE_VETOES,
    DEFAULT_PROTOCOL_PROMPT,
    DEFAULT_SENTINEL,
    DEFAULT_VETO_WINDOW_SECONDS,
    VetoSectionConfig,
    _migrate_legacy_baked_defaults,
    resolve_effective_veto_config,
)


def test_resolve_effective_veto_config_uses_builtin_defaults_when_empty():
    effective = resolve_effective_veto_config(VetoSectionConfig())
    assert effective.reject_sentinel == DEFAULT_SENTINEL
    assert effective.escalate_consecutive_vetoes == DEFAULT_ESCALATE_CONSECUTIVE_VETOES
    assert effective.max_consecutive_vetoes == DEFAULT_MAX_CONSECUTIVE_VETOES
    assert effective.veto_window_seconds == DEFAULT_VETO_WINDOW_SECONDS
    assert effective.protocol_prompt == DEFAULT_PROTOCOL_PROMPT


def test_resolve_effective_veto_config_respects_user_override():
    effective = resolve_effective_veto_config(
        VetoSectionConfig(max_consecutive_vetoes=3, protocol_prompt="自定义 {sentinel}")
    )
    assert effective.max_consecutive_vetoes == 3
    assert effective.protocol_prompt == "自定义 {sentinel}"


def test_migrate_legacy_baked_defaults_strips_shipped_values():
    config = {
        "plugin": {"config_version": "1.1.0"},
        "veto": {
            "reject_sentinel": DEFAULT_SENTINEL,
            "max_consecutive_vetoes": DEFAULT_MAX_CONSECUTIVE_VETOES,
        },
    }
    migrated, changed = _migrate_legacy_baked_defaults(config)
    assert changed is True
    assert migrated["veto"]["reject_sentinel"] == ""
    assert migrated["veto"]["max_consecutive_vetoes"] is None
    assert migrated["plugin"]["config_version"] == CURRENT_CONFIG_VERSION
