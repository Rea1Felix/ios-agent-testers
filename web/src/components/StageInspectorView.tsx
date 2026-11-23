import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeftIcon } from "../icons";
import { buildArtifactUrl, buildStreamUrl, type QaMcpBridge } from "../mcpClient";
import {
  formatShortDevice,
  formatTextSizeChip,
  isLargeTextSize,
  isLivePersonaStatus,
  isRtlLocale,
  type FallbackReason,
  type HostCapabilitiesSummary,
  type PersonaDetailResult,
  type PersonaRunState,
  type PhoneKey,
  type StreamConfig,
  type StreamMode,
  type ToastItem,
} from "../types";
import { ActionToolbar } from "./ActionToolbar";
import { KeyboardBar } from "./KeyboardBar";
import { PersonaReportDrawer } from "./PersonaReportDrawer";
import { PhoneStage } from "./PhoneStage";
import { StepFilmstrip } from "./StepFilmstrip";
import { TakeoverBar } from "./TakeoverBar";

interface StageInspectorViewProps {
  runId: string;
  personas: PersonaRunState[];
  activePersonaId: string | null;
  detail: PersonaDetailResult | null;
  loadingDetail: boolean;
  streamConfig: StreamConfig;
  capabilities: HostCapabilitiesSummary;
  bridge: QaMcpBridge;
  requestedStep: number | null;
  requestedA11yOn: boolean;
  requestNonce: number;
  onSelectPersona: (personaId: string) => void;
  onBackToMatrix: () => void;
  onRefreshState: () => Promise<void>;
  onOpenMarkdown: (absPath: string) => void;
  onToast: (message: string, tone?: ToastItem["tone"]) => void;
}

const POLL_BASE_INTERVAL_MS = 260;

