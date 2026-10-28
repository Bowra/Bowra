import { EXTRA_HELP_ARTICLES, extraHelpRoot } from "@/edition/help";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { buildLocale, type Locale } from "@/lib/locale";

export type HelpArticleGroup =
  | "getting_started"
  | "interface"
  | "team_access"
  | "agents_skills"
  | "security_data"
  | "reference";

/** `/me` capabilities that can gate a Help article. */
export type HelpArticleCapability = "access_requests" | "google_workspace_feature" | "shopify_plugin" | "account_deletion" | "agent_eval_alignment";

export interface HelpArticle {
  id: string;
  title: string;
  description: string;
  group: HelpArticleGroup | null;
  content: string;
  /** Publication dates keyed by release title, for the standalone release notes. */
  releaseDates?: Record<string, string>;
  /** When set, the article stays hidden unless `/me` reports this capability as true. */
  requiresCapability?: HelpArticleCapability;
}

interface LocalizedText {
  "zh-CN": string;
  "en-US": string;
  "de-DE"?: string;
  "fr-FR"?: string;
}

export interface HelpArticleDefinition extends Omit<
  HelpArticle,
  "content" | "title" | "description"
> {
  title: LocalizedText;
  description: LocalizedText;
  source: string;
}

/** Standalone release notes, linked from the account menu and Help › About. */
export const RELEASE_NOTES_ARTICLE_ID = "release-notes";

/**
 * Oldest version page published in the application. Earlier pages were written for maintainers and stay in the
 * repository only; the user-facing notes start with 1.5.0.
 */
export const FIRST_PUBLISHED_RELEASE = "1.5.0";

