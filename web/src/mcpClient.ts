import {
  App,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
  type McpUiHostContext,
  type StandardSchemaV1,
} from "@modelcontextprotocol/ext-apps";
import type {
  CompareToolResult,
  DeviceActionResult,
  DeviceActionType,
  FrameResult,
  HostCapabilitiesSummary,
  ListRunsResult,
  PersonaDetailResult,
  PhoneHardwareButton,
  PhoneKey,
  RunStateResult,
  StreamConfig,
  TakeoverResult,
  ToolResultBase,
} from "./types";

interface CallToolResultLike {
  isError?: boolean;
  structuredContent?: Record<string, unknown>;
  content?: Array<{ type?: string; text?: string }>;
}

interface ToggleAuxPaneResult {
  isError?: boolean;
  [key: string]: unknown;
}

const toggleAuxPaneResultSchema: StandardSchemaV1<ToggleAuxPaneResult> = {
  "~standard": {
    version: 1,
    vendor: "ios-agent-testers",
    validate(value: unknown) {
      if (value && typeof value === "object") {
        return { value: value as ToggleAuxPaneResult };
      }
      return { value: {} };
    },
  },
};

export function extractTextError(result: CallToolResultLike | undefined): string | undefined {
  if (!result?.content) return undefined;
  const parts = result.content
    .filter((block) => block?.type === "text" && typeof block.text === "string")
    .map((block) => block.text!.trim())
    .filter(Boolean);
  return parts.length > 0 ? parts.join(" ") : undefined;
}

export function parseStructuredPayload<T extends ToolResultBase>(
  result: CallToolResultLike | undefined,
): T | null {
  if (!result) return null;
  if (result.structuredContent && typeof result.structuredContent === "object") {
    const sc = result.structuredContent as T;
    if (result.isError && sc.ok !== false) {
      return {
        ...sc,
        ok: false,
        error: sc.error ?? sc.message ?? extractTextError(result) ?? "Tool call failed",
      };
    }
    return sc;
  }

  const text = extractTextError(result);
  if (text && (text.startsWith("{") || text.startsWith("["))) {
    try {
      const parsed = JSON.parse(text) as T;
      if (parsed && typeof parsed === "object") {
        return parsed;
      }
    } catch {
      // fall through
    }
  }

  if (result.isError) {
    return {
      ok: false,
      error: text ?? "Tool execution failed",
    } as T;
  }

  return null;
}

export function applyHostContextToDocument(ctx: McpUiHostContext | undefined): void {
  if (!ctx) return;
  if (ctx.theme) {
    applyDocumentTheme(ctx.theme);
  }
  if (ctx.styles?.variables) {
    applyHostStyleVariables(ctx.styles.variables);
  }
  if (ctx.styles?.css?.fonts) {
    applyHostFonts(ctx.styles.css.fonts);
  }
}

export function toFileUri(absPath: string): string {
  const normalized = absPath.replace(/\\/g, "/");
  const encoded = normalized
    .split("/")
    .map((segment, idx) => (idx === 0 && segment === "" ? "" : encodeURIComponent(segment)))
    .join("/");
  return encoded.startsWith("/") ? `file://${encoded}` : `file:///${encoded}`;
}

export function buildStreamUrl(
  cfg: StreamConfig,
  udid: string | null | undefined,
  opts?: { fps?: number; scale?: number; nonce?: number },
): string | null {
  if (!cfg.httpBaseUrl || !cfg.streamToken || !udid) return null;
  const base = cfg.httpBaseUrl.replace(/\/+$/, "");
  const params = new URLSearchParams({
    udid,
    fps: String(opts?.fps ?? 24),
    scale: String(opts?.scale ?? 0.5),
    token: cfg.streamToken,
  });
  if (opts?.nonce !== undefined) {
    params.set("v", String(opts.nonce));
  }
  return `${base}/stream?${params.toString()}`;
}

