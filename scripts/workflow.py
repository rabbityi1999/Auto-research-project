#!/usr/bin/env python3
"""Single-writer local research state tracker. No network or third-party packages."""
import argparse
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


class WorkflowError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise WorkflowError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local_file(run, value):
    root = Path(run).resolve()
    path = (root / value).resolve()
    require(root in path.parents, "Evidence files must stay inside this run directory.")
    require(path.is_file() and path.stat().st_size > 0, "Missing or empty evidence file: " + str(path))
    return path


def record(run, value):
    path = local_file(run, value)
    return {"path": path.relative_to(Path(run).resolve()).as_posix(), "sha256": digest(path)}


def verify(run, item):
    path = local_file(run, item["path"])
    require(digest(path) == item["sha256"], "Evidence changed; review and register again: " + item["path"])


def load(run):
    state = read_json(Path(run) / "state.json")
    require(state.get("schema_version") == 1, "Unsupported state schema.")
    return state


def save(run, state, event, details=None):
    state["updated_at"] = now()
    state["history"].append({"at": state["updated_at"], "event": event, "details": details})
    write_json(Path(run) / "state.json", state)
    return state


def init(run, topic):
    root = Path(run)
    require(topic.strip(), "Topic is required.")
    require(not root.exists() or not any(root.iterdir()), "Run directory must be new or empty.")
    root.mkdir(parents=True, exist_ok=True)
    for folder in ("notes", "slides", "sources"):
        (root / folder).mkdir()
    (root / "brief.md").write_text("# " + topic + "\n\n在此记录研究问题、范围、纳入标准和汇报需求。\n", encoding="utf-8")
    (root / "search-log.md").write_text("# 检索日志\n\n记录实际日期、平台、查询式及筛选依据。\n", encoding="utf-8")
    write_json(root / "papers.json", [])
    state = {"schema_version": 1, "topic": topic, "phase": "collecting", "created_at": now(),
             "papers_hash": digest(root / "papers.json"), "briefing": None, "selection": None,
             "readings": {}, "delivery": None, "history": []}
    return save(root, state, "init")


def http_url(value):
    parsed = urlparse(value)
    return parsed.scheme in ("https", "http") and bool(parsed.netloc)


def validate_papers(papers):
    require(isinstance(papers, list) and papers, "Papers must be a nonempty JSON array.")
    ids, titles, dois = set(), set(), set()
    for p in papers:
        require(isinstance(p, dict), "Each paper must be an object.")
        for key in ("id", "title", "url", "evidence_level"):
            require(isinstance(p.get(key), str) and p[key].strip(), "Missing string field: " + key)
        require(re.fullmatch(r"P[0-9]{3,}", p["id"]), "Invalid paper ID: " + p["id"])
        require(p["id"] not in ids, "Duplicate paper ID: " + p["id"])
        title = " ".join(p["title"].casefold().split())
        require(title not in titles, "Duplicate title: " + p["title"])
        doi = p.get("doi")
        require(doi is None or isinstance(doi, str), "DOI must be string or null.")
        if doi:
            doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi.strip(), flags=re.I).lower()
            require(doi not in dois, "Duplicate DOI: " + doi)
            dois.add(doi)
        require(http_url(p["url"]), "Paper URL must be HTTP(S).")
        require(p["evidence_level"] in ("metadata", "abstract", "full_text"), "Invalid evidence level.")
        ids.add(p["id"])
        titles.add(title)


def papers_for(run, state):
    path = Path(run) / "papers.json"
    require(digest(path) == state["papers_hash"], "Candidate library changed outside import; restore it or import before selection.")
    papers = read_json(path)
    validate_papers(papers)
    return papers


