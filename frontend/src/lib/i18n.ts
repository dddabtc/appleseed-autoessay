/**
 * UI language ≠ paper language (HANDOFF §0.2 H 设计原则).
 *
 * - **UI language** (this module's `UILanguage`): only controls
 *   localized chrome — labels, banners, error messages, help text.
 *   Persisted in `localStorage[autoessay.ui_language]`. Switched by
 *   the header EN/ZH/JA toggle. Independent of any project state.
 * - **Paper / project language** (`Project.language` field): controls
 *   what language the writing steps output via
 *   `language_directive(...)`. Set per
 *   project at create time on NewRunPage.
 *
 * The two are intentionally independent. A Chinese user can write a
 * Chinese paper while reading the UI in English (or vice versa).
 * NewRunPage may default the paper-language picker to the current
 * UI language for convenience, but once the user picks they stay
 * independent.
 */

import { useMemo, useSyncExternalStore } from "react";

export type UILanguage = "en" | "zh" | "ja";

const STORAGE_KEY = "autoessay.ui_language";
const SUPPORTED: readonly UILanguage[] = ["en", "zh", "ja"] as const;

function readStored(): UILanguage {
  if (typeof window === "undefined") return "en";
  const raw = window.localStorage.getItem(STORAGE_KEY);
  if (raw && (SUPPORTED as readonly string[]).includes(raw)) {
    return raw as UILanguage;
  }
  // Auto-detect from browser; fall back to en for anything we don't support.
  const browser = (navigator.language ?? "en").toLowerCase();
  if (browser.startsWith("zh")) return "zh";
  if (browser.startsWith("ja")) return "ja";
  return "en";
}

const listeners = new Set<() => void>();

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function setUILanguage(lang: UILanguage): void {
  if (typeof window !== "undefined") {
    window.localStorage.setItem(STORAGE_KEY, lang);
  }
  for (const fn of listeners) fn();
}

function getSnapshot(): UILanguage {
  return readStored();
}

export function useUILanguage(): [UILanguage, (lang: UILanguage) => void] {
  const ssrSnapshot = (): UILanguage => "en";
  const lang = useSyncExternalStore(subscribe, getSnapshot, ssrSnapshot);
  return [lang, setUILanguage];
}

// --- translation table ---------------------------------------------------

type Strings = Record<UILanguage, string>;
type Catalog = Record<string, Strings>;

