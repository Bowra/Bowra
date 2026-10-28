"""Serve static locale builds without changing the user's language choice.

NEXT_LOCALE selects a supported locale. With no valid cookie, serve English,
regardless of the browser's Accept-Language header. Localized builds preserve
first-render/hydration consistency; changing language writes the cookie and reloads.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Awaitable, Callable
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from bowhead_core.language_context import _normalize_accept_language
from bowhead_core.theme_surface_registry_generated import (
    CANONICAL_THEME_TOKEN_CSS,
)
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp

from bowhead_gateway.product_identity import product_name
from bowhead_gateway.public_entry_context import current_public_access_context

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send

    from bowhead_gateway.public_entry_context import PublicAccessContext

_SUPPORTED = ("zh-CN", "en-US", "de-DE", "fr-FR")
_HTML_START_TAG_RE = re.compile(r"<[A-Za-z][^<>]*>")
_ROOT_PATH_ATTRIBUTE_RE = re.compile(
    r"(?P<prefix>\b(?:action|href|poster|src)\s*=\s*[\"'])/(?!/)",
    re.IGNORECASE,
)
_ROOT_PATH_ATTRIBUTE_VALUE_RE = re.compile(
    r"(?P<prefix>\b(?:action|href|poster|src)\s*=\s*)"
    r"(?P<quote>[\"'])(?P<url>/(?!/)[^\"']*)(?P=quote)",
    re.IGNORECASE,
)
_SCRIPT_BLOCK_RE = re.compile(
    r"(?P<open><script\b[^>]*>)(?P<body>.*?)(?P<close></script\s*>)",
    re.IGNORECASE | re.DOTALL,
)
_CSS_ROOT_URL_RE = re.compile(r"(url\(\s*[\"']?)/(?!/)")
_CSS_ROOT_URL_VALUE_RE = re.compile(
    r"(?P<prefix>url\(\s*[\"']?)(?P<url>/(?!/)[^\"')\s]+)"
    r"(?P<suffix>[\"']?\s*\))",
    re.IGNORECASE,
)
_NEXT_STATIC_PREFIX = "_next/static/"
_NEXT_STATIC_CACHE_CONTROL = "private, max-age=31536000, immutable"


def frontend_static_app(app: ASGIApp) -> ASGIApp:
    """Compress the frontend mount without touching API or SSE responses."""
    return GZipMiddleware(app, minimum_size=1024, compresslevel=6)


def _static_cache_control(path: str) -> str:
    """Cache content-addressed Next assets in the browser, never a shared proxy."""
    if path.lstrip("/").startswith(_NEXT_STATIC_PREFIX):
        return _NEXT_STATIC_CACHE_CONTROL
    return "no-cache"


def _relative_static_directory_path(path: str) -> str:
    """Return a directory redirect that preserves an unknown browser prefix."""
    name = posixpath.basename(path.strip("/"))
    return f"{name}/" if name else "./"


def _rewrite_html_for_base_path(
    html: str,
    base_path: str,
    *,
    internal_path: str = "/",
    public_brand: dict[str, Any] | None = None,
) -> str:
    """Keep browser-facing static markup relative to the current document."""
    context = current_public_access_context()
    # Reverse proxies commonly remove a public URI prefix before forwarding the
    # request.  The backend therefore cannot infer that prefix from any request
    # context, including a direct or forwarded root-path context.  Relative
    # markup is the only representation that remains correct for every browser
    # entry, so do not emit prefix-derived absolute paths here.
    rewritten = _rewrite_html_markup_paths_relative(html, internal_path)
    browser_context = context.to_browser_dict() if context is not None else None
    config = _runtime_base_path_script(
        base_path,
        context.base_url if context is not None else "",
        browser_context,
        public_brand,
        internal_path=internal_path,
    )
    return rewritten.replace("<head>", f"<head>{config}", 1)


def _rewrite_html_markup_paths_relative(
    html: str,
    internal_path: str,
) -> str:
    """Make markup URLs resolve below the browser's current entry."""

    def rewrite_start_tags(fragment: str) -> str:
        def rewrite_attribute(match: re.Match[str]) -> str:
            relative = _relative_browser_url(
                match.group("url"),
                internal_path,
            )
            return f"{match.group('prefix')}{match.group('quote')}{relative}{match.group('quote')}"

        return _HTML_START_TAG_RE.sub(
            lambda match: _ROOT_PATH_ATTRIBUTE_VALUE_RE.sub(
                rewrite_attribute,
                match.group(0),
            ),
            fragment,
        )

    parts: list[str] = []
    cursor = 0
    for match in _SCRIPT_BLOCK_RE.finditer(html):
        parts.append(rewrite_start_tags(html[cursor : match.start()]))
        parts.append(rewrite_start_tags(match.group("open")))
        parts.append(match.group("body"))
        parts.append(match.group("close"))
        cursor = match.end()
    parts.append(rewrite_start_tags(html[cursor:]))
    return "".join(parts)


