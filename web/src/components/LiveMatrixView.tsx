import { useEffect, useRef, useState } from "react";
import {
  AlertTriangleIcon,
  BugIcon,
  HandIcon,
  PhoneIcon,
  TabletIcon,
} from "../icons";
import { buildArtifactUrl, buildSnapshotUrl } from "../mcpClient";
import {
  formatShortDevice,
  formatTextSizeChip,
  isIpadSimulator,
  isLargeTextSize,
  isLivePersonaStatus,
  isRtlLocale,
  SEVERITY_ORDER,
  totalIssues,
  type PersonaRunState,
  type RunStateResult,
  type StreamConfig,
} from "../types";

interface LiveMatrixViewProps {
  runState: RunStateResult;
  streamConfig: StreamConfig;
  isActiveView: boolean;
  onInspectPersona: (personaId: string) => void;
}

const SNAPSHOT_POLL_MS = 1500;

interface PersonaCardProps {
  runId: string;
  persona: PersonaRunState;
  manifestMaxActions?: number;
  streamConfig: StreamConfig;
  pollEnabled: boolean;
  onInspect: () => void;
}

function PersonaCard({
  runId,
  persona,
  manifestMaxActions,
  streamConfig,
  pollEnabled,
  onInspect,
}: PersonaCardProps) {
  const isLive = isLivePersonaStatus(persona.status) && Boolean(persona.udid);
  const isIpad = isIpadSimulator(persona.device?.simulator || persona.device_label);
  const isRtl = isRtlLocale(persona.locale);
  const isLargeText = isLargeTextSize(persona.device?.text_size);
  const isDarkTheme = persona.device?.theme === "dark";

  const fallbackArtifactUrl = buildArtifactUrl(
    streamConfig,
    runId,
    persona.id,
    persona.last_screenshot,
  );

  // Offscreen-preloaded snapshot URL so running cards never blink blank between polls.
  const [liveSnapshotSrc, setLiveSnapshotSrc] = useState<string | null>(null);
  const [imgFailed, setImgFailed] = useState(false);
  const tickRef = useRef(0);

  useEffect(() => {
    setImgFailed(false);
    if (!isLive) {
      setLiveSnapshotSrc(null);
      return;
    }
    if (!pollEnabled || !persona.udid || !streamConfig.httpBaseUrl || !streamConfig.streamToken) {
      return;
    }

    let cancelled = false;
    let timerId: number | null = null;
    let inFlightImg: HTMLImageElement | null = null;

    const loadOnce = () => {
      if (cancelled) return;
      if (document.hidden) {
        timerId = window.setTimeout(loadOnce, SNAPSHOT_POLL_MS);
        return;
      }
      tickRef.current += 1;
      const url = buildSnapshotUrl(streamConfig, persona.udid, {
        maxWidth: isIpad ? 360 : 280,
        quality: 0.65,
        tick: tickRef.current,
      });
      if (!url) return;

      const probe = new Image();
      inFlightImg = probe;
      const scheduleNext = () => {
        if (cancelled) return;
        timerId = window.setTimeout(loadOnce, SNAPSHOT_POLL_MS);
      };
      probe.onload = () => {
        if (cancelled) return;
        setLiveSnapshotSrc(url);
        setImgFailed(false);
        scheduleNext();
      };
      probe.onerror = () => {
        if (cancelled) return;
        scheduleNext();
      };
      probe.src = url;
    };

    loadOnce();

    const handleVisibility = () => {
      if (!document.hidden && !cancelled) {
        if (timerId !== null) {
          window.clearTimeout(timerId);
          timerId = null;
        }
        loadOnce();
      }
    };
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      cancelled = true;
      if (timerId !== null) window.clearTimeout(timerId);
      if (inFlightImg) {
        inFlightImg.onload = null;
        inFlightImg.onerror = null;
        inFlightImg.src = "";
      }
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [
    isLive,
    pollEnabled,
    persona.udid,
    isIpad,
    streamConfig.httpBaseUrl,
    streamConfig.streamToken,
  ]);

  const previewSrc = (isLive ? liveSnapshotSrc : null) || fallbackArtifactUrl;
  const actionCount = persona.action_count ?? persona.step_count;
  const maxActions = Math.max(
    1,
    persona.max_actions || persona.max_steps || manifestMaxActions || 30,
  );
  const obsCount = persona.observation_count;
  const hasObsDiff = obsCount != null && obsCount !== actionCount;
  const progressPct = Math.min(100, Math.round((actionCount / maxActions) * 100));
  const progressTooltip = hasObsDiff
    ? `${actionCount}/${maxActions} actions · ${obsCount} captures`
    : `${actionCount}/${maxActions} actions`;
  const issueCount = totalIssues(persona.issue_counts);

  const statusLabel = (() => {
    switch (persona.status) {
      case "pending":
        return "Pending";
      case "provisioning":
        return "Provisioning";
      case "running":
        return "Running";
      case "paused":
        return "Paused";
      case "crashed":
        return "Crashed";
      case "done":
        return "Done";
      case "error":
        return "Error";
      default:
        return persona.status;
    }
  })();

  return (
    <article
      className={`persona-card status-card-${persona.status} ${persona.paused_by_human ? "is-takeover" : ""}`}
      role="button"
      tabIndex={0}
      aria-label={`Inspect persona ${persona.id} (${statusLabel}, ${actionCount} of ${maxActions} actions${hasObsDiff ? `, ${obsCount} captures` : ""})`}
      onClick={onInspect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onInspect();
        }
      }}
    >
      <div className="persona-card-preview">
        {previewSrc && !imgFailed ? (
          <img
            src={previewSrc}
            alt={`${persona.id} simulator screen`}
            className={`persona-preview-img ${isIpad ? "is-ipad" : ""}`}
            loading="lazy"
            draggable={false}
            onError={() => setImgFailed(true)}
          />
        ) : (
          <div className="persona-preview-empty">
            {persona.status === "provisioning" ? (
              <span className="spinner-ring spinner-muted" aria-hidden="true" />
            ) : isIpad ? (
              <TabletIcon width={26} height={26} />
            ) : (
              <PhoneIcon width={26} height={26} />
            )}
            <span className="preview-empty-label">
              {persona.status === "provisioning"
                ? "Booting simulator…"
                : persona.status === "pending"
                  ? "Queued"
                  : persona.status === "error"
                    ? "Provision error"
                    : "No frame yet"}
            </span>
          </div>
        )}

        {/* Top floating badges over screen preview */}
        <div className="preview-overlay-top">
          <span className={`status-pill status-${persona.status}`}>
            {(persona.status === "running" || persona.status === "provisioning") && (
              <span className="status-dot" aria-hidden="true" />
            )}
            {statusLabel}
          </span>

          {persona.score !== null && persona.score !== undefined && (
            <span
              className={`score-badge ${
                persona.score >= 8
                  ? "score-high"
                  : persona.score >= 5
                    ? "score-mid"
                    : "score-low"
              } tabular-nums`}
              title={`Experience score: ${persona.score}/10`}
            >
              {persona.score}/10
            </span>
          )}
        </div>

        {/* Takeover / Crash banners */}
        {persona.paused_by_human && (
          <div className="preview-banner preview-banner-paused">
            <HandIcon width={12} height={12} />
            <span>Human Takeover</span>
          </div>
        )}

        {!persona.paused_by_human && (persona.crash_count > 0 || persona.status === "crashed") && (
          <div className="preview-banner preview-banner-crash">
            <AlertTriangleIcon width={12} height={12} />
            <span>
              {persona.crash_count > 0
                ? `${persona.crash_count} Crash${persona.crash_count > 1 ? "es" : ""}`
                : "Crashed"}
            </span>
          </div>
        )}
      </div>

      <div className="persona-card-body">
        <div className="persona-card-head">
          <span className="persona-id">{persona.id}</span>
          <div className="persona-locale-group">
            <span className="meta-chip">{persona.locale}</span>
            {isRtl && <span className="meta-chip meta-chip-rtl">RTL</span>}
          </div>
        </div>

        <div className="persona-device-row">
          <span className="meta-chip meta-chip-device" title={persona.device_label || persona.device?.simulator}>
            {formatShortDevice(persona.device?.simulator)}
          </span>
          <span
            className={`meta-chip ${isLargeText ? "meta-chip-a11y" : ""}`}
            title={`Dynamic Type: ${persona.device?.text_size || "large"}`}
          >
            {formatTextSizeChip(persona.device?.text_size)}
          </span>
          <span
            className={`theme-dot ${isDarkTheme ? "is-dark" : "is-light"}`}
            title={`Simulator theme: ${isDarkTheme ? "Dark" : "Light"}`}
          />
        </div>

        {/* Action budget progress bar */}
        <div className="step-progress-wrap" title={progressTooltip}>
          <div className="step-progress-track" aria-hidden="true">
            <div
              className={`step-progress-fill status-fill-${persona.status}`}
              style={{ width: `${progressPct}%` }}
            />
          </div>
          <span className="step-progress-label tabular-nums">
            {actionCount}/{maxActions}
            {hasObsDiff && (
              <span className="step-progress-obs" aria-label={`${obsCount} captures`}>
                {" "}· {obsCount} cap
              </span>
            )}
          </span>
        </div>

        {/* Footer: severity chips + mode verdict */}
        <div className="persona-card-footer">
          <div className="severity-chip-row">
            {issueCount === 0 ? (
              <span className="sev-none">
                {persona.status === "done" ? "0 issues" : "—"}
              </span>
            ) : (
              SEVERITY_ORDER.map((sev) => {
                const count = persona.issue_counts?.[sev] ?? 0;
                if (count <= 0) return null;
                return (
                  <span
                    key={sev}
                    className={`sev-chip sev-${sev} tabular-nums`}
                    title={`${count} ${sev}`}
                  >
                    {sev[0].toUpperCase()}:{count}
                  </span>
                );
              })
            )}
          </div>

          {persona.reproduced && (
            <span className={`verdict-chip verdict-${persona.reproduced}`} title="Bug reproduction verdict">
              <BugIcon width={11} height={11} />
              {persona.reproduced}
            </span>
          )}

          {!persona.reproduced && persona.goal_reached && (
            <span className={`verdict-chip verdict-${persona.goal_reached}`} title="Walkthrough goal">
              goal:{persona.goal_reached}
            </span>
          )}
        </div>

        {persona.error && (
          <p className="persona-error-line" title={persona.error}>
            {persona.error}
          </p>
        )}
      </div>
    </article>
  );
}

