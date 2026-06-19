# k-kernel

Structured async execution over PTY for AI agents. REPL-agnostic. Zero config.

Agent fires code, polls for output, gets JSON. REPL stays alive between cells. Any readline prompt works.

**Requires tmux 3.0+** — k drives tmux for PTY multiplexing; it does not bundle or replace it.

## Quick Start

```bash
k new work bash
k run -j work "echo hello"
# {"cell_id":"...","status":"done","output":"hello"}

k new py python3 -i                         # Python 3.12 and below
k new py "env PYTHON_BASIC_REPL=1 python3 -i"  # Python 3.13+ (disables _pyrepl auto-indent)
k run -j py "print(42)"
```

## Install

```bash
# requires: python 3.8+, tmux 3.0+
git clone <repo> && cd agent-repl
ln -sf "$(pwd)/scripts/k"  /usr/local/bin/k
ln -sf "$(pwd)/scripts/km" /usr/local/bin/km
```

Or add `scripts/` to PATH: `export PATH="/path/to/agent-repl/scripts:$PATH"`

## Commands

```
k new    <session> [cmd...] [--prompt="x"]     spawn (default: bash)
k new    <session> <cmd> --prompt=./hook        hook mode
k fire   [-t N] [session] <code>               async fire (default 300s)
k poll   [session] [cell_id]                   poll (O(1))
k run    [-j] [-t N] [session] <code>          sync (default 30s)
k await  ...                                   alias for run
k notify [session] <message>                   notification
k int    [session]                             ctrl-c
k kill   <session>                             kill + cleanup
k ls                                           list sessions
k status [session]                             health check
k watch  [session]                             live filtered view
k history [-n N] [session]                     last N×5 lines (default 5)
```

## Frame Detection

Three modes via `--prompt`:

| --prompt= | mode | how |
|-----------|------|-----|
| *(not set)* | repeat | 5 empty Enters → 5 identical lines → done |
| `"(gdb)"` | exact | match prompt string |
| `./hook.py` | hook | stdin lines → hook exit → done |

Hook protocol: k feeds ANSI-stripped lines to stdin. Hook exits = frame end. Hook paths must include a path separator (`/`, or `\` on Windows). Path is canonicalised to absolute at `k new` time; hook must exist and be executable.

## How It Works

```
k fire "echo hello"
  |
  +-- acquires lock (rejected fire = zero side effects)
  +-- sends code via paste-buffer (atomic)
  +-- sends 5 frame Enters (repeat mode only)
  +-- starts background stream processor
  |
  stream processor tails log:
    ECHOING: skip echo_count lines
    OUTPUT:  collect lines
    DONE:    5 identical lines / prompt match / hook exit
  |
  writes result file -> exits
  |
k poll
  +-- checks result file (O(1))
  +-- returns JSON
```

## Safety

| invariant | mechanism |
|-----------|-----------|
| one cell per session | O_EXCL lock, acquired before send |
| timeout keeps lock | lock marked `timed_out`; subsequent polls say `use k int or k kill` |
| orphan recovery | bg PID in lock, poll checks `os.kill(pid, 0)` (POSIX) |
| no line-wrap skew | tmux width 10000 |
| atomic send | per-session named paste-buffer `k_{session}` |
| ctrl-c safe | kills watcher, writes `{"status": "error", "output": "interrupted"}`, re-sends frame enters (repeat only) |
| session name validation | `[A-Za-z0-9_.-]+`, no `..`, no path traversal |
| idempotent pipe restart | pipe-pane replaced on every fire/run |
| atomic result writes | tmp + fsync + `os.replace` — poll never reads partial JSON |
| no output classification | "done" = prompt appeared, not success |

## JSON Schema (k)

```
fired:        {"cell_id": "...", "status": "fired"}
running:      {"cell_id": "...", "status": "running"}
done:         {"cell_id": "...", "status": "done", "output": "..."}
timeout:      {"cell_id": "...", "status": "timeout", "output": ""}
timeout(2+):  {"cell_id": "...", "status": "timeout", "output": "use k int or k kill"}
error:        {"status": "error", "output": "..."}
cell error:   {"cell_id": "...", "status": "error", "output": "..."}
```

Errors without `cell_id`: `no session 'x'`, `active cell 'x'`, `pipe failed: ...`, `send failed: ...`, `no active cell on 'x'`.
Errors with `cell_id`: `interrupted`, `unknown cell`, `watcher died`, `lock update failed; use k int or k kill`, `interrupt failed; use k kill`.

## km — event monitor

```
km <session> [cell_id] [-1]
```

Watches a session via pipe-pane. Each stdout line is one JSON event. `-1` exits after first completion (one-shot `.then()`).

```
fired:   {"cell_id": "...", "session": "...", "status": "fired",  "ts": "..."}
done:    {"cell_id": "...", "session": "...", "status": "done",   "ts": "..."}
notify:  {"session": "...", "status": "notify", "from": "...", "message": "...", "ts": "..."}
closed:  {"session": "...", "status": "closed", "ts": "..."}
error:   {"session": "...", "status": "error",  "message": "...", "ts": "..."}
```

## Testing

```bash
python tests/test_contracts.py      # static code contracts, no tmux
python tests/test_docs.py           # README/SKILL drift, no tmux
bash test.sh                       # 34 tests (32 without gdb), runtime smoke suite
python tests/test_regressions.py    # targeted audit regressions
python tests/run_all.py             # all suites
```

## Files

```
scripts/k      main script
scripts/km     event monitor
test.sh        runtime smoke suite
tests/*.py     static, docs, and regression suites
SKILL.md       agent reference
EXAMPLES.md    patterns + philosophy
```
