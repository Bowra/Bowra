"""密钥脱敏的正/负双向 recall 表（批 1-c，tech-design-batch1c-secret-bypass.md §5）.

设计原稿的失败模式：把「规则能抓住密钥」当已证事实，而覆盖率从未被测量；支撑「零误伤」
的 20 条语料全是纯中文 cron 句（用错字段做否证）。本文件就是那个缺失的度量——**闸必须能变红**：
真密钥漏抓 → 正样本红；业务串误伤 → 负样本红。
"""

from __future__ import annotations

import pytest
from bowhead_core.secret_redaction import MAX_SECRET_HITS, REDACTED, extract_secrets, redact_secrets

# Construct nonfunctional credential shapes from obvious synthetic pieces.
SYNTHETIC_GITHUB_TOKEN = "ghp_" + "0123456789abcdef" * 3
SYNTHETIC_GITHUB_PAT = "github_pat_" + "Abcdef0123456789_" * 4
SYNTHETIC_AWS_ACCESS_KEY = "AKIA" + "EXAMPLE1234567890"
PRIVATE_KEY_LABEL = "RSA PRIVATE KEY"
SYNTHETIC_PRIVATE_KEY = f"-----BEGIN {PRIVATE_KEY_LABEL}-----\nsynthetic-key-body\n-----END {PRIVATE_KEY_LABEL}-----"