export function StageInspectorView({
  runId,
  personas,
  activePersonaId,
  detail,
  loadingDetail,
  streamConfig,
  capabilities,
  bridge,
  requestedStep,
  requestedA11yOn,
  requestNonce,
  onSelectPersona,
  onBackToMatrix,
  onRefreshState,
  onOpenMarkdown,
  onToast,
}: StageInspectorViewProps) {
  const persona = useMemo(() => {
    if (!personas.length) return null;
    if (activePersonaId) {
      return personas.find((p) => p.id === activePersonaId) ?? personas[0];
    }
    return personas[0];
  }, [personas, activePersonaId]);

  const isLiveAvailable = Boolean(
    persona && isLivePersonaStatus(persona.status) && persona.udid,
  );

  // null = Live stream view; number = archived step number
  const [selectedStepNum, setSelectedStepNum] = useState<number | null>(null);
  const [showA11yOverlay, setShowA11yOverlay] = useState(false);
  const [highlightedA11yKey, setHighlightedA11yKey] = useState<string | null>(null);

  const [streamMode, setStreamMode] = useState<StreamMode>("stream");
  const [fallbackReason, setFallbackReason] = useState<FallbackReason>("none");
  const [streamNonce, setStreamNonce] = useState(1);
  const [pollFrameSrc, setPollFrameSrc] = useState<string | null>(null);

  const [keyboardOpen, setKeyboardOpen] = useState(false);
  const [simDarkMode, setSimDarkMode] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const pollBurstRef = useRef(0);
  const wakePollRef = useRef<(() => void) | null>(null);
  const prevPersonaIdRef = useRef<string | null>(null);

  const steps = detail?.persona_id === persona?.id ? detail?.steps ?? [] : [];

  // Respond to explicit step jump requests (e.g. from SummaryReportView screenshot chip).
  useEffect(() => {
    if (requestNonce === 0) return;
    if (requestedStep !== null) {
      setSelectedStepNum(requestedStep);
      setShowA11yOverlay(requestedA11yOn);
      setHighlightedA11yKey(null);
    }
  }, [requestedStep, requestedA11yOn, requestNonce]);

  // Reset per-persona local state when switching personas.
  useEffect(() => {
    if (!persona) return;
    if (prevPersonaIdRef.current !== persona.id) {
      prevPersonaIdRef.current = persona.id;
      setSimDarkMode(persona.device?.theme === "dark");
      setPollFrameSrc(null);
      setHighlightedA11yKey(null);
      setStreamMode(streamConfig.httpBaseUrl && streamConfig.streamToken ? "stream" : "poll");
      setFallbackReason(
        streamConfig.httpBaseUrl && streamConfig.streamToken ? "none" : "no-stream",
      );
      setStreamNonce((n) => n + 1);
      // Only reset selectedStepNum to Live if no explicit requestedStep is active for this persona.
      if (requestedStep === null) {
        setSelectedStepNum(null);
      }
    }
  }, [persona?.id, persona?.device?.theme, streamConfig.httpBaseUrl, streamConfig.streamToken, requestedStep]);

  // If persona is not live (e.g. done/crashed) and selectedStepNum is null, auto-select
  // the last step with a screenshot (falling back to the final step) so an action-only
  // final step never displays a blank phone screen.
  const effectiveArchivedStep = useMemo(() => {
    if (steps.length === 0) return null;
    if (selectedStepNum !== null) {
      return steps.find((s) => s.step === selectedStepNum) ?? steps[steps.length - 1];
    }
    if (!isLiveAvailable) {
      for (let i = steps.length - 1; i >= 0; i -= 1) {
        if (Boolean(steps[i].screenshot_file)) {
          return steps[i];
        }
      }
      return steps[steps.length - 1];
    }
    return null;
  }, [steps, selectedStepNum, isLiveAvailable]);

  const isViewingArchived = Boolean(selectedStepNum !== null || !isLiveAvailable);

  const liveStreamUrl = useMemo(() => {
    if (!isLiveAvailable || !persona?.udid) return null;
    return buildStreamUrl(streamConfig, persona.udid, {
      fps: 24,
      scale: 0.5,
      nonce: streamNonce,
    });
  }, [isLiveAvailable, persona?.udid, streamConfig, streamNonce]);

  const archivedImageUrl = useMemo(() => {
    if (
      !isViewingArchived ||
      !persona ||
      !effectiveArchivedStep ||
      !effectiveArchivedStep.screenshot_file
    ) {
      return null;
    }
    return buildArtifactUrl(
      streamConfig,
      runId,
      persona.id,
      effectiveArchivedStep.screenshot_file,
    );
  }, [isViewingArchived, persona, effectiveArchivedStep, streamConfig, runId]);

  const triggerPollBurst = useCallback(() => {
    pollBurstRef.current = 4;
    wakePollRef.current?.();
  }, []);

  // Plan B strictly serialized polling loop (`app_qa_frame`) when live & streamMode === "poll".
  useEffect(() => {
    if (
      !bridge.isConnected() ||
      !persona?.udid ||
      !isLiveAvailable ||
      isViewingArchived ||
      streamMode !== "poll"
    ) {
      return;
    }

    let cancelled = false;
    let timerId: number | null = null;
    let consecutiveErrors = 0;
    let wakePending: (() => void) | null = null;
    const targetUdid = persona.udid;

    const sleepInterruptible = (ms: number) =>
      new Promise<void>((resolve) => {
        if (cancelled) {
          resolve();
          return;
        }
        const finish = () => {
          if (timerId !== null) {
            window.clearTimeout(timerId);
            timerId = null;
          }
          wakePending = null;
          resolve();
        };
        wakePending = finish;
        timerId = window.setTimeout(finish, ms);
      });

    wakePollRef.current = () => wakePending?.();

    const handleVisibility = () => {
      if (!document.hidden) wakePending?.();
    };
    document.addEventListener("visibilitychange", handleVisibility);

    const loop = async () => {
      while (!cancelled) {
        if (document.hidden) {
          await sleepInterruptible(1000);
          continue;
        }
        try {
          const frame = await bridge.fetchFrame({
            udid: targetUdid,
            maxWidth: 480,
            quality: 0.72,
          });
          if (cancelled) break;
          const b64 = frame?.data || frame?.base64;
          if (frame && frame.ok !== false && b64) {
            consecutiveErrors = 0;
            const mime = frame.mimeType || "image/jpeg";
            setPollFrameSrc(`data:${mime};base64,${b64}`);
          } else if (frame && frame.ok === false) {
            consecutiveErrors += 1;
          }
        } catch {
          consecutiveErrors += 1;
        }

        if (cancelled) break;

        let delay = POLL_BASE_INTERVAL_MS;
        if (consecutiveErrors > 0) {
          delay = Math.min(2000, 450 * Math.pow(1.6, Math.min(consecutiveErrors - 1, 4)));
        } else if (pollBurstRef.current > 0) {
          const burst = pollBurstRef.current;
          pollBurstRef.current -= 1;
          delay = burst >= 4 ? 80 : burst === 3 ? 120 : 160;
        }
        await sleepInterruptible(delay);
      }
    };

    void loop();

    return () => {
      cancelled = true;
      wakePollRef.current = null;
      if (timerId !== null) window.clearTimeout(timerId);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [bridge, persona?.udid, isLiveAvailable, isViewingArchived, streamMode]);

  const handleAutoFallback = useCallback(
    (reason: string) => {
      setStreamMode((current) => {
        if (current === "poll") return current;
        setFallbackReason("auto");
        onToast(reason, "warning");
        return "poll";
      });
    },
    [onToast],
  );

  const handleStreamHealthy = useCallback(() => {
    setFallbackReason("none");
  }, []);

  const handleToggleStreamMode = () => {
    if (!isLiveAvailable) return;
    if (streamMode === "stream") {
      setStreamMode("poll");
      setFallbackReason("manual");
      onToast("Switched to frame polling mode", "info");
    } else if (streamConfig.httpBaseUrl && streamConfig.streamToken) {
      setStreamMode("stream");
      setFallbackReason("none");
      setStreamNonce((n) => n + 1);
      onToast("Reconnecting 24fps live MJPEG stream…", "info");
    } else {
      onToast("Loopback stream URL unavailable; staying in frame poll mode", "warning");
    }
  };

  const handleTap = useCallback(
    (x: number, y: number) => {
      if (!persona?.udid) return;
      void bridge
        .tap(persona.udid, x, y)
        .then((res) => {
          if (res && res.ok === false && res.error) {
            onToast(`Tap failed: ${res.error}`, "danger");
          } else {
            triggerPollBurst();
          }
        })
        .catch((err) => {
          onToast(err instanceof Error ? `Tap failed: ${err.message}` : "Tap failed", "danger");
        });
    },
    [bridge, persona?.udid, onToast, triggerPollBurst],
  );

  const handleSwipe = useCallback(
    (x1: number, y1: number, x2: number, y2: number, duration: number) => {
      if (!persona?.udid) return;
      void bridge
        .swipe(persona.udid, x1, y1, x2, y2, duration)
        .then((res) => {
          if (res && res.ok === false && res.error) {
            onToast(`Swipe failed: ${res.error}`, "danger");
          } else {
            triggerPollBurst();
          }
        })
        .catch((err) => {
          onToast(
            err instanceof Error ? `Swipe failed: ${err.message}` : "Swipe failed",
            "danger",
          );
        });
    },
    [bridge, persona?.udid, onToast, triggerPollBurst],
  );

  const handleToggleTakeover = async (paused: boolean, note?: string) => {
    if (!persona) return;
    setBusyAction("takeover");
    try {
      const res = await bridge.toggleTakeover(runId, persona.id, paused, note);
      if (res.ok === false) {
        throw new Error(res.error || res.message || "Takeover update failed");
      }
      await onRefreshState();
      onToast(
        paused
          ? `Paused ${persona.id} — you now have direct control`
          : `Resumed ${persona.id}${note ? " with steering note" : ""}`,
        paused ? "warning" : "success",
      );
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Failed to toggle takeover", "danger");
    } finally {
      setBusyAction(null);
    }
  };

  const handleAskAgentInspect = async (note?: string) => {
    if (!persona) return;
    setBusyAction("inspect");
    try {
      const inspectPayload = {
        runId,
        personaId: persona.id,
        deviceLabel: persona.device_label || persona.device?.simulator,
        stepNumber: isViewingArchived ? effectiveArchivedStep?.step ?? null : null,
        screenshotFile: isViewingArchived
          ? effectiveArchivedStep?.screenshot_file ?? null
          : persona.last_screenshot,
        a11ySummary: isViewingArchived ? effectiveArchivedStep?.a11y?.summary_line ?? null : null,
        humanNote: note ?? persona.human_note,
      };

      if (capabilities.canSendMessage) {
        const res = await bridge.askAgentToInspect(inspectPayload);
        if (res?.isError) {
          throw new Error("Host rejected agent message");
        }
        onToast(`Asked agent to inspect ${persona.id}`, "success");
      } else if (capabilities.canUpdateModelContext) {
        await bridge.attachInspectorContext(inspectPayload);
        onToast(`Attached ${persona.id} context for agent`, "success");
      }
    } catch (err) {
      onToast(
        err instanceof Error ? `Agent handoff failed: ${err.message}` : "Agent handoff failed",
        "danger",
      );
    } finally {
      setBusyAction(null);
    }
  };

  const handlePressButton = async (button: "HOME" | "LOCK") => {
    if (!persona?.udid) return;
    setBusyAction(button.toLowerCase());
    try {
      const res = await bridge.pressButton(persona.udid, button);
      if (res.ok === false) {
        throw new Error(res.error || res.message || `${button} button failed`);
      }
      triggerPollBurst();
    } catch (err) {
      onToast(err instanceof Error ? err.message : `${button} button failed`, "danger");
    } finally {
      setBusyAction(null);
    }
  };

  const handleToggleSimTheme = async () => {
    if (!persona?.udid) return;
    const nextDark = !simDarkMode;
    setBusyAction("theme");
    try {
      const res = await bridge.deviceAction(persona.udid, nextDark ? "dark" : "light");
      if (res.ok === false) {
        throw new Error(res.error || res.message || "Appearance toggle failed");
      }
      setSimDarkMode(nextDark);
      triggerPollBurst();
      onToast(`Simulator set to ${nextDark ? "Dark" : "Light"} appearance`, "info");
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Appearance toggle failed", "danger");
    } finally {
      setBusyAction(null);
    }
  };

  const handleSaveScreenshot = async () => {
    if (!persona?.udid) return;
    setBusyAction("screenshot");
    try {
      const res = await bridge.deviceAction(persona.udid, "save_screenshot");
      if (res.ok === false) {
        throw new Error(res.error || res.message || "Screenshot save failed");
      }
      const savedPath = res.screenshot_path || res.screenshotPath || res.path;
      onToast(savedPath ? `Saved screenshot: ${savedPath}` : "Screenshot saved", "success");
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Screenshot save failed", "danger");
    } finally {
      setBusyAction(null);
    }
  };

  const handleStopSimulator = async () => {
    if (!persona?.udid) return;
    setBusyAction("stop");
    try {
      const res = await bridge.deviceAction(persona.udid, "stop");
      if (res.ok === false) {
        throw new Error(res.error || res.message || "Stop simulator failed");
      }
      setPollFrameSrc(null);
      await onRefreshState();
      onToast(`Stopped simulator for ${persona.id}`, "info");
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Stop simulator failed", "danger");
    } finally {
      setBusyAction(null);
    }
  };

  const handleTypeText = async (text: string) => {
    if (!persona?.udid) return;
    setBusyAction("type");
    try {
      const res = await bridge.typeText(persona.udid, text);
      if (res.ok === false) {
        throw new Error(res.error || res.message || "Text input failed");
      }
      triggerPollBurst();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Text input failed", "danger");
    } finally {
      setBusyAction(null);
    }
  };

  const handleSendKey = async (key: PhoneKey) => {
    if (!persona?.udid) return;
    setBusyAction("key");
    try {
      const res = await bridge.sendKey(persona.udid, key);
      if (res.ok === false) {
        throw new Error(res.error || res.message || "Key press failed");
      }
      triggerPollBurst();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Key press failed", "danger");
    } finally {
      setBusyAction(null);
    }
  };

  if (!persona) {
    return (
      <section className="view-scroll stage-empty-wrap">
        <div className="empty-card">
          <h3>No Persona Selected</h3>
          <p>Select a persona from the Live Matrix to inspect its simulator screen, steps, and A11y findings.</p>
          <button type="button" className="primary-cta" onClick={onBackToMatrix}>
            <ArrowLeftIcon width={14} height={14} />
            <span>Back to Live Matrix</span>
          </button>
        </div>
      </section>
    );
  }

  const isRtl = isRtlLocale(persona.locale);
  const isLargeText = isLargeTextSize(persona.device?.text_size);

  return (
    <section className="stage-inspector-root" aria-label={`Stage inspector for ${persona.id}`}>
      {/* Persona Switcher & Device Metadata Bar */}
      <div className="stage-persona-bar">
        <button
          type="button"
          className="back-matrix-btn"
          onClick={onBackToMatrix}
          title="Back to Live Matrix"
        >
          <ArrowLeftIcon width={14} height={14} />
          <span>Matrix</span>
        </button>

        <div className="persona-switcher-pills" role="tablist" aria-label="Switch persona">
          {personas.map((p) => {
            const active = p.id === persona.id;
            return (
              <button
                key={p.id}
                type="button"
                role="tab"
                aria-selected={active}
                className={`persona-pill-btn ${active ? "is-active" : ""} status-pill-mini-${p.status}`}
                onClick={() => {
                  setSelectedStepNum(null);
                  onSelectPersona(p.id);
                }}
              >
                <span className={`mini-status-dot dot-${p.status}`} aria-hidden="true" />
                <span>{p.id}</span>
                {p.paused_by_human && <span className="pill-paused-mark">‖</span>}
              </button>
            );
          })}
        </div>

        <div className="stage-device-chips">
          <span className="meta-chip">{persona.locale}</span>
          {isRtl && <span className="meta-chip meta-chip-rtl">RTL</span>}
          <span className="meta-chip" title={persona.device_label || persona.device?.simulator}>
            {formatShortDevice(persona.device?.simulator)}
          </span>
          <span
            className={`meta-chip ${isLargeText ? "meta-chip-a11y" : ""}`}
            title={`Dynamic Type: ${persona.device?.text_size || "large"}`}
          >
            {formatTextSizeChip(persona.device?.text_size)}
          </span>
        </div>
      </div>

      {/* Prominent Human Takeover & Agent Handoff Bar */}
      <TakeoverBar
        persona={persona}
        isLiveAvailable={isLiveAvailable}
        busyAction={busyAction}
        capabilities={capabilities}
        onToggleTakeover={handleToggleTakeover}
        onAskAgentInspect={handleAskAgentInspect}
      />

      {/* Responsive Split: Left (PhoneStage + Controls + Filmstrip) | Right (Inspector Drawer) */}
      <div className="stage-split">
        <div className="stage-left-col">
          <PhoneStage
            persona={persona}
            isLiveAvailable={isLiveAvailable}
            isViewingArchived={isViewingArchived}
            archivedStep={effectiveArchivedStep}
            archivedImageUrl={archivedImageUrl}
            liveStreamUrl={liveStreamUrl}
            streamMode={streamMode}
            fallbackReason={fallbackReason}
            streamNonce={streamNonce}
            pollFrameSrc={pollFrameSrc}
            showA11yOverlay={showA11yOverlay}
            highlightedA11yKey={highlightedA11yKey}
            onToggleA11yOverlay={() => setShowA11yOverlay((v) => !v)}
            onHighlightA11y={setHighlightedA11yKey}
            onBackToLive={() => {
              setSelectedStepNum(null);
              setHighlightedA11yKey(null);
            }}
            onToggleStreamMode={handleToggleStreamMode}
            onAutoFallback={handleAutoFallback}
            onStreamHealthy={handleStreamHealthy}
            onTap={handleTap}
            onSwipe={handleSwipe}
          />

          <ActionToolbar
            isLiveAvailable={isLiveAvailable}
            busyAction={busyAction}
            simDarkMode={simDarkMode}
            keyboardOpen={keyboardOpen}
            onPressHome={() => void handlePressButton("HOME")}
            onPressLock={() => void handlePressButton("LOCK")}
            onToggleSimTheme={() => void handleToggleSimTheme()}
            onSaveScreenshot={() => void handleSaveScreenshot()}
            onToggleKeyboard={() => setKeyboardOpen((o) => !o)}
            onStopSimulator={() => void handleStopSimulator()}
          />

          {keyboardOpen && isLiveAvailable && (
            <div className="keyboard-drawer">
              <KeyboardBar
                disabled={!isLiveAvailable}
                sending={busyAction === "type" || busyAction === "key"}
                onTypeText={handleTypeText}
                onSendKey={handleSendKey}
              />
            </div>
          )}

          <StepFilmstrip
            runId={runId}
            personaId={persona.id}
            steps={steps}
            isLiveAvailable={isLiveAvailable}
            selectedStep={isViewingArchived ? effectiveArchivedStep?.step ?? null : null}
            streamConfig={streamConfig}
            onSelectStep={(stepNum) => {
              setSelectedStepNum(stepNum);
              setHighlightedA11yKey(null);
            }}
          />
        </div>

        <PersonaReportDrawer
          persona={persona}
          detail={detail?.persona_id === persona.id ? detail : null}
          loadingDetail={loadingDetail}
          selectedStep={isViewingArchived ? effectiveArchivedStep : null}
          showA11yOverlay={showA11yOverlay}
          highlightedA11yKey={highlightedA11yKey}
          onHighlightA11y={setHighlightedA11yKey}
          onEnsureA11yOverlay={() => setShowA11yOverlay(true)}
          onJumpToStep={(stepNum, enableA11y) => {
            setSelectedStepNum(stepNum);
            if (enableA11y) setShowA11yOverlay(true);
            setHighlightedA11yKey(null);
          }}
          onOpenMarkdown={onOpenMarkdown}
        />
      </div>
    </section>
  );
}
