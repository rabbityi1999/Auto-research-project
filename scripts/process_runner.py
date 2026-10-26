"""Process handling for bounded research supervision. Standard library only."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from workflow import write_json as atomic_json

STATE_DIR = '.research-supervisor'

TRANSIENT = re.compile(
    r"stream disconnected|transport error|network error|error decoding response bod|"
    r"connection (?:reset|closed|refused)|timed? out|timeout|temporary failure|"
    r"websocket.*(?:closed|reset)|\b(?:502|503|504)\b", re.I)
PERMANENT = re.compile(
    r"unauthorized|forbidden|invalid.api.key|authentication|not logged in|"
    r"quota|usage limit|rate limit|insufficient.credits|permission denied|"
    r"approval.*(?:required|denied)|\b(?:401|403|429)\b|"
    r"invalid.*config|unexpected argument|model.*not.*found", re.I)


@contextmanager
def exclusive_lock(path):
    """OS lock: no stale PID heuristics or automatic removal of a live lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(path, "a+b")
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another supervisor owns this workspace")
        yield
    finally:
        stream.close()


def stop_tree(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                capture_output=True, timeout=20)
        if result.returncode and proc.poll() is None:
            # Terminate our owned handle even when process-tree enumeration is
            # denied by a host sandbox; do not retry because children are uncertain.
            proc.terminate()
            proc.wait(timeout=15)
            raise RuntimeError("Could not stop child process tree; refusing to restart")
    else:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=15)


def cli_prefix(executable):
    executable = shutil.which(executable) or str(Path(executable).resolve())
    if not Path(executable).is_file():
        raise ValueError("Codex CLI executable not found: " + executable)
    # npm on Windows installs a .cmd shim. Execute its adjacent JS entry point
    # directly; never pass user-controlled prompts through cmd.exe / shell=True.
    if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat", ".ps1"):
        js = Path(executable).parent / "node_modules/@openai/codex/bin/codex.js"
        node = shutil.which("node")
        if not node or not js.is_file():
            raise ValueError("Use --codex with the native codex.exe path")
        return [node, str(js)]
    return [executable]


def command(prefix, session_id=None):
    cmd = prefix + ["exec", "--sandbox", "workspace-write",
                    "-c", 'approval_policy="never"', "--json", "--skip-git-repo-check"]
    if session_id:
        cmd += ["resume", session_id]
    return cmd + ["-"]


def preflight(prefix):
    for suffix, required in ((["exec", "--help"], ["--json", "--sandbox"]),
                             (["exec", "resume", "--help"], ["SESSION_ID"])):
        result = subprocess.run(prefix + suffix, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=20)
        if result.returncode or any(flag not in result.stdout for flag in required):
            raise ValueError("Installed Codex CLI lacks required exec/resume features")


def run_attempt(cmd, prompt, root, log_path, timeout, on_session):
    """Capture output incrementally; classify errors, not quoted agent content."""
    env = dict(os.environ, RESEARCH_WORKFLOW_SUPERVISED="1")
    kwargs = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    proc = subprocess.Popen(cmd, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, **kwargs)
    process_record = {"pid": proc.pid, "started_at": time.time(), "status": "running"}
    atomic_json(root / STATE_DIR / "process.json", process_record)
    events = queue.Queue()

    def pump(stream, source):
        try:
            for line in iter(stream.readline, b""):
                events.put((source, line.decode("utf-8", errors="replace")))
        finally:
            stream.close()
            events.put((source, None))

    readers = []
    for stream, source in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        reader = threading.Thread(target=pump, args=(stream, source), daemon=True)
        reader.start()
        readers.append(reader)
    errors, ended, completed = [], set(), False
    begin = time.monotonic()
    reason = None
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        with open(log_path, "w", encoding="utf-8") as log:
            while len(ended) < 2 or proc.poll() is None:
                if (root / STATE_DIR / "STOP").exists():
                    reason = "stopped"
                    stop_tree(proc)
                    break
                if time.monotonic() - begin > timeout:
                    reason = "timeout"
                    stop_tree(proc)
                    break
                try:
                    source, line = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    ended.add(source)
                    continue
                log.write(json.dumps({"source": source, "line": line}, ensure_ascii=False) + "\n")
                log.flush()
                if source == "stderr":
                    errors.append(line[-4000:])
                else:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        # A broken JSON stream is not a successful run.
                        errors.append("Malformed JSON event: " + line[-1000:])
                        continue
                    if not isinstance(item, dict):
                        errors.append("Malformed JSON event")
                        continue
                    kind = item.get("type")
                    if kind == "thread.started" and item.get("thread_id"):
                        on_session(str(item["thread_id"]))
                    elif kind in ("error", "turn.failed"):
                        errors.append(json.dumps(item, ensure_ascii=False))
                    elif kind == "turn.completed":
                        completed = True
                errors = errors[-50:]
        code = proc.wait(timeout=15)
    except BaseException:
        stop_tree(proc)
        raise
    finally:
        if not proc.stdin.closed:
            proc.stdin.close()
        for reader in readers:
            reader.join(timeout=1)
        process_record.update(status="exited" if proc.poll() is not None else "uncertain",
                              returncode=proc.poll(), ended_at=time.time())
        atomic_json(root / STATE_DIR / "process.json", process_record)
    return {"returncode": code, "reason": reason, "turn_completed": completed,
            "error_text": "\n".join(errors)[-12000:]}


def retry_kind(result):
    if result["reason"] == "stopped":
        return "stopped"
    if PERMANENT.search(result["error_text"]):
        return "needs_input"
    if result["reason"] == "timeout":
        # Timeout can be a healthy long render. Preserve and inspect, don't kill/retry forever.
        return "needs_inspection"
    if "Malformed JSON event" in result["error_text"]:
        return "needs_inspection"
    if result["returncode"] == 0 and result["turn_completed"]:
        return "continue"
    if TRANSIENT.search(result["error_text"]):
        return "retry"
    return "needs_inspection"