export function LiveMatrixView({
  runState,
  streamConfig,
  isActiveView,
  onInspectPersona,
}: LiveMatrixViewProps) {
  const personas = runState.personas ?? [];
  const manifest = runState.manifest;
  const runId = runState.run_id ?? manifest?.run_id ?? "";

  const liveCount = personas.filter((p) => isLivePersonaStatus(p.status)).length;
  const doneCount = personas.filter((p) => p.status === "done").length;
  const issueSum = personas.reduce((acc, p) => acc + totalIssues(p.issue_counts), 0);

  return (
    <section className="view-scroll matrix-view" aria-label="Persona Live Matrix">
      <div className="matrix-meta-strip">
        <div className="matrix-meta-left">
          <span className="meta-app-id" title={manifest?.app_path || manifest?.app_id || ""}>
            {manifest?.app_id || "Unknown App"}
          </span>
          {manifest?.bug_report_title && (
            <span className="meta-bug-title" title={manifest.bug_report_title}>
              <BugIcon width={12} height={12} />
              <span>{manifest.bug_report_title}</span>
            </span>
          )}
        </div>
        <div className="matrix-meta-stats tabular-nums">
          <span>{liveCount} live</span>
          <span className="meta-sep">·</span>
          <span>{doneCount} done</span>
          <span className="meta-sep">·</span>
          <span>{issueSum} raw findings</span>
        </div>
      </div>

      {personas.length === 0 ? (
        <div className="empty-card">
          <PhoneIcon width={28} height={28} className="empty-icon" />
          <h3>No Personas in This Run</h3>
          <p>Waiting for personas to be registered for run <code>{runId}</code>.</p>
        </div>
      ) : (
        <div className="persona-grid">
          {personas.map((persona) => (
            <PersonaCard
              key={persona.id}
              runId={runId}
              persona={persona}
              manifestMaxActions={manifest?.max_actions}
              streamConfig={streamConfig}
              pollEnabled={isActiveView}
              onInspect={() => onInspectPersona(persona.id)}
            />
          ))}
        </div>
      )}
    </section>
  );
}
