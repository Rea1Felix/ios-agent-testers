import { useEffect, useRef } from "react";
import { StreamIcon } from "../icons";
import { buildArtifactUrl } from "../mcpClient";
import type { StepRecord, StreamConfig } from "../types";

interface StepFilmstripProps {
  runId: string;
  personaId: string;
  steps: StepRecord[];
  isLiveAvailable: boolean;
  selectedStep: number | null; // null = Live view
  streamConfig: StreamConfig;
  onSelectStep: (step: number | null) => void;
}

export function StepFilmstrip({
  runId,
  personaId,
  steps,
  isLiveAvailable,
  selectedStep,
  streamConfig,
  onSelectStep,
}: StepFilmstripProps) {
  const activeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (activeRef.current && typeof activeRef.current.scrollIntoView === "function") {
      activeRef.current.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
        inline: "nearest",
      });
    }
  }, [selectedStep, personaId]);

  if (!isLiveAvailable && steps.length === 0) {
    return (
      <div className="filmstrip-empty">
        No step captures recorded yet for <strong>{personaId}</strong>.
      </div>
    );
  }

  const captureCount = steps.filter((s) => Boolean(s.screenshot_file)).length;
  const hasActionOnlySteps = captureCount < steps.length;

  return (
    <div className="filmstrip-shell" aria-label="Step screenshot filmstrip">
      <div className="filmstrip-header">
        <span className="filmstrip-title">Step Filmstrip</span>
        <span className="filmstrip-count tabular-nums">
          {captureCount} {captureCount === 1 ? "capture" : "captures"}
          {hasActionOnlySteps && (
            <>
              {" "}· {steps.length} {steps.length === 1 ? "step" : "steps"}
            </>
          )}
        </span>
      </div>

      <div className="filmstrip-track" role="listbox" aria-label="Captured steps">
        {isLiveAvailable && (
          <button
            ref={selectedStep === null ? activeRef : undefined}
            type="button"
            role="option"
            aria-selected={selectedStep === null}
            className={`film-tile film-tile-live ${selectedStep === null ? "is-selected" : ""}`}
            onClick={() => onSelectStep(null)}
            title="Switch to live simulator view"
          >
            <span className="film-live-indicator">
              <span className="pulse-dot" aria-hidden="true" />
              <span>LIVE</span>
            </span>
            <span className="film-tile-caption">Stream</span>
          </button>
        )}

        {steps.map((step) => {
          const isSelected = selectedStep === step.step;
          const isActionOnly = !step.screenshot_file;
          const imgUrl = isActionOnly
            ? null
            : buildArtifactUrl(
                streamConfig,
                runId,
                personaId,
                step.screenshot_file,
              );
          const unnamedCount = step.a11y?.unnamed?.length ?? 0;
          const undersizedCount = step.a11y?.undersized?.length ?? 0;
          const hasCrash = (step.crashes?.length ?? 0) > 0 || step.app_running === false;
          const stepLabel = String(step.step).padStart(2, "0");
          const actionKind = step.action?.kind || "action";

          return (
            <button
              key={step.step}
              ref={isSelected ? activeRef : undefined}
              type="button"
              role="option"
              aria-selected={isSelected}
              className={`film-tile ${isSelected ? "is-selected" : ""} ${hasCrash ? "has-crash" : ""} ${isActionOnly ? "is-action-only" : ""}`}
              onClick={() => onSelectStep(step.step)}
              title={`Step #${stepLabel}${step.action?.kind ? ` · ${step.action.kind}` : ""}${isActionOnly ? " · action only (no screenshot)" : ""}${step.ax_unavailable_reason ? " · AX unavailable" : ""}${unnamedCount > 0 ? ` · ${unnamedCount} unnamed` : ""}${undersizedCount > 0 ? ` · ${undersizedCount} <44pt` : ""}`}
            >
              <div className="film-thumb-wrap">
                {isActionOnly ? (
                  <div className="film-action-only-body">
                    <StreamIcon width={14} height={14} className="film-action-only-icon" />
                    <span className="film-action-only-kind">{actionKind}</span>
                  </div>
                ) : imgUrl ? (
                  <img
                    src={imgUrl}
                    alt={`Step ${stepLabel}`}
                    className="film-thumb-img"
                    loading="lazy"
                    draggable={false}
                  />
                ) : (
                  <span className="film-thumb-fallback tabular-nums">#{stepLabel}</span>
                )}

                <span className="film-step-num tabular-nums">{stepLabel}</span>

                {(hasCrash || unnamedCount > 0 || undersizedCount > 0) && (
                  <span className="film-dots" aria-hidden="true">
                    {hasCrash && <span className="film-dot dot-crash" />}
                    {unnamedCount > 0 && <span className="film-dot dot-unnamed" />}
                    {undersizedCount > 0 && <span className="film-dot dot-undersized" />}
                  </span>
                )}
              </div>

              <span className="film-tile-caption">
                {isActionOnly ? "action" : step.action?.kind || "observe"}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
