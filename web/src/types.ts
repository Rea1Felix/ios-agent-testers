export type QaMode = "explore" | "reproduce" | "improve" | "targeted";
export type Severity = "blocker" | "major" | "minor" | "cosmetic";
export type PersonaStatus =
  | "pending"
  | "provisioning"
  | "running"
  | "paused"
  | "crashed"
  | "done"
  | "error";

export type DashboardTab = "matrix" | "stage" | "summary" | "compare";
export type StreamMode = "stream" | "poll";
export type FallbackReason = "none" | "auto" | "manual" | "no-stream";

export type PhoneKey = "return" | "backspace" | "escape" | "tab";
export type PhoneHardwareButton = "HOME" | "LOCK" | "SIRI" | "SIDE_BUTTON";
export type DeviceActionType = "dark" | "light" | "save_screenshot" | "stop";

export const SEVERITY_ORDER: readonly Severity[] = [
  "blocker",
  "major",
  "minor",
  "cosmetic",
] as const;

export const ISSUE_CLASSES = [
  "layout and text size",
  "feedback and errors",
  "accessibility",
  "translation",
  "right to left",
  "tablet",
  "system bars",
  "copy",
  "visual polish",
  "crash",
  "likely test artefact",
  "other",
] as const;

export interface ToolResultBase {
  ok?: boolean;
  error?: string;
  message?: string;
}

export interface HostCapabilitiesSummary {
  canUpdateModelContext: boolean;
  canSendMessage: boolean;
  canCallServerTools: boolean;
}

export interface RunSummary {
  run_id: string;
  mode: QaMode;
  created_at: string;
  app_id: string | null;
  persona_count: number;
  active_count: number;
  done_count: number;
  issue_count: number;
  avg_score: number | null;
  has_summary: boolean;
}

export interface DashboardBootstrap extends ToolResultBase {
  runs: RunSummary[];
  activeRunId?: string | null;
  activePersonaId?: string | null;
  httpBaseUrl?: string | null;
  streamToken?: string | null;
}

export interface ListRunsResult extends ToolResultBase {
  runs: RunSummary[];
  httpBaseUrl?: string | null;
  streamToken?: string | null;
}

export interface RunManifest {
  run_id: string;
  mode: QaMode;
  app_id: string | null;
  app_path: string | null;
  created_at: string;
  record_video: boolean;
  max_concurrency: number;
  max_actions?: number;
  bug_report_title: string | null;
}

export interface PersonaDeviceSpec {
  simulator: string;
  ios?: number | string;
  text_size?: string;
  theme?: "light" | "dark" | string;
}

export interface SeverityCounts {
  blocker: number;
  major: number;
  minor: number;
  cosmetic: number;
}

export interface PersonaRunState {
  id: string;
  profile?: string;
  goal?: string;
  locale: string;
  device: PersonaDeviceSpec;
  status: PersonaStatus;
  udid: string | null;
  device_label: string | null;
  width_points: number;
  height_points: number;
  step_count: number;
  max_steps: number;
  action_count?: number;
  observation_count?: number;
  max_actions?: number;
  paused_by_human: boolean;
  human_note: string | null;
  issue_counts: SeverityCounts;
  score: number | null;
  reproduced: "yes" | "no" | "unclear" | null;
  goal_reached: "yes" | "partly" | "no" | null;
  crash_count: number;
  has_video: boolean;
  last_screenshot: string | null;
  error: string | null;
}

export interface A11yFinding {
  category: "unnamed" | "undersized" | string;
  kind: string;
  name: string;
  x: number;
  y: number;
  width: number;
  height: number;
  cx: number;
  cy: number;
}

export interface A11yAuditResult {
  summary_line: string | null;
  unnamed: A11yFinding[];
  undersized: A11yFinding[];
}

export interface StepRecord {
  step: number;
  timestamp: string;
  screenshot_file: string | null;
  action: {
    kind: string;
    detail?: string;
    [key: string]: unknown;
  } | null;
  a11y: A11yAuditResult | null;
  app_running: boolean | null;
  crashes: Array<{ name?: string; excerpt?: string; [key: string]: unknown }>;
  elements_count: number;
  ax_unavailable_reason?: string | null;
}

export interface CrashItem {
  name: string;
  excerpt: string;
}

