"""Validate durable history, transfer generated files, and build Pages safely."""
import argparse
import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

from .dedup import build_seen_aliases, load_seen, seen_entry_aliases
from .copilot import safe_diagnostic
from .util import atomic_write

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("mapf-mapd-warehouse", "doc-structure-rag", "reading")


def git(*args, root=ROOT, check=True):
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          encoding="utf-8", timeout=120, check=check)


def generated_path(rel):
    return bool(
        rel in {"index.html", "data/seen.json", *(f"{field}/index.html" for field in FIELDS)}
        or re.fullmatch(r"mapf-mapd-warehouse/[a-z0-9][a-z0-9._-]*\.html", rel)
        or re.fullmatch(r"data/runs/\d{4}-\d{2}-\d{2}\.(?:json|html)", rel)
    )


def safe_file(root, rel):
    path = Path(root) / rel
    if not path.resolve().is_relative_to(Path(root).resolve()) or path.is_symlink():
        raise ValueError(f"Unsafe artifact path: {rel}")
    return path


def validate_history(root=ROOT, baseline=None):
    root = Path(root)
    seen = load_seen(root / "data/seen.json")
    if baseline is None:
        baseline = json.loads(git("show", "HEAD:data/seen.json", root=root).stdout)
    aliases = set().union(*(build_seen_aliases(entries) for entries in baseline.values()))
    for field, entries in baseline.items():
        for key, info in entries.items():
            if seen.get(field, {}).get(key) != info:
                raise ValueError(f"Historical metadata changed or removed: {field}/{key}")
    added = []
    for field, entries in seen.items():
        for key, info in entries.items():
            rel = info.get("file", "")
            path = safe_file(root, rel)
            if not path.is_file() or not path.read_text(encoding="utf-8").rstrip().endswith("</html>"):
                raise ValueError(f"Missing/incomplete article: {rel}")
            if key in baseline.get(field, {}):
                continue
            if field != FIELDS[0] or not info.get("engine", "").startswith("copilot-cli:"):
                raise ValueError(f"Unexpected field or unverified/stub article: {rel}")
            if not info.get("basis", "").startswith("fulltext") or len(info.get("tldr", "")) < 40:
                raise ValueError(f"Insufficient source/summary: {rel}")
            new_aliases = set(seen_entry_aliases(key, info))
            if aliases & new_aliases:
                raise ValueError(f"Duplicate article: {rel}")
            aliases.update(new_aliases)
            added.append(info)
    if len(added) > 2:
        raise ValueError("More than two new articles")
    for day in {info.get("added") for info in added}:
        count = sum(info.get("added") == day and info.get("selection") != "manual"
                    for info in seen.get(FIELDS[0], {}).values())
        if count > 2:
            raise ValueError(f"Daily two-paper limit exceeded: {day}")
    return seen


def stage(destination, root=ROOT):
    root, destination = Path(root), Path(destination)
    validate_history(root)
    changed = git("diff", "--name-only", "HEAD", root=root).stdout.splitlines()
    changed += git("ls-files", "--others", "--exclude-standard", root=root).stdout.splitlines()
    files = sorted({rel for rel in changed if generated_path(rel)})
    seen_files = {info["file"] for entries in load_seen(root / "data/seen.json").values() for info in entries.values()}
    for rel in files:
        path = safe_file(root, rel)
        if not path.is_file():
            raise ValueError(f"Generated history deletion is forbidden: {rel}")
        if rel.startswith(FIELDS[0] + "/") and not rel.endswith("/index.html"):
            if rel not in seen_files:
                raise ValueError(f"Article was not checkpointed: {rel}")
            previous = git("cat-file", "-e", f"HEAD:{rel}", root=root, check=False)
            if previous.returncode == 0:
                raise ValueError(f"Existing article was overwritten: {rel}")
        target = safe_file(destination / "files", rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    destination.mkdir(parents=True, exist_ok=True)
    atomic_write(destination / "manifest.json", json.dumps({
        "base_sha": git("rev-parse", "HEAD", root=root).stdout.strip(), "files": files,
    }, indent=2))
    print(f"Validated recovery artifact: {len(files)} files")


def apply(source, root=ROOT):
    source, root = Path(source), Path(root)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest["base_sha"] != git("rev-parse", "HEAD", root=root).stdout.strip():
        raise ValueError("Artifact baseline differs; check out its base_sha before applying")
    for rel in manifest["files"]:
        if not generated_path(rel):
            raise ValueError(f"Unexpected artifact file: {rel}")
        content = safe_file(source / "files", rel).read_text(encoding="utf-8")
        atomic_write(safe_file(root, rel), content)
    validate_history(root)
    return manifest["files"]


def build(destination, root=ROOT):
    """Only public HTML/report assets enter Pages; no source, tokens or PDFs."""
    root, destination = Path(root), Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Pages output must be empty to avoid publishing stale files")
    # Completeness check also works after committing, with no Git dependency.
    validate_history(root, baseline=load_seen(root / "data/seen.json"))
    files = [root / "index.html", root / ".nojekyll"]
    for field in FIELDS:
        files += sorted((root / field).glob("*.html"))
    files += sorted((root / "data/runs").glob("*.html"))
    files += sorted((root / "data/runs").glob("*.json"))
    for path in files:
        rel = path.relative_to(root).as_posix()
        safe_file(root, rel)
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    print(f"Pages build: {len(files)} files -> {destination}")


def push(source, root=ROOT, branch="main", retry_delay=5):
    """Commit verified paths; bounded fast-forward/rebase retries, never force."""
    root = Path(root)
    manifest = json.loads((Path(source) / "manifest.json").read_text(encoding="utf-8"))
    files = manifest["files"]
    if any(not generated_path(rel) for rel in files):
        raise ValueError("Unexpected file in commit manifest")
    validate_history(root)
    if files:
        git("add", "--", *files, root=root)
    if git("diff", "--cached", "--quiet", root=root, check=False).returncode:
        day = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date()
        git("-c", "user.name=github-actions[bot]", "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-m", f"daily: MAPF survey {day}", root=root)
    for attempt in range(3):
        fetched = git("fetch", "origin", branch, root=root, check=False)
        if fetched.returncode == 0:
            rebased = git("rebase", "FETCH_HEAD", root=root, check=False)
            if rebased.returncode:
                conflicts = git("diff", "--name-only", "--diff-filter=U", root=root, check=False).stdout
                git("rebase", "--abort", root=root, check=False)
                raise RuntimeError("Git rebase conflict; recovery artifact preserved. Resolve without rerunning Copilot: " + safe_diagnostic(conflicts))
            published = git("push", "origin", f"HEAD:refs/heads/{branch}", root=root, check=False)
            if published.returncode == 0:
                print("Verified history persisted to GitHub")
                return
        diagnostic = fetched.stderr if fetched.returncode else published.stderr
        print(f"Git sync/push failed ({attempt + 1}/3): {safe_diagnostic(diagnostic)}")
        if attempt < 2:
            time.sleep(retry_delay)
    raise RuntimeError("Git push failed; Pages deployment stopped. Recovery artifact preserved.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["stage", "apply", "build", "push"])
    parser.add_argument("path")
    args = parser.parse_args()
    globals()[args.operation](args.path)


if __name__ == "__main__":
    main()
