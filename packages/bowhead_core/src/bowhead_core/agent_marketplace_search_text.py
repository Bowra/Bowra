"""Controlled localized text indexed by Agent Marketplace search.

The frontend message catalogs are contract-tested against this table so a UI
translation change cannot silently make the visible text unsearchable.
"""

from __future__ import annotations

SUPPORTED_MARKETPLACE_LOCALES = ("en-US", "zh-CN")
# System Agent names and summaries are rendered by the backend (@ list, reserved names).
SYSTEM_AGENT_TEXT_LOCALES = ("en-US", "zh-CN", "de-DE", "fr-FR")

MARKETPLACE_SEARCH_TEXT: dict[str, dict[str, str]] = {
    "cap_analysis": {"en-US": "Analyze and handle business work", "zh-CN": "分析与业务处理"},
    "cap_compute": {"en-US": "Run isolated computations", "zh-CN": "运行隔离计算"},
    "cap_data_discovery": {"en-US": "Discover data", "zh-CN": "发现数据"},
    "cap_data_query": {"en-US": "Query and analyze data", "zh-CN": "查询与分析数据"},
    "cap_document": {"en-US": "Create documents", "zh-CN": "制作文档"},
    "cap_knowledge_search": {"en-US": "Search team knowledge", "zh-CN": "检索团队知识"},
    "cap_presentation": {"en-US": "Create presentations", "zh-CN": "制作演示文稿"},
    "cap_report": {"en-US": "Write reports", "zh-CN": "撰写报告"},
    "cap_skill": {"en-US": "Apply specialist workflows", "zh-CN": "使用专业工作方法"},
    "cap_spreadsheet": {"en-US": "Create spreadsheets", "zh-CN": "制作电子表格"},
    "cap_team_read": {"en-US": "Read team systems", "zh-CN": "查询团队系统"},
    "cap_team_write": {"en-US": "Act in team systems", "zh-CN": "操作团队系统"},
    "cap_visualize": {"en-US": "Create charts", "zh-CN": "制作图表"},
    "cap_web_research": {"en-US": "Research the public web", "zh-CN": "研究公开网络"},
    "system_configuration_agent_name": {
        "en-US": "Configuration Agent",
        "zh-CN": "配置智能体",
        "de-DE": "Konfigurationsagent",
        "fr-FR": "Agent de configuration",
    },
    "system_configuration_agent_summary": {
        "en-US": "Explain and adjust supported personal and current Space settings with your permissions.",
        "zh-CN": "按你的权限解释和调整受支持的个人与当前空间设置。",
        "de-DE": "Unterstützte persönliche und aktuelle Raumeinstellungen mit Ihren Berechtigungen erklären und anpassen.",
        "fr-FR": "Expliquer et modifier les réglages personnels et de l’espace actuel pris en charge selon vos permissions.",
    },
    "system_agent_coach_name": {
        "en-US": "Coach",
        "zh-CN": "Coach",
        "de-DE": "Coach",
        "fr-FR": "Coach",
    },
    "system_agent_coach_summary": {
        "en-US": "Create and improve Agents, Skills and Plugins, and run checks before release.",
        "zh-CN": "创建和改进智能体、技能与插件，并在发布前运行检查。",
        "de-DE": "Agents, Skills und Plugins erstellen und verbessern und vor der Veröffentlichung Prüfungen ausführen.",
        "fr-FR": "Créer et améliorer des Agents, des compétences et des plugins, et lancer les vérifications avant publication.",
    },
    "system_business_analyst_name": {
        "en-US": "Business Growth Analyst",
        "zh-CN": "业务增长分析师",
        "de-DE": "Analyst für Geschäftswachstum",
        "fr-FR": "Analyste de croissance commerciale",
    },
    "system_business_analyst_summary": {
        "en-US": "Find actionable customer, channel, growth, funnel, and retention insights in business data.",
        "zh-CN": "从业务数据中分析客户、渠道、增长、漏斗与留存，找出值得行动的变化。",
        "de-DE": "Findet umsetzbare Erkenntnisse zu Kundschaft, Kanälen, Wachstum, Funnel und Bindung in Geschäftsdaten.",
        "fr-FR": "Trouvez dans les données métier des informations exploitables sur les clients, les canaux, la croissance, le parcours de conversion et la fidélisation.",
    },
    "system_compliance_analyst_name": {
        "en-US": "Compliance Analyst",
        "zh-CN": "合规分析师",
        "de-DE": "Compliance-Analyst",
        "fr-FR": "Analyste de conformité",
    },
    "system_compliance_analyst_summary": {
        "en-US": "Review policies, controls, privacy obligations, and business risks with structured findings.",
        "zh-CN": "审阅政策、内控、隐私义务与业务风险，形成结构化风险发现。",
        "de-DE": "Prüft Richtlinien, Kontrollen, Datenschutzpflichten und Geschäftsrisiken anhand strukturierter Erkenntnisse.",
        "fr-FR": "Examinez les politiques, contrôles, obligations de confidentialité et risques métier avec des constats structurés.",
    },
    "system_compute_name": {
        "en-US": "Data & Compute Analyst",
        "zh-CN": "数据与计算分析师",
        "de-DE": "Daten- und Berechnungsanalyst",
        "fr-FR": "Analyste de données et calculs",
    },
    "system_compute_summary": {
        "en-US": "Query data, run calculations, create charts, and turn the evidence into a clear analysis.",
        "zh-CN": "查询数据、运行计算、制作图表，并把证据整理成清晰的业务分析。",
        "de-DE": "Fragt Daten ab, führt Berechnungen aus, erstellt Diagramme und macht aus den Nachweisen eine klare Analyse.",
        "fr-FR": "Interrogez les données, effectuez des calculs, créez des graphiques et transformez les éléments recueillis en une analyse claire.",
    },
    "system_consultant_name": {
        "en-US": "Strategy Deck Consultant",
        "zh-CN": "战略演示顾问",
        "de-DE": "Berater für Strategiedecks",
        "fr-FR": "Consultant en présentations stratégiques",
    },
    "system_consultant_summary": {
        "en-US": "Shape complex business analysis into a structured, board-ready presentation.",
        "zh-CN": "把复杂商业分析整理成结构清楚、适合管理层评审的演示文稿。",
        "de-DE": "Formt komplexe Geschäftsanalysen zu einer strukturierten, präsentationsreifen Unterlage für die Unternehmensleitung.",
        "fr-FR": "Transformez les analyses métier complexes en une présentation structurée, prête pour le conseil d’administration.",
    },
    "system_financial_analyst_name": {
        "en-US": "Financial Analyst",
        "zh-CN": "财务分析师",
        "de-DE": "Finanzanalyst",
        "fr-FR": "Analyste financier",
    },
    "system_financial_analyst_summary": {
        "en-US": "Analyze financial statements, cash flow, key ratios, and period-over-period performance.",
        "zh-CN": "分析财务报表、现金流、关键比率与同比环比表现。",
        "de-DE": "Analysiert Finanzberichte, Cashflow, zentrale Kennzahlen und die Entwicklung über Zeiträume hinweg.",
        "fr-FR": "Analysez les états financiers, les flux de trésorerie, les ratios clés et les performances d’une période à l’autre.",
    },
    "system_kb_analyst_name": {
        "en-US": "Knowledge Researcher",
        "zh-CN": "知识研究员",
        "de-DE": "Wissensrechercheur",
        "fr-FR": "Chercheur en connaissances",
    },
    "system_kb_analyst_summary": {
        "en-US": "Research team documents, connect evidence across sources, and produce cited findings.",
        "zh-CN": "研究团队文档、连接多份来源中的证据，并产出带引用的发现。",
        "de-DE": "Recherchiert Teamdokumente, verknüpft Nachweise über Quellen hinweg und erstellt zitierte Erkenntnisse.",
        "fr-FR": "Étudiez les documents de l’équipe, reliez les éléments issus de différentes sources et produisez des constats documentés.",
    },
    "system_marketing_analyst_name": {
        "en-US": "Marketing Analyst",
        "zh-CN": "市场营销分析师",
        "de-DE": "Marketinganalyst",
        "fr-FR": "Analyste marketing",
    },
    "system_marketing_analyst_summary": {
        "en-US": "Develop marketing insights, campaign reviews, competitor research, and visual creative briefs.",
        "zh-CN": "完成营销洞察、活动复盘、竞品研究与视觉创意简报。",
        "de-DE": "Entwickelt Marketing-Erkenntnisse, Kampagnenbewertungen, Wettbewerbsrecherchen und visuelle Kreativ-Briefings.",
        "fr-FR": "Élaborez des analyses marketing, des bilans de campagnes, des études concurrentielles et des briefs créatifs visuels.",
    },
}


def system_agent_message_keys(system_name: str) -> tuple[str, str]:
    """The ``agents_marketplace`` name and summary message keys of a system Agent."""
    stem = "system_" + system_name.replace("-", "_")
    return f"{stem}_name", f"{stem}_summary"


def marketplace_search_values(*keys: str) -> tuple[str, ...]:
    """Return every controlled localized rendering for message keys.

    Capability labels carry the search locales; system Agent names and
    summaries carry all four product locales, because the @ list and reserved
    names render them too.
    """
    return tuple(value for key in keys for value in MARKETPLACE_SEARCH_TEXT[key].values())


def capability_search_values(capability_id: str) -> tuple[str, ...]:
    if capability_id not in MARKETPLACE_SEARCH_TEXT or not capability_id.startswith("cap_"):
        return ()
    return marketplace_search_values(capability_id)


def system_agent_search_values(system_name: str) -> tuple[str, ...]:
    keys = tuple(key for key in system_agent_message_keys(system_name) if key in MARKETPLACE_SEARCH_TEXT)
    return marketplace_search_values(*keys)
