import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CompareRunsView } from "./components/CompareRunsView";
import { DashboardHeader } from "./components/DashboardHeader";
import { LiveMatrixView } from "./components/LiveMatrixView";
import { StageInspectorView } from "./components/StageInspectorView";
import { SummaryReportView } from "./components/SummaryReportView";
import { ToastRegion } from "./components/ToastRegion";
import { MatrixIcon, RefreshIcon, TerminalIcon } from "./icons";
import {
  applyHostContextToDocument,
  parseStructuredPayload,
  QaMcpBridge,
} from "./mcpClient";
import {
  isLivePersonaStatus,
  type DashboardBootstrap,
  type DashboardTab,
  type HostCapabilitiesSummary,
  type PersonaDetailResult,
  type RunStateResult,
  type RunSummary,
  type StreamConfig,
  type ToastItem,
} from "./types";

let sharedBridge: QaMcpBridge | null = null;
function getSharedBridge(): QaMcpBridge {
  if (!sharedBridge) {
    sharedBridge = new QaMcpBridge();
  }
  return sharedBridge;
}

function announceToScreenReaders(
  message: string,
  setLive: (msg: string) => void,
): void {
  const doc = document as Document & {
    ariaNotify?: (text: string, options?: { priority?: "normal" | "high" }) => void;
  };
  if (typeof doc.ariaNotify === "function") {
    try {
      doc.ariaNotify(message);
    } catch {
      // fall through
    }
  }
  setLive("");
  window.requestAnimationFrame(() => setLive(message));
}

