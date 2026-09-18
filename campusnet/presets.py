"""学校预设。

加一条就多支持一个学校 —— 欢迎 PR 补充自己的学校。
每条至少要有 label / provider / options.portal。
"""

from __future__ import annotations

from typing import Any

__all__ = ["PRESETS", "get_preset", "preset_choices", "apply_preset"]

PRESETS: dict[str, dict[str, Any]] = {
    "cuit": {
        "label": "成都信息工程大学",
        "provider": "ruijie_sam_cas",
        "options": {"portal": "http://10.254.241.66"},
    },
}

NO_PRESET = "（不确定／让程序自己检测）"


def get_preset(key: str) -> dict[str, Any] | None:
    return PRESETS.get(key)


def preset_choices() -> list[str]:
    """Labels shown in the GUI dropdown, with a "none" entry first."""
    return [NO_PRESET] + [item["label"] for item in PRESETS.values()]


def key_for_label(label: str) -> str | None:
    for key, item in PRESETS.items():
        if item["label"] == label:
            return key
    return None


def apply_preset(cfg_raw: dict[str, Any], key: str) -> bool:
    """Merge a preset into a raw config dict. Returns False for unknown keys."""
    item = PRESETS.get(key)
    if not item:
        return False
    cfg_raw["provider"] = item["provider"]
    cfg_raw["options"] = {**(cfg_raw.get("options") or {}), **(item.get("options") or {})}
    return True
