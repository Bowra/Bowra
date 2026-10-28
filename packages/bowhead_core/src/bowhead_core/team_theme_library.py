"""Built-in palettes and render payloads shared by both editions."""

from __future__ import annotations

from typing import Any, Final

from bowhead_core.theme_compiler import compile_theme_scheme

# Built-in palettes put the current product default first.
TEAM_THEME_PALETTE_REGISTRY_V1: Final[tuple[dict[str, Any], ...]] = (
    {
        "id": "saas-blue",
        "label": {"zh-CN": "专业蓝", "en-US": "SaaS Blue", "de-DE": "SaaS-Blau", "fr-FR": "Bleu SaaS"},
        "source": {
            "version": 2,
            "configurationType": "custom",
            "presetId": None,
            "primarySeed": "#2563EB",
            "neutralSeed": "#64748B",
            "backgroundStyle": "custom",
            "overrides": {
                "light": {"background": "#F4F7FB", "surface": "#FFFFFF"},
                "dark": {"background": "#0F172A", "surface": "#172033"},
            },
        },
        "defaults": {},
    },
    {
        "id": "bowhead-editorial",
        "label": {"zh-CN": "Bowhead 编辑", "en-US": "Bowhead Editorial", "de-DE": "Bowhead Redaktion", "fr-FR": "Bowhead éditorial"},
        "source": {
            "version": 2,
            "configurationType": "custom",
            "presetId": None,
            "primarySeed": "#B04A2A",
            "neutralSeed": "#8A8170",
            "backgroundStyle": "custom",
            "overrides": {
                "light": {"background": "#F7F2E8", "surface": "#FFFFFF"},
                "dark": {"background": "#17130F", "surface": "#211B16"},
            },
        },
        "defaults": {},
    },
    {
        "id": "product-indigo",
        "label": {"zh-CN": "产品靛蓝", "en-US": "Product Indigo", "de-DE": "Produkt-Indigo", "fr-FR": "Indigo produit"},
        "source": {
            "version": 2,
            "configurationType": "custom",
            "presetId": None,
            "primarySeed": "#635BFF",
            "neutralSeed": "#667085",
            "backgroundStyle": "custom",
            "overrides": {
                "light": {"background": "#F2F4FA", "surface": "#FFFFFF"},
                "dark": {"background": "#101323", "surface": "#181B31"},
            },
        },
        "defaults": {},
    },
    {
        "id": "collaboration-teal",
        "label": {"zh-CN": "协作青", "en-US": "Collaboration Teal", "de-DE": "Zusammenarbeit in Petrol", "fr-FR": "Bleu sarcelle collaboratif"},
        "source": {
            "version": 2,
            "configurationType": "custom",
            "presetId": None,
            "primarySeed": "#0F766E",
            "neutralSeed": "#667572",
            "backgroundStyle": "custom",
            "overrides": {
                "light": {"background": "#F0FDFA", "surface": "#FFFFFF"},
                "dark": {"background": "#0B1F1D", "surface": "#132B28"},
            },
        },
        "defaults": {},
    },
    {
        "id": "focus-violet",
        "label": {"zh-CN": "专注紫", "en-US": "Focus Violet", "de-DE": "Fokus-Violett", "fr-FR": "Violet concentration"},
        "source": {
            "version": 2,
            "configurationType": "custom",
            "presetId": None,
            "primarySeed": "#7C3AED",
            "neutralSeed": "#756B80",
            "backgroundStyle": "custom",
            "overrides": {
                "light": {"background": "#FAF5FF", "surface": "#FFFFFF"},
                "dark": {"background": "#171126", "surface": "#211932"},
            },
        },
        "defaults": {},
    },
    {
        "id": "neutral-graphite",
        "label": {"zh-CN": "中性石墨", "en-US": "Neutral Graphite", "de-DE": "Neutrales Graphit", "fr-FR": "Graphite neutre"},
        "source": {
            "version": 2,
            "configurationType": "custom",
            "presetId": None,
            "primarySeed": "#4F5B5C",
            "neutralSeed": "#727876",
            "backgroundStyle": "custom",
            "overrides": {
                "light": {"background": "#F7F7F5", "surface": "#FFFFFF"},
                "dark": {"background": "#141617", "surface": "#1D2120"},
            },
        },
        "defaults": {},
    },
)


def team_theme_palette_payload() -> list[dict[str, Any]]:
    return [theme_palette_payload(item, builtin=True) for item in TEAM_THEME_PALETTE_REGISTRY_V1]


def theme_palette_payload(item: dict[str, Any], *, builtin: bool) -> dict[str, Any]:
    compiled = compile_theme_scheme(item["source"])
    payload = {
        "id": item["id"],
        "name": item.get("name"),
        "label": item.get("label"),
        "source": item["source"],
        "defaults": item["defaults"],
        "builtin": builtin,
        "swatch": {mode: {key: compiled[mode][key] for key in ("background", "surface", "accent", "accentInk")} for mode in ("light", "dark")},
    }
    if not builtin:
        payload.update({key: item[key] for key in ("createdBy", "createdAt", "updatedBy", "updatedAt")})
    return payload