export function App() {
  const bridgeRef = useRef<QaMcpBridge>(getSharedBridge());

  const [connectionStatus, setConnectionStatus] = useState<
    "connecting" | "connected" | "standalone" | "cancelled" | "error"
  >("connecting");
  const [connectionMessage, setConnectionMessage] = useState<string | undefined>();
  const [loadingInitial, setLoadingInitial] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const [capabilities, setCapabilities] = useState<HostCapabilitiesSummary>({
    canUpdateModelContext: false,
    canSendMessage: false,
    canCallServerTools: false,
  });

  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [runState, setRunState] = useState<RunStateResult | null>(null);

  const [activeTab, setActiveTab] = useState<DashboardTab>("matrix");
  const [activePersonaId, setActivePersonaId] = useState<string | null>(null);
  const [personaDetail, setPersonaDetail] = useState<PersonaDetailResult | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const [streamConfig, setStreamConfig] = useState<StreamConfig>({
    httpBaseUrl: null,
    streamToken: null,
  });

  // Cross-view step jump state (e.g. Summary screenshot chip -> Stage at Step + A11y)
  const [requestedStep, setRequestedStep] = useState<number | null>(null);
  const [requestedA11yOn, setRequestedA11yOn] = useState(false);
  const [requestNonce, setRequestNonce] = useState(0);

  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [liveAnnouncement, setLiveAnnouncement] = useState("");
  const nextToastIdRef = useRef(1);
  const receivedToolResultRef = useRef(false);
  const activeRunIdRef = useRef<string | null>(null);
  const activePersonaIdRef = useRef<string | null>(null);

  activeRunIdRef.current = activeRunId;
  activePersonaIdRef.current = activePersonaId;

  const pushToast = useCallback(
    (message: string, tone: ToastItem["tone"] = "info") => {
      const id = nextToastIdRef.current++;
      setToasts((prev) => [...prev.slice(-2), { id, message, tone }]);
      announceToScreenReaders(message, setLiveAnnouncement);
    },
    [],
  );

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((item) => item.id !== id));
  }, []);

  const updateStreamConfigFromPayload = useCallback(
    (payload: { httpBaseUrl?: string | null; streamToken?: string | null } | null | undefined) => {
      if (!payload) return;
      if (payload.httpBaseUrl || payload.streamToken) {
        setStreamConfig((prev) => ({
          httpBaseUrl: payload.httpBaseUrl ?? prev.httpBaseUrl,
          streamToken: payload.streamToken ?? prev.streamToken,
        }));
      }
    },
    [],
  );

  const loadRunState = useCallback(
    async (runId: string, opts?: { background?: boolean }) => {
      const bridge = bridgeRef.current;
      if (!bridge.isConnected() || !runId) return null;
      const state = await bridge.getRunState(runId, opts);
      if (!state) return null;
      if (state.ok === false) {
        if (!opts?.background && state.error) {
          pushToast(state.error, "danger");
        }
        return null;
      }
      // Guard against stale async response after user switched runs.
      if (activeRunIdRef.current && activeRunIdRef.current !== runId) {
        return null;
      }
      updateStreamConfigFromPayload(state);
      setRunState(state);
      setActivePersonaId((prev) => {
        if (prev && state.personas.some((p) => p.id === prev)) return prev;
        return state.personas[0]?.id ?? null;
      });
      return state;
    },
    [pushToast, updateStreamConfigFromPayload],
  );

  const loadPersonaDetail = useCallback(
    async (runId: string, personaId: string, opts?: { background?: boolean }) => {
      const bridge = bridgeRef.current;
      if (!bridge.isConnected() || !runId || !personaId) return null;
      if (!opts?.background) {
        setLoadingDetail(true);
      }
      try {
        const detail = await bridge.getPersonaDetail(runId, personaId, opts);
        if (!detail) return null;
        if (detail.ok === false) {
          if (!opts?.background && detail.error) {
            pushToast(detail.error, "danger");
          }
          return null;
        }
        if (
          activeRunIdRef.current === runId &&
          activePersonaIdRef.current === personaId
        ) {
          setPersonaDetail(detail);
        }
        return detail;
      } finally {
        if (!opts?.background) {
          setLoadingDetail(false);
        }
      }
    },
    [pushToast],
  );

  const applyBootstrap = useCallback(
    async (boot: DashboardBootstrap) => {
      updateStreamConfigFromPayload(boot);
      const nextRuns = Array.isArray(boot.runs) ? boot.runs : [];
      setRuns(nextRuns);

      const chosenRunId =
        (boot.activeRunId && nextRuns.some((r) => r.run_id === boot.activeRunId)
          ? boot.activeRunId
          : boot.activeRunId) ||
        nextRuns[0]?.run_id ||
        null;

      setActiveRunId(chosenRunId);
      activeRunIdRef.current = chosenRunId;

      if (boot.activePersonaId) {
        setActivePersonaId(boot.activePersonaId);
        activePersonaIdRef.current = boot.activePersonaId;
        setActiveTab("stage");
      }

      setLoadingInitial(false);

      if (chosenRunId && bridgeRef.current.isConnected()) {
        try {
          const state = await loadRunState(chosenRunId);
          const targetPersona =
            boot.activePersonaId || state?.personas?.[0]?.id || null;
          if (boot.activePersonaId && targetPersona) {
            await loadPersonaDetail(chosenRunId, targetPersona);
          }
        } catch (err) {
          pushToast(
            err instanceof Error ? `Failed to load run state: ${err.message}` : "Failed to load run state",
            "danger",
          );
        }
      } else if (!chosenRunId) {
        setRunState(null);
        setPersonaDetail(null);
      }
    },
    [loadRunState, loadPersonaDetail, pushToast, updateStreamConfigFromPayload],
  );

  // Connect to Antigravity MCP Apps host on mount.
  useEffect(() => {
    let disposed = false;
    let bootstrapTimer: number | null = null;
    const bridge = bridgeRef.current;
    const app = bridge.app;

    // Register ALL handlers before calling app.connect().
    app.ontoolinput = () => {
      if (disposed) return;
      if (!receivedToolResultRef.current) {
        setLoadingInitial(true);
      }
    };

    app.ontoolresult = (result) => {
      if (disposed) return;
      receivedToolResultRef.current = true;
      if (bootstrapTimer !== null) {
        window.clearTimeout(bootstrapTimer);
        bootstrapTimer = null;
      }
      const parsed = parseStructuredPayload<DashboardBootstrap>(
        result as Parameters<typeof parseStructuredPayload>[0],
      );
      if (parsed) {
        if (parsed.ok === false && parsed.error) {
          pushToast(parsed.error, "danger");
        }
        void applyBootstrap(parsed);
      } else {
        setLoadingInitial(false);
      }
    };

    app.ontoolcancelled = (params) => {
      if (disposed) return;
      setLoadingInitial(false);
      if (!receivedToolResultRef.current) {
        setConnectionStatus("cancelled");
        setConnectionMessage(params?.reason || "Tool invocation was cancelled");
      } else {
        pushToast("Previous action was cancelled", "warning");
      }
    };

    app.onhostcontextchanged = (ctx) => {
      if (disposed) return;
      applyHostContextToDocument(ctx);
    };

    const connectToHost = async () => {
      if (window.parent === window) {
        setConnectionStatus("standalone");
        setLoadingInitial(false);
        return;
      }

      try {
        if (!bridge.isConnected()) {
          await app.connect();
          bridge.markConnected();
        }
        if (disposed) return;

        applyHostContextToDocument(app.getHostContext());
        const caps = bridge.getCapabilitiesSummary();
        setCapabilities(caps);
        setConnectionStatus("connected");

        // Standalone global open fallback: if no ontoolresult arrives within 650ms, list runs directly.
        bootstrapTimer = window.setTimeout(async () => {
          if (disposed || receivedToolResultRef.current) return;
          if (!caps.canCallServerTools) {
            setLoadingInitial(false);
            return;
          }
          try {
            const list = await bridge.listRuns();
            if (disposed || receivedToolResultRef.current) return;
            receivedToolResultRef.current = true;
            await applyBootstrap({
              ok: list.ok,
              error: list.error,
              runs: list.runs ?? [],
              activeRunId: list.runs?.[0]?.run_id ?? null,
              activePersonaId: null,
              httpBaseUrl: list.httpBaseUrl,
              streamToken: list.streamToken,
            });
          } catch (err) {
            if (!disposed && !receivedToolResultRef.current) {
              setLoadingInitial(false);
              setConnectionStatus("error");
              setConnectionMessage(
                err instanceof Error ? err.message : "Failed to load QA runs",
              );
            }
          }
        }, 650);
      } catch (err) {
        if (disposed) return;
        setLoadingInitial(false);
        setConnectionStatus("error");
        setConnectionMessage(
          err instanceof Error ? err.message : "Unable to connect to MCP App host",
        );
      }
    };

    void connectToHost();

    return () => {
      disposed = true;
      if (bootstrapTimer !== null) {
        window.clearTimeout(bootstrapTimer);
      }
    };
  }, [applyBootstrap, pushToast]);

  // Fetch persona detail whenever entering Stage view or switching activePersonaId.
  useEffect(() => {
    if (
      connectionStatus !== "connected" ||
      activeTab !== "stage" ||
      !activeRunId ||
      !activePersonaId
    ) {
      return;
    }
    void loadPersonaDetail(activeRunId, activePersonaId).catch(() => {
      // toast handled inside loadPersonaDetail
    });
  }, [connectionStatus, activeTab, activeRunId, activePersonaId, loadPersonaDetail]);

  const hasLivePersonas = useMemo(() => {
    return (runState?.personas ?? []).some(
      (p) => isLivePersonaStatus(p.status) || p.status === "pending",
    );
  }, [runState?.personas]);

  // Background polling for active run state & active stage persona detail (strictly yielding).
  useEffect(() => {
    if (connectionStatus !== "connected" || !activeRunId) return;
    if (activeTab === "compare") return;

    const intervalMs = hasLivePersonas ? 2500 : 8000;
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      void loadRunState(activeRunId, { background: true }).catch(() => undefined);
      if (activeTab === "stage" && activePersonaIdRef.current && hasLivePersonas) {
        void loadPersonaDetail(activeRunId, activePersonaIdRef.current, {
          background: true,
        }).catch(() => undefined);
      }
    }, intervalMs);

    return () => window.clearInterval(timer);
  }, [connectionStatus, activeRunId, activeTab, hasLivePersonas, loadRunState, loadPersonaDetail]);

  const handleRefreshAll = useCallback(async () => {
    const bridge = bridgeRef.current;
    if (!bridge.isConnected()) return;
    setRefreshing(true);
    try {
      const list = await bridge.listRuns();
      updateStreamConfigFromPayload(list);
      const nextRuns = list.runs ?? [];
      setRuns(nextRuns);

      const targetRunId =
        activeRunIdRef.current && nextRuns.some((r) => r.run_id === activeRunIdRef.current)
          ? activeRunIdRef.current
          : nextRuns[0]?.run_id ?? null;

      setActiveRunId(targetRunId);
      activeRunIdRef.current = targetRunId;

      if (targetRunId) {
        const state = await loadRunState(targetRunId);
        const targetPersona =
          activePersonaIdRef.current &&
          state?.personas?.some((p) => p.id === activePersonaIdRef.current)
            ? activePersonaIdRef.current
            : state?.personas?.[0]?.id ?? null;
        if (activeTab === "stage" && targetPersona) {
          await loadPersonaDetail(targetRunId, targetPersona);
        }
      } else {
        setRunState(null);
        setPersonaDetail(null);
      }
      setConnectionStatus("connected");
    } catch (err) {
      pushToast(
        err instanceof Error ? `Refresh failed: ${err.message}` : "Refresh failed",
        "danger",
      );
    } finally {
      setRefreshing(false);
    }
  }, [activeTab, loadPersonaDetail, loadRunState, pushToast, updateStreamConfigFromPayload]);

  const handleSelectRun = useCallback(
    async (nextRunId: string) => {
      if (!nextRunId || nextRunId === activeRunIdRef.current) return;
      setActiveRunId(nextRunId);
      activeRunIdRef.current = nextRunId;
      setPersonaDetail(null);
      setRequestedStep(null);
      setRefreshing(true);
      try {
        const state = await loadRunState(nextRunId);
        if (activeTab === "stage" && state?.personas?.[0]?.id) {
          await loadPersonaDetail(nextRunId, state.personas[0].id);
        }
      } catch (err) {
        pushToast(
          err instanceof Error ? `Failed to switch run: ${err.message}` : "Failed to switch run",
          "danger",
        );
      } finally {
        setRefreshing(false);
      }
    },
    [activeTab, loadPersonaDetail, loadRunState, pushToast],
  );

  const handleInspectPersona = useCallback((personaId: string) => {
    setActivePersonaId(personaId);
    activePersonaIdRef.current = personaId;
    setRequestedStep(null);
    setRequestedA11yOn(false);
    setActiveTab("stage");
  }, []);

  const handleJumpToPersonaStep = useCallback(
    (personaId: string, step: number | null, enableA11y = false) => {
      setActivePersonaId(personaId);
      activePersonaIdRef.current = personaId;
      setRequestedStep(step);
      setRequestedA11yOn(enableA11y);
      setRequestNonce((n) => n + 1);
      setActiveTab("stage");
    },
    [],
  );

  const handleOpenMarkdown = useCallback(
    async (absPath: string) => {
      const opened = await bridgeRef.current.openFileInAuxPane(absPath);
      if (opened) {
        pushToast("Opened markdown report in IDE pane", "info");
      } else {
        pushToast(`Report path: ${absPath}`, "info");
      }
    },
    [pushToast],
  );

  const handleRefreshCurrentRun = useCallback(async () => {
    if (!activeRunIdRef.current) return;
    await loadRunState(activeRunIdRef.current);
    if (activePersonaIdRef.current) {
      await loadPersonaDetail(activeRunIdRef.current, activePersonaIdRef.current);
    }
  }, [loadPersonaDetail, loadRunState]);

  const headerMarkdownTarget = useMemo(() => {
    if (
      activeTab === "stage" &&
      personaDetail?.persona_id === activePersonaId &&
      personaDetail?.report_path
    ) {
      return { path: personaDetail.report_path, label: "report.md" };
    }
    if (runState?.summary_path) {
      return { path: runState.summary_path, label: "SUMMARY.md" };
    }
    return { path: null, label: "Report" };
  }, [activeTab, activePersonaId, personaDetail, runState?.summary_path]);

  const renderMainBody = () => {
    if (loadingInitial || connectionStatus === "connecting") {
      return (
        <div className="center-state">
          <span className="spinner-ring spinner-muted" aria-hidden="true" />
          <h3>Connecting to iOS QA Dashboard…</h3>
          <p>Waiting for Antigravity MCP host & loopback server handshake.</p>
        </div>
      );
    }

    if (connectionStatus === "standalone") {
      return (
        <div className="center-state">
          <div className="onboarding-card">
            <MatrixIcon width={28} height={28} className="empty-icon" />
            <h2>iOS Agent Testers — QA Dashboard</h2>
            <p>
              Open this MCP App inside Antigravity&apos;s side pane (<strong>iOS QA 測試台</strong>)
              or sidebar global view to stream live multi-persona iOS simulators, take over touch
              control, inspect A11y overlays, and triage QA summaries.
            </p>
          </div>
        </div>
      );
    }

    if (connectionStatus === "error" || connectionStatus === "cancelled") {
      return (
        <div className="center-state">
          <div className="onboarding-card">
            <h3>{connectionStatus === "cancelled" ? "Dashboard Cancelled" : "Connection Issue"}</h3>
            {connectionMessage && <p>{connectionMessage}</p>}
            <button
              type="button"
              className="primary-cta"
              onClick={() => void handleRefreshAll()}
            >
              <RefreshIcon width={14} height={14} />
              <span>Retry Loading Runs</span>
            </button>
          </div>
        </div>
      );
    }

    if (runs.length === 0 || !activeRunId) {
      return (
        <div className="center-state">
          <div className="onboarding-card">
            <MatrixIcon width={30} height={30} className="empty-icon" />
            <h2>No QA Runs Recorded Yet</h2>
            <p>
              Start a multi-persona exploratory, bug-reproduction, or UX-improvement QA pass on an
              iOS Simulator app to watch personas stream live here.
            </p>
            <div className="onboarding-steps">
              <div className="onboarding-step">
                <span className="step-num-pill">1</span>
                <div>
                  <strong>Ask Antigravity</strong>
                  <p className="onboarding-code-hint">
                    &ldquo;Run a smoke iOS QA pass (clara, amira, sven) on my iOS app and open the QA
                    dashboard.&rdquo;
                  </p>
                </div>
              </div>
              <div className="onboarding-step">
                <span className="step-num-pill">2</span>
                <div>
                  <strong>Or create a run from CLI</strong>
                  <pre className="onboarding-cli">
                    <TerminalIcon width={12} height={12} />{" "}
                    <code>ios-agent-testers run create --app MyApp.app --personas smoke</code>
                  </pre>
                </div>
              </div>
            </div>
            <button
              type="button"
              className="primary-cta"
              onClick={() => void handleRefreshAll()}
              disabled={refreshing}
            >
              <RefreshIcon width={14} height={14} />
              <span>{refreshing ? "Checking…" : "Check for Runs"}</span>
            </button>
          </div>
        </div>
      );
    }

    if (activeTab === "compare") {
      return (
        <CompareRunsView
          runs={runs}
          activeRunId={activeRunId}
          bridge={bridgeRef.current}
          onOpenMarkdown={(p) => void handleOpenMarkdown(p)}
          onToast={pushToast}
        />
      );
    }

    if (!runState) {
      return (
        <div className="center-state">
          <span className="spinner-ring spinner-muted" aria-hidden="true" />
          <h3>Loading Run {activeRunId}…</h3>
        </div>
      );
    }

    if (activeTab === "matrix") {
      return (
        <LiveMatrixView
          runState={runState}
          streamConfig={streamConfig}
          isActiveView={activeTab === "matrix"}
          onInspectPersona={handleInspectPersona}
        />
      );
    }

    if (activeTab === "stage") {
      return (
        <StageInspectorView
          runId={activeRunId}
          personas={runState.personas}
          activePersonaId={activePersonaId}
          detail={personaDetail}
          loadingDetail={loadingDetail}
          streamConfig={streamConfig}
          capabilities={capabilities}
          bridge={bridgeRef.current}
          requestedStep={requestedStep}
          requestedA11yOn={requestedA11yOn}
          requestNonce={requestNonce}
          onSelectPersona={(pid) => {
            setActivePersonaId(pid);
            activePersonaIdRef.current = pid;
            setRequestedStep(null);
          }}
          onBackToMatrix={() => setActiveTab("matrix")}
          onRefreshState={handleRefreshCurrentRun}
          onOpenMarkdown={(p) => void handleOpenMarkdown(p)}
          onToast={pushToast}
        />
      );
    }

    return (
      <SummaryReportView
        runId={activeRunId}
        summary={runState.summary}
        summaryPath={runState.summary_path}
        personas={runState.personas}
        onJumpToPersonaStep={handleJumpToPersonaStep}
        onOpenMarkdown={(p) => void handleOpenMarkdown(p)}
      />
    );
  };

  return (
    <div className="app-shell">
      <DashboardHeader
        runs={runs}
        activeRunId={activeRunId}
        runState={runState}
        activePersonaId={activePersonaId}
        activeTab={activeTab}
        refreshing={refreshing}
        loadingInitial={loadingInitial}
        markdownPath={headerMarkdownTarget.path}
        markdownLabel={headerMarkdownTarget.label}
        onSelectRun={(id) => void handleSelectRun(id)}
        onSelectTab={(tab) => setActiveTab(tab)}
        onRefresh={() => void handleRefreshAll()}
        onOpenMarkdown={(p) => void handleOpenMarkdown(p)}
      />

      <main className="app-main">{renderMainBody()}</main>

      <ToastRegion
        toasts={toasts}
        liveAnnouncement={liveAnnouncement}
        onDismiss={dismissToast}
      />
    </div>
  );
}