export function buildSnapshotUrl(
  cfg: StreamConfig,
  udid: string | null | undefined,
  opts?: { maxWidth?: number; quality?: number; tick?: number },
): string | null {
  if (!cfg.httpBaseUrl || !cfg.streamToken || !udid) return null;
  const base = cfg.httpBaseUrl.replace(/\/+$/, "");
  const params = new URLSearchParams({
    udid,
    max_width: String(opts?.maxWidth ?? 320),
    quality: String(opts?.quality ?? 0.65),
    token: cfg.streamToken,
  });
  if (opts?.tick !== undefined) {
    params.set("t", String(opts.tick));
  }
  return `${base}/snapshot?${params.toString()}`;
}

export function buildArtifactUrl(
  cfg: StreamConfig,
  runId: string | null | undefined,
  personaId: string | null | undefined,
  relFile: string | null | undefined,
): string | null {
  if (!cfg.httpBaseUrl || !cfg.streamToken || !runId || !personaId || !relFile) {
    return null;
  }
  const base = cfg.httpBaseUrl.replace(/\/+$/, "");
  const params = new URLSearchParams({
    run: runId,
    persona: personaId,
    file: relFile,
    token: cfg.streamToken,
  });
  return `${base}/artifact?${params.toString()}`;
}

interface QueuedTask<T> {
  priority: number; // 0 = control/takeover/nav, 1 = gesture, 2 = background poll/frame
  run: () => Promise<T>;
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
}

/**
 * Antigravity enforces at most 4 in-flight `app.callServerTool` requests.
 * We strictly cap concurrency at 3, keep gestures serialized, and ensure
 * background polling / Plan-B frame capture always yields to interactive controls.
 */
export class QaMcpBridge {
  readonly app: App;
  private connected = false;
  private inFlight = 0;
  private readonly maxInFlight = 3;
  private readonly queue: Array<QueuedTask<unknown>> = [];

  private gestureChain: Promise<unknown> = Promise.resolve();
  private pendingGestures = 0;
  private readonly maxPendingGestures = 6;
  private frameInFlight = false;
  private statePollInFlight = false;
  private detailPollInFlight = false;

  constructor() {
    this.app = new App(
      { name: "ios-agent-testers-dashboard", version: "0.1.0" },
      {},
      { autoResize: true },
    );
  }

  isConnected(): boolean {
    return this.connected;
  }

  markConnected(): void {
    this.connected = true;
  }

  getCapabilitiesSummary(): HostCapabilitiesSummary {
    const caps = this.app.getHostCapabilities();
    return {
      canUpdateModelContext: Boolean(caps?.updateModelContext),
      canSendMessage: Boolean(caps?.message),
      canCallServerTools: Boolean(caps?.serverTools),
    };
  }

  private pump(): void {
    while (this.inFlight < this.maxInFlight && this.queue.length > 0) {
      this.queue.sort((a, b) => a.priority - b.priority);
      const next = this.queue.shift();
      if (!next) break;
      this.inFlight += 1;
      next
        .run()
        .then(next.resolve, next.reject)
        .finally(() => {
          this.inFlight = Math.max(0, this.inFlight - 1);
          this.pump();
        });
    }
  }