export interface PersonaFindingIssue {
  severity: Severity;
  title: string;
  screen?: string;
  screens?: string[];
  description?: string;
  details?: string;
  [key: string]: unknown;
}

export interface PersonaFindingImprovement {
  impact: "high" | "medium" | "low" | string;
  area: string;
  title: string;
  screen?: string;
  screens?: string[];
  why?: string;
}

export interface PersonaFindings {
  mode?: QaMode;
  score?: number | null;
  reproduced?: "yes" | "no" | "unclear" | null;
  evidence?: string[];
  note?: string | null;
  goal_reached?: "yes" | "partly" | "no" | null;
  actions?: number | null;
  issues?: PersonaFindingIssue[];
  improvements?: PersonaFindingImprovement[];
  [key: string]: unknown;
}

export interface PersonaDetailResult extends ToolResultBase {
  run_id: string;
  persona_id: string;
  state: PersonaRunState;
  steps: StepRecord[];
  logs_tail: string[];
  crashes: CrashItem[];
  report_md: string | null;
  report_path: string | null;
  findings: PersonaFindings | null;
}

export interface SummaryReproductionRow {
  persona: string;
  reproduced: "yes" | "no" | "unclear" | string;
  device: string;
  locale: string;
  text_size: string;
  evidence: string[];
  note: string;
}

export interface SummaryWalkthroughRow {
  persona: string;
  goal_reached: "yes" | "partly" | "no" | string;
  actions: number | null;
  score: number | null;
  device: string;
}

export interface SummaryImprovementArea {
  area: string;
  persona_count: number;
  count: number;
}

export interface SummaryImprovementItem {
  impact: "high" | "medium" | "low" | string;
  area: string;
  title: string;
  persona: string;
  screen?: string;
  screens: string[];
  why: string;
}

export interface SummaryIssueRawReport {
  id: number;
  persona: string;
  severity: Severity;
  screen: string;
  screens: string[];
  title: string;
}

export interface SummaryIssueGroup {
  title: string;
  severity: Severity;
  class: string;
  personas: string[];
  screens: string[];
  reports: SummaryIssueRawReport[];
}

export interface SummaryClassBreakdown {
  class: string;
  personas: string[];
  count: number;
}

export interface SummaryDict {
  run_id: string;
  mode: QaMode;
  bug_report_title: string | null;
  grouped: boolean;
  persona_count: number;
  report_count: number;
  distinct_issue_count: number;
  avg_score: number | null;
  severity_counts: SeverityCounts;
  reproduction: {
    counts: { yes: number; no: number; unclear: number };
    rows: SummaryReproductionRow[];
  } | null;
  walkthrough: {
    goal_counts: { yes: number; partly: number; no: number };
    median_actions: number | null;
    rows: SummaryWalkthroughRow[];
    areas: SummaryImprovementArea[];
    improvements: SummaryImprovementItem[];
  } | null;
  issues: SummaryIssueGroup[];
  classes: SummaryClassBreakdown[];
}

export interface RunStateResult extends ToolResultBase {
  run_id: string | null;
  manifest: RunManifest | null;
  personas: PersonaRunState[];
  summary: SummaryDict | null;
  summary_path: string | null;
  httpBaseUrl?: string | null;
  streamToken?: string | null;
}

export interface ComparePersonaRow {
  persona: string;
  score_a: number | null;
  score_b: number | null;
  delta: number | null;
  actions_a: number | null;
  actions_b: number | null;
  goal_a: string | null;
  goal_b: string | null;
  issues_a: number;
  issues_b: number;
}

export interface CompareUniqueIssue {
  title: string;
  severity: Severity;
  personas: string[];
}

export interface CompareImprovementDelta {
  area: string;
  count_a: number;
  count_b: number;
}

export interface CompareDict {
  name_a: string;
  name_b: string;
  overview: {
    personas: [number, number];
    goal_reached: [
      { yes: number; partly: number; no: number },
      { yes: number; partly: number; no: number },
    ];
    median_actions: [number | null, number | null];
    avg_score: [number | null, number | null];
    issues: [SeverityCounts, SeverityCounts];
  };
  personas: ComparePersonaRow[];
  only_a_personas: string[];
  only_b_personas: string[];
  score_summary: {
    higher: number;
    lower: number;
    same: number;
  };
  only_in_a: CompareUniqueIssue[];
  only_in_b: CompareUniqueIssue[];
  improvements_by_area: CompareImprovementDelta[];
}

