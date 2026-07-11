"""离线冒烟测试：配置归一化与 WebUI 空值落盘。"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent / "maibot-plugin-sdk"))

import plugin as corpus  # noqa: E402


def _flatten(value: object) -> list[object]:
    if isinstance(value, dict):
        items: list[object] = []
        for nested in value.values():
            items.extend(_flatten(nested))
        return items
    if isinstance(value, list):
        items = []
        for nested in value:
            items.extend(_flatten(nested))
        return items
    return [value]


def test_webui_blank_and_none_persist() -> None:
    inst = corpus.create_plugin()
    normalized, _ = inst.normalize_plugin_config({})
    assert all(v is not None for v in _flatten(normalized))
    blanked, _ = inst.normalize_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": corpus.CURRENT_CONFIG_VERSION},
            "veto": {
                "escalate_consecutive_vetoes": "",
                "max_consecutive_vetoes": "  ",
                "veto_window_seconds": "",
            },
        }
    )
    assert all(v is not None for v in _flatten(blanked))
    assert "escalate_consecutive_vetoes" not in blanked.get("veto", {})
    print("ok: webui blank/None omitted for toml persist")


def main() -> None:
    test_webui_blank_and_none_persist()
    print("\n全部冒烟测试通过")


if __name__ == "__main__":
    main()
