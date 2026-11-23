"""Standalone CLI for `ios-agent-testers` wrapping the shared Python core."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from ios_agent_testers.core.bug_report import fetch_or_format_bug_report
from ios_agent_testers.core.compare import compare_runs
from ios_agent_testers.core.run_store import DEFAULT_MAX_ACTIONS, VALID_MODES, RunStore
from ios_agent_testers.core.summarize import summarize_run

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _format_exc(exc: BaseException) -> str:
    msg = str(exc).strip()
    if msg:
        return msg
    if isinstance(exc, TimeoutError):
        return "TimeoutError: operation timed out"
    return type(exc).__name__


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _resolve_run_dir(target: str, runs_dir: str | None) -> Path:
    candidate = Path(target).expanduser()
    if candidate.is_dir():
        return candidate.resolve()
    store = RunStore(Path(runs_dir) if runs_dir else None)
    rdir = store.run_dir(target)
    if not rdir.is_dir():
        raise FileNotFoundError(f"Run directory not found for {target!r} (checked {rdir})")
    return rdir


def _cmd_doctor(args: argparse.Namespace) -> int:
    from ios_agent_testers.core.sim_provisioner import doctor_check

    result = doctor_check()
    if args.json:
        _print_json(result)
    else:
        status = "OK" if result.get("ok") else "FAILED"
        print(f"ios-agent-testers doctor: {status}")
        for name, detail in (result.get("checks") or {}).items():
            print(f"  - {name}: {detail}")
        runtimes = result.get("runtimes") or []
        if runtimes:
            print(f"  - iOS runtimes ({len(runtimes)}):")
            for rt in runtimes:
                if isinstance(rt, dict):
                    print(
                        f"      * {rt.get('name') or rt.get('version')} "
                        f"({rt.get('identifier', '')}, "
                        f"{rt.get('supported_device_count', '?')} devices)"
                    )
                else:
                    print(f"      * {rt}")
        for warn in result.get("warnings") or []:
            print(f"  ! WARNING: {warn}")
    return 0 if result.get("ok") else 1


def _cmd_personas(args: argparse.Namespace) -> int:
    from ios_agent_testers.core.personas import select_personas

    selected = select_personas(
        selector=args.selector,
        custom_path=args.custom_path,
    )
    if args.json:
        _print_json(selected)
    else:
        for p in selected:
            d = p.get("device") or {}
            print(
                f"{p['id']:<8} | {p.get('locale', ''):<6} | "
                f"{d.get('simulator', ''):<26} | {d.get('text_size', ''):<36} | "
                f"{d.get('theme', '')}"
            )
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    from ios_agent_testers.core.builder import build_ios_simulator_app

    result = asyncio.run(
        build_ios_simulator_app(
            args.project_path,
            scheme=args.scheme,
            configuration=args.configuration,
            build_args=args.build_args,
            derived_data_dir=args.derived_data,
            clean=bool(args.clean),
        )
    )
    if args.json:
        _print_json(result)
    else:
        print(f"Framework: {result['framework']}")
        print(f"Bundle ID: {result['bundle_id']}")
        print(f"App Path:  {result['app_path']}")
        if result.get("scheme"):
            print(f"Scheme:    {result['scheme']}")
        if result.get("derived_data_dir"):
            print(f"DerivedData: {result['derived_data_dir']}")
    return 0


def _cmd_cache_clean(args: argparse.Namespace) -> int:
    from ios_agent_testers.core.builder import prune_derived_data_cache

    keep = int(args.keep)
    if keep < 0:
        raise ValueError("--keep must be a non-negative integer")
    result = prune_derived_data_cache(keep=keep)
    if args.json:
        _print_json(result)
    else:
        removed = result.get("removed") or []
        kept = result.get("kept") or []
        freed_bytes = int(result.get("freed_bytes") or 0)
        freed_mb = freed_bytes / (1024 * 1024)
        print(
            f"DerivedData cache cleaned: removed {len(removed)}, "
            f"kept {len(kept)}, freed {freed_mb:.1f} MB ({freed_bytes} bytes)."
        )
        for path in removed:
            print(f"  - removed: {path}")
    return 0


def _cmd_run_create(args: argparse.Namespace) -> int:
    from ios_agent_testers.core.personas import select_personas
    from ios_agent_testers.core.sim_provisioner import extract_bundle_id

    selected = select_personas(
        selector=args.personas,
        custom_path=args.custom_personas,
    )
    bundle_id = args.bundle_id
    if not bundle_id and args.app_path:
        bundle_id = extract_bundle_id(args.app_path)

    bug_report_md: str | None = None
    if args.bug_report:
        bug_report_md = fetch_or_format_bug_report(args.bug_report)

    store = RunStore(Path(args.runs_dir) if args.runs_dir else None)
    manifest = store.create_run(
        app_path=args.app_path,
        bundle_id=bundle_id,
        personas=selected,
        mode=args.mode,
        brief_path=args.brief_path,
        bug_report_md=bug_report_md,
        custom_goal=args.custom_goal,
        record_video=args.record_video,
        max_concurrency=args.max_concurrency,
        max_actions=args.max_actions,
        run_id=args.run_id,
    )
    if args.json:
        _print_json(manifest)
    else:
        pids = ", ".join(p["id"] for p in manifest["personas"])
        print(f"Created run: {manifest['run_id']}")
        print(f"  mode:        {manifest['mode']}")
        print(f"  app_id:      {manifest.get('app_id') or '(none)'}")
        print(f"  max_actions: {manifest.get('max_actions', DEFAULT_MAX_ACTIONS)}")
        print(f"  personas:    {pids}")
        print(f"  dir:         {store.run_dir(manifest['run_id'])}")
    return 0


def _cmd_run_list(args: argparse.Namespace) -> int:
    store = RunStore(Path(args.runs_dir) if args.runs_dir else None)
    runs = store.list_runs()
    if args.json:
        _print_json(runs)
    else:
        if not runs:
            print("No QA runs found.")
            return 0
        for r in runs:
            score = f"{r['avg_score']:.1f}/10" if r["avg_score"] is not None else "-"
            print(
                f"{r['run_id']} | {r['mode']:<9} | "
                f"done {r['done_count']}/{r['persona_count']} "
                f"(active {r['active_count']}) | issues {r['issue_count']} | "
                f"score {score} | {r.get('app_id') or '-'}"
            )
    return 0


def _cmd_run_status(args: argparse.Namespace) -> int:
    store = RunStore(Path(args.runs_dir) if args.runs_dir else None)
    manifest = store.get_manifest(args.run_id)
    state = store.get_state(args.run_id)
    payload = {"manifest": manifest, "state": state}
    if args.json:
        _print_json(payload)
    else:
        print(f"Run {manifest['run_id']} ({manifest['mode']}) — app: {manifest.get('app_id') or '-'}")
        order = state.get("persona_order") or list((state.get("personas") or {}).keys())
        personas = state.get("personas") or {}
        for pid in order:
            p = personas.get(pid) or {}
            score = f"{p['score']}/10" if p.get("score") is not None else "-"
            paused = " [PAUSED]" if p.get("paused_by_human") else ""
            action_count = (
                p["action_count"]
                if p.get("action_count") is not None
                else p.get("step_count", 0)
            )
            max_actions = (
                p.get("max_actions")
                or manifest.get("max_actions")
                or p.get("max_steps")
                or DEFAULT_MAX_ACTIONS
            )
            obs_count = (
                p["observation_count"]
                if p.get("observation_count") is not None
                else p.get("step_count", 0)
            )
            print(
                f"  - {pid:<8} | {p.get('status', '?'):<12}{paused} | "
                f"actions {action_count}/{max_actions} (obs {obs_count}) | "
                f"score {score} | udid={p.get('udid') or '-'}"
            )
    return 0


def _cmd_run_cleanup(args: argparse.Namespace) -> int:
    from ios_agent_testers.core.sim_provisioner import teardown_persona_sim

    store = RunStore(Path(args.runs_dir) if args.runs_dir else None)
    state = store.get_state(args.run_id)
    personas = state.get("personas") or {}

    async def _cleanup_all() -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for pid, p in personas.items():
            udid = p.get("udid")
            if not udid:
                continue
            res = await teardown_persona_sim(
                udid,
                ephemeral=bool(p.get("ephemeral", True)),
                delete_simulator=not args.keep_simulators,
                video_pid=p.get("video_pid"),
            )
            store.update_persona_state(
                args.run_id,
                pid,
                udid=None,
                video_pid=None,
            )
            cleaned.append({"persona_id": pid, "udid": udid, "result": res})
        return cleaned

    cleaned = asyncio.run(_cleanup_all())
    if args.json:
        _print_json({"run_id": args.run_id, "cleaned": cleaned})
    else:
        if not cleaned:
            print(f"Run {args.run_id}: no active simulators to clean up.")
        else:
            for item in cleaned:
                print(f"Cleaned {item['persona_id']} (udid={item['udid']})")
    return 0


def _cmd_sim_provision(args: argparse.Namespace) -> int:
    from ios_agent_testers.core.sim_provisioner import provision_persona_sim

    store = RunStore(Path(args.runs_dir) if args.runs_dir else None)
    manifest = store.get_manifest(args.run_id)
    persona_cfg = next(
        (p for p in (manifest.get("personas") or []) if p.get("id") == args.persona_id),
        None,
    )
    if persona_cfg is None:
        raise KeyError(f"Persona {args.persona_id!r} not found in run {args.run_id!r}")

    store.update_persona_state(args.run_id, args.persona_id, status="provisioning")
    video_path = (
        store.persona_dir(args.run_id, args.persona_id) / "video" / "rec_01.mp4"
        if manifest.get("record_video")
        else None
    )

    try:
        res = asyncio.run(
            provision_persona_sim(
                args.run_id,
                persona_cfg,
                app_path=manifest.get("app_path"),
                bundle_id=manifest.get("app_id") or manifest.get("bundle_id"),
                record_video=bool(manifest.get("record_video", False)),
                video_path=video_path,
                keep_simulator=bool(args.keep_simulator),
            )
        )
    except Exception as exc:
        store.update_persona_state(
            args.run_id,
            args.persona_id,
            status="error",
            error=_format_exc(exc),
        )
        raise

    store.update_persona_state(
        args.run_id,
        args.persona_id,
        status="running",
        udid=res.get("udid"),
        device_label=res.get("device_label"),
        ephemeral=bool(res.get("ephemeral", True)),
        video_pid=res.get("video_pid"),
        video_path=res.get("video_path"),
        run_start_ts=time.time(),
    )
    if args.json:
        _print_json(res)
    else:
        print(
            f"Provisioned {args.persona_id}: udid={res['udid']} "
            f"device={res['device_label']}"
        )
    return 0


def _cmd_sim_observe(args: argparse.Namespace) -> int:
    from ios_agent_testers.core.idb_driver import IDBDriver

    store = RunStore(Path(args.runs_dir) if args.runs_dir else None)
    manifest = store.get_manifest(args.run_id)
    state = store.get_state(args.run_id)
    pstate = (state.get("personas") or {}).get(args.persona_id)
    if not pstate:
        raise KeyError(f"Persona {args.persona_id!r} not found in run {args.run_id!r}")
    udid = pstate.get("udid")
    if not udid:
        raise RuntimeError(
            f"Persona {args.persona_id!r} has no provisioned simulator UDID; run `sim provision` first."
        )

    step = store.next_step_number(args.run_id, args.persona_id)
    pdir = store.persona_dir(args.run_id, args.persona_id)
    out_png = pdir / f"{step:02d}.png"
    crashes_dir = pdir / "crashes"
    crashes_dir.mkdir(parents=True, exist_ok=True)

    async def _run_observe() -> dict[str, Any]:
        driver = IDBDriver()
        try:
            await driver.connect(udid, ephemeral=bool(pstate.get("ephemeral", True)))
            return await driver.observe(
                udid,
                bundle_id=manifest.get("app_id") or manifest.get("bundle_id"),
                output_png=out_png,
                run_start_ts=float(pstate.get("run_start_ts") or time.time()),
                crashes_dir=crashes_dir,
                max_elements=args.max_elements,
            )
        finally:
            await driver.close_all()

    obs = asyncio.run(_run_observe())
    store.update_persona_state(
        args.run_id,
        args.persona_id,
        width_points=obs.get("width_points"),
        height_points=obs.get("height_points"),
    )
    store.record_step(
        args.run_id,
        args.persona_id,
        step,
        screenshot_file=out_png.name,
        action=None,
        a11y=obs.get("a11y"),
        app_running=obs.get("app_running"),
        crashes=obs.get("crashes") or [],
        recent_logs=obs.get("recent_logs") or [],
        elements=obs.get("elements") or [],
        ax_unavailable_reason=obs.get("ax_unavailable_reason"),
    )

    if args.json:
        _print_json({"step": step, **obs})
    else:
        print(
            f"Step {step:02d}: {obs['screenshot_path']} "
            f"({obs['width_points']}x{obs['height_points']} pt)"
        )
        if obs.get("ax_unavailable_reason"):
            print(f"WARNING: Accessibility tree unavailable ({obs['ax_unavailable_reason']}).")
        if obs.get("elements_text"):
            print(obs["elements_text"])
        if obs.get("a11y_line"):
            print(obs["a11y_line"])
        if obs.get("app_running") is False:
            print("WARNING: the app is no longer running (crash or closed).")
    return 0


def _cmd_sim_teardown(args: argparse.Namespace) -> int:
    from ios_agent_testers.core.sim_provisioner import teardown_persona_sim

    store = RunStore(Path(args.runs_dir) if args.runs_dir else None)
    state = store.get_state(args.run_id)
    pstate = (state.get("personas") or {}).get(args.persona_id)
    if not pstate:
        raise KeyError(f"Persona {args.persona_id!r} not found in run {args.run_id!r}")
    udid = pstate.get("udid")
    if not udid:
        res = {"ok": True, "detail": "No active simulator to tear down."}
    else:
        res = asyncio.run(
            teardown_persona_sim(
                udid,
                ephemeral=bool(pstate.get("ephemeral", True)),
                delete_simulator=not args.keep_simulator,
                video_pid=pstate.get("video_pid"),
            )
        )
        store.update_persona_state(
            args.run_id,
            args.persona_id,
            udid=None,
            video_pid=None,
        )
    if args.json:
        _print_json(res)
    else:
        print(f"Torn down {args.persona_id}: {res}")
    return 0


def _cmd_summarize(args: argparse.Namespace) -> int:
    run_dir = _resolve_run_dir(args.run, args.runs_dir)
    groups_payload: Any = None
    if args.groups:
        groups_payload = json.loads(Path(args.groups).expanduser().read_text(encoding="utf-8"))
    use_external = bool(args.use_external_llm) and not bool(args.no_grouping)
    md, summary = summarize_run(
        run_dir,
        groups_json=groups_payload,
        use_external_llm=use_external,
    )
    if args.json:
        _print_json(summary)
    else:
        print(md, end="")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    dir_a = _resolve_run_dir(args.run_a, args.runs_dir)
    dir_b = _resolve_run_dir(args.run_b, args.runs_dir)
    md, comp = compare_runs(
        dir_a,
        dir_b,
        name_a=args.name_a or dir_a.name,
        name_b=args.name_b or dir_b.name,
    )
    if args.json:
        _print_json(comp)
    else:
        print(md, end="")
    return 0


def _cmd_install_plugin(args: argparse.Namespace) -> int:
    plugins_dir = Path(args.plugins_dir).expanduser()
    target = plugins_dir / "ios-agent-testers"
    source = PROJECT_ROOT.resolve()

    if args.uninstall:
        if not target.exists() and not target.is_symlink():
            payload = {"ok": True, "action": "noop", "target": str(target), "detail": "Not installed."}
        elif target.is_symlink() and target.resolve() == source:
            target.unlink()
            payload = {"ok": True, "action": "uninstalled", "target": str(target)}
        else:
            raise RuntimeError(
                f"Refusing to remove {target}: not a symlink pointing to {source}"
            )
    else:
        plugins_dir.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            if target.resolve() == source:
                payload = {
                    "ok": True,
                    "action": "already_installed",
                    "target": str(target),
                    "source": str(source),
                }
            else:
                raise RuntimeError(
                    f"Target symlink {target} already points to {target.resolve()}; remove it manually first."
                )
        elif target.exists():
            raise RuntimeError(
                f"Target path {target} already exists and is not a symlink; refusing to overwrite."
            )
        else:
            target.symlink_to(source)
            payload = {
                "ok": True,
                "action": "installed",
                "target": str(target),
                "source": str(source),
            }

    if args.json:
        _print_json(payload)
    else:
        print(f"Plugin {payload['action']}: {payload['target']} -> {source}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ios-agent-testers",
        description="Multi-persona iOS Simulator QA engine and report toolkit.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # doctor
    p_doc = sub.add_parser("doctor", help="Check local simctl, runtimes, and idb environment.")
    p_doc.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_doc.set_defaults(func=_cmd_doctor)

    # personas
    p_per = sub.add_parser("personas", help="List or select QA user personas.")
    p_per.add_argument(
        "--selector",
        default="all",
        help="Preset (all, smoke, iphone, ipad, a11y) or comma-separated IDs.",
    )
    p_per.add_argument("--custom-path", default=None, help="Path to custom personas JSON.")
    p_per.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_per.set_defaults(func=_cmd_personas)

    # build
    p_bld = sub.add_parser("build", help="Detect framework and build unsigned arm64 iOS Simulator .app.")
    p_bld.add_argument("project_path", help="Path to the iOS/cross-platform project.")
    p_bld.add_argument("--scheme", default=None, help="Explicit Xcode scheme.")
    p_bld.add_argument("--configuration", default="Debug", help="Build configuration (default: Debug).")
    p_bld.add_argument("--build-args", default=None, help="Extra build arguments string.")
    p_bld.add_argument("--derived-data", default=None, help="Optional DerivedData path.")
    p_bld.add_argument("--clean", action="store_true", help="Remove cached DerivedData before building.")
    p_bld.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_bld.set_defaults(func=_cmd_build)

    # cache
    p_cache = sub.add_parser("cache", help="Manage Xcode DerivedData build cache.")
    cache_sub = p_cache.add_subparsers(dest="cache_command", required=True)

    p_cc = cache_sub.add_parser(
        "clean",
        help="Prune managed DerivedData cache directories (keeps N most recent).",
    )
    p_cc.add_argument(
        "--keep",
        type=int,
        default=0,
        help="Number of most recent cache directories to keep (default: 0, remove all).",
    )
    p_cc.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_cc.set_defaults(func=_cmd_cache_clean)

    # run
    p_run = sub.add_parser("run", help="Manage QA runs.")
    run_sub = p_run.add_subparsers(dest="run_command", required=True)

    p_rc = run_sub.add_parser("create", help="Create a new QA run.")
    p_rc.add_argument("--app-path", default=None, help="Path to .app bundle.")
    p_rc.add_argument("--bundle-id", default=None, help="App CFBundleIdentifier.")
    p_rc.add_argument("--personas", default="smoke", help="Persona selector (default: smoke).")
    p_rc.add_argument("--custom-personas", default=None, help="Path to custom personas JSON.")
    p_rc.add_argument("--mode", default="explore", choices=VALID_MODES, help="QA test mode.")
    p_rc.add_argument("--brief-path", default=None, help="Optional custom brief markdown path.")
    p_rc.add_argument("--bug-report", default=None, help="GitHub issue URL, owner/repo#N, or raw bug text.")
    p_rc.add_argument("--custom-goal", default=None, help="Targeted goal overriding persona goal.")
    p_rc.add_argument("--record-video", action="store_true", help="Record H.264 video per persona.")
    p_rc.add_argument("--max-concurrency", type=int, default=2, help="Max concurrent simulators (default: 2).")
    p_rc.add_argument(
        "--max-actions",
        type=int,
        default=DEFAULT_MAX_ACTIONS,
        help=f"Per-persona action budget, 1..100 (default: {DEFAULT_MAX_ACTIONS}).",
    )
    p_rc.add_argument("--run-id", default=None, help="Optional explicit run ID.")
    p_rc.add_argument("--runs-dir", default=None, help="Override runs directory.")
    p_rc.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_rc.set_defaults(func=_cmd_run_create)

    p_rl = run_sub.add_parser("list", help="List existing QA runs.")
    p_rl.add_argument("--runs-dir", default=None, help="Override runs directory.")
    p_rl.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_rl.set_defaults(func=_cmd_run_list)

    p_rs = run_sub.add_parser("status", help="Show status of a QA run.")
    p_rs.add_argument("run_id", help="Run ID.")
    p_rs.add_argument("--runs-dir", default=None, help="Override runs directory.")
    p_rs.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_rs.set_defaults(func=_cmd_run_status)

    p_rx = run_sub.add_parser("cleanup", help="Tear down any active ephemeral simulators for a run.")
    p_rx.add_argument("run_id", help="Run ID.")
    p_rx.add_argument("--keep-simulators", action="store_true", help="Shut down without deleting simulators.")
    p_rx.add_argument("--runs-dir", default=None, help="Override runs directory.")
    p_rx.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_rx.set_defaults(func=_cmd_run_cleanup)

    # sim
    p_sim = sub.add_parser("sim", help="Provision, observe, or tear down a persona simulator.")
    sim_sub = p_sim.add_subparsers(dest="sim_command", required=True)

    p_sp = sim_sub.add_parser("provision", help="Provision ephemeral simulator for a persona.")
    p_sp.add_argument("run_id", help="Run ID.")
    p_sp.add_argument("persona_id", help="Persona ID.")
    p_sp.add_argument("--keep-simulator", action="store_true", help="Keep simulator on teardown.")
    p_sp.add_argument("--runs-dir", default=None, help="Override runs directory.")
    p_sp.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_sp.set_defaults(func=_cmd_sim_provision)

    p_so = sim_sub.add_parser("observe", help="Capture screenshot, accessibility tree, a11y audit, and crash check.")
    p_so.add_argument("run_id", help="Run ID.")
    p_so.add_argument("persona_id", help="Persona ID.")
    p_so.add_argument("--max-elements", type=int, default=120, help="Maximum UI elements to return.")
    p_so.add_argument("--runs-dir", default=None, help="Override runs directory.")
    p_so.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_so.set_defaults(func=_cmd_sim_observe)

    p_st = sim_sub.add_parser("teardown", help="Shut down and remove a persona simulator.")
    p_st.add_argument("run_id", help="Run ID.")
    p_st.add_argument("persona_id", help="Persona ID.")
    p_st.add_argument("--keep-simulator", action="store_true", help="Shut down without deleting simulator.")
    p_st.add_argument("--runs-dir", default=None, help="Override runs directory.")
    p_st.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_st.set_defaults(func=_cmd_sim_teardown)

    # summarize
    p_sum = sub.add_parser("summarize", help="Generate SUMMARY.md and summary.json for a run.")
    p_sum.add_argument("run", help="Run ID or path to run directory.")
    p_sum.add_argument("--groups", default=None, help="Path to semantic groups JSON file.")
    p_sum.add_argument("--no-grouping", action="store_true", help="Disable external LLM grouping.")
    p_sum.add_argument(
        "--use-external-llm",
        action="store_true",
        help="Invoke SUMMARY_LLM_CMD or claude CLI for semantic grouping if --groups is not supplied.",
    )
    p_sum.add_argument("--runs-dir", default=None, help="Override runs directory.")
    p_sum.add_argument("--json", action="store_true", help="Emit summary.json to stdout instead of Markdown.")
    p_sum.set_defaults(func=_cmd_summarize)

    # compare
    p_cmp = sub.add_parser("compare", help="Generate COMPARE.md and compare.json comparing two runs.")
    p_cmp.add_argument("run_a", help="Run A ID or directory path.")
    p_cmp.add_argument("run_b", help="Run B ID or directory path.")
    p_cmp.add_argument("--name-a", default="A", help="Display label for Run A.")
    p_cmp.add_argument("--name-b", default="B", help="Display label for Run B.")
    p_cmp.add_argument("--runs-dir", default=None, help="Override runs directory.")
    p_cmp.add_argument("--json", action="store_true", help="Emit compare.json to stdout instead of Markdown.")
    p_cmp.set_defaults(func=_cmd_compare)

    # install-plugin
    p_ins = sub.add_parser(
        "install-plugin",
        help="Symlink (or remove symlink for) this plugin in ~/.gemini/config/plugins/ios-agent-testers.",
    )
    p_ins.add_argument("--uninstall", action="store_true", help="Remove the plugin symlink.")
    p_ins.add_argument(
        "--plugins-dir",
        default=str(Path.home() / ".gemini" / "config" / "plugins"),
        help="Antigravity plugins directory.",
    )
    p_ins.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_ins.set_defaults(func=_cmd_install_plugin)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"Error: {_format_exc(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