export interface CompareToolResult extends ToolResultBase {
  markdown: string;
  compare: CompareDict;
  compare_path: string | null;
}

export interface FrameResult extends ToolResultBase {
  mimeType?: string;
  data?: string;
  base64?: string;
  width_points?: number;
  height_points?: number;
}

export interface DeviceActionResult extends ToolResultBase {
  screenshot_path?: string;
  screenshotPath?: string;
  screenshot_uri?: string;
  screenshotUri?: string;
  path?: string;
}

export interface TakeoverResult extends ToolResultBase {
  paused: boolean;
  note: string | null;
}

export interface TapRipple {
  id: number;
  pctX: number;
  pctY: number;
}

export interface SwipeTrail {
  id: number;
  x1Pct: number;
  y1Pct: number;
  x2Pct: number;
  y2Pct: number;
  active: boolean;
}

export interface ToastItem {
  id: number;
  message: string;
  tone: "info" | "success" | "warning" | "danger";
}

export interface StreamConfig {
  httpBaseUrl: string | null;
  streamToken: string | null;
}

/* ---------- Pure display helpers ---------- */

export function isRtlLocale(locale: string | null | undefined): boolean {
  if (!locale) return false;
  const lang = locale.toLowerCase().split(/[-_]/)[0];
  return lang === "ar" || lang === "he" || lang === "fa" || lang === "ur";
}

export function isLivePersonaStatus(status: PersonaStatus): boolean {
  return status === "running" || status === "paused" || status === "provisioning";
}

export function isIpadSimulator(simulator: string | null | undefined): boolean {
  return Boolean(simulator && simulator.toLowerCase().includes("ipad"));
}

export function formatShortDevice(simulator: string | null | undefined): string {
  if (!simulator) return "iOS Sim";
  return simulator
    .replace(/\s*\(3rd generation\)/i, " 3rd")
    .replace(/\s*\(6th generation\)/i, " 6th")
    .replace(/\s*\(10th generation\)/i, " 10th")
    .replace(/\s*\(11-inch\)/i, ' 11"')
    .replace(/\s*\(13-inch\)/i, ' 13"')
    .replace(/\s*\(M\d\)/i, "");
}

const TEXT_SIZE_ABBR: Record<string, string> = {
  "extra-small": "XS",
  small: "S",
  medium: "M",
  large: "L",
  "extra-large": "XL",
  "extra-extra-large": "XXL",
  "extra-extra-extra-large": "XXXL",
  "accessibility-medium": "AX-M",
  "accessibility-large": "AX-L",
  "accessibility-extra-large": "AX-XL",
  "accessibility-extra-extra-large": "AX-XXL",
  "accessibility-extra-extra-extra-large": "AX-XXXL",
};

export function formatTextSizeChip(textSize: string | null | undefined): string {
  if (!textSize) return "L";
  return TEXT_SIZE_ABBR[textSize.toLowerCase()] ?? textSize;
}

export function isLargeTextSize(textSize: string | null | undefined): boolean {
  if (!textSize) return false;
  const lower = textSize.toLowerCase();
  return lower.startsWith("accessibility-") || lower.includes("extra-extra");
}

export function parseStepNumber(screenRef: string | number | null | undefined): number | null {
  if (typeof screenRef === "number" && Number.isFinite(screenRef)) {
    return Math.max(1, Math.round(screenRef));
  }
  if (typeof screenRef !== "string") return null;
  const match = screenRef.match(/(\d+)/);
  if (!match) return null;
  const num = Number.parseInt(match[1], 10);
  return Number.isFinite(num) && num > 0 ? num : null;
}

export function totalIssues(counts: SeverityCounts | null | undefined): number {
  if (!counts) return 0;
  return (
    (counts.blocker || 0) +
    (counts.major || 0) +
    (counts.minor || 0) +
    (counts.cosmetic || 0)
  );
}

export function formatShortTimestamp(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 16);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${mm}-${dd} ${hh}:${mi}`;
}

export function formatModeLabel(mode: QaMode | string | null | undefined): string {
  switch (mode) {
    case "explore":
      return "Explore";
    case "reproduce":
      return "Reproduce";
    case "improve":
      return "Improve";
    case "targeted":
      return "Targeted";
    default:
      return mode || "Explore";
  }
}
