"""Built-in palette contracts stay available in Community."""

from bowhead_core.team_theme_library import TEAM_THEME_PALETTE_REGISTRY_V1, team_theme_palette_payload
from bowhead_core.theme_compiler import compile_theme_scheme, product_default_compiled_scheme


def test_builtin_palettes_compile_and_expose_both_modes() -> None:
    palettes = team_theme_palette_payload()
    assert [item["id"] for item in palettes] == [
        "saas-blue",
        "bowhead-editorial",
        "product-indigo",
        "collaboration-teal",
        "focus-violet",
        "neutral-graphite",
    ]
    assert all(item["builtin"] for item in palettes)
    expected_names = {
        "de-DE": ["SaaS-Blau", "Bowhead Redaktion", "Produkt-Indigo", "Zusammenarbeit in Petrol", "Fokus-Violett", "Neutrales Graphit"],
        "fr-FR": ["Bleu SaaS", "Bowhead éditorial", "Indigo produit", "Bleu sarcelle collaboratif", "Violet concentration", "Graphite neutre"],
    }
    for locale, names in expected_names.items():
        assert [item["label"][locale] for item in palettes] == names

    assert all(item["swatch"]["light"]["accentInk"].startswith("#") for item in palettes)
    assert all(item["swatch"]["dark"]["background"].startswith("#") for item in palettes)


def test_saas_blue_builtin_matches_the_product_default_scheme() -> None:
    """Picking SaaS Blue and making no color choice render the same 24 tokens in both modes."""

    saas_blue = next(item for item in TEAM_THEME_PALETTE_REGISTRY_V1 if item["id"] == "saas-blue")
    picked = compile_theme_scheme(saas_blue["source"])
    default = product_default_compiled_scheme()
    for mode in ("light", "dark"):
        assert picked[mode] == default[mode]
    assert TEAM_THEME_PALETTE_REGISTRY_V1[0]["id"] == "saas-blue"
