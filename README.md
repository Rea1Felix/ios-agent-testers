# iOS Agent Testers

**Zero-script multi-persona iOS Simulator QA engine & live Antigravity MCP App dashboard.**

`ios-agent-testers` brings the full iOS feature set of [`pierridotite/mobile-agent-testers`](https://github.com/pierridotite/mobile-agent-testers) natively to macOS and Antigravity on top of [`Rea1Felix/ios-phone-pane`](https://github.com/Rea1Felix/ios-phone-pane)'s `fb-idb 1.7.8` + `xcrun simctl` + loopback MJPEG + Preact MCP App architecture — with zero extra Python packages beyond `terminal-phone`'s existing `idb-venv` and zero need for `axe` or external CI runners.

## Key Capabilities

- **10 Diverse Built-in iOS Personas** (`personas.json`): Spanning iPhone SE (3rd gen) to iPhone 16 Pro Max and iPad mini / Air / Pro across 10 locales (including RTL `ar-SA`), Dynamic Type up to `accessibility-extra-extra-extra-large`, light/dark themes, GPS coordinates, and clean US QWERTY keyboard hygiene.
- **4 Native QA Modes**:
  - `explore`: Autonomous exploratory usability, layout, localization, crash, and accessibility testing.
  - `reproduce`: Fetch a GitHub issue (`owner/repo#N` or URL) or raw bug report and attempt reproduction with strict prompt-injection isolation.
  - `improve`: Goal-driven UX walkthrough tracking `goal_reached`, action count, and prioritized improvements across 10 product areas.
  - `targeted`: Focused verification guided by a custom goal or brief.
- **Deterministic Per-Step Auditing & Telemetry**:
  - Point-scaled PNG step screenshots (`01.png..NN.png`, screenshot pixels == tap points).
  - Nested `fb-idb` accessibility tree extraction with semantic element refs (`e1`, `e2`, ...).
  - Deterministic A11y audit flagging unnamed active controls (excluding `AXValue` as a label) and `<44pt` touch targets with scroll-container edge-clipping awareness and bounding-box coordinates for visual overlays.
  - Live process-alive checks, `fb-idb` + `.ips` crash detection, streaming app log tailing, and optional H.264 video capture.
- **Live Antigravity QA Dashboard (`iOS QA 測試台`)**:
  - Dual-surface (`aux_pane` + `global`) Preact MCP App with **Live Matrix**, **Stage Inspector** (24fps MJPEG stream, interactive tap/swipe/keyboard, **Pause Agent & Take Over** + steering note handoff, step filmstrip, toggleable SVG A11y overlays), **Summary Triage** (11 issue classes), and **Run A vs. Run B Compare**.
- **Joint Runtime Resolver**: Matches `(device_type, runtime)` jointly against each available iOS runtime's `supportedDeviceTypes` (preventing mismatches on Xcode hosts where newer specialty runtimes like iOS 27.1 only support a subset of devices).
- **Standalone CLI + MCP Server + Antigravity Skill & `run_workflow` Matrix**: Share one Python 3.13 core across terminal workflows and Antigravity agent orchestration.

## Architecture

```text
ios-agent-testers/
├── plugin.json                        # Antigravity plugin manifest
├── mcp_config.json                    # Registers MCP server `testers` (ios-agent-testers_testers)
├── personas.json                      # 10 iOS personas & built-in presets
├── assets/logo.svg                    # 128x128 plugin icon
├── briefs/                            # AGENT_BRIEF.md, reproduce.md, improve.md, targeted.md
├── ios_agent_testers/
│   ├── core/                          # Shared Python 3.13 core (zero extra deps)
│   │   ├── personas.py                # Persona loader, validator, preset selector
│   │   ├── sim_provisioner.py         # Joint runtime resolver, simctl provisioning, teardown, doctor
│   │   ├── idb_driver.py              # fb-idb 1.7.8 driver, StreamHub MJPEG, crash & log capture
│   │   ├── a11y.py                    # Deterministic iOS A11y audit + overlay geometry
│   │   ├── builder.py                 # Framework detector & unsigned arm64 Simulator builder
│   │   ├── run_store.py               # Atomic runs/ state store & human takeover coordination
│   │   ├── bug_report.py              # GitHub issue fetcher & bug report formatter
│   │   ├── briefs.py                  # Persona prompt renderer
│   │   ├── summarize.py               # Cross-persona deduplication -> SUMMARY.md & summary.json
│   │   └── compare.py                 # Run A vs Run B comparison -> COMPARE.md & compare.json
│   └── cli.py                         # Standalone CLI entrypoint
├── bin/ios-agent-testers              # Executable CLI wrapper
├── server/
│   ├── bin/ios-agent-testers-server   # MCP stdio launcher (idb-venv Python 3.13)
│   ├── http_server.py                 # Pure-asyncio 127.0.0.1:0 server (192-bit token)
│   └── main.py                        # JSON-RPC 2.0 MCP server (11 model + 12 app-only tools)
├── skills/ios-agent-testers/SKILL.md  # Antigravity QA orchestration skill
├── agents/ios-persona-tester.md       # Dedicated persona tester agent profile
├── workflows/run_qa_matrix.py         # Antigravity `run_workflow` bounded-concurrency matrix
├── web/ -> dist/app.html              # Preact + Vite singlefile MCP App bundle
└── tests/                             # Unit & MCP protocol/security test suites
```

## Prerequisites

- macOS with Xcode & iOS Simulator runtime installed (`xcrun simctl`)
- [`terminal-phone`](https://github.com/Rea1Felix/ios-phone-pane) dependencies initialized (`~/Library/Application Support/terminal-phone/dependencies/{idb-venv,companion}`)
- Optional: GitHub CLI (`gh`) authenticated if fetching private/public GitHub issues in `reproduce` mode

Verify your environment at any time:

```bash
./bin/ios-agent-testers doctor
```

## Installing as an Antigravity Plugin

Link this repository into `~/.gemini/config/plugins/ios-agent-testers`:

```bash
./bin/ios-agent-testers install-plugin
```

To uninstall the symlink later:

```bash
./bin/ios-agent-testers install-plugin --uninstall
```

`ios-agent-testers` is completely isolated from `ios-phone-pane` and `terminal-phone`:
- Plugin name: `ios-agent-testers` (MCP namespace: `ios-agent-testers_testers`)
- UI resource URI: `ui://ios-agent-testers/app.html`
- Entrypoint title: `iOS QA 測試台`
- Ephemeral companion sockets: `/tmp/ios-agent-testers-<uid>/`
- Tool names: `open_qa_dashboard`, `qa_*`, `app_qa_*`

## Built-in Personas

| ID | Device | Locale | Text Size | Theme | Focus |
|---|---|---|---|---|---|
| `clara` | iPhone SE (3rd generation) | `fr-FR` | `large` | `light` | Pixel alignment, French localization, refuses location |
| `jonas` | iPad Air 11-inch (M2) | `de-DE` | `accessibility-extra-large` | `dark` | iPad + large a11y text + edge-case inputs |
| `noah` | iPhone 16 Pro Max | `en-US` | `extra-extra-extra-large` | `light` | Fast main-task completion at max standard type |
| `yuki` | iPhone 16 | `ja-JP` | `medium` | `dark` | Japanese dark mode visual polish |
| `amira` | iPhone 16 Pro | `ar-SA` | `large` | `light` | Arabic RTL mirroring, navigation, icons & numerals |
| `mateo` | iPad mini (A17 Pro) | `es-MX` | `large` | `light` | Compact iPad, mid-task backgrounding/resume |
| `sven` | iPhone SE (3rd generation) | `sv-SE` | `accessibility-extra-extra-extra-large` | `dark` | Extreme Dynamic Type on smallest screen |
| `giulia` | iPhone 16 Plus | `it-IT` | `accessibility-medium` | `light` | Fine print, legal/copy truncation audit |
| `minjun` | iPad Pro 13-inch (M4) | `ko-KR` | `small` | `dark` | Large iPad density & stretched-layout audit |
| `wei` | iPhone 16 | `zh-Hans-CN` | `large` | `light` | Simplified Chinese layout & CJK input |

Presets: `smoke` (`clara`, `amira`, `sven`), `all`, `iphone`, `ipad`, `a11y`.

## Antigravity Usage

Ask Antigravity naturally — the bundled `ios-agent-testers` skill handles the full flow:
- *"Run a smoke QA test (`clara`, `amira`, `sven`) on `/path/to/MyApp` and open the QA dashboard."*
- *"Reproduce `owner/repo#42` on my iOS app across `smoke` personas."*
- *"Run an `improve` UX walkthrough on my app and suggest high-impact onboarding fixes."*
- *"Compare the last two QA runs and show regressions."*

During a run, click **Pause Agent & Take Over** in the **iOS QA 測試台** Stage Inspector to interact directly with any live simulator (tap, swipe, type, theme toggle). Leave an optional steering note and click **Resume Agent** — the persona subagent receives your note once on its very next observation without ever fighting your input.

## Standalone CLI Reference

```bash
./bin/ios-agent-testers doctor
./bin/ios-agent-testers personas --selector smoke
./bin/ios-agent-testers build /path/to/MyApp --configuration Debug
./bin/ios-agent-testers run create --bundle-id com.apple.Preferences --personas smoke --mode explore
./bin/ios-agent-testers run list
./bin/ios-agent-testers run status <run_id>
./bin/ios-agent-testers sim provision <run_id> sven
./bin/ios-agent-testers sim observe <run_id> sven
./bin/ios-agent-testers sim teardown <run_id> sven
./bin/ios-agent-testers summarize <run_id>
./bin/ios-agent-testers compare <run_a> <run_b> --name-a v1 --name-b v2
```

## MCP Tools Summary

- **Model-visible (11)**: `open_qa_dashboard`, `qa_list_personas`, `qa_build_app`, `qa_create_run`, `qa_provision_persona`, `qa_observe`, `qa_act`, `qa_record_findings`, `qa_finish_persona`, `qa_summarize_run`, `qa_compare_runs`.
- **App-only (`visibility: ["app"]`, 12)**: `app_qa_list_runs`, `app_qa_get_run_state`, `app_qa_get_persona_detail`, `app_qa_frame`, `app_qa_tap`, `app_qa_swipe`, `app_qa_type`, `app_qa_key`, `app_qa_button`, `app_qa_device_action`, `app_qa_toggle_takeover`, `app_qa_compare`.

## Local Safety

- **No root/firewall mutations**: Unlike CI-only scripts, `ios-agent-testers` never modifies `/etc/hosts` or `pfctl`. Use `build_args` or staging endpoints to isolate analytics locally.
- **Loopback-only authenticated HTTP**: Streaming (`/stream`), thumbnails (`/snapshot`), and run files (`/artifact`) bind exclusively to `127.0.0.1:0`, enforce a 192-bit per-session token checked via `hmac.compare_digest` (never exposed in model text `content`), and enforce canonical path-containment inside `runs_root`.
- **Bounded concurrency**: Defaults to `max_concurrency=2` (max `4`) with automatic simulator deletion on `qa_finish_persona` unless `keep_simulator: true`.

## Running Tests

```bash
IDB_PY="$HOME/Library/Application Support/terminal-phone/dependencies/idb-venv/bin/python3"
PYTHONPATH=. "$IDB_PY" -m unittest discover -s tests -v

cd web && npm install && npm run typecheck && npm run build
```

## License

MIT — see [LICENSE](LICENSE).