export const HELP_ARTICLE_DEFINITIONS: readonly HelpArticleDefinition[] = [
  {
    id: "team-capabilities",
    title: {
      "zh-CN": "使用团队插件",
      "en-US": "Use team Plugins",
     "fr-FR": "Utiliser les plugins de l’équipe", },
    description: {
      "zh-CN": "组合 Tools 与 Skills，并供多个智能体使用",
      "en-US": "Combine Tools and Skills for multiple Agents",
     "fr-FR": "Combiner Tools et Skills pour plusieurs Agents", },
    group: "agents_skills",
    source: "team-capabilities.zh-CN.md",
  },
  {
    id: "shopify-plugin",
    title: { "en-US": "Connect Shopify for analysis", "zh-CN": "连接 Shopify 进行分析" },
    description: { "en-US": "Read shop data and content with a selected Agent Plugin", "zh-CN": "通过指定的 Agent Plugin 读取店铺数据与内容" },
    group: "agents_skills",
    source: "shopify-plugin.zh-CN.md",
    requiresCapability: "shopify_plugin",
  },
  {
    id: "personal-mcp",
    title: {
      "zh-CN": "连接个人 MCP 服务",
      "en-US": "Connect a personal MCP service",
     "fr-FR": "Connecter un service MCP personnel", },
    description: {
      "zh-CN": "为自己的私有智能体选择外部工具并确认调用",
      "en-US":
        "Choose external tools for a private Agent and approve each call",
     "fr-FR": "Choisir des outils externes pour un Agent privé et confirmer chaque appel", },
    group: "agents_skills",
    source: "personal-mcp.zh-CN.md",
  },
  {
    id: "model-settings",
    title: {
      "zh-CN": "管理团队模型设置",
      "en-US": "Manage team model settings",
     "fr-FR": "Gérer les modèles de l’équipe", },
    description: {
      "zh-CN": "分别维护连接、生产分工、外部能力和索引状态",
      "en-US":
        "Maintain connections, production assignments, external capabilities, and indexes",
     "fr-FR": "Maintenir connexions, affectations de production, capacités externes et index", },
    group: "reference",
    source: "model-settings.zh-CN.md",
  },
  {
    id: "home",
    title: { "zh-CN": "帮助中心首页", "en-US": "Help Center home", "fr-FR": "Accueil du centre d’aide", },
    description: {
      "zh-CN": "按你的目标找到下一步",
      "en-US": "Find the next step for your goal",
     "fr-FR": "Trouver la prochaine étape pour votre objectif", },
    group: null,
    source: "home.zh-CN.md",
  },
  {
    id: "getting-started",
    title: { "zh-CN": "开始一次分析工作", "en-US": "Start an analysis", "fr-FR": "Commencer une analyse", },
    description: {
      "zh-CN": "选择空间、说明目标并查看结果",
      "en-US": "Choose a space, state the goal, and review the result",
     "fr-FR": "Choisir un espace, préciser l’objectif et vérifier le résultat", },
    group: "getting_started",
    source: "getting-started.zh-CN.md",
  },
  {
    id: "knowledge-base",
    title: { "zh-CN": "使用知识库", "en-US": "Use the knowledge base", "fr-FR": "Utiliser la base de connaissances", },
    description: {
      "zh-CN": "上传资料、看懂文件状态，让对话用上你的资料",
      "en-US": "Upload material, read file status, and let chats use it",
     "fr-FR": "Importer des documents, comprendre leur état et les utiliser dans les conversations", },
    group: "getting_started",
    source: "knowledge-base.zh-CN.md",
  },
  {
    id: "revise-report",
    title: { "zh-CN": "修改已有报告", "en-US": "Revise an existing report", "fr-FR": "Modifier un rapport existant", },
    description: {
      "zh-CN": "在同一对话里改口径、改图或补充内容",
      "en-US": "Change the scope, charts, or content in the same chat",
     "fr-FR": "Changer périmètre, graphiques ou contenu dans la même conversation", },
    group: "getting_started",
    source: "revise-report.zh-CN.md",
  },
  {
    id: "email",
    title: { "zh-CN": "发送邮件", "en-US": "Send email", "fr-FR": "Envoyer des e-mails", },
    description: {
      "zh-CN": "在对话中确认后发信，以及配置团队邮件服务",
      "en-US": "Confirm emails sent from chat and set up the team mail service",
     "fr-FR": "Confirmer l’envoi depuis une conversation et configurer le service de l’équipe", },
    group: "getting_started",
    source: "email.zh-CN.md",
  },
  {
    id: "menus",
    title: { "zh-CN": "菜单的用法", "en-US": "Use menus", "fr-FR": "Utiliser les menus", },
    description: {
      "zh-CN": "设置目录、手机面板、危险操作确认、灰色项与键盘操作",
      "en-US":
        "Settings directory, mobile panels, confirmed actions, unavailable items, and keyboard use",
     "fr-FR": "Répertoire des paramètres, panneaux mobiles, confirmations, éléments indisponibles et clavier", },
    group: "interface",
    source: "menus.zh-CN.md",
  },
  {
    id: "row-actions",
    title: {
      "zh-CN": "列表里的更多操作",
      "en-US": "More actions in lists",
     "fr-FR": "Plus d’actions dans les listes", },
    description: {
      "zh-CN": "在行尾「⋯」里找到编辑、下载和删除；手机上的管理列表",
      "en-US": "Find edit, download, and delete in the row “⋯” menu; admin lists on a phone",
     "fr-FR": "Modifier, télécharger et supprimer depuis le menu de ligne ; listes sur téléphone", },
    group: "interface",
    source: "row-actions.zh-CN.md",
  },
  {
    id: "conversation-workbench",
    title: {
      "zh-CN": "对话页的布局与操作",
      "en-US": "Conversation page layout",
     "fr-FR": "Présentation de la page de conversation", },
    description: {
      "zh-CN": "单列首页、运行进度框、回答下方的图标操作、引用角标、成果区切换，以及报告工具条的写作进度、状态与分享",
      "en-US": "The single-column home page, the running progress box, icon actions under answers, citation markers, switching to results, and writing progress, status, and sharing in the report toolbar",
     "fr-FR": "Accueil, progression, actions, citations, résultats et barre des rapports", },
    group: "interface",
    source: "conversation-workbench.zh-CN.md",
  },
  {
    id: "conversation-workbench-actions",
    title: {
      "zh-CN": "对话页的拖放、快捷键与提示",
      "en-US": "Drag and drop, shortcuts, and hints on the conversation page",
     "fr-FR": "Glisser-déposer, raccourcis et indications", },
    description: {
      "zh-CN": "把文件拖进对话、用键盘调宽度与放大成果、新成果圆点、发送不可用原因、改写草稿与重试、报告里的键盘操作",
      "en-US": "Drop files into the chat, resize and enlarge results from the keyboard, the new-results dot, why Send is unavailable, rewrite drafts and retry, and keyboard use in reports",
     "fr-FR": "Ajouter des fichiers, ajuster les résultats et utiliser le clavier ou relancer une réécriture", },
    group: "interface",
    source: "conversation-workbench-actions.zh-CN.md",
  },
  {
    id: "settings-save",
    title: {
      "zh-CN": "设置页怎样保存",
      "en-US": "How settings are saved",
     "fr-FR": "Enregistrer les paramètres", },
    description: {
      "zh-CN": "开关立即生效，文字改动在底部保存栏保存或放弃",
      "en-US": "Switches apply immediately; save or discard text changes in the bottom save bar",
     "fr-FR": "Choix immédiats et enregistrement ou abandon des changements de texte", },
    group: "interface",
    source: "settings-save.zh-CN.md",
  },
  {
    id: "account-deletion",
    title: { "en-US": "Delete your account", "zh-CN": "注销账号", "fr-FR": "Supprimer votre compte" },
    description: { "en-US": "Email reuse, personal data retention, and team ownership", "zh-CN": "邮箱复用、个人数据保留及团队所有权", "fr-FR": "Adresse e-mail, conservation des données et propriété des équipes" },
    group: "security_data",
    source: "account-deletion.zh-CN.md",
    requiresCapability: "account_deletion",
  },
  {
    id: "mobile-dialogs-undo",
    title: {
      "zh-CN": "手机上的弹窗与撤销",
      "en-US": "Mobile dialogs and undo",
     "fr-FR": "Fenêtres mobiles et annulation", },
    description: {
      "zh-CN": "底部面板怎样关闭，哪些操作完成后可以撤销",
      "en-US": "Close bottom panels and undo supported actions",
     "fr-FR": "Fermer les panneaux et annuler les actions prises en charge", },
    group: "interface",
    source: "mobile-dialogs-undo.zh-CN.md",
  },
  {
    id: "conversation-links",
    title: {
      "zh-CN": "会话链接与浏览器前进后退",
      "en-US": "Conversation links and browser navigation",
     "fr-FR": "Liens de conversation et navigation du navigateur", },
    description: {
      "zh-CN": "地址栏、刷新和前进后退怎样对应会话",
      "en-US": "How the address bar, refresh, and back/forward follow chats",
     "fr-FR": "Suivre les conversations avec l’adresse, l’actualisation et précédent ou suivant", },
    group: "interface",
    source: "conversation-links.zh-CN.md",
  },
  {
    id: "sign-in",
    title: {
      "zh-CN": "登录、注册与找回密码",
      "en-US": "Sign in, sign up, and reset your password",
      "fr-FR": "Se connecter, créer un compte et réinitialiser son mot de passe",
    },
    description: {
      "zh-CN": "登录方式、验证邮箱、重设密码和接受邀请",
      "en-US": "Ways to sign in, email verification, password reset, and invitations",
      "fr-FR": "Modes de connexion, vérification de l’e-mail, réinitialisation du mot de passe et invitations",
    },
    group: "getting_started",
    source: "sign-in.zh-CN.md",
  },
  {
    id: "language",
    title: { "zh-CN": "语言设置", "en-US": "Language settings", "fr-FR": "Paramètres de langue", },
    description: {
      "zh-CN": "切换界面语言，设置 AI 回答语言",
      "en-US": "Change the interface language and the AI answer language",
     "fr-FR": "Choisir la langue de l’interface et celle des réponses IA", },
    group: "interface",
    source: "language.zh-CN.md",
  },
  {
    id: "credits",
    title: { "zh-CN": "查看和补充额度", "en-US": "View and allocate credits", "fr-FR": "Consulter et attribuer des crédits", },
    description: {
      "zh-CN": "额度用完时会怎样，以及怎样为成员补充",
      "en-US": "Understand exhausted credits and allocate them to members",
     "fr-FR": "Comprendre les crédits épuisés et en attribuer aux membres", },
    group: "getting_started",
    source: "credits.zh-CN.md",
  },
  {
    id: "team-operations",
    title: { "zh-CN": "查看团队运营数据", "en-US": "View team operations data", "de-DE": "Betriebsdaten des Teams anzeigen", "fr-FR": "Consulter les données d’activité de l’équipe" },
    description: { "zh-CN": "查看每日登录、会话和 AI 使用情况", "en-US": "Review daily logins, sessions, and AI usage", "de-DE": "Tägliche Anmeldungen, Sitzungen und KI-Nutzung prüfen", "fr-FR": "Voir les connexions, sessions et usages IA quotidiens" },
    group: "team_access",
    source: "team-operations.zh-CN.md",
  },
  {
    id: "overview",
    title: {
      "zh-CN": "权限与访问说明书",
      "en-US": "Permissions and access guide",
     "fr-FR": "Guide des autorisations et des accès", },
    description: {
      "zh-CN": "先理解完整权限模型和配置流程",
      "en-US": "Understand the complete permission model and setup flow",
     "fr-FR": "Comprendre les périmètres d’autorisation et la configuration complète", },
    group: "team_access",
    source: "permissions.zh-CN.md",
  },
  {
    id: "security-sandbox",
    title: {
      "zh-CN": "安全与离线沙盒",
      "en-US": "Security and offline sandbox",
     "fr-FR": "Sécurité et environnement isolé hors ligne", },
    description: {
      "zh-CN": "了解执行环境、隔离边界和防护措施",
      "en-US": "Learn about the runtime, isolation boundaries, and safeguards",
     "fr-FR": "Comprendre l’exécution, l’isolation et les protections", },
    group: "security_data",
    source: "security-sandbox.zh-CN.md",
  },
  {
    id: "team-roles",
    title: { "zh-CN": "团队角色与权限", "en-US": "Team roles and permissions", "fr-FR": "Rôles et autorisations de l’équipe", },
    description: {
      "zh-CN": "了解四种团队角色的职责与管理边界",
      "en-US":
        "Understand the responsibilities and administrative limits of team roles",
     "fr-FR": "Comprendre les responsabilités et limites de chaque rôle", },
    group: "team_access",
    source: "permissions/team-roles.zh-CN.md",
  },
  {
    id: "space-roles",
    title: {
      "zh-CN": "空间角色与协作权限",
      "en-US": "Space roles and collaboration access",
     "fr-FR": "Rôles et collaboration dans l’espace", },
    description: {
      "zh-CN": "区分加入空间、管理空间和内容访问",
      "en-US":
        "Distinguish joining a space, managing it, and accessing its content",
     "fr-FR": "Distinguer appartenance, gestion et accès aux contenus", },
    group: "team_access",
    source: "permissions/space-roles.zh-CN.md",
  },
  {
    id: "space-access",
    title: { "zh-CN": "开通空间访问", "en-US": "Grant space access", "fr-FR": "Accorder un accès à l’espace", },
    description: {
      "zh-CN": "添加成员并选择空间角色",
      "en-US": "Add members and choose their space roles",
     "fr-FR": "Ajouter des membres et choisir leurs rôles", },
    group: "team_access",
    source: "permissions/space-access.zh-CN.md",
  },
  {
    id: "space-instructions",
    title: { "zh-CN": "空间说明", "en-US": "Space instructions", "fr-FR": "Instructions de l’espace" },
    description: {
      "zh-CN": "为空间里的每次对话写下共同遵循的说明",
      "en-US": "Write instructions that every conversation in a space follows",
      "fr-FR": "Rédiger les instructions suivies par chaque conversation d’un espace",
    },
    group: "team_access",
    source: "space-instructions.zh-CN.md",
  },
  {
    id: "team-members",
    title: { "zh-CN": "管理团队成员", "en-US": "Manage team members", "fr-FR": "Gérer les membres de l’équipe", },
    description: {
      "zh-CN": "邀请或添加同事、管理空间权限与移出团队",
      "en-US":
        "Invite or add colleagues, manage space access, and remove members",
     "fr-FR": "Inviter des collègues, gérer les espaces et retirer des membres", },
    group: "team_access",
    source: "team-members.zh-CN.md",
  },

  ...EXTRA_HELP_ARTICLES,
  {
    id: "team-role",
    title: {
      "zh-CN": "设置团队管理员",
      "en-US": "Assign a team administrator",
     "fr-FR": "Nommer un administrateur de l’équipe", },
    description: {
      "zh-CN": "委派团队治理职责",
      "en-US": "Delegate team governance responsibilities",
     "fr-FR": "Déléguer les responsabilités de gouvernance", },
    group: "team_access",
    source: "permissions/team-role.zh-CN.md",
  },
  {
    id: "agent-dashboard",
    title: { "en-US": "Review the Agent overview", "zh-CN": "查看智能体概览", "de-DE": "Agent-Übersicht prüfen", "fr-FR": "Consulter la vue d’ensemble des Agents" },
    description: { "en-US": "Team directory, defaults, listing checks, and pending approvals", "zh-CN": "团队目录、默认配置、上架检查与待审批事项", "de-DE": "Teamverzeichnis, Standards, Eintragsprüfungen und Freigaben", "fr-FR": "Répertoire d’équipe, réglages par défaut, contrôles et validations" },
    group: "agents_skills",
    source: "agent-dashboard.zh-CN.md",
  },
  {
    id: "agent-charts",
    title: { "en-US": "Explore Agent charts", "zh-CN": "浏览智能体排行榜", "de-DE": "Agent-Ranglisten entdecken", "fr-FR": "Explorer les classements des Agents" },
    description: { "en-US": "Top, Trending, recent updates, and chart availability", "zh-CN": "热门、上升、最近更新与榜单可用状态", "de-DE": "Top, Trends, aktuelle Änderungen und Verfügbarkeit", "fr-FR": "Popularité, progression, mises à jour et disponibilité" },
    group: "agents_skills",
    source: "agent-charts.zh-CN.md",
  },
  {
    id: "create-agent",
    title: {
      "zh-CN": "创建和管理自己的智能体",
      "en-US": "Create and manage your own Agent",
     "fr-FR": "Créer et gérer votre Agent", },
    description: {
      "zh-CN": "起草、保存草稿、确认创建并开始使用",
      "en-US": "Draft, save, confirm creation, and start using it",
     "fr-FR": "Préparer, enregistrer, confirmer et commencer à utiliser un Agent", },
    group: "agents_skills",
    source: "create-agent.zh-CN.md",
  },
  {
    id: "publish-agent",
    title: { "zh-CN": "发布 Agent", "en-US": "Publish an Agent", "fr-FR": "Publier un Agent", },
    description: {
      "zh-CN": "配置受众、检查并发布版本",
      "en-US": "Set the audience, review, and publish a version",
     "fr-FR": "Choisir le public, vérifier et publier une version", },
    group: "agents_skills",
    source: "permissions/publish-agent.zh-CN.md",
  },
  {
    id: "use-agent",
    title: { "zh-CN": "聘用和使用 Agent", "en-US": "Hire and use an Agent", "fr-FR": "Recruter et utiliser un Agent", },
    description: {
      "zh-CN": "发现、聘用、停用与解聘",
      "en-US": "Discover, hire, pause, and remove an Agent",
     "fr-FR": "Découvrir, recruter, mettre en pause et retirer un Agent", },
    group: "agents_skills",
    source: "permissions/use-agent.zh-CN.md",
  },
  {
    id: "publish-skill",
    title: { "zh-CN": "发布团队 Skill", "en-US": "Publish a team Skill", "fr-FR": "Publier un Skill d’équipe", },
    description: {
      "zh-CN": "提交、审批和更新团队版本",
      "en-US": "Submit, approve, and update a team version",
     "fr-FR": "Soumettre, approuver et actualiser une version d’équipe", },
    group: "agents_skills",
    source: "permissions/publish-skill.zh-CN.md",
  },
  {
    id: "configure-capabilities",
    title: {
      "zh-CN": "配置 Agent 能力",
      "en-US": "Configure Agent capabilities",
     "fr-FR": "Configurer les capacités d’un Agent", },
    description: {
      "zh-CN": "启用内置 Agent 并配置依赖",
      "en-US": "Enable built-in Agents and configure their dependencies",
     "fr-FR": "Activer les Agents intégrés et configurer leurs dépendances", },
    group: "agents_skills",
    source: "permissions/configure-capabilities.zh-CN.md",
  },
  {
    id: "agent-evaluation-with-coach",
    title: {
      "zh-CN": "和 Coach 一起改进智能体",
      "en-US": "Improve an Agent with Coach",
      "fr-FR": "Améliorer un Agent avec Coach",
    },
    description: {
      "zh-CN": "说出问题，在真实回答上核对 Coach 的判断，并了解留出用例",
      "en-US": "Say what's wrong, check Coach's verdicts on real answers, and use held-out cases",
      "fr-FR": "Dire ce qui ne va pas, vérifier les verdicts de Coach et utiliser les cas mis de côté",
    },
    group: "agents_skills",
    source: "agent-evaluation-with-coach.zh-CN.md",
    requiresCapability: "agent_eval_alignment",
  },
  {
    id: "builtin-tools",
    title: {
      "zh-CN": "内置第三方工具",
      "en-US": "Built-in third-party tools",
     "fr-FR": "Outils tiers intégrés", },
    description: {
      "zh-CN": "接入 Tavily 与北极九章并分配给智能体",
      "en-US": "Connect Tavily and Datarc, then assign them to Agents",
     "fr-FR": "Connecter Tavily, Datarc et Power BI et les affecter aux Agents", },
    group: "agents_skills",
    source: "builtin-tools.zh-CN.md",
  },
  {
    id: "google-workspace",
    title: {
      "zh-CN": "连接 Google Workspace",
      "en-US": "Connect Google Workspace",
      "fr-FR": "Connecter Google Workspace",
    },
    description: {
      "zh-CN": "配置团队连接、连接本人 Google 账号，读取和写回 Google 文件",
      "en-US": "Set up the team connection, connect your Google account, and read or write Google files",
      "fr-FR": "Configurer la connexion de l’équipe, connecter votre compte Google, puis lire ou écrire des fichiers Google",
    },
    group: "agents_skills",
    source: "google-workspace.zh-CN.md",
    requiresCapability: "google_workspace_feature",
  },
  {
    id: "team-mcp-tools",
    title: { "zh-CN": "添加团队 MCP 工具", "en-US": "Add a team MCP tool", "fr-FR": "Ajouter un outil MCP d’équipe", },
    description: {
      "zh-CN": "为团队智能体接入外部 MCP 服务",
      "en-US": "Connect an external MCP service for team Agents",
     "fr-FR": "Connecter un service MCP externe pour les Agents de l’équipe", },
    group: "agents_skills",
    source: "team-mcp-tools.zh-CN.md",
  },
  {
    id: "access-requests",
    title: {
      "zh-CN": "申请访问与处理申请",
      "en-US": "Request access and handle requests",
      "fr-FR": "Demander un accès et traiter les demandes",
    },
    description: {
      "zh-CN": "无权访问时在线申请，以及审批别人对你内容的申请",
      "en-US": "Ask for access you don't have yet, and approve requests for your content",
      "fr-FR": "Demander l’accès nécessaire et approuver les demandes concernant vos contenus",
    },
    group: "team_access",
    source: "access-requests.zh-CN.md",
    requiresCapability: "access_requests",
  },
  {
    id: "troubleshoot-access",
    title: { "zh-CN": "排查访问问题", "en-US": "Troubleshoot access", "fr-FR": "Résoudre les problèmes d’accès", },
    description: {
      "zh-CN": "按权限链定位失败环节",
      "en-US": "Locate the failed step in the permission chain",
     "fr-FR": "Identifier l’étape bloquée dans les autorisations", },
    group: "security_data",
    source: "permissions/troubleshoot-access.zh-CN.md",
  },
  {
    id: "reference",
    title: {
      "zh-CN": "角色与操作参考",
      "en-US": "Roles and actions reference",
     "fr-FR": "Référence des rôles et actions", },
    description: {
      "zh-CN": "快速核对谁能执行哪些操作",
      "en-US": "Quickly check who can perform each action",
     "fr-FR": "Vérifier rapidement qui peut effectuer chaque action", },
    group: "reference",
    source: "permissions-reference.zh-CN.md",
  },
  {
    // Title and body name the product with the {applicationName} placeholder; the Help page fills in the
    // team's application name (P2 batch 6 brand rule), so a branded team does not read "About Bowhead".
    id: "about",
    title: {
      "zh-CN": "关于 {applicationName}",
      "en-US": "About {applicationName}",
      "de-DE": "Über {applicationName}",
      "fr-FR": "À propos de {applicationName}",
    },
    description: {
      "zh-CN": "版本信息、更新说明与第三方软件许可",
      "en-US": "Version information, release notes, and third-party licenses",
      "de-DE": "Versionsangaben, Versionshinweise und Lizenzen von Drittanbietern",
      "fr-FR": "Informations de version, notes de version et licences tierces",
    },
    group: "reference",
    source: "about.zh-CN.md",
  },
] as const;

