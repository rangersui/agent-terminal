#!/usr/bin/env python3
"""Static contract tests for k/km. No tmux session is started."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
K_PATH = ROOT / "scripts" / "k"
KM_PATH = ROOT / "scripts" / "km"
K_SRC = K_PATH.read_text(encoding="utf-8")
KM_SRC = KM_PATH.read_text(encoding="utf-8")
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILURES.append(f"{name}: {detail}".rstrip(": "))


def parse(path: Path, src: str) -> ast.Module:
    try:
        return ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        FAILURES.append(f"{path.name}: syntax error: {exc}")
        return ast.Module(body=[], type_ignores=[])


K_TREE = parse(K_PATH, K_SRC)
KM_TREE = parse(KM_PATH, KM_SRC)


def function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    FAILURES.append(f"missing function {name}")
    return None


def segment(src: str, node: ast.FunctionDef | None) -> str:
    if node is None:
        return ""
    lines = src.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def call_lines(node: ast.FunctionDef | None, name: str) -> list[int]:
    if node is None:
        return []
    out: list[int] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name) and func.id == name:
                out.append(child.lineno)
            elif isinstance(func, ast.Attribute) and func.attr == name:
                out.append(child.lineno)
    return sorted(out)


cmd_fire = function(K_TREE, "cmd_fire")
cmd_run = function(K_TREE, "cmd_run")
cmd_poll = function(K_TREE, "cmd_poll")
cmd_int = function(K_TREE, "cmd_int")
stream_process = function(K_TREE, "_stream_process")
cmd_new = function(K_TREE, "cmd_new")
main = function(K_TREE, "main")

# ── CellLock RAII structure ──
cell_lock_cls = None
for node in K_TREE.body:
    if isinstance(node, ast.ClassDef) and node.name == "CellLock":
        cell_lock_cls = node
        break
check("CellLock: class exists", cell_lock_cls is not None)
if cell_lock_cls:
    methods = {n.name for n in cell_lock_cls.body if isinstance(n, ast.FunctionDef)}
    for m in ("__init__", "__enter__", "__exit__", "mark_sent", "mark_keep"):
        check(f"CellLock: has {m}", m in methods)
    cl_seg = segment(K_SRC, cell_lock_cls)
    check("CellLock: __init__ acquires lock", "_acquire(" in cl_seg)
    check("CellLock: __exit__ releases lock", "_release(" in cl_seg)
    check("CellLock: __exit__ sends interrupt", "_send_interrupt(" in cl_seg)
    check("CellLock: __exit__ keeps lock on failed interrupt", "interrupt_failed" in cl_seg)

send_int_fn = function(K_TREE, "_send_interrupt")
si_seg = segment(K_SRC, send_int_fn)
check("_send_interrupt: sends ctrl-c", "send_int(" in si_seg)
check("_send_interrupt: returns bool", "return True" in si_seg and "return not" in si_seg)
check("_send_interrupt: checks session alive on failure", "T.has(" in si_seg)
check("_send_interrupt: re-frames via helper", "_send_frame_enters(" in si_seg)

for fn_name, fn in (("cmd_fire", cmd_fire), ("cmd_run", cmd_run)):
    seg = segment(K_SRC, fn)
    ensure = call_lines(fn, "_ensure_pipe")
    send = call_lines(fn, "_send_code")
    check(f"{fn_name}: uses CellLock", "CellLock(" in seg)
    check(f"{fn_name}: no raw _acquire", "_acquire(" not in seg)
    check(f"{fn_name}: no raw _release", "_release(" not in seg)
    check(f"{fn_name}: ensure_pipe exists", bool(ensure))
    check(f"{fn_name}: send_code exists", bool(send))
    check(f"{fn_name}: calls mark_sent", "mark_sent()" in seg)
    check(f"{fn_name}: pipe failure path", "pipe failed" in seg)
    # lock (CellLock) before pipe/send
    cl_lines = [child.lineno for child in ast.walk(fn)
                if isinstance(child, ast.Call)
                and isinstance(getattr(child, 'func', None), ast.Name)
                and child.func.id == "CellLock"]
    if cl_lines and ensure and send:
        check(f"{fn_name}: lock before pipe/send",
              min(cl_lines) < min(ensure) < min(send),
              f"CellLock={cl_lines}, _ensure_pipe={ensure}, _send_code={send}")

check("cmd_fire: calls mark_keep", "mark_keep()" in segment(K_SRC, cmd_fire))
check("cmd_run: calls mark_keep", "mark_keep()" in segment(K_SRC, cmd_run))

poll_seg = segment(K_SRC, cmd_poll)
check("poll: timeout marks lock", "_update_lock(" in poll_seg and "timed_out" in poll_seg)
check("poll: timeout checks update success", "if not _update_lock(" in poll_seg)
check("poll: timed_out blocks orphan release", 'meta.get("timed_out")' in poll_seg and "use k int or k kill" in poll_seg)
check("poll: wrong explicit cell is unknown", 'meta.get("cell_id") != cell_id' in poll_seg and '"unknown cell"' in poll_seg)
check("poll: no bare except on result read", "except: pass" not in poll_seg)
check("poll: no release on decode error", "corrupt" not in poll_seg,
      "JSONDecodeError must not release lock")

int_seg = segment(K_SRC, cmd_int)
check("int: uses _send_interrupt", "_send_interrupt(" in int_seg)
check("int: bails on failed interrupt", "interrupt failed" in int_seg)
check("int: writes error/interrupted", '"status": "error"' in int_seg and '"output": "interrupted"' in int_seg)
check("int: overwrites stale timeout result", "not os.path.exists(rpath)" not in int_seg)
check("int: kills watcher before release", int_seg.find("_kill_watcher") < int_seg.find("_release"))

new_seg = segment(K_SRC, cmd_new)
stream_seg = segment(K_SRC, stream_process)
check("hook: canonicalises path", "os.path.abspath(os.path.expanduser(prompt))" in new_seg)
check("hook: checks executable", "os.access(prompt, os.X_OK)" in new_seg)
check("hook: runtime uses absolute file path", "os.path.isabs(prompt)" in stream_seg and '"/" in prompt' not in stream_seg)

main_seg = segment(K_SRC, main)
check("session: _bg validates session", 'verb == "_bg"' in main_seg and "_validate_name(session)" in main_seg)
check("session: notify direct path validates session", 'verb == "notify"' in main_seg and "_validate_name(rest[0])" in main_seg)
check("session: k safe name exists", "_SAFE_NAME" in K_SRC and "def _validate_name" in K_SRC)
check("session: km safe name exists", "_SAFE_NAME" in KM_SRC and "def _validate_name" in KM_SRC and "_validate_name(session)" in KM_SRC)

check("pipe-pane: k replace mode", '"-o"' not in K_SRC)
check("pipe-pane: km replace mode", '"-o"' not in KM_SRC)

# cmd_kill must terminate bg watcher before killing session
cmd_kill = function(K_TREE, "cmd_kill")
kill_seg = segment(K_SRC, cmd_kill)
check("kill: terminates bg watcher", "_kill_watcher(" in kill_seg)

# _kill_watcher helper must use SIGTERM
kw_fn = function(K_TREE, "_kill_watcher")
kw_seg = segment(K_SRC, kw_fn)
check("_kill_watcher: sends SIGTERM", "signal.SIGTERM" in kw_seg)

# _send_frame_enters helper must use FRAME_ENTERS
sfe_fn = function(K_TREE, "_send_frame_enters")
sfe_seg = segment(K_SRC, sfe_fn)
check("_send_frame_enters: uses FRAME_ENTERS", "FRAME_ENTERS" in sfe_seg)

# _write_result helper: atomic write via os.replace
wr_fn = function(K_TREE, "_write_result")
wr_seg = segment(K_SRC, wr_fn)
check("_write_result: uses os.replace", "os.replace(" in wr_seg)
check("_write_result: uses fsync", "os.fsync(" in wr_seg)

# _update_lock helper exists
ul_fn = function(K_TREE, "_update_lock")
check("_update_lock: exists", ul_fn is not None)

# cmd_fire must store bg_pid in lock for orphan detection
fire_seg = segment(K_SRC, cmd_fire)
check("fire: stores bg_pid in lock", "_update_lock(" in fire_seg and "bg_pid" in fire_seg)

# ── dedup invariants: helpers used, not inlined ──
# only _send_frame_enters and _send_code should reference FRAME_ENTERS directly
for fn_name, fn in (("cmd_fire", cmd_fire), ("cmd_run", cmd_run),
                     ("cmd_int", cmd_int), ("_send_interrupt", send_int_fn)):
    seg = segment(K_SRC, fn)
    check(f"{fn_name}: no inline frame enters", "FRAME_ENTERS" not in seg,
          "should use _send_frame_enters()")
# only _write_result should do result file writes
for fn_name, fn in (("_stream_process", stream_process), ("cmd_int", cmd_int)):
    seg = segment(K_SRC, fn)
    check(f"{fn_name}: uses _write_result", "_write_result(" in seg,
          "should use _write_result() for atomic writes")
    check(f"{fn_name}: no inline json.dump to result", "json.dump(result, f)" not in seg,
          "should use _write_result()")
# only _kill_watcher should contain os.kill + SIGTERM
for fn_name, fn in (("cmd_int", cmd_int), ("cmd_kill", cmd_kill)):
    seg = segment(K_SRC, fn)
    check(f"{fn_name}: no inline os.kill for watcher", "signal.SIGTERM" not in seg,
          "should use _kill_watcher()")

# ANSI_RE must be consistent between k and km (compare compiled patterns)
def _extract_ansi_re(tree: ast.Module, src: str) -> str:
    """Find ANSI_RE assignment via AST, exec it, return .pattern."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "ANSI_RE":
                    lines = src.splitlines()
                    chunk = "\n".join(lines[node.lineno - 1 : node.end_lineno])
                    ns = {"re": __import__("re")}
                    exec(chunk, ns)
                    return ns["ANSI_RE"].pattern
    return ""

k_pat = _extract_ansi_re(K_TREE, K_SRC)
km_pat = _extract_ansi_re(KM_TREE, KM_SRC)
check("ansi_re: k and km consistent", k_pat == km_pat and k_pat != "",
      f"k={k_pat[:60]}... km={km_pat[:60]}...")

if FAILURES:
    print("contract failures:")
    for failure in FAILURES:
        print(f"  - {failure}")
    raise SystemExit(1)

print("contract tests passed")
