"""Provider registry.

Built-in providers:

======================  ==================================================
name                    system
======================  ==================================================
``ruijie_sam_cas``      锐捷 SAM/ePortal 5.x + CAS 单点登录（含成都信息工程大学）
``ruijie_eportal``      锐捷 ePortal 经典接口 ``InterFace.do?method=login``
``srun``                深澜 Srun 门户
``generic_form``        通用 HTML 表单 POST（抓包模板）
======================  ==================================================
"""

from __future__ import annotations

from .base import LoginResult, Provider

__all__ = ["LoginResult", "Provider", "get_provider", "list_providers", "provider_names"]

_BUILTINS = (
    ("ruijie_sam_cas", "campusnet.providers.ruijie_sam_cas", "RuijieSamCasProvider"),
    ("ruijie_eportal", "campusnet.providers.ruijie_eportal", "RuijieEportalProvider"),
    ("srun", "campusnet.providers.srun", "SrunProvider"),
    ("generic_form", "campusnet.providers.generic_form", "GenericFormProvider"),
)


def _import(dotted: str, attr: str) -> type:
    import importlib

    mod = importlib.import_module(dotted)
    return getattr(mod, attr)


def provider_names() -> list[str]:
    return [name for name, _, _ in _BUILTINS]


def list_providers() -> list[type]:
    return [_import(d, a) for _, d, a in _BUILTINS]


def get_provider(name: str) -> type:
    for key, dotted, attr in _BUILTINS:
        if key == name:
            return _import(dotted, attr)
    raise KeyError(
        f"未知的 provider: {name!r}\n可用: {', '.join(provider_names())}"
    )