export const RELEASE_NOTES_DEFINITION: HelpArticleDefinition = {
  id: RELEASE_NOTES_ARTICLE_ID,
  title: {
    "zh-CN": "更新说明",
    "en-US": "Release notes",
    "de-DE": "Versionshinweise",
    "fr-FR": "Notes de version",
  },
  description: {
    "zh-CN": "按发布版本整理的产品变化",
    "en-US": "Product changes by released version",
    "de-DE": "Produktänderungen nach veröffentlichter Version",
    "fr-FR": "Évolutions du produit par version publiée",
  },
  group: null,
  source: "release-notes/index.zh-CN.md",
};

const GERMAN_HELP_METADATA: Record<
  string,
  { title: string; description: string }
> = {
  "personal-mcp": {
    title: "Persönlichen MCP-Dienst verbinden",
    description:
      "Externe Tools für einen privaten Agent auswählen und jeden Aufruf genehmigen",
  },
  "model-settings": {
    title: "Teamweite Modelleinstellungen verwalten",
    description:
      "Verbindungen, produktive Zuweisungen, externe Capabilities und Indizes verwalten",
  },
  home: {
    title: "Startseite des Hilfe-Centers",
    description: "Den nächsten Schritt für Ihr Ziel finden",
  },
  "getting-started": {
    title: "Eine Analyse starten",
    description:
      "Einen Bereich auswählen, das Ziel beschreiben und das Ergebnis prüfen",
  },
  "knowledge-base": {
    title: "Wissensdatenbank verwenden",
    description:
      "Material hochladen, Dateistatus prüfen und es in Chats verwenden",
  },
  credits: {
    title: "Kontingent prüfen und zuweisen",
    description:
      "Aufgebrauchtes Kontingent verstehen und Mitgliedern zuweisen",
  },
  overview: {
    title: "Anleitung zu Berechtigungen und Zugriff",
    description: "Berechtigungsmodell und Einrichtungsablauf verstehen",
  },
  "access-requests": {
    title: "Zugriff anfragen und Anfragen bearbeiten",
    description:
      "Fehlenden Zugriff anfragen und Zugriffsanfragen für eigene Inhalte genehmigen",
  },
  "security-sandbox": {
    title: "Sicherheit und Offline-Sandbox",
    description:
      "Laufzeitumgebung, Isolationsgrenzen und Schutzmaßnahmen kennenlernen",
  },
  "team-roles": {
    title: "Teamrollen und Berechtigungen",
    description: "Aufgaben und Verwaltungsgrenzen der Teamrollen verstehen",
  },
  "space-roles": {
    title: "Bereichsrollen und Zusammenarbeit",
    description:
      "Mitgliedschaft, Verwaltung und Zugriff auf Inhalte eines Bereichs unterscheiden",
  },
  "space-access": {
    title: "Zugriff auf einen Bereich gewähren",
    description: "Mitglieder hinzufügen und ihre Bereichsrollen festlegen",
  },
  "space-instructions": {
    title: "Bereichsanweisungen",
    description: "Anweisungen schreiben, die jede Unterhaltung in einem Bereich befolgt",
  },
  "team-role": {
    title: "Teamadministration zuweisen",
    description: "Aufgaben der Teamverwaltung übertragen",
  },
  "publish-agent": {
    title: "Agent veröffentlichen",
    description: "Zielgruppe festlegen, Version prüfen und veröffentlichen",
  },
  "use-agent": {
    title: "Agent übernehmen und verwenden",
    description: "Agent finden, übernehmen, pausieren und entfernen",
  },
  "publish-skill": {
    title: "Team-Skill veröffentlichen",
    description: "Teamversion einreichen, freigeben und aktualisieren",
  },
  "configure-capabilities": {
    title: "Agent-Capabilities konfigurieren",
    description:
      "Integrierte Agents aktivieren und ihre Abhängigkeiten konfigurieren",
  },
  "troubleshoot-access": {
    title: "Zugriffsprobleme beheben",
    description: "Fehlerhafte Schritte in der Berechtigungskette finden",
  },
  reference: {
    title: "Rollen und Aktionen nachschlagen",
    description: "Schnell prüfen, wer welche Aktionen ausführen darf",
  },
  "team-mcp-tools": {
    title: "MCP-Tool für das Team hinzufügen",
    description: "Einen externen MCP-Dienst für das Team konfigurieren",
  },
  "agent-evaluation-with-coach": {
    title: "Einen Agent mit Coach verbessern",
    description: "Sagen, was nicht stimmt, Coachs Urteile an echten Antworten prüfen und zurückgehaltene Testfälle nutzen",
  },
  "google-workspace": {
    title: "Google Workspace verbinden",
    description:
      "Team-Verbindung einrichten, eigenes Google-Konto verbinden und Google-Dateien lesen oder schreiben",
  },
  "builtin-tools": {
    title: "Integrierte Drittanbieter-Tools",
    description:
      "Tavily, Datarc und Power BI verbinden und Team-Agents zuweisen",
  },
  "sign-in": {
    title: "Anmelden, registrieren und Passwort zurücksetzen",
    description:
      "Anmeldewege, E-Mail-Bestätigung, Zurücksetzen des Passworts und Einladungen",
  },
  "conversation-links": {
    title: "Unterhaltungslinks und Browsernavigation",
    description:
      "Unterhaltungen teilen, aktualisieren und mit Zurück oder Vorwärts öffnen",
  },
  "conversation-workbench-actions": {
    title: "Dateien, Tastenkürzel und Hinweise auf der Unterhaltungsseite",
    description:
      "Dateien ablegen, Bereiche anpassen und Tastaturaktionen verwenden",
  },
  "conversation-workbench": {
    title: "Aufbau der Unterhaltungsseite",
    description:
      "Fortschritt, Antworten, Zitate, Ergebnisse und Berichte auf der Unterhaltungsseite",
  },
  "create-agent": {
    title: "Einen eigenen Agent erstellen und verwalten",
    description:
      "Erstellung, Konfiguration, Entwürfe und Verwendung eigener Agents",
  },
  email: {
    title: "E-Mail senden",
    description:
      "E-Mails im Chat bestätigen und den Team-Maildienst konfigurieren",
  },

  language: {
    title: "Spracheinstellungen",
    description:
      "Sprache der Oberfläche und Sprache der KI-Antworten auswählen",
  },
  menus: {
    title: "Menüs verwenden",
    description:
      "Einstellungsverzeichnis, Menüs auf Mobilgeräten, Bestätigungen, nicht verfügbare Aktionen und Tastaturbedienung",
  },
  "mobile-dialogs-undo": {
    title: "Dialoge und Rückgängig auf Mobilgeräten",
    description:
      "Bedienfelder auf schmalen Bildschirmen und rückgängig machbare Aktionen",
  },
  "revise-report": {
    title: "Einen bestehenden Bericht überarbeiten",
    description:
      "Umfang, Diagramme oder Inhalte im selben Chat ändern",
  },
  "row-actions": {
    title: "Weitere Aktionen in Listen",
    description:
      "Bearbeiten, herunterladen und löschen über das Zeilenmenü; Verwaltungslisten auf dem Smartphone",
  },
  "settings-save": {
    title: "Einstellungen speichern",
    description:
      "Sofort gespeicherte Auswahl und Änderungen mit der Speicherleiste sichern",
  },
  "team-capabilities": {
    title: "Team-Plugins verwenden",
    description:
      "Tools und Skills kombinieren und mehreren Agents bereitstellen",
  },
  "team-members": {
    title: "Teammitglieder verwalten",
    description:
      "Kolleginnen und Kollegen einladen, Bereichszugriff verwalten und Credits ansehen",
  },
};

