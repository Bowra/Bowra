---
name: analyst-method-internal-control-aml
description: "内控合规穿透(Internal Control & AML): 职责分离/CDD身份验证/EDD高风险识别/可疑交易上报/大额现金监测 — 用于'监管处罚/数据泄露/内控薄弱/合规缺口/控制成熟度'的审计。"
required_skills: [analyst-framework]
version: 1.0.0
pipes_output_to: analyst-sop-output-contract
audience: peer # peer 执行细节，不进 Lead 路由目录（skills-tools-ssot §5-G4）
title: "Internal controls and anti-money laundering"
title_zh: "内控与反洗钱"
category: analysis
---

# Method · 内控合规穿透 (Internal Control & AML)

**何时用**: 任何涉及合规风险、资金合规、数据访问控制、业务连续性的审计。金融机构/跨境企业看到流程/数据/交易应**默认**先跑这一关。

**前置**: `skill_read("analyst-framework")`(通用骨架 + 三档契约)。

**核心命题**: 内控是管理信任、合规是监管底线。本 method 强制 4 域检查，把"流程文件"与"实际执行"拆穿成"控制缺陷严重度 + 监管风险"。

## 4 域协议(MUST 全跑, 缺数据显式标缺口, 不得跳过装完整)

### 域 1: 职责分离 (SoD — Segregation of Duties)

1.1 **五权不兼**: 录入、审核、批准、执行、对账 必须不同人物。  
1.2 **操作日志**：所有四权操作需完整留痕（谁+什么时间+什么操作+变更前后值）。  
1.3 **例外处理**: 任何 SoD 豁免(如小额特批) 需事前书面授权 + 事后至少月度对账。  
⚠️ **红线**: 同一人同时拥有"入账 + 审批"或"审批 + 执行"= 直接高风险标志。

### 域 2: CDD/EDD (Customer Due Diligence / Enhanced Due Diligence)

2.1 **CDD 基础**: 新客户/交易对手必须有 (i)身份验证(实名制证件扫描)、(ii)受益所有人(UBO)披露、(iii)业务关系证明文件。  
2.2 **EDD 触发**: 若客户属 PEP(政治人物)、外资背景、高风险国家、或高净值(>500W 人民币/100W USD) → 升级 EDD，补充背景调查、资金来源核验、关联企业穿透。  
2.3 **跨境交易 EDD**: 一切涉美交易(美方主体/美元支付/美国受益人) 加倍尽调，补OFAC 制裁库复核。  
⚠️ **红线**: 无 CDD 记录、UBO 信息缺失、EDD 触发未升级 = 违反 PIPL §6/§14/GDPR Art 22/AML Law §7。

### 域 3: STR/大额监测 (Suspicious Transaction Report / Large Transaction Monitoring)

3.1 **大额现金阈值**: ≥5万(人民币) 或 ≥1万(美元) 必须上报；跨境转账无额度限制，全量采集。  
3.2 **可疑交易触发**: (i)交易模式异常(如频繁小额合并绕过阈值)、(ii)资金来源/去向无合理商业目的、(iii)黑名单命中、(iv)客户尽调缺失 → 立即标记"STR 可疑"，30 天内(或法规规定)上报AMLCO。  
3.3 **报告链路**: 交易→发现→STR 初报→合规复审→报送金融情报机构(FIU)→反馈 = 必须可跟踪。  
⚠️ **红线**: 大额交易漏报、STR 延报、未建立监测阈值模型 = 违反《AML法》§16。

### 域 4: 数据访问控制 & 审计日志 (Data Access Lineage & Anomaly Alert)

4.1 **角色权限矩阵**: 每角色(客户经理/合规官/财务/IT) 需明确系统权限边界(可查/可改/可删)。  
4.2 **访问日志留痕**: 所有敏感数据(客户 KYC/账户明细/交易记录/内控文件)的读写删需 24h 内留下不可篡改日志(含IP/时间/操作人/操作)。  
4.3 **异常告警**: (i)非工作时间异常访问、(ii)高权限账户批量导出、(iii)已离职员工仍可访问、(iv)数据库直连操作(绕过应用) → 实时 alert。  
⚠️ **红线**: 数据泄露轨迹不可追溯、访问日志缺失 > 3 月、离职离职交接遗漏 = 违反 PIPL §28/§38/§55 数据安全。

## 计算走 python_sandbox(不靠心算)

拿到内控评价表/操作日志/CDD 记录/交易清单后，`skill_invoke("python_sandbox")` 跑 `scripts/aml_control_checks.py`(它扫描 4 域检查清单，输出覆盖度 + 缺陷等级 + 成熟度评分，并按阈值打 flag)。**检查与阈值判定交给脚本, 确定且可审计。**

## 产出(进 payload 的 risk_flags + insights)

- 每条 finding 带域(SoD/CDD/STR/Access)、等级(critical/high/medium/low)、证据来源 ID。
- risk_flags 例:`{"level":"critical","domain":"SoD","scenario":"财务主管兼审批与执行权, 离职未交接访问权","evidence":"art_xxx","regulation":"COSO §4.2"}`。
- 缺操作日志/CDD 记录/交易清单 → flag "数据缺口", 相关结论标"无法完整验证"。

## 三档(对齐 analyst-framework §0.5)

- **MUST**: SoD 权限矩阵检查 + CDD 覆盖扫描 + 大额现金监测规则存在性 + 结论。
- **SHOULD**(complexity≥0.7 / "详细审计"): 补 STR 报告链路完整性 + 数据访问日志采样检查 + 离职人员权限清理核查。
- **MAY**("深度审计/全套成熟度"): UBO 穿透完整性 + OFAC 库同步频率 + 内控成熟度模型评分(CMM 1-5) + 补救建议优先级。
