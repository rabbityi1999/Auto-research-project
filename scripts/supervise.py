"""Bounded local Codex supervision for a single research run. Opt-in, never recursive."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from workflow import load, resume, write_json, digest
from process_runner import (STATE_DIR, exclusive_lock, cli_prefix, command, preflight,
                            run_attempt, retry_kind)


def gate(root):
    report = resume(root)
    if report["control"]["status"] == "needs_input":
        return "needs_input"
    if report["phase"] == "awaiting_selection" or (
            report["phase"] in ("reading", "ready_for_slides", "complete") and report["selection_required"]):
        return "awaiting_selection"
    if report["phase"] == "complete":
        return "complete" if not report["issues"] else "needs_inspection"
    return "active"


def progress_fingerprint(root):
    """Ignore heartbeat timestamps: actual notes and stage transitions count as progress."""
    state = load(root)
    values = {"phase": state["phase"], "selection": state["selection"], "readings": state["readings"]}
    paths = [root / name for name in ("papers.json", "briefing.md", "search-log.md", "synthesis.md")]
    for folder in ("notes", "slides"):
        paths.extend(p for p in (root / folder).rglob("*") if p.is_file())
    values["files"] = {p.relative_to(root).as_posix(): digest(p) for p in paths if p.is_file()}
    return json.dumps(values, ensure_ascii=False, sort_keys=True)


def make_prompt(root):
    repo = Path(__file__).resolve().parents[1]
    return (
        f"Use the research workflow at {repo / '.agents/skills/research-workflow/SKILL.md'}.\n"
        f"Run directory: {root}\nUse the CLI at {repo / 'scripts/workflow.py'}.\n"
        "Read brief.md and run resume first. This is a bounded supervised turn: complete at most ONE "
        "useful unit (one query batch, one paper or paper section, synthesis, or a small slide batch), "
        "save work to files and checkpoint it, then end the turn. The supervisor will arrange continuation. "
        "Check saved_notes_to_review BEFORE re-reading papers; inspect and register completed notes, "
        "preserve partial drafts. Register each completed paper immediately using read. "
        "Read only the current unit's sources. Do not print whole logs or whole source collections. "
        "Keep all research output in the run directory. Parent AGENTS.md still applies. "
        "Do not launch experiments, access remote compute, activate schedules, publish, push, or send messages. "
        "Do not run other Codex processes, supervisors, or subagents. RESEARCH_WORKFLOW_SUPERVISED=1. "
        "Never manufacture selection: stop at awaiting_selection. If input, tools, access or original sources "
        "are missing, save a precise block using workflow.py block --reason and end. "
        "Check tool processes and outputs before retrying an operation whose response was lost. "
        "For slides, save authoring source and actual previews; complete only after real content and visual QA. "
        "A file hash is not a scientific or visual review. Do not mark drafts as complete.\n"
    )


def supervise(args, prefix=None, attempt_runner=run_attempt):
    if os.environ.get("RESEARCH_WORKFLOW_SUPERVISED") == "1" and not args.stop:
        raise RuntimeError("Recursive supervision is disabled")
    root = args.run.resolve()
    load(root)  # Existing run only; never invent a new research task.
    control = root / STATE_DIR
    control.mkdir(exist_ok=True)
    if args.stop:
        (control / "STOP").write_text("Stop requested\n", encoding="utf-8")
        return 130
    with exclusive_lock(control / "supervisor.lock"):
        if (control / "STOP").exists():
            return 130
        path = control / "supervisor.json"
        runtime = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "version": 1, "run": str(root), "attempts": 0, "session_id": None,
            "status": "ready", "no_progress": 0}
        if runtime["run"] != str(root):
            raise ValueError("Supervisor is bound to a different run")
        if runtime["status"] == "running" and not args.recover_interrupted:
            raise RuntimeError("Previous invocation may still be running. Inspect process.json and tool jobs, "
                               "then use --recover-interrupted only after old writers have stopped.")

        def persist(status):
            runtime.update(status=status, updated_at=time.time())
            write_json(path, runtime)

        def on_session(session_id):
            if runtime["session_id"] and runtime["session_id"] != session_id:
                raise RuntimeError("Unexpected session ID")
            runtime["session_id"] = session_id
            persist("running")

        while True:
            state_gate = gate(root)
            if state_gate != "active":
                persist(state_gate)
                print(state_gate, flush=True)
                return 0 if state_gate == "complete" else 2
            if runtime["attempts"] >= args.max_attempts:
                break
            if (control / "STOP").exists():
                persist("stopped")
                return 130
            if prefix is None:
                prefix = cli_prefix(args.codex)
                preflight(prefix)
            before = progress_fingerprint(root)
            runtime["attempts"] += 1
            number = runtime["attempts"]
            persist("running")
            print(f"Unit {number}/{args.max_attempts}; session={runtime['session_id'] or 'new'}", flush=True)
            try:
                result = attempt_runner(command(prefix, runtime["session_id"]), make_prompt(root), root,
                                        control / f"attempt-{number:03d}.jsonl", args.attempt_timeout, on_session)
            except KeyboardInterrupt:
                persist("stopped")
                return 130
            except Exception as exc:
                runtime["last_error"] = str(exc)
                persist("needs_inspection")
                raise
            runtime["last_result"] = result
            kind = retry_kind(result)
            if kind == "stopped" or (control / "STOP").exists():
                persist("stopped")
                return 130
            state_gate = gate(root)
            if state_gate != "active":
                persist(state_gate)
                return 0 if state_gate == "complete" else 2
            if kind not in ("retry", "continue"):
                persist(kind)
                return 2
            after = progress_fingerprint(root)
            if before != after:
                runtime["no_progress"] = 0
            elif kind == "continue":
                runtime["no_progress"] += 1
            # Failed network turns use the total retry cap. Two successful but unproductive turns stop.
            if kind == "continue" and runtime["no_progress"] >= 2:
                persist("no_progress")
                return 2
            persist(kind)
            if runtime["attempts"] >= args.max_attempts:
                break
            delay = min(args.retry_delay * (2 ** min(number - 1, 6)), 60) if kind == "retry" else 0
            end = time.monotonic() + delay
            while time.monotonic() < end:
                if (control / "STOP").exists():
                    persist("stopped")
                    return 130
                time.sleep(min(.2, max(0, end - time.monotonic())))
        persist("attempt_limit")
        return 3


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--codex", default="codex")
    p.add_argument("--max-attempts", type=int, default=12, help="Cumulative turns, including prior launches")
    p.add_argument("--attempt-timeout", type=float, default=1200, help="Seconds; hard timeout stops for inspection")
    p.add_argument("--retry-delay", type=float, default=15)
    p.add_argument("--recover-interrupted", action="store_true")
    p.add_argument("--stop", action="store_true")
    args = p.parse_args()
    if args.max_attempts < 1 or args.attempt_timeout <= 0 or args.retry_delay < 0:
        p.error("Invalid attempt limit, timeout, or delay")
    try:
        return supervise(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