export function helpContentLocale(): Locale {
  return buildLocale();
}

/**
 * Chinese and English Help bodies are complete sets (one missing article fails the build); other UI languages (German and future additions)
 * read the English version for any missing translation, never falling back to Chinese.
 */
function hasCompleteHelpSources(locale: Locale): boolean {
  return locale === "zh-CN" || locale === "en-US";
}

function localizedSource(source: string, locale: Locale, root?: string): string {
  const localized = source.replace(".zh-CN.md", `.${locale}.md`);
  if (!hasCompleteHelpSources(locale) && root && !existsSync(path.join(root, localized))) {
    // Help articles without a translation in this language remain readable in English.
    return source.replace(".zh-CN.md", ".en-US.md");
  }
  return localized;
}

function sourceToArticle(locale: Locale): Map<string, string> {
  return new Map(
    [...HELP_ARTICLE_DEFINITIONS, RELEASE_NOTES_DEFINITION].flatMap(({ id, source }) => {
      const localized = localizedSource(source, locale);
      if (!hasCompleteHelpSources(locale)) {
        return [
          [localized, id],
          [source.replace(".zh-CN.md", ".en-US.md"), id],
        ];
      }
      return [[localized, id]];
    }),
  );
}

function resolveHelpRoot(): string {
  const candidates = [
    path.resolve(process.cwd(), "docs/help"),
    path.resolve(process.cwd(), "../../docs/help"),
  ];
  const root = candidates.find((candidate) => {
    try {
      readFileSync(path.join(candidate, "permissions.zh-CN.md"), "utf8");
      return true;
    } catch {
      return false;
    }
  });
  if (!root) {
    throw new Error("Bowhead Help content directory could not be resolved");
  }
  return root;
}

