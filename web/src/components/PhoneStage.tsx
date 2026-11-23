import { useEffect, useRef, useState, type CSSProperties } from "react";
import { A11yIcon, PhoneIcon, PollIcon, StreamIcon, TabletIcon } from "../icons";
import { A11yOverlay, flattenA11yBoxes } from "./A11yOverlay";
import {
  isIpadSimulator,
  type A11yAuditResult,
  type FallbackReason,
  type PersonaRunState,
  type StepRecord,
  type StreamMode,
  type SwipeTrail,
  type TapRipple,
} from "../types";

interface PhoneStageProps {
  persona: PersonaRunState;
  isLiveAvailable: boolean;
  isViewingArchived: boolean;
  archivedStep: StepRecord | null;
  archivedImageUrl: string | null;
  liveStreamUrl: string | null;
  streamMode: StreamMode;
  fallbackReason: FallbackReason;
  streamNonce: number;
  pollFrameSrc: string | null;
  showA11yOverlay: boolean;
  highlightedA11yKey: string | null;
  onToggleA11yOverlay: () => void;
  onHighlightA11y: (key: string | null) => void;
  onBackToLive: () => void;
  onToggleStreamMode: () => void;
  onAutoFallback: (reason: string) => void;
  onStreamHealthy: () => void;
  onTap: (x: number, y: number) => void;
  onSwipe: (x1: number, y1: number, x2: number, y2: number, duration: number) => void;
}

