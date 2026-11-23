import { useState, type ReactNode } from "react";
import {
  A11yIcon,
  AlertTriangleIcon,
  BugIcon,
  FileMarkdownIcon,
  SearchIcon,
  TerminalIcon,
} from "../icons";
import { flattenA11yBoxes } from "./A11yOverlay";
import {
  parseStepNumber,
  type PersonaDetailResult,
  type PersonaRunState,
  type StepRecord,
} from "../types";

interface PersonaReportDrawerProps {
  persona: PersonaRunState;
  detail: PersonaDetailResult | null;
  loadingDetail: boolean;
  selectedStep: StepRecord | null;
  showA11yOverlay: boolean;
  highlightedA11yKey: string | null;
  onHighlightA11y: (key: string | null) => void;
  onEnsureA11yOverlay: () => void;
  onJumpToStep: (stepNum: number, enableA11y?: boolean) => void;
  onOpenMarkdown: (absPath: string) => void;
}

type DrawerTab = "step" | "logs" | "report";

function renderInlineMarkdown(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }
    const token = match[0];
    if (token.startsWith("**") && token.endsWith("**")) {
      nodes.push(<strong key={`b-${key++}`}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("`") && token.endsWith("`")) {
      nodes.push(<code key={`c-${key++}`}>{token.slice(1, -1)}</code>);
    }
    lastIndex = pattern.lastIndex;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }
  return nodes;
}