export function rewriteHelpLinks(
  markdown: string,
  source: string,
  locale: Locale = helpContentLocale(),
): string {
  const sources = sourceToArticle(locale);
  const sourceDirectory = path.posix.dirname(source);
  return markdown.replace(/\]\(([^)]+)\)/g, (match, rawTarget: string) => {
    const target = rawTarget.trim();
    if (
      !target ||
      target.startsWith("#") ||
      target.startsWith("/") ||
      /^[a-z][a-z\d+.-]*:/i.test(target)
    ) {
      return match;
    }

    const [pathname, fragment] = target.split("#", 2);
    const normalizedSource = path.posix.normalize(
      path.posix.join(sourceDirectory, pathname),
    );
    const articleId = sources.get(normalizedSource);
    if (!articleId) return match;
    const hash = fragment ? `#${fragment}` : "";
    return articleId === RELEASE_NOTES_ARTICLE_ID
      ? `](/release-notes/${hash})`
      : `](/help/?article=${articleId}${hash})`;
  });
}

export function loadHelpArticles(
  root: string | undefined = undefined,
  locale: Locale = helpContentLocale(),
): HelpArticle[] {
  const resolvedRoot = root ?? resolveHelpRoot();
  return HELP_ARTICLE_DEFINITIONS.map(
    ({ source, title, description, ...article }): HelpArticle => {
      const sourceRoot = root ?? extraHelpRoot(source) ?? resolvedRoot;
      const localized = localizedSource(source, locale, sourceRoot);
      const german = locale === "de-DE" ? GERMAN_HELP_METADATA[article.id] : null;
      return {
        ...article,
        title: german?.title ?? title[locale] ?? title["en-US"],
        description:
          german?.description ?? description[locale] ?? description["en-US"],
        content: rewriteHelpLinks(
          readFileSync(path.join(sourceRoot, localized), "utf8"),
          localized,
          locale,
        ),
      };
    },
  );
}

