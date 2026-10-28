"""external-capability-onboarding §3.1 — curated connector presets (one-click connectors).

A small, hand-maintained catalog of well-known external tools with **verified** hosted-MCP /
OpenAPI endpoints. ``GET /team-tool-sources/presets`` serves the entitlement-filtered list; the FE
``CreateToolCard`` renders it as a one-click gallery that prefills the source field.

Endpoints are external and can drift — verify before adding/changing. Entries are hosted MCP
servers: OAuth ones flow through the card's create-then-authorize path; no-auth ones preview +
enumerate directly without requiring a provider authorization round trip. Legal field combos only
(create enforces ``auth=oauth ⇒ kind=mcp``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ConnectorPreset:
    id: str
    name: str
    icon: str  # FE PD_ICONS key (git/doc/hash/card/layers/cloud/…)
    kind: str  # "openapi" | "mcp"
    auth: str  # none | bearer | api_key | basic | oauth
    blurb: str
    spec_url: str | None = None  # openapi: doc URL
    mcp_url: str | None = None  # mcp: remote endpoint

    def public_dict(self) -> dict[str, object]:
        # drop None fields so the FE gets exactly {id,name,icon,kind,auth,blurb,(spec_url|mcp_url)}
        return {k: v for k, v in asdict(self).items() if v is not None}


# Verified 2026-07-23 (hosted MCP; oauth → create-then-authorize, none → direct preview+enumerate).
CURATED_PRESETS: tuple[ConnectorPreset, ...] = (
    # GitHub's hosted MCP server accepts a Personal Access Token as a plain Bearer credential
    # (measured: no token → 401 "No access token was provided"; a syntactically-valid but bogus one →
    # 401 "Token is not authorized" — the header IS read and validated). Cataloguing it as `bearer`
    # rather than `oauth` is what makes it usable at all: its authorization server implements no
    # dynamic client registration, so the DCR-only OAuth path can never complete, and a PAT needs no
    # callback — which also means it works on a plain-HTTP deployment.
    ConnectorPreset("github", "GitHub", "git", "mcp", "bearer", "仓库 / Issue / PR 检索", mcp_url="https://api.githubcopilot.com/mcp/"),
    ConnectorPreset("notion", "Notion", "doc", "mcp", "oauth", "检索页面与数据库", mcp_url="https://mcp.notion.com/mcp"),
    ConnectorPreset("linear", "Linear", "layers", "mcp", "oauth", "Issue 与项目进度", mcp_url="https://mcp.linear.app/mcp"),
    ConnectorPreset("stripe", "Stripe", "card", "mcp", "oauth", "支付 / 客户 / 发票查询", mcp_url="https://mcp.stripe.com"),
    # No-auth hosted MCP: direct preview/enumerate without an OAuth callback. Verified 2026-07-23.
    ConnectorPreset("deepwiki", "DeepWiki", "search", "mcp", "none", "GitHub 仓库文档问答", mcp_url="https://mcp.deepwiki.com/mcp"),
    ConnectorPreset("context7", "Context7", "braces", "mcp", "none", "开源库 API 文档检索", mcp_url="https://mcp.context7.com/mcp"),
)


def presets_for(*, mcp_licensed: bool, oauth_callback_https: bool = True) -> list[dict[str, object]]:
    """Curated presets the team may actually create AND finish authorizing.

    Tool-source create/preview are all ``mcp``-feature gated, so an unlicensed team could only 422 →
    return an empty catalog (§3.1 P2-11: never advertise presets a team can't create). In saas the
    feature check is a no-op (everyone licensed), so that filter passes everything on current prod.

    advertise-execute-gap B2 extends that SAME rule from "can't create" to "can't authorize", because
    a one-click connector whose click always ends in ``OAuthRegistrationError`` is exactly the shape
    this catalog exists to avoid:

    - ``oauth_callback_https=False`` — this deployment's public URL is plain HTTP, and OAuth 2.1
      requires an HTTPS redirect_uri (loopback excepted). 116 measured with an identical dynamic
      registration body: ``http://…`` → 400 ``invalid_redirect_uri``, ``https://…`` → 201. Every
      OAuth provider rejects it, so no OAuth preset is usable until the deployment terminates TLS.
    Recoverable: terminate TLS on the deployment and the entries reappear on their own. Presets that
    need NO callback are unaffected — no-auth ones, and static-credential ones such as GitHub, whose
    hosted MCP server takes a Personal Access Token as a Bearer. That is why a plain-HTTP 116 still
    catalogues DeepWiki, Context7 and GitHub.
    """
    if not mcp_licensed:
        return []
    return [p.public_dict() for p in CURATED_PRESETS if _preset_is_usable(p, oauth_callback_https=oauth_callback_https)]


def _preset_is_usable(preset: ConnectorPreset, *, oauth_callback_https: bool) -> bool:
    # Only the OAuth handshake needs a callback; every other auth type is deployment-independent.
    return preset.auth != "oauth" or oauth_callback_https