const CATALOG: Catalog = {
  // essays list page (i18n keys retain `runs.*` for stability — values use "essays")
  "runs.section_label": { en: "Essays", zh: "论文", ja: "論文一覧" },
  "runs.heading": {
    en: "My essays",
    zh: "我的论文",
    ja: "論文一覧",
  },
  "runs.list_hint": {
    en: "Tap a card to open its workspace and continue from where it stopped.",
    zh: "点击下方卡片即可进入工作台，从上次停下的位置继续。",
    ja: "カードをタップすると作業画面が開き、続きから再開できます。",
  },
  "runs.empty_hint": {
    en: "No essays yet. Create your first project to start.",
    zh: "还没有论文。创建第一个项目即可开始。",
    ja: "まだ論文はありません。最初のプロジェクトを作成して始めましょう。",
  },
  "runs.cta_new_project": {
    en: "New essay",
    zh: "新建论文",
    ja: "新規論文",
  },
  "runs.domain_label": { en: "Domain", zh: "领域", ja: "分野" },
  "runs.loading": { en: "Loading...", zh: "加载中…", ja: "読み込み中…" },
  "runs.time_just_now": { en: "just now", zh: "刚才", ja: "たった今" },
  "runs.search_placeholder": {
    en: "Search by title…",
    zh: "按题目搜索…",
    ja: "題名で検索…",
  },
  "runs.search_no_results": {
    en: "No essays match your search.",
    zh: "没有与搜索匹配的论文。",
    ja: "検索に一致する論文はありません。",
  },
  "runs.search_clear": { en: "Clear", zh: "清除", ja: "クリア" },
  "runs.show_deleted": {
    en: "Show deleted",
    zh: "显示已删除",
    ja: "削除済みを表示",
  },
  "runs.deleted_badge": {
    en: "Deleted",
    zh: "已删除",
    ja: "削除済み",
  },
  "runs.delete_button": {
    en: "Delete essay",
    zh: "删除论文",
    ja: "論文を削除",
  },
  "runs.delete_run_button": { en: "Delete", zh: "删除", ja: "削除" },
  "runs.restore_button": {
    en: "Restore essay",
    zh: "恢复论文",
    ja: "論文を復元",
  },
  "runs.restore_run_button": {
    en: "Restore run",
    zh: "恢复本次运行",
    ja: "この実行を復元",
  },
  "runs.delete_confirm": {
    en: "Delete this essay? You can restore it later.",
    zh: "删除该论文？之后还可以恢复。",
    ja: "この論文を削除しますか？後で復元できます。",
  },
  "runs.delete_run_confirm": {
    en: "Delete this paper run?",
    zh: "删除此论文 run 吗？",
    ja: "この論文 run を削除しますか？",
  },
  "runs.restore_run_confirm": {
    en: "Restore this run? It will reappear in the active runs list for this essay.",
    zh: "恢复本次运行？它会重新出现在该论文的可用运行列表中。",
    ja: "この実行を復元しますか？この論文の有効な実行一覧に戻ります。",
  },
  "runs.delete_failed": {
    en: "Delete failed",
    zh: "删除失败",
    ja: "削除に失敗しました",
  },
  "runs.delete_run_failed": {
    en: "Delete run failed",
    zh: "删除运行失败",
    ja: "実行の削除に失敗しました",
  },
  "runs.restore_failed": {
    en: "Restore failed",
    zh: "恢复失败",
    ja: "復元に失敗しました",
  },
  "runs.restore_run_failed": {
    en: "Restore run failed",
    zh: "恢复运行失败",
    ja: "実行の復元に失敗しました",
  },

  // Per-state human labels — codex-AGREEd 9-step mapping.
  // (N/9) prefix on phase states; terminal/error states get no prefix.
  "runs.state.TOPIC_ENTERED": {
    en: "(1/9) Topic captured",
    zh: "(1/9) 已记录题目",
    ja: "(1/9) テーマを記録",
  },
  "runs.state.DOMAIN_LOADED": {
    en: "(1/9) Loading domain…",
    zh: "(1/9) 加载领域…",
    ja: "(1/9) 分野を読込中…",
  },
  "runs.state.PROPOSAL_DRAFTING": {
    en: "(1/9) Drafting proposal…",
    zh: "(1/9) 正在起草提案…",
    ja: "(1/9) 提案を作成中…",
  },
  "runs.state.USER_PROPOSAL_REVIEW": {
    en: "(1/9) Review proposal",
    zh: "(1/9) 审阅提案",
    ja: "(1/9) 提案を確認",
  },
  "runs.state.SCOUT_RUNNING": {
    en: "(2/9) Searching sources…",
    zh: "(2/9) 检索文献中…",
    ja: "(2/9) 文献を検索中…",
  },
  "runs.state.USER_SEARCH_REVIEW": {
    en: "(2/9) Review found sources",
    zh: "(2/9) 审阅检索结果",
    ja: "(2/9) 検索結果を確認",
  },
  "runs.state.CURATOR_RUNNING": {
    en: "(3/9) Ranking sources…",
    zh: "(3/9) 评估文献中…",
    ja: "(3/9) 文献を評価中…",
  },
  "runs.state.USER_DEEP_DIVE_REVIEW": {
    en: "(3/9) Review shortlist",
    zh: "(3/9) 审阅精选清单",
    ja: "(3/9) ショートリストを確認",
  },
  "runs.state.SYNTHESIZER_RUNNING": {
    en: "(4/9) Reading sources…",
    zh: "(4/9) 阅读文献中…",
    ja: "(4/9) 文献を読込中…",
  },
  "runs.state.USER_FIELD_REVIEW": {
    en: "(4/9) Review material diagnostic",
    zh: "(4/9) 审阅资料诊断",
    ja: "(4/9) 資料診断を確認",
  },
  "runs.state.IDEATOR_RUNNING": {
    en: "(5/9) Ideating angles…",
    zh: "(5/9) 生成角度中…",
    ja: "(5/9) 角度を生成中…",
  },
  "runs.state.USER_NOVELTY_REVIEW": {
    en: "(5/9) Pick an angle",
    zh: "(5/9) 选择角度",
    ja: "(5/9) 角度を選択",
  },
  "runs.state.DRAFTER_RUNNING": {
    en: "(6/9) Drafting paper…",
    zh: "(6/9) 正在撰写论文…",
    ja: "(6/9) 論文を執筆中…",
  },
  "runs.state.STYLIST_RUNNING": {
    en: "(7/9) Polishing prose…",
    zh: "(7/9) 润色文字…",
    ja: "(7/9) 文章を推敲中…",
  },
  // Slice E final_rewrite phase, opt-in via
  // ``AUTOESSAY_FINAL_REWRITE_ENABLED``. Default ON; sits between stylist (7/9)
  // and critic (8/9); we keep it in the (7/9) bracket so the visible
  // numbering doesn't bump downstream when the toggle flips.
  "runs.state.REWRITE_RUNNING": {
    en: "(7/9) Final rewrite for compliance…",
    zh: "(7/9) 最终改写以满足合规要求…",
    ja: "(7/9) コンプライアンス対応の最終書き直し中…",
  },
  "runs.state.CRITIC_RUNNING": {
    en: "(8/9) Reviewing draft…",
    zh: "(8/9) 评审稿件中…",
    ja: "(8/9) 原稿を査読中…",
  },
  "runs.state.USER_REVISION_REVIEW": {
    en: "(8/9) Review revisions",
    zh: "(8/9) 审阅修改",
    ja: "(8/9) 修正を確認",
  },
  "runs.state.USER_EXTERNAL_SCAN_APPROVAL": {
    en: "(8/9) Approve external scan",
    zh: "(8/9) 批准外部检测",
    ja: "(8/9) 外部スキャンを承認",
  },
  "runs.state.INTEGRITY_RUNNING": {
    en: "(9/9) Running integrity check…",
    zh: "(9/9) 学术诚信检测中…",
    ja: "(9/9) 整合性チェック中…",
  },
  "runs.state.USER_INTEGRITY_REVIEW": {
    en: "(9/9) Review integrity findings",
    zh: "(9/9) 审阅诚信检测结果",
    ja: "(9/9) 整合性結果を確認",
  },
  "runs.state.USER_FINAL_ACCEPTANCE": {
    en: "(9/9) Final acceptance",
    zh: "(9/9) 最终确认",
    ja: "(9/9) 最終承認",
  },
  "runs.state.EXPORTS_RUNNING": {
    en: "(9/9) Exporting…",
    zh: "(9/9) 导出中…",
    ja: "(9/9) エクスポート中…",
  },
  "runs.state.EXPORTS_DONE": { en: "Done", zh: "完成", ja: "完了" },
  "runs.state.CANCELLED": {
    en: "Cancelled",
    zh: "已取消",
    ja: "キャンセル済み",
  },
  "runs.state.FAILED_FIXABLE": {
    en: "Needs fix",
    zh: "需修复",
    ja: "要対応",
  },
  "runs.state.FAILED_NEEDS_USER": {
    en: "Needs your input",
    zh: "需用户处理",
    ja: "ユーザー対応が必要",
  },
  "runs.state.FAILED_VENDOR": {
    en: "External service failure",
    zh: "外部服务失败",
    ja: "外部サービス障害",
  },
  "runs.state.FAILED_POLICY": {
    en: "Blocked by policy",
    zh: "策略阻断",
    ja: "ポリシーで停止",
  },
  "runs.state.EXPRESS_RUNNING": {
    en: "Express writing…",
    zh: "Express 正在写作…",
    ja: "Express 執筆中…",
  },
  "runs.state.EXPRESS_DONE": {
    en: "Express done",
    zh: "Express 完成",
    ja: "Express 完了",
  },
  "runs.state.EXPRESS_FAILED": {
    en: "Express failed",
    zh: "Express 失败",
    ja: "Express 失敗",
  },

  // header nav
  "nav.runs": { en: "Essays", zh: "论文", ja: "論文一覧" },
  "nav.new_run": { en: "New essay", zh: "新建论文", ja: "新規論文" },
  "nav.corpus": { en: "Corpus", zh: "语料库", ja: "コーパス" },
  "nav.help": { en: "Help", zh: "帮助", ja: "ヘルプ" },

  // workspace chrome
  "workspace.eyebrow": { en: "Workspace", zh: "工作台", ja: "作業画面" },
  "workspace.heading_default": {
    en: "Essay workspace",
    zh: "论文工作台",
    ja: "論文作業画面",
  },
  "workspace.state_and_checkpoints": {
    en: "Status and history",
    zh: "状态与历史",
    ja: "状態と履歴",
  },
  "workspace.exports_done_banner": {
    en: "Exports complete — paper ready to download from the Exports tab.",
    zh: '导出完成——可在 "导出" 标签页下载论文。',
    ja: 'エクスポート完了 — "エクスポート" タブから論文をダウンロードできます。',
  },
  "workspace.express.eyebrow": {
    en: "Express result",
    zh: "Express 结果",
    ja: "Express 結果",
  },
  "workspace.express.heading": {
    en: "Compact transparency panel",
    zh: "透明度摘要面板",
    ja: "透明性サマリーパネル",
  },
  "workspace.express.summary": {
    en: "Express has no phase preview. This panel shows token usage, the audit summary, the outline, and the final manuscript.",
    zh: "Express 没有阶段预览。此处展示用量、audit 摘要、大纲和最终正文。",
    ja: "Express にはフェーズプレビューがありません。使用量、監査、大綱、最終原稿を表示します。",
  },
  "workspace.express.regenerate": {
    en: "Regenerate express",
    zh: "重新生成 Express",
    ja: "Express を再生成",
  },
  "workspace.express.start_deep": {
    en: "Start new Deep run",
    zh: "启动新的 Deep run",
    ja: "新しい Deep 実行を開始",
  },
  "workspace.express.creating": {
    en: "Creating…",
    zh: "创建中…",
    ja: "作成中…",
  },
  "workspace.express.tokens": {
    en: "Tokens actual / cap",
    zh: "Token 实际 / 上限",
    ja: "Tokens 実績 / 上限",
  },
  "workspace.express.provider": {
    en: "Provider / model",
    zh: "Provider / model",
    ja: "Provider / model",
  },
  "workspace.express.audit": {
    en: "Audit critic",
    zh: "Audit critic",
    ja: "Audit critic",
  },
  "workspace.express.outline": {
    en: "Outline / section map",
    zh: "大纲 / 章节映射",
    ja: "大綱 / セクションマップ",
  },
  "workspace.express.outline_pending": {
    en: "No parseable headings yet.",
    zh: "暂无可解析标题。",
    ja: "解析可能な見出しはまだありません。",
  },
  "workspace.express.preview": {
    en: "Final manuscript preview",
    zh: "最终正文预览",
    ja: "最終原稿プレビュー",
  },
  "workspace.express.preview_pending": {
    en: "Final manuscript is not available yet.",
    zh: "最终正文尚未生成。",
    ja: "最終原稿はまだありません。",
  },
  "workspace.express.load_failed": {
    en: "Express transparency artifacts are not available yet.",
    zh: "Express 透明度产物尚未可用。",
    ja: "Express の透明性成果物はまだ利用できません。",
  },
  "workspace.back_to_runs": {
    en: "Back to essays",
    zh: "回到论文列表",
    ja: "論文一覧に戻る",
  },
  "workspace.start_new_project": {
    en: "Start a new project",
    zh: "开始新项目",
    ja: "新しいプロジェクトを開始",
  },
  "workspace.recent_events": {
    en: "Recent events",
    zh: "最近事件",
    ja: "最近のイベント",
  },
  "workspace.checkpoint_eyebrow": {
    en: "Saved review",
    zh: "已保存审核",
    ja: "保存済みレビュー",
  },
  "workspace.run_state": {
    en: "Essay state",
    zh: "论文状态",
    ja: "論文ステータス",
  },
  "workspace.state_label": { en: "State", zh: "状态", ja: "状態" },
  "workspace.last_event_label": {
    en: "Last event",
    zh: "最近事件",
    ja: "最終イベント",
  },
  "workspace.loading": {
    en: "Loading essay...",
    zh: "加载论文中…",
    ja: "論文を読み込み中…",
  },
  "workspace.unknown": { en: "unknown", zh: "未知", ja: "不明" },
  "workspace.none": { en: "none", zh: "无", ja: "なし" },
  "workspace.cancel": { en: "Cancel", zh: "取消", ja: "キャンセル" },
  "workspace.paper_language_edit_hint": {
    en: "Click to change paper language for later phases.",
    zh: "点击修改后续阶段的论文语言。",
    ja: "クリックして以降のフェーズで使用する論文言語を変更。",
  },
  "workspace.errors.language_update_failed": {
    en: "Failed to update paper language",
    zh: "更新论文语言失败",
    ja: "論文言語の更新に失敗しました",
  },
  "workspace.stream_disconnected": {
    en: "Live event stream is reconnecting...",
    zh: "事件流正在重连…",
    ja: "イベントストリーム再接続中…",
  },
  // workspace tabs
  "workspace.tab.console": { en: "Console", zh: "控制台", ja: "コンソール" },
  "workspace.tab.proposal": { en: "Proposal", zh: "提案", ja: "提案" },
  "workspace.tab.sources": { en: "Sources", zh: "文献", ja: "文献" },
  "workspace.tab.synthesis": {
    en: "Synthesis",
    zh: "综述",
    ja: "統合",
  },
  "workspace.tab.novelty": { en: "Novelty", zh: "新颖性", ja: "新規性" },
  "workspace.tab.draft": { en: "Draft", zh: "草稿", ja: "草稿" },
  "workspace.tab.style": { en: "Style", zh: "文风", ja: "文体" },
  "workspace.tab.review": { en: "Review", zh: "评审", ja: "査読" },
  "workspace.tab.integrity": {
    en: "Integrity",
    zh: "检测",
    ja: "検査",
  },
  "workspace.tab.export": { en: "Export", zh: "导出", ja: "エクスポート" },
  "workspace.tab.corpus": { en: "Corpus", zh: "语料库", ja: "コーパス" },
  "workspace.tab.lens": {
    en: "Framework lens",
    zh: "框架镜框",
    ja: "理論的レンズ",
  },

  // PR-B3: per-project corpus management surfaced inline in the
  // workspace, so users no longer have to leave the workspace and
  // navigate to /corpus to upload prior papers or change which
  // global corpora this project uses.
  "workspace.corpus.heading": {
    en: "Corpus for this project",
    zh: "本项目的语料",
    ja: "このプロジェクトのコーパス",
  },
  "workspace.corpus.intro": {
    en: "Prior papers used as references for drafting and deduplication.",
    zh: "用于撰写与查重的参考材料。",
    ja: "起草と重複検出に用いる参考資料。",
  },
  "workspace.corpus.section.project": {
    en: "Project documents",
    zh: "本项目文档",
    ja: "プロジェクト固有の文書",
  },
  "workspace.corpus.section.globals": {
    en: "Selected from your global corpus",
    zh: "从全局语料库中选择",
    ja: "全体コーパスから選択",
  },
  "workspace.corpus.upload.label": {
    en: "Upload a prior paper to this project",
    zh: "为本项目上传一份既往论文",
    ja: "このプロジェクトに既存論文をアップロード",
  },
  "workspace.corpus.upload.cta": {
    en: "Choose file",
    zh: "选择文件",
    ja: "ファイルを選択",
  },
  "workspace.corpus.upload.hint": {
    en: "PDF / DOCX / MD / TXT. Up to 30 MB.",
    zh: "支持 PDF / DOCX / MD / TXT，最大 30 MB。",
    ja: "PDF / DOCX / MD / TXT、最大 30 MB。",
  },
  "workspace.corpus.upload.error.too_large": {
    en: "File exceeds 30 MB limit",
    zh: "文件超过 30 MB 限制",
    ja: "ファイルが 30 MB の上限を超えています",
  },
  "workspace.corpus.upload.error.empty": {
    en: "File is empty",
    zh: "文件为空",
    ja: "ファイルが空です",
  },
  "workspace.corpus.upload.uploading": {
    en: "Uploading…",
    zh: "上传中…",
    ja: "アップロード中…",
  },
  "workspace.corpus.column.title": { en: "Title", zh: "标题", ja: "タイトル" },
  "workspace.corpus.column.status": {
    en: "Status",
    zh: "状态",
    ja: "状態",
  },
  "workspace.corpus.column.size": { en: "Size", zh: "大小", ja: "サイズ" },
  "workspace.corpus.column.uploaded": {
    en: "Uploaded",
    zh: "上传时间",
    ja: "アップロード日",
  },
  "workspace.corpus.column.docs": {
    en: "Docs",
    zh: "文档数",
    ja: "文書数",
  },
  "workspace.corpus.column.use": {
    en: "Use in this project",
    zh: "本项目使用",
    ja: "このプロジェクトで使用",
  },
  "workspace.corpus.empty.no_project_docs": {
    en: "No project documents yet.",
    zh: "暂无本项目文档。",
    ja: "プロジェクト文書はまだありません。",
  },
  "workspace.corpus.empty.no_globals": {
    en: "You haven't created any global corpus collections yet.",
    zh: "尚未创建任何全局语料集合。",
    ja: "全体コーパスはまだ作成されていません。",
  },
  "workspace.corpus.empty.fully_empty": {
    en: "This project has no prior papers. Upload one above, or select from your global corpus once you create some on the corpus page.",
    zh: "本项目还没有任何既往论文。可在上方上传，或在语料库页面创建全局集合后回到此处选择。",
    ja: "このプロジェクトに既存論文はまだありません。上のフォームからアップロードするか、コーパスページで全体コーパスを作成してから選択してください。",
  },
  "workspace.corpus.warn.stale_after_draft": {
    en: "Changing corpus selection after the draft has run only affects future runs of drafter. Rerun drafter to apply the new corpus to deduplication.",
    zh: "草稿生成后再修改语料选择，仅对再次运行起草节点时生效。如需让查重立即用上新语料，请重跑起草节点。",
    ja: "草稿生成後にコーパス選択を変更しても、次回の起草フェーズから反映されます。重複検出に即時反映するには起草フェーズを再実行してください。",
  },
  "workspace.corpus.manage_globals": {
    en: "Manage globals (rebuild profile, delete documents)",
    zh: "管理全局语料（重建画像、删除文档）",
    ja: "全体コーパスを管理（プロファイル再構築・文書削除）",
  },
  "workspace.corpus.error.load": {
    en: "Failed to load corpus state.",
    zh: "加载语料状态失败。",
    ja: "コーパス状態の読み込みに失敗しました。",
  },
  "workspace.corpus.error.upload": {
    en: "Upload failed.",
    zh: "上传失败。",
    ja: "アップロードに失敗しました。",
  },
  "workspace.corpus.error.selection": {
    en: "Failed to update selection.",
    zh: "更新选择失败。",
    ja: "選択の更新に失敗しました。",
  },
  "workspace.corpus.saving": { en: "Saving…", zh: "保存中…", ja: "保存中…" },

  // phase action buttons
  "phase.proposal.start": {
    en: "Generate Initial Proposal",
    zh: "生成初始提案",
    ja: "初期提案を生成",
  },
  "phase.proposal.starting": {
    en: "Drafting Proposal...",
    zh: "正在生成提案…",
    ja: "提案を作成中…",
  },
  "phase.express.start": {
    en: "Start Express generation",
    zh: "启动 Express 生成",
    ja: "Express 生成を開始",
  },
  "phase.express.starting": {
    en: "Starting Express…",
    zh: "正在启动 Express…",
    ja: "Express 起動中…",
  },
  "phase.express.regenerate": {
    en: "Regenerate Express",
    zh: "重新生成 Express",
    ja: "Express を再生成",
  },
  "phase.proposal.accept": {
    en: "Accept Proposal and Run Scout",
    zh: "接受提案并启动文献检索",
    ja: "提案を承認して文献検索を実行",
  },
  "phase.scout.starting": {
    en: "Starting Scout...",
    zh: "正在启动文献检索…",
    ja: "文献検索を起動中…",
  },
  "phase.curator.start": {
    en: "Run Curator",
    zh: "启动文献整理",
    ja: "文献整理を実行",
  },
  "phase.curator.starting": {
    en: "Starting Curator...",
    zh: "正在启动文献整理…",
    ja: "文献整理を起動中…",
  },
  "phase.synthesizer.start": {
    en: "Run Synthesizer",
    zh: "启动综述",
    ja: "統合を実行",
  },
  "phase.synthesizer.starting": {
    en: "Starting Synthesizer...",
    zh: "正在生成综述…",
    ja: "統合を生成中…",
  },
  "phase.ideator.start": {
    en: "Run Ideator",
    zh: "启动新颖性提案",
    ja: "新規性提案を実行",
  },
  "phase.ideator.starting": {
    en: "Starting Ideator...",
    zh: "正在生成新颖性提案…",
    ja: "新規性提案を生成中…",
  },
  "phase.drafter.start": {
    en: "Run Drafter",
    zh: "启动撰稿",
    ja: "起草を実行",
  },
  "phase.drafter.starting": {
    en: "Starting Drafter...",
    zh: "正在撰稿…",
    ja: "起草中…",
  },
  "phase.drafter.needs_angle": {
    en: "Pick an angle card on the Novelty tab before running Drafter.",
    zh: "请先在「新颖性」标签页选定一张角度卡片，再启动撰稿。",
    ja: "起草を実行する前に「新規性」タブで角度カードを選択してください。",
  },
  "phase.stylist.start": {
    en: "Run Stylist",
    zh: "启动文风修订",
    ja: "文体調整を実行",
  },
  "phase.stylist.needs_drafter_done": {
    en: "Wait for Drafter to finish before running Stylist.",
    zh: "请等待撰稿完成后再启动文风修订。",
    ja: "起草が完了してから文体調整を実行してください。",
  },
  "phase.stylist.starting": {
    en: "Starting Stylist...",
    zh: "正在进行文风修订…",
    ja: "文体調整中…",
  },
  "phase.critic.start": {
    en: "Run Critic",
    zh: "启动评审",
    ja: "査読を実行",
  },
  "phase.critic.starting": {
    en: "Starting Critic...",
    zh: "正在启动评审…",
    ja: "査読を起動中…",
  },
  "nav.logout": { en: "Logout", zh: "退出登录", ja: "ログアウト" },
  "nav.menu_open": {
    en: "Open navigation menu",
    zh: "打开导航菜单",
    ja: "メニューを開く",
  },
  "header.ui_language": { en: "UI language", zh: "界面语言", ja: "UI 言語" },

  // corpus page
  "corpus.section_label": { en: "Corpus", zh: "语料库", ja: "コーパス" },
  "corpus.heading": { en: "Prior papers", zh: "过往论文", ja: "過去の論文" },
  "corpus.rebuild_profile": {
    en: "Rebuild style profile",
    zh: "重建文风画像",
    ja: "文体プロファイルを再構築",
  },
  "corpus.upload_prior": {
    en: "Upload prior paper",
    zh: "上传过往论文",
    ja: "過去の論文をアップロード",
  },
  "corpus.privacy_notice": {
    en: "Your prior papers stay local on this server. They are never sent to plagiarism / AI-detection vendors per design §6.4.",
    zh: "过往论文仅留存在本服务器。按设计 §6.4，绝不会发送给抄袭检测或 AI 检测服务商。",
    ja: "過去の論文は本サーバー内にのみ保存され、設計 §6.4 に従い剽窃／AI 検出ベンダーへ送信されません。",
  },
  "corpus.uploaded_papers": {
    en: "Uploaded papers",
    zh: "已上传论文",
    ja: "アップロード済み論文",
  },
  "corpus.cta_start_project": {
    en: "Start a new project →",
    zh: "开始新项目 →",
    ja: "新しいプロジェクトを開始 →",
  },
  "corpus.cta_help": {
    en: "Once your prior papers are profiled, the next step is to create a project and start a new essay.",
    zh: "过往论文已建立画像后，下一步就是新建项目并开始一篇新论文。",
    ja: "過去の論文のプロファイル作成後、次のステップは新しいプロジェクトを作成して新しい論文を始めることです。",
  },
  "corpus.profiled": { en: "profiled", zh: "已建画像", ja: "プロファイル済" },
  "corpus.type": { en: "Type", zh: "类型", ja: "種類" },
  "corpus.size": { en: "Size", zh: "大小", ja: "サイズ" },
  "corpus.uploaded": { en: "Uploaded", zh: "上传时间", ja: "アップロード日" },
  "corpus.delete": { en: "Delete", zh: "删除", ja: "削除" },
  // Style profile preview labels — moved out of hardcoded English
  // strings inside CorpusPage as part of the PR-B2 diagnostics
  // surfacing.
  "corpus.profile_heading": {
    en: "Style profile preview",
    zh: "风格画像预览",
    ja: "文体プロファイルのプレビュー",
  },
  "corpus.profile.empty": {
    en: "No style profile has been built yet.",
    zh: "尚未生成风格画像。",
    ja: "文体プロファイルはまだ作成されていません。",
  },
  "corpus.profile.paragraph_length": {
    en: "Paragraph length",
    zh: "段落长度",
    ja: "段落の長さ",
  },
  "corpus.profile.sentence_length": {
    en: "Sentence length",
    zh: "句子长度",
    ja: "文の長さ",
  },
  "corpus.profile.openers": { en: "Openers", zh: "段落开头", ja: "段落の冒頭" },
  "corpus.profile.hedges": { en: "Hedges", zh: "婉转用语", ja: "ヘッジ表現" },
  "corpus.profile.common_terms": {
    en: "Common terms",
    zh: "高频术语",
    ja: "頻出用語",
  },
  // PR-B2 diagnostic fields. Surface the language the backend
  // detected, the raw counts, and any warnings that explain why
  // a particular section is empty — directly answers the "是不是
  // 假的？" trust question.
  "corpus.profile.detected_language": {
    en: "Detected language",
    zh: "检测到的语言",
    ja: "検出された言語",
  },
  "corpus.profile.document_count": {
    en: "Documents",
    zh: "文档数",
    ja: "ドキュメント数",
  },
  "corpus.profile.total_token_count": {
    en: "Total tokens",
    zh: "总 token 数",
    ja: "総トークン数",
  },
  "corpus.profile.warnings": {
    en: "Warnings",
    zh: "警告",
    ja: "警告",
  },
  "corpus.profile.language.en": { en: "English", zh: "英文", ja: "英語" },
  "corpus.profile.language.zh": { en: "Chinese", zh: "中文", ja: "中国語" },
  "corpus.profile.language.ja": { en: "Japanese", zh: "日文", ja: "日本語" },
  "corpus.profile.language.unknown": {
    en: "Unknown",
    zh: "未知",
    ja: "不明",
  },

  // new essay page (i18n keys retain `newrun.*` for stability — values use "essay")
  "newrun.section_label": { en: "New essay", zh: "新建论文", ja: "新規論文" },
  "newrun.heading": {
    en: "Create an empty project",
    zh: "创建空项目",
    ja: "新しいプロジェクトを作成",
  },
  "newrun.domain": { en: "Domain", zh: "领域", ja: "分野" },
  "newrun.title": { en: "Project title", zh: "项目标题", ja: "プロジェクト名" },
  "newrun.target_journal": {
    en: "Target journal",
    zh: "目标期刊",
    ja: "投稿先ジャーナル",
  },
  "newrun.optional": { en: "Optional", zh: "可选", ja: "任意" },
  "newrun.paper_language": {
    en: "Paper language",
    zh: "论文语言",
    ja: "論文の言語",
  },
  "newrun.paper_language_hint": {
    en: "The language used for the paper itself. Defaults to your interface language but can be different, such as writing a Japanese paper from the English interface.",
    zh: "论文正文使用的语言。默认与你的界面语言一致，也可以不同，例如用英文界面写中文论文。",
    ja: "論文本体で使う言語です。通常は UI 言語と同じですが、英語の画面で日本語の論文を書くこともできます。",
  },
  "newrun.generation_mode.label": {
    en: "Generation mode",
    zh: "生成模式",
    ja: "生成モード",
  },
  "newrun.generation_mode.hint": {
    en: "Choose the manuscript generation architecture. This is separate from paper mode, which controls article shape.",
    zh: "选择生成架构。它独立于论文模式；论文模式决定文章形态。",
    ja: "原稿生成の構成を選びます。論文モードは論文の形を決める別項目です。",
  },
  "newrun.generation_mode.express.label": {
    en: "Express",
    zh: "Express",
    ja: "Express",
  },
  "newrun.generation_mode.express.detail": {
    en: "Fast and cheaper, about 30K tokens. No phase preview; audit-only critic.",
    zh: "更快、更便宜，约 30K token。没有阶段预览；仅运行 audit-only critic。",
    ja: "高速で低コスト、約 30K tokens。フェーズプレビューなし、監査専用 critic。",
  },
  "newrun.generation_mode.deep.label": {
    en: "Deep",
    zh: "Deep",
    ja: "Deep",
  },
  "newrun.generation_mode.deep.detail": {
    en: "13-phase workflow with phase preview, paired_runner critic, and review gates.",
    zh: "13 阶段流程，包含阶段预览、paired_runner critic 和 review gates。",
    ja: "13 フェーズ構成。フェーズプレビュー、paired_runner critic、確認ゲートあり。",
  },
  "newrun.create_and_open": {
    en: "Create project and open workspace",
    zh: "创建项目并进入工作台",
    ja: "プロジェクトを作成して作業画面を開く",
  },
  "newrun.mathematical_mode.label": {
    en: "Mathematical-strength mode",
    zh: "数理增强模式",
    ja: "数理強化モード",
  },
  "newrun.mathematical_mode.tooltip": {
    en: "Run a heavyweight holistic rewrite for round-0 — can introduce LaTeX formulas, tables, and 【TODO】 placeholders. Adds ~20-30 min and ~10x token cost.",
    zh: "做高强度整体战略改稿，可建议 LaTeX 公式、表格、待填占位；预计 +20-30 分钟，token 成本约 10x。",
    ja: "大規模な総合書き換えを行い、LaTeX 数式・表・【未記入】を補強。所要時間 +20-30 分、トークン費用は約 10 倍。",
  },
  "newrun.auto_advance.label": {
    en: "One-click auto-pilot",
    zh: "一键全自动",
    ja: "ワンクリック自動運転",
  },
  "newrun.auto_advance.tooltip": {
    en: "Skip every USER_*_REVIEW gate automatically and run end-to-end without intervention. Failures (FAILED_*) still pause for user decision. Combine with mathematical mode for the fullest auto path.",
    zh: "所有 USER_*_REVIEW 检查点自动通过，全流程一键跑完不用守在屏前。失败状态（FAILED_*）仍会停下来等用户决定。可与数理增强模式叠加。",
    ja: "USER_*_REVIEW チェックポイントを自動承認し、最後まで放置で完走。失敗（FAILED_*）はユーザー判断のため停止。数理強化モードと併用可。",
  },
  "newrun.auto_advance.deep_only": {
    en: "Only Deep mode has review gates to auto-advance. Express runs without phase review gates.",
    zh: "只有 Deep 模式有可自动通过的 review gates。Express 不走阶段审阅点。",
    ja: "自動通過する確認ゲートがあるのは Deep のみです。Express はフェーズ確認ゲートを使いません。",
  },
  "auto_pilot.badge": {
    en: "Auto-pilot",
    zh: "全自动",
    ja: "自動運転",
  },
  "auto_pilot.tooltip": {
    en: "Auto-pilot is on. The run will advance through every review gate without manual clicks; failures still pause.",
    zh: "全自动模式开启。该 run 会自动通过所有 review 检查点，失败状态仍会暂停等待。",
    ja: "自動運転モード ON。すべての審査ゲートを自動通過し、失敗時のみ停止します。",
  },
  "runs.bulk_select_all": {
    en: "Select all",
    zh: "全选",
    ja: "全選択",
  },
  "runs.bulk_clear_selection": {
    en: "Clear selection",
    zh: "取消选择",
    ja: "選択解除",
  },
  "runs.bulk_selected_count": {
    en: "{n} selected",
    zh: "已选 {n} 项",
    ja: "{n} 件選択中",
  },
  "runs.bulk_hard_delete_submit": {
    en: "Permanently delete",
    zh: "永久删除",
    ja: "完全削除",
  },
  "runs.bulk_hard_delete_in_flight": {
    en: "Deleting…",
    zh: "删除中…",
    ja: "削除中…",
  },
  "runs.hard_delete_confirm": {
    en: "Permanently delete {n} items? This cannot be undone.",
    zh: "永久删除 {n} 项？此操作无法撤销。",
    ja: "{n} 件を完全削除しますか？取り消しはできません。",
  },
  "runs.hard_delete_failed": {
    en: "Permanent delete failed",
    zh: "永久删除失败",
    ja: "完全削除に失敗しました",
  },
  "lang.en_label": {
    en: "English",
    zh: "英文 (English)",
    ja: "英語 (English)",
  },
  "lang.zh_label": {
    en: "Chinese (中文)",
    zh: "中文 (Chinese)",
    ja: "中国語 (中文)",
  },
  "lang.ja_label": {
    en: "Japanese (日本語)",
    zh: "日文 (日本語)",
    ja: "日本語 (Japanese)",
  },
  "newrun.create_button": {
    en: "Create project",
    zh: "创建项目",
    ja: "プロジェクトを作成",
  },
  "newrun.creating_button": { en: "Creating...", zh: "创建中…", ja: "作成中…" },
  "newrun.created": {
    en: "Created project",
    zh: "已创建项目",
    ja: "プロジェクトを作成しました",
  },
  "newrun.essay_limit_reached": {
    en: "You already have 3 active essays. Finish or delete one before starting a new essay.",
    zh: "你已有 3 篇进行中的论文。请先完成或删除其中一篇再创建新论文。",
    ja: "進行中の論文が 3 本に達しています。新規作成の前に既存のものを完了または削除してください。",
  },

  // Settings / authors
  "nav.settings": { en: "Settings", zh: "设置", ja: "設定" },
  "settings.heading": { en: "Settings", zh: "设置", ja: "設定" },
  "settings.authors_section": {
    en: "Authors",
    zh: "作者",
    ja: "著者",
  },
  "settings.authors_hint": {
    en: "Authors you maintain here can be picked when creating or editing an essay. Deleting an author keeps existing essays intact.",
    zh: "这里维护的作者可以在创建或编辑论文时选用。删除作者后，已使用该作者的论文不受影响。",
    ja: "ここで管理する著者は論文の作成・編集時に選択できます。著者を削除しても既存の論文に影響はありません。",
  },
  "authors.add_button": {
    en: "Add author",
    zh: "添加作者",
    ja: "著者を追加",
  },
  "authors.display_name": {
    en: "Display name",
    zh: "显示名",
    ja: "表示名",
  },
  "authors.affiliation": {
    en: "Affiliation",
    zh: "单位",
    ja: "所属",
  },
  "authors.email": { en: "Email", zh: "邮箱", ja: "メール" },
  "authors.orcid": { en: "ORCID", zh: "ORCID", ja: "ORCID" },
  "authors.self_label": {
    en: "Yourself",
    zh: "本人",
    ja: "本人",
  },
  "authors.delete_button": { en: "Delete", zh: "删除", ja: "削除" },
  "authors.save_button": { en: "Save", zh: "保存", ja: "保存" },
  "authors.cancel_button": {
    en: "Cancel",
    zh: "取消",
    ja: "キャンセル",
  },
  "authors.delete_confirm": {
    en: "Delete this author? Existing essays that already include them will be unchanged.",
    zh: "删除该作者？已选用此作者的论文不受影响。",
    ja: "この著者を削除しますか？既にこの著者を使用している論文には影響しません。",
  },
  "authors.empty_hint": {
    en: "No authors yet besides yourself. Add co-authors here.",
    zh: "除你之外暂无作者。可在此添加合作作者。",
    ja: "あなた以外の著者はまだいません。共著者をここで追加できます。",
  },
  "authors.deleted_badge": {
    en: "deleted",
    zh: "已删除",
    ja: "削除済み",
  },
  "authors.cannot_delete_self": {
    en: "The self-author cannot be deleted; edit it instead.",
    zh: "本人作者不能删除，请直接编辑。",
    ja: "本人著者は削除できません。編集してください。",
  },

  // ---------------------------------------------------------------
  // Help page (codex-AGREEd #7) — plain-language only.
  // Keep Help page copy plain and user-facing.
  // anywhere in the user-visible strings. Static check enforces this.
  // ---------------------------------------------------------------
  "help.heading": {
    en: "Help",
    zh: "帮助中心",
    ja: "ヘルプ",
  },
  "help.subtitle": {
    en: "Start a paper, choose auto-pilot, review gates, fix failures, and download the final files.",
    zh: "新建论文、选择全自动、审阅步骤、处理失败、下载文件，都在这里看。",
    ja: "論文の開始、自動運転の選択、確認ゲート、失敗時の対応、最終ファイルの取得を説明します。",
  },

  // 1. Getting started
  "help.getting-started.title": {
    en: "Getting started",
    zh: "开始使用",
    ja: "はじめに",
  },
  "help.getting-started.body": {
    en: "Sign in with the account from your workspace administrator, or ask them to create one for you. After sign-in, the runs list is your dashboard. It shows every paper you can open, continue, delete, restore, or inspect.\n\nEach card shows the current status, last update, and an auto-pilot badge when that mode is on. Use New Run to start a paper. Use an existing card to return to the workspace for that paper.",
    zh: "使用工作区管理员提供的账号登录；如果还没有账号，请先请管理员开通。登录后看到的是论文列表，也就是你的 dashboard。这里能打开、继续、删除、恢复或查看每篇论文。\n\n每张卡片都会显示当前状态、最近更新时间；如果已开启全自动，也会显示对应徽章。点 New Run 新建论文；点已有卡片回到那篇论文的工作台。",
    ja: "ワークスペース管理者から渡されたアカウントでサインインします。まだアカウントがない場合は、管理者に作成を依頼してください。サインイン後に表示される論文一覧が dashboard です。ここから論文を開く、続行する、削除する、復元する、状態を確認することができます。\n\n各カードには現在の状態、最終更新時刻、自動運転が有効な場合のバッジが表示されます。New Run から新しい論文を始め、既存カードから作業画面に戻ります。",
  },
  "help.getting-started.callout": {
    en: "If you cannot see the dashboard or New Run button, ask your workspace administrator to check your access.",
    zh: "如果看不到 dashboard 或 New Run，请联系工作区管理员检查权限。",
    ja: "dashboard や New Run が見えない場合は、ワークスペース管理者に権限を確認してもらってください。",
  },
  "help.getting-started.screenshot_alt": {
    en: "Desktop dashboard showing paper cards, statuses, and auto-pilot badges",
    zh: "桌面端 dashboard，显示论文卡片、状态和全自动徽章",
    ja: "論文カード、状態、自動運転バッジを表示したデスクトップ dashboard",
  },

  // 2. Creating an essay
  "help.creating-essay.title": {
    en: "Creating a paper",
    zh: "创建论文项目",
    ja: "論文プロジェクトを作成する",
  },
  "help.creating-essay.body": {
    en: "On New Run, enter a clear title or research question, choose the closest domain, select a paper mode, and choose the manuscript language. The interface language and manuscript language are separate.\n\nTurn on One-click auto-pilot when you want Appleseed to accept review gates for you. Turn on Mathematical-strength mode when the paper needs LaTeX formulas, tables, or explicit `【TODO】` placeholders. Mathematical-strength mode can add about 20-30 minutes and about 10x token use. You can use both modes together.",
    zh: "在新建页面，填写清楚的题目或研究问题，选择最接近的领域、论文模式和论文语言。界面语言和论文语言是两件事：你可以用中文界面写英文论文，也可以反过来。\n\n想让 Appleseed 自动确认各个审阅点，就勾选一键全自动。论文需要 LaTeX 公式、表格或明确的 `【待填】` 占位时，再勾选数理增强模式。数理增强通常会多 20-30 分钟，token 用量约 10 倍。两项可以一起使用。",
    ja: "New Run では、明確な題名または研究質問を入力し、近い研究分野、論文モード、原稿の言語を選びます。画面の言語と原稿の言語は別々です。中国語の画面で英語論文を書くことも、その逆もできます。\n\n確認ゲートを Appleseed に任せたい場合は、ワンクリック自動運転を有効にします。LaTeX 数式、表、明示的な `【TODO】` が必要な論文では、数理強化モードを有効にします。数理強化モードは通常より 20-30 分ほど長くなり、トークン使用量は約 10 倍です。2 つのモードは併用できます。",
  },
  "help.creating-essay.callout": {
    en: "If you want to choose sources yourself, leave auto-pilot off until after the reading list is approved.",
    zh: "如果你想亲自选文献，可以先不开全自动；确认阅读清单后再打开也可以。",
    ja: "資料を自分で選びたい場合は、読書リストを承認するまで自動運転をオフにしておけます。",
  },
  "help.creating-essay.screenshot_alt": {
    en: "Desktop New Run form with auto-pilot and mathematical-strength options",
    zh: "桌面端新建论文表单，包含全自动和数理增强选项",
    ja: "自動運転と数理強化の選択肢があるデスクトップ版 New Run フォーム",
  },

  // 3. Review steps
  "help.review-steps.title": {
    en: "Review gates",
    zh: "审阅点",
    ja: "確認ゲート",
  },
  "help.review-steps.body": {
    en: "A manual run can ask you to review ten gates: proposal, found sources, reading list, material diagnosis, theory or tension notes when shown, angle choice, draft and style, final review, integrity findings, and final acceptance before export.\n\nAt each gate, check three things: the paper still matches your question, the sources look useful, and the writing stays within the evidence. With auto-pilot on, Appleseed accepts these gates automatically. A `FAILED_*` state still pauses for your decision.",
    zh: "手动运行时，通常会看到十个审阅点：提案、检索候选、阅读清单、资料诊断、理论或张力提示（出现时）、角度选择、草稿与文风、最终评审、诚信检测结果、导出前最终确认。\n\n每次审阅重点看三件事：方向是否符合你的问题，文献是否有用，文字是否超出证据。开启全自动后，Appleseed 会自动确认这些审阅点。进入 `FAILED_*` 状态时仍会停下来等你决定。",
    ja: "手動で進める場合、確認ゲートは主に 10 個あります。提案、検索された資料、読書リスト、資料診断、表示される場合の理論メモまたは論点メモ、角度選択、草稿と文体、最終レビュー、整合性結果、エクスポート前の最終承認です。\n\n各ゲートでは、研究質問から外れていないか、資料が使えるか、本文が証拠を超えていないかを確認します。自動運転が有効な場合、Appleseed がこれらのゲートを自動で承認します。`FAILED_*` の状態では、利用者の判断を待ちます。",
  },
  "help.review-steps.callout": {
    en: "Auto-pilot is best when the topic and source limits are already clear. For exploratory topics, review the source gates yourself.",
    zh: "题目和文献范围已经很清楚时，适合打开全自动。探索型题目建议亲自看文献审阅点。",
    ja: "テーマと資料範囲がはっきりしている場合は自動運転が向いています。探索型のテーマでは資料ゲートを自分で確認してください。",
  },
  "help.review-steps.screenshot_alt": {
    en: "Desktop proposal review gate",
    zh: "桌面端提案审阅点",
    ja: "デスクトップ版の提案確認ゲート",
  },

  // 4. Workspace states
  "help.workspace-states.title": {
    en: "Workspace states",
    zh: "工作台状态",
    ja: "ワークスペースの状態",
  },
  "help.workspace-states.body": {
    en: "Use the workspace header and the runs list to see what is happening. Working means the current step is still active. Waiting for review means Appleseed needs a decision. Needs fix means a banner will explain what to repair or retry. Done means exports are ready. Deleted means the item is hidden unless you show deleted runs.\n\nThe auto-pilot badge tells you the run is accepting review gates for you. If no badge appears, expect to review each gate manually.",
    zh: "看工作台顶部和论文列表，就能知道当前进展。正在运行表示当前步骤还在处理；等待审阅表示需要你做决定；需修复表示页面会说明要补什么或重试什么；完成表示导出文件已准备好；已删除表示默认隐藏，勾选显示已删除后才能看到。\n\n全自动徽章表示这次运行会自动确认审阅点。没有这个徽章时，就按手动方式逐步审阅。",
    ja: "作業画面の上部と論文一覧で、現在の状況を確認できます。実行中は現在のステップが進んでいます。確認待ちは判断が必要です。要対応の場合は、修正または再試行する内容がバナーに表示されます。完了は出力ファイルが準備済みです。削除済みは、削除済み表示を有効にしたときだけ見えます。\n\n自動運転バッジがある実行は、確認ゲートを自動で承認します。バッジがない場合は、各ゲートを手動で確認します。",
  },
  "help.workspace-states.callout": {
    en: "Refreshing the browser is safe. It does not remove saved paper work.",
    zh: "刷新浏览器是安全的，不会删除已保存的论文内容。",
    ja: "ブラウザの再読み込みは安全です。保存済みの論文内容は消えません。",
  },
  "help.workspace-states.screenshot_alt": {
    en: "Desktop workspace header with current status",
    zh: "桌面端工作台顶部，显示当前状态",
    ja: "現在の状態を表示したデスクトップ版作業画面の上部",
  },

  // 5. Mid-flight edits
  "help.mid-flight-edits.title": {
    en: "Making changes mid-run",
    zh: "中途修改",
    ja: "途中で変更する",
  },
  "help.mid-flight-edits.body": {
    en: "You can still change direction after a paper has started. Use the workspace controls to edit the research kernel, topic notes, authors, target journal, manuscript language, or available draft text.\n\nSmall wording edits can stay local. A changed research question should usually update the research kernel and then generate the affected steps again. When you change earlier inputs, later tabs may show that their old output needs review before you continue.",
    zh: "论文开始后仍然可以调整方向。可在工作台里修改研究核心信息、题目说明、作者、目标期刊、论文语言，或可编辑的草稿内容。\n\n小的措辞修改可以直接改。研究问题变了，通常应先更新研究核心信息，再重新生成受影响的后续步骤。修改较早输入后，后面的页面可能会提示旧结果需要重新审阅。",
    ja: "論文作成が始まった後でも方向を変えられます。作業画面で、研究コア、テーマメモ、著者、投稿先、原稿の言語、編集可能な草稿を変更できます。\n\n小さな表現修正はその場で直せます。研究質問が変わった場合は、研究コアを更新し、影響を受ける後続ステップをもう一度生成するのが基本です。早い段階の入力を変えると、後続タブで古い出力の再確認を求められることがあります。",
  },
  "help.mid-flight-edits.callout": {
    en: "Turn auto-pilot off before a large direction change if you want to inspect the next gate yourself.",
    zh: "如果要大改方向，并且想亲自看下一步结果，先把全自动关掉。",
    ja: "大きく方向を変え、次の結果を自分で確認したい場合は、先に自動運転をオフにしてください。",
  },
  "help.mid-flight-edits.screenshot_alt": {
    en: "Desktop workspace with controls for mid-run edits",
    zh: "桌面端工作台，显示中途修改入口",
    ja: "途中編集の操作があるデスクトップ版作業画面",
  },

  // 6. Final review
  "help.final-review.title": {
    en: "Final review",
    zh: "最终审阅",
    ja: "最終レビュー",
  },
  "help.final-review.body": {
    en: "Near the end, read the full manuscript, not just the last message. Check whether the evidence supports the claims, whether citations are present where needed, and whether any section still looks unfinished.\n\nThe final review and integrity screens may ask you to revise, retry, or accept. Accept only when you are ready to create export files. Acceptance does not mean the paper is ready to submit without your own review.",
    zh: "接近结束时，请读完整稿件，不要只看最后一条提示。重点检查证据是否支撑论点、该有引用的地方是否有引用、是否还有未完成的章节。\n\n最终评审和诚信检测页面可能会要求你修改、重试或接受。点击接受只表示你准备生成导出文件，不表示论文已经可以不经人工审阅直接投稿。",
    ja: "終盤では、最後の通知だけでなく原稿全体を読んでください。主張を証拠が支えているか、必要な場所に引用があるか、未完成に見える章が残っていないかを確認します。\n\n最終レビューと整合性チェックでは、修正、再試行、承認を求められることがあります。承認は出力ファイルを作ってよいという判断です。自分で確認せずに投稿してよい、という意味ではありません。",
  },
  "help.final-review.callout": {
    en: "Before accepting, search for `TODO`, `【TODO】`, `【待填】`, and citation placeholders.",
    zh: "最终接受前，建议搜索 `TODO`、`【TODO】`、`【待填】` 和占位引用。",
    ja: "承認前に `TODO`、`【TODO】`、`【待填】`、仮の引用表示を検索してください。",
  },
  "help.final-review.screenshot_alt": {
    en: "Desktop final review with integrity findings",
    zh: "桌面端最终评审与诚信检测结果",
    ja: "整合性結果を表示したデスクトップ版最終レビュー",
  },

  // 7. Exports & downloads
  "help.exports-downloads.title": {
    en: "Exports and downloads",
    zh: "导出与下载",
    ja: "エクスポートとダウンロード",
  },
  "help.exports-downloads.body": {
    en: "When export is complete, the Export tab lists nine files: `manuscript.md`, `manuscript.docx`, `manuscript.html`, `manuscript.tex`, `citations.bib`, `citations.csl.json`, `manifest.json`, `literature_usage_table.md`, and `self_check_report.md`.\n\nManuscript and citation downloads use the project title as a file slug, such as `真题评测-2026-05-13-晚清江南刊本断代依据.docx`. If the title is empty or still the default, the fallback is `manuscript-{first 8 chars of run id}`. Download the manuscript and citations together so later edits remain traceable.",
    zh: "导出完成后，导出页会列出九个文件：`manuscript.md`、`manuscript.docx`、`manuscript.html`、`manuscript.tex`、`citations.bib`、`citations.csl.json`、`manifest.json`、`literature_usage_table.md`、`self_check_report.md`。\n\n正文和引用文件会按项目题目生成下载名，例如 `真题评测-2026-05-13-晚清江南刊本断代依据.docx`。如果题目为空或仍是默认题目，会使用 `manuscript-{runid前8位}`。建议把正文和引用文件一起保存，方便以后追溯。",
    ja: "エクスポートが完了すると、Export タブに 9 個のファイルが表示されます。`manuscript.md`、`manuscript.docx`、`manuscript.html`、`manuscript.tex`、`citations.bib`、`citations.csl.json`、`manifest.json`、`literature_usage_table.md`、`self_check_report.md` です。\n\n原稿と引用ファイルは、プロジェクト題名から作ったファイル名でダウンロードされます。例: `真题评测-2026-05-13-晚清江南刊本断代依据.docx`。題名が空、または初期値のままの場合は `manuscript-{run id 先頭 8 文字}` になります。後で追跡しやすいように、原稿と引用ファイルは一緒に保存してください。",
  },
  "help.exports-downloads.callout": {
    en: "If a download button does not respond, refresh once. If a file is still missing, run export again.",
    zh: "如果下载按钮没有反应，先刷新一次；文件仍缺失时，再重新导出。",
    ja: "ダウンロードボタンが反応しない場合は、一度再読み込みしてください。まだ見つからない場合は、エクスポートをもう一度実行します。",
  },
  "help.exports-downloads.screenshot_alt": {
    en: "Desktop export screen listing manuscript, citation, and sidecar files",
    zh: "桌面端导出页面，列出正文、引用和辅助文件",
    ja: "原稿、引用、補助ファイルを表示したデスクトップ版エクスポート画面",
  },

  // 8. Managing essays
  "help.managing-essays.title": {
    en: "Managing papers",
    zh: "管理论文",
    ja: "論文の管理",
  },
  "help.managing-essays.body": {
    en: "Use the runs list to open recent work, create another run, delete a run you no longer need, or restore one that was deleted. Deleting a run affects that run only; it does not remove every run under the same paper.\n\nTo permanently delete old items, turn on Show deleted, select the deleted cards, and use the red Permanently delete button. This is a two-step protection: an item must be deleted first before it can be permanently deleted.",
    zh: "在论文列表里，你可以打开最近的工作、新建运行、删除不再需要的运行，或恢复已删除运行。删除某次运行只影响这一次，不会把同一论文下的其他运行一起删掉。\n\n需要永久删除旧内容时，先勾选显示已删除，再选择已删除卡片，最后点红色的永久删除按钮。这是两步保护：必须先进入已删除列表，才能永久删除。",
    ja: "論文一覧では、最近の作業を開く、新しい実行を作る、不要な実行を削除する、削除済みの実行を復元する、といった操作ができます。1 回の実行を削除しても、同じ論文の他の実行は削除されません。\n\n古い項目を完全に削除するには、削除済みを表示し、削除済みカードを選択して、赤い完全削除ボタンを使います。これは 2 段階の保護です。先に削除済みにした項目だけ、完全削除できます。",
  },
  "help.managing-essays.callout": {
    en: "Permanent deletion cannot be undone. Restore the run first if you are not sure.",
    zh: "永久删除无法撤销。不确定时，先恢复运行再看清楚。",
    ja: "完全削除は取り消せません。迷う場合は、先に実行を復元して内容を確認してください。",
  },
  "help.managing-essays.screenshot_alt": {
    en: "Runs list with deleted items and bulk delete controls",
    zh: "论文列表，显示已删除项和批量删除控件",
    ja: "削除済み項目と一括削除操作を表示した論文一覧",
  },

  // 9. Authors
  "help.authors.title": { en: "Authors", zh: "作者信息", ja: "著者情報" },
  "help.authors.body": {
    en: "Open Settings, then Authors, to keep your author list. Add your display name, affiliation, email, and ORCID if available. Add frequent coauthors so you can select them quickly when creating a paper.\n\nAuthor order appears in the manuscript front matter and exported files. Review names, affiliations, and order before final export, especially for multi-author work.",
    zh: "打开设置里的作者页面，维护作者名单。填写自己的显示名、单位、邮箱和 ORCID（如有）。常用合作者也可以提前加入，新建论文时直接选择。\n\n作者顺序会出现在稿件首页和导出文件中。多作者论文在最终导出前，请再确认姓名、单位和顺序。",
    ja: "Settings の Authors で著者リストを管理します。自分の表示名、所属、メール、ORCID があれば入力してください。よく使う共著者も登録しておくと、論文作成時にすぐ選べます。\n\n著者順は原稿の冒頭情報と出力ファイルに反映されます。複数著者の論文では、最終エクスポート前に名前、所属、順番を確認してください。",
  },
  "help.authors.callout": {
    en: "Do not leave placeholder authors in work you plan to submit.",
    zh: "准备投稿的稿件，不要保留占位作者。",
    ja: "投稿予定の原稿には仮の著者を残さないでください。",
  },
  "help.authors.screenshot_alt": {
    en: "Author roster settings",
    zh: "作者名单设置",
    ja: "著者リスト設定",
  },

  // 10. Troubleshooting
  "help.troubleshooting.title": {
    en: "Troubleshooting",
    zh: "常见问题",
    ja: "トラブル対応",
  },
  "help.troubleshooting.body": {
    en: "`FAILED_FIXABLE` means Appleseed stopped on something you can usually repair: missing files, too few usable sources, a validation issue, or a step that needs another try. Read the banner, fix what it asks for, then use retry when the button appears.\n\nRetry exists because downloads, outside services, and long writing steps can fail once and succeed the next time. If a production deployment interrupts an auto-pilot run, Appleseed usually continues it within a few minutes. If the same error repeats, copy the paper title, current status, and failure time before contacting your administrator.",
    zh: "`FAILED_FIXABLE` 表示 Appleseed 遇到了通常可以修复的问题：缺文件、可用文献太少、校验没过，或某个步骤需要再试一次。先读页面提示，按要求补材料或修改内容；出现重试按钮时再点重试。\n\n需要重试，是因为下载、外部服务和长时间写作步骤偶尔会第一次失败、第二次成功。如果生产部署中断了全自动运行，Appleseed 通常会在几分钟内继续处理。同一错误反复出现时，请记录论文题目、当前状态和失败时间，再联系管理员。",
    ja: "`FAILED_FIXABLE` は、通常は利用者側で直せる問題で止まったことを示します。ファイル不足、使える資料が少ない、検証で止まった、またはもう一度試すべきステップなどです。バナーを読み、求められた修正を行い、再試行ボタンが表示されたら使ってください。\n\n再試行が必要なのは、ダウンロード、外部サービス、長い執筆ステップが一度失敗しても次に成功することがあるためです。本番デプロイで自動運転の実行が中断された場合、多くの場合は数分以内に続きから進みます。同じエラーが続く場合は、論文タイトル、現在の状態、失敗時刻を控えて管理者に連絡してください。",
  },
  "help.troubleshooting.callout": {
    en: "Do not start a duplicate paper just because one step is slow. Refresh once and check the current status first.",
    zh: "不要因为某一步慢就马上新建一篇。先刷新一次，看清当前状态。",
    ja: "1 つのステップが遅いだけで、同じ論文を作り直さないでください。まず一度再読み込みし、現在の状態を確認します。",
  },
  "help.troubleshooting.screenshot_alt": {
    en: "Troubleshooting guidance for a paper run",
    zh: "论文运行的故障处理说明",
    ja: "論文実行時のトラブル対応案内",
  },

  // 11. Privacy & data
  "help.privacy-data.title": {
    en: "Privacy and data",
    zh: "隐私与数据",
    ja: "プライバシーとデータ",
  },
  "help.privacy-data.body": {
    en: "Appleseed stores what it needs to work on your paper: account details, project settings, author records, source results, uploaded PDFs, drafts, review decisions, and export files. These records let you return to a paper, review what happened, and download past outputs.\n\nOnly upload material you are allowed to use. You can see your own workspace items. Workspace administrators and deployment maintainers may access stored data when they need to support accounts, fix failures, handle retention, or process deletion requests.",
    zh: "Appleseed 会保存写论文所需的信息：账号资料、项目设置、作者记录、文献检索结果、已上传 PDF、草稿、审阅决定和导出文件。这些记录用于回到论文、查看过程和下载历史成果。\n\n请只上传你有权使用的材料。你可以查看自己工作区里的内容。工作区管理员和部署维护人员在处理账号、故障、保留策略或删除请求时，可能需要访问已保存数据。",
    ja: "Appleseed は、論文作成に必要な情報を保存します。アカウント情報、プロジェクト設定、著者情報、資料検索結果、アップロードした PDF、草稿、確認結果、出力ファイルなどです。これにより、論文に戻る、経過を確認する、過去の出力をダウンロードすることができます。\n\n利用してよい資料だけをアップロードしてください。自分のワークスペース項目は自分で確認できます。ワークスペース管理者とデプロイ担当者は、アカウント対応、失敗時の調査、保存期間、削除依頼のために保存データへアクセスする場合があります。",
  },
  "help.privacy-data.callout": {
    en: "Deleted in the list does not always mean all stored data is gone. Use permanent deletion where available, or ask your administrator.",
    zh: "列表里显示已删除，不一定表示所有数据都已清除。可用永久删除时请使用；不确定就联系管理员。",
    ja: "一覧で削除済みと表示されても、保存データがすべて消えたとは限りません。利用できる場合は完全削除を使い、不明な場合は管理者に確認してください。",
  },
  "help.privacy-data.screenshot_alt": {
    en: "Privacy and data guidance for stored paper records",
    zh: "关于论文数据保存与隐私的说明",
    ja: "保存される論文データとプライバシーに関する案内",
  },

  // Rerun (codex-AGREEd #2 stage 1)
  "workspace.rerun_phase": {
    en: "Rerun {phase}",
    zh: "重新运行 {phase}",
    ja: "{phase} を再実行",
  },
  "workspace.rerun_running": {
    en: "Rerunning…",
    zh: "重新运行中…",
    ja: "再実行中…",
  },
  "workspace.rerun_failed": {
    en: "Rerun failed",
    zh: "重新运行失败",
    ja: "再実行に失敗しました",
  },
  "workspace.source_rerun_confirm.title": {
    en: "Rerun {phase}?",
    zh: "重跑「{phase}」？",
    ja: "「{phase}」を再実行しますか？",
  },
  "workspace.source_rerun_confirm.body": {
    en: "This will clear generated source artifacts for this step and downstream phases.\nScout candidates: {skim}\nShortlist items: {shortlist}\nManual upload requests: {manual}\nGenerated downstream phases affected: {downstream}",
    zh: "这会清空该步骤及下游阶段生成的文献产物。\nScout 候选：{skim}\n入选文献：{shortlist}\n手动上传请求：{manual}\n受影响的已生成下游阶段：{downstream}",
    ja: "このステップと下流ステージで生成された文献成果物をクリアします。\nScout 候補: {skim}\nショートリスト項目: {shortlist}\n手動アップロード依頼: {manual}\n影響を受ける生成済み下流ステージ: {downstream}",
  },
  "workspace.source_rerun_confirm.uploads_retained": {
    en: "User-uploaded PDFs are retained: {uploads}.",
    zh: "用户已上传 PDF 会保留：{uploads} 个。",
    ja: "ユーザーがアップロードした PDF は保持されます: {uploads} 件。",
  },
  "workspace.source_rerun_confirm.cancel": {
    en: "Cancel",
    zh: "取消",
    ja: "キャンセル",
  },
  "workspace.source_rerun_confirm.submit": {
    en: "Rerun",
    zh: "确认重跑",
    ja: "再実行",
  },
  "workspace.stale_banner_title": {
    en: "An earlier step was rerun.",
    zh: "之前的步骤已被重新运行。",
    ja: "前のステップが再実行されました。",
  },
  "workspace.stale_banner_body": {
    en: 'Sections at and after "{phase}" may be out of date. Refresh "{phase}" first to bring them up to date.',
    zh: '"{phase}" 及之后的内容可能已过期。请先刷新 "{phase}"，再继续后续步骤。',
    ja: "「{phase}」以降の内容は古い可能性があります。先に「{phase}」を更新してから先のステップに進んでください。",
  },
  // P0 fix (codex state-machine audit §1.1 A): when active_phase_lock
  // is held by the same phase as ``stale_from_phase``, the user
  // already triggered a rerun — show a "running, please wait"
  // variant instead of asking them to click rerun again.
  "workspace.stale_banner_running_title": {
    en: "The earlier step is being refreshed.",
    zh: "之前的步骤正在刷新中。",
    ja: "前のステップを更新中です。",
  },
  "workspace.stale_banner_running_body": {
    en: '"{phase}" is currently running. Sections after it will be brought up to date once it finishes — please wait.',
    zh: '"{phase}" 正在运行中。完成后下游内容将自动同步，请稍候。',
    ja: "「{phase}」は現在実行中です。完了後に以降の内容が同期されます。しばらくお待ちください。",
  },
  "workspace.restore_recovery.title": {
    en: "Restored run needs review.",
    zh: "恢复后的运行需要核对。",
    ja: "復元した実行は確認が必要です。",
  },
  "workspace.restore_recovery.body": {
    en: 'This run recorded "{phase}" as complete after a delete/cancel request. Review the phase artifacts and audit history before continuing.',
    zh: '该运行在删除/取消请求之后仍记录了 "{phase}" 完成。继续前请核对该阶段产物和审计历史。',
    ja: "削除/キャンセル要求の後に「{phase}」の完了が記録されています。続行前にフェーズ成果物と監査履歴を確認してください。",
  },
  "workspace.restore_recovery.phase_unknown": {
    en: "a phase",
    zh: "某个阶段",
    ja: "フェーズ",
  },
  "workspace.failed_banner_title": {
    en: '"{phase}" failed. You can retry it from here.',
    zh: '"{phase}" 失败了，可以在此直接重试。',
    ja: "「{phase}」が失敗しました。ここから再試行できます。",
  },
  "workspace.failed_banner_title_generic": {
    en: "This phase failed. You can retry it from here.",
    zh: "当前阶段失败，可以在此直接重试。",
    ja: "このフェーズは失敗しました。ここから再試行できます。",
  },
  "workspace.failed_banner_body_fallback": {
    en: "The system did not finish this step. Click retry, or open the phase history modal to edit prompts before rerunning.",
    zh: "系统未能完成该步骤。点击重试，或打开阶段历史并在重跑前编辑提示词。",
    ja: "このステップは完了できませんでした。再試行するか、フェーズ履歴を開いてプロンプトを編集してから再実行してください。",
  },
  "workspace.failure_per_source_summary": {
    en: "Per-source failure breakdown ({visible} of {total} shown)",
    zh: "按来源查看失败原因（共 {total} 条，展示 {visible} 条）",
    ja: "ソース別の失敗詳細（{total} 件中 {visible} 件を表示）",
  },
  "workspace.failed_retry_button": {
    en: 'Retry "{phase}"',
    zh: '重试 "{phase}"',
    ja: "「{phase}」を再試行",
  },
  "workspace.failed_retry_button_generic": {
    en: "Retry this phase",
    zh: "重试该阶段",
    ja: "このフェーズを再試行",
  },
  "workspace.failed_policy_retry_disabled": {
    en: "Policy blocks must be resolved through force approve or phase review; direct retry is disabled.",
    zh: "策略拦截需通过强制通过或阶段复核处理，不能直接重试。",
    ja: "ポリシーブロックは強制承認またはフェーズ確認で解決してください。直接の再試行は無効です。",
  },
  "workspace.failed_vendor_banner_title": {
    en: "External vendor failed.",
    zh: "外部服务失败。",
    ja: "外部ベンダーが失敗しました。",
  },
  "workspace.failed_vendor_banner_body": {
    en: "The integrity vendor returned an error or timed out. Retry the external scan, or skip integrity to proceed.",
    zh: "外部完整性扫描服务返回错误或超时。可以重试外部扫描，或跳过完整性检查继续。",
    ja: "外部の完全性スキャンが失敗またはタイムアウトしました。再試行するか、完全性をスキップして進めることができます。",
  },
  "workspace.failed_vendor_retry_button": {
    en: "Retry external scan",
    zh: "重试外部扫描",
    ja: "外部スキャンを再試行",
  },
  "workspace.failed_vendor_skip_button": {
    en: "Skip integrity",
    zh: "跳过完整性检查",
    ja: "完全性チェックをスキップ",
  },
  "workspace.failed_needs_user_banner_title": {
    en: "Your input is needed.",
    zh: "需要你处理。",
    ja: "ユーザーの対応が必要です。",
  },
  "workspace.failed_needs_user_banner_body": {
    en: "The system paused waiting for your input. Review the latest event payload below for the specific action.",
    zh: "系统暂停以等待你的处理。请查看下方最近事件负载以了解具体动作。",
    ja: "システムは入力待ちで一時停止しています。下の最近のイベント情報で具体的な操作を確認してください。",
  },
  "workspace.failed_policy_banner_title": {
    en: "Blocked by policy.",
    zh: "被策略拦截。",
    ja: "ポリシーによりブロックされました。",
  },
  "workspace.failed_policy_banner_body": {
    en: "Policy guardrails blocked this run from continuing. Review the audit details below; some blockers can be resolved by editing citations or revising the manuscript.",
    zh: "策略护栏阻止了运行继续。请查看下方审计详情；部分问题可通过编辑引用或修订正文解决。",
    ja: "ポリシーガードレールにより継続できません。下の監査詳細を確認してください。引用の修正や原稿の修正で解決できる場合があります。",
  },
  "workspace.cancelled_banner_title": {
    en: "Run cancelled.",
    zh: "运行已取消。",
    ja: "実行はキャンセルされました。",
  },
  "workspace.cancelled_banner_body": {
    en: "This run was cancelled. Create a new run to continue.",
    zh: "此次运行已被取消。请创建新的运行以继续。",
    ja: "この実行はキャンセルされました。新しい実行を作成してください。",
  },
  "workspace.failed_navigate_link": {
    en: "Open the {phase} tab to inspect / edit",
    zh: "打开「{phase}」标签查看 / 编辑",
    ja: "「{phase}」タブを開いて確認 / 編集",
  },
  "workspace.force_approve_button": {
    en: "Force approve and continue",
    zh: "强制通过并继续",
    ja: "強制承認して続行",
  },
  "workspace.force_approve_phase_button": {
    en: "Force approve {phase} and continue",
    zh: "强制通过 {phase} 并继续",
    ja: "{phase} を強制承認して続行",
  },
  "workspace.force_approve_modal_title": {
    en: "Force approve — confirm",
    zh: "强制通过 — 确认",
    ja: "強制承認 — 確認",
  },
  "workspace.force_approve_reason_label": {
    en: "Reason (required, ≥ 5 characters)",
    zh: "原因（必填，≥ 5 个字符）",
    ja: "理由（必須、5 文字以上）",
  },
  "workspace.force_approve_reason_placeholder": {
    en: "Why are you overriding the system block? This is recorded in the audit trail.",
    zh: "为什么需要强制通过系统拦截？该理由会记录到审计日志。",
    ja: "なぜシステムのブロックを上書きするのですか? 監査記録に残ります。",
  },
  "workspace.force_approve_reason_count": {
    en: "{count} / 1000 characters",
    zh: "{count} / 1000 字符",
    ja: "{count} / 1000 文字",
  },
  "workspace.force_approve_confirm": {
    en: "Confirm force-approve",
    zh: "确认强制通过",
    ja: "強制承認を実行",
  },
  "workspace.draft_degraded_minor_title": {
    en: "Draft completed with placeholder sections.",
    zh: "草稿完成，但有占位段落。",
    ja: "ドラフトは完了しましたが、プレースホルダー段落があります。",
  },
  "workspace.draft_degraded_major_title": {
    en: "Draft completed but quality is degraded — re-running the draft step is recommended.",
    zh: "草稿完成，但质量较差，建议重新运行草稿步骤。",
    ja: "ドラフトは完了しましたが品質が低下しています — 草稿ステップの再実行を推奨します。",
  },
  "workspace.draft_degraded_body": {
    en: "{stubbed} of {total} sections fell back to placeholder content. The manuscript is usable but needs review before export.",
    zh: "{total} 个章节中有 {stubbed} 个使用了占位内容，可继续使用，但导出前请人工复核。",
    ja: "{total} 章のうち {stubbed} 章がプレースホルダーになっています。原稿は使用可能ですが、エクスポート前に確認してください。",
  },
  "workspace.draft_degraded_section_ids": {
    en: "Sections to review",
    zh: "需复核的章节",
    ja: "確認が必要な章",
  },
  "workspace.close_details": {
    en: "Close workspace details",
    zh: "关闭工作台详情",
    ja: "作業画面の詳細を閉じる",
  },

  // Phase version history (codex-AGREEd #2 stage 2.A)
  "workspace.history.button": {
    en: "Phase history",
    zh: "阶段历史",
    ja: "ステージ履歴",
  },
  "workspace.history.title": {
    en: "Phase version history",
    zh: "阶段版本历史",
    ja: "ステージ版本履歴",
  },
  "workspace.history.body": {
    en: "Each phase keeps every successful run. Switch back to an earlier version to use it as the active output.",
    zh: "每个阶段会保留所有成功的运行记录。可切换到任一历史版本作为当前结果。",
    ja: "各ステージは成功した実行をすべて保存します。任意の履歴版に切り替えて現在の結果として使用できます。",
  },
  "workspace.history.no_versions": {
    en: "No versions yet for this phase.",
    zh: "该阶段尚无版本。",
    ja: "このステージにはまだ版本がありません。",
  },
  "workspace.history.rerun_phase": {
    en: "Rerun phase",
    zh: "重跑该阶段",
    ja: "このフェーズを再実行",
  },
  "workspace.history.edit_prompt_and_rerun": {
    en: "Edit prompt and rerun",
    zh: "编辑提示词并重跑",
    ja: "プロンプトを編集して再実行",
  },
  "workspace.history.version_label": {
    en: "Version {n}",
    zh: "版本 {n}",
    ja: "版本 {n}",
  },
  "workspace.history.status.done": {
    // PR-I4.b B1: pre-fix this said "active" / "当前" / "現在" which
    // collided with the head-version "Currently active" badge AND
    // with the run's own "current phase" concept. Three different
    // notions of "current" stacked on top of each other in one
    // modal made users (correctly) doubt which step the run was on.
    // The badge here means "this version's run finished cleanly" —
    // not "this is the active version" (that's the separate
    // is_active badge below).
    en: "completed",
    zh: "已完成",
    ja: "完了",
  },
  "workspace.history.status.superseded": {
    en: "earlier success",
    zh: "历史成功",
    ja: "過去の成功",
  },
  "workspace.history.status.failed": {
    en: "failed",
    zh: "失败",
    ja: "失敗",
  },
  "workspace.history.status.cancelled": {
    en: "cancelled",
    zh: "已取消",
    ja: "キャンセル済み",
  },
  "workspace.history.status.running": {
    en: "running",
    zh: "运行中",
    ja: "実行中",
  },
  "workspace.history.activate": {
    en: "Use this version",
    zh: "使用此版本",
    ja: "この版本を使用",
  },
  "workspace.history.activating": {
    en: "Switching…",
    zh: "切换中…",
    ja: "切り替え中…",
  },
  "workspace.history.activate_failed": {
    en: "Could not switch to this version",
    zh: "无法切换到该版本",
    ja: "この版本に切り替えできません",
  },
  "workspace.history.is_active": {
    en: "Currently active",
    zh: "当前生效",
    ja: "現在有効",
  },
  // PR-I4.c: shown on the backend head pointer while a NEWER
  // version is mid-flight. The user expects the running version to
  // wear the "Currently active" badge (because all downstream stale
  // markers have flipped to it), so the previous head gets a
  // passive "previous head" tag.
  "workspace.history.previous_head": {
    en: "previous head",
    zh: "上一版",
    ja: "前のヘッド",
  },
  // ── Login + landing page (PR theme + i18n redesign) ─────────────────
  // All hero / login / feature copy goes through i18n so the language
  // pill in the header actually changes visible text.
  "login.brand.name": {
    en: "Appleseed AutoEssay",
    zh: "Appleseed AutoEssay",
    ja: "Appleseed AutoEssay",
  },
  "login.brand.tagline": {
    en: "AI-POWERED ACADEMIC CREATION",
    zh: "AI 驱动的学术创作",
    ja: "AI による学術創作",
  },
  "login.nav.home": { en: "Home", zh: "首页", ja: "ホーム" },
  "login.nav.features": { en: "Features", zh: "功能", ja: "機能" },
  "login.nav.pricing": { en: "Pricing", zh: "价格", ja: "料金" },
  "login.nav.solutions": {
    en: "Solutions",
    zh: "解决方案",
    ja: "ソリューション",
  },
  "login.nav.resources": { en: "Resources", zh: "资源", ja: "リソース" },
  "login.nav.about": { en: "About", zh: "关于", ja: "私たちについて" },
  "login.cta.signup": { en: "Sign Up", zh: "注册", ja: "新規登録" },
  "login.theme.blue": { en: "Blue", zh: "蓝色", ja: "ブルー" },
  "login.theme.green": { en: "Green", zh: "绿色", ja: "グリーン" },
  "login.aria.home": {
    en: "Appleseed AutoEssay home",
    zh: "Appleseed AutoEssay 首页",
    ja: "Appleseed AutoEssay ホーム",
  },
  "login.aria.primary_navigation": {
    en: "Landing primary navigation",
    zh: "登录页主导航",
    ja: "ログインページの主要ナビゲーション",
  },
  "login.aria.mobile_menu": {
    en: "Open menu",
    zh: "打开菜单",
    ja: "メニューを開く",
  },
  "login.aria.close_menu": {
    en: "Close menu",
    zh: "关闭菜单",
    ja: "メニューを閉じる",
  },
  "login.aria.mobile_navigation": {
    en: "Mobile navigation",
    zh: "移动端导航",
    ja: "モバイルナビゲーション",
  },
  "login.aria.theme_switcher": {
    en: "Login theme",
    zh: "登录页主题",
    ja: "ログインテーマ",
  },
  "login.aria.language_switcher": {
    en: "UI language",
    zh: "界面语言",
    ja: "UI 言語",
  },
  "login.aria.features": {
    en: "Product features",
    zh: "产品功能",
    ja: "製品機能",
  },
  "login.decor.calligraphy_line_1": {
    en: "Learn without weariness",
    zh: "為學日益",
    ja: "学びて厭わず",
  },
  "login.decor.calligraphy_line_2": {
    en: "Refine day by day",
    zh: "為道日損",
    ja: "日々に磨く",
  },
  "login.decor.seal_top": { en: "Wis", zh: "智", ja: "知" },
  "login.decor.seal_bottom": { en: "dom", zh: "學", ja: "学" },
  "login.hero.title_pre": {
    en: "Ancient Wisdom",
    zh: "古典智慧",
    ja: "古典の知恵",
  },
  "login.hero.title_main": { en: "Modern", zh: "现代", ja: "現代の" },
  "login.hero.title_accent": {
    en: "Writing",
    zh: "写作",
    ja: "執筆",
  },
  "login.hero.tagline": {
    en: "You are the soul of the writing. Direction, judgment, and originality come from you; the rest, leave to Appleseed.",
    zh: "你是文章的灵魂。方向、判断与创新，源于你；其余的，交给 Appleseed。",
    ja: "文章の魂はあなた。方向性、判断、独創性はあなたから生まれます。残りは Appleseed にお任せください。",
  },
  "login.cta.get_started": {
    en: "Get Started",
    zh: "立即开始",
    ja: "はじめる",
  },
  "login.cta.learn_more": {
    en: "Learn More",
    zh: "了解更多",
    ja: "詳しく見る",
  },
  "login.card.welcome_back": {
    en: "Welcome Back",
    zh: "欢迎回来",
    ja: "おかえりなさい",
  },
  "login.card.subtitle": {
    en: "Sign in to continue to Appleseed AutoEssay",
    zh: "登录以继续使用 Appleseed AutoEssay",
    ja: "Appleseed AutoEssay を続けるにはサインインしてください",
  },
  "login.input.username_placeholder": {
    en: "Username",
    zh: "用户名",
    ja: "ユーザー名",
  },
  "login.input.password_placeholder": {
    en: "Password",
    zh: "密码",
    ja: "パスワード",
  },
  "login.input.remember_me": {
    en: "Remember me",
    zh: "记住我",
    ja: "ログイン状態を保持",
  },
  "login.input.forgot_password": {
    en: "Forgot password?",
    zh: "忘记密码？",
    ja: "パスワードを忘れた？",
  },
  "login.input.show_password": {
    en: "Show password",
    zh: "显示密码",
    ja: "パスワードを表示",
  },
  "login.input.hide_password": {
    en: "Hide password",
    zh: "隐藏密码",
    ja: "パスワードを隠す",
  },
  "login.cta.sign_in": { en: "Sign In", zh: "登录", ja: "サインイン" },
  "login.cta.signing_in": {
    en: "Signing in…",
    zh: "正在登录…",
    ja: "サインイン中…",
  },
  "login.error.invalid_credentials": {
    en: "Invalid username or password.",
    zh: "用户名或密码错误。",
    ja: "ユーザー名またはパスワードが正しくありません。",
  },
  "login.error.rate_limited": {
    en: "Too many failed attempts. Please try again in {minutes} minute(s).",
    zh: "失败次数过多，请在 {minutes} 分钟后再试。",
    ja: "失敗が多すぎます。{minutes} 分後に再度お試しください。",
  },
  "login.error.network": {
    en: "Login service is unreachable. Please try again.",
    zh: "无法连接登录服务，请重试。",
    ja: "ログインサービスに接続できません。再度お試しください。",
  },
  "login.about.heading": {
    en: "About Appleseed AutoEssay",
    zh: "关于 Appleseed AutoEssay",
    ja: "Appleseed AutoEssay について",
  },
  "login.about.body": {
    en: "Appleseed AutoEssay is a writing collaborator for serious academic work. You bring the research question; the system walks it from source search through final manuscript with reviewable steps for sources, synthesis, outline, draft, style, critique, integrity, and export. Built for scholars and students who want AI assistance without giving up control over what gets written and why.",
    zh: "Appleseed AutoEssay 是为认真学术写作打造的写作协作伙伴。你带来研究问题，系统用一组可审查的步骤把它从文献检索推进到最终稿，包括文献、综述、框架、构思、起草、风格、评审、完整性检查和导出。它面向需要 AI 协助、但不愿放弃内容与论证主导权的学者与学生。",
    ja: "Appleseed AutoEssay は本格的な学術執筆のための共同執筆パートナーです。リサーチクエスチョンを入力すると、文献検索、統合、構成、下書き、文体調整、レビュー、整合性確認、書き出しまで、確認しながら進められる手順で支援します。AI の支援を受けつつ「何を、なぜ書くか」の主導権を手放したくない研究者と学生のためのツールです。",
  },
  "login.coming_soon.title": {
    en: "Coming Soon",
    zh: "暂未开放",
    ja: "近日公開予定",
  },
  "login.coming_soon.body": {
    en: "This feature isn't available yet. Stay tuned — we'll open it shortly.",
    zh: "敬请恭候，此功能即将开放。",
    ja: "公開準備中です。もうしばらくお待ちください。",
  },
  "login.coming_soon.dismiss": {
    en: "Got it",
    zh: "知道了",
    ja: "了解しました",
  },
  "login.divider.or_continue": {
    en: "or continue with",
    zh: "或使用以下方式继续",
    ja: "または以下で続行",
  },
  "login.oauth.google": {
    en: "Sign in with Google",
    zh: "使用 Google 登录",
    ja: "Google でサインイン",
  },
  "login.oauth.microsoft": {
    en: "Sign in with Microsoft",
    zh: "使用 Microsoft 登录",
    ja: "Microsoft でサインイン",
  },
  "login.oauth.sso": {
    en: "Sign in with SSO",
    zh: "使用 SSO 登录",
    ja: "SSO でサインイン",
  },
  "login.feature.ai_writing.title": {
    en: "AI Writing",
    zh: "AI 写作",
    ja: "AI ライティング",
  },
  "login.feature.ai_writing.blurb": {
    en: "Generate, refine, and elevate academic content with AI.",
    zh: "用 AI 生成、修订并提升学术内容。",
    ja: "AI で学術コンテンツを生成、改善、向上させます。",
  },
  "login.feature.research.title": {
    en: "Research",
    zh: "研究",
    ja: "リサーチ",
  },
  "login.feature.research.blurb": {
    en: "Organize sources, analyze papers, and build knowledge.",
    zh: "整理文献、分析论文、构建知识。",
    ja: "資料整理、論文分析、知識構築。",
  },
  "login.feature.multilingual.title": {
    en: "Multilingual",
    zh: "多语言",
    ja: "多言語",
  },
  "login.feature.multilingual.blurb": {
    en: "Write and translate across languages with precision.",
    zh: "跨语言精准写作与翻译。",
    ja: "言語を超えて精緻に書き、翻訳します。",
  },
  "login.feature.knowledge.title": {
    en: "Knowledge Base",
    zh: "知识库",
    ja: "ナレッジベース",
  },
  "login.feature.knowledge.blurb": {
    en: "Capture insights and build your personal knowledge library.",
    zh: "记录洞见，构建你的个人知识库。",
    ja: "知見を記録し、あなたの個人ナレッジを築きます。",
  },
  // PR-I4.b B2: phase-history modal top banner shown when the run
  // is currently in any *_RUNNING state. Tells the user up front
  // that mutations are locked out (so the disabled buttons + 409s
  // make sense) and which phase is running. The {phase} placeholder
  // gets the human-readable phase label from `phase.${name}`.
  "workspace.history.running_banner": {
    en: "{phase} is currently running. Rerun / edit / activate / delete actions are locked until it finishes.",
    zh: "{phase} 正在运行。重跑 / 编辑 / 激活 / 删除等操作要等该步骤跑完才能用。",
    ja: "{phase} を実行中です。完了するまで再実行 / 編集 / 有効化 / 削除などの操作はロックされます。",
  },
  // PR-I4.b A9: prompt sub-modal Save and Save-and-rerun buttons
  // when the run is RUNNING — surfaced inline next to the disabled
  // buttons so a user who opened the modal before kicking off
  // synthesizer doesn't think the modal is just broken.
  "workspace.prompt.locked_running": {
    en: "Locked while a phase is running. Save / save-and-rerun re-enable when the current phase finishes.",
    zh: "有阶段正在运行，已锁定。等当前阶段跑完后保存 / 保存并重跑会自动恢复。",
    ja: "他のフェーズが実行中のためロックされています。完了後に保存 / 保存して再実行が再び使えます。",
  },
  // workspace.history.source.* — origin badges shown alongside each
  // version entry. The automatic writing origin is intentionally not
  // labelled because every existing version uses it;
  // showing a badge for the common case would be visual noise. Only
  // other origins (``user_edit`` from PR-A2's PUT endpoints,
  // future ``rerun`` / ``fork`` etc.) get a visible badge.
  "workspace.history.source.user_edit": {
    en: "User edit",
    zh: "用户编辑",
    ja: "ユーザー編集",
  },
  // PR-A4.4 phase-history modal redesign keys (codex AGREE
  // 2026-05-02 amendment 3: stay under workspace.history.*).
  "workspace.history.state.generated.title": {
    en: "Generated",
    zh: "已生成",
    ja: "生成済み",
  },
  "workspace.history.state.generated.reason": {
    en: "Up to date with upstream and prompt.",
    zh: "与上游和提示词保持一致。",
    ja: "上流とプロンプトに整合しています。",
  },
  "workspace.history.state.prompt_edited.title": {
    en: "Prompt edited, not regenerated",
    zh: "已编辑提示词，未重新生成",
    ja: "プロンプト編集済み、未再生成",
  },
  "workspace.history.state.prompt_edited.reason": {
    en: "Cancel to revert to the last saved prompt, or regenerate to apply your edit.",
    zh: "取消可恢复上次保存的提示词；重新生成可应用你的修改。",
    ja: "取り消すと前回の保存内容に戻ります。再生成すると編集が反映されます。",
  },
  "workspace.history.state.upstream_superseded.title": {
    en: "Upstream advanced",
    zh: "上游已升级",
    ja: "上流が更新されました",
  },
  "workspace.history.state.upstream_superseded.reason": {
    en: "An upstream phase changed since this version ran. Snap to a matching version, or regenerate.",
    zh: "本版本运行后上游已更新。可激活与上游 lineage 一致的旧版本，或重跑生成新版。",
    ja: "上流が変更されたため、整合する旧版を有効化するか、再生成してください。",
  },
  // Codex round 2 amendment 3: when prompt_dirty AND
  // lineage_dirty coexist, prompt edits take priority so
  // ``activate_lineage_match`` is intentionally NOT shown — but
  // we still need to tell the user the upstream changed. Use a
  // dedicated message instead of reusing the upstream-superseded
  // reason (which would point them to an action that isn't
  // surfaced).
  "workspace.history.state.prompt_edited_with_upstream.advisory": {
    en: "Upstream also changed. To use a matching historical version, cancel the prompt edit first; otherwise regenerate to incorporate both.",
    zh: "上游同时已更新。如想使用与上游一致的旧版本，请先取消提示词编辑；否则请重跑以同时纳入提示词与上游变化。",
    ja: "上流も変更されました。整合する旧版を使うにはプロンプト編集をキャンセルしてください。さもなければ再生成で両方を取り込みます。",
  },
  "workspace.history.state.ungenerated.title": {
    en: "Not generated",
    zh: "未生成",
    ja: "未生成",
  },
  "workspace.history.state.ungenerated.reason": {
    en: "This phase has no active version on this branch.",
    zh: "本分支当前没有该阶段的活动版本。",
    ja: "このブランチにはこのフェーズの有効な版がありません。",
  },
  // Action labels — primary action row.
  "workspace.history.action.rerun": {
    en: "Rerun",
    zh: "重跑该阶段",
    ja: "再実行",
  },
  "workspace.history.action.edit_prompt": {
    en: "Edit prompt…",
    zh: "编辑提示词…",
    ja: "プロンプトを編集…",
  },
  "workspace.history.action.cancel_prompt": {
    en: "Cancel edit",
    zh: "取消修改",
    ja: "編集を取り消し",
  },
  "workspace.history.action.regenerate": {
    en: "Regenerate",
    zh: "重新生成",
    ja: "再生成",
  },
  "workspace.history.action.activate_lineage_match": {
    en: "Use matching version",
    zh: "激活匹配版本",
    ja: "整合する版を有効化",
  },
  "workspace.history.action.rerun_for_new_match": {
    en: "Regenerate to match",
    zh: "重跑生成新版",
    ja: "再生成して整合",
  },
  "workspace.history.action.run_now": {
    en: "Run this phase",
    zh: "运行该阶段",
    ja: "このフェーズを実行",
  },
  "workspace.history.action.run_now_blocked": {
    en: "Finish prerequisite phases first",
    zh: "先完成前置步骤",
    ja: "先に前提ステップを完了してください",
  },
  "workspace.history.action.disabled_running": {
    en: "A phase is running — wait until it finishes",
    zh: "某节点正在运行，请等待完成",
    ja: "フェーズ実行中です。完了までお待ちください",
  },
  "workspace.history.action.scroll_to_versions": {
    en: "Activate a historical version",
    zh: "激活历史版本",
    ja: "履歴の版を有効化",
  },
  "workspace.history.action.delete": {
    en: "Delete",
    zh: "删除",
    ja: "削除",
  },
  // Delete-block tooltips (codex amendment 4: render via i18n,
  // never display raw backend reason strings to users).
  "workspace.history.delete_block.active_head": {
    en: "Active head — activate a different version first",
    zh: "当前生效版本，激活其他版本后再删",
    ja: "有効な版です。別の版を有効化してから削除してください",
  },
  "workspace.history.delete_block.lineage_child": {
    en: "Has a child version — delete the child first",
    zh: "存在子版本，请先删除子版本",
    ja: "子版が存在します。先に子版を削除してください",
  },
  "workspace.history.delete_block.fork_point": {
    en: "Fork point of branch '{branch}' — delete the branch first",
    zh: "分支 '{branch}' 的 fork 起点，请先删除该分支",
    ja: "ブランチ '{branch}' の分岐点です。先にブランチを削除してください",
  },
  "workspace.history.delete_block.downstream_dependent": {
    en: "Used by {name} — delete the downstream version first",
    zh: "被 {name} 引用，请先删除下游版本",
    ja: "{name} から参照されています。下流の版を先に削除してください",
  },
  // Card-level labels.
  "workspace.history.upstream_label": {
    en: "Upstream",
    zh: "上游",
    ja: "上流",
  },
  "workspace.history.upstream_mismatch_hint": {
    en: "(out of sync)",
    zh: "（不一致）",
    ja: "（不整合）",
  },
  "workspace.history.versions_section_label": {
    en: "All versions ({n})",
    zh: "所有版本（{n}）",
    ja: "全版本（{n}）",
  },
  "workspace.history.versions_section_empty": {
    en: "No versions yet on this branch.",
    zh: "本分支尚无版本。",
    ja: "このブランチにまだ版がありません。",
  },
  "workspace.history.error.load": {
    en: "Failed to load phase history.",
    zh: "加载阶段历史失败。",
    ja: "ステージ履歴の読み込みに失敗しました。",
  },
  "workspace.history.error.action": {
    en: "Action failed: {message}",
    zh: "操作失败：{message}",
    ja: "操作に失敗しました：{message}",
  },
  // workspace.edit_content.* — generic per-phase artifact edit modal
  // (PR-A2). Opens from a button under the workspace tab strip when
  // the active subview maps to a user-editable phase.
  "workspace.edit_content.button": {
    en: "Edit current step's content",
    zh: "编辑当前节点的内容",
    ja: "現在のステップの内容を編集",
  },
  "workspace.edit_content.title": {
    en: "Edit step artifacts",
    zh: "编辑节点产物",
    ja: "ステップ成果物を編集",
  },
  "workspace.edit_content.description": {
    en: 'Save creates a new version tagged "User edit" and marks downstream steps stale.',
    zh: "保存会新建一个标记为「用户编辑」的版本，下游步骤会被标为待重跑。",
    ja: "保存すると「ユーザー編集」とタグ付けされた新しいバージョンが作成され、後続ステップが要再実行となります。",
  },
  "workspace.edit_content.empty": {
    en: "This step has no editable artifacts yet, or has not produced output on this branch.",
    zh: "该节点尚无可编辑产物，或当前分支上未产出。",
    ja: "このステップにはまだ編集可能な成果物がない、または現在のブランチで未生成です。",
  },
  // PR-A3 unified replace-vs-new toggle. Shown when no downstream
  // phase has produced output AND the head pv is exclusive to the
  // active branch. When unavailable the modal falls back to "new"
  // and shows the existing description copy above.
  "workspace.edit_content.mode_heading": {
    en: "Save as",
    zh: "保存方式",
    ja: "保存方法",
  },
  "workspace.edit_content.mode.replace": {
    en: "Replace current version",
    zh: "替换当前版本",
    ja: "現在のバージョンを置き換え",
  },
  "workspace.edit_content.mode.replace_hint": {
    en: "No downstream step has run yet. Skips creating a new version row.",
    zh: "尚未运行下游节点。不会新增版本记录。",
    ja: "後続ステップは未実行。新しいバージョンレコードは作成されません。",
  },
  "workspace.edit_content.mode.new": {
    en: "Publish new version",
    zh: "发布新版本",
    ja: "新しいバージョンとして公開",
  },
  "workspace.edit_content.mode.new_hint": {
    en: "Creates a new version on the timeline.",
    zh: "在版本时间线上新增一条记录。",
    ja: "バージョンタイムラインに新しいレコードを追加します。",
  },
  "workspace.edit_content.mode.new_forced": {
    en: "Downstream steps have already run, so saving will create a new version.",
    zh: "下游步骤已运行，因此保存会新建版本。",
    ja: "後続ステップが既に実行済みのため、保存は新しいバージョンを作成します。",
  },
  "workspace.edit_content.save": {
    en: "Save",
    zh: "保存",
    ja: "保存",
  },
  "workspace.edit_content.saving": {
    en: "Saving…",
    zh: "保存中…",
    ja: "保存中…",
  },
  "workspace.edit_content.cancel": {
    en: "Cancel",
    zh: "取消",
    ja: "キャンセル",
  },
  "workspace.edit_content.required_with_hint": {
    en: "Must be saved together with: {path}",
    zh: "必须与下列文件一起保存：{path}",
    ja: "次のファイルと一緒に保存する必要があります：{path}",
  },
  "workspace.history.created_label": {
    en: "Created",
    zh: "创建于",
    ja: "作成日時",
  },
  "workspace.history.artifact_count": {
    en: "{n} files",
    zh: "{n} 个文件",
    ja: "{n} 個のファイル",
  },
  "workspace.history.phase_section": {
    en: "{phase}",
    zh: "{phase}",
    ja: "{phase}",
  },
  "workspace.history.loading": {
    en: "Loading version history…",
    zh: "正在加载版本历史…",
    ja: "版本履歴を読み込み中…",
  },
  "workspace.history.load_failed": {
    en: "Could not load version history.",
    zh: "无法加载版本历史。",
    ja: "版本履歴を読み込めません。",
  },
  "workspace.history.view_prompt": {
    en: "View prompt",
    zh: "查看提示词",
    ja: "プロンプトを表示",
  },

  // Per-phase prompt override (codex-AGREEd #2 stage 2.B)
  "workspace.prompt.edit_button": {
    en: "Edit prompt",
    zh: "编辑提示词",
    ja: "プロンプトを編集",
  },
  "workspace.prompt.eyebrow": {
    en: "Prompt for {phase}",
    zh: "{phase} 的提示词",
    ja: "{phase} のプロンプト",
  },
  "workspace.prompt.title": {
    en: "Phase prompt",
    zh: "阶段提示词",
    ja: "ステージプロンプト",
  },
  "workspace.prompt.body": {
    en: "Edit the static instruction block this phase sends to the model. Dynamic context (sources, schemas) is always appended automatically.",
    zh: "编辑该阶段发送给模型的静态指令。动态上下文（文献、结构定义）会自动附加。",
    ja: "このステージがモデルに送る静的な指示ブロックを編集できます。動的なコンテキスト（文献、スキーマ）は自動的に追加されます。",
  },
  "workspace.prompt.default_label": {
    en: "Default instructions",
    zh: "默认指令",
    ja: "既定の指示",
  },
  "workspace.prompt.override_label": {
    en: "Your override",
    zh: "你的覆盖",
    ja: "上書き内容",
  },
  "workspace.prompt.override_placeholder": {
    en: "Leave blank to use the default instructions.",
    zh: "留空表示使用默认指令。",
    ja: "空のままにすると既定の指示が使われます。",
  },
  "workspace.prompt.dynamic_context_note": {
    en: "Source data, claim lists, and required JSON schemas are not editable here.",
    zh: "文献数据、论点列表与必需的 JSON 结构在此处不可编辑。",
    ja: "文献データ、主張リスト、必須の JSON スキーマはここでは編集できません。",
  },
  "workspace.prompt.save": {
    en: "Save draft",
    zh: "保存草稿",
    ja: "下書きを保存",
  },
  "workspace.prompt.saving": {
    en: "Saving…",
    zh: "保存中…",
    ja: "保存中…",
  },
  "workspace.prompt.save_and_rerun": {
    en: "Save and rerun",
    zh: "保存并重新运行",
    ja: "保存して再実行",
  },
  "workspace.prompt.discard": {
    en: "Revert to default",
    zh: "恢复默认",
    ja: "既定に戻す",
  },
  "workspace.prompt.loading": {
    en: "Loading prompt…",
    zh: "正在加载提示词…",
    ja: "プロンプトを読み込み中…",
  },
  "workspace.prompt.load_failed": {
    en: "Could not load the prompt",
    zh: "无法加载提示词",
    ja: "プロンプトを読み込めません",
  },
  "workspace.prompt.save_failed": {
    en: "Could not save the prompt",
    zh: "无法保存提示词",
    ja: "プロンプトを保存できません",
  },
  "workspace.prompt.key_label": {
    en: "Prompt surface",
    zh: "提示词类型",
    ja: "プロンプト種別",
  },
  "workspace.prompt.discard_unsaved_confirm": {
    en: "You have unsaved edits. Switch surface and discard them?",
    zh: "当前提示词存在未保存的修改，切换将丢弃这些修改，确定吗？",
    ja: "未保存の変更があります。種別を切り替えて破棄しますか?",
  },
  "workspace.prompt.history_eyebrow": {
    en: "Prompt used by this version",
    zh: "该版本使用的提示词",
    ja: "この版本が使用したプロンプト",
  },
  "workspace.prompt.no_snapshot": {
    en: "No prompt snapshot is recorded for this version.",
    zh: "该版本未记录提示词快照。",
    ja: "この版本にはプロンプトのスナップショットがありません。",
  },

  // Branches / forks (codex-AGREEd #2 stage 2.C)
  "workspace.branch.label": {
    en: "Branch",
    zh: "分支",
    ja: "ブランチ",
  },
  "workspace.branch.fork_button": {
    en: "Fork from this version",
    zh: "从该版本分叉",
    ja: "この版本から分岐",
  },
  "workspace.branch.fork_eyebrow": {
    en: "Forking from {label}",
    zh: "从 {label} 分叉",
    ja: "{label} から分岐",
  },
  "workspace.branch.fork_title": {
    en: "Name the new branch",
    zh: "为新分支命名",
    ja: "新しいブランチ名",
  },
  "workspace.branch.fork_body": {
    en: "Upstream phases stay shared with the source branch. The forked phase and everything downstream start empty so a rerun produces fresh output for this branch.",
    zh: "上游阶段与源分支保持共享。被分叉的阶段及其下游从空开始，重新运行会为该分支生成新的输出。",
    ja: "上流のステージは元のブランチと共有されます。分岐対象のステージとそれ以降は空の状態で開始し、再実行するとこのブランチ専用の新しい出力が生成されます。",
  },
  "workspace.branch.name_placeholder": {
    en: "e.g. shorter-claims",
    zh: "例如：shorter-claims",
    ja: "例：shorter-claims",
  },
  "workspace.branch.name_required": {
    en: "Branch name required.",
    zh: "需要填写分支名称。",
    ja: "ブランチ名が必要です。",
  },
  "workspace.branch.fork_confirm": {
    en: "Create branch",
    zh: "创建分支",
    ja: "ブランチを作成",
  },
  "workspace.branch.forking": {
    en: "Forking…",
    zh: "正在分叉…",
    ja: "分岐中…",
  },
  "workspace.branch.fork_failed": {
    en: "Could not create branch",
    zh: "无法创建分支",
    ja: "ブランチを作成できません",
  },

  // Per-version diff (codex-AGREEd #2 stage 2.D)
  "workspace.diff.button": {
    en: "Compare to parent",
    zh: "与父版本对比",
    ja: "親版と比較",
  },
  "workspace.diff.eyebrow": {
    en: "Version diff",
    zh: "版本对比",
    ja: "版本差分",
  },
  "workspace.diff.title": {
    en: "Version {from} → version {to}",
    zh: "版本 {from} → 版本 {to}",
    ja: "版本 {from} → 版本 {to}",
  },
  "workspace.diff.context_prompt_changed": {
    en: "Prompt changed",
    zh: "提示词已变化",
    ja: "プロンプトが変更",
  },
  "workspace.diff.context_prompt_same": {
    en: "Same prompt",
    zh: "提示词相同",
    ja: "同一プロンプト",
  },
  "workspace.diff.context_upstream_same": {
    en: "Same upstream inputs",
    zh: "上游输入相同",
    ja: "上流入力が同じ",
  },
  "workspace.diff.context_upstream_changed": {
    en: "Upstream inputs changed",
    zh: "上游输入已变化",
    ja: "上流入力が変更",
  },
  "workspace.diff.summary": {
    en: "{added} added · {removed} removed · {changed} changed · {unchanged} unchanged",
    zh: "新增 {added} · 删除 {removed} · 修改 {changed} · 未变 {unchanged}",
    ja: "追加 {added} · 削除 {removed} · 変更 {changed} · 未変更 {unchanged}",
  },
  "workspace.diff.no_changes": {
    en: "No file-level changes between these two versions.",
    zh: "这两个版本之间没有文件级别的变化。",
    ja: "この2つの版本間にファイルレベルの差分はありません。",
  },
  "workspace.diff.expand": {
    en: "Expand",
    zh: "展开",
    ja: "展開",
  },
  "workspace.diff.collapse": {
    en: "Collapse",
    zh: "收起",
    ja: "折りたたむ",
  },
  "workspace.diff.match_basis": {
    en: "Records matched by: {basis}",
    zh: "记录匹配方式：{basis}",
    ja: "レコード照合方法：{basis}",
  },
  "workspace.diff.binary": {
    en: "Binary file differs.",
    zh: "二进制文件存在差异。",
    ja: "バイナリファイルが異なります。",
  },
  "workspace.diff.whole_file_meta": {
    en: "{size} bytes · sha256 {sha}",
    zh: "{size} 字节 · sha256 {sha}",
    ja: "{size} バイト · sha256 {sha}",
  },
  "workspace.diff.loading": {
    en: "Computing diff…",
    zh: "正在计算差分…",
    ja: "差分を計算中…",
  },
  "workspace.diff.load_failed": {
    en: "Could not load diff",
    zh: "无法加载差分",
    ja: "差分を読み込めません",
  },
  "workspace.paper_language_title": {
    en: "Paper language: {language}",
    zh: "论文语言：{language}",
    ja: "論文の言語：{language}",
  },
  "workspace.tablist_label": {
    en: "Workspace views",
    zh: "工作台视图",
    ja: "作業画面ビュー",
  },
  "workspace.console.scout_progress": {
    en: "Literature search progress",
    zh: "文献检索进度",
    ja: "文献検索の進捗",
  },
  "workspace.console.no_source_progress": {
    en: "Searching for sources, waiting for the first results…",
    zh: "正在检索相关文献，等待第一批结果…",
    ja: "関連文献を検索中、最初の結果を待機中…",
  },
  "workspace.console.scout_report": {
    en: "Literature search report",
    zh: "文献检索报告",
    ja: "文献検索レポート",
  },
  "workspace.console.report_pending": {
    en: "Report pending.",
    zh: "报告待生成。",
    ja: "レポート保留中。",
  },
  "workspace.console.curator_progress": {
    en: "Curation progress",
    zh: "文献筛选进度",
    ja: "選定の進捗",
  },
  "workspace.console.no_curator_progress": {
    en: "Filtering candidate sources, waiting for progress updates…",
    zh: "正在筛选候选文献，等待进度更新…",
    ja: "候補文献を選定中、進捗の更新を待機中…",
  },
  "workspace.console.no_events": {
    en: "No events yet.",
    zh: "尚无事件。",
    ja: "まだイベントはない。",
  },
  "workspace.console.subtab.timeline": {
    en: "Status timeline",
    zh: "状态时间线",
    ja: "状態タイムライン",
  },
  "workspace.console.subtab.system_output": {
    en: "System output",
    zh: "系统输出",
    ja: "システム出力",
  },
  "phase.scout": {
    en: "Literature search",
    zh: "文献检索",
    ja: "文献検索",
  },
  "phase.curator": {
    en: "Curation",
    zh: "文献筛选",
    ja: "選定",
  },
  "phase.synthesizer": {
    en: "Synthesis",
    zh: "综合",
    ja: "合成",
  },
  "phase.tension_extraction": {
    en: "Tension extraction",
    zh: "张力提取",
    ja: "テンション抽出",
  },
  "phase.ideator": {
    en: "Novelty",
    zh: "新颖性",
    ja: "新規性",
  },
  "phase.drafter": {
    en: "Draft",
    zh: "草稿",
    ja: "原稿",
  },
  "phase.stylist": {
    en: "Style",
    zh: "文风",
    ja: "文体",
  },
  "phase.final_rewrite": {
    en: "Final rewrite",
    zh: "最终改写",
    ja: "最終書き直し",
  },
  "phase.critic": {
    en: "Review",
    zh: "评审",
    ja: "レビュー",
  },
  "phase.integrity": {
    en: "Integrity",
    zh: "完整性",
    ja: "整合性",
  },
  "phase.exports": {
    en: "Exports",
    zh: "导出",
    ja: "エクスポート",
  },
  "phase.proposal": {
    en: "Proposal",
    zh: "提案",
    ja: "提案",
  },
  "console.timeline.run_created": {
    en: "Essay run created.",
    zh: "论文运行已创建。",
    ja: "論文の実行を作成しました。",
  },
  "console.timeline.run_cancelled": {
    en: "Essay run cancelled.",
    zh: "论文运行已取消。",
    ja: "論文の実行をキャンセルしました。",
  },
  "console.timeline.state_transition": {
    en: "Status: {from} → {to}.",
    zh: "状态：{from} → {to}。",
    ja: "状態：{from} → {to}。",
  },
  "console.timeline.state_set": {
    en: "Status: {to}.",
    zh: "状态：{to}。",
    ja: "状態：{to}。",
  },
  "console.timeline.state_changed": {
    en: "Status changed.",
    zh: "状态已变更。",
    ja: "状態が変更されました。",
  },
  "console.timeline.phase_started": {
    en: "{phase} started.",
    zh: "{phase} 步骤开始。",
    ja: "{phase} を開始しました。",
  },
  "console.timeline.phase_done": {
    en: "{phase} finished.",
    zh: "{phase} 步骤完成。",
    ja: "{phase} が完了しました。",
  },
  "console.timeline.phase_failed": {
    en: "{phase} failed.",
    zh: "{phase} 步骤失败。",
    ja: "{phase} が失敗しました。",
  },
  "console.timeline.phase_failed_with_reason": {
    en: "{phase} failed: {reason}",
    zh: "{phase} 步骤失败：{reason}",
    ja: "{phase} が失敗しました：{reason}",
  },
  "console.timeline.phase_waiting": {
    en: "{phase} is waiting for your input.",
    zh: "{phase} 步骤等待你的输入。",
    ja: "{phase} があなたの入力を待っています。",
  },
  "console.timeline.source_progress": {
    en: "Source {source}: {status}.",
    zh: "文献 {source}：{status}。",
    ja: "文献 {source}：{status}。",
  },
  "console.timeline.section_progress": {
    en: "Section {section}: {status}.",
    zh: "章节 {section}：{status}。",
    ja: "セクション {section}：{status}。",
  },
  "console.timeline.proposal_saved": {
    en: "Proposal saved.",
    zh: "提案已保存。",
    ja: "提案を保存しました。",
  },
  "console.timeline.source_uploaded": {
    en: "You uploaded a source PDF.",
    zh: "你上传了一份 PDF 文献。",
    ja: "PDF 文献をアップロードしました。",
  },
  "console.timeline.checkpoint_recorded": {
    en: "Review choice saved.",
    zh: "审核选择已保存。",
    ja: "レビュー内容を保存しました。",
  },
  "console.timeline.force_approve": {
    en: "You force-approved and continued.",
    zh: "你执行了强制通过并继续。",
    ja: "強制承認して続行しました。",
  },
  "console.timeline.force_approve_with_reason": {
    en: "You force-approved and continued. Reason: {reason}",
    zh: "你执行了强制通过并继续。理由：{reason}",
    ja: "強制承認して続行しました。理由：{reason}",
  },
  "console.timeline.phase_lock_force_cleared": {
    en: "Operations cleared a stuck lock on {phase}.",
    zh: "运维清除了 {phase} 上的卡死锁。",
    ja: "オペレーションが {phase} の停滞ロックを解除しました。",
  },
  // PR-I3: StuckRunBanner — user-triggered escape hatch when worker
  // SIGKILL leaves a run permanently in *_RUNNING. Threshold mirrors
  // backend ``_ZOMBIE_PHASE_IDLE_SECONDS_DEFAULT`` (15 min) and the
  // text deliberately does NOT promise "we will fix it" — the
  // recover button asks the same compound gate the reaper uses;
  // gate may refuse (409) and the user gets the refresh hint.
  "workspace.stuck_banner.title": {
    en: "This step looks stuck",
    zh: "这一步似乎卡住了",
    ja: "このステップが停止している可能性があります",
  },
  "workspace.stuck_banner.body": {
    en: "{phase} has been silent for {minutes} minutes. The worker process may have died mid-flight (out-of-memory, container restart). Click the button below to force this step into a retryable state.",
    zh: "{phase} 阶段已经 {minutes} 分钟没有任何进展。worker 进程可能在中途死掉了（OOM、容器重启等）。点击下方按钮把当前步骤转为可重试状态。",
    ja: "{phase} は {minutes} 分間進展がありません。ワーカープロセスが途中で停止した可能性があります（OOM、コンテナ再起動など）。下のボタンを押して、このステップをリトライ可能な状態に強制移行してください。",
  },
  "workspace.stuck_banner.recover_button": {
    en: "Force recover this step",
    zh: "强制恢复该步骤",
    ja: "このステップを強制復旧",
  },
  "workspace.stuck_banner.recovering": {
    en: "Recovering…",
    zh: "正在恢复…",
    ja: "復旧中…",
  },
  "workspace.stuck_banner.gate_refused": {
    en: "Recovery refused — the worker may still be alive, or the step has already finished. Refresh the page to see the latest state.",
    zh: "无法恢复 — worker 可能还在运行，或者该步骤已经完成。请刷新页面查看最新状态。",
    ja: "復旧できません — ワーカーがまだ動作中か、ステップが既に完了している可能性があります。ページを更新して最新状態を確認してください。",
  },
  "workspace.stuck_banner.error_generic": {
    en: "Recovery failed: {message}",
    zh: "恢复失败：{message}",
    ja: "復旧に失敗しました：{message}",
  },
  "console.timeline.scan_kinds_skipped": {
    en: "Skipped checks: {kinds}.",
    zh: "已跳过的检查：{kinds}。",
    ja: "スキップしたチェック：{kinds}。",
  },
  "workspace.common.results_suffix": {
    en: "results",
    zh: "条结果",
    ja: "件",
  },
  "workspace.common.source_default": {
    en: "source",
    zh: "文献",
    ja: "文献",
  },
  "workspace.common.section_default": {
    en: "section",
    zh: "章节",
    ja: "セクション",
  },
  "workspace.common.status_pending": {
    en: "pending",
    zh: "待处理",
    ja: "保留中",
  },
  "workspace.common.previous": { en: "Previous", zh: "上一页", ja: "前へ" },
  "workspace.common.next": { en: "Next", zh: "下一页", ja: "次へ" },
  "workspace.common.page_of": {
    en: "Page {current} of {total}",
    zh: "第 {current} / {total} 页",
    ja: "{current} / {total} ページ",
  },
  "workspace.common.none_lower": { en: "none", zh: "无", ja: "なし" },

  // workspace.proposal.*
  "workspace.proposal.heading": { en: "Proposal", zh: "提案", ja: "提案" },
  // Shown above the proposal form when the user edits the
  // proposal AFTER USER_PROPOSAL_REVIEW has already moved on (i.e.
  // scout has been triggered). Saving in that state will mark the
  // earliest already-completed downstream phase stale, prompting
  // the user to rerun it. Mirrors the post-edit stale banner from
  // PR #91/#94.
  "workspace.proposal.post_accept_warning": {
    en: "Editing the proposal after acceptance will mark the earliest already-completed phase from scout onwards as stale. Rerun that phase to apply the new proposal downstream.",
    zh: "在接受提案后再次编辑，会把从「文献检索」开始的最早已完成阶段标为待重跑；如需让后续阶段使用新提案，请重跑该阶段。",
    ja: "受理後に提案を編集すると、文献検索以降で最初に完了している段階が要再実行となります。新しい提案を以降の段階に反映するには、その段階を再実行してください。",
  },
  // PR-A3 proposal save mode toggle.
  "workspace.proposal.mode.replace": {
    en: "Replace current version",
    zh: "替换当前版本",
    ja: "現在のバージョンを置き換え",
  },
  "workspace.proposal.mode.replace_hint": {
    en: "No phase has produced output yet. Overwrites proposal v{version}.",
    zh: "尚无阶段产出。覆盖提案 v{version}。",
    ja: "まだ出力された段階はありません。提案 v{version} を上書きします。",
  },
  "workspace.proposal.mode.new": {
    en: "Publish new version",
    zh: "发布新版本",
    ja: "新しいバージョンとして公開",
  },
  "workspace.proposal.mode.new_hint": {
    en: "Creates proposal v{next_version} on the version timeline.",
    zh: "在提案版本时间线上新增 v{next_version}。",
    ja: "提案バージョンタイムラインに v{next_version} を追加します。",
  },
  "workspace.proposal.mode.new_forced": {
    en: "Downstream phases have already run, so saving will create a new proposal version.",
    zh: "下游阶段已运行，因此保存会新建提案版本。",
    ja: "後続段階が既に実行済みのため、保存は新しい提案バージョンを作成します。",
  },
  // PR-A3 inline project title editor in the workspace heading.
  "workspace.title.edit_button": {
    en: "Edit title",
    zh: "编辑标题",
    ja: "タイトルを編集",
  },
  "workspace.title.save": { en: "Save", zh: "保存", ja: "保存" },
  "workspace.title.cancel": { en: "Cancel", zh: "取消", ja: "キャンセル" },
  "workspace.title.error_required": {
    en: "Title cannot be empty",
    zh: "标题不能为空",
    ja: "タイトルは空にできません",
  },
  "workspace.title.error_failed": {
    en: "Failed to update title",
    zh: "更新标题失败",
    ja: "タイトルの更新に失敗しました",
  },
  "workspace.proposal.md_initial_proposal": {
    en: "Initial Proposal",
    zh: "初始提案",
    ja: "初期提案",
  },
  "workspace.proposal.md_research_question": {
    en: "Research Question",
    zh: "研究问题",
    ja: "研究課題",
  },
  "workspace.proposal.md_significance": {
    en: "Significance",
    zh: "研究意义",
    ja: "意義",
  },
  "workspace.proposal.md_preliminary_approach": {
    en: "Preliminary Approach",
    zh: "初步方法",
    ja: "初期アプローチ",
  },
  "workspace.proposal.md_expected_contribution": {
    en: "Expected Contribution",
    zh: "预期贡献",
    ja: "期待される貢献",
  },
  "workspace.proposal.md_scope": {
    en: "Scope",
    zh: "研究范围",
    ja: "範囲",
  },
  "workspace.proposal.md_preliminary_keywords": {
    en: "Preliminary Keywords",
    zh: "初步关键词",
    ja: "初期キーワード",
  },
  "workspace.proposal.md_pending": {
    en: "Pending.",
    zh: "待定。",
    ja: "保留中。",
  },
  "workspace.proposal.draft_notes": {
    en: "Draft notes",
    zh: "草稿备注",
    ja: "草稿メモ",
  },
  "workspace.proposal.drafting_in_progress": {
    en: "Proposal draft is being generated.",
    zh: "提案草稿正在生成。",
    ja: "提案の草稿を生成中。",
  },
  "workspace.proposal.regenerating": {
    en: "Regenerating...",
    zh: "正在重新生成…",
    ja: "再生成中…",
  },
  "workspace.proposal.regenerate": {
    en: "Regenerate",
    zh: "重新生成",
    ja: "再生成",
  },
  "workspace.proposal.saving": {
    en: "Saving...",
    zh: "保存中…",
    ja: "保存中…",
  },
  "workspace.proposal.save_edits": {
    en: "Save edits",
    zh: "保存修改",
    ja: "編集を保存",
  },
  "workspace.proposal.starting_search": {
    en: "Starting search...",
    zh: "正在启动检索…",
    ja: "検索を起動中…",
  },
  "workspace.proposal.accept_and_search": {
    en: "Accept and start literature search",
    zh: "接受并开始文献检索",
    ja: "承認して文献検索を開始",
  },
  "workspace.proposal.artifact_pending": {
    en: "Proposal artifact pending.",
    zh: "提案产物待生成。",
    ja: "提案成果物の生成待ち。",
  },
  "workspace.proposal.no_proposal_yet": {
    en: "No proposal yet",
    zh: "尚未生成提案",
    ja: "提案はまだありません",
  },
  "workspace.proposal.no_proposal_yet_body": {
    en: "This run started without a proposal artifact. Continue from Sources, or generate a proposal before editing this tab.",
    zh: "本运行没有提案产物。可以继续查看文献步骤；如需编辑此页，请先生成提案。",
    ja: "この実行には提案成果物がありません。文献ステップを続行するか、このタブを編集する前に提案を生成してください。",
  },
  "workspace.proposal.research_question": {
    en: "Research question",
    zh: "研究问题",
    ja: "研究課題",
  },
  "workspace.proposal.significance": {
    en: "Significance",
    zh: "研究意义",
    ja: "意義",
  },
  "workspace.proposal.preliminary_approach": {
    en: "Preliminary approach",
    zh: "初步方法",
    ja: "初期アプローチ",
  },
  "workspace.proposal.expected_contribution": {
    en: "Expected contribution",
    zh: "预期贡献",
    ja: "期待される貢献",
  },
  "workspace.proposal.scope": { en: "Scope", zh: "研究范围", ja: "範囲" },
  "workspace.proposal.preliminary_keywords": {
    en: "Preliminary keywords",
    zh: "初步关键词",
    ja: "初期キーワード",
  },
  "workspace.proposal.remove_keyword_aria": {
    en: "Remove {keyword}",
    zh: "移除 {keyword}",
    ja: "{keyword} を削除",
  },
  "workspace.proposal.drafting_button": {
    en: "Drafting proposal...",
    zh: "正在生成提案…",
    ja: "提案を作成中…",
  },
  "workspace.proposal.generate_initial": {
    en: "Generate Initial Proposal",
    zh: "生成初始提案",
    ja: "初期提案を生成",
  },

  // workspace.sources.*
  "workspace.sources.heading": { en: "Sources", zh: "文献", ja: "文献" },
  "workspace.sources.disabled.running": {
    en: "Curator is currently running; please wait until it finishes.",
    zh: "文献筛选节点正在运行，请等待完成。",
    ja: "文献整理が実行中です。完了までお待ちください。",
  },
  "workspace.sources.disabled.waiting_scout": {
    en: "Curator becomes available after the scout step finishes.",
    zh: "需要先完成文献检索节点才能启动文献筛选。",
    ja: "文献検索完了後に文献整理が利用可能になります。",
  },
  "workspace.sources.disabled.upstream_pending": {
    en: "Curator becomes available after the proposal accept step.",
    zh: "需要先在「提案」节点点击「接受提案并启动文献检索」后才能进入此步骤。",
    ja: "「提案」ステップで承認後に文献整理が利用可能になります。",
  },
  "workspace.sources.disabled.already_done": {
    en: "Curator already completed; see history if you need to revise.",
    zh: "文献筛选节点已完成。如需修改，请到「状态与历史」查看。",
    ja: "文献整理は完了済みです。修正が必要な場合は「ステータスと履歴」を参照してください。",
  },
  "workspace.sources.disabled.blocked_phase": {
    en: "This run is blocked in {phase}; resolve or retry that phase before starting source curation.",
    zh: "当前运行阻塞在 {phase} 阶段。请先修复或重试该阶段，再启动文献整理。",
    ja: "このランは {phase} フェーズで停止しています。文献整理を始める前に、そのフェーズを修正または再試行してください。",
  },
  "workspace.sources.disabled.blocked_unknown": {
    en: "This run is blocked. Resolve the failure banner before starting source curation.",
    zh: "当前运行处于阻塞状态。请先处理失败提示，再启动文献整理。",
    ja: "このランは停止状態です。文献整理を始める前に失敗バナーを処理してください。",
  },
  "workspace.sources.scout_candidates_notice": {
    en: "These are Scout candidates sorted by source score. They have not been curated yet.",
    zh: "这些是 Scout 检索候选，已按来源分数排序，但尚未经过文献整理。",
    ja: "これは Scout の候補で、ソーススコア順に並んでいます。まだ文献整理は済んでいません。",
  },
  "workspace.sources.review.search_heading": {
    en: "Review Scout candidates",
    zh: "审核 Scout 候选",
    ja: "Scout 候補を確認",
  },
  "workspace.sources.review.deep_heading": {
    en: "Review curated shortlist",
    zh: "审核入选清单",
    ja: "選定リストを確認",
  },
  "workspace.sources.review.summary": {
    en: "{selected}/{total} selected, {rejected} rejected, {pinned} pinned, {pending} pending.",
    zh: "已选 {selected}/{total}，已拒绝 {rejected}，置顶 {pinned}，待定 {pending}。",
    ja: "{selected}/{total} 件を選択、{rejected} 件を却下、{pinned} 件をピン留め、{pending} 件が未決です。",
  },
  "workspace.sources.review.approve_all": {
    en: "Approve all",
    zh: "全部通过",
    ja: "すべて承認",
  },
  "workspace.sources.review.clear": {
    en: "Clear",
    zh: "清空",
    ja: "クリア",
  },
  "workspace.sources.review.reset": {
    en: "Reset",
    zh: "重置",
    ja: "リセット",
  },
  "workspace.sources.review.approved": {
    en: "Approve",
    zh: "通过",
    ja: "承認",
  },
  "workspace.sources.review.rejected": {
    en: "Reject",
    zh: "拒绝",
    ja: "却下",
  },
  "workspace.sources.review.pinned": {
    en: "Pin",
    zh: "置顶",
    ja: "ピン留め",
  },
  "workspace.sources.review.status_pending": {
    en: "Pending",
    zh: "待定",
    ja: "未決",
  },
  "workspace.sources.review.status_approved": {
    en: "Approved",
    zh: "已通过",
    ja: "承認済み",
  },
  "workspace.sources.review.status_rejected": {
    en: "Rejected",
    zh: "已拒绝",
    ja: "却下済み",
  },
  "workspace.sources.review.status_pinned": {
    en: "Pinned",
    zh: "已置顶",
    ja: "ピン留め済み",
  },
  "workspace.sources.review.select_before_curator": {
    en: "Approve or pin at least one Scout candidate before starting curation.",
    zh: "启动文献整理前，请至少通过或置顶一个 Scout 候选。",
    ja: "文献整理を開始する前に、少なくとも 1 件の Scout 候補を承認またはピン留めしてください。",
  },
  "workspace.sources.review.select_before_synthesizer": {
    en: "Approve or pin at least one shortlisted source before continuing to synthesis.",
    zh: "进入综合分析前，请至少通过或置顶一条入选文献。",
    ja: "統合に進む前に、少なくとも 1 件の選定文献を承認またはピン留めしてください。",
  },
  "workspace.sources.review.save_failed": {
    en: "Could not save the source review choice.",
    zh: "未能保存文献审核选择。",
    ja: "文献レビューの内容を保存できませんでした。",
  },
  "workspace.sources.review.open_search_review": {
    en: "Review search candidates",
    zh: "审核检索候选",
    ja: "検索候補を確認",
  },
  "workspace.sources.review.open_deep_review": {
    en: "Review shortlist",
    zh: "审核入选清单",
    ja: "選定リストを確認",
  },
  "workspace.sources.scout_running": {
    en: "Searching the literature, please wait…",
    zh: "正在检索文献，请稍候…",
    ja: "文献を検索中、しばらくお待ちください…",
  },
  "workspace.sources.curator_running": {
    en: "Curating sources, please wait…",
    zh: "正在整理文献，请稍候…",
    ja: "文献を整理中、しばらくお待ちください…",
  },
  "workspace.sources.upload_pdf": {
    en: "Upload PDF",
    zh: "上传 PDF",
    ja: "PDF をアップロード",
  },
  "workspace.sources.upload_pdf_for": {
    en: "Upload PDF for {title}",
    zh: "为 {title} 上传 PDF",
    ja: "{title} の PDF をアップロード",
  },
  "workspace.sources.advance_to_synthesizer": {
    en: "Confirm & continue to synthesis",
    zh: "确认并进入综合分析",
    ja: "確認して総合分析へ進む",
  },
  "workspace.sources.uploading": {
    en: "Uploading...",
    zh: "上传中…",
    ja: "アップロード中…",
  },
  "workspace.sources.upload": { en: "Upload", zh: "上传", ja: "アップロード" },
  "workspace.sources.aria_source_id": {
    en: "Source reference",
    zh: "文献编号",
    ja: "文献番号",
  },
  "workspace.sources.title_placeholder": {
    en: "Title",
    zh: "标题",
    ja: "タイトル",
  },
  "workspace.sources.authors_placeholder": {
    en: "Authors",
    zh: "作者",
    ja: "著者",
  },
  "workspace.sources.year_placeholder": {
    en: "Year",
    zh: "年份",
    ja: "発行年",
  },
  "workspace.sources.doi_placeholder": { en: "DOI", zh: "DOI", ja: "DOI" },
  "workspace.sources.url_placeholder": { en: "URL", zh: "URL", ja: "URL" },
  "workspace.sources.pdf_aria": {
    en: "PDF",
    zh: "PDF 文件",
    ja: "PDF ファイル",
  },
  "workspace.sources.tablist_label": {
    en: "Source lists",
    zh: "文献列表",
    ja: "文献リスト",
  },
  "workspace.sources.tab_shortlist": {
    en: "Shortlist",
    zh: "入选清单",
    ja: "選定リスト",
  },
  "workspace.sources.tab_manual": {
    en: "Manual-upload required",
    zh: "需要手动上传",
    ja: "手動アップロードが必要",
  },
  "workspace.sources.tab_skimmed": {
    en: "Skimmed",
    zh: "已速览",
    ja: "概観済み",
  },
  "workspace.sources.no_sources": {
    en: "No sources yet.",
    zh: "尚无文献。",
    ja: "まだ文献はない。",
  },
  "workspace.sources.empty_shortlist_search_review": {
    en: "No shortlist yet. Scout candidates are on the Skimmed tab; start curation to build the shortlist.",
    zh: "尚无入选清单。Scout 候选在「已速览」标签中；启动文献整理后才会生成入选清单。",
    ja: "選定リストはまだありません。Scout 候補は「概観済み」タブにあります。文献整理を開始すると選定リストが作成されます。",
  },
  "workspace.sources.empty_shortlist_has_skimmed": {
    en: "No shortlist rows here. Open Skimmed to inspect the Scout candidates.",
    zh: "入选清单暂无条目。请打开「已速览」查看 Scout 候选。",
    ja: "選定リストに項目はありません。「概観済み」を開いて Scout 候補を確認してください。",
  },
  "workspace.sources.empty_skimmed_search_review": {
    en: "No Scout candidates are available yet. Wait for Scout to finish or rerun it from history.",
    zh: "尚无 Scout 候选。请等待文献检索完成，或在「状态与历史」中重新运行 Scout。",
    ja: "Scout 候補はまだありません。文献検索の完了を待つか、履歴から Scout を再実行してください。",
  },
  "workspace.sources.empty_skimmed": {
    en: "No skimmed candidates.",
    zh: "暂无已速览候选。",
    ja: "概観済み候補はありません。",
  },
  "workspace.sources.no_manual": {
    en: "No manual uploads required.",
    zh: "无需手动上传。",
    ja: "手動アップロードは不要。",
  },
  "workspace.sources.curator_report": {
    en: "Curation report",
    zh: "文献筛选报告",
    ja: "選定レポート",
  },
  "workspace.sources.quality.off_topic_dropped": {
    en: "Off-topic dropped",
    zh: "已丢弃跑题项",
    ja: "対象外として除外",
  },
  "workspace.sources.quality.verification_rejected": {
    en: "Verification rejected",
    zh: "验证拒绝",
    ja: "検証で除外",
  },
  "workspace.sources.quality.runner_up": {
    en: "Runner-up",
    zh: "候补未入选",
    ja: "次点",
  },
  "workspace.sources.quality.weak_anchor": {
    en: "Weak anchor",
    zh: "弱锚定",
    ja: "弱いアンカー",
  },
  "workspace.sources.quality.weak_anchor_badge": {
    en: "Weak topic anchor",
    zh: "主题锚定较弱",
    ja: "トピックのアンカーが弱い",
  },
  "workspace.sources.unknown_authors": {
    en: "Unknown authors",
    zh: "未知作者",
    ja: "著者不明",
  },
  "workspace.sources.no_date": { en: "n.d.", zh: "无日期", ja: "日付不明" },
  "workspace.sources.unknown_venue": {
    en: "Unknown venue",
    zh: "未知出处",
    ja: "掲載先不明",
  },

  // workspace.synthesis.*
  "workspace.synthesis.heading": { en: "Synthesis", zh: "综述", ja: "統合" },
  "workspace.synthesis.advance_to_lens": {
    en: "Confirm & continue to framework lens",
    zh: "确认并进入理论镜框",
    ja: "確認して理論的レンズへ進む",
  },
  "workspace.synthesis.skip_lens_to_novelty": {
    en: "Skip lens & continue to novelty",
    zh: "跳过镜框直接进入新颖性",
    ja: "レンズをスキップして新規性へ進む",
  },
  "workspace.synthesis.progress_heading": {
    en: "Synthesis progress",
    zh: "综合进度",
    ja: "合成の進捗",
  },
  "workspace.synthesis.disabled.running": {
    en: "Synthesizer is currently running; please wait until it finishes.",
    zh: "综合节点正在运行，请等待完成。",
    ja: "統合フェーズが実行中です。完了までお待ちください。",
  },
  "workspace.synthesis.disabled.waiting_curator": {
    en: "Synthesizer becomes available after the curator step finishes.",
    zh: "需要先完成文献筛选节点才能启动综合。",
    ja: "文献整理ステップ完了後に統合が利用可能になります。",
  },
  "workspace.synthesis.disabled.deep_dive_review_required": {
    en: "Review the shortlisted sources in the Sources tab before starting synthesis.",
    zh: "请先在「文献」标签审核入选文献，再启动综合。",
    ja: "統合を開始する前に、「文献」タブで候補文献を確認してください。",
  },
  "workspace.synthesis.disabled.upstream_pending": {
    en: "Synthesizer becomes available after the previous steps are complete.",
    zh: "需要先完成前置节点（提案 / 文献检索 / 文献筛选）才能启动综合。",
    ja: "上流ステップ完了後に統合が利用可能になります。",
  },
  "workspace.synthesis.disabled.already_done": {
    en: "Synthesizer already completed; see history if you need to revise.",
    zh: "综合节点已完成。如需修改，请到「状态与历史」查看。",
    ja: "統合は完了済みです。修正が必要な場合は「ステータスと履歴」を参照してください。",
  },
  "workspace.synthesis.no_progress": {
    en: "Synthesizing claims from sources, waiting for progress updates…",
    zh: "正在从文献中综合论点，等待进度更新…",
    ja: "文献から論点を統合中、進捗の更新を待機中…",
  },
  "workspace.synthesis.report_heading": {
    en: "Synthesis report",
    zh: "综合报告",
    ja: "合成レポート",
  },
  "workspace.synthesis.claims_heading": {
    en: "Claims",
    zh: "论断",
    ja: "主張",
  },
  "workspace.synthesis.no_claims": {
    en: "No claims yet.",
    zh: "尚无论断。",
    ja: "まだ主張はない。",
  },
  "workspace.synthesis.diagnostic_heading": {
    en: "Material diagnostic",
    zh: "资料诊断",
    ja: "資料診断",
  },
  "workspace.synthesis.diagnostic_pending": {
    en: "Material diagnostic not generated yet.",
    zh: "资料诊断尚未生成。",
    ja: "資料診断はまだ生成されていない。",
  },
  "workspace.synthesis.diagnostic_sufficient": {
    en: "Material is sufficient",
    zh: "资料是否充分",
    ja: "資料は十分か",
  },
  "workspace.synthesis.diagnostic_action": {
    en: "Recommended next step",
    zh: "建议下一步",
    ja: "推奨される次の手",
  },
  "workspace.synthesis.diagnostic_candidate_titles": {
    en: "Feasible research questions / titles",
    zh: "可行的研究问题 / 题名",
    ja: "実行可能な研究課題 / 題名",
  },
  "workspace.synthesis.diagnostic_missing": {
    en: "Missing materials",
    zh: "尚缺资料",
    ja: "不足している資料",
  },
  "workspace.synthesis.diagnostic_risks": {
    en: "Risks",
    zh: "风险提示",
    ja: "リスク",
  },
  "workspace.synthesis.diagnostic_rationale": {
    en: "Rationale",
    zh: "判断理由",
    ja: "判断理由",
  },
  "workspace.synthesis.diagnostic_yes": { en: "Yes", zh: "是", ja: "はい" },
  "workspace.synthesis.diagnostic_no": { en: "No", zh: "否", ja: "いいえ" },
  "workspace.synthesis.diagnostic_action_proceed": {
    en: "Proceed to outline",
    zh: "可继续生成大纲",
    ja: "アウトラインへ進む",
  },
  "workspace.synthesis.diagnostic_action_iterate": {
    en: "Iterate on the topic or add sources before continuing",
    zh: "建议先调整选题或补充资料",
    ja: "テーマ調整または資料補充を推奨",
  },
  "workspace.synthesis.diagnostic_action_incomplete": {
    en: "Diagnostic incomplete",
    zh: "诊断未完成",
    ja: "診断未完了",
  },
  "workspace.synthesis.diagnostic_none": {
    en: "(none)",
    zh: "（无）",
    ja: "（なし）",
  },
  "workspace.novelty.outline_heading": {
    en: "Detailed outline (locks the draft step)",
    zh: "详细大纲（用于锁定草稿步骤）",
    ja: "詳細アウトライン（草稿ステップを固定）",
  },
  "workspace.novelty.outline_pending": {
    en: "No detailed outline for this angle yet.",
    zh: "尚未为该角度生成详细大纲。",
    ja: "この角度の詳細アウトラインはまだない。",
  },
  "workspace.novelty.outline_function": {
    en: "Function",
    zh: "本节作用",
    ja: "節の役割",
  },
  "workspace.novelty.outline_argument": {
    en: "Argument",
    zh: "核心论点",
    ja: "中心論点",
  },
  "workspace.novelty.outline_literature": {
    en: "Literature",
    zh: "文献使用",
    ja: "文献の使い方",
  },
  "workspace.novelty.outline_materials": {
    en: "Materials",
    zh: "材料",
    ja: "資料",
  },
  "workspace.novelty.outline_relation": {
    en: "Relation to thesis",
    zh: "与中心论点关系",
    ja: "中心命題との関係",
  },
  "workspace.novelty.outline_weakness": {
    en: "Weakness",
    zh: "潜在弱点",
    ja: "弱点",
  },
  "workspace.novelty.outline_empty_field": {
    en: "(to be filled)",
    zh: "（待补）",
    ja: "（未記入）",
  },

  // workspace.novelty.*
  "workspace.novelty.heading": { en: "Novelty", zh: "新颖性", ja: "新規性" },
  "workspace.novelty.starting_ideator": {
    en: "Starting novelty analysis…",
    zh: "正在启动新颖性分析…",
    ja: "新規性分析を起動中…",
  },
  "workspace.novelty.run_ideator": {
    en: "Run novelty analysis",
    zh: "启动新颖性分析",
    ja: "新規性分析を実行",
  },
  "workspace.novelty.accepting": {
    en: "Accepting...",
    zh: "正在确认…",
    ja: "承認中…",
  },
  "workspace.novelty.accept_current": {
    en: "Accept current cards",
    zh: "接受当前卡片",
    ja: "現在のカードを承認",
  },
  "workspace.novelty.generating": {
    en: "Angle cards are being generated.",
    zh: "正在生成角度卡片。",
    ja: "アングルカードを生成中。",
  },
  "workspace.novelty.collapse": {
    en: "Collapse",
    zh: "收起",
    ja: "折りたたむ",
  },
  "workspace.novelty.expand": { en: "Expand", zh: "展开", ja: "展開" },
  "workspace.novelty.why_novel": {
    en: "Why novel",
    zh: "新颖之处",
    ja: "新規性の理由",
  },
  "workspace.novelty.evidence_so_far": {
    en: "Evidence so far",
    zh: "现有证据",
    ja: "現時点の根拠",
  },
  "workspace.novelty.missing_evidence": {
    en: "Missing evidence",
    zh: "缺失证据",
    ja: "不足している根拠",
  },
  "workspace.novelty.journal_fit": {
    en: "Journal fit",
    zh: "期刊契合度",
    ja: "ジャーナル適合度",
  },
  "workspace.novelty.key_claims": {
    en: "Key claims",
    zh: "关键论断",
    ja: "主要な主張",
  },
  "workspace.novelty.risks": { en: "Risks", zh: "风险", ja: "リスク" },
  "workspace.novelty.framework_lens": {
    en: "Framework lens",
    zh: "框架镜框",
    ja: "理論的レンズ",
  },
  "workspace.novelty.methodological_choice": {
    en: "Methodological choice",
    zh: "方法选择",
    ja: "方法論",
  },
  "workspace.novelty.current_choice": {
    en: "Current choice",
    zh: "当前选择",
    ja: "現在の選択",
  },
  "workspace.novelty.make_current": {
    en: "Make current choice",
    zh: "设为当前选择",
    ja: "現在の選択にする",
  },
  "workspace.novelty.selecting": {
    en: "Selecting...",
    zh: "正在选择…",
    ja: "選択中…",
  },
  "workspace.novelty.select_angle": {
    en: "Select this angle",
    zh: "选择此角度",
    ja: "このアングルを選択",
  },
  "workspace.novelty.selected_for_drafting": {
    en: "Selected for drafting.",
    zh: "已选定用于撰稿。",
    ja: "草稿執筆用に選択済み。",
  },
  "workspace.novelty.no_cards": {
    en: "No angle cards yet.",
    zh: "尚无角度卡片。",
    ja: "まだアングルカードはない。",
  },
  "workspace.novelty.ideator_report": {
    en: "Novelty analysis report",
    zh: "新颖性分析报告",
    ja: "新規性分析レポート",
  },
  "workspace.novelty.discussion_heading": {
    en: "Discussion",
    zh: "讨论",
    ja: "ディスカッション",
  },
  "workspace.novelty.no_discussion": {
    en: "No discussion yet.",
    zh: "尚无讨论。",
    ja: "まだ議論はない。",
  },
  "workspace.novelty.regenerating_cards": {
    en: "Regenerating cards...",
    zh: "正在重新生成卡片…",
    ja: "カードを再生成中…",
  },
  "workspace.novelty.submitting": {
    en: "Submitting...",
    zh: "提交中…",
    ja: "送信中…",
  },
  "workspace.novelty.submit": { en: "Submit", zh: "提交", ja: "送信" },
  "workspace.novelty.disabled.running": {
    en: "Ideator is running — wait until it finishes.",
    zh: "Ideator 正在运行，请等待完成后再操作。",
    ja: "Ideator が実行中です。完了までお待ちください。",
  },
  "workspace.novelty.disabled.upstream_pending": {
    en: "Upstream phases are not complete yet.",
    zh: "上游节点尚未完成，无法启动。",
    ja: "上流フェーズがまだ完了していません。",
  },
  "workspace.novelty.disabled.already_done": {
    en: "Ideator has already produced angle cards for this run.",
    zh: "本次运行的角度卡片已经生成。",
    ja: "この実行ではすでに角度カードが生成されています。",
  },
  "workspace.novelty.accept_disabled.running": {
    en: "Ideator is running — angle cards will appear when it finishes.",
    zh: "Ideator 正在运行，完成后才能选定角度。",
    ja: "Ideator 実行中です。完了後に角度を選択できます。",
  },
  "workspace.novelty.accept_disabled.waiting_ideator": {
    en: "Run Ideator first to generate angle cards.",
    zh: "请先运行 Ideator 生成角度卡片。",
    ja: "まず Ideator を実行して角度カードを生成してください。",
  },
  "workspace.novelty.accept_disabled.upstream_pending": {
    en: "Angle selection unlocks once Ideator has produced cards.",
    zh: "角度卡片生成后才能选定。",
    ja: "Ideator が角度カードを生成した後に選択できます。",
  },

  // workspace.running_banner.* — shared banner shown at the top of
  // each subview while its phase is mid-run. Title varies per phase
  // (so users see "正在生成草稿…" not just "运行中…"); the
  // step / count / starting / finalizing strings are shared so all
  // phases pulse with the same UX.
  "workspace.running_banner.starting": {
    en: "Starting up…",
    zh: "正在启动…",
    ja: "起動中…",
  },
  "workspace.running_banner.finalizing": {
    en: "Wrapping up the final pass…",
    zh: "正在收尾整合…",
    ja: "最終仕上げ中…",
  },
  "workspace.running_banner.in_progress_step": {
    en: "Currently working on step {step} / {total}…",
    zh: "正在处理第 {step} / {total} 步…",
    ja: "現在 {step} / {total} 番目を処理中…",
  },
  "workspace.running_banner.completed_count": {
    en: "Completed {completed} / {total}",
    zh: "已完成 {completed} / {total}",
    ja: "完了 {completed} / {total}",
  },
  "workspace.running_banner.proposal.title": {
    en: "Drafting your proposal…",
    zh: "正在生成提案…",
    ja: "提案を作成中…",
  },
  "workspace.running_banner.scout.title": {
    en: "Scouting literature for your topic…",
    zh: "正在检索相关文献…",
    ja: "関連文献を検索中…",
  },
  "workspace.running_banner.curator.title": {
    en: "Curating sources from search results…",
    zh: "正在整理筛选文献…",
    ja: "文献を整理中…",
  },
  "workspace.running_banner.synthesizer.title": {
    en: "Synthesizing claims from sources…",
    zh: "正在从文献中综合论点…",
    ja: "文献から論点を統合中…",
  },
  "workspace.running_banner.tension_extraction.title": {
    en: "Extracting tensions across the literature…",
    zh: "正在提取文献中的张力…",
    ja: "文献間の対立点を抽出中…",
  },
  "workspace.running_banner.framework_lens.title": {
    en: "Generating the framework lens…",
    zh: "正在生成理论镜框…",
    ja: "フレームワーク分析を生成中…",
  },
  "workspace.running_banner.ideator.title": {
    en: "Generating novelty angles…",
    zh: "正在生成新颖角度…",
    ja: "新規性の角度を生成中…",
  },
  "workspace.running_banner.drafter.title": {
    en: "Drafting your paper…",
    zh: "正在撰写草稿…",
    ja: "原稿を執筆中…",
  },
  "workspace.running_banner.stylist.title": {
    en: "Polishing the draft…",
    zh: "正在润色文风…",
    ja: "文体を整え中…",
  },
  "workspace.running_banner.final_rewrite.title": {
    en: "Final rewrite for compliance…",
    zh: "正在最终改写以满足合规要求…",
    ja: "コンプライアンス対応の最終書き直し中…",
  },
  "workspace.running_banner.critic.title": {
    en: "Auditing claims and citations…",
    zh: "正在审查论点与引用…",
    ja: "主張と引用を監査中…",
  },
  "workspace.running_banner.integrity.title": {
    en: "Running integrity scans…",
    zh: "正在运行学术规范扫描…",
    ja: "整合性スキャンを実行中…",
  },
  "workspace.running_banner.exports.title": {
    en: "Generating downloadable formats…",
    zh: "正在生成可下载文件…",
    ja: "ダウンロード可能な形式を生成中…",
  },

  // workspace.draft.*
  "workspace.draft.heading": { en: "Draft", zh: "草稿", ja: "草稿" },
  "workspace.draft.progress_heading": {
    en: "Draft progress",
    zh: "草稿进度",
    ja: "草稿の進捗",
  },
  "workspace.draft.no_progress": {
    en: "Drafting sections, waiting for progress updates…",
    zh: "正在撰写章节，等待进度更新…",
    ja: "セクションを執筆中、進捗の更新を待機中…",
  },
  "workspace.draft.versions_label": {
    en: "Versions",
    zh: "版本",
    ja: "バージョン",
  },
  "workspace.draft.uncited_claims_suffix": {
    en: "uncited claims",
    zh: "条未引用论断",
    ja: "件の未引用主張",
  },
  "workspace.draft.download_bibtex": {
    en: "Download BibTeX",
    zh: "下载 BibTeX",
    ja: "BibTeX をダウンロード",
  },
  "workspace.draft.artifact_pending": {
    en: "Draft artifact pending.",
    zh: "草稿产物待生成。",
    ja: "草稿成果物の生成待ち。",
  },
  "workspace.draft.claim_map_heading": {
    en: "Claim map",
    zh: "论断映射",
    ja: "主張マップ",
  },
  "workspace.draft.no_claim_map": {
    en: "No claim map entries.",
    zh: "尚无论断映射条目。",
    ja: "主張マップのエントリはない。",
  },
  "workspace.draft.uncited_tag": {
    en: "[UNCITED]",
    zh: "[未引用]",
    ja: "[未引用]",
  },

  // workspace.style.*
  "workspace.style.heading": { en: "Style", zh: "文风", ja: "文体" },
  "workspace.style.mathematical_mode.label": {
    en: "Mathematical-strength mode",
    zh: "数理增强模式",
    ja: "数理強化モード",
  },
  "workspace.style.mathematical_mode.tooltip": {
    en: "Run a heavyweight holistic rewrite for round-0 — can introduce LaTeX formulas, tables, and 【TODO】 placeholders. Adds ~20-30 min and ~10x token cost.",
    zh: "做高强度整体战略改稿，可建议 LaTeX 公式、表格、待填占位；预计 +20-30 分钟，token 成本约 10x。",
    ja: "大規模な総合書き換えを行い、LaTeX 数式・表・【未記入】を補強。所要時間 +20-30 分、トークン費用は約 10 倍。",
  },
  "workspace.style.mathematical_mode.locked": {
    en: "Locked while rewriter or critic is running. Wait until the phase finishes, then toggle.",
    zh: "改稿器或审稿器正在运行，无法切换。等当前阶段结束后再勾选。",
    ja: "リライターまたは批評が実行中のため変更できません。フェーズ終了後に再度切り替えてください。",
  },
  "workspace.style.auto_advance.label": {
    en: "One-click auto-pilot",
    zh: "一键全自动",
    ja: "ワンクリック自動運転",
  },
  "workspace.style.auto_advance.tooltip": {
    en: "Toggle on to auto-advance every USER_*_REVIEW gate from now until EXPORTS_DONE. Failures still pause for user.",
    zh: "勾选后所有 USER_*_REVIEW 检查点自动通过，直到 EXPORTS_DONE。失败状态仍会停下来。",
    ja: "ON にすると以降の USER_*_REVIEW を自動承認し EXPORTS_DONE まで進行。失敗時のみ停止。",
  },
  "workspace.style.advance_to_critic": {
    en: "Confirm & continue to review",
    zh: "确认并进入审稿",
    ja: "確認して批評へ進む",
  },
  "workspace.style.starting_stylist": {
    en: "Starting style polish…",
    zh: "正在启动文风润色…",
    ja: "文体調整を起動中…",
  },
  "workspace.style.rerun_stylist": {
    en: "Re-run style polish",
    zh: "重新运行文风润色",
    ja: "文体調整を再実行",
  },
  "workspace.style.run_stylist": {
    en: "Run style polish",
    zh: "启动文风润色",
    ja: "文体調整を実行",
  },
  "workspace.style.waiting_for_drafter": {
    en: "Waiting for the draft to finish before style polish can run.",
    zh: "草稿仍在生成中，文风润色将在完整稿件就绪后启用。",
    ja: "草稿の生成完了を待っています。完成後に文体調整が利用可能になります。",
  },
  "workspace.style.disabled.running": {
    en: "Stylist is running — wait until it finishes.",
    zh: "文风润色正在运行，请等待完成。",
    ja: "文体調整が実行中です。完了までお待ちください。",
  },
  "workspace.style.disabled.upstream_pending": {
    en: "Upstream phases are not complete yet.",
    zh: "上游节点尚未完成，无法启动文风润色。",
    ja: "上流フェーズがまだ完了していません。",
  },
  "workspace.style.disabled.already_done": {
    en: "Stylist has already produced revisions for this run.",
    zh: "本次运行的文风润色已经完成。",
    ja: "この実行ではすでに文体調整が完了しています。",
  },
  "workspace.style.progress_heading": {
    en: "Style progress",
    zh: "文风进度",
    ja: "文体の進捗",
  },
  "workspace.style.no_progress": {
    en: "Polishing style, waiting for progress updates…",
    zh: "正在润色文风，等待进度更新…",
    ja: "文体を調整中、進捗の更新を待機中…",
  },
  "workspace.style.tablist_label": {
    en: "Style artifacts",
    zh: "文风产物",
    ja: "文体成果物",
  },
  "workspace.style.tab_manuscript": {
    en: "Manuscript",
    zh: "稿件",
    ja: "原稿",
  },
  "workspace.style.tab_diff": { en: "Diff", zh: "差异", ja: "差分" },
  "workspace.style.tab_score": { en: "Score", zh: "评分", ja: "スコア" },
  "workspace.style.diff_pending": {
    en: "Diff pending.",
    zh: "差异待生成。",
    ja: "差分の生成待ち。",
  },
  "workspace.style.artifacts_pending": {
    en: "Style artifacts pending.",
    zh: "文风产物待生成。",
    ja: "文体成果物の生成待ち。",
  },
  "workspace.style.score_total": {
    en: "Total {total} / 50",
    zh: "总分 {total} / 50",
    ja: "合計 {total} / 50",
  },
  "workspace.style.score_initial_suffix": {
    en: "(initial {initial})",
    zh: "（初始 {initial}）",
    ja: "（初期 {initial}）",
  },
  "workspace.style.repolish_attempted": {
    en: "One re-polish pass was attempted.",
    zh: "已尝试一次重抛光。",
    ja: "再ポリッシュを 1 回実施した。",
  },
  "workspace.style.dim.directness": {
    en: "directness",
    zh: "直接性",
    ja: "直接性",
  },
  "workspace.style.dim.rhythm": {
    en: "rhythm",
    zh: "节奏",
    ja: "リズム",
  },
  "workspace.style.dim.trust": { en: "trust", zh: "可信度", ja: "信頼性" },
  "workspace.style.dim.authenticity": {
    en: "authenticity",
    zh: "真实感",
    ja: "真正性",
  },
  "workspace.style.dim.density": {
    en: "density",
    zh: "信息密度",
    ja: "密度",
  },

  // workspace.review.*
  "workspace.review.heading": { en: "Review", zh: "评审", ja: "査読" },
  "workspace.review.starting_critic": {
    en: "Starting review…",
    zh: "正在启动评审…",
    ja: "レビューを起動中…",
  },
  "workspace.review.run_critic": {
    en: "Run review",
    zh: "启动评审",
    ja: "レビューを実行",
  },
  "workspace.review.audit_running": {
    en: "Review audit is running.",
    zh: "评审进行中。",
    ja: "レビューを実行中。",
  },
  "workspace.review.disabled.running": {
    en: "Review is currently running; please wait until it finishes.",
    zh: "评审节点正在运行，请等待完成。",
    ja: "レビューが実行中です。完了までお待ちください。",
  },
  "workspace.review.disabled.waiting_stylist": {
    en: "Review can run after style polish has produced its output.",
    zh: "需要先完成文风节点才能启动评审。",
    ja: "文体調整の出力が完成した後でレビューを実行できます。",
  },
  "workspace.review.disabled.upstream_pending": {
    en: "Review becomes available after the previous steps are complete.",
    zh: "需要先完成前置节点（综述 / 新颖性 / 草稿 / 文风）后才能启动评审。",
    ja: "上流ステップ完了後にレビューが利用可能になります。",
  },
  "workspace.review.disabled.already_done": {
    en: "Review already passed; see history if you need to revise.",
    zh: "评审节点已完成。如需修改，请到「状态与历史」查看。",
    ja: "レビューは完了済みです。修正が必要な場合は「ステータスと履歴」を参照してください。",
  },
  "workspace.review.blocking_issues": {
    en: "Blocking issues",
    zh: "阻塞问题",
    ja: "ブロッキング問題",
  },
  "workspace.review.no_paragraph": {
    en: "No paragraph",
    zh: "无段落",
    ja: "段落なし",
  },
  "workspace.review.no_blockers": {
    en: "No blocking review issues recorded.",
    zh: "未记录阻塞性评审问题。",
    ja: "ブロッキングのレビュー問題は記録されていない。",
  },
  "workspace.review.citation_audit": {
    en: "Citation audit",
    zh: "引用审核",
    ja: "引用監査",
  },
  "workspace.review.citation_audit_aria": {
    en: "Citation audit",
    zh: "引用审核",
    ja: "引用監査",
  },
  "workspace.review.col_paragraph": {
    en: "Paragraph",
    zh: "段落",
    ja: "段落",
  },
  "workspace.review.col_status": { en: "Status", zh: "状态", ja: "状態" },
  "workspace.review.col_sources": { en: "Sources", zh: "来源", ja: "出典" },
  "workspace.review.col_claim": { en: "Claim", zh: "论断", ja: "主張" },
  "workspace.review.citation_audit_pending": {
    en: "Citation audit pending.",
    zh: "引用审核待生成。",
    ja: "引用監査の生成待ち。",
  },
  "workspace.review.external_scan_approval": {
    en: "External scan approval",
    zh: "外部扫描审批",
    ja: "外部スキャンの承認",
  },
  "workspace.review.plagiarism": {
    en: "Plagiarism",
    zh: "抄袭检测",
    ja: "剽窃検査",
  },
  "workspace.review.ai_style": {
    en: "AI-style",
    zh: "AI 文风检测",
    ja: "AI 文体検査",
  },
  "workspace.review.starting_integrity": {
    en: "Starting Integrity...",
    zh: "正在启动 Integrity…",
    ja: "Integrity を起動中…",
  },
  "workspace.review.approve_and_scan": {
    en: "Approve and scan",
    zh: "批准并扫描",
    ja: "承認してスキャン",
  },
  "workspace.review.skip_note": {
    en: "Skip note",
    zh: "跳过说明",
    ja: "スキップ理由",
  },
  "workspace.review.skip_placeholder": {
    en: "Reason for skipping external scan",
    zh: "跳过外部扫描的原因",
    ja: "外部スキャンをスキップする理由",
  },
  "workspace.review.skip_with_note": {
    en: "Skip external scan with note",
    zh: "附带说明跳过外部扫描",
    ja: "理由を添えて外部スキャンをスキップ",
  },
  "workspace.review.revision_plan": {
    en: "Revision plan",
    zh: "修改计划",
    ja: "改訂計画",
  },
  "workspace.review.revision_plan_pending": {
    en: "Revision plan pending.",
    zh: "修改计划待生成。",
    ja: "改訂計画の生成待ち。",
  },
  "workspace.review.critic_report": {
    en: "Review report",
    zh: "评审报告",
    ja: "レビューレポート",
  },
  "workspace.review.critic_report_pending": {
    en: "Review report pending.",
    zh: "评审报告待生成。",
    ja: "レビューレポートの生成待ち。",
  },

  // workspace.integrity.*
  "workspace.integrity.heading": {
    en: "Integrity",
    zh: "检测",
    ja: "検査",
  },
  "workspace.integrity.accept_findings": {
    en: "Accept findings",
    zh: "接受检测结果",
    ja: "結果を承認",
  },
  "workspace.integrity.revise_selected": {
    en: "Revise selected",
    zh: "修改所选",
    ja: "選択分を改訂",
  },
  "workspace.integrity.scans_running": {
    en: "Vendor scans are running.",
    zh: "外部扫描进行中。",
    ja: "ベンダースキャン実行中。",
  },
  "workspace.integrity.disabled.running": {
    en: "Integrity scans are running; please wait until they finish.",
    zh: "完整性检测正在运行，请等待完成。",
    ja: "完全性スキャンが実行中です。完了までお待ちください。",
  },
  "workspace.integrity.disabled.waiting_scans": {
    en: "Integrity decisions become available once vendor scans finish.",
    zh: "需要等待外部扫描完成后才能在此页做接受 / 修订决定。",
    ja: "外部スキャン完了後に判断が可能になります。",
  },
  "workspace.integrity.disabled.upstream_pending": {
    en: "Integrity becomes available after the previous steps are complete.",
    zh: "需要先完成前置节点（评审 / 外部扫描审批）才能进行完整性检测。",
    ja: "上流ステップ完了後に完全性検査が利用可能になります。",
  },
  "workspace.integrity.disabled.already_done": {
    en: "Integrity already accepted; see history if you need to revise.",
    zh: "完整性节点已确认。如需修改，请到「状态与历史」查看。",
    ja: "完全性は確認済みです。修正が必要な場合は「ステータスと履歴」を参照してください。",
  },
  "workspace.integrity.vendor_pending": {
    en: "vendor pending",
    zh: "供应商待定",
    ja: "ベンダー保留中",
  },
  "workspace.integrity.score_na": {
    en: "score n/a",
    zh: "评分 n/a",
    ja: "スコア n/a",
  },
  "workspace.integrity.score_label": {
    en: "score {score}",
    zh: "评分 {score}",
    ja: "スコア {score}",
  },
  "workspace.integrity.spans_suffix": {
    en: "spans",
    zh: "处片段",
    ja: "件のスパン",
  },
  "workspace.integrity.span_decisions": {
    en: "Span decisions",
    zh: "片段决策",
    ja: "スパンの判断",
  },
  "workspace.integrity.revision_dimension": {
    en: "Revision dimension",
    zh: "修改维度",
    ja: "改訂の観点",
  },
  "workspace.integrity.dim_thesis": { en: "Thesis", zh: "论点", ja: "テーゼ" },
  "workspace.integrity.dim_structure": {
    en: "Structure",
    zh: "结构",
    ja: "構成",
  },
  "workspace.integrity.dim_evidence": {
    en: "Evidence",
    zh: "证据",
    ja: "根拠",
  },
  "workspace.integrity.dim_prose": {
    en: "Prose",
    zh: "文字表达",
    ja: "文章",
  },
  "workspace.integrity.confidence_na": {
    en: "confidence n/a",
    zh: "置信度 n/a",
    ja: "信頼度 n/a",
  },
  "workspace.integrity.decision_accept": {
    en: "Accept",
    zh: "接受",
    ja: "承認",
  },
  "workspace.integrity.decision_revise": {
    en: "Revise",
    zh: "修改",
    ja: "改訂",
  },
  "workspace.integrity.decision_ignore": {
    en: "Ignore",
    zh: "忽略",
    ja: "無視",
  },
  "workspace.integrity.manuscript_highlights": {
    en: "Manuscript highlights",
    zh: "稿件高亮",
    ja: "原稿のハイライト",
  },
  "workspace.integrity.no_spans": {
    en: "No integrity spans returned yet.",
    zh: "尚未返回检测片段。",
    ja: "検査スパンはまだ返却されていない。",
  },
  "workspace.integrity.plagiarism_report": {
    en: "Plagiarism report",
    zh: "抄袭检测报告",
    ja: "剽窃検査レポート",
  },
  "workspace.integrity.plagiarism_pending": {
    en: "Plagiarism report pending.",
    zh: "抄袭检测报告待生成。",
    ja: "剽窃検査レポートの生成待ち。",
  },
  "workspace.integrity.ai_style_report": {
    en: "AI-style report",
    zh: "AI 文风检测报告",
    ja: "AI 文体検査レポート",
  },
  "workspace.integrity.ai_pending": {
    en: "AI report pending.",
    zh: "AI 检测报告待生成。",
    ja: "AI 検査レポートの生成待ち。",
  },

  // workspace.export.*
  "workspace.export.heading": {
    en: "Export",
    zh: "导出",
    ja: "エクスポート",
  },
  "workspace.export.starting": {
    en: "Starting exports...",
    zh: "正在启动导出…",
    ja: "エクスポートを起動中…",
  },
  "workspace.export.accept_and_export": {
    en: "Accept final draft and export",
    zh: "接受终稿并导出",
    ja: "最終稿を承認してエクスポート",
  },
  "workspace.export.run_exports": {
    en: "Run exports",
    zh: "运行导出",
    ja: "エクスポートを実行",
  },
  "workspace.export.disabled.running": {
    en: "Exports are running; please wait until they finish.",
    zh: "导出正在运行，请等待完成。",
    ja: "エクスポートが実行中です。完了までお待ちください。",
  },
  "workspace.export.disabled.upstream_pending": {
    en: "Exports become available after the previous steps are complete.",
    zh: "需要先完成前置节点（评审 / 完整性检测 / 最终确认）才能导出。",
    ja: "上流ステップ完了後にエクスポートが利用可能になります。",
  },
  "workspace.export.disabled.pick_format": {
    en: "Choose at least one export format.",
    zh: "请至少选择一种导出格式。",
    ja: "少なくとも 1 つのエクスポート形式を選択してください。",
  },
  "workspace.export.disabled.already_done": {
    en: "Exports already produced; download files below.",
    zh: "导出已完成。下方可下载文件。",
    ja: "エクスポート済みです。下のファイルからダウンロードできます。",
  },
  "workspace.export.generating": {
    en: "Exports are being generated.",
    zh: "正在生成导出文件。",
    ja: "エクスポートを生成中。",
  },
  "workspace.export.files_heading": {
    en: "Files",
    zh: "文件",
    ja: "ファイル",
  },
  "workspace.export.no_exports": {
    en: "No exports available yet.",
    zh: "尚无可用的导出文件。",
    ja: "利用可能なエクスポートはまだない。",
  },
  "workspace.export.manifest": {
    en: "Manifest",
    zh: "清单",
    ja: "マニフェスト",
  },
  "workspace.export.manifest_pending": {
    en: "Manifest pending.",
    zh: "清单待生成。",
    ja: "マニフェストの生成待ち。",
  },

  // workspace.errors.*
  "workspace.errors.run_fetch": {
    en: "Run fetch failed",
    zh: "获取运行信息失败",
    ja: "実行情報の取得に失敗",
  },
  "workspace.errors.discovery_fetch": {
    en: "Discovery fetch failed",
    zh: "获取检索结果失败",
    ja: "探索結果の取得に失敗",
  },
  "workspace.errors.sources_fetch": {
    en: "Sources fetch failed",
    zh: "获取文献失败",
    ja: "文献の取得に失敗",
  },
  "workspace.errors.synthesis_fetch": {
    en: "Synthesis fetch failed",
    zh: "获取综述失败",
    ja: "統合結果の取得に失敗",
  },
  "workspace.errors.novelty_fetch": {
    en: "Novelty fetch failed",
    zh: "获取新颖性数据失败",
    ja: "新規性データの取得に失敗",
  },
  "workspace.errors.draft_fetch": {
    en: "Draft fetch failed",
    zh: "获取草稿失败",
    ja: "草稿の取得に失敗",
  },
  "workspace.errors.proposal_start": {
    en: "Proposal start failed",
    zh: "启动提案失败",
    ja: "提案の起動に失敗",
  },
  "workspace.errors.proposal_save": {
    en: "Proposal save failed",
    zh: "保存提案失败",
    ja: "提案の保存に失敗",
  },
  "workspace.errors.proposal_accept": {
    en: "Proposal acceptance failed",
    zh: "接受提案失败",
    ja: "提案の承認に失敗",
  },
  "workspace.errors.curator_start": {
    en: "Failed to start curation",
    zh: "启动文献筛选失败",
    ja: "選定の起動に失敗",
  },
  "workspace.errors.synthesizer_start": {
    en: "Failed to start synthesis",
    zh: "启动综合失败",
    ja: "合成の起動に失敗",
  },
  "workspace.errors.ideator_start": {
    en: "Failed to start novelty analysis",
    zh: "启动新颖性分析失败",
    ja: "新規性分析の起動に失敗",
  },
  "workspace.errors.drafter_start": {
    en: "Failed to start drafting",
    zh: "启动草稿撰写失败",
    ja: "草稿執筆の起動に失敗",
  },
  "workspace.errors.stylist_start": {
    en: "Failed to start style polish",
    zh: "启动文风润色失败",
    ja: "文体調整の起動に失敗",
  },
  "workspace.errors.critic_start": {
    en: "Failed to start review",
    zh: "启动评审失败",
    ja: "レビューの起動に失敗",
  },
  "workspace.errors.settings_update": {
    en: "Failed to update run settings",
    zh: "保存运行设置失败",
    ja: "実行設定の保存に失敗",
  },
  "workspace.errors.mode_run_create": {
    en: "Failed to create the requested run",
    zh: "创建指定模式 run 失败",
    ja: "指定したモードの実行作成に失敗",
  },
  "workspace.errors.external_scan_approval": {
    en: "External scan approval failed",
    zh: "外部扫描审批失败",
    ja: "外部スキャンの承認に失敗",
  },
  "workspace.errors.external_scan_skip": {
    en: "External scan skip failed",
    zh: "跳过外部扫描失败",
    ja: "外部スキャンのスキップに失敗",
  },
  "workspace.errors.integrity_start": {
    en: "Integrity start failed",
    zh: "启动 Integrity 失败",
    ja: "Integrity の起動に失敗",
  },
  "workspace.errors.integrity_accept": {
    en: "Integrity acceptance failed",
    zh: "接受 Integrity 结果失败",
    ja: "Integrity 結果の承認に失敗",
  },
  "workspace.errors.integrity_revision": {
    en: "Integrity revision request failed",
    zh: "请求 Integrity 修改失败",
    ja: "Integrity の改訂要求に失敗",
  },
  "workspace.errors.final_acceptance": {
    en: "Final acceptance failed",
    zh: "最终接受失败",
    ja: "最終承認に失敗",
  },
  "workspace.errors.export_start": {
    en: "Export start failed",
    zh: "启动导出失败",
    ja: "エクスポートの起動に失敗",
  },
  "workspace.errors.angle_select": {
    en: "Angle selection failed",
    zh: "选择角度失败",
    ja: "アングル選択に失敗",
  },
  "workspace.errors.novelty_discussion": {
    en: "Novelty discussion failed",
    zh: "新颖性讨论失败",
    ja: "新規性ディスカッションに失敗",
  },
  "workspace.errors.pdf_upload": {
    en: "PDF upload failed",
    zh: "PDF 上传失败",
    ja: "PDF アップロードに失敗",
  },
  "workspace.errors.pdf_upload_running": {
    // PR-I4.b A7: shown when the user clicks upload while any
    // phase is mid-flight. Backend would 409 anyway; we refuse
    // up front for clarity.
    en: "Cannot upload while a phase is running. Wait for it to finish.",
    zh: "有阶段正在运行，暂时无法上传。等当前阶段跑完再试。",
    ja: "フェーズ実行中はアップロードできません。完了するまでお待ちください。",
  },

  // ---- PR-C0.b2.tests: research-kernel intake i18n -------------------
  // kernel.form.* — strings shared by the form on NewRunPage and the
  // KernelEditModal. paper_mode.* — picker / status pill / ack /
  // fallback. kernel.validation.* — submit-disabled reasons.
  // newrun.kernel.* / workspace.kernel.* — page-specific copy.

  "kernel.form.observed_puzzle_label": {
    en: "Observed puzzle / problem",
    zh: "观察到的疑点 / 问题点",
    ja: "観察した疑問・問題点",
  },
  "kernel.form.observed_puzzle_placeholder": {
    en: "An inconsistency / anomaly / gap you noticed in existing scholarship; at least 30 characters.",
    zh: "既有研究中观察到的不一致 / 反常 / 缺口，至少 30 字",
    ja: "既存研究で観察した不一致・反例・欠落、30字以上で記述",
  },
  "kernel.form.observed_puzzle_hint": {
    en: "At least {min} characters. In your own words. {count}/{min}",
    zh: "至少 {min} 字。请用您自己的话描述发现的反常或论争。{count}/{min}",
    ja: "{min}字以上。ご自身の言葉で観察した不一致や論争を記述してください。{count}/{min}",
  },
  "kernel.form.tentative_question_label": {
    en: "Tentative research question",
    zh: "拟研究问题",
    ja: "暫定リサーチクエスチョン",
  },
  "kernel.form.tentative_question_placeholder": {
    en: "The research question that addresses the puzzle.",
    zh: "对应「疑点」的研究问题",
    ja: "観察した疑問に対応するリサーチクエスチョン",
  },
  "kernel.form.scope_label": {
    en: "Scope",
    zh: "研究范围",
    ja: "研究範囲",
  },
  "kernel.form.scope_placeholder": {
    en: "Period / region / source genre boundaries; at most 200 characters.",
    zh: "时段 / 地域 / 文献体例等边界，不超过 200 字",
    ja: "時期・地域・資料体系などの境界、200字以内",
  },
  "kernel.form.scope_hint": {
    en: "Be specific. At most {max} characters. {count}/{max}",
    zh: "越具体越好，最多 {max} 字。{count}/{max}",
    ja: "具体的に。最大{max}字。{count}/{max}",
  },
  "kernel.form.method_preference_label": {
    en: "Method preference",
    zh: "方法偏好",
    ja: "方法の選好",
  },
  "kernel.form.method_preference_placeholder": {
    en: "Optional; e.g. archival research, oral history interviews, close reading.",
    zh: "可选；如「档案考证」「口述史访谈」「文本细读」等",
    ja: "任意；例：「档案考証」「オーラルヒストリー」「精読」など",
  },
  "kernel.form.theory_preference_label": {
    en: "Theory preference",
    zh: "理论倾向",
    ja: "理論的選好",
  },
  "kernel.form.theory_preference_placeholder": {
    en: "Optional; key thinker / framework name.",
    zh: "可选；填关键学者 / 框架名",
    ja: "任意；主要な学者・フレームワーク名",
  },
  "kernel.form.optional_hint": {
    en: "Optional",
    zh: "选填",
    ja: "任意",
  },
  "kernel.form.primary_materials_legend": {
    en: "Primary materials",
    zh: "一手材料状态",
    ja: "一次資料の状況",
  },
  "kernel.form.primary.yes": {
    en: "Already gathered",
    zh: "已经收集到",
    ja: "既に収集済み",
  },
  "kernel.form.primary.will_upload_later": {
    en: "Will upload later",
    zh: "稍后上传",
    ja: "後でアップロード",
  },
  "kernel.form.primary.none": {
    en: "No primary materials (theory / review only)",
    zh: "暂无（仅理论 / 综述）",
    ja: "なし（理論・レビューのみ）",
  },

  "kernel.validation.no_mode": {
    en: "Please select a paper mode.",
    zh: "请选择论文模式",
    ja: "論文モードを選択してください",
  },
  "kernel.validation.mode_coming_soon": {
    en: "This mode is coming soon and not yet selectable ({mode_id}).",
    zh: "该模式即将开放，暂不可选 ({mode_id})",
    ja: "このモードは近日公開予定で、現在選択できません ({mode_id})",
  },
  "kernel.validation.preview_ack_required": {
    en: "Please confirm you understand the preview mode's limitations.",
    zh: "请先确认已了解预览模式的限制",
    ja: "プレビュー段階の制限を理解した旨にチェックしてください",
  },
  "kernel.validation.puzzle_too_short": {
    en: "Observed puzzle must be at least {min} characters.",
    zh: "「观察到的疑点 / 问题点」至少需要 {min} 个字符",
    ja: "「観察した疑問・問題点」は{min}字以上必要です",
  },
  "kernel.validation.question_required": {
    en: "Please fill in the tentative research question.",
    zh: "请填写「拟研究问题」",
    ja: "「暫定リサーチクエスチョン」を入力してください",
  },
  "kernel.validation.scope_required": {
    en: "Please fill in the scope.",
    zh: "请填写「研究范围」",
    ja: "「研究範囲」を入力してください",
  },
  "kernel.validation.scope_too_long": {
    en: "Scope cannot exceed {max} characters.",
    zh: "「研究范围」不要超过 {max} 字符",
    ja: "「研究範囲」は{max}字を超えないでください",
  },
  "kernel.validation.primary_required": {
    en: "Empirical mode requires primary materials. Switch to Case analysis or change the materials status.",
    zh: "实证模式需要一手材料；请改选「个案分析」或确认材料状态",
    ja: "実証モードは一次資料が必要です。「個別事例分析」に変更するか資料の状況を更新してください",
  },

  "paper_mode.legend": {
    en: "Paper mode",
    zh: "论文模式",
    ja: "論文モード",
  },
  "paper_mode.readonly_hint": {
    en: "Paper mode is fixed once a proposal exists. Create a new run to use a different mode.",
    zh: "创建运行后，论文模式不可更改。如需更换模式，请新建运行。",
    ja: "提案が作成された後は論文モードを変更できません。別モードを使うには新規ランを作成してください。",
  },
  "paper_mode.degraded_banner": {
    en: "Mode registry unavailable; falling back to case_analysis.",
    zh: "模式注册表加载失败，已使用安全默认（个案分析）",
    ja: "モードレジストリの取得に失敗。安全な既定値（個別事例分析）を使用しています。",
  },
  "paper_mode.status.preview": {
    en: "preview",
    zh: "预览",
    ja: "プレビュー",
  },
  "paper_mode.status.coming_soon": {
    en: "coming soon",
    zh: "即将开放",
    ja: "近日公開",
  },
  "paper_mode.ack_text": {
    en: "I understand this mode is in preview; some capabilities will be added in a later release.",
    zh: "我理解此模式当前为预览形态；部分能力将在后续版本完整启用",
    ja: "このモードがプレビュー段階であり、一部機能が後続リリースで完全提供されることを理解しました",
  },
  "paper_mode.reason.coming_soon": {
    en: "Coming soon",
    zh: "即将开放",
    ja: "近日公開",
  },
  "paper_mode.reason.preview_needs_ack": {
    en: "Tick the preview-mode acknowledgement to enable submit",
    zh: "需先勾选预览模式确认",
    ja: "プレビューモードの確認にチェックが必要",
  },

  "newrun.kernel.section_label": {
    en: "Research kernel",
    zh: "研究内核",
    ja: "リサーチカーネル",
  },
  "newrun.kernel.intro_paragraph": {
    en: "The system does not invent your research question. The fields below are the research kernel you give the system.",
    zh: "系统不替您构思研究问题；下面这些字段是您给系统的「研究内核」。",
    ja: "システムが研究の問いを発案するのではなく、以下の項目はあなたがシステムに渡す「リサーチカーネル」です。",
  },
  "newrun.kernel.suggest_button": {
    en: "AI fill",
    zh: "AI 帮我填",
    ja: "AI 入力",
  },
  "newrun.kernel.suggest_loading": {
    en: "Generating...",
    zh: "生成中…",
    ja: "生成中…",
  },
  "newrun.kernel.suggest_title": {
    en: "Generate empty research-kernel fields from the title and domain.",
    zh: "根据标题和领域生成空白的研究内核字段。",
    ja: "題名と分野から空欄のリサーチカーネル項目を生成します。",
  },
  "newrun.kernel.suggest_title_required": {
    en: "Enter a project title of at least 4 characters first.",
    zh: "请先填写至少 4 个字符的项目标题。",
    ja: "先に4文字以上のプロジェクト名を入力してください。",
  },
  "newrun.kernel.suggest_domain_required": {
    en: "Select a domain before generating the research kernel.",
    zh: "请先选择领域，再生成研究内核。",
    ja: "リサーチカーネル生成の前に分野を選択してください。",
  },
  "newrun.kernel.suggest_error_generic": {
    en: "Research-kernel suggestion failed. Please try again.",
    zh: "研究内核生成失败，请稍后重试。",
    ja: "リサーチカーネルの生成に失敗しました。もう一度お試しください。",
  },
  "newrun.kernel.partial_failure_banner": {
    en: "The kernel could not be saved. Please re-enter your research kernel below — the workspace will save it as soon as you submit.",
    zh: "研究内核未能保存。请在下方重新填写——保存后将立即生效。",
    ja: "リサーチカーネルを保存できませんでした。以下から再度入力してください — 保存後すぐに反映されます。",
  },
  "newrun.kernel.domain_required": {
    en: "Please select a domain first.",
    zh: "请先选择领域",
    ja: "まず領域を選択してください",
  },
  "newrun.kernel.no_domains": {
    en: "No domains available; please contact the administrator.",
    zh: "暂无可用领域，请联系管理员",
    ja: "利用可能な領域がありません。管理者に連絡してください",
  },

  "workspace.kernel.eyebrow": {
    en: "Research kernel",
    zh: "研究内核",
    ja: "リサーチカーネル",
  },
  "workspace.kernel.modal_title": {
    en: "Edit research kernel",
    zh: "编辑研究内核",
    ja: "リサーチカーネルを編集",
  },
  "workspace.kernel.modal_description": {
    en: "Editing the kernel is recorded as a new proposal version (when downstream phases have run) and marks downstream phases as stale.",
    zh: "修改研究内核会被记录为一个新的 proposal 版本（如果下游已经运行过），并把下游 phase 标记为「过时」。",
    ja: "リサーチカーネルの編集は、下流フェーズが実行済みの場合に新しい提案バージョンとして記録され、下流フェーズが「古い」と印付けされます。",
  },
  "workspace.kernel.readonly_mode_hint": {
    en: "Paper mode is locked (a proposal already exists). Create a new run to switch modes.",
    zh: "论文模式已锁定（已存在 proposal 版本）。如需更换模式，请新建运行。",
    ja: "論文モードはロック済み（提案バージョンが存在します）。モード変更には新規ランを作成してください。",
  },
  "workspace.kernel.edit_button": {
    en: "Edit research kernel",
    zh: "编辑研究内核",
    ja: "リサーチカーネルを編集",
  },
  "workspace.kernel.close_aria_label": {
    en: "Close",
    zh: "关闭",
    ja: "閉じる",
  },
  "workspace.kernel.cancel": {
    en: "Cancel",
    zh: "取消",
    ja: "キャンセル",
  },
  "workspace.kernel.save": {
    en: "Save",
    zh: "保存",
    ja: "保存",
  },
  "workspace.kernel.saving": {
    en: "Saving…",
    zh: "保存中…",
    ja: "保存中…",
  },
  "workspace.kernel.repair_banner": {
    en: "Kernel save failed during run creation. Click below to finish setup.",
    zh: "研究内核保存失败，运行已用默认内核创建。请点击下方按钮编辑研究内核以完成设置。",
    ja: "ラン作成中にリサーチカーネルの保存に失敗しました。下のボタンから設定を完了してください。",
  },
  "workspace.kernel.repair_banner_dismiss": {
    en: "Dismiss",
    zh: "关闭提示",
    ja: "閉じる",
  },
  "workspace.kernel.conflict_message": {
    en: "Another session has modified the kernel. The form below keeps your changes; the server's latest values are shown for comparison. Re-submit to keep your changes, or click ‘Use server values’ to overwrite.",
    zh: "另一个会话已修改了内核。下方表单保留了您的输入；右侧展示服务器最新值供对照。如要保留您的修改请重新提交；如要使用服务器版本请点击「用服务器版本替换」。",
    ja: "別のセッションがカーネルを変更しました。下部のフォームには入力内容が保持され、右側にサーバー最新値が表示されます。変更を保持するなら再送信、サーバー値を使うなら「サーバー値を使用」を押してください。",
  },
  "workspace.kernel.conflict_panel_heading": {
    en: "Server's latest values (display only — your edits are not auto-overwritten)",
    zh: "服务器最新值（仅展示，不会自动覆盖您的修改）",
    ja: "サーバーの最新値（表示のみ — 入力は自動で上書きされません）",
  },
  "workspace.kernel.conflict_field.mode": {
    en: "Mode",
    zh: "模式",
    ja: "モード",
  },
  "workspace.kernel.conflict_field.observed_puzzle": {
    en: "Observed puzzle",
    zh: "观察到的疑点",
    ja: "観察した疑問",
  },
  "workspace.kernel.conflict_field.tentative_question": {
    en: "Research question",
    zh: "研究问题",
    ja: "リサーチクエスチョン",
  },
  "workspace.kernel.conflict_field.scope": {
    en: "Scope",
    zh: "研究范围",
    ja: "研究範囲",
  },
  "workspace.kernel.conflict_field.primary_materials": {
    en: "Primary materials",
    zh: "一手材料",
    ja: "一次資料",
  },
  "workspace.kernel.conflict_apply_server": {
    en: "Use server values",
    zh: "用服务器版本替换我的修改",
    ja: "サーバー値を使用",
  },
  "workspace.kernel.conflict_fetch_failed": {
    en: "Conflict detected, but server state could not be fetched.",
    zh: "检测到冲突，但服务器状态获取失败。",
    ja: "競合を検出しましたが、サーバー状態の取得に失敗しました。",
  },

  // ---- PR-C1.b: research_role + dual-track + evidence ledger ----------
  // Strings that name the four research-role tiers + describe the
  // dual-track synthesis view + drive the evidence-ledger sub-tab.

  "research_role.primary_source.label": {
    en: "Primary",
    zh: "一手材料",
    ja: "一次資料",
  },
  "research_role.primary_source.description": {
    en: "Evidentiary item: archive, fieldwork transcript, manuscript, statute, contemporary witness.",
    zh: "证据性条目：档案、田野访谈、手稿、法令、当时人记述。",
    ja: "証拠資料：文書、フィールド調査記録、写本、法令、同時代人の証言。",
  },
  "research_role.secondary_argument.label": {
    en: "Secondary",
    zh: "二手讨论",
    ja: "二次的議論",
  },
  "research_role.secondary_argument.description": {
    en: "Published scholarship arguing a position about the topic.",
    zh: "对议题持有立场的既有学术著述。",
    ja: "テーマに関して立場を論じる既刊学術文献。",
  },
  "research_role.theoretical_lens.label": {
    en: "Lens",
    zh: "理论镜框",
    ja: "理論的レンズ",
  },
  "research_role.theoretical_lens.description": {
    en: "Framework-level work used as a conceptual lens.",
    zh: "用于概念视角的框架性著作。",
    ja: "概念的レンズとして用いるフレームワーク級の研究。",
  },
  "research_role.methodological_reference.label": {
    en: "Method",
    zh: "方法参照",
    ja: "方法参照",
  },
  "research_role.methodological_reference.description": {
    en: "Cited only for a method (e.g. how to do an X analysis).",
    zh: "仅作方法引用（如如何做某类分析）。",
    ja: "方法のみのために引用（例：分析手法など）。",
  },

  "workspace.sources.research_role.adjust_button": {
    en: "Adjust tier",
    zh: "调整层级",
    ja: "層を調整",
  },
  "workspace.sources.research_role.adjust_heading": {
    en: "Set research role",
    zh: "设置研究层级",
    ja: "研究層を設定",
  },
  "workspace.sources.research_role.cancel": {
    en: "Cancel",
    zh: "取消",
    ja: "キャンセル",
  },
  "workspace.sources.research_role.synthesis_stale_warning": {
    en: "Synthesis already ran for this run. Changing a source's tier marks synthesis as stale; rerun synthesizer to refresh the dual-track view.",
    zh: "本运行已生成综述。修改来源层级会将综述标记为陈旧，需要重新运行综合阶段以刷新双轨视图。",
    ja: "このランは既に統合フェーズが完了しています。層を変更すると統合が古いと印付けされ、双方トラック表示を更新するには統合を再実行する必要があります。",
  },

  "workspace.synthesis.dual_track.heading": {
    en: "Dual-track synthesis",
    zh: "双轨综述",
    ja: "双方トラックの統合",
  },
  "workspace.synthesis.dual_track.primary_heading": {
    en: "Primary-track claims (evidence)",
    zh: "一手材料论断（证据）",
    ja: "一次資料の主張（証拠）",
  },
  "workspace.synthesis.dual_track.secondary_heading": {
    en: "Secondary-track positions (literature)",
    zh: "学术史立场（文献）",
    ja: "二次トラックの立場（既存文献）",
  },
  "workspace.synthesis.dual_track.lens_heading": {
    en: "Theoretical-lens references",
    zh: "理论镜框参考",
    ja: "理論的レンズの参照",
  },
  "workspace.synthesis.dual_track.method_heading": {
    en: "Methodological references",
    zh: "方法参照",
    ja: "方法参照",
  },
  "workspace.synthesis.dual_track.no_primary": {
    en: "No primary-track claims yet.",
    zh: "尚无一手材料论断。",
    ja: "一次資料の主張はまだありません。",
  },
  "workspace.synthesis.dual_track.no_secondary": {
    en: "No secondary-track positions yet.",
    zh: "尚无学术史立场。",
    ja: "二次トラックの立場はまだありません。",
  },

  "workspace.evidence_ledger.tab_label": {
    en: "Evidence ledger",
    zh: "证据账本",
    ja: "証拠台帳",
  },
  "workspace.evidence_ledger.heading": {
    en: "Evidence ledger",
    zh: "证据账本",
    ja: "証拠台帳",
  },
  "workspace.evidence_ledger.column.source": {
    en: "Source",
    zh: "来源",
    ja: "出典",
  },
  "workspace.evidence_ledger.column.claim": {
    en: "Claim",
    zh: "论断",
    ja: "主張",
  },
  "workspace.evidence_ledger.column.citation": {
    en: "Citation target",
    zh: "引用目标",
    ja: "引用対象",
  },
  "workspace.evidence_ledger.column.confidence": {
    en: "Confidence",
    zh: "置信度",
    ja: "信頼度",
  },
  "workspace.evidence_ledger.column.action": {
    en: "Override",
    zh: "用户判定",
    ja: "ユーザー判定",
  },
  "workspace.evidence_ledger.action.attribute_to_user": {
    en: "Attribute to user",
    zh: "标记为我的判断",
    ja: "ユーザー判断として記録",
  },
  "workspace.evidence_ledger.action.cite_normally": {
    en: "Cite normally",
    zh: "恢复正常引用",
    ja: "通常引用に戻す",
  },
  "workspace.evidence_ledger.action.none": {
    en: "—",
    zh: "—",
    ja: "—",
  },
  "workspace.evidence_ledger.empty.legacy": {
    en: "No evidence ledger (this run completed synthesis before C1.a).",
    zh: "没有证据账本（运行早于 C1.a）。",
    ja: "証拠台帳はありません（このランは C1.a より前に統合を完了しました）。",
  },
  "workspace.evidence_ledger.empty.no_primary": {
    en: "No primary-track evidence yet for this run.",
    zh: "本运行暂无一手材料证据。",
    ja: "このランにはまだ一次資料の証拠がありません。",
  },
  "workspace.evidence_ledger.empty.not_yet": {
    en: "Synthesis hasn't run yet.",
    zh: "综述阶段尚未执行。",
    ja: "統合フェーズはまだ実行されていません。",
  },
  "workspace.evidence_ledger.attribute_all_button": {
    en: "Attribute entire source to user",
    zh: "整篇来源标记为我的判断",
    ja: "出典全体をユーザー判断として記録",
  },

  // ---- PR-C2.b audit: framework_lens subview + theory_article unlock copy ---

  "workspace.lens.heading": {
    en: "Framework lens",
    zh: "框架镜框",
    ja: "理論的レンズ",
  },
  "workspace.lens.run": {
    en: "Run framework lens",
    zh: "启动框架镜框",
    ja: "理論的レンズを実行",
  },
  "workspace.lens.starting": {
    en: "Starting framework lens…",
    zh: "正在启动框架镜框…",
    ja: "理論的レンズを起動中…",
  },
  "workspace.lens.confirm_and_ideate": {
    en: "Confirm & continue to novelty",
    zh: "确认并进入新颖性",
    ja: "確認して新規性へ進む",
  },
  "workspace.lens.starting_ideator": {
    en: "Starting ideation…",
    zh: "正在启动构思…",
    ja: "新規性ステップを起動中…",
  },
  "workspace.lens.running_indicator": {
    en: "Framework lens running…",
    zh: "正在生成理论镜框分析…",
    ja: "理論的レンズの分析を生成中…",
  },
  "workspace.lens.running_hint": {
    en: "Framework lens is running; please wait until it completes.",
    zh: "框架镜框节点正在运行，请等待完成。",
    ja: "理論的レンズが実行中です。完了までお待ちください。",
  },
  "workspace.lens.review_hint": {
    en: "Lens analysis complete. Confirm or rerun before continuing to the novelty step.",
    zh: "框架镜框已完成。确认后即可进入新颖性节点。",
    ja: "レンズ分析が完了しました。次の新規性ステップへ進む前に確認してください。",
  },
  "workspace.lens.waiting_for_synthesizer": {
    en: "Lens analysis runs after the synthesizer step completes.",
    zh: "需要先完成综合节点后才能启动框架镜框。",
    ja: "統合ステップ完了後に理論的レンズが利用可能になります。",
  },
  "workspace.lens.not_yet_at_field_review": {
    en: "Lens phase becomes available after you review the synthesis.",
    zh: "请先在综合节点完成审核，再启动框架镜框。",
    ja: "統合の確認後に理論的レンズフェーズが利用可能になります。",
  },
  "workspace.lens.theory_article_mandatory": {
    en: "Theoretical-article mode requires this phase. Tag at least one source as ‘theoretical_lens’ on the Sources tab before running.",
    zh: "理论论文模式必须先完成框架镜框节点。请在「文献」页将至少一个来源标记为「理论镜框」层级，然后再启动。",
    ja: "理論論文モードはこのフェーズを必須とします。「文献」タブで少なくとも 1 つの出典を「理論的レンズ」層に設定してから実行してください。",
  },
  "workspace.lens.signals_heading": {
    en: "Lens signals",
    zh: "理论镜框信号",
    ja: "レンズ信号",
  },
  "workspace.lens.no_signals_yet": {
    en: "No lens signals yet. Tag sources as ‘theoretical_lens’ on the Sources tab and rerun.",
    zh: "暂无框架镜框信号。请在「文献」页将一手或学术来源标记为「理论镜框」层级，然后重新启动。",
    ja: "理論的レンズの信号はまだありません。「文献」タブで出典を「理論的レンズ」層に設定し、再実行してください。",
  },
  "workspace.lens.signal_source_label": {
    en: "Source",
    zh: "出处",
    ja: "出典",
  },
  "workspace.lens.empty_artifact": {
    en: "Lens phase produced no signals.",
    zh: "框架镜框节点未产出任何信号。",
    ja: "レンズフェーズは信号を生成しませんでした。",
  },
  "workspace.lens.edit_pending_tooltip": {
    en: "PR-F1 待开发",
    zh: "PR-F1 待开发",
    ja: "PR-F1 待开发",
  },

  "phase.framework_lens": {
    en: "Framework lens",
    zh: "框架镜框",
    ja: "理論的レンズ",
  },
  "phase.framework_lens.start": {
    en: "Run framework lens",
    zh: "启动框架镜框",
    ja: "理論的レンズを実行",
  },
  "phase.framework_lens.starting": {
    en: "Starting framework lens…",
    zh: "正在启动框架镜框…",
    ja: "理論的レンズを起動中…",
  },

  "workspace.errors.framework_lens_start": {
    en: "Failed to start framework lens.",
    zh: "启动框架镜框失败。",
    ja: "理論的レンズの起動に失敗しました。",
  },
};

function interpolate(
  template: string,
  vars: Record<string, string | number>,
): string {
  let out = template;
  for (const [name, value] of Object.entries(vars)) {
    out = out.split(`{${name}}`).join(String(value));
  }
  return out;
}

export function t(
  key: string,
  lang: UILanguage,
  vars?: Record<string, string | number>,
): string {
  const entry = CATALOG[key];
  if (!entry) return key;
  const template = entry[lang] ?? entry.en ?? key;
  return vars ? interpolate(template, vars) : template;
}

// `useT` returns a translator bound to the current UI language. It re-runs on
// every language change because the underlying useUILanguage hook subscribes
// to the language store.
export function useT(): (
  key: string,
  vars?: Record<string, string | number>,
) => string {
  const [lang] = useUILanguage();
  return useMemo(
    () => (key: string, vars?: Record<string, string | number>) =>
      t(key, lang, vars),
    [lang],
  );
}

export const UI_LANGUAGE_LABELS: Record<UILanguage, string> = {
  en: "EN",
  zh: "中",
  ja: "日",
};
