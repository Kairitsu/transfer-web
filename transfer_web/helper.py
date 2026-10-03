"""Filesystem operations for one endpoint.

This module runs in two ways:

* imported by the server for local endpoints;
* streamed over SSH to ``python3 -`` on remote endpoints (see
  ``remote_script``), printing one JSON line as the result.

It must therefore stay self-contained (standard library only) and compatible
with the older Python 3 versions that remote servers may still run.
"""
import base64
import json
import os
import posixpath
import stat
import sys

MAX_LIST_ENTRIES = 5000
FORBIDDEN_CHARS = ("\x00", "\n", "\r")


class HelperError(Exception):
    pass


def path_problem(value):
    if not isinstance(value, str):
        return "路径必须是字符串。"
    if len(value) > 4096:
        return "路径过长。"
    if value in ("", "."):
        return ""
    if any(char in value for char in FORBIDDEN_CHARS):
        return "路径包含换行或空字符。"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "路径不是有效的 UTF-8。"
    if value.startswith("/"):
        return "只能使用相对路径。"
    if any(part in ("", ".", "..") for part in value.split("/")):
        return "路径包含非法路径段（空、. 或 ..）。"
    return ""


def validate_rel(value):
    problem = path_problem(value or "")
    if problem:
        raise HelperError(problem)
    return "" if value in (None, "", ".") else value


def real_root(root):
    real = os.path.realpath(root)
    if not os.path.isdir(real):
        raise HelperError("根目录不存在：{}".format(root))
    return real


def is_inside(real, base):
    return real == base or real.startswith(base.rstrip("/") + "/")


def resolve(root, rel, must_exist):
    base = real_root(root)
    rel = validate_rel(rel)
    target = os.path.join(base, rel) if rel else base
    shown = posixpath.join(root, rel) if rel else root
    if must_exist and not os.path.lexists(target):
        raise HelperError("路径不存在：{}".format(shown))
    if not is_inside(os.path.realpath(target), base):
        raise HelperError("路径跳出了允许的根目录：{}".format(shown))
    return target, rel, shown


def display_name(name):
    # Undecodable bytes arrive as surrogate escapes; show them as U+FFFD.
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def entry_to_dict(parent_rel, entry):
    st = entry.stat(follow_symlinks=False)
    mode = st.st_mode
    is_symlink = stat.S_ISLNK(mode)
    is_dir = stat.S_ISDIR(mode)
    rel = posixpath.join(parent_rel, entry.name) if parent_rel else entry.name
    reason = ""
    if path_problem(rel):
        reason = "文件名包含换行或非 UTF-8 字符，无法传输"
    elif is_symlink:
        reason = "符号链接不可单独传输"
    return {
        "name": display_name(entry.name),
        "path": "" if path_problem(rel) else rel,
        "type": "dir" if is_dir else "symlink" if is_symlink else "file",
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "safe": not reason,
        "reason": reason,
    }


def op_list(root, path, show_hidden=False):
    target, rel, shown = resolve(root, path, must_exist=True)
    if not os.path.isdir(target):
        raise HelperError("不是目录：{}".format(shown))
    entries = []
    limited = False
    with os.scandir(target) as it:
        for entry in it:
            if not show_hidden and entry.name.startswith("."):
                continue
            if len(entries) >= MAX_LIST_ENTRIES:
                limited = True
                break
            try:
                entries.append(entry_to_dict(rel, entry))
            except OSError:
                continue
    entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
    return {"path": rel, "root": root, "entries": entries, "limited": limited}


def op_check(root, paths):
    """Check that each source exists, is inside the root and is not a symlink."""
    results = []
    for path in paths:
        try:
            target, rel, shown = resolve(root, path, must_exist=True)
            if not rel:
                raise HelperError("不能直接传输根目录。")
            if os.path.islink(target):
                raise HelperError("符号链接不可单独传输：{}".format(shown))
            results.append({"path": rel, "ok": True, "type": "dir" if os.path.isdir(target) else "file"})
        except HelperError as exc:
            results.append({"path": path, "ok": False, "error": str(exc)})
    return {"results": results}


def op_mkdir(root, path):
    target, rel, shown = resolve(root, path, must_exist=False)
    os.makedirs(target, exist_ok=True)
    if not os.path.isdir(target):
        raise HelperError("目标不是目录：{}".format(shown))
    if not is_inside(os.path.realpath(target), real_root(root)):
        raise HelperError("目标目录跳出了允许的根目录：{}".format(shown))
    return {"path": rel}


def op_stat(root, path):
    target, rel, shown = resolve(root, path, must_exist=False)
    if not os.path.lexists(target):
        return {"path": rel, "exists": False, "type": ""}
    return {"path": rel, "exists": True, "type": "dir" if os.path.isdir(target) else "file"}


def op_ping(root):
    real_root(root)
    return {}


def run(request):
    op = request.get("op")
    root = request.get("root") or ""
    if not root.startswith("/"):
        raise HelperError("根目录必须是绝对路径。")
    if op == "list":
        return op_list(root, request.get("path", ""), bool(request.get("showHidden")))
    if op == "check":
        return op_check(root, request.get("paths") or [])
    if op == "mkdir":
        return op_mkdir(root, request.get("path", ""))
    if op == "stat":
        return op_stat(root, request.get("path", ""))
    if op == "ping":
        return op_ping(root)
    raise HelperError("未知操作：{}".format(op))


def encode_request(request):
    return base64.b64encode(json.dumps(request).encode("utf-8")).decode("ascii")


def main(encoded_request):
    try:
        request = json.loads(base64.b64decode(encoded_request.encode("ascii")).decode("utf-8"))
        payload = run(request)
        payload["ok"] = True
    except HelperError as exc:
        payload = {"ok": False, "error": str(exc)}
    except Exception as exc:  # report unexpected failures instead of a traceback
        payload = {"ok": False, "error": "{}: {}".format(type(exc).__name__, exc)}
    # ASCII-only output survives remote shells whose locale is not UTF-8.
    sys.stdout.write(json.dumps(payload) + "\n")


def remote_script(request):
    """Source for ``python3 -``: this module plus a call carrying the request.

    The request travels on stdin rather than argv so large item lists never
    hit the kernel's per-argument size limit.
    """
    with open(__file__, encoding="utf-8") as fh:
        source = fh.read()
    return "{}\nmain({!r})\n".format(source, encode_request(request))


if __name__ == "__main__" and len(sys.argv) > 1:
    main(sys.argv[1])
