---
name: ios-agent-testers
description: >-
  Run zero-script multi-persona iOS Simulator QA passes (smoke, all, reproduce,
  improve, targeted, or A/B compare) using ephemeral iOS simulators, fb-idb
  accessibility auditing, semantic issue deduplication, and the live Antigravity
  iOS QA Dashboard. Activate whenever the user asks to QA-test, explore,
  reproduce a bug on, audit accessibility/RTL/Dynamic Type for, or compare
  builds of an iOS app.
---

# iOS Agent Testers Skill

Use this skill to run multi-persona iOS Simulator QA sessions locally on macOS via the `ios-agent-testers` MCP server (`qa_*` tools), the `workflows/run_qa_matrix.py` matrix orchestrator, and the live **iOS QA 測試台** MCP App dashboard.

## Persona Presets & Test Modes

- **Persona selectors** (`qa_list_personas` / `qa_create_run`):
  - `smoke` *(default, fast & diverse)*: `clara` (iPhone SE, fr-FR, light), `amira` (iPhone 16 Pro, ar-SA RTL, light), `sven` (iPhone SE, sv-SE, `accessibility-extra-extra-extra-large`, dark)
  - `all`: all 10 personas (`clara`, `jonas`, `noah`, `yuki`, `amira`, `mateo`, `sven`, `giulia`, `minjun`, `wei`)
  - `iphone`: 7 iPhone personas
  - `ipad`: 3 iPad personas (`jonas`, `mateo`, `minjun`)
  - `a11y`: 4 large/accessibility Dynamic Type personas (`sven`, `jonas`, `giulia`, `noah`)
  - Custom: comma-separated persona IDs (e.g. `"clara,sven"`) or array `["clara", "sven"]`
- **Test modes**:
  - `explore` *(default)*: open-ended persona exploration, usability, layout, localization, accessibility, and crash hunting
  - `reproduce`: verify whether a GitHub issue (`https://github.com/owner/repo/issues/N` or `owner/repo#N`) or plain-text bug report reproduces across devices/locales (strict prompt-injection guard applies)
  - `improve`: goal-oriented UX walkthrough scoring `goal_reached` (`yes|partly|no`), action count, and prioritized improvements across 10 areas
  - `targeted`: focused scenario driven by `custom_goal` or a custom `brief_path`

## Standard Workflow

### Step 1: Resolve or Build the Target iOS App

- If the user gave a source project directory (Flutter, Capacitor, Expo, React Native, .NET MAUI, KMP, or native Xcode):
  1. Call `qa_build_app(project_path=..., scheme=..., configuration="Debug", build_args=..., clean=false)`. Incremental builds automatically reuse a deterministic DerivedData cache (`derived_data_dir`); pass `clean: true` only when a fresh clean rebuild is needed.
  2. Capture `app_path`, `bundle_id`, and `derived_data_dir` from the result.
- If the user already has a simulator `.app` path, pass `app_path` directly (bundle ID is extracted automatically from `Info.plist`).
- To test a pre-installed or system iOS app (e.g., smoke-testing `com.apple.Preferences`), pass `bundle_id` without `app_path`.

### Step 2: Create the Run & Open the Live Dashboard Immediately

1. Call `qa_create_run`:
   ```json
   {
     "app_path": "/path/to/MyApp.app",
     "bundle_id": "com.example.myapp",
     "personas": "smoke",
     "mode": "explore",
     "bug_report": "optional owner/repo#123 or text",
     "custom_goal": "optional focused goal",
     "record_video": false,
     "max_concurrency": 2,
     "max_actions": 30
   }
   ```
   Keep `max_concurrency` at `2` (range `1..4`) on laptops to avoid simulator RAM/CPU thrashing, and set `max_actions` (`1..100`, default `30`) to control the per-persona action budget independently from observations.
2. **Immediately** call `open_qa_dashboard(run_id=<run_id>)` so the user gets live MJPEG/thumbnail monitoring, step filmstrip inspection, A11y bounding-box overlays, and the **Pause & Take Over** button from the start.

### Step 3: Execute Persona Testers