def _rewrite_webpack_public_path(
    script: str,
) -> str:
    """Point Next's runtime chunk loader at the browser-resolved app base.

    This script is requested from ``/_next/static/chunks/`` after a proxy may
    have stripped the browser-visible prefix.  Its ASGI request path is thus
    *not* the current HTML document path and cannot be used to form a relative
    chunk URL.  The HTML bootstrap derives the base path from ``location``
    before webpack executes, which is the only reliable browser-side anchor.
    """
    replacement = 'p=(window.__BOWHEAD_BASE_PATH__||"")+"/_next/"'
    return script.replace('p="/_next/"', replacement)


def _rewrite_css_urls_for_base_path(css: str, base_path: str) -> str:
    """Prefix root-relative CSS assets such as Next local-font URLs."""
    return _CSS_ROOT_URL_RE.sub(rf"\1{base_path}/", css)


def _rewrite_css_urls_relative(
    css: str,
    internal_path: str,
) -> str:
    """Make CSS assets resolve below the browser's current entry."""

    def rewrite_url(match: re.Match[str]) -> str:
        relative = _relative_browser_url(
            match.group("url"),
            internal_path,
        )
        return f"{match.group('prefix')}{relative}{match.group('suffix')}"

    return _CSS_ROOT_URL_VALUE_RE.sub(rewrite_url, css)


def _relative_browser_url(url: str, internal_path: str) -> str:
    parsed = urlsplit(url)
    current_directory = internal_path if internal_path.endswith("/") else f"{posixpath.dirname(internal_path)}/"
    relative_path = posixpath.relpath(
        parsed.path,
        start=current_directory,
    )
    if parsed.path.endswith("/"):
        relative_path = "./" if relative_path == "." else f"{relative_path.rstrip('/')}/"
    return urlunsplit(
        ("", "", relative_path, parsed.query, parsed.fragment),
    )


def _browser_internal_path(scope: Scope) -> str:
    raw_path = scope.get("raw_path")
    if isinstance(raw_path, bytes):
        try:
            return raw_path.decode("ascii")
        except UnicodeDecodeError:
            pass
    return str(scope.get("path") or "/")


def canonical_document_title() -> str:
    """Return the browser title for the fixed packaged product."""
    return f"{product_name()} — Verified Intelligence"


def public_document_title(public_brand: dict[str, Any] | None) -> str:
    """Use the packaged product title or preserve an injected customer name."""

    brand = public_brand.get("brand") if isinstance(public_brand, dict) else None
    name = brand.get("applicationName") if isinstance(brand, dict) else None
    if not isinstance(name, str) or not name.strip() or name.strip() == product_name():
        return canonical_document_title()
    return name