const TAP_THRESHOLD_PX = 8;
const FIRST_FRAME_TIMEOUT_MS = 3500;
const WHEEL_DEBOUNCE_MS = 110;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function PhoneStage({
  persona,
  isLiveAvailable,
  isViewingArchived,
  archivedStep,
  archivedImageUrl,
  liveStreamUrl,
  streamMode,
  fallbackReason,
  streamNonce,
  pollFrameSrc,
  showA11yOverlay,
  highlightedA11yKey,
  onToggleA11yOverlay,
  onHighlightA11y,
  onBackToLive,
  onToggleStreamMode,
  onAutoFallback,
  onStreamHealthy,
  onTap,
  onSwipe,
}: PhoneStageProps) {
  const screenRef = useRef<HTMLDivElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);

  const [streamLoaded, setStreamLoaded] = useState(false);
  const [archivedError, setArchivedError] = useState(false);
  const [ripples, setRipples] = useState<TapRipple[]>([]);
  const [swipeTrail, setSwipeTrail] = useState<SwipeTrail | null>(null);

  const nextFxId = useRef(1);
  const fallbackTriggeredRef = useRef(false);

  const dragRef = useRef<{
    pointerId: number;
    startClientX: number;
    startClientY: number;
    startTime: number;
    startPctX: number;
    startPctY: number;
    startIosX: number;
    startIosY: number;
    moved: boolean;
  } | null>(null);

  const wheelStateRef = useRef<{
    deltaX: number;
    deltaY: number;
    anchorIosX: number;
    anchorIosY: number;
    timer: number | null;
  }>({
    deltaX: 0,
    deltaY: 0,
    anchorIosX: 0,
    anchorIosY: 0,
    timer: null,
  });

  const isIpad = isIpadSimulator(persona.device?.simulator || persona.device_label);
  const deviceWidth = persona.width_points > 0 ? persona.width_points : isIpad ? 744 : 393;
  const deviceHeight = persona.height_points > 0 ? persona.height_points : isIpad ? 1133 : 852;
  const aspectRatio = deviceWidth / deviceHeight;

  const isInteractiveLive = !isViewingArchived && isLiveAvailable && Boolean(persona.udid);
  const showStreamImg = Boolean(
    isInteractiveLive && streamMode === "stream" && liveStreamUrl,
  );

  // Reset archived image error when URL changes.
  useEffect(() => {
    setArchivedError(false);
  }, [archivedImageUrl]);

  // Reset stream-loaded gate whenever stream URL, mode, or persona changes.
  useEffect(() => {
    setStreamLoaded(false);
    fallbackTriggeredRef.current = false;
  }, [liveStreamUrl, streamMode, streamNonce, persona.id, isViewingArchived]);

  // Actively abort MJPEG HTTP connection when switching to archived step, poll mode, or unmounting.
  useEffect(() => {
    const img = imgRef.current;
    return () => {
      if (img && showStreamImg) {
        img.src = "";
      }
    };
  }, [showStreamImg, liveStreamUrl]);

  // 3.5-second first-frame watchdog for MJPEG -> app_qa_frame automatic fallback.
  useEffect(() => {
    if (!showStreamImg || streamLoaded) return;
    const timer = window.setTimeout(() => {
      const el = imgRef.current;
      if (el && el.complete && el.naturalWidth > 0) {
        setStreamLoaded(true);
        onStreamHealthy();
        return;
      }
      if (!fallbackTriggeredRef.current) {
        fallbackTriggeredRef.current = true;
        onAutoFallback("No MJPEG frame within 3.5s — switched to frame polling");
      }
    }, FIRST_FRAME_TIMEOUT_MS);

    return () => window.clearTimeout(timer);
  }, [showStreamImg, streamLoaded, liveStreamUrl, onAutoFallback, onStreamHealthy]);

  const mapClientToPhone = (clientX: number, clientY: number) => {
    const targetEl = imgRef.current ?? screenRef.current;
    if (!targetEl) return null;
    const rect = targetEl.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;

    const normX = clamp((clientX - rect.left) / rect.width, 0, 1);
    const normY = clamp((clientY - rect.top) / rect.height, 0, 1);

    return {
      iosX: clamp(Math.round(normX * deviceWidth), 0, deviceWidth),
      iosY: clamp(Math.round(normY * deviceHeight), 0, deviceHeight),
      pctX: normX * 100,
      pctY: normY * 100,
    };
  };

  // Non-passive mouse-wheel-to-swipe handler on live screen.
  useEffect(() => {
    const screen = screenRef.current;
    if (!screen || !isInteractiveLive) return;

    const handleWheel = (e: WheelEvent) => {
      e.preventDefault();
      const mapped = mapClientToPhone(e.clientX, e.clientY);
      const ws = wheelStateRef.current;
      ws.deltaX += e.deltaX;
      ws.deltaY += e.deltaY;
      if (mapped) {
        ws.anchorIosX = clamp(
          mapped.iosX,
          Math.round(deviceWidth * 0.15),
          Math.round(deviceWidth * 0.85),
        );
        ws.anchorIosY = clamp(
          mapped.iosY,
          Math.round(deviceHeight * 0.2),
          Math.round(deviceHeight * 0.8),
        );
      } else {
        ws.anchorIosX = Math.round(deviceWidth * 0.5);
        ws.anchorIosY = Math.round(deviceHeight * 0.5);
      }

      if (ws.timer !== null) {
        window.clearTimeout(ws.timer);
      }
      ws.timer = window.setTimeout(() => {
        const dx = ws.deltaX;
        const dy = ws.deltaY;
        const cx = ws.anchorIosX || Math.round(deviceWidth * 0.5);
        const cy = ws.anchorIosY || Math.round(deviceHeight * 0.5);
        ws.deltaX = 0;
        ws.deltaY = 0;
        ws.timer = null;

        if (Math.abs(dy) >= Math.abs(dx)) {
          if (Math.abs(dy) < 4) return;
          const travel = clamp(Math.abs(dy) * 1.15, 95, deviceHeight * 0.52);
          const half = travel / 2;
          const safeTop = Math.round(deviceHeight * 0.14);
          const safeBottom = Math.round(deviceHeight * 0.86);
          const midY = clamp(cy, safeTop + half, safeBottom - half);
          const y1 = Math.round(dy > 0 ? midY + half : midY - half);
          const y2 = Math.round(dy > 0 ? midY - half : midY + half);
          onSwipe(cx, y1, cx, y2, 0.22);
        } else {
          if (Math.abs(dx) < 4) return;
          const travel = clamp(Math.abs(dx) * 1.15, 80, deviceWidth * 0.65);
          const half = travel / 2;
          const safeLeft = Math.round(deviceWidth * 0.12);
          const safeRight = Math.round(deviceWidth * 0.88);
          const midX = clamp(cx, safeLeft + half, safeRight - half);
          const x1 = Math.round(dx > 0 ? midX + half : midX - half);
          const x2 = Math.round(dx > 0 ? midX - half : midX + half);
          onSwipe(x1, cy, x2, cy, 0.22);
        }
      }, WHEEL_DEBOUNCE_MS);
    };

    screen.addEventListener("wheel", handleWheel, { passive: false });
    return () => {
      screen.removeEventListener("wheel", handleWheel);
      if (wheelStateRef.current.timer !== null) {
        window.clearTimeout(wheelStateRef.current.timer);
        wheelStateRef.current.timer = null;
      }
    };
  }, [isInteractiveLive, deviceWidth, deviceHeight, onSwipe]);

  const spawnRipple = (pctX: number, pctY: number) => {
    const id = nextFxId.current++;
    setRipples((prev) => [...prev.slice(-4), { id, pctX, pctY }]);
    window.setTimeout(() => {
      setRipples((prev) => prev.filter((item) => item.id !== id));
    }, 460);
  };

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!isInteractiveLive) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;

    const mapped = mapClientToPhone(e.clientX, e.clientY);
    if (!mapped) return;

    e.preventDefault();
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      // ignore
    }

    dragRef.current = {
      pointerId: e.pointerId,
      startClientX: e.clientX,
      startClientY: e.clientY,
      startTime: performance.now(),
      startPctX: mapped.pctX,
      startPctY: mapped.pctY,
      startIosX: mapped.iosX,
      startIosY: mapped.iosY,
      moved: false,
    };
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;

    const dist = Math.hypot(
      e.clientX - drag.startClientX,
      e.clientY - drag.startClientY,
    );
    if (dist >= TAP_THRESHOLD_PX) {
      drag.moved = true;
      const mapped = mapClientToPhone(e.clientX, e.clientY);
      if (mapped) {
        setSwipeTrail({
          id: nextFxId.current,
          x1Pct: drag.startPctX,
          y1Pct: drag.startPctY,
          x2Pct: mapped.pctX,
          y2Pct: mapped.pctY,
          active: true,
        });
      }
    }
  };

  const handlePointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    dragRef.current = null;

    try {
      if (e.currentTarget.hasPointerCapture(e.pointerId)) {
        e.currentTarget.releasePointerCapture(e.pointerId);
      }
    } catch {
      // ignore
    }

    const mapped = mapClientToPhone(e.clientX, e.clientY);
    const dist = Math.hypot(
      e.clientX - drag.startClientX,
      e.clientY - drag.startClientY,
    );

    if (dist < TAP_THRESHOLD_PX || !mapped) {
      setSwipeTrail(null);
      spawnRipple(drag.startPctX, drag.startPctY);
      onTap(drag.startIosX, drag.startIosY);
      return;
    }

    const rawSeconds = (performance.now() - drag.startTime) / 1000;
    const duration = Number(clamp(rawSeconds, 0.15, 1.2).toFixed(2));
    const trailId = nextFxId.current++;

    setSwipeTrail({
      id: trailId,
      x1Pct: drag.startPctX,
      y1Pct: drag.startPctY,
      x2Pct: mapped.pctX,
      y2Pct: mapped.pctY,
      active: false,
    });
    window.setTimeout(() => {
      setSwipeTrail((prev) => (prev && prev.id === trailId ? null : prev));
    }, 360);

    onSwipe(drag.startIosX, drag.startIosY, mapped.iosX, mapped.iosY, duration);
  };

  const handlePointerCancel = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    dragRef.current = null;
    setSwipeTrail(null);
  };

  const isActionOnlyArchived = Boolean(
    isViewingArchived && archivedStep && !archivedStep.screenshot_file,
  );
  const a11yData: A11yAuditResult | null = isActionOnlyArchived ? null : archivedStep?.a11y ?? null;
  const a11yCount = flattenA11yBoxes(a11yData).length;
  const unnamedCount = a11yData?.unnamed?.length ?? 0;
  const undersizedCount = a11yData?.undersized?.length ?? 0;

  const renderScreen = () => {
    if (isViewingArchived) {
      if (isActionOnlyArchived && archivedStep) {
        const stepLabel = String(archivedStep.step).padStart(2, "0");
        return (
          <div className="screen-placeholder screen-action-only">
            <div className="action-only-stage-card">
              <div className="action-only-stage-icon" aria-hidden="true">
                <StreamIcon width={20} height={20} />
              </div>
              <span className="action-only-stage-kicker tabular-nums">
                Step #{stepLabel}
              </span>
              <p className="action-only-stage-title">
                Action-only step (observe_after=false)
              </p>
              <div className="action-only-stage-meta">
                <span className="action-only-stage-kind">
                  {archivedStep.action?.kind || "action"}
                </span>
                {archivedStep.action?.detail && (
                  <code className="action-only-stage-detail">
                    {archivedStep.action.detail}
                  </code>
                )}
              </div>
              <p className="action-only-stage-hint">
                No screenshot or accessibility tree was captured for this step.
              </p>
            </div>
          </div>
        );
      }
      if (archivedImageUrl && !archivedError) {
        return (
          <img
            ref={imgRef}
            src={archivedImageUrl}
            alt={`${persona.id} step ${archivedStep?.step ?? ""} screenshot`}
            className="phone-video-img is-loaded"
            draggable={false}
            onError={() => setArchivedError(true)}
          />
        );
      }
      return (
        <div className="screen-placeholder">
          {isIpad ? <TabletIcon width={28} height={28} /> : <PhoneIcon width={28} height={28} />}
          <p className="placeholder-title">
            {archivedStep ? `Step #${String(archivedStep.step).padStart(2, "0")}` : "No Capture Yet"}
          </p>
          <p className="placeholder-sub">
            {archivedError
              ? "Screenshot artifact could not be loaded."
              : "Screenshots appear here as the persona completes steps."}
          </p>
        </div>
      );
    }

    if (!isLiveAvailable || !persona.udid) {
      return (
        <div className="screen-placeholder">
          {isIpad ? <TabletIcon width={28} height={28} /> : <PhoneIcon width={28} height={28} />}
          <p className="placeholder-title">Simulator Inactive</p>
          <p className="placeholder-sub">
            Status: {persona.status}. Select a captured step from the filmstrip below to inspect screenshots and A11y boxes.
          </p>
        </div>
      );
    }

    if (showStreamImg && liveStreamUrl) {
      return (
        <>
          <img
            ref={imgRef}
            key={liveStreamUrl}
            src={liveStreamUrl}
            alt={`${persona.id} live simulator stream`}
            className={`phone-video-img ${streamLoaded ? "is-loaded" : ""}`}
            draggable={false}
            onLoad={(e) => {
              if (e.currentTarget.naturalWidth > 0) {
                setStreamLoaded(true);
                onStreamHealthy();
              }
            }}
            onError={() => {
              if (!fallbackTriggeredRef.current) {
                fallbackTriggeredRef.current = true;
                onAutoFallback("MJPEG stream failed — switched to frame polling");
              }
            }}
          />
          {!streamLoaded && (
            <div className="screen-loading-overlay">
              <span className="spinner-ring" aria-hidden="true" />
              <p className="placeholder-sub">Connecting 24fps live stream…</p>
            </div>
          )}
        </>
      );
    }

    return pollFrameSrc ? (
      <img
        ref={imgRef}
        src={pollFrameSrc}
        alt={`${persona.id} frame-polled simulator preview`}
        className="phone-video-img is-loaded"
        draggable={false}
      />
    ) : (
      <div className="screen-loading-overlay">
        <span className="spinner-ring" aria-hidden="true" />
        <p className="placeholder-sub">Capturing simulator frame…</p>
      </div>
    );
  };

  return (
    <div className="phone-stage-wrap">
      {/* Stage mode / overlay header strip */}
      <div className="stage-controls-strip">
        <div className="stage-controls-left">
          {isViewingArchived ? (
            <>
              <span className="stage-pill stage-pill-archived tabular-nums">
                Step #{String(archivedStep?.step ?? 0).padStart(2, "0")}
              </span>
              {isActionOnlyArchived && (
                <span className="stage-pill stage-pill-action-only">Action only</span>
              )}
              {isLiveAvailable && (
                <button
                  type="button"
                  className="stage-pill-btn stage-pill-live"
                  onClick={onBackToLive}
                >
                  <span className="pulse-dot" aria-hidden="true" />
                  <span>Back to Live</span>
                </button>
              )}
            </>
          ) : (
            <button
              type="button"
              className={`stage-pill-btn ${
                streamMode === "stream"
                  ? "stage-pill-stream"
                  : fallbackReason === "auto"
                    ? "stage-pill-fallback"
                    : "stage-pill-poll"
              }`}
              onClick={onToggleStreamMode}
              disabled={!isLiveAvailable}
              title={
                streamMode === "stream"
                  ? "24fps MJPEG live stream (click to switch to frame polling)"
                  : "Frame polling mode (click to reconnect 24fps stream)"
              }
            >
              {streamMode === "stream" ? (
                <StreamIcon width={12} height={12} />
              ) : (
                <PollIcon width={12} height={12} />
              )}
              <span>
                {streamMode === "stream"
                  ? "Live 24fps"
                  : fallbackReason === "auto"
                    ? "Frame Poll (fallback)"
                    : "Frame Poll"}
              </span>
            </button>
          )}
        </div>

        <div className="stage-controls-right">
          {isViewingArchived && archivedStep && Boolean(archivedStep.screenshot_file) && (
            <button
              type="button"
              className={`stage-pill-btn stage-pill-a11y ${showA11yOverlay ? "is-on" : ""}`}
              aria-pressed={showA11yOverlay}
              onClick={onToggleA11yOverlay}
              title={
                a11yCount > 0
                  ? `Toggle A11y overlay (${unnamedCount} unnamed, ${undersizedCount} <44pt)`
                  : "No A11y violations detected on this step"
              }
            >
              <A11yIcon width={13} height={13} />
              <span>A11y</span>
              {a11yCount > 0 && (
                <span className="a11y-pill-counts tabular-nums">
                  {unnamedCount > 0 && <em className="a11y-cnt-unnamed">{unnamedCount}</em>}
                  {undersizedCount > 0 && <em className="a11y-cnt-size">{undersizedCount}</em>}
                </span>
              )}
            </button>
          )}

          <span className="stage-dims tabular-nums" title="Display coordinate points">
            {deviceWidth}×{deviceHeight}pt
          </span>
        </div>
      </div>

      {/* Bezel & Screen Canvas */}
      <div
        id="qa-phone-stage"
        className="stage-viewport"
        aria-label={`${persona.id} simulator canvas`}
      >
        <div
          className={`phone-bezel ${isIpad ? "is-ipad" : ""} ${
            isInteractiveLive ? "is-awake" : "is-archived"
          } ${persona.paused_by_human ? "is-takeover-frame" : ""}`}
          style={{ "--phone-aspect": String(aspectRatio) } as CSSProperties}
        >
          {!isIpad && (
            <>
              <span className="bezel-btn bezel-btn-silent" aria-hidden="true" />
              <span className="bezel-btn bezel-btn-vol-up" aria-hidden="true" />
              <span className="bezel-btn bezel-btn-vol-down" aria-hidden="true" />
              <span className="bezel-btn bezel-btn-power" aria-hidden="true" />
            </>
          )}

          <div
            ref={screenRef}
            className={`phone-screen ${isInteractiveLive ? "is-interactive" : ""}`}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerUp}
            onPointerCancel={handlePointerCancel}
            role={isInteractiveLive ? "application" : undefined}
            aria-label={
              isInteractiveLive
                ? `${persona.id} interactive iOS screen (click to tap, drag to swipe, scroll wheel to scroll)`
                : isActionOnlyArchived
                  ? `${persona.id} action-only step ${archivedStep?.step ?? ""}`
                  : `${persona.id} archived step screenshot`
            }
          >
            {renderScreen()}

            {/* Visual A11y SVG Overlay when inspecting archived step */}
            {isViewingArchived && !isActionOnlyArchived && showA11yOverlay && a11yData && (
              <A11yOverlay
                a11y={a11yData}
                widthPoints={deviceWidth}
                heightPoints={deviceHeight}
                highlightedKey={highlightedA11yKey}
                onHighlightChange={onHighlightA11y}
              />
            )}

            {/* Touch ripples */}
            {ripples.map((r) => (
              <span
                key={r.id}
                className="touch-ripple"
                style={{ left: `${r.pctX}%`, top: `${r.pctY}%` }}
                aria-hidden="true"
              />
            ))}

            {/* Swipe SVG trail */}
            {swipeTrail && (
              <svg
                className={`swipe-trail-svg ${swipeTrail.active ? "is-active" : "is-fading"}`}
                viewBox={`0 0 ${deviceWidth} ${deviceHeight}`}
                preserveAspectRatio="xMidYMid meet"
                aria-hidden="true"
              >
                <line
                  x1={(swipeTrail.x1Pct / 100) * deviceWidth}
                  y1={(swipeTrail.y1Pct / 100) * deviceHeight}
                  x2={(swipeTrail.x2Pct / 100) * deviceWidth}
                  y2={(swipeTrail.y2Pct / 100) * deviceHeight}
                  className="swipe-trail-line"
                  strokeWidth="4"
                  strokeLinecap="round"
                />
                <circle
                  cx={(swipeTrail.x1Pct / 100) * deviceWidth}
                  cy={(swipeTrail.y1Pct / 100) * deviceHeight}
                  r="6"
                  className="swipe-trail-start"
                />
                <circle
                  cx={(swipeTrail.x2Pct / 100) * deviceWidth}
                  cy={(swipeTrail.y2Pct / 100) * deviceHeight}
                  r="10"
                  className="swipe-trail-end"
                />
              </svg>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
