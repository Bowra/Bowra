"""Reviewed user-facing listing descriptions for built-in Skills, keyed by Skill name.

Pickers, the Skills page and the Capability Center show this text in the interface
language. It lives outside the Skill bundles on purpose: SKILL.md bytes, and therefore
each built-in bundle revision that released Agents pin, never change for display copy.
The model reads the frontmatter ``description`` and the body, never this text.
"""

from __future__ import annotations

BUILTIN_LISTING_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "agent-authoring": {
        "en-US": "Helps set up or change an Agent for you: it finds the resources the Agent can use and prepares changes you review before saving. Pick it for Agents that build or improve other Agents.",
        "zh-CN": "帮你创建或修改智能体：查找智能体可用的资源，并准备好修改供你确认后再保存。适合用来搭建或改进其他智能体的智能体。",
        "de-DE": "Hilft beim Einrichten oder Ändern eines Agenten: Er findet die Ressourcen, die der Agent nutzen kann, und bereitet Änderungen vor, die Sie vor dem Speichern prüfen. Wählen Sie ihn für Agenten, die andere Agenten erstellen oder verbessern.",
        "fr-FR": "Aide à créer ou modifier un Agent : il trouve les ressources que l’Agent peut utiliser et prépare des modifications que vous validez avant l’enregistrement. Choisissez-le pour les Agents qui créent ou améliorent d’autres Agents.",
    },
    "analyst-framework": {
        "en-US": "A step-by-step method for complex analysis: understand the data, break the question down, find the causes and then quantify them. Pick it for attribution, breakdowns across several dimensions or derived metrics.",
        "zh-CN": "复杂分析的通用方法：先摸清数据，再拆解问题、找出原因并量化。适合做归因、多维拆解或推导衍生指标时选用。",
        "de-DE": "Eine schrittweise Methode für komplexe Analysen: Daten verstehen, die Frage zerlegen, Ursachen finden und sie beziffern. Wählen Sie ihn für Ursachenanalysen, Aufschlüsselungen nach mehreren Dimensionen oder abgeleitete Kennzahlen.",
        "fr-FR": "Une méthode pas à pas pour les analyses complexes : comprendre les données, décomposer la question, trouver les causes puis les chiffrer. Choisissez-le pour les analyses d’attribution, les ventilations sur plusieurs dimensions ou les indicateurs dérivés.",
    },
    "analyst-method-cohort-retention": {
        "en-US": "Analyzes how well new customers stay over time and how the revenue they bring holds up. Pick it for questions about retention rates, churn or customer loyalty.",
        "zh-CN": "分析新客户随时间的留存情况，以及他们带来的收入如何变化。适合回答留存率、流失原因或用户粘性等问题。",
        "de-DE": "Analysiert, wie gut neue Kunden im Zeitverlauf bleiben und wie sich der Umsatz mit ihnen hält. Wählen Sie ihn für Fragen zu Bindungsraten, Abwanderung oder Kundentreue.",
        "fr-FR": "Analyse la fidélité des nouveaux clients dans le temps et l’évolution du chiffre d’affaires qu’ils génèrent. Choisissez-le pour les questions de rétention, d’attrition ou de fidélité.",
    },
    "analyst-method-contract-risk": {
        "en-US": "Reviews contracts and acquisition agreements for risky or missing terms, such as liabilities, non-compete clauses and closing conditions. Pick it for due diligence or contract review.",
        "zh-CN": "检查合同和并购协议中有风险或缺失的条款，如或有负债、竞业限制和交割条件。适合做尽职调查或合同审查时选用。",
        "de-DE": "Prüft Verträge und Übernahmevereinbarungen auf riskante oder fehlende Klauseln, etwa Haftung, Wettbewerbsverbote und Abschlussbedingungen. Wählen Sie ihn für Due-Diligence- oder Vertragsprüfungen.",
        "fr-FR": "Examine les contrats et accords d’acquisition pour repérer les clauses risquées ou manquantes, comme les passifs, la non-concurrence et les conditions de réalisation. Choisissez-le pour un audit préalable ou une revue de contrat.",
    },
    "analyst-method-customer-structure": {
        "en-US": "Shows how much you depend on a few large customers, groups customers by value and finds the ones you lose money on. Pick it for questions about customer concentration or customer mix.",
        "zh-CN": "看业务对少数大客户的依赖程度，按价值给客户分层，并找出不赚钱的客户。适合分析客户集中度或客户结构时选用。",
        "de-DE": "Zeigt, wie stark Sie von wenigen Großkunden abhängen, gliedert Kunden nach Wert und findet die, mit denen Sie Verlust machen. Wählen Sie ihn für Fragen zu Kundenkonzentration oder Kundenstruktur.",
        "fr-FR": "Mesure votre dépendance à quelques grands clients, segmente les clients par valeur et repère ceux qui ne sont pas rentables. Choisissez-le pour les questions de concentration ou de structure de la clientèle.",
    },
    "analyst-method-data-compliance": {
        "en-US": "Checks how personal data is collected, used and transferred against privacy rules such as PIPL and GDPR. Pick it for privacy audits or data compliance questions.",
        "zh-CN": "对照《个人信息保护法》、GDPR 等隐私法规，检查个人数据的收集、使用和跨境传输。适合做隐私审计或数据合规评估时选用。",
        "de-DE": "Prüft Erhebung, Nutzung und Übermittlung personenbezogener Daten anhand von Datenschutzregeln wie PIPL und DSGVO. Wählen Sie ihn für Datenschutzprüfungen oder Fragen zur Daten-Compliance.",
        "fr-FR": "Vérifie la collecte, l’usage et le transfert des données personnelles au regard de règles comme la PIPL et le RGPD. Choisissez-le pour un audit de confidentialité ou une question de conformité des données.",
    },
    "analyst-method-earnings-quality": {
        "en-US": "Checks whether reported profit is backed by real cash and spots one-off gains or unusual accounting. Pick it when profit grows but cash does not, or receivables jump.",
        "zh-CN": "检查账面利润有没有真实现金支撑，识别一次性收益和异常的会计处理。适合利润增长但现金没跟上、应收账款暴增时选用。",
        "de-DE": "Prüft, ob der ausgewiesene Gewinn durch echte Zahlungsmittel gedeckt ist, und erkennt Einmaleffekte oder ungewöhnliche Bilanzierung. Wählen Sie ihn, wenn der Gewinn steigt, der Cashflow aber nicht, oder die Forderungen sprunghaft wachsen.",
        "fr-FR": "Vérifie si le bénéfice publié est adossé à de la trésorerie réelle et repère les gains ponctuels ou les traitements comptables inhabituels. Choisissez-le quand le bénéfice progresse sans la trésorerie ou que les créances bondissent.",
    },
    "analyst-method-funnel-growth": {
        "en-US": "Breaks revenue growth down by funnel step, new versus lost customers, and price, volume and product mix. Pick it to find where growth comes from or which conversion step is weakest.",
        "zh-CN": "把营收增长拆到转化漏斗的各个环节、新增与流失客户，以及价格、销量和产品结构。适合找增长来源或最薄弱的转化环节时选用。",
        "de-DE": "Zerlegt Umsatzwachstum nach Trichterstufen, neuen und verlorenen Kunden sowie Preis, Menge und Produktmix. Wählen Sie ihn, um Wachstumsquellen oder die schwächste Konversionsstufe zu finden.",
        "fr-FR": "Décompose la croissance du chiffre d’affaires par étape du tunnel, clients gagnés et perdus, prix, volume et mix produit. Choisissez-le pour trouver l’origine de la croissance ou l’étape de conversion la plus faible.",
    },
    "analyst-method-internal-control-aml": {
        "en-US": "Reviews internal controls and anti-money-laundering checks, such as separation of duties, customer identity checks and suspicious transaction reporting. Pick it to find compliance gaps or prepare for a regulatory audit.",
        "zh-CN": "审查内部控制和反洗钱措施，如职责分离、客户身份核验和可疑交易报告。适合排查合规缺口或准备监管审计时选用。",
        "de-DE": "Prüft interne Kontrollen und Geldwäscheprävention, etwa Funktionstrennung, Identitätsprüfung von Kunden und Meldung verdächtiger Transaktionen. Wählen Sie ihn, um Compliance-Lücken zu finden oder eine aufsichtsrechtliche Prüfung vorzubereiten.",
        "fr-FR": "Examine le contrôle interne et la lutte contre le blanchiment, comme la séparation des tâches, la vérification d’identité des clients et la déclaration des opérations suspectes. Choisissez-le pour repérer des lacunes de conformité ou préparer un audit réglementaire.",
    },
    "analyst-method-roe-dupont": {
        "en-US": "Breaks return on equity into margin, asset turnover and leverage, and checks whether returns really beat the cost of capital. Pick it when return on equity looks high or may be inflated by debt.",
        "zh-CN": "把净资产收益率拆成利润率、资产周转率和杠杆，并判断回报是否真正高于资本成本。适合净资产收益率看起来偏高、可能靠负债撑起时选用。",
        "de-DE": "Zerlegt die Eigenkapitalrendite in Marge, Kapitalumschlag und Verschuldung und prüft, ob die Rendite die Kapitalkosten wirklich übersteigt. Wählen Sie ihn, wenn die Eigenkapitalrendite hoch wirkt oder durch Schulden aufgebläht sein könnte.",
        "fr-FR": "Décompose la rentabilité des capitaux propres en marge, rotation des actifs et levier, et vérifie si le rendement dépasse vraiment le coût du capital. Choisissez-le quand cette rentabilité paraît élevée ou gonflée par l’endettement.",
    },
    "analyst-method-solvency-cashflow": {
        "en-US": "Checks whether a company can pay its debts and whether its cash flow is sustainable, using liquidity, leverage and interest coverage. Pick it for questions about short-term debt, liquidity or running out of cash.",
        "zh-CN": "通过流动性、杠杆和利息保障等指标，判断企业能否偿还债务、现金流是否可持续。适合分析短期偿债、流动性或现金紧张问题时选用。",
        "de-DE": "Prüft anhand von Liquidität, Verschuldung und Zinsdeckung, ob ein Unternehmen seine Schulden bedienen kann und ob sein Cashflow tragfähig ist. Wählen Sie ihn für Fragen zu kurzfristigen Schulden, Liquidität oder Zahlungsengpässen.",
        "fr-FR": "Vérifie, à l’aide de la liquidité, du levier et de la couverture des intérêts, si une entreprise peut honorer ses dettes et si sa trésorerie est durable. Choisissez-le pour les questions de dette à court terme, de liquidité ou de manque de trésorerie.",
    },
    "analyst-method-unit-economics": {
        "en-US": "Compares what a customer is worth over time with what it costs to win them, and how long it takes to earn that back. Pick it to compare channels or check whether growth pays off.",
        "zh-CN": "比较客户生命周期价值与获客成本，以及收回成本需要多久。适合对比获客渠道或判断增长是否划算时选用。",
        "de-DE": "Vergleicht den Wert eines Kunden über die Zeit mit den Kosten, ihn zu gewinnen, und wie lange es dauert, diese wieder einzuspielen. Wählen Sie ihn, um Kanäle zu vergleichen oder zu prüfen, ob sich Wachstum lohnt.",
        "fr-FR": "Compare la valeur d’un client dans le temps au coût de son acquisition et au délai pour le rentabiliser. Choisissez-le pour comparer des canaux ou vérifier si la croissance est rentable.",
    },
    "analyst-method-valuation": {
        "en-US": "Helps choose a suitable valuation method for a business and keeps discounted cash flow assumptions realistic. Pick it for valuation questions or to cross-check a target price.",
        "zh-CN": "按生意模式选择合适的估值方法，并让现金流折现的假设保持合理。适合做估值或交叉验证目标价时选用。",
        "de-DE": "Hilft, eine passende Bewertungsmethode für ein Unternehmen zu wählen, und hält die Annahmen einer Discounted-Cashflow-Bewertung realistisch. Wählen Sie ihn für Bewertungsfragen oder zur Gegenprüfung eines Kursziels.",
        "fr-FR": "Aide à choisir la méthode de valorisation adaptée à une activité et à garder des hypothèses réalistes pour l’actualisation des flux de trésorerie. Choisissez-le pour une valorisation ou pour recouper un objectif de cours.",
    },
    "analyst-sop-business-review": {
        "en-US": "Gives an overall view of how the business is doing: a few key metrics, their monthly trend and a breakdown by the main dimension. Pick it for questions like “How is business lately?”",
        "zh-CN": "整体看业务表现：几项关键指标、按月趋势和主要维度拆分。适合回答「最近经营怎么样」这类问题。",
        "de-DE": "Gibt einen Gesamtüberblick über die Geschäftsentwicklung: einige Kennzahlen, ihren Monatsverlauf und eine Aufschlüsselung nach der wichtigsten Dimension. Wählen Sie ihn für Fragen wie „Wie läuft das Geschäft zurzeit?“",
        "fr-FR": "Donne une vue d’ensemble de l’activité : quelques indicateurs clés, leur tendance mensuelle et une ventilation selon la dimension principale. Choisissez-le pour des questions comme « Comment va l’activité en ce moment ? »",
    },
    "analyst-sop-compute": {
        "en-US": "Runs calculations that ordinary data queries can’t, such as significance tests, forecasts, clustering and regression. Pick it when you need statistics or a forecast from your data.",
        "zh-CN": "完成普通数据查询做不到的计算，如显著性检验、预测、聚类和回归。适合需要统计分析或预测时选用。",
        "de-DE": "Führt Berechnungen aus, die normale Datenabfragen nicht leisten, etwa Signifikanztests, Prognosen, Clustering und Regression. Wählen Sie ihn, wenn Sie Statistiken oder eine Prognose aus Ihren Daten brauchen.",
        "fr-FR": "Effectue les calculs que les requêtes de données ordinaires ne permettent pas : tests de significativité, prévisions, regroupements et régressions. Choisissez-le quand vous avez besoin de statistiques ou d’une prévision.",
    },
    "analyst-sop-diagnosis": {
        "en-US": "Finds out why a metric dropped: it confirms the drop, then narrows it down by dimension, by time and by price versus volume. Pick it for questions like “Why did sales fall?”",
        "zh-CN": "找出指标下跌的原因：先确认下跌，再按维度、时间点以及量价逐层定位。适合回答「为什么销售额下降了」这类问题。",
        "de-DE": "Findet heraus, warum eine Kennzahl gesunken ist: Er bestätigt den Rückgang und grenzt ihn nach Dimension, Zeitpunkt sowie Preis und Menge ein. Wählen Sie ihn für Fragen wie „Warum ist der Umsatz gesunken?“",
        "fr-FR": "Identifie pourquoi un indicateur a baissé : il confirme la baisse, puis la localise par dimension, dans le temps et entre prix et volume. Choisissez-le pour des questions comme « Pourquoi les ventes ont-elles baissé ? »",
    },
    "analyst-sop-growth-driver": {
        "en-US": "Ranks what contributed most to growth and checks the result across other dimensions. Pick it for questions like “Which product drove the growth?”",
        "zh-CN": "按贡献大小找出增长的主要来源，并在其他维度上交叉验证。适合回答「增长主要来自哪个产品」这类问题。",
        "de-DE": "Ordnet, was am meisten zum Wachstum beigetragen hat, und prüft das Ergebnis über weitere Dimensionen. Wählen Sie ihn für Fragen wie „Welches Produkt hat das Wachstum getragen?“",
        "fr-FR": "Classe ce qui a le plus contribué à la croissance et vérifie le résultat sur d’autres dimensions. Choisissez-le pour des questions comme « Quel produit a porté la croissance ? »",
    },
    "analyst-sop-output-contract": {
        "en-US": "Turns finished analysis into a clear chat answer or a report with its supporting numbers. Pick it together with the other analysis Skills so results come back in a consistent form.",
        "zh-CN": "把完成的分析整理成清晰的对话回答，或附带依据数字的报告。和其他分析类技能一起选用，让结果以统一的形式返回。",
        "de-DE": "Bringt eine fertige Analyse in eine klare Chat-Antwort oder einen Bericht mit den zugehörigen Zahlen. Wählen Sie ihn zusammen mit den anderen Analyse-Skills, damit Ergebnisse einheitlich zurückkommen.",
        "fr-FR": "Met une analyse terminée sous forme de réponse claire dans la conversation ou de rapport avec les chiffres qui l’étayent. Choisissez-le avec les autres Skills d’analyse pour obtenir des résultats homogènes.",
    },
    "analyst-sop-period-compare": {
        "en-US": "Compares a metric across periods, such as year over year or month over month, with a trend and a breakdown by dimension. Pick it for questions like “How does this compare with last year?”",
        "zh-CN": "对比指标在不同周期的表现（同比、环比等），并给出趋势和维度拆分。适合回答「和去年同期比怎么样」这类问题。",
        "de-DE": "Vergleicht eine Kennzahl über Zeiträume, etwa zum Vorjahr oder Vormonat, mit Verlauf und Aufschlüsselung nach Dimension. Wählen Sie ihn für Fragen wie „Wie steht es im Vergleich zum Vorjahr?“",
        "fr-FR": "Compare un indicateur entre périodes, par exemple sur un an ou d’un mois à l’autre, avec la tendance et une ventilation par dimension. Choisissez-le pour des questions comme « Comment cela se compare-t-il à l’an dernier ? »",
    },
    "bowra-docx": {
        "en-US": "Creates editable Word documents with headings, lists and tables, or reads the text of a Word file. Pick it for a simple Word report; tracked changes are not supported.",
        "zh-CN": "生成带标题、列表和表格的可编辑 Word 文档，或读取 Word 文件中的文字。适合需要一份简单的 Word 报告时选用；不支持修订记录。",
        "de-DE": "Erstellt bearbeitbare Word-Dokumente mit Überschriften, Listen und Tabellen oder liest den Text einer Word-Datei. Wählen Sie ihn für einen einfachen Word-Bericht; Änderungsverfolgung wird nicht unterstützt.",
        "fr-FR": "Crée des documents Word modifiables avec titres, listes et tableaux, ou lit le texte d’un fichier Word. Choisissez-le pour un rapport Word simple ; le suivi des modifications n’est pas pris en charge.",
    },
    "bowra-pdf": {
        "en-US": "Creates paginated PDF reports from text and tables, or reads the text in a PDF. Pick it for simple PDF output; scanned pages can’t be read.",
        "zh-CN": "用文字和表格生成分页的 PDF 报告，或读取 PDF 中的文字。适合需要简单的 PDF 时选用；无法识别扫描页。",
        "de-DE": "Erstellt paginierte PDF-Berichte aus Text und Tabellen oder liest den Text einer PDF-Datei. Wählen Sie ihn für einfache PDF-Ausgaben; gescannte Seiten können nicht gelesen werden.",
        "fr-FR": "Crée des rapports PDF paginés à partir de texte et de tableaux, ou lit le texte d’un PDF. Choisissez-le pour un PDF simple ; les pages numérisées ne peuvent pas être lues.",
    },
    "bowra-pptx": {
        "en-US": "Creates a basic widescreen PowerPoint deck from titles, bullet points and tables, or reads the text on slides. Pick it for a quick deck; it doesn’t edit your own templates.",
        "zh-CN": "用标题、要点和表格生成基础的宽屏 PowerPoint 演示文稿，或读取幻灯片上的文字。适合快速做一份演示文稿；不会修改你自己的模板。",
        "de-DE": "Erstellt eine einfache PowerPoint-Präsentation im Breitbildformat aus Titeln, Aufzählungen und Tabellen oder liest den Text von Folien. Wählen Sie ihn für eine schnelle Präsentation; eigene Vorlagen werden nicht bearbeitet.",
        "fr-FR": "Crée une présentation PowerPoint simple au format large à partir de titres, de puces et de tableaux, ou lit le texte des diapositives. Choisissez-le pour une présentation rapide ; il ne modifie pas vos propres modèles.",
    },
    "bowra-xlsx": {
        "en-US": "Creates editable Excel sheets with numbers, dates and number formats, or reads cell values and formulas. Pick it for simple spreadsheets; formulas are not calculated.",
        "zh-CN": "生成带数值、日期和数字格式的可编辑 Excel 表格，或读取单元格的值和公式。适合做简单的电子表格；不会计算公式结果。",
        "de-DE": "Erstellt bearbeitbare Excel-Tabellen mit Zahlen, Datumswerten und Zahlenformaten oder liest Zellwerte und Formeln. Wählen Sie ihn für einfache Tabellen; Formeln werden nicht berechnet.",
        "fr-FR": "Crée des feuilles Excel modifiables avec nombres, dates et formats numériques, ou lit les valeurs et formules des cellules. Choisissez-le pour des tableurs simples ; les formules ne sont pas calculées.",
    },
    "building-dashboards": {
        "en-US": "Builds a monitoring dashboard with key metrics, charts and checks against your business rules. Pick it when you want an ongoing view to watch rather than a one-off answer.",
        "zh-CN": "搭建监控看板，包含关键指标、图表和业务规则检查。适合需要持续关注的看板，而不是一次性的问答。",
        "de-DE": "Erstellt ein Monitoring-Dashboard mit Kennzahlen, Diagrammen und Prüfungen gegen Ihre Geschäftsregeln. Wählen Sie ihn, wenn Sie eine laufende Übersicht statt einer einmaligen Antwort möchten.",
        "fr-FR": "Construit un tableau de bord de suivi avec indicateurs clés, graphiques et contrôles de vos règles métier. Choisissez-le pour une vue à suivre dans la durée plutôt qu’une réponse ponctuelle.",
    },
    "canvas-design": {
        "en-US": "Creates original static visuals, such as posters and design pieces, as images or PDFs. Pick it when you want a poster, artwork or visual design.",
        "zh-CN": "以图片或 PDF 形式创作原创的静态视觉作品，如海报和设计稿。适合需要海报、插画或视觉设计时选用。",
        "de-DE": "Erstellt originelle statische Grafiken wie Poster und Designs als Bild oder PDF. Wählen Sie ihn, wenn Sie ein Poster, ein Kunstwerk oder ein visuelles Design möchten.",
        "fr-FR": "Crée des visuels statiques originaux, comme des affiches et des créations graphiques, en image ou en PDF. Choisissez-le pour une affiche, une illustration ou un design visuel.",
    },
    "canvas-docx": {
        "en-US": "Exports the report on the Canvas as an editable Word document. Pick it when you want to share or edit the report in Word.",
        "zh-CN": "把画布上的报告导出为可编辑的 Word 文档。适合需要在 Word 中分享或修改报告时选用。",
        "de-DE": "Exportiert den Bericht im Canvas als bearbeitbares Word-Dokument. Wählen Sie ihn, wenn Sie den Bericht in Word teilen oder bearbeiten möchten.",
        "fr-FR": "Exporte le rapport du Canvas en document Word modifiable. Choisissez-le pour partager ou modifier le rapport dans Word.",
    },
    "canvas-pdf": {
        "en-US": "Exports the report on the Canvas as a print-ready PDF. Pick it when you want a fixed-layout copy to send or print.",
        "zh-CN": "把画布上的报告导出为可直接打印的 PDF。适合需要发送或打印固定版式的报告时选用。",
        "de-DE": "Exportiert den Bericht im Canvas als druckfertige PDF-Datei. Wählen Sie ihn für eine Fassung mit festem Layout zum Versenden oder Drucken.",
        "fr-FR": "Exporte le rapport du Canvas en PDF prêt à imprimer. Choisissez-le pour une version à mise en page fixe à envoyer ou imprimer.",
    },
    "canvas-pptx": {
        "en-US": "Exports the report on the Canvas as an editable PowerPoint deck, with as many slides as the content needs. Pick it when you want to present the report.",
        "zh-CN": "把画布上的报告导出为可编辑的 PowerPoint 演示文稿，页数按内容自动分配。适合需要拿报告去汇报时选用。",
        "de-DE": "Exportiert den Bericht im Canvas als bearbeitbare PowerPoint-Präsentation mit so vielen Folien, wie der Inhalt braucht. Wählen Sie ihn, wenn Sie den Bericht präsentieren möchten.",
        "fr-FR": "Exporte le rapport du Canvas en présentation PowerPoint modifiable, avec autant de diapositives que le contenu l’exige. Choisissez-le pour présenter le rapport.",
    },
    "canvas-xlsx": {
        "en-US": "Exports the report on the Canvas as an Excel workbook. Pick it when you want to keep working with the report’s tables and numbers.",
        "zh-CN": "把画布上的报告导出为 Excel 工作簿。适合需要继续处理报告中的表格和数字时选用。",
        "de-DE": "Exportiert den Bericht im Canvas als Excel-Arbeitsmappe. Wählen Sie ihn, wenn Sie mit den Tabellen und Zahlen des Berichts weiterarbeiten möchten.",
        "fr-FR": "Exporte le rapport du Canvas en classeur Excel. Choisissez-le pour retravailler les tableaux et les chiffres du rapport.",
    },
    "doc-coauthoring": {
        "en-US": "Guides you through writing a structured document, such as a proposal, spec or decision note: gather context, draft, refine and check. Pick it when you want to write a document together step by step.",
        "zh-CN": "引导你一步步写出结构化文档，如方案、需求说明或决策文档：收集背景、起草、打磨和核对。适合想和智能体一起逐步写文档时选用。",
        "de-DE": "Führt Sie durch das Schreiben eines strukturierten Dokuments wie Angebot, Spezifikation oder Entscheidungsvorlage: Kontext sammeln, entwerfen, überarbeiten und prüfen. Wählen Sie ihn, wenn Sie ein Dokument Schritt für Schritt gemeinsam schreiben möchten.",
        "fr-FR": "Vous guide dans la rédaction d’un document structuré, comme une proposition, une spécification ou une note de décision : contexte, brouillon, révision et vérification. Choisissez-le pour rédiger un document ensemble, étape par étape.",
    },
    "docx": {
        "en-US": "Guidance for reading, creating and editing Word files, including formatting, images and tracked changes. This is a methods reference, not the report export action.",
        "zh-CN": "提供 Word 阅读、创建和编辑方法，包括格式、图片及修订记录。属于方法参考，不是已有报告的导出操作。",
        "de-DE": "Anleitung zum Lesen, Erstellen und Bearbeiten von Word-Dateien, einschließlich Formatierung, Bildern und Änderungsverfolgung. Eine Methodenreferenz, keine Berichtsexport-Aktion.",
        "fr-FR": "Guide de lecture, de création et de modification de fichiers Word, avec mise en forme, images et suivi des modifications. Une référence de méthodes, pas une action d’export de rapport.",
    },
    "frontend-design": {
        "en-US": "Designs distinctive, polished web pages and interface components. Pick it when you want a web page, landing page or dashboard layout that looks well designed.",
        "zh-CN": "设计有辨识度、精致的网页和界面组件。适合需要一个设计感好的网页、落地页或看板布局时选用。",
        "de-DE": "Gestaltet unverwechselbare, ausgereifte Webseiten und Oberflächenkomponenten. Wählen Sie ihn für eine gut gestaltete Webseite, Landingpage oder ein Dashboard-Layout.",
        "fr-FR": "Conçoit des pages web et des composants d’interface soignés et distinctifs. Choisissez-le pour une page web, une page d’accueil ou une mise en page de tableau de bord bien conçue.",
    },
    "mcp-builder": {
        "en-US": "Helps developers build a connector server that lets AI assistants use an external service’s API. Pick it when you are developing a new MCP server.",
        "zh-CN": "帮助开发者构建连接服务，让 AI 助手可以调用外部服务的接口。适合开发新的 MCP 服务时选用。",
        "de-DE": "Hilft Entwicklern, einen Konnektor-Server zu bauen, über den KI-Assistenten die API eines externen Dienstes nutzen können. Wählen Sie ihn, wenn Sie einen neuen MCP-Server entwickeln.",
        "fr-FR": "Aide les développeurs à créer un serveur de connexion permettant aux assistants IA d’utiliser l’API d’un service externe. Choisissez-le pour développer un nouveau serveur MCP.",
    },
    "pdf": {
        "en-US": "Guidance for extracting text and tables, merging, splitting and other PDF operations. Actual operations depend on the available execution environment; this does not export the current report or convert a presentation.",
        "zh-CN": "提供 PDF 文字与表格提取、合并拆分等处理方法，实际操作取决于可用执行环境；不等于导出当前报告或转换演示文稿。",
        "de-DE": "Anleitung zum Extrahieren von Text und Tabellen, Zusammenführen, Teilen und weiteren PDF-Aufgaben. Die Ausführung hängt von der verfügbaren Umgebung ab; kein Export des aktuellen Berichts und keine Präsentationskonvertierung.",
        "fr-FR": "Guide d’extraction de texte et de tableaux, de fusion, de découpage et d’autres opérations PDF. Leur exécution dépend de l’environnement disponible ; ce n’est ni l’export du rapport courant ni la conversion d’une présentation.",
    },
    "pptx": {
        "en-US": "Guidance for inspecting and authoring PowerPoint presentations, including style references. Editing an uploaded template uses the separate draft review and confirmation workflow; this guide does not publish it.",
        "zh-CN": "提供 PPT 阅读、制作及样例风格参考方法。修改上传模板走独立的草稿审阅和确认流程，本参考不会直接发布模板修改。",
        "de-DE": "Anleitung zum Prüfen und Erstellen von PowerPoint-Präsentationen, auch mit Stilvorlagen. Änderungen an importierten Vorlagen nutzen den separaten Entwurfs- und Bestätigungsablauf; dieser Leitfaden veröffentlicht sie nicht.",
        "fr-FR": "Guide d’inspection et de création de présentations PowerPoint, avec références de style. La modification d’un modèle importé passe par le circuit séparé de brouillon et de confirmation ; ce guide ne la publie pas.",
    },
    "python_sandbox": {
        "en-US": "Runs Python calculations in a secure, offline environment for statistics, forecasts and other analysis that data queries can’t do. Pick it when an Agent needs to calculate with code.",
        "zh-CN": "在安全的离线环境中运行 Python 计算，用于统计、预测等数据查询做不到的分析。适合智能体需要写代码计算时选用。",
        "de-DE": "Führt Python-Berechnungen in einer sicheren Offline-Umgebung aus, für Statistiken, Prognosen und andere Analysen, die Datenabfragen nicht leisten. Wählen Sie ihn, wenn ein Agent mit Code rechnen muss.",
        "fr-FR": "Exécute des calculs Python dans un environnement sécurisé et hors ligne, pour les statistiques, prévisions et autres analyses que les requêtes de données ne permettent pas. Choisissez-le quand un Agent doit calculer avec du code.",
    },
    "release-notes": {
        "en-US": "Writes and reviews release notes and changelogs in clear, accurate language that doesn’t over-promise. Pick it when you are announcing a product update.",
        "zh-CN": "撰写和审阅发布说明与更新记录，用清楚、准确、不夸大的语言。适合发布产品更新时选用。",
        "de-DE": "Schreibt und prüft Versionshinweise und Änderungsprotokolle in klarer, genauer Sprache ohne Übertreibungen. Wählen Sie ihn, wenn Sie ein Produkt-Update ankündigen.",
        "fr-FR": "Rédige et relit des notes de version et des journaux des modifications dans un langage clair, précis et sans promesses excessives. Choisissez-le pour annoncer une mise à jour produit.",
    },
    "settings-configuration": {
        "en-US": "Lets the system settings assistant change supported settings for you in conversation. Used only by the system settings assistant.",
        "zh-CN": "让系统设置助手在对话中帮你修改支持的设置。只供系统设置助手使用。",
        "de-DE": "Lässt den Systemeinstellungs-Assistenten unterstützte Einstellungen im Gespräch für Sie ändern. Wird nur vom Systemeinstellungs-Assistenten verwendet.",
        "fr-FR": "Permet à l’assistant des paramètres système de modifier avec vous les paramètres pris en charge, dans la conversation. Utilisé uniquement par l’assistant des paramètres système.",
    },
    "settings-language-preferences": {
        "en-US": "Saves the language you want answers in when you ask for a lasting change rather than for one reply. Used only by the system settings assistant.",
        "zh-CN": "当你要求长期改变而不只是这一次时，保存你希望的回答语言。只供系统设置助手使用。",
        "de-DE": "Speichert die gewünschte Antwortsprache, wenn Sie eine dauerhafte Änderung statt nur für eine Antwort wünschen. Wird nur vom Systemeinstellungs-Assistenten verwendet.",
        "fr-FR": "Enregistre la langue de réponse souhaitée lorsque vous demandez un changement durable, et pas seulement pour une réponse. Utilisé uniquement par l’assistant des paramètres système.",
    },
    "settings-personal-appearance": {
        "en-US": "Adjusts your personal appearance, such as dark mode, text size, density, corners and motion. Used only by the system settings assistant.",
        "zh-CN": "调整你的个人外观，如深色模式、文字大小、密度、圆角和动效。只供系统设置助手使用。",
        "de-DE": "Passt Ihr persönliches Erscheinungsbild an, etwa Dunkelmodus, Textgröße, Dichte, Ecken und Animationen. Wird nur vom Systemeinstellungs-Assistenten verwendet.",
        "fr-FR": "Ajuste votre apparence personnelle : mode sombre, taille du texte, densité, arrondis et animations. Utilisé uniquement par l’assistant des paramètres système.",
    },
    "settings-space-instructions": {
        "en-US": "Explains or changes the instructions every conversation in the current Space follows, after you confirm. Used only by the system settings assistant.",
        "zh-CN": "说明或修改当前空间里每段对话都会遵循的空间说明，经你确认后生效。只供系统设置助手使用。",
        "de-DE": "Erklärt oder ändert nach Ihrer Bestätigung die Anweisungen, denen jede Unterhaltung im aktuellen Bereich folgt. Wird nur vom Systemeinstellungs-Assistenten verwendet.",
        "fr-FR": "Explique ou modifie, après votre confirmation, les instructions que suit chaque conversation de l’espace actuel. Utilisé uniquement par l’assistant des paramètres système.",
    },
    "settings-space-metadata": {
        "en-US": "Explains or changes the current Space’s name and description, after you confirm. Used only by the system settings assistant.",
        "zh-CN": "说明或修改当前空间的名称和描述，经你确认后生效。只供系统设置助手使用。",
        "de-DE": "Erklärt oder ändert nach Ihrer Bestätigung Name und Beschreibung des aktuellen Bereichs. Wird nur vom Systemeinstellungs-Assistenten verwendet.",
        "fr-FR": "Explique ou modifie, après votre confirmation, le nom et la description de l’espace actuel. Utilisé uniquement par l’assistant des paramètres système.",
    },
    "settings-space-preferences": {
        "en-US": "Explains or changes the current Space’s week start and the language of its stored data, after you confirm. Used only by the system settings assistant.",
        "zh-CN": "说明或修改当前空间的每周起始日和数据语言，经你确认后生效。只供系统设置助手使用。",
        "de-DE": "Erklärt oder ändert nach Ihrer Bestätigung Wochenbeginn und Datensprache des aktuellen Bereichs. Wird nur vom Systemeinstellungs-Assistenten verwendet.",
        "fr-FR": "Explique ou modifie, après votre confirmation, le début de semaine et la langue des données de l’espace actuel. Utilisé uniquement par l’assistant des paramètres système.",
    },
    "shopify-analysis": {
        "en-US": "Answers questions about your connected Shopify store’s orders, products, inventory and store content without changing anything. Pick it for day-to-day store questions.",
        "zh-CN": "回答已连接 Shopify 店铺的订单、商品、库存和店铺内容相关问题，只读不改。适合处理日常店铺经营问题时选用。",
        "de-DE": "Beantwortet Fragen zu Bestellungen, Produkten, Lagerbestand und Inhalten Ihres verbundenen Shopify-Shops, ohne etwas zu ändern. Wählen Sie ihn für alltägliche Fragen zum Shop.",
        "fr-FR": "Répond aux questions sur les commandes, produits, stocks et contenus de votre boutique Shopify connectée, sans rien modifier. Choisissez-le pour les questions courantes sur la boutique.",
    },
    "shopify-content-optimization": {
        "en-US": "Improves product descriptions, product SEO and page content in your connected Shopify store, and prepares the changes for you to confirm. Pick it for store copy; it doesn’t change layouts, prices or inventory.",
        "zh-CN": "优化已连接 Shopify 店铺的商品文案、商品 SEO 和页面内容，并准备好修改供你确认。适合改店铺文案；不涉及主题布局、价格或库存。",
        "de-DE": "Verbessert Produkttexte, Produkt-SEO und Seiteninhalte in Ihrem verbundenen Shopify-Shop und bereitet die Änderungen zur Bestätigung vor. Wählen Sie ihn für Shop-Texte; Layouts, Preise und Lagerbestand bleiben unverändert.",
        "fr-FR": "Améliore les fiches produits, le référencement des produits et le contenu des pages de votre boutique Shopify connectée, et prépare les modifications pour votre validation. Choisissez-le pour les textes de la boutique ; il ne touche ni à la mise en page, ni aux prix, ni aux stocks.",
    },
    "skill-creator": {
        "en-US": "Helps you create or change a reusable Skill, for example by saving a workflow that worked well. Pick it when you want an Agent to follow the same steps next time.",
        "zh-CN": "帮你创建或修改可复用的技能，例如把一次验证有效的流程保存下来。适合想让智能体下次照同样步骤做时选用。",
        "de-DE": "Hilft Ihnen, einen wiederverwendbaren Skill zu erstellen oder zu ändern, etwa indem Sie einen bewährten Ablauf speichern. Wählen Sie ihn, wenn ein Agent beim nächsten Mal dieselben Schritte befolgen soll.",
        "fr-FR": "Vous aide à créer ou modifier un Skill réutilisable, par exemple en enregistrant une méthode qui a fait ses preuves. Choisissez-le pour qu’un Agent suive les mêmes étapes la prochaine fois.",
    },
    "theme-factory": {
        "en-US": "Gives slides, documents, reports and web pages a consistent look with matching colors and fonts, from a preset or a new theme. Pick it after creating content you want to style.",
        "zh-CN": "为演示文稿、文档、报告和网页套用统一的配色与字体，可选预设主题或新建主题。适合内容做好后想统一风格时选用。",
        "de-DE": "Gibt Folien, Dokumenten, Berichten und Webseiten ein einheitliches Aussehen mit passenden Farben und Schriften, aus einem vorgefertigten oder neuen Thema. Wählen Sie ihn, nachdem Sie Inhalte erstellt haben, die gestaltet werden sollen.",
        "fr-FR": "Donne aux présentations, documents, rapports et pages web une apparence cohérente, couleurs et polices assorties, à partir d’un thème prédéfini ou nouveau. Choisissez-le une fois le contenu créé, pour l’harmoniser.",
    },
    "web-artifacts-builder": {
        "en-US": "Builds interactive web pages with several parts, such as tabs, forms or navigation. Pick it for a richer interactive page; a simple single page doesn’t need it.",
        "zh-CN": "构建由多个部分组成的交互式网页，如标签页、表单或页面导航。适合需要较复杂交互的网页；简单的单页不需要它。",
        "de-DE": "Erstellt interaktive Webseiten aus mehreren Teilen, etwa mit Tabs, Formularen oder Navigation. Wählen Sie ihn für umfangreichere interaktive Seiten; für eine einfache Einzelseite ist er nicht nötig.",
        "fr-FR": "Crée des pages web interactives en plusieurs parties, avec onglets, formulaires ou navigation. Choisissez-le pour une page interactive plus riche ; une page simple n’en a pas besoin.",
    },
    "workspace-capabilities": {
        "en-US": "Explains what you can do in this Space: which data is available, what can be analyzed and a few example questions to try. Pick it for questions like “What can you do?”",
        "zh-CN": "介绍在当前空间里能做什么：有哪些数据、能分析什么，并给出几个可以试试的问题。适合回答「你能干什么」这类问题。",
        "de-DE": "Erklärt, was Sie in diesem Bereich tun können: welche Daten verfügbar sind, was sich analysieren lässt und einige Beispielfragen. Wählen Sie ihn für Fragen wie „Was kannst du?“",
        "fr-FR": "Explique ce que vous pouvez faire dans cet espace : données disponibles, analyses possibles et quelques exemples de questions. Choisissez-le pour des questions comme « Que sais-tu faire ? »",
    },
    "xlsx": {
        "en-US": "Guidance for reading and editing spreadsheets, formulas, formatting and charts. This is a methods reference; use report export to download an existing report's tables.",
        "zh-CN": "提供电子表格阅读、编辑、公式、格式和图表的方法参考。下载已有报告中的表格，应使用报告导出。",
        "de-DE": "Anleitung zum Lesen und Bearbeiten von Tabellen, Formeln, Formatierungen und Diagrammen. Eine Methodenreferenz; vorhandene Berichtstabellen werden über den Berichtsexport heruntergeladen.",
        "fr-FR": "Guide de lecture et de modification de tableurs, de formules, de mise en forme et de graphiques. Une référence de méthodes ; utilisez l’export de rapport pour télécharger ses tableaux.",
    },
}
