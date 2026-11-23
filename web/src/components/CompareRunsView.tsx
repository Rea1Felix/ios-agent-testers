import { useCallback, useEffect, useState } from "react";
import {
  CheckCircleIcon,
  CompareIcon,
  FileMarkdownIcon,
  SwapIcon,
} from "../icons";
import type { QaMcpBridge } from "../mcpClient";
import {
  formatShortTimestamp,
  SEVERITY_ORDER,
  totalIssues,
  type CompareToolResult,
  type RunSummary,
  type ToastItem,
} from "../types";

interface CompareRunsViewProps {
  runs: RunSummary[];
  activeRunId: string | null;
  bridge: QaMcpBridge;
  onOpenMarkdown: (absPath: string) => void;
  onToast: (message: string, tone?: ToastItem["tone"]) => void;
}

function formatSignedDelta(delta: number | null, precision = 0): string {
  if (delta === null || !Number.isFinite(delta)) return "—";
  if (delta === 0) return "0";
  const formatted = precision > 0 ? Math.abs(delta).toFixed(precision) : String(Math.abs(delta));
  return delta > 0 ? `+${formatted}` : `-${formatted}`;
}

export function CompareRunsView({
  runs,
  activeRunId,
  bridge,
  onOpenMarkdown,
  onToast,
}: CompareRunsViewProps) {
  const [runA, setRunA] = useState<string>("");
  const [runB, setRunB] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<CompareToolResult | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Initialize sensible defaults: Run A = previous run, Run B = newest / active run.
  useEffect(() => {
    if (runs.length < 2) return;
    setRunB((prevB) => {
      if (prevB && runs.some((r) => r.run_id === prevB)) return prevB;
      return activeRunId && runs.some((r) => r.run_id === activeRunId)
        ? activeRunId
        : runs[0].run_id;
    });
    setRunA((prevA) => {
      if (prevA && runs.some((r) => r.run_id === prevA)) return prevA;
      const targetB =
        activeRunId && runs.some((r) => r.run_id === activeRunId)
          ? activeRunId
          : runs[0].run_id;
      const other = runs.find((r) => r.run_id !== targetB) ?? runs[1];
      return other.run_id;
    });
  }, [runs, activeRunId]);

  const executeCompare = useCallback(
    async (idA: string, idB: string) => {
      if (!idA || !idB || idA === idB || !bridge.isConnected()) return;
      setLoading(true);
      setErrorMsg(null);
      try {
        const res = await bridge.compareRuns(idA, idB, idA, idB);
        if (res.ok === false) {
          throw new Error(res.error || res.message || "Comparison failed");
        }
        setResult(res);
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Failed to compare runs";
        setErrorMsg(msg);
        onToast(`Compare failed: ${msg}`, "danger");
      } finally {
        setLoading(false);
      }
    },
    [bridge, onToast],
  );

  // Auto-run comparison when Run A and Run B are distinct valid runs.
  useEffect(() => {
    if (runA && runB && runA !== runB && bridge.isConnected()) {
      void executeCompare(runA, runB);
    }
  }, [runA, runB, bridge, executeCompare]);

  if (runs.length < 2) {
    return (
      <section className="view-scroll compare-view" aria-label="Compare QA Runs">
        <div className="empty-card">
          <CompareIcon width={30} height={30} className="empty-icon" />
          <h3>Need at Least Two QA Runs</h3>
          <p>
            Run a second QA pass after shipping fixes, then compare Run A (baseline) vs Run B
            (candidate) to inspect score deltas, resolved bugs, and regressions.
          </p>
        </div>
      </section>
    );
  }

  const cmp = result?.compare ?? null;
  const ov = cmp?.overview ?? null;

  const avgDelta =
    ov && ov.avg_score[0] !== null && ov.avg_score[1] !== null
      ? Number((ov.avg_score[1] - ov.avg_score[0]).toFixed(2))
      : null;

  const totalIssuesA = ov ? totalIssues(ov.issues[0]) : 0;
  const totalIssuesB = ov ? totalIssues(ov.issues[1]) : 0;
  const totalIssuesDelta = totalIssuesB - totalIssuesA;

  const hasGoals = Boolean(
    ov &&
      (ov.goal_reached[0].yes + ov.goal_reached[0].partly + ov.goal_reached[0].no > 0 ||
        ov.goal_reached[1].yes + ov.goal_reached[1].partly + ov.goal_reached[1].no > 0),
  );

  return (
    <section className="view-scroll compare-view" aria-label="Compare QA Runs">
      {/* Selector Bar */}
      <div className="compare-selector-bar">
        <div className="compare-pick-group">
          <label className="compare-pick-label" htmlFor="compare-run-a">
            Baseline (A)
          </label>
          <select
            id="compare-run-a"
            className="filter-select compare-select"
            value={runA}
            onChange={(e) => setRunA(e.target.value)}
          >
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.run_id} · {r.mode} ({formatShortTimestamp(r.created_at)})
              </option>
            ))}
          </select>
        </div>

        <button
          type="button"
          className="hdr-icon-btn compare-swap-btn"
          title="Swap Run A and Run B"
          aria-label="Swap Run A and Run B"
          onClick={() => {
            const prevA = runA;
            setRunA(runB);
            setRunB(prevA);
          }}
        >
          <SwapIcon width={15} height={15} />
        </button>

        <div className="compare-pick-group">
          <label className="compare-pick-label" htmlFor="compare-run-b">
            Candidate (B)
          </label>
          <select
            id="compare-run-b"
            className="filter-select compare-select"
            value={runB}
            onChange={(e) => setRunB(e.target.value)}
          >
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.run_id} · {r.mode} ({formatShortTimestamp(r.created_at)})
              </option>
            ))}
          </select>
        </div>

        <div className="compare-actions">
          <button
            type="button"
            className="primary-cta compare-go-btn"
            disabled={loading || !runA || !runB || runA === runB}
            onClick={() => void executeCompare(runA, runB)}
          >
            {loading ? "Comparing…" : "Compare"}
          </button>

          {result?.compare_path && (
            <button
              type="button"
              className="hdr-btn"
              onClick={() => onOpenMarkdown(result.compare_path!)}
              title="Open COMPARE.md in IDE side pane"
            >
              <FileMarkdownIcon width={14} height={14} />
              <span>COMPARE.md</span>
            </button>
          )}
        </div>
      </div>

      {runA && runB && runA === runB && (
        <div className="info-banner">Select two different runs to view deltas.</div>
      )}

      {errorMsg && <div className="error-banner">{errorMsg}</div>}

      {loading && !cmp && (
        <div className="drawer-loading">
          <span className="spinner-ring spinner-muted" aria-hidden="true" />
          <span>Computing cross-run comparison…</span>
        </div>
      )}

      {cmp && ov && (
        <>
          {/* Overview KPI Deltas */}
          <div className="kpi-grid">
            <div className="kpi-card">
              <span className="kpi-label">Personas</span>
              <span className="kpi-value tabular-nums">
                {ov.personas[0]} → {ov.personas[1]}
              </span>
            </div>

            <div className="kpi-card">
              <span className="kpi-label">Avg Score</span>
              <div className="kpi-delta-line tabular-nums">
                <span className="kpi-value">
                  {ov.avg_score[0] !== null ? ov.avg_score[0].toFixed(1) : "—"} →{" "}
                  {ov.avg_score[1] !== null ? ov.avg_score[1].toFixed(1) : "—"}
                </span>
                {avgDelta !== null && (
                  <span
                    className={`delta-pill ${
                      avgDelta > 0 ? "delta-good" : avgDelta < 0 ? "delta-bad" : "delta-flat"
                    }`}
                  >
                    {formatSignedDelta(avgDelta, 1)}
                  </span>
                )}
              </div>
            </div>

            <div className="kpi-card">
              <span className="kpi-label">Total Issues</span>
              <div className="kpi-delta-line tabular-nums">
                <span className="kpi-value">
                  {totalIssuesA} → {totalIssuesB}
                </span>
                <span
                  className={`delta-pill ${
                    totalIssuesDelta < 0
                      ? "delta-good"
                      : totalIssuesDelta > 0
                        ? "delta-bad"
                        : "delta-flat"
                  }`}
                >
                  {formatSignedDelta(totalIssuesDelta)}
                </span>
              </div>
            </div>

            {SEVERITY_ORDER.map((sev) => {
              const a = ov.issues[0]?.[sev] ?? 0;
              const b = ov.issues[1]?.[sev] ?? 0;
              const d = b - a;
              return (
                <div key={sev} className={`kpi-card kpi-sev-${sev}`}>
                  <span className="kpi-label">{sev}</span>
                  <div className="kpi-delta-line tabular-nums">
                    <span className="kpi-value">
                      {a} → {b}
                    </span>
                    <span
                      className={`delta-pill ${
                        d < 0 ? "delta-good" : d > 0 ? "delta-bad" : "delta-flat"
                      }`}
                    >
                      {formatSignedDelta(d)}
                    </span>
                  </div>
                </div>
              );
            })}

            {hasGoals && (
              <div className="kpi-card kpi-card-wide">
                <span className="kpi-label">Goal Reached (Yes / Partly / No)</span>
                <div className="kpi-verdict-row tabular-nums">
                  <span className="meta-chip">
                    A: {ov.goal_reached[0].yes}/{ov.goal_reached[0].partly}/{ov.goal_reached[0].no}
                  </span>
                  <span>→</span>
                  <span className="meta-chip">
                    B: {ov.goal_reached[1].yes}/{ov.goal_reached[1].partly}/{ov.goal_reached[1].no}
                  </span>
                </div>
              </div>
            )}

            {(ov.median_actions[0] !== null || ov.median_actions[1] !== null) && (
              <div className="kpi-card">
                <span className="kpi-label">Median Actions</span>
                <span className="kpi-value tabular-nums">
                  {ov.median_actions[0] ?? "—"} → {ov.median_actions[1] ?? "—"}
                </span>
              </div>
            )}
          </div>

          {/* Persona-by-Persona Delta Table */}
          <div className="panel-card">
            <div className="panel-card-head">
              <h3>Persona-by-Persona Deltas</h3>
              <div className="score-summary-chips tabular-nums">
                <span className="delta-pill delta-good">↑ {cmp.score_summary.higher} higher</span>
                <span className="delta-pill delta-bad">↓ {cmp.score_summary.lower} lower</span>
                <span className="delta-pill delta-flat">= {cmp.score_summary.same} same</span>
              </div>
            </div>

            <div className="table-scroll">
              <table className="qa-table">
                <thead>
                  <tr>
                    <th>Persona</th>
                    <th className="num-col">Score A → B</th>
                    <th className="num-col">Δ</th>
                    <th className="num-col">Issues A → B</th>
                    {hasGoals && <th>Goal A → B</th>}
                    {cmp.personas.some((p) => p.actions_a !== null || p.actions_b !== null) && (
                      <th className="num-col">Actions</th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {cmp.personas.map((row) => {
                    const issDelta = row.issues_b - row.issues_a;
                    return (
                      <tr key={row.persona}>
                        <td>
                          <strong>{row.persona}</strong>
                        </td>
                        <td className="num-col tabular-nums">
                          {row.score_a ?? "—"} → {row.score_b ?? "—"}
                        </td>
                        <td className="num-col tabular-nums">
                          <span
                            className={`delta-pill ${
                              row.delta !== null && row.delta > 0
                                ? "delta-good"
                                : row.delta !== null && row.delta < 0
                                  ? "delta-bad"
                                  : "delta-flat"
                            }`}
                          >
                            {formatSignedDelta(row.delta)}
                          </span>
                        </td>
                        <td className="num-col tabular-nums">
                          {row.issues_a} → {row.issues_b}{" "}
                          <span
                            className={`delta-inline ${
                              issDelta < 0
                                ? "text-good"
                                : issDelta > 0
                                  ? "text-bad"
                                  : "text-muted"
                            }`}
                          >
                            ({formatSignedDelta(issDelta)})
                          </span>
                        </td>
                        {hasGoals && (
                          <td>
                            {row.goal_a || "—"} → {row.goal_b || "—"}
                          </td>
                        )}
                        {cmp.personas.some((p) => p.actions_a !== null || p.actions_b !== null) && (
                          <td className="num-col tabular-nums">
                            {row.actions_a ?? "—"} → {row.actions_b ?? "—"}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {(cmp.only_a_personas.length > 0 || cmp.only_b_personas.length > 0) && (
              <div className="unmatched-personas-row">
                {cmp.only_a_personas.length > 0 && (
                  <span className="meta-chip">
                    Only in A: {cmp.only_a_personas.join(", ")}
                  </span>
                )}
                {cmp.only_b_personas.length > 0 && (
                  <span className="meta-chip">
                    Only in B: {cmp.only_b_personas.join(", ")}
                  </span>
                )}
              </div>
            )}

            <p className="caveat-note">
              Note: Single-run LLM persona scores can fluctuate by ±1 point; focus on severity counts
              and unique issue diffs for release decisions.
            </p>
          </div>

          {/* Unique Issues Diff: Only in A (Fixed) vs Only in B (New/Regressions) */}
          <div className="compare-diff-grid">
            <div className="panel-card diff-card-fixed">
              <div className="panel-card-head">
                <h3>
                  <CheckCircleIcon width={14} height={14} /> Only in A — Resolved (
                  {cmp.only_in_a.length})
                </h3>
              </div>
              {cmp.only_in_a.length === 0 ? (
                <p className="muted-empty">No issues uniquely present in Run A.</p>
              ) : (
                <ul className="diff-issue-list">
                  {cmp.only_in_a.map((iss, idx) => (
                    <li key={idx} className="diff-issue-item">
                      <span className={`sev-pill sev-${iss.severity}`}>{iss.severity}</span>
                      <span className="diff-issue-title">{iss.title}</span>
                      <span className="diff-issue-personas">
                        {iss.personas.map((p) => (
                          <span key={p} className="persona-tag-chip">
                            {p}
                          </span>
                        ))}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="panel-card diff-card-new">
              <div className="panel-card-head">
                <h3>Only in B — New / Regressions ({cmp.only_in_b.length})</h3>
              </div>
              {cmp.only_in_b.length === 0 ? (
                <p className="muted-empty">No newly introduced issues in Run B.</p>
              ) : (
                <ul className="diff-issue-list">
                  {cmp.only_in_b.map((iss, idx) => (
                    <li key={idx} className="diff-issue-item">
                      <span className={`sev-pill sev-${iss.severity}`}>{iss.severity}</span>
                      <span className="diff-issue-title">{iss.title}</span>
                      <span className="diff-issue-personas">
                        {iss.personas.map((p) => (
                          <span key={p} className="persona-tag-chip">
                            {p}
                          </span>
                        ))}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          {/* Improvement Area Shifts */}
          {cmp.improvements_by_area && cmp.improvements_by_area.length > 0 && (
            <div className="panel-card">
              <div className="panel-card-head">
                <h3>Improvement Area Shifts</h3>
              </div>
              <div className="table-scroll">
                <table className="qa-table">
                  <thead>
                    <tr>
                      <th>UX Area</th>
                      <th className="num-col">Run A</th>
                      <th className="num-col">Run B</th>
                      <th className="num-col">Δ</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cmp.improvements_by_area.map((row) => {
                      const d = row.count_b - row.count_a;
                      return (
                        <tr key={row.area}>
                          <td>
                            <span className="meta-chip">{row.area}</span>
                          </td>
                          <td className="num-col tabular-nums">{row.count_a}</td>
                          <td className="num-col tabular-nums">{row.count_b}</td>
                          <td className="num-col tabular-nums">
                            <span
                              className={`delta-pill ${
                                d < 0 ? "delta-good" : d > 0 ? "delta-bad" : "delta-flat"
                              }`}
                            >
                              {formatSignedDelta(d)}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