- **Multi-persona runs (2+ personas)** — use `run_workflow` with `workflows/run_qa_matrix.py` (located under the installed plugin directory, e.g. `~/.gemini/config/plugins/ios-agent-testers/workflows/run_qa_matrix.py`):
  ```text
  run_workflow(
    ScriptPath="<plugin_root>/workflows/run_qa_matrix.py",
    Description="Run iOS QA matrix (<mode>, <personas>)",
    Args={
      "run_id": "<run_id>",
      "personas": ["clara", "amira", "sven"],
      "mode": "explore",
      "max_concurrency": 2,
      "max_actions": 30,
      "keep_simulator": false
    }
  )
  ```
  The workflow automatically batches personas by `max_concurrency`, isolates per-persona exceptions so sibling simulators are never cancelled, and executes the semantic deduplication + summary phase at the end.
- **Single-persona quick run** — you may run the loop directly:
  1. `qa_provision_persona(run_id, persona_id)` — follow the returned `persona_prompt`.
  2. `qa_observe(run_id, persona_id)` -> iterative `qa_act(run_id, persona_id, action={...})` up to `max_actions` actions (tracked via `action_count / max_actions`, separate from `observation_count`; stop early after 2 crashes or 4 stuck attempts).
     - `qa_observe` and `qa_act` default to `include_image=false` and always print the absolute `screenshot_path` — inspect screenshots with `read_file(screenshot_path)` instead of passing `include_image: true`.
     - If an observation reports `ax_unavailable_reason` (e.g. transient AX timeout or system modal), do not assume the app froze — inspect `screenshot_path` via `read_file` and tap by display-point `(x, y)` coordinates if needed.
  3. **Human Takeover Rule**: If `qa_observe` or `qa_act` returns `paused_by_human: true`, stop acting immediately so you never fight the human operator. Wait briefly and poll `qa_observe` until `paused_by_human` becomes `false`; honor any `human_note` returned on resume.
  4. `qa_record_findings(run_id, persona_id, report_md=..., findings_json=...)`.
  5. **Always** call `qa_finish_persona(run_id, persona_id, status=..., delete_simulator=true)` to clean up ephemeral simulators.

### Step 4: Semantic Deduplication & Summary

If you ran personas outside `workflows/run_qa_matrix.py` (or want to re-group):
1. Call `qa_summarize_run(run_id=<run_id>)` (without `groups_json`) **once**. The response includes `run_dir`, `summary_path`, and the flat `grouping_items` list (`id. [persona, severity, screen] title`) directly in both text and `structuredContent` — never scan or guess run paths on disk.
2. If `grouping_items` is empty (0 reports), skip step 3 (no second `qa_summarize_run` call needed). Otherwise, group semantically identical underlying bugs from `grouping_items` across personas and classify each group into **exactly one** of the 11 issue classes:
   - `layout and text size`
   - `feedback and errors`
   - `accessibility`
   - `translation`
   - `right to left`
   - `tablet`
   - `system bars`
   - `copy`
   - `visual polish`
   - `crash`
   - `likely test artefact`
3. Call `qa_summarize_run(run_id=<run_id>, groups_json={"groups": [{"title": "...", "class": "...", "ids": [1, 3]}]})` to write `SUMMARY.md` and `summary.json`.
4. Call `open_qa_dashboard(run_id=<run_id>)` so the user can triage the updated Summary view interactively.

### Step 5: Comparing Two Runs (Before vs. After Fix)

When the user wants to verify a fix or compare two builds:
1. Call `qa_compare_runs(run_a="<baseline_run_id>", run_b="<candidate_run_id>", name_a="Before", name_b="After")`.
2. Report overview KPI deltas (avg score, severity counts, goal reached), per-persona deltas, issues resolved (only in A) vs. newly introduced (only in B), and link `COMPARE.md`.

## Local Host Safety & Guardrails

- **Never mutate host networking**: `ios-agent-testers` does not touch `/etc/hosts` or `pfctl`; handle analytics/staging isolation via app `build_args`.
- **Ephemeral simulator hygiene**: Every persona runs on a freshly created `qa-<short_run>-<persona>` simulator configured with US QWERTY hardware/software keyboard defaults and cleanly deleted on `qa_finish_persona` unless `keep_simulator: true`.
- **Standalone CLI parity**: The exact same Python core is also available via `bin/ios-agent-testers` (`doctor`, `personas`, `build`, `cache`, `run`, `sim`, `summarize`, `compare`, `install-plugin`).
