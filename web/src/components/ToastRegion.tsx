import { useEffect, useRef, type CSSProperties } from "react";
import { CloseIcon } from "../icons";
import type { ToastItem } from "../types";

interface ToastRegionProps {
  toasts: ToastItem[];
  liveAnnouncement: string;
  onDismiss: (id: number) => void;
}

const SUPPORTS_POPOVER =
  typeof HTMLElement !== "undefined" &&
  Object.hasOwn(HTMLElement.prototype, "popover");

interface SingleToastProps {
  toast: ToastItem;
  index: number;
  onDismiss: (id: number) => void;
}

function SingleToast({ toast, index, onDismiss }: SingleToastProps) {
  const elRef = useRef<HTMLDivElement | null>(null);
  const timerRef = useRef<number | null>(null);
  const pausedRef = useRef(false);
  const triggerElRef = useRef<HTMLElement | null>(null);

  const scheduleDismiss = () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (pausedRef.current) return;
    timerRef.current = window.setTimeout(() => {
      const node = elRef.current;
      if (node && document.activeElement && node.contains(document.activeElement)) {
        return;
      }
      if (SUPPORTS_POPOVER && node) {
        try {
          node.hidePopover();
        } catch {
          // ignore
        }
      }
      onDismiss(toast.id);
    }, 4200);
  };

  useEffect(() => {
    triggerElRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const node = elRef.current;
    if (SUPPORTS_POPOVER && node) {
      node.setAttribute("popover", "manual");
      try {
        node.showPopover();
      } catch {
        // ignore
      }
    }

    scheduleDismiss();

    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (SUPPORTS_POPOVER && node) {
        try {
          node.hidePopover();
        } catch {
          // ignore
        }
      }
    };
  }, [toast.id]);

  const handleManualClose = () => {
    const node = elRef.current;
    const hadFocus = Boolean(
      node && document.activeElement && node.contains(document.activeElement),
    );
    if (SUPPORTS_POPOVER && node) {
      try {
        node.hidePopover();
      } catch {
        // ignore
      }
    }
    onDismiss(toast.id);
    if (hadFocus && triggerElRef.current && triggerElRef.current.isConnected) {
      triggerElRef.current.focus();
    }
  };

  const pauseTimer = () => {
    pausedRef.current = true;
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const resumeTimer = () => {
    pausedRef.current = false;
    scheduleDismiss();
  };

  const popoverId = `qa-toast-${toast.id}`;

  return (
    <div
      ref={elRef}
      id={popoverId}
      className={`toast-item toast-tone-${toast.tone}`}
      style={{ "--toast-index": index } as CSSProperties}
      onPointerEnter={pauseTimer}
      onPointerLeave={resumeTimer}
      onFocus={pauseTimer}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
          resumeTimer();
        }
      }}
      {...(SUPPORTS_POPOVER ? ({ popover: "manual" } as Record<string, string>) : {})}
    >
      <span className="toast-dot" aria-hidden="true" />
      <span className="toast-text">{toast.message}</span>
      <button
        type="button"
        className="toast-close"
        aria-label="Dismiss notification"
        title="Dismiss"
        onClick={handleManualClose}
        {...(SUPPORTS_POPOVER
          ? ({ popovertarget: popoverId, popovertargetaction: "hide" } as Record<string, string>)
          : {})}
      >
        <CloseIcon width={14} height={14} />
      </button>
    </div>
  );
}

export function ToastRegion({ toasts, liveAnnouncement, onDismiss }: ToastRegionProps) {
  return (
    <>
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {liveAnnouncement}
      </div>

      <div role="region" aria-label="Notifications" className="toast-stack">
        {toasts.map((toast, index) => (
          <SingleToast key={toast.id} toast={toast} index={index} onDismiss={onDismiss} />
        ))}
      </div>
    </>
  );
}