def import_papers(run, source):
    state = load(run)
    require(state["phase"] in ("collecting", "awaiting_selection"), "Selection already exists; use a new run for another search round.")
    papers = read_json(source)
    validate_papers(papers)
    old = {p["id"]: p for p in read_json(Path(run) / "papers.json")}
    for p in papers:
        if p["id"] in old:
            require(" ".join(p["title"].casefold().split()) == " ".join(old[p["id"]]["title"].casefold().split()),
                    "Do not reuse an existing paper ID for a different title.")
    write_json(Path(run) / "papers.json", papers)
    state.update(phase="collecting", papers_hash=digest(Path(run) / "papers.json"), briefing=None)
    return save(run, state, "import", {"count": len(papers)})


def brief(run, filename):
    state = load(run)
    require(state["phase"] in ("collecting", "awaiting_selection"), "Cannot replace briefing after selection; use a new round.")
    papers_for(run, state)
    state.update(briefing=record(run, filename), phase="awaiting_selection")
    return save(run, state, "brief")


def select(run, ids, user_message):
    state = load(run)
    require(state["phase"] != "collecting" and state["briefing"], "Introduce the papers before recording selection.")
    verify(run, state["briefing"])
    known = {p["id"] for p in papers_for(run, state)}
    require(bool(ids) and len(ids) == len(set(ids)), "Selection must be nonempty and unique.")
    require(set(ids) <= known, "Unknown paper IDs: " + ", ".join(sorted(set(ids) - known)))
    require(user_message.strip(), "Record the actual user decision.")
    state.update(phase="reading", selection={"ids": ids, "user_message": user_message, "at": now()}, readings={}, delivery=None)
    return save(run, state, "select", state["selection"])


def check(run, state=None):
    state = load(run) if state is None else state
    papers_for(run, state)
    require(state["selection"] is not None, "No user selection recorded.")
    verify(run, state["briefing"])
    missing = set(state["selection"]["ids"]) - set(state["readings"])
    require(not missing, "Missing full-text reading notes: " + ", ".join(sorted(missing)))
    for paper_id in state["selection"]["ids"]:
        verify(run, state["readings"][paper_id])
    return state


def read_paper(run, paper_id, note, source):
    state = load(run)
    papers_for(run, state)
    require(state["selection"] and paper_id in state["selection"]["ids"], "Only user-selected papers can be marked read.")
    verify(run, state["briefing"])
    require(source.strip(), "Full-text source is required.")
    if not http_url(source):
        source_path = Path(source)
        if not source_path.is_absolute():
            source_path = Path(run) / source_path
        require(source_path.is_file() and source_path.stat().st_size > 0, "Local full-text source is missing or empty.")
    item = record(run, note)
    require(Path(item["path"]).suffix.lower() == ".md", "Reading notes must be Markdown.")
    item.update(source=source, at=now())
    state["readings"][paper_id] = item
    state["delivery"] = None
    state["phase"] = "reading"
    try:
        check(run, state)
    except WorkflowError:
        pass  # Other selected notes may be missing or need re-registration.
    else:
        state["phase"] = "ready_for_slides"
    return save(run, state, "read", {"id": paper_id, "note": item})