# ── Positive samples: real token shapes (miss = iron rule 4 violation) ─────────
POSITIVES = [
    ("openai", "OPENAI_API_KEY=sk-Xy8Kd0LmQr3TvWzA1B2C3D4E5F6G7H8I9J0KaLbMcNdOePf"),
    ("anthropic", "ANTHROPIC_API_KEY=sk-ant-api03-abc123XYZ_def456-ghi789jklMNOpqrstuvwx"),
    ("github", f"GITHUB_TOKEN={SYNTHETIC_GITHUB_TOKEN}"),
    ("github_pat", f"GITHUB_PAT={SYNTHETIC_GITHUB_PAT}"),
    ("aws_access_key_id", f"AWS_ACCESS_KEY_ID={SYNTHETIC_AWS_ACCESS_KEY}"),  # 20 characters; relies on label rule
    ("aws_secret", "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    ("google", "GOOGLE_API_KEY=AIzaSyD-1234567890abcdefghijklmnopqrstu"),
    ("feishu_short", "FEISHU_APP_SECRET=cli_a1b2c3d4e5f6g7h8"),  # 19 characters
    ("human_password", "DB_PASSWORD=hunter2"),  # 7 characters, low entropy
    ("short_api_key", "API_KEY=abc123def456ghi7"),  # 16 characters
    ("case_api_key", '{"case_api_key":"synthetic-credential"}'),
    ("dsn_password", "DATABASE_URL=postgres://admin:Sup3rS3cretPw@db:5432/app"),
    ("url_userinfo", "连接串 https://alice:P@ssw0rd123@api.example.com/v1"),
    ("bearer", "curl -H 'Authorization: Bearer sk-FAKE1234567890abcdefFAKE9d0e1234'"),
    ("json_env", '{"mcpServers":{"gh":{"env":{"GITHUB_TOKEN":"' + SYNTHETIC_GITHUB_TOKEN + '"}}}}'),
    ("bare_token", "把 sk-FAKE1234567890abcdefFAKE9d0e1234abcd 配进环境变量"),
    ("private_key", SYNTHETIC_PRIVATE_KEY),
]

# ── Negative samples: typical product business prose (false positives claim the user's table name is a secret) ──
NEGATIVES = [
    ("sql_table", "SELECT * FROM ads_marketing_channel_conversion_summary_v2"),
    ("cn_table", "从 datarc 的 ads_user_behavior_funnel_conversion_d 表取数"),
    ("uuid", "数据集 id 是 8f14e45f-ceea-467a-9a3e-8d4a34f4a6d0"),
    ("plain_url", "抓取 https://demo.datarc.cn/api/v1/datasets/marketing_overview_daily"),
    ("azure_base_url", "base_url 是 https://my-company-prod.openai.azure.com/openai/deployments/gpt4o/chat"),
    ("org_id", "organization=org-AbCdEfGhIjKlMnOpQrStUvWx"),
    ("deployment", "deployment_name=gpt-4o-mini-2024-07-18-eastus-prod"),
    ("parquet_path", "路径 /data/warehouse/prod/dt=2026-07-31/part-00000.parquet"),
    ("cron_cn", "每月1号早上10点出上个月的经营月报，发给运营组"),
    ("chinese_query", "汇总上周各渠道销售额与同比环比，按品类拆解"),
]

# ── **Business keys** containing secret substrings: unbounded matching erases them all (implementation review: 22/22 false positives) ──
# Among this product's most frequent keys (BI *_key, LLM *_tokens, connection-registry connection_*).
BUSINESS_KEY_NAMES = [
    ("primary_key", "primary_key=user_id"),
    ("partition_key", "partition_key=dt"),
    ("sort_key", "sort_key=created_at"),
    ("group_key", "group_key=channel"),
    ("case_key", '{"case_key":"quality.empty"}'),
    ("keyword", "keyword=双十一大促"),
    ("key_metrics", "key_metrics=gmv,uv,conversion_rate"),
    ("connection_name", "connection_name=datarc_prod_readonly_replica_cluster"),
    ("connection_timeout", "connection_timeout=30"),
    ("max_tokens", "max_tokens=4096"),
    ("prompt_tokens", "prompt_tokens=812"),
    ("completion_tokens", "completion_tokens=433"),
    ("token_count", "token_count=1200"),
    ("author_name", "author_name=Allen Liu"),
    ("auth_mode", "auth_mode=oauth"),
    ("monkey_patch", "monkey_patch=false"),
    ("secret_name", "secret_name=OPENAI_API_KEY"),  # Exactly the key 1-d needs the model to see would be erased
    ("json_keyword", '{"keyword": "双十一"}'),
]

# ── Secrets mid-sentence/in lists, not line-start (AiFillPanel is freeform, not an .env file) ──
MID_SENTENCE_POSITIVES = [
    ("中文句中", "连接串是 DB_PASSWORD=hunter2 记得配上", "hunter2"),
    ("英文句中", "please set DB_PASSWORD=hunter2 on the server", "hunter2"),
    ("一行两个KV", "API_KEY=hunter2 DB_PASSWORD=swordfish", "swordfish"),
    ("markdown 短横", "- password: hunter2", "hunter2"),
    ("markdown 星号", "* password: hunter2", "hunter2"),
    ("中文顿号后", "配置项、api_key=sk-FAKE1234567890abcdefFAKE9d0e1234abcd", "sk-FAKE1234567890abcdefFAKE9d0e1234abcd"),
    ("中文括号内", "（token=abc123def456ghi7）", "abc123def456ghi7"),
]

# Indistinguishable from real tokens; residual false positives accepted in review (explicitly recorded, never silently ignored).
KNOWN_FALSE_POSITIVES = [
    ("git_sha", "commit e9324d6b8f1a2c3d4e5f60718293a4b5c6d7e8f9"),
    ("kb_doc_id", "文档 kbf_9f2a1c8e7b6d5a4f3e2d1c0b9a8f7e6d"),
    ("npm_scope", "装 @modelcontextprotocol/server-filesystem 这个包"),
]


@pytest.mark.parametrize(("name", "text"), POSITIVES, ids=[n for n, _ in POSITIVES])
def test_positive_recall(name: str, text: str) -> None:
    """真密钥必须命中，且原值不得残留在脱敏文本里。"""
    redacted, hits = extract_secrets(text)
    assert hits, f"{name}: 漏抓——该形态会明文进 prompt"
    for hit in hits:
        assert hit.value not in redacted, f"{name}: hit 与脱敏文本不同源（原值残留）"


@pytest.mark.parametrize(("name", "text"), NEGATIVES, ids=[n for n, _ in NEGATIVES])
def test_negative_no_false_positive(name: str, text: str) -> None:
    """业务描述不得被当密钥——误伤会让模型看不到表名/base_url，预填质量塌方。"""
    _, hits = extract_secrets(text)
    assert not hits, f"{name}: 误伤 {[h.value for h in hits]}"


@pytest.mark.parametrize(("name", "text"), KNOWN_FALSE_POSITIVES, ids=[n for n, _ in KNOWN_FALSE_POSITIVES])
@pytest.mark.xfail(reason="形状与真 token 不可分，review 判定接受；挂账而非无声放过", strict=True)
def test_known_false_positives_accounted(name: str, text: str) -> None:
    _, hits = extract_secrets(text)
    assert not hits


def test_kv_label_preserved() -> None:
    """`KEY=value` 形态必须保住键名——模型要靠它把 [REDACTED] 映射到表单字段（review P1-1）。"""
    redacted, hits = extract_secrets("API_KEY=sk-FAKE1234567890abcdefFAKE9d0e1234")
    assert redacted == f"API_KEY={REDACTED}"
    assert hits[0].label == "API_KEY"


def test_jwt_is_one_whole_hit() -> None:
    """JWT 必须整串一个 hit：切碎既让中段 claims 明文残留，也让旁路值不可直填（review P0-1/P2-5）。"""
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    redacted, hits = extract_secrets(f"SUPABASE_KEY={jwt}")
    assert len(hits) == 1
    assert hits[0].value == jwt
    assert "eyJzdWIi" not in redacted  # No middle-segment residue


def test_url_userinfo_keeps_username() -> None:
    redacted, hits = extract_secrets("https://alice:P@ssw0rd123@api.example.com")
    assert "alice" in redacted and "P@ssw0rd123" not in redacted
    assert hits[0].kind == "url_userinfo"


def test_labels_by_kind() -> None:
    _, kv = extract_secrets(f"GITHUB_TOKEN={SYNTHETIC_GITHUB_TOKEN}")
    assert (kv[0].label, kv[0].kind) == ("GITHUB_TOKEN", "kv")
    _, js = extract_secrets('{"env":{"API_KEY":"sk-FAKE1234567890abcdefFAKE9d0e1234"}}')
    assert (js[0].label, js[0].kind) == ("API_KEY", "json")
    # Authorization key takes KV path (key retained, whole credential one hit); bare bearer takes R6.
    _, hdr = extract_secrets("curl -H 'Authorization: Bearer sk-FAKE1234567890abcdefFAKE9d0e1234'")
    assert (hdr[0].label, hdr[0].kind) == ("Authorization", "kv")
    assert hdr[0].value == "Bearer sk-FAKE1234567890abcdefFAKE9d0e1234"
    _, br = extract_secrets("请求头里写 Bearer sk-FAKE1234567890abcdefFAKE9d0e1234 就能调通")
    assert (br[0].label, br[0].kind) == ("authorization", "bearer")


def test_auth_header_line_is_one_credential() -> None:
    """`Authorization: Bearer xxx` 整行一个 hit——方案词单独成 hit 是废值，旁路直填会填进 "Bearer"。"""
    redacted, hits = extract_secrets("Authorization: Bearer sk-FAKE1234567890abcdefFAKE9d0e1234")
    assert len(hits) == 1
    assert hits[0].value == "Bearer sk-FAKE1234567890abcdefFAKE9d0e1234"
    assert redacted == f"Authorization: {REDACTED}"


def test_dedupe_and_cap() -> None:
    token = "sk-FAKE1234567890abcdefFAKE9d0e1234abcd"
    _, hits = extract_secrets(f"A_KEY={token}\nB_KEY={token}")
    assert len(hits) == 1  # Deduplicate identical values
    many = "\n".join(f"K{i}_TOKEN=sk-FAKE{i:04d}567890abcdefFAKE9d0e1234abcd" for i in range(30))
    _, capped = extract_secrets(many)
    assert len(capped) == 20  # MAX_SECRET_HITS


def test_cap_truncates_hits_but_never_redaction() -> None:
    """上限只截 hits 列表，脱敏必须覆盖全部 span。

    截断若同时截脱敏，第 21 条起就明文进 prompt——而 hits 只有 20 条会让人以为「都处理了」。
    30 条密钥的粘贴不是构造场景：一份 .env 或 claude_desktop_config.json 轻松超过 20。
    """
    tokens = [f"sk-FAKE{i:04d}567890abcdefFAKE9d0e1234abcd" for i in range(30)]
    redacted, hits = extract_secrets("\n".join(f"K{i}_TOKEN={token}" for i, token in enumerate(tokens)))
    assert len(hits) == MAX_SECRET_HITS
    assert not [token for token in tokens if token in redacted], "有密钥越过上限明文残留"
    assert redacted.count(REDACTED) == len(tokens)


def test_same_source_guarantee() -> None:
    """extract 与旧签名同源（review §2「脱敏文本与 hits 必须同源」）。"""
    text = "OPENAI_API_KEY=sk-Xy8Kd0LmQr3TvWzA1B2C3D4E5F6G7H8I9J0KaLbMcNdOePf\n表 ads_daily_summary"
    redacted, hits = extract_secrets(text)
    assert redacted == redact_secrets(text)[0]
    assert redact_secrets(text)[1] is bool(hits)


def test_empty_and_clean_text_unchanged() -> None:
    assert extract_secrets("") == ("", [])
    clean = "每周一早上九点出销售周报"
    redacted, hits = extract_secrets(clean)
    assert redacted == clean and hits == []


@pytest.mark.parametrize(("name", "text"), BUSINESS_KEY_NAMES, ids=[n for n, _ in BUSINESS_KEY_NAMES])
def test_business_key_names_not_treated_as_secret(name: str, text: str) -> None:
    """密钥词表必须词级匹配：`primary_key`/`max_tokens`/`author_name` 不是密钥。

    子串匹配下这 17 条全部被抹——模型看不见表主键、看不见 token 预算、看不见连接名，
    预填质量塌方；`author_name=Allen Liu` 甚至只抹半个词，输出破碎。
    """
    _, hits = extract_secrets(text)
    assert not hits, f"{name}: 业务键名被当密钥 {[h.value for h in hits]}"


@pytest.mark.parametrize(("name", "text", "secret"), MID_SENTENCE_POSITIVES, ids=[n for n, _, _ in MID_SENTENCE_POSITIVES])
def test_secret_caught_mid_sentence(name: str, text: str, secret: str) -> None:
    """描述框里的密钥不在行首——行首锚会让 headline 案例在主路径上整类漏抓。"""
    redacted, hits = extract_secrets(text)
    assert hits, f"{name}: 漏抓"
    assert secret not in redacted, f"{name}: 原值残留 {redacted!r}"


def test_key_name_semantics() -> None:
    """键名判定：默认从宽（漏抓破铁律 4），只在实测支撑的业务形状上放行。

    R2 逮到「无边界子串」误伤 22 条业务键名；R3 又逮到「末词精确匹配」矫枉过正，
    PGPASSWORD / accessKeySecret / SECRET_KEY_BASE / key1 等 25 条真实厂商键名反而漏抓。
    两个方向都要有闸，否则修一头必崩另一头。
    """
    from bowhead_core.secret_redaction import _is_secret_key

    # Genuine vendor keys: abbreviations, camelCase, version suffixes, format suffixes.
    for name in (
        "api_key",
        "API_KEY",
        "client_secret",
        "DB_PASSWORD",
        "access_token",
        "AWS_ACCESS_KEY_ID",
        "apiKey",
        "accessKeySecret",
        "AccessKeyId",
        "secretKey",
        "apiToken",
        "PGPASSWORD",
        "MYSQL_PWD",
        "SECRET_KEY_BASE",
        "credentials_json",
        "private_key_pem",
        "OPENAI_API_KEY_V2",
        "key1",
        "password2",
        "db_pass",
        "GITHUB_PAT",
        "STRIPE_SK",
        "x-api-key",
        "X-Auth-Token",
        "webhook_secret",
        "encryption_key",
        "passphrase",
        "DATABASE_DSN",
    ):
        assert _is_secret_key(name), f"漏抓真实凭据键名 {name}"

    # Business keys: lookalikes / business suffixes / structure and count qualifiers.
    for name in (
        "primary_key",
        "partition_key",
        "sort_key",
        "group_key",
        "key_metrics",
        "key_columns",
        "keyword",
        "keyspace",
        "monkey_patch",
        "author_name",
        "authority",
        "authentication_provider",
        "connection_name",
        "connection_timeout",
        "connector_type",
        "tokenizer",
        "secret_name",
        "token_count",
        "auth_mode",
        "max_tokens",
        "prompt_tokens",
        "client_id",
        # camelCase forms: without splitting, one long word defeats all business exceptions (splitting is load-bearing **for exemptions**,
        # less relevant to recall with broad substring matching, so place the guard on this side).
        "primaryKey",
        "partitionKey",
        "maxTokens",
        "tokenCount",
        "authorName",
        "connectionName",
        "keyMetrics",
    ):
        assert not _is_secret_key(name), f"业务键名被当密钥 {name}"

    # Bare `key` is ambiguous (`WHERE key='gmv'`, `分组 key: 渠道维度`); token tests handle genuinely credential-valued cases.
    assert not _is_secret_key("key")


def test_no_false_positive_in_prose_and_sql() -> None:
    """R3-P1-2：放宽行内边界后，中文行文与 SQL 是新的误伤面。"""
    for text in (
        "分组 key: 渠道维度",
        "SELECT * FROM dim WHERE key='gmv'",
        "按 keyword 分组统计，token 消耗控制在 max_tokens=4096 以内",
        "字段 password_hint 是提示不是口令",
    ):
        _, hits = extract_secrets(text)
        assert not hits, f"行文误伤 {text!r} → {[h.value for h in hits]}"


def test_url_query_and_comma_separated_kv() -> None:
    """R3-P1-3：边界表与 value 组必须自洽——`?`/`&`/`,` 分隔的 KV 之前整类漏抓。"""
    redacted, hits = extract_secrets("https://api.x.com/v1?api_key=hunter2&mode=fast")
    assert "hunter2" not in redacted and hits[0].label == "api_key"
    assert "mode=fast" in redacted  # Redact only credentials; retain subsequent parameters
    redacted2, _ = extract_secrets("a=1,password=hunter2")
    assert redacted2 == f"a=1,password={REDACTED}"


def test_cjk_value_never_treated_as_credential() -> None:
    """值含中日韩字符时一定不是凭据——这是行文误伤的整类闸。"""
    _, hits = extract_secrets("token: 渠道维度汇总")
    assert not hits
