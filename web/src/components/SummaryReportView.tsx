import { useMemo, useState } from "react";
import {
  BugIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  FileMarkdownIcon,
  SearchIcon,
  SummaryIcon,
} from "../icons";
import {
  ISSUE_CLASSES,
  parseStepNumber,
  SEVERITY_ORDER,
  type PersonaRunState,
  type Severity,
  type SummaryDict,
} from "../types";

interface SummaryReportViewProps {
  runId: string;
  summary: SummaryDict | null;
  summaryPath: string | null;
  personas: PersonaRunState[];
  onJumpToPersonaStep: (personaId: string, step: number | null, enableA11y?: boolean) => void;
  onOpenMarkdown: (absPath: string) => void;
}

export function SummaryReportView({
  runId,
  summary,
  summaryPath,
  personas,
  onJumpToPersonaStep,
  onOpenMarkdown,
}: SummaryReportViewProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [severityFilter, setSeverityFilter] = useState<Severity | "all">("all");
  const [classFilter, setClassFilter] = useState<string>("all");
  const [personaFilter, setPersonaFilter] = useState<string>("all");
  const [expandedRows, setExpandedRows] = useState<Record<number, boolean>>({});

  const personaIds = useMemo(() => {
    const set = new Set<string>();
    personas.forEach((p) => set.add(p.id));
    summary?.issues?.forEach((iss) => {
      iss.personas.forEach((p) => set.add(p));
      iss.reports.forEach((r) => set.add(r.persona));
    });
    return Array.from(set).sort();
  }, [personas, summary]);

  const classOptions = useMemo(() => {
    const known = new Set<string>(ISSUE_CLASSES);
    summary?.issues?.forEach((iss) => {
      if (iss.class) known.add(iss.class);
    });
    summary?.classes?.forEach((c) => {
      if (c.class) known.add(c.class);
    });
    return Array.from(known);
  }, [summary]);

  const filteredIssues = useMemo(() => {
    if (!summary?.issues) return [];
    const q = searchQuery.trim().toLowerCase();
    return summary.issues
      .map((issue, originalIdx) => ({ issue, originalIdx }))
      .filter(({ issue }) => {
        if (severityFilter !== "all" && issue.severity !== severityFilter) return false;
        if (classFilter !== "all" && issue.class !== classFilter) return false;
        if (personaFilter !== "all" && !issue.personas.includes(personaFilter)) return false;
        if (q) {
          const hay = [
            issue.title,
            issue.class,
            ...issue.personas,
            ...issue.screens,
            ...issue.reports.map((r) => `${r.title} ${r.screen} ${r.persona}`),
          ]
            .join(" ")
            .toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      });
  }, [summary, searchQuery, severityFilter, classFilter, personaFilter]);

  if (!summary) {
    return (
      <section className="view-scroll summary-view" aria-label="Summary Report">
        <div className="empty-card">
          <SummaryIcon width={30} height={30} className="empty-icon" />
          <h3>Summary Not Generated Yet</h3>
          <p>
            Run <code>{runId}</code> does not have a compiled <code>summary.json</code> yet. Once
            personas record findings, run <code>qa_summarize_run</code> (or{" "}
            <code>ios-agent-testers summarize {runId}</code>) to deduplicate issues across the 11
            QA issue classes.
          </p>
        </div>
      </section>
    );
  }

  const toggleExpand = (idx: number) => {
    setExpandedRows((prev) => ({ ...prev, [idx]: !prev[idx] }));
  };

  const hasActiveFilters =
    severityFilter !== "all" ||
    classFilter !== "all" ||
    personaFilter !== "all" ||
    searchQuery.trim().length > 0;

  const maxAreaCount = Math.max(
    1,
    ...(summary.walkthrough?.areas?.map((a) => a.count) ?? [1]),
  );

  // Build Class x Persona presence map for visual breakdown matrix
  const classMatrixPersonas = personaIds;
  const classPersonaMap = new Map<string, Map<string, number>>();
  for (const iss of summary.issues ?? []) {
    const cls = iss.class || "other";
    let perMap = classPersonaMap.get(cls);
    if (!perMap) {
      perMap = new Map<string, number>();
      classPersonaMap.set(cls, perMap);
    }
    for (const r of iss.reports ?? []) {
      perMap.set(r.persona, (perMap.get(r.persona) ?? 0) + 1);
    }
  }

  return (
    <section className="view-scroll summary-view" aria-label="Run Summary Report">
      {/* Header Bar */}
      <div className="section-top-bar">
        <div className="section-top-titles">
          <h2 className="view-title">QA Summary Triage</h2>
          <span className="meta-chip">
            {summary.grouped ? "Semantic Deduplication" : "Exact Title Grouping"}
          </span>
          {summary.bug_report_title && (
            <span className="meta-bug-title">
              <BugIcon width={12} height={12} />
              <span>{summary.bug_report_title}</span>
            </span>
          )}
        </div>

        {summaryPath && (
          <button
            type="button"
            className="hdr-btn"
            onClick={() => onOpenMarkdown(summaryPath)}
            title="Open SUMMARY.md in IDE side pane"
          >
            <FileMarkdownIcon width={14} height={14} />
            <span>Open SUMMARY.md</span>
          </button>
        )}
      </div>

      {/* KPI Strip */}
      <div className="kpi-grid">
        <div className="kpi-card">
          <span className="kpi-label">Personas</span>
          <span className="kpi-value tabular-nums">{summary.persona_count}</span>
        </div>

        <div className="kpi-card">
          <span className="kpi-label">Avg Score</span>
          <span className="kpi-value tabular-nums">
            {summary.avg_score !== null && summary.avg_score !== undefined
              ? `${summary.avg_score.toFixed(1)}/10`
              : "—"}
          </span>
        </div>

        <div className="kpi-card">
          <span className="kpi-label">Distinct Issues</span>
          <span className="kpi-value tabular-nums">
            {summary.distinct_issue_count}
            <span className="kpi-sub-val"> / {summary.report_count} raw</span>
          </span>
        </div>

        {SEVERITY_ORDER.map((sev) => {
          const cnt = summary.severity_counts?.[sev] ?? 0;
          const active = severityFilter === sev;
          return (
            <button
              key={sev}
              type="button"
              className={`kpi-card kpi-sev-btn kpi-sev-${sev} ${active ? "is-selected" : ""}`}
              onClick={() => setSeverityFilter(active ? "all" : sev)}
              title={`Filter issues by ${sev}`}
            >
              <span className="kpi-label">{sev}</span>
              <span className="kpi-value tabular-nums">{cnt}</span>
            </button>
          );
        })}

        {summary.reproduction && (
          <div className="kpi-card kpi-card-wide">
            <span className="kpi-label">Reproduction Verdict</span>
            <div className="kpi-verdict-row tabular-nums">
              <span className="verdict-chip verdict-yes">
                Yes: {summary.reproduction.counts.yes}
              </span>
              <span className="verdict-chip verdict-no">
                No: {summary.reproduction.counts.no}
              </span>
              <span className="verdict-chip verdict-unclear">
                Unclear: {summary.reproduction.counts.unclear}
              </span>
            </div>
          </div>
        )}

        {summary.walkthrough && (
          <div className="kpi-card kpi-card-wide">
            <span className="kpi-label">Walkthrough Goal & Pace</span>
            <div className="kpi-verdict-row tabular-nums">
              <span className="verdict-chip verdict-yes">
                Yes: {summary.walkthrough.goal_counts.yes}
              </span>
              <span className="verdict-chip verdict-partly">
                Partly: {summary.walkthrough.goal_counts.partly}
              </span>
              <span className="verdict-chip verdict-no">
                No: {summary.walkthrough.goal_counts.no}
              </span>
              {summary.walkthrough.median_actions !== null && (
                <span className="meta-chip">
                  Median: {summary.walkthrough.median_actions} acts
                </span>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Mode-Specific: Reproduction Table */}
      {summary.reproduction && summary.reproduction.rows.length > 0 && (
        <div className="panel-card">
          <div className="panel-card-head">
            <h3>Reproduction Matrix</h3>
          </div>
          <div className="table-scroll">
            <table className="qa-table">
              <thead>
                <tr>
                  <th>Persona</th>
                  <th>Verdict</th>
                  <th>Device / Locale</th>
                  <th>Evidence</th>
                  <th>Note</th>
                </tr>
              </thead>
              <tbody>
                {summary.reproduction.rows.map((row, idx) => (
                  <tr key={`${row.persona}-${idx}`}>
                    <td>
                      <button
                        type="button"
                        className="persona-link-btn"
                        onClick={() => onJumpToPersonaStep(row.persona, null)}
                      >
                        {row.persona}
                      </button>
                    </td>
                    <td>
                      <span className={`verdict-chip verdict-${row.reproduced}`}>
                        {row.reproduced}
                      </span>
                    </td>
                    <td>
                      <div className="cell-chips">
                        <span>{row.device}</span>
                        <span className="meta-chip">{row.locale}</span>
                        {row.text_size && <span className="meta-chip">{row.text_size}</span>}
                      </div>
                    </td>
                    <td>
                      <div className="cell-chips">
                        {(row.evidence ?? []).map((ev, eIdx) => {
                          const stepNum = parseStepNumber(ev);
                          return stepNum ? (
                            <button
                              key={`${ev}-${eIdx}`}
                              type="button"
                              className="screen-jump-chip tabular-nums"
                              onClick={() => onJumpToPersonaStep(row.persona, stepNum, true)}
                              title={`Inspect ${row.persona} step #${String(stepNum).padStart(2, "0")} with A11y overlay`}
                            >
                              #{String(stepNum).padStart(2, "0")}
                            </button>
                          ) : (
                            <span key={`${ev}-${eIdx}`} className="meta-chip">
                              {ev}
                            </span>
                          );
                        })}
                      </div>
                    </td>
                    <td className="cell-wrap">{row.note || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Filterable Deduplicated Issues Table */}
      <div className="panel-card">
        <div className="panel-card-head">
          <h3>
            Deduplicated Issues{" "}
            <span className="head-count tabular-nums">
              ({filteredIssues.length}/{summary.issues.length})
            </span>
          </h3>
          {hasActiveFilters && (
            <button
              type="button"
              className="inline-link-btn"
              onClick={() => {
                setSearchQuery("");
                setSeverityFilter("all");
                setClassFilter("all");
                setPersonaFilter("all");
              }}
            >
              Reset filters
            </button>
          )}
        </div>

        <div className="filter-bar">
          <div className="search-input-wrap filter-search">
            <SearchIcon width={13} height={13} />
            <input
              type="search"
              className="mini-search-input"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search titles, screens, classes…"
              aria-label="Search issues"
            />
          </div>

          <select
            className="filter-select"
            value={severityFilter}
            onChange={(e) => setSeverityFilter(e.target.value as Severity | "all")}
            aria-label="Filter by severity"
          >
            <option value="all">All Severities</option>
            {SEVERITY_ORDER.map((sev) => (
              <option key={sev} value={sev}>
                {sev}
              </option>
            ))}
          </select>

          <select
            className="filter-select"
            value={classFilter}
            onChange={(e) => setClassFilter(e.target.value)}
            aria-label="Filter by issue class"
          >
            <option value="all">All 11 Classes</option>
            {classOptions.map((cls) => (
              <option key={cls} value={cls}>
                {cls}
              </option>
            ))}
          </select>

          <select
            className="filter-select"
            value={personaFilter}
            onChange={(e) => setPersonaFilter(e.target.value)}
            aria-label="Filter by persona"
          >
            <option value="all">All Personas</option>
            {personaIds.map((pid) => (
              <option key={pid} value={pid}>
                {pid}
              </option>
            ))}
          </select>
        </div>

        {filteredIssues.length === 0 ? (
          <p className="muted-empty">
            {summary.issues.length === 0
              ? "Zero issues reported in this run."
              : "No issues match the current filters."}
          </p>
        ) : (
          <div className="issues-accordion">
            {filteredIssues.map(({ issue, originalIdx }) => {
              const isOpen = Boolean(expandedRows[originalIdx]);
              return (
                <div key={originalIdx} className={`issue-group-row ${isOpen ? "is-open" : ""}`}>
                  <button
                    type="button"
                    className="issue-group-trigger"
                    aria-expanded={isOpen}
                    onClick={() => toggleExpand(originalIdx)}
                  >
                    <span className="issue-chevron" aria-hidden="true">
                      {isOpen ? (
                        <ChevronDownIcon width={14} height={14} />
                      ) : (
                        <ChevronRightIcon width={14} height={14} />
                      )}
                    </span>

                    <span className={`sev-pill sev-${issue.severity}`}>{issue.severity}</span>

                    <span className="issue-group-main">
                      <span className="issue-group-title">{issue.title}</span>
                      <span className="issue-group-sub">
                        <span className="class-pill">{issue.class || "other"}</span>
                        {issue.screens && issue.screens.length > 0 && (
                          <span className="issue-screens-text">
                            {issue.screens.join(", ")}
                          </span>
                        )}
                      </span>
                    </span>

                    <span className="issue-group-personas">
                      {issue.personas.map((pid) => (
                        <span key={pid} className="persona-tag-chip">
                          {pid}
                        </span>
                      ))}
                    </span>
                  </button>

                  {isOpen && (
                    <div className="issue-raw-reports">
                      {issue.reports.map((rep) => (
                        <div key={`${rep.id}-${rep.persona}`} className="raw-report-item">
                          <div className="raw-report-head">
                            <button
                              type="button"
                              className="persona-link-btn"
                              onClick={() => onJumpToPersonaStep(rep.persona, null)}
                            >
                              {rep.persona}
                            </button>
                            <span className={`sev-pill sev-${rep.severity}`}>{rep.severity}</span>
                            {rep.screen && <span className="meta-chip">{rep.screen}</span>}
                            <span className="raw-report-title">{rep.title}</span>
                          </div>

                          {rep.screens && rep.screens.length > 0 && (
                            <div className="raw-report-shots">
                              <span className="shots-label">Jump to screenshot:</span>
                              {rep.screens.map((scr, sIdx) => {
                                const stepNum = parseStepNumber(scr);
                                return stepNum ? (
                                  <button
                                    key={`${scr}-${sIdx}`}
                                    type="button"
                                    className="screen-jump-chip tabular-nums"
                                    onClick={() =>
                                      onJumpToPersonaStep(rep.persona, stepNum, true)
                                    }
                                    title={`Open ${rep.persona} at Step #${String(stepNum).padStart(2, "0")} with A11y overlay`}
                                  >
                                    {rep.persona} #{String(stepNum).padStart(2, "0")}
                                  </button>
                                ) : (
                                  <span key={`${scr}-${sIdx}`} className="meta-chip">
                                    {scr}
                                  </span>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Mode-Specific: Walkthrough & Improvements */}
      {summary.walkthrough && (
        <div className="walkthrough-grid">
          {summary.walkthrough.rows.length > 0 && (
            <div className="panel-card">
              <div className="panel-card-head">
                <h3>UX Walkthrough Results</h3>
              </div>
              <div className="table-scroll">
                <table className="qa-table">
                  <thead>
                    <tr>
                      <th>Persona</th>
                      <th>Goal</th>
                      <th className="num-col">Actions</th>
                      <th className="num-col">Score</th>
                      <th>Device</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.walkthrough.rows.map((row, idx) => (
                      <tr key={`${row.persona}-${idx}`}>
                        <td>
                          <button
                            type="button"
                            className="persona-link-btn"
                            onClick={() => onJumpToPersonaStep(row.persona, null)}
                          >
                            {row.persona}
                          </button>
                        </td>
                        <td>
                          <span className={`verdict-chip verdict-${row.goal_reached}`}>
                            {row.goal_reached}
                          </span>
                        </td>
                        <td className="num-col tabular-nums">{row.actions ?? "—"}</td>
                        <td className="num-col tabular-nums">
                          {row.score !== null && row.score !== undefined ? `${row.score}/10` : "—"}
                        </td>
                        <td>{row.device}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {summary.walkthrough.areas.length > 0 && (
            <div className="panel-card">
              <div className="panel-card-head">
                <h3>Improvements by Area</h3>
              </div>
              <div className="area-bars">
                {summary.walkthrough.areas.map((a) => (
                  <div key={a.area} className="area-bar-row">
                    <span className="area-bar-name">{a.area}</span>
                    <div className="area-bar-track">
                      <div
                        className="area-bar-fill"
                        style={{ width: `${Math.round((a.count / maxAreaCount) * 100)}%` }}
                      />
                    </div>
                    <span className="area-bar-meta tabular-nums">
                      {a.count} ({a.persona_count}p)
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {summary.walkthrough.improvements.length > 0 && (
            <div className="panel-card panel-card-full">
              <div className="panel-card-head">
                <h3>Suggested Improvements ({summary.walkthrough.improvements.length})</h3>
              </div>
              <div className="table-scroll">
                <table className="qa-table">
                  <thead>
                    <tr>
                      <th>Impact</th>
                      <th>Area</th>
                      <th>Improvement</th>
                      <th>Persona</th>
                      <th>Screens</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.walkthrough.improvements.map((imp, idx) => (
                      <tr key={idx}>
                        <td>
                          <span className={`impact-pill impact-${imp.impact}`}>{imp.impact}</span>
                        </td>
                        <td>
                          <span className="meta-chip">{imp.area}</span>
                        </td>
                        <td className="cell-wrap">
                          <strong>{imp.title}</strong>
                          {imp.why && <div className="cell-subtext">{imp.why}</div>}
                        </td>
                        <td>
                          <button
                            type="button"
                            className="persona-link-btn"
                            onClick={() => onJumpToPersonaStep(imp.persona, null)}
                          >
                            {imp.persona}
                          </button>
                        </td>
                        <td>
                          <div className="cell-chips">
                            {(imp.screens ?? []).map((scr, sIdx) => {
                              const stepNum = parseStepNumber(scr);
                              return stepNum ? (
                                <button
                                  key={`${scr}-${sIdx}`}
                                  type="button"
                                  className="screen-jump-chip tabular-nums"
                                  onClick={() =>
                                    onJumpToPersonaStep(imp.persona, stepNum, true)
                                  }
                                >
                                  #{String(stepNum).padStart(2, "0")}
                                </button>
                              ) : (
                                <span key={`${scr}-${sIdx}`} className="meta-chip">
                                  {scr}
                                </span>
                              );
                            })}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Issue Class x Persona Breakdown */}
      {summary.classes && summary.classes.length > 0 && (
        <div className="panel-card">
          <div className="panel-card-head">
            <h3>Issue Class × Persona Breakdown</h3>
          </div>
          <div className="table-scroll">
            <table className="qa-table matrix-table">
              <thead>
                <tr>
                  <th>Issue Class</th>
                  <th className="num-col">Count</th>
                  {classMatrixPersonas.map((pid) => (
                    <th key={pid} className="num-col">
                      {pid}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {summary.classes.map((clsRow) => {
                  const perMap = classPersonaMap.get(clsRow.class);
                  return (
                    <tr key={clsRow.class}>
                      <td>
                        <button
                          type="button"
                          className="class-filter-link"
                          onClick={() =>
                            setClassFilter(classFilter === clsRow.class ? "all" : clsRow.class)
                          }
                        >
                          {clsRow.class}
                        </button>
                      </td>
                      <td className="num-col tabular-nums">
                        <strong>{clsRow.count}</strong>
                      </td>
                      {classMatrixPersonas.map((pid) => {
                        const hitCount =
                          perMap?.get(pid) ?? (clsRow.personas.includes(pid) ? 1 : 0);
                        return (
                          <td key={pid} className="num-col tabular-nums">
                            {hitCount > 0 ? (
                              <span className="matrix-hit-dot">{hitCount}</span>
                            ) : (
                              <span className="matrix-miss">·</span>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