def _runtime_base_path_script(
    base_path: str,
    public_base_url: str,
    public_context: dict[str, str | None] | None,
    public_brand: dict[str, Any] | None = None,
    *,
    internal_path: str = "/",
) -> str:
    """Install the public path before Next hydrates client-side navigation.

    The proxy removes the public prefix before requests reach the gateway.
    Flight data covers Next links, but client-side history calls and plain
    anchors can still emit root-relative paths after hydration. Normalize those
    browser-visible paths here; ASGI request paths remain unchanged.
    """
    prefix = json.dumps(base_path)
    public_url = json.dumps(public_base_url)
    context = json.dumps(public_context, ensure_ascii=True, separators=(",", ":"))
    brand = json.dumps(public_brand, ensure_ascii=True, separators=(",", ":"))
    token_names = json.dumps(
        CANONICAL_THEME_TOKEN_CSS,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    route = json.dumps(internal_path)
    canonical_title = json.dumps(canonical_document_title(), ensure_ascii=True)
    brand_title = json.dumps(public_document_title(public_brand), ensure_ascii=True)
    for raw, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        prefix = prefix.replace(raw, escaped)
        public_url = public_url.replace(raw, escaped)
        context = context.replace(raw, escaped)
        brand = brand.replace(raw, escaped)
        token_names = token_names.replace(raw, escaped)
        route = route.replace(raw, escaped)
        canonical_title = canonical_title.replace(raw, escaped)
        brand_title = brand_title.replace(raw, escaped)
    meta_path = escape(base_path, quote=True)
    meta_route = escape(internal_path, quote=True)
    return f"""<meta name="bowhead-base-path" content="{meta_path}"><meta name="bowhead-internal-path" content="{meta_route}"><script>window.__BOWHEAD_PUBLIC_CONTEXT__={context};window.__BOWHEAD_PUBLIC_BRAND__={brand};window.__BOWHEAD_BASE_PATH__={prefix};window.__BOWHEAD_BASE_URL__={public_url};(()=>{{
const c=window.__BOWHEAD_PUBLIC_CONTEXT__,r={route},q=location.pathname,d=r===\"/\"?(q.endsWith(\"/\")?q.slice(0,-1):null):(q.endsWith(r)?q.slice(0,-r.length):null),p=d!==null?d:(c?.basePath??{prefix}),b=location.origin+p,same=!!c&&c.basePath===p&&c.baseUrl===b;window.__BOWHEAD_PUBLIC_CONTEXT__=same?c:{{entryId:null,teamId:null,baseUrl:b,basePath:p,matchSource:\"browser_hint\"}};window.__BOWHEAD_BASE_PATH__=p;window.__BOWHEAD_BASE_URL__=b;
const toPublic=(u)=>{{if(typeof u!==\"string\"||!u.startsWith(\"/\")||u.startsWith(\"//\"))return u;if(p&&(u===p||u.startsWith(p+\"?\")||u.startsWith(p+\"#\")))return p+\"/\"+u.slice(p.length);if(u.startsWith(p+\"/\"))return u;return u===\"/\"?p+\"/\":p+u;}};
const toPublicUrl=(u)=>{{if(u instanceof URL&&u.origin===location.origin)return new URL(toPublic(u.pathname+u.search+u.hash),u.origin);return typeof u===\"string\"?toPublic(u):u;}};
const toPublicHistoryUrl=(url)=>toPublicUrl(typeof url===\"string\"?new URL(url,location.href):url);
const ensureMetadataNode=(id,tag)=>{{let node=document.getElementById(id);if(node&&node.tagName.toLowerCase()===tag)return node;if(node)node.remove();node=document.createElement(tag);node.id=id;document.head.append(node);return node;}};
const setOptionalMetadata=(node,name,value)=>{{if(typeof value===\"string\"&&value)node.setAttribute(name,value);else node.removeAttribute(name);}};
const generatedFavicon=(value)=>{{const initial=Array.from(value||\"?\")[0].toUpperCase().replace(/[&<>\"']/g,character=>({{\"&\":\"&amp;\",\"<\":\"&lt;\",\">\":\"&gt;\",'\"':\"&quot;\",\"'\":\"&apos;\"}})[character]||character),svg='<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 64 64\"><rect width=\"64\" height=\"64\" rx=\"14\" fill=\"#161310\"/><text x=\"32\" y=\"42\" text-anchor=\"middle\" font-family=\"system-ui,sans-serif\" font-size=\"34\" font-weight=\"700\" fill=\"#FAF9F6\">'+initial+'</text></svg>';return \"data:image/svg+xml,\"+encodeURIComponent(svg);}};
const applyMetadata=(documentTitle,faviconHref,source,meta)=>{{const title=ensureMetadataNode(\"bowhead-document-title\",\"title\");for(const node of [title]){{node.setAttribute(\"data-bowhead-metadata-owned\",\"\");node.setAttribute(\"data-bowhead-metadata-owner\",\"bootstrap\");node.setAttribute(\"data-bowhead-metadata-initialized\",\"\");node.setAttribute(\"data-bowhead-metadata-source\",source);setOptionalMetadata(node,\"data-bowhead-metadata-team\",meta&&meta.id);setOptionalMetadata(node,\"data-bowhead-metadata-revision\",meta&&meta.revisionId);}}title.textContent=documentTitle;const favicon=ensureMetadataNode(\"bowhead-runtime-favicon\",\"link\");favicon.setAttribute(\"data-bowhead-metadata-owned\",\"\");favicon.setAttribute(\"data-bowhead-metadata-owner\",\"bootstrap\");favicon.setAttribute(\"data-bowhead-metadata-initialized\",\"\");favicon.setAttribute(\"data-bowhead-metadata-source\",source);setOptionalMetadata(favicon,\"data-bowhead-metadata-team\",meta&&meta.id);setOptionalMetadata(favicon,\"data-bowhead-metadata-revision\",meta&&meta.revisionId);favicon.rel=\"icon\";if(faviconHref.startsWith(\"data:image/svg+xml\")||faviconHref.endsWith(\".svg\"))favicon.type=\"image/svg+xml\";else favicon.removeAttribute(\"type\");favicon.setAttribute(\"href\",faviconHref);}};
const applyPublicBrand=()=>{{try{{const b=window.__BOWHEAD_PUBLIC_BRAND__,r=document.documentElement,e=b&&b.schemaVersion===2&&b.effective,brand=b&&b.brand,meta=b&&b.team;applyMetadata({canonical_title},toPublic(\"/bowhead-icon.svg\"),\"canonical\",null);if(!e||!brand||typeof brand.applicationName!==\"string\"||!brand.applicationName||brand.applicationName.length>80||/[\\u0000-\\u001F\\u007F\\u202A-\\u202E\\u2066-\\u2069]/.test(brand.applicationName))return;const dark=window.matchMedia&&window.matchMedia(\"(prefers-color-scheme: dark)\").matches,mode=dark?\"dark\":\"light\",scheme=e.colorScheme;r.dataset.theme=mode;r.dataset.themeMode=\"system\";r.dataset.applicationName=brand.applicationName;r.dataset.typographyProfile=e.typographyProfile.id;r.dataset.themeDensity=e.density;r.dataset.themeRadius=e.radius;r.style.colorScheme=mode;if(scheme){{const names={token_names},keys=Object.keys(names),declarations=(tokens)=>{{if(!tokens||Object.keys(tokens).length!==keys.length)return null;const out=[];for(const key of keys){{const color=tokens[key];if(typeof color!==\"string\"||!/^#[0-9A-F]{{6}}$/.test(color))return null;for(const name of names[key])out.push(name+\":\"+color);}}return out.join(\";\");}},lightCss=declarations(scheme.light),darkCss=declarations(scheme.dark);if(lightCss&&darkCss){{let style=document.getElementById(\"bowhead-team-brand-tokens\");if(style&&style.tagName!==\"STYLE\"){{style.remove();style=null;}}if(!style){{style=document.createElement(\"style\");style.id=\"bowhead-team-brand-tokens\";document.head.append(style);}}style.textContent=':root[data-theme=\"light\"]{{'+lightCss+'}}:root[data-theme=\"dark\"]{{'+darkCss+'}}';for(const vars of Object.values(names))for(const name of vars)r.style.removeProperty(name);}}}}const fav=brand.favicon,configurationType=meta&&meta.configurationType;let href=\"\";if(fav&&fav.kind===\"asset\")href=toPublic(fav.path);else if(configurationType===\"bowhead\"||configurationType===\"legacy\"||(!meta&&brand.applicationName===\"Bowhead\"))href=toPublic(\"/bowhead-icon.svg\");else if(fav&&fav.kind===\"generatedInitial\")href=generatedFavicon(fav.text);else href=generatedFavicon(brand.applicationName);applyMetadata({brand_title},href,\"public-bootstrap\",meta);}}catch(_){{}}}};
applyPublicBrand();
for(const m of [\"pushState\",\"replaceState\"]){{const original=history[m];history[m]=function(state,title,url){{return original.call(this,state,title,toPublicHistoryUrl(url));}};}}
const originalFetch=window.fetch.bind(window),toPublicRequest=(input)=>{{if(input instanceof Request){{const url=toPublicUrl(new URL(input.url));return url instanceof URL?new Request(url,input):input;}}return toPublicUrl(input);}};window.fetch=function(input,init){{const request=toPublicRequest(input),url=request instanceof Request?new URL(request.url):request instanceof URL?request:new URL(request,location.href);if(url.origin!==location.origin)return originalFetch(request,init);const headers=new Headers(init&&init.headers?init.headers:request instanceof Request?request.headers:undefined);headers.set(\"X-Bowhead-Public-Base\",window.__BOWHEAD_PUBLIC_CONTEXT__.baseUrl);return request instanceof Request?originalFetch(new Request(request,{{...(init||{{}}),headers}})):originalFetch(request,{{...(init||{{}}),headers}});}};
const createElement=document.createElement;document.createElement=function(name,options){{const element=createElement.call(this,name,options),tag=String(name).toLowerCase(),prop=tag===\"script\"?\"src\":tag===\"link\"?\"href\":null;if(!prop)return element;let prototype=Object.getPrototypeOf(element),descriptor;while(prototype&&!descriptor){{descriptor=Object.getOwnPropertyDescriptor(prototype,prop);prototype=Object.getPrototypeOf(prototype);}}if(descriptor?.set)Object.defineProperty(element,prop,{{...descriptor,set(value){{descriptor.set.call(this,toPublic(value));}}}});return element;}};
const setAttribute=Element.prototype.setAttribute;Element.prototype.setAttribute=function(name,value){{if((this instanceof HTMLScriptElement&&name===\"src\")||(this instanceof HTMLLinkElement&&name===\"href\"))value=toPublic(value);return setAttribute.call(this,name,value);}};
document.addEventListener(\"click\",event=>{{const target=event.target instanceof Element?event.target.closest(\"a[href]\"):null;if(!target||target.hasAttribute(\"download\"))return;const href=target.getAttribute(\"href\");if(href)target.setAttribute(\"href\",toPublic(href));}},true);
}})();</script>"""


class RuntimeBasePathStaticFiles(StaticFiles):
    """Static export server that applies the resolved public-entry prefix at request time."""

    def __init__(
        self,
        *,
        directory: str | Path,
        html: bool = False,
        public_brand_provider: Callable[
            [PublicAccessContext | None],
            Awaitable[dict[str, Any] | None],
        ]
        | None = None,
    ) -> None:
        super().__init__(directory=directory, html=html)
        self._public_brand_provider = public_brand_provider

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        context = current_public_access_context()
        base_path = context.base_path if context is not None else ""
        # Browser-facing static URLs and directory redirects must never depend
        # on a proxy reporting its stripped URI prefix.  Keep them relative to
        # the current document for every request context.
        internal_path = _browser_internal_path(scope)
        if 300 <= response.status_code < 400 and "location" in response.headers:
            # Starlette builds its directory-slash Location from ``scope``.  At
            # this point scope carries the reverse proxy's internal host (for
            # example deployment.example:8000), so forwarding that Location
            # leaks the port to browsers.  Browser-facing redirects must come
            # solely from the resolved public entry.  Starlette's own Location
            # preserves ``scope["query_string"]``; ours rebuilds the target from
            # ``path`` alone, so the query must be re-appended or invite/reset
            # tokens (``/invite?token=...`` → ``/invite/``) are silently dropped.
            target = _relative_static_directory_path(path)
            query_string = scope.get("query_string", b"")
            if query_string:
                target = f"{target}?{query_string.decode('latin-1')}"
            return RedirectResponse(
                target,
                status_code=response.status_code,
            )
        static_path = getattr(response, "path", None)
        if not isinstance(static_path, str):
            return response
        content_type = response.headers.get("content-type", "")
        if Path(static_path).name.startswith("webpack-") and "javascript" in content_type:
            script = Path(static_path).read_text(encoding="utf-8")
            return Response(
                _rewrite_webpack_public_path(script),
                status_code=response.status_code,
                headers={
                    "cache-control": _static_cache_control(path),
                    "content-type": content_type,
                },
            )
        if content_type.startswith("text/css"):
            css = Path(static_path).read_text(encoding="utf-8")
            return Response(
                _rewrite_css_urls_relative(css, internal_path),
                status_code=response.status_code,
                headers={
                    "cache-control": _static_cache_control(path),
                    "content-type": content_type,
                },
            )
        if not content_type.startswith("text/html"):
            if _static_cache_control(path) == _NEXT_STATIC_CACHE_CONTROL:
                response.headers["cache-control"] = _NEXT_STATIC_CACHE_CONTROL
            return response
        html = Path(static_path).read_text(encoding="utf-8")
        public_brand = None
        if self._public_brand_provider is not None:
            public_brand = await self._public_brand_provider(context)
        return HTMLResponse(
            _rewrite_html_for_base_path(
                html,
                base_path,
                internal_path=internal_path,
                public_brand=public_brand,
            ),
            status_code=response.status_code,
            headers={"cache-control": "no-cache"},
        )


def _detect_locale(scope: Scope) -> str:
    """Use an explicit cookie, then browser/system language, then English."""
    if scope.get("type") != "http":
        return "en-US"
    raw_headers = scope.get("headers", [])
    cookie_b = b""
    accept_language_b = b""
    for k, v in raw_headers:
        if k == b"cookie":
            cookie_b = v
        elif k == b"accept-language":
            accept_language_b = v

    # The cookie records an explicit interface-language preference.
    cookie_str = cookie_b.decode("latin-1", errors="replace")
    for part in cookie_str.split(";"):
        kv = part.strip().split("=", 1)
        if len(kv) == 2 and kv[0] == "NEXT_LOCALE" and kv[1] in _SUPPORTED:
            return kv[1]

    return _normalize_accept_language(accept_language_b.decode("latin-1")) or "en-US"


class LocalizedStaticFiles:
    """ASGI 应用: 按 locale 选 zh / en / de / fr 的 StaticFiles 实例 dispatch（de、fr 目录缺失时用 en）."""

    def __init__(
        self,
        *,
        zh_dir: str,
        en_dir: str,
        de_dir: str | None = None,
        fr_dir: str | None = None,
        public_brand_provider: Callable[
            [PublicAccessContext | None],
            Awaitable[dict[str, Any] | None],
        ]
        | None = None,
    ) -> None:
        # html=True returns index.html directly for / (SPA routing convention).
        self._zh = RuntimeBasePathStaticFiles(
            directory=zh_dir,
            html=True,
            public_brand_provider=public_brand_provider,
        )
        self._en = RuntimeBasePathStaticFiles(
            directory=en_dir,
            html=True,
            public_brand_provider=public_brand_provider,
        )
        self._de = (
            RuntimeBasePathStaticFiles(
                directory=de_dir,
                html=True,
                public_brand_provider=public_brand_provider,
            )
            if de_dir is not None
            else self._en  # Missing German directory (old images): use English output, never Chinese.
        )
        # Missing French static directory (old images): use English output, never Chinese.
        self._fr = (
            RuntimeBasePathStaticFiles(
                directory=fr_dir,
                html=True,
                public_brand_provider=public_brand_provider,
            )
            if fr_dir is not None
            else self._en
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            # The frontend mount ordinarily receives HTTP requests only.
            await self._en(scope, receive, send)
            return
        locale = _detect_locale(scope)
        target = {"en-US": self._en, "de-DE": self._de, "fr-FR": self._fr}.get(locale, self._zh)
        await target(scope, receive, send)