/** Loads only the standalone release notes, without the Help Center article payload. */
export function loadReleaseNotes(
  root: string | undefined = undefined,
  locale: Locale = helpContentLocale(),
): HelpArticle {
  const { source, title, description, ...article } = RELEASE_NOTES_DEFINITION;
  const resolvedRoot = root ?? resolveHelpRoot();
  return {
    ...article,
    title: title[locale] ?? title["en-US"],
    description: description[locale] ?? description["en-US"],
    ...composeReleaseNotes(
      resolvedRoot,
      localizedSource(source, locale, resolvedRoot),
      locale,
    ),
  };
}

/** Drops a leading YAML front-matter block (release pages carry title / summary metadata for maintainers). */
export function stripFrontMatter(markdown: string): string {
  return markdown.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n+/, "");
}

/** Moves every ATX heading outside fenced code one level down (`#` → `##`), up to level 6. */
function demoteHeadings(markdown: string): string {
  let fenced = false;
  return markdown
    .split("\n")
    .map((line) => {
      if (/^\s*(```|~~~)/.test(line)) fenced = !fenced;
      if (fenced || !/^#{1,5} /.test(line)) return line;
      return `#${line}`;
    })
    .join("\n");
}

/** Compares dotted numeric versions ("1.5.0" vs "1.10.2"); missing parts count as 0. */
export function compareVersions(left: string, right: string): number {
  const a = left.split(".").map(Number);
  const b = right.split(".").map(Number);
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const difference = (a[index] ?? 0) - (b[index] ?? 0);
    if (difference !== 0) return difference;
  }
  return 0;
}

/**
 * Version pages the application publishes, in the index's newest-first order: released pages (`v<version>.*.md`)
 * from {@link FIRST_PUBLISHED_RELEASE} on. The page progressively displays this complete history; Unreleased is a
 * maintainers' staging area and is never published.
 */
export function publishedReleaseSources(indexMarkdown: string, indexSource: string): string[] {
  const directory = path.posix.dirname(indexSource);
  return Array.from(indexMarkdown.matchAll(/^- \[[^\]]*\]\(([^)]+\.md)\)/gm), (match) =>
    path.posix.normalize(path.posix.join(directory, match[1])),
  )
    .filter((source) => {
      const version = /\/v(\d+(?:\.\d+)*)\.[a-z]{2}-[A-Z]{2}\.md$/.exec(source)?.[1];
      return version !== undefined && compareVersions(version, FIRST_PUBLISHED_RELEASE) >= 0;
    });
}

