import { useEffect, useState } from "react";
import { HandIcon, PauseIcon, PlayIcon, SparkleInspectIcon } from "../icons";
import type { HostCapabilitiesSummary, PersonaRunState } from "../types";

interface TakeoverBarProps {
  persona: PersonaRunState;
  isLiveAvailable: boolean;
  busyAction: string | null;
  capabilities: HostCapabilitiesSummary;
  onToggleTakeover: (paused: boolean, note?: string) => Promise<void>;
  onAskAgentInspect: (note?: string) => Promise<void>;
}

export function TakeoverBar({
  persona,
  isLiveAvailable,
  busyAction,
  capabilities,
  onToggleTakeover,
  onAskAgentInspect,
}: TakeoverBarProps) {
  const [note, setNote] = useState(persona.human_note ?? "");

  // Sync note when switching personas or when server updates human_note.
  useEffect(() => {
    setNote(persona.human_note ?? "");
  }, [persona.id, persona.human_note]);

  const isPaused = Boolean(persona.paused_by_human || persona.status === "paused");
  const isBusy = busyAction === "takeover" || busyAction === "inspect";
  const canHandoff = capabilities.canSendMessage || capabilities.canUpdateModelContext;

  const handleToggle = async () => {
    const nextPaused = !isPaused;
    await onToggleTakeover(nextPaused, note.trim() || undefined);
    if (!nextPaused) {
      setNote("");
    }
  };

  return (
    <div
      className={`takeover-bar ${isPaused ? "is-paused" : ""}`}
      role="region"
      aria-label="Human takeover and agent handoff controls"
    >
      <div className="takeover-primary-row">
        <button
          type="button"
          className={`takeover-btn ${isPaused ? "is-resume" : "is-pause"}`}
          disabled={!isLiveAvailable || isBusy}
          onClick={() => void handleToggle()}
          title={
            !isLiveAvailable
              ? "Persona simulator is not currently running"
              : isPaused
                ? "Resume autonomous QA agent (passes steering note if entered)"
                : "Pause QA agent so you can tap/swipe directly on the simulator"
          }
        >
          {isPaused ? (
            <>
              <PlayIcon width={13} height={13} />
              <span>{busyAction === "takeover" ? "Resuming…" : "Resume Agent"}</span>
            </>
          ) : (
            <>
              <PauseIcon width={13} height={13} />
              <span>{busyAction === "takeover" ? "Pausing…" : "Pause Agent & Take Over"}</span>
            </>
          )}
        </button>

        {isPaused && (
          <span className="takeover-active-pill">
            <HandIcon width={12} height={12} />
            <span>Direct Control Active</span>
          </span>
        )}

        {canHandoff && (
          <button
            type="button"
            className="inspect-handoff-btn"
            disabled={isBusy}
            onClick={() => void onAskAgentInspect(note.trim() || undefined)}
            title={
              capabilities.canSendMessage
                ? "Attach current screen/step context and ask the agent in chat to inspect"
                : "Attach current screen/step context to the agent's next turn"
            }
          >
            <SparkleInspectIcon width={13} height={13} />
            <span>
              {busyAction === "inspect"
                ? "Sending…"
                : capabilities.canSendMessage
                  ? "Ask Agent to Inspect"
                  : "Attach Context"}
            </span>
          </button>
        )}
      </div>

      <div className="takeover-note-row">
        <input
          type="text"
          className="takeover-note-input"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder={
            isPaused
              ? "Optional steering note for agent on resume (e.g. 'Signed in, test checkout')…"
              : "Optional human steering note (sent on pause/resume or inspect)…"
          }
          aria-label="Human steering note"
          disabled={isBusy}
          onKeyDown={(e) => {
            if (e.key === "Enter" && isLiveAvailable && !isBusy) {
              e.preventDefault();
              void onToggleTakeover(isPaused, note.trim() || undefined);
            }
          }}
        />
        {isPaused && note.trim().length > 0 && isLiveAvailable && (
          <button
            type="button"
            className="takeover-save-note-btn"
            disabled={isBusy}
            onClick={() => void onToggleTakeover(true, note.trim())}
            title="Save steering note while staying paused"
          >
            Save Note
          </button>
        )}
      </div>
    </div>
  );
}
