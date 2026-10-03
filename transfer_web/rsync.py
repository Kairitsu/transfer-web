import re

from .errors import AppError

PARTIAL_DIR = ".rsync-partial"
PREVIEW_LIST_LIMIT = 200

BASE_OPTIONS = [
    "rsync",
    "--archive",
    # Interrupted files wait in a hidden directory and are resumed with the
    # delta algorithm next time. Unlike --append-verify this never skips a
    # destination file just because it is not shorter than the source.
    f"--partial-dir={PARTIAL_DIR}",
    # Build the whole file list first so progress2 reports overall progress.
    "--no-inc-recursive",
    # -s: pass paths to the remote rsync without shell word-splitting
    # (--protect-args / --secluded-args depending on the rsync version).
    "-s",
    "--safe-links",
]

PROGRESS_RE = re.compile(
    r"^\s*(?P<bytes>[\d.,']+)\s+(?P<percent>\d{1,3})%\s+(?P<speed>\S+/s)\s+(?P<eta>\d+:\d{2}:\d{2})"
)
ITEMIZE_RE = re.compile(r"^(?P<update>[<>ch.*])(?P<kind>[fdLDS])(?P<attrs>[.+?a-zA-Z ]{9,10}) (?P<path>.+)$")
STATS_PATTERNS = {
    "files": re.compile(r"^Number of files: ([\d.,']+)"),
    "createdFiles": re.compile(r"^Number of created files: ([\d.,']+)"),
    "transferredFiles": re.compile(r"^Number of regular files transferred: ([\d.,']+)"),
    "totalSize": re.compile(r"^Total file size: ([\d.,']+) bytes"),
    "transferredSize": re.compile(r"^Total transferred file size: ([\d.,']+) bytes"),
}


def parse_number(text):
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else 0


def endpoints_for_transfer(src, dst):
    if src.id == dst.id:
        raise AppError("源和目标不能是同一个端点。")
    if not src.is_local and not dst.is_local:
        raise AppError("rsync 不能直接在两个远端之间传输，其中一端必须是运行本服务的机器。")


def build_command(src, dst, items, dest_path, skip_existing=False, dry_run=False):
    endpoints_for_transfer(src, dst)
    cmd = list(BASE_OPTIONS)
    if dry_run:
        cmd += ["--dry-run", "--itemize-changes", "--info=stats2"]
    else:
        cmd += ["--info=progress2,stats2,name1"]
    if skip_existing:
        cmd.append("--ignore-existing")
    if dst.is_local and dst.endpoint.owner:
        # Only matters when the service runs as root: otherwise rsync would
        # give received files the remote side's owner.
        cmd.append(f"--chown={dst.endpoint.owner}")
    remote = dst if src.is_local else src
    if not remote.is_local:
        cmd += ["-e", remote.rsync_shell()]
    cmd += [src.rsync_path(item) for item in items]
    cmd.append(dst.rsync_path(dest_path, directory=True))
    return cmd


def parse_progress(line):
    match = PROGRESS_RE.match(line)
    if not match:
        return None
    return {
        "bytes": parse_number(match.group("bytes")),
        "percent": min(100, int(match.group("percent"))),
        "speed": match.group("speed"),
        "eta": match.group("eta"),
    }


def parse_stats(text):
    stats = {}
    for line in text.splitlines():
        for key, pattern in STATS_PATTERNS.items():
            match = pattern.match(line.strip())
            if match:
                stats[key] = parse_number(match.group(1))
    return stats


def parse_preview(output):
    """Summarise ``rsync --dry-run --itemize-changes`` output."""
    new_files = 0
    new_dirs = 0
    updated = []
    updated_count = 0
    for line in output.splitlines():
        match = ITEMIZE_RE.match(line)
        if not match:
            continue
        update, kind, attrs, path = match.group("update", "kind", "attrs", "path")
        if update == "*":
            continue
        is_new = attrs.startswith("+++++++")
        if kind == "d":
            if is_new:
                new_dirs += 1
            continue
        if update not in "<>":
            continue  # attribute-only change or local symlink creation
        if is_new:
            new_files += 1
        else:
            updated_count += 1
            if len(updated) < PREVIEW_LIST_LIMIT:
                updated.append(path)
    stats = parse_stats(output)
    return {
        "newFiles": new_files,
        "newDirs": new_dirs,
        "updatedCount": updated_count,
        "updated": updated,
        "updatedTruncated": updated_count > len(updated),
        "transferSize": stats.get("transferredSize", 0),
        "totalSize": stats.get("totalSize", 0),
        "files": stats.get("files", 0),
    }