/**
 * Release notes for the standalone page: the index introduction followed by the published version pages, newest
 * first, each under its own heading. Version pages are not separate Help articles, so the index's link list is
 * replaced by the pages themselves; German and French read the English page when a translation is missing.
 */
function composeReleaseNotes(root: string, indexSource: string, locale: Locale): Pick<HelpArticle, "content" | "releaseDates"> {
  const index = stripFrontMatter(readFileSync(path.join(root, indexSource), "utf8"));
  const listStart = index.search(/^- \[/m);
  const intro = (listStart >= 0 ? index.slice(0, listStart) : index)
    // Drop the "Latest" heading that introduced the link list the pages replace.
    .replace(/\n## [^\n]*\s*$/, "\n")
    .trim();
  const releaseDates: Record<string, string> = {};
  const pages = publishedReleaseSources(index, indexSource).map((source) => {
    const english = source.replace(/\.[a-z]{2}-[A-Z]{2}\.md$/, ".en-US.md");
    const file = existsSync(path.join(root, source)) ? source : english;
    const markdown = readFileSync(path.join(root, file), "utf8");
    const frontMatter = /^---\r?\n([\s\S]*?)\r?\n---/.exec(markdown)?.[1] ?? "";
    const date = /^released:\s*["']?(\d{4}-\d{2}-\d{2})["']?\s*$/m.exec(frontMatter)?.[1];
    const title = /^# (v[\d.]+)\s*$/m.exec(stripFrontMatter(markdown))?.[1];
    if (date && title && !Number.isNaN(Date.parse(date))) releaseDates[title] = date;
    return demoteHeadings(stripFrontMatter(markdown).trim());
  });
  return { content: rewriteHelpLinks(`${[intro, ...pages].join("\n\n")}\n`, indexSource, locale), releaseDates };
}