  private schedule<T>(priority: number, fn: () => Promise<T>): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      this.queue.push({
        priority,
        run: fn,
        resolve: resolve as (value: unknown) => void,
        reject,
      });
      this.pump();
    });
  }

  private async invokeTool<T extends ToolResultBase>(
    name: string,
    args: Record<string, unknown>,
    priority: number,
  ): Promise<T> {
    if (!this.connected) {
      throw new Error("Not connected to Antigravity host");
    }
    const raw = await this.schedule(priority, () =>
      this.app.callServerTool({ name, arguments: args }),
    );
    const parsed = parseStructuredPayload<T>(raw as CallToolResultLike);
    if (!parsed) {
      throw new Error(`Tool ${name} returned no structured payload`);
    }
    return parsed;
  }

  /* ---------- Run & Persona State Tools ---------- */

  listRuns(): Promise<ListRunsResult> {
    return this.invokeTool<ListRunsResult>("app_qa_list_runs", {}, 0);
  }

  getRunState(runId?: string, opts?: { background?: boolean }): Promise<RunStateResult | null> {
    const args: Record<string, unknown> = {};
    if (runId) args.run_id = runId;

    if (opts?.background) {
      if (!this.connected || this.statePollInFlight || this.inFlight >= this.maxInFlight - 1) {
        return Promise.resolve(null);
      }
      this.statePollInFlight = true;
      return this.invokeTool<RunStateResult>("app_qa_get_run_state", args, 2).finally(() => {
        this.statePollInFlight = false;
      });
    }

    return this.invokeTool<RunStateResult>("app_qa_get_run_state", args, 0);
  }

  getPersonaDetail(
    runId: string,
    personaId: string,
    opts?: { background?: boolean },
  ): Promise<PersonaDetailResult | null> {
    const args = { run_id: runId, persona_id: personaId };
    if (opts?.background) {
      if (!this.connected || this.detailPollInFlight || this.inFlight >= this.maxInFlight - 1) {
        return Promise.resolve(null);
      }
      this.detailPollInFlight = true;
      return this.invokeTool<PersonaDetailResult>("app_qa_get_persona_detail", args, 2).finally(
        () => {
          this.detailPollInFlight = false;
        },
      );
    }

    return this.invokeTool<PersonaDetailResult>("app_qa_get_persona_detail", args, 0);
  }

  compareRuns(
    runA: string,
    runB: string,
    nameA?: string,
    nameB?: string,
  ): Promise<CompareToolResult> {
    const args: Record<string, unknown> = { run_a: runA, run_b: runB };
    if (nameA) args.name_a = nameA;
    if (nameB) args.name_b = nameB;
    return this.invokeTool<CompareToolResult>("app_qa_compare", args, 0);
  }

  /* ---------- Plan B Fallback Frame Fetch ---------- */

  async fetchFrame(params: {
    udid: string;
    maxWidth?: number;
    quality?: number;
  }): Promise<FrameResult | null> {
    if (!this.connected || this.frameInFlight) return null;
    if (this.inFlight >= this.maxInFlight - 1) return null;

    this.frameInFlight = true;
    try {
      const args: Record<string, unknown> = { udid: params.udid };
      if (params.maxWidth !== undefined) args.maxWidth = params.maxWidth;
      if (params.quality !== undefined) args.quality = params.quality;
      return await this.invokeTool<FrameResult>("app_qa_frame", args, 2);
    } finally {
      this.frameInFlight = false;
    }
  }

  /* ---------- Serialized Simulator Input & Control Tools ---------- */

  private enqueueGesture<T>(task: () => Promise<T>): Promise<T | null> {
    if (this.pendingGestures >= this.maxPendingGestures) {
      return Promise.resolve(null);
    }
    this.pendingGestures += 1;
    const resultPromise = this.gestureChain
      .catch(() => undefined)
      .then(async () => {
        try {
          return await task();
        } finally {
          this.pendingGestures = Math.max(0, this.pendingGestures - 1);
        }
      });
    this.gestureChain = resultPromise;
    return resultPromise;
  }

  tap(udid: string, x: number, y: number): Promise<ToolResultBase | null> {
    return this.enqueueGesture(() =>
      this.invokeTool<ToolResultBase>("app_qa_tap", { udid, x, y }, 1),
    );
  }

  swipe(
    udid: string,
    x1: number,
    y1: number,
    x2: number,
    y2: number,
    duration?: number,
  ): Promise<ToolResultBase | null> {
    return this.enqueueGesture(() => {
      const args: Record<string, unknown> = { udid, x1, y1, x2, y2 };
      if (duration !== undefined) args.duration = duration;
      return this.invokeTool<ToolResultBase>("app_qa_swipe", args, 1);
    });
  }

  typeText(udid: string, text: string): Promise<ToolResultBase> {
    return this.invokeTool<ToolResultBase>("app_qa_type", { udid, text }, 0);
  }

  sendKey(udid: string, key: PhoneKey): Promise<ToolResultBase> {
    return this.invokeTool<ToolResultBase>("app_qa_key", { udid, key }, 0);
  }

  pressButton(udid: string, button: PhoneHardwareButton): Promise<ToolResultBase> {
    return this.invokeTool<ToolResultBase>("app_qa_button", { udid, button }, 0);
  }

  deviceAction(udid: string, action: DeviceActionType): Promise<DeviceActionResult> {
    return this.invokeTool<DeviceActionResult>("app_qa_device_action", { udid, action }, 0);
  }

  toggleTakeover(
    runId: string,
    personaId: string,
    paused: boolean,
    note?: string,
  ): Promise<TakeoverResult> {
    const args: Record<string, unknown> = {
      run_id: runId,
      persona_id: personaId,
      paused,
    };
    if (note !== undefined && note.trim().length > 0) {
      args.note = note.trim();
    }
    return this.invokeTool<TakeoverResult>("app_qa_toggle_takeover", args, 0);
  }

  /* ---------- Antigravity Host Integrations ---------- */

  async openFileInAuxPane(absPath: string): Promise<boolean> {
    if (!this.connected || !absPath) return false;
    try {
      const res = await this.app.request(
        {
          method: "com.google.antigravity/toggle-aux-pane",
          params: {
            tabType: "fileView",
            uri: toFileUri(absPath),
          },
        },
        toggleAuxPaneResultSchema,
      );
      return !res?.isError;
    } catch {
      return false;
    }
  }

  async attachInspectorContext(params: {
    runId: string;
    personaId: string;
    stepNumber?: number | null;
    screenshotFile?: string | null;
    a11ySummary?: string | null;
    humanNote?: string | null;
  }): Promise<void> {
    const caps = this.getCapabilitiesSummary();
    if (!caps.canUpdateModelContext) {
      throw new Error("Current surface does not support attaching model context");
    }

    const lines: string[] = [
      `iOS QA Dashboard Inspector — Run ${params.runId}, Persona ${params.personaId}`,
    ];
    if (params.stepNumber != null) {
      lines.push(`Inspecting Step #${String(params.stepNumber).padStart(2, "0")}`);
    } else {
      lines.push("Inspecting Live Simulator View");
    }
    if (params.a11ySummary) {
      lines.push(`A11y Audit: ${params.a11ySummary}`);
    }
    if (params.humanNote) {
      lines.push(`Human Steering Note: ${params.humanNote}`);
    }

    const content: Array<
      | { type: "text"; text: string }
      | { type: "resource_link"; uri: string; name: string; mimeType: string }
    > = [{ type: "text", text: lines.join("\n") }];

    if (params.screenshotFile) {
      content.push({
        type: "resource_link",
        uri: `ios-testers://run/${encodeURIComponent(params.runId)}/${encodeURIComponent(params.personaId)}/${encodeURIComponent(params.screenshotFile)}`,
        name: `${params.personaId}-${params.screenshotFile}`,
        mimeType: "image/png",
      });
    }

    await this.app.updateModelContext({ content });
  }

  async askAgentToInspect(params: {
    runId: string;
    personaId: string;
    deviceLabel?: string | null;
    stepNumber?: number | null;
    screenshotFile?: string | null;
    a11ySummary?: string | null;
    humanNote?: string | null;
  }): Promise<{ isError?: boolean }> {
    const caps = this.getCapabilitiesSummary();
    if (!caps.canSendMessage) {
      throw new Error("Current surface does not support sending messages to the agent");
    }

    if (caps.canUpdateModelContext) {
      try {
        await this.attachInspectorContext(params);
      } catch {
        // non-fatal; proceed with message
      }
    }

    const parts: string[] = [
      `Please inspect QA run \`${params.runId}\` for persona \`${params.personaId}\`${params.deviceLabel ? ` (${params.deviceLabel})` : ""}.`,
    ];
    if (params.stepNumber != null) {
      parts.push(
        `Focus on Step #${String(params.stepNumber).padStart(2, "0")}${params.screenshotFile ? ` (${params.screenshotFile})` : ""}.`,
      );
    } else {
      parts.push("Focus on the current live simulator state.");
    }
    if (params.a11ySummary) {
      parts.push(`A11y check flagged: ${params.a11ySummary}`);
    }
    if (params.humanNote && params.humanNote.trim()) {
      parts.push(`Human note: ${params.humanNote.trim()}`);
    }

    return this.app.sendMessage({
      role: "user",
      content: [{ type: "text", text: parts.join(" ") }],
    });
  }
}
