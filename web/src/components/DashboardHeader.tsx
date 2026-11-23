import {
  CompareIcon,
  FileMarkdownIcon,
  MatrixIcon,
  PhoneIcon,
  RefreshIcon,
  SummaryIcon,
} from "../icons";
import {
  formatModeLabel,
  formatShortTimestamp,
  type DashboardTab,
  type RunStateResult,
  type RunSummary,
} from "../types";

interface DashboardHeaderProps {
  runs: RunSummary[];
  activeRunId: string | null;
  runState: RunStateResult | null;
  activePersonaId: string | null;
  activeTab: DashboardTab;
  refreshing: boolean;
  loadingInitial: boolean;
  markdownPath: string | null;
  markdownLabel: string;
  onSelectRun: (runId: string) => void;
  onSelectTab: (tab: DashboardTab) => void;
  onRefresh: () => void;
  onOpenMarkdown: (absPath: string) => void;
}

export function DashboardHeader({
  runs,
  activeRunId,
  runState,
  activePersonaId,
  activeTab,
  refreshing,
  loadingInitial,
  markdownPath,
  markdownLabel,
  onSelectRun,
  onSelectTab,
  onRefresh,
  onOpenMarkdown,
}: DashboardHeaderProps) {
  const activeRunMeta = runs.find((r) => r.run_id === activeRunId) ?? null;
  const mode = runState?.manifest?.mode ?? activeRunMeta?.mode ?? null;
  const personas = runState?.personas ?? [];

  const totalPersonas = personas.length || activeRunMeta?.persona_count || 0;
  const donePersonas = personas.length
    ? personas.filter((p) => p.status === "done" || p.status === "crashed" || p.status === "error")
        .length
    : activeRunMeta?.done_count ?? 0;
  const activePersonas = personas.length
    ? personas.filter(
        (p) => p.status === "running" || p.status === "paused" || p.status === "provisioning",
      ).length
    : activeRunMeta?.active_count ?? 0;
  const pausedCount = personas.filter((p) => p.paused_by_human || p.status === "paused").length;

  const issueTotal =
    runState?.summary?.distinct_issue_count ??
    activeRunMeta?.issue_count ??
    0;

  return (
    <header className="dash-header" aria-label="iOS QA Dashboard navigation and run controls">
      <div className="dash-header-top">
        <div className="dash-header-left">
          <div className="brand-mark" title="iOS Agent Testers QA Dashboard">
            <span className="brand-dot" aria-hidden="true" />
            <span className="brand-title">iOS QA</span>
          </div>

          <label htmlFor="qa-run-select" className="sr-only">
            Select QA Run
          </label>
          <select
            id="qa-run-select"
            className="run-select"
            value={activeRunId ?? ""}
            disabled={runs.length === 0}
            onChange={(e) => {
              if (e.target.value) onSelectRun(e.target.value);
            }}
            aria-label="Select QA Run"
          >
            {runs.length === 0 ? (
              <option value="">
                {loadingInitial ? "Loading runs…" : "No QA runs yet"}
              </option>
            ) : (
              runs.map((r) => {
                const appPart = r.app_id ? ` · ${r.app_id.split(".").pop()}` : "";
                const ts = formatShortTimestamp(r.created_at);
                const liveDot = r.active_count > 0 ? "● " : "";
                return (
                  <option key={r.run_id} value={r.run_id}>
                    {liveDot}
                    {r.run_id}
                    {appPart}
                    {ts ? ` (${ts})` : ""}
                  </option>
                );
              })
            )}
          </select>

          {mode && (
            <span className={`mode-pill mode-pill-${mode}`} title={`Test mode: ${mode}`}>
              {formatModeLabel(mode)}
            </span>
          )}

          {totalPersonas > 0 && (
            <span
              className={`progress-pill ${activePersonas > 0 ? "is-active" : ""} ${pausedCount > 0 ? "is-paused" : ""}`}
              title={`${donePersonas} of ${totalPersonas} personas finished${activePersonas > 0 ? `, ${activePersonas} active` : ""}${pausedCount > 0 ? `, ${pausedCount} paused for human takeover` : ""}`}
            >
              {activePersonas > 0 && <span className="pulse-dot" aria-hidden="true" />}
              <span className="tabular-nums">
                {donePersonas}/{totalPersonas}
              </span>
              {pausedCount > 0 && <span className="progress-paused-tag">PAUSED</span>}
            </span>
          )}
        </div>

        <div className="dash-header-right">
          {markdownPath && (
            <button
              type="button"
              className="hdr-btn"
              onClick={() => onOpenMarkdown(markdownPath)}
              title={`Open ${markdownLabel} in IDE side pane`}
              aria-label={`Open ${markdownLabel} in IDE side pane`}
            >
              <FileMarkdownIcon width={14} height={14} />
              <span className="hdr-btn-label">{markdownLabel}</span>
            </button>
          )}

          <button
            type="button"
            className={`hdr-icon-btn ${refreshing ? "is-spinning" : ""}`}
            onClick={onRefresh}
            disabled={refreshing}
            title="Refresh runs and status"
            aria-label="Refresh runs and status"
          >
            <RefreshIcon width={14} height={14} />
          </button>
        </div>
      </div>

      <nav className="dash-tabs" role="tablist" aria-label="Dashboard views">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "matrix"}
          className={`dash-tab ${activeTab === "matrix" ? "is-active" : ""}`}
          onClick={() => onSelectTab("matrix")}
        >
          <MatrixIcon width={14} height={14} />
          <span>Live Matrix</span>
          {activePersonas > 0 && (
            <span className="tab-badge tab-badge-live tabular-nums">{activePersonas}</span>
          )}
        </button>

        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "stage"}
          className={`dash-tab ${activeTab === "stage" ? "is-active" : ""}`}
          onClick={() => onSelectTab("stage")}
        >
          <PhoneIcon width={14} height={14} />
          <span>Stage</span>
          {activePersonaId && (
            <span className="tab-badge tab-badge-persona">{activePersonaId}</span>
          )}
        </button>

        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "summary"}
          className={`dash-tab ${activeTab === "summary" ? "is-active" : ""}`}
          onClick={() => onSelectTab("summary")}
        >
          <SummaryIcon width={14} height={14} />
          <span>Summary</span>
          {issueTotal > 0 && (
            <span className="tab-badge tabular-nums">{issueTotal}</span>
          )}
        </button>

        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "compare"}
          className={`dash-tab ${activeTab === "compare" ? "is-active" : ""}`}
          onClick={() => onSelectTab("compare")}
        >
          <CompareIcon width={14} height={14} />
          <span>Compare</span>
        </button>
      </nav>
    </header>
  );
}