def complete(run, deck, qa):
    state = check(run)
    path = local_file(run, deck)
    require(path.suffix.lower() == ".pptx", "Deliver an actual .pptx file.")
    require(zipfile.is_zipfile(path), "PPTX is not a valid ZIP package.")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require("[Content_Types].xml" in names and "ppt/presentation.xml" in names and
                any(re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", n) for n in names), "PPTX has no presentation/slides.")
        require(archive.testzip() is None, "PPTX package integrity check failed.")
    qa_record = record(run, qa)
    require(Path(qa_record["path"]).suffix.lower() == ".md", "QA record must be Markdown.")
    state.update(phase="complete", delivery={"deck": record(run, deck), "qa": qa_record, "at": now()})
    return save(run, state, "complete")


def status(run):
    state = load(run)
    issues = []
    try:
        if state["phase"] != "collecting":
            papers_for(run, state)
            verify(run, state["briefing"])
        if state["selection"]:
            check(run, state)
        if state["delivery"]:
            verify(run, state["delivery"]["deck"])
            verify(run, state["delivery"]["qa"])
    except (WorkflowError, OSError) as exc:
        issues.append(str(exc))
    return {"topic": state["topic"], "phase": state["phase"], "selected": state["selection"],
            "read": list(state["readings"]), "delivery": state["delivery"], "issues": issues}


def checkpoint(run, current, next_step, files):
    """Save partial progress without marking papers or the run complete."""
    state = load(run)
    require(current.strip() and next_step.strip(), "Current progress and next step are required.")
    require(bool(files), "Save at least one work file before creating a checkpoint.")
    item = {"current": current, "next_step": next_step, "files": [record(run, f) for f in files],
            "at": now(), "phase": state["phase"], "papers_hash": state["papers_hash"],
            "selection": state["selection"]}
    state["checkpoint"] = item
    return save(run, state, "checkpoint", item)


def resume(run):
    """Read-only recovery report. Never interpret a checkpoint as user approval."""
    state = load(run)
    report = status(run)
    valid, pending = [], []
    for paper_id in (state["selection"] or {}).get("ids", []):
        item = state["readings"].get(paper_id)
        try:
            require(item is not None, "Not registered")
            verify(run, item)
        except (WorkflowError, OSError):
            pending.append(paper_id)
        else:
            valid.append(paper_id)
    item = state.get("checkpoint")
    checkpoint_issues = []
    if item:
        if (item["phase"] != state["phase"] or item["papers_hash"] != state["papers_hash"]
                or item["selection"] != state["selection"]):
            checkpoint_issues.append("Checkpoint belongs to an earlier phase, candidate set, or selection.")
        for evidence in item["files"]:
            try:
                verify(run, evidence)
            except (WorkflowError, OSError) as exc:
                checkpoint_issues.append(str(exc))
    report.update(verified_notes=valid, pending_papers=pending, checkpoint=item,
                  checkpoint_issues=checkpoint_issues, checkpoint_usable=bool(item) and not checkpoint_issues,
                  selection_required=state["selection"] is None)
    report["recovery_rule"] = (
        "Verify actual files and any running tools before retrying. Reuse unchanged registered notes. "
        "Partial files are drafts, not completed readings. A checkpoint never substitutes for user selection. "
        "If the checkpoint is stale, use current state and inspect the files before deciding the next step."
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "import", "brief", "select", "read", "check", "complete", "checkpoint", "resume"):
        sub = commands.add_parser(name)
        sub.add_argument("run", type=Path)
        if name == "init":
            sub.add_argument("--topic", required=True)
        elif name == "import":
            sub.add_argument("--papers", required=True, type=Path)
        elif name == "brief":
            sub.add_argument("--file", required=True)
        elif name == "select":
            sub.add_argument("--ids", required=True, nargs="+")
            sub.add_argument("--user-message", required=True)
        elif name == "read":
            sub.add_argument("--id", required=True)
            sub.add_argument("--note", required=True)
            sub.add_argument("--source", required=True)
        elif name == "complete":
            sub.add_argument("--deck", required=True)
            sub.add_argument("--qa", required=True)
        elif name == "checkpoint":
            sub.add_argument("--current", required=True)
            sub.add_argument("--next", dest="next_step", required=True)
            sub.add_argument("--files", required=True, nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command == "init": result = init(args.run, args.topic)
        elif args.command == "status": result = status(args.run)
        elif args.command == "import": result = import_papers(args.run, args.papers)
        elif args.command == "brief": result = brief(args.run, args.file)
        elif args.command == "select": result = select(args.run, args.ids, args.user_message)
        elif args.command == "read": result = read_paper(args.run, args.id, args.note, args.source)
        elif args.command == "check": result = check(args.run)
        elif args.command == "checkpoint": result = checkpoint(args.run, args.current, args.next_step, args.files)
        elif args.command == "resume": result = resume(args.run)
        else: result = complete(args.run, args.deck, args.qa)
    except (WorkflowError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