function SimpleMarkdown({ content }: { content: string }) {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let idx = 0;

  while (idx < lines.length) {
    const line = lines[idx];
    const trimmed = line.trim();

    if (!trimmed) {
      idx += 1;
      continue;
    }

    // Fenced code block
    if (trimmed.startsWith("```")) {
      const codeLines: string[] = [];
      idx += 1;
      while (idx < lines.length && !lines[idx].trim().startsWith("```")) {
        codeLines.push(lines[idx]);
        idx += 1;
      }
      if (idx < lines.length) idx += 1;
      blocks.push(
        <pre key={`code-${idx}`} className="md-code-block">
          <code>{codeLines.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    // Headings
    const headingMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      const body = renderInlineMarkdown(headingMatch[2]);
      blocks.push(
        <div key={`h-${idx}`} className={`md-heading md-h${level}`}>
          {body}
        </div>,
      );
      idx += 1;
      continue;
    }

    // Bullet list
    if (/^[-*+]\s+/.test(trimmed)) {
      const items: ReactNode[] = [];
      while (idx < lines.length && /^[-*+]\s+/.test(lines[idx].trim())) {
        const text = lines[idx].trim().replace(/^[-*+]\s+/, "");
        items.push(<li key={`li-${idx}`}>{renderInlineMarkdown(text)}</li>);
        idx += 1;
      }
      blocks.push(
        <ul key={`ul-${idx}`} className="md-list">
          {items}
        </ul>,
      );
      continue;
    }

    // Numbered list
    if (/^\d+\.\s+/.test(trimmed)) {
      const items: ReactNode[] = [];
      while (idx < lines.length && /^\d+\.\s+/.test(lines[idx].trim())) {
        const text = lines[idx].trim().replace(/^\d+\.\s+/, "");
        items.push(<li key={`oli-${idx}`}>{renderInlineMarkdown(text)}</li>);
        idx += 1;
      }
      blocks.push(
        <ol key={`ol-${idx}`} className="md-list">
          {items}
        </ol>,
      );
      continue;
    }

    // Paragraph (coalesce contiguous plain lines)
    const paraLines: string[] = [];
    while (
      idx < lines.length &&
      lines[idx].trim() &&
      !lines[idx].trim().startsWith("```") &&
      !/^#{1,4}\s+/.test(lines[idx].trim()) &&
      !/^[-*+]\s+/.test(lines[idx].trim()) &&
      !/^\d+\.\s+/.test(lines[idx].trim())
    ) {
      paraLines.push(lines[idx].trim());
      idx += 1;
    }
    blocks.push(
      <p key={`p-${idx}`} className="md-para">
        {renderInlineMarkdown(paraLines.join(" "))}
      </p>,
    );
  }

  return <div className="md-rendered">{blocks}</div>;
}

export function PersonaReportDrawer({
  persona,
  detail,
  loadingDetail,
  selectedStep,
  showA11yOverlay,
  highlightedA11yKey,
  onHighlightA11y,
  onEnsureA11yOverlay,
  onJumpToStep,
  onOpenMarkdown,
}: PersonaReportDrawerProps) {
  const [drawerTab, setDrawerTab] = useState<DrawerTab>("step");
  const [logFilter, setLogFilter] = useState("");
  const [showRawJson, setShowRawJson] = useState(false);

  const crashes = detail?.crashes ?? [];
  const logs = detail?.logs_tail ?? [];
  const findings = detail?.findings ?? null;
  const issueCount = findings?.issues?.length ?? 0;

  const filteredLogs = logFilter.trim()
    ? logs.filter((line) => line.toLowerCase().includes(logFilter.trim().toLowerCase()))
    : logs;

  const isActionOnlyStep = Boolean(selectedStep && !selectedStep.screenshot_file);
  const axUnavailableReason =
    !isActionOnlyStep && selectedStep?.ax_unavailable_reason?.trim()
      ? selectedStep.ax_unavailable_reason.trim()
      : null;

  const a11yBoxes = isActionOnlyStep ? [] : flattenA11yBoxes(selectedStep?.a11y);

  const latestObservedStep = (() => {
    const all = detail?.steps ?? [];
    for (let i = all.length - 1; i >= 0; i -= 1) {
      if (Boolean(all[i].screenshot_file)) {
        return all[i];
      }
    }
    return null;
  })();
  const latestInspectTarget =
    latestObservedStep ??
    (detail && detail.steps.length > 0 ? detail.steps[detail.steps.length - 1] : null);

  return (
    <aside className="inspector-drawer" aria-label="Persona inspection details">
      {/* Compact Persona Profile Brief */}
      {(persona.profile || persona.goal) && (
        <div className="persona-brief-card">
          {persona.profile && <p className="persona-brief-profile">{persona.profile}</p>}
          {persona.goal && (
            <p className="persona-brief-goal">
              <strong>Goal:</strong> {persona.goal}
            </p>
          )}
        </div>
      )}

      {/* Inspector Sub-Tabs */}
      <div className="drawer-tabs" role="tablist" aria-label="Inspector detail sections">
        <button
          type="button"
          role="tab"
          aria-selected={drawerTab === "step"}
          className={`drawer-tab ${drawerTab === "step" ? "is-active" : ""}`}
          onClick={() => setDrawerTab("step")}
        >
          <A11yIcon width={13} height={13} />
          <span>Step & A11y</span>
          {a11yBoxes.length > 0 && (
            <span className="drawer-tab-badge tabular-nums">{a11yBoxes.length}</span>
          )}
        </button>

        <button
          type="button"
          role="tab"
          aria-selected={drawerTab === "logs"}
          className={`drawer-tab ${drawerTab === "logs" ? "is-active" : ""}`}
          onClick={() => setDrawerTab("logs")}
        >
          <TerminalIcon width={13} height={13} />
          <span>Logs & Crashes</span>
          {crashes.length > 0 && (
            <span className="drawer-tab-badge badge-danger tabular-nums">{crashes.length}</span>
          )}
        </button>

        <button
          type="button"
          role="tab"
          aria-selected={drawerTab === "report"}
          className={`drawer-tab ${drawerTab === "report" ? "is-active" : ""}`}
          onClick={() => setDrawerTab("report")}
        >
          <BugIcon width={13} height={13} />
          <span>Report</span>
          {issueCount > 0 && (
            <span className="drawer-tab-badge tabular-nums">{issueCount}</span>
          )}
        </button>
      </div>

      <div className="drawer-body">
        {loadingDetail && !detail && (
          <div className="drawer-loading">
            <span className="spinner-ring spinner-muted" aria-hidden="true" />
            <span>Loading persona trace…</span>
          </div>
        )}

        {/* TAB 1: Step Action + A11y Findings */}
        {drawerTab === "step" && (
          <div className="drawer-section">
            {selectedStep ? (
              <>
                <div className="step-meta-card">
                  <div className="step-meta-head">
                    <span className="step-badge tabular-nums">
                      Step #{String(selectedStep.step).padStart(2, "0")}
                    </span>
                    {isActionOnlyStep ? (
                      <>
                        <span className="app-alive-pill is-action-only">Action only</span>
                        <span className="step-el-count">observe_after=false</span>
                      </>
                    ) : (
                      <>
                        <span
                          className={`app-alive-pill ${
                            selectedStep.app_running === false
                              ? "is-dead"
                              : selectedStep.app_running === true
                                ? "is-alive"
                                : ""
                          }`}
                        >
                          {selectedStep.app_running === false
                            ? "App Not Running"
                            : selectedStep.app_running === true
                              ? "App Alive"
                              : "Status Unknown"}
                        </span>
                        <span className="step-el-count tabular-nums">
                          {selectedStep.elements_count} AX elements
                        </span>
                      </>
                    )}
                  </div>

                  {selectedStep.action ? (
                    <div className="step-action-box">
                      <span className="action-kind-pill">{selectedStep.action.kind}</span>
                      {selectedStep.action.detail && (
                        <span className="action-detail-text">
                          {selectedStep.action.detail}
                        </span>
                      )}
                    </div>
                  ) : (
                    <div className="step-action-box">
                      <span className="action-kind-pill">observe</span>
                      <span className="action-detail-text">Initial or passive observation</span>
                    </div>
                  )}

                  {selectedStep.timestamp && (
                    <div className="step-timestamp">{selectedStep.timestamp}</div>
                  )}
                </div>

                {axUnavailableReason && (
                  <div className="ax-unavailable-banner" role="status">
                    <AlertTriangleIcon width={14} height={14} className="ax-unavailable-icon" />
                    <div className="ax-unavailable-body">
                      <strong className="ax-unavailable-title">
                        Accessibility tree unavailable (0 AX elements)
                      </strong>
                      <p className="ax-unavailable-reason">{axUnavailableReason}</p>
                    </div>
                  </div>
                )}

                {selectedStep.crashes && selectedStep.crashes.length > 0 && (
                  <div className="step-crash-alert">
                    <div className="step-crash-title">
                      <AlertTriangleIcon width={14} height={14} />
                      <span>
                        {selectedStep.crashes.length} Crash
                        {selectedStep.crashes.length > 1 ? "es" : ""} Detected on This Step
                      </span>
                    </div>
                    {selectedStep.crashes.map((c, i) => (
                      <div key={i} className="step-crash-item">
                        <strong>{c.name || `Crash #${i + 1}`}</strong>
                        {c.excerpt && <pre className="crash-excerpt">{c.excerpt}</pre>}
                      </div>
                    ))}
                  </div>
                )}

                {/* A11y Audit Box List */}
                <div className="a11y-panel">
                  <div className="a11y-panel-head">
                    <h4>Accessibility Audit</h4>
                    {a11yBoxes.length > 0 && (
                      <span className="a11y-legend">
                        <span className="legend-dot legend-unnamed" /> Unnamed
                        <span className="legend-dot legend-undersized" /> &lt;44pt
                      </span>
                    )}
                  </div>

                  {!isActionOnlyStep && selectedStep.a11y?.summary_line && (
                    <p className="a11y-summary-line">{selectedStep.a11y.summary_line}</p>
                  )}

                  {isActionOnlyStep ? (
                    <p className="muted-empty">
                      Action-only step (<code>observe_after=false</code>) — no screenshot or accessibility audit was captured.
                    </p>
                  ) : axUnavailableReason && a11yBoxes.length === 0 ? (
                    <p className="muted-empty">
                      Accessibility audit skipped on this step because the AX tree could not be retrieved.
                    </p>
                  ) : a11yBoxes.length === 0 ? (
                    <p className="muted-empty">
                      No unnamed active controls or undersized (&lt;44pt) touch targets detected on this step.
                    </p>
                  ) : (
                    <ul className="a11y-finding-list">
                      {a11yBoxes.map(({ key, bucket, finding }) => {
                        const isActive = highlightedA11yKey === key;
                        return (
                          <li key={key}>
                            <button
                              type="button"
                              className={`a11y-finding-row a11y-row-${bucket} ${isActive ? "is-selected" : ""}`}
                              onMouseEnter={() => onHighlightA11y(key)}
                              onMouseLeave={() => onHighlightA11y(null)}
                              onClick={() => {
                                if (!showA11yOverlay) onEnsureA11yOverlay();
                                onHighlightA11y(isActive ? null : key);
                              }}
                            >
                              <span className={`a11y-row-tag tag-${bucket}`}>
                                {bucket === "unnamed" ? "Unnamed" : "<44pt"}
                              </span>
                              <span className="a11y-row-main">
                                <strong>{finding.kind || "Control"}</strong>
                                {finding.name ? ` · ${finding.name}` : ""}
                              </span>
                              <span className="a11y-row-dims tabular-nums">
                                {Math.round(finding.width)}×{Math.round(finding.height)}pt
                              </span>
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              </>
            ) : (
              <div className="muted-card">
                <p>
                  Viewing <strong>live simulator feed</strong>. Click any captured frame in the{" "}
                  <strong>Step Filmstrip</strong> to inspect its recorded action, accessibility bounding boxes, and crash state.
                </p>
                {latestInspectTarget && (
                  <button
                    type="button"
                    className="inline-link-btn"
                    onClick={() =>
                      onJumpToStep(
                        latestInspectTarget.step,
                        Boolean(latestInspectTarget.screenshot_file),
                      )
                    }
                  >
                    Inspect Latest Step (#{String(latestInspectTarget.step).padStart(2, "0")})
                  </button>
                )}
              </div>
            )}
          </div>
        )}

        {/* TAB 2: Logs & Crashes */}
        {drawerTab === "logs" && (
          <div className="drawer-section">
            {crashes.length > 0 && (
              <div className="crashes-section">
                <h4 className="section-subhead">
                  <AlertTriangleIcon width={13} height={13} />
                  <span>Crash Reports ({crashes.length})</span>
                </h4>
                {crashes.map((c, idx) => (
                  <details key={idx} className="crash-disclosure" open={idx === 0}>
                    <summary>{c.name || `Crash Report #${idx + 1}`}</summary>
                    <pre className="crash-excerpt">{c.excerpt || "(empty excerpt)"}</pre>
                  </details>
                ))}
              </div>
            )}

            <div className="logs-section">
              <div className="logs-toolbar">
                <div className="search-input-wrap">
                  <SearchIcon width={13} height={13} />
                  <input
                    type="search"
                    className="mini-search-input"
                    value={logFilter}
                    onChange={(e) => setLogFilter(e.target.value)}
                    placeholder="Filter app logs…"
                    aria-label="Filter simulator app logs"
                  />
                </div>
                <span className="logs-count tabular-nums">
                  {filteredLogs.length}/{logs.length} lines
                </span>
              </div>

              {filteredLogs.length === 0 ? (
                <p className="muted-empty">
                  {logs.length === 0
                    ? "No simulator app logs captured yet."
                    : "No log lines match your filter."}
                </p>
              ) : (
                <div className="log-terminal" role="log" aria-label="Simulator log tail">
                  {filteredLogs.map((line, i) => {
                    const lower = line.toLowerCase();
                    const tone =
                      lower.includes("error") ||
                      lower.includes("exception") ||
                      lower.includes("fatal") ||
                      lower.includes("crash")
                        ? "log-err"
                        : lower.includes("warn") || lower.includes("fault")
                          ? "log-warn"
                          : "";
                    return (
                      <div key={i} className={`log-line ${tone}`}>
                        {line}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}

        {/* TAB 3: Persona Report & Findings */}
        {drawerTab === "report" && (
          <div className="drawer-section">
            <div className="report-actions-row">
              {detail?.report_path && (
                <button
                  type="button"
                  className="hdr-btn"
                  onClick={() => onOpenMarkdown(detail.report_path!)}
                >
                  <FileMarkdownIcon width={13} height={13} />
                  <span>Open report.md</span>
                </button>
              )}
              {findings && (
                <button
                  type="button"
                  className="hdr-btn"
                  onClick={() => setShowRawJson((v) => !v)}
                >
                  <span>{showRawJson ? "Formatted View" : "Raw findings.json"}</span>
                </button>
              )}
            </div>

            {showRawJson && findings ? (
              <pre className="json-preview">{JSON.stringify(findings, null, 2)}</pre>
            ) : (
              <>
                {findings && (
                  <div className="findings-cards">
                    <div className="findings-summary-pills">
                      {findings.score !== null && findings.score !== undefined && (
                        <span className="meta-chip tabular-nums">Score: {findings.score}/10</span>
                      )}
                      {findings.reproduced && (
                        <span className={`verdict-chip verdict-${findings.reproduced}`}>
                          Reproduced: {findings.reproduced}
                        </span>
                      )}
                      {findings.goal_reached && (
                        <span className={`verdict-chip verdict-${findings.goal_reached}`}>
                          Goal: {findings.goal_reached}
                        </span>
                      )}
                      {findings.actions !== null && findings.actions !== undefined && (
                        <span className="meta-chip tabular-nums">
                          {findings.actions} actions
                        </span>
                      )}
                    </div>

                    {findings.evidence && findings.evidence.length > 0 && (
                      <div className="evidence-row">
                        <span className="evidence-label">Evidence:</span>
                        {findings.evidence.map((ref, idx) => {
                          const stepNum = parseStepNumber(ref);
                          return stepNum ? (
                            <button
                              key={`${ref}-${idx}`}
                              type="button"
                              className="screen-jump-chip tabular-nums"
                              onClick={() => onJumpToStep(stepNum, true)}
                            >
                              #{String(stepNum).padStart(2, "0")}
                            </button>
                          ) : (
                            <span key={`${ref}-${idx}`} className="meta-chip">
                              {ref}
                            </span>
                          );
                        })}
                      </div>
                    )}

                    {findings.issues && findings.issues.length > 0 && (
                      <div className="persona-findings-list">
                        <h4 className="section-subhead">
                          Reported Issues ({findings.issues.length})
                        </h4>
                        {findings.issues.map((iss, idx) => (
                          <div key={idx} className="persona-issue-card">
                            <div className="persona-issue-head">
                              <span className={`sev-pill sev-${iss.severity}`}>
                                {iss.severity}
                              </span>
                              <span className="persona-issue-title">{iss.title}</span>
                            </div>
                            {(iss.description || iss.details) && (
                              <p className="persona-issue-desc">
                                {iss.description || iss.details}
                              </p>
                            )}
                            <div className="persona-issue-meta">
                              {iss.screen && <span className="meta-chip">{iss.screen}</span>}
                              {(iss.screens ?? []).map((s, sIdx) => {
                                const stepNum = parseStepNumber(s);
                                return stepNum ? (
                                  <button
                                    key={`${s}-${sIdx}`}
                                    type="button"
                                    className="screen-jump-chip tabular-nums"
                                    onClick={() => onJumpToStep(stepNum, true)}
                                  >
                                    Step #{String(stepNum).padStart(2, "0")}
                                  </button>
                                ) : null;
                              })}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}

                    {findings.improvements && findings.improvements.length > 0 && (
                      <div className="persona-findings-list">
                        <h4 className="section-subhead">
                          UX Improvements ({findings.improvements.length})
                        </h4>
                        {findings.improvements.map((imp, idx) => (
                          <div key={idx} className="persona-issue-card">
                            <div className="persona-issue-head">
                              <span className={`impact-pill impact-${imp.impact}`}>
                                {imp.impact}
                              </span>
                              <span className="meta-chip">{imp.area}</span>
                              <span className="persona-issue-title">{imp.title}</span>
                            </div>
                            {imp.why && <p className="persona-issue-desc">{imp.why}</p>}
                            {imp.screens && imp.screens.length > 0 && (
                              <div className="persona-issue-meta">
                                {imp.screens.map((s, sIdx) => {
                                  const stepNum = parseStepNumber(s);
                                  return stepNum ? (
                                    <button
                                      key={`${s}-${sIdx}`}
                                      type="button"
                                      className="screen-jump-chip tabular-nums"
                                      onClick={() => onJumpToStep(stepNum, true)}
                                    >
                                      Step #{String(stepNum).padStart(2, "0")}
                                    </button>
                                  ) : null;
                                })}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {detail?.report_md ? (
                  <div className="persona-report-md-wrap">
                    <h4 className="section-subhead">Narrative Report</h4>
                    <SimpleMarkdown content={detail.report_md} />
                  </div>
                ) : !findings ? (
                  <p className="muted-empty">
                    This persona has not recorded its final <code>report.md</code> or{" "}
                    <code>findings.json</code> yet.
                  </p>
                ) : null}
              </>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
