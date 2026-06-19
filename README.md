# k-kernel

Structured async execution over PTY for AI agents. REPL-agnostic. Zero config.

Agent fires code, polls for output, gets JSON. REPL stays alive between cells. Any readline prompt works.

## Quick Start

```bash
k new work bash
k run -j work "echo hello"
# {"cell_id":"...","status":"done","output":"hello"}

k new py python3 -i
k run -j py "print(42)"
```

## Commands

```
k new    <session> <cmd...> [--prompt="x"]     spawn
k new    <session> <cmd> --prompt=./hook        hook mode
k fire   [session] <code> [-t N]               async fire
k poll   [session] [cell_id]                   poll (O(1))
k run    [session] <code>                      sync (lock + send + wait)
k run -j [session] <code>                      sync, JSON
k run -j -t N [session] <code>                 sync, timeout
k notify [session] <message>                   notification
k int    [session]                             ctrl-c
k kill   <session>                             cleanup
k ls / k status / k watch / k history
```

## Frame Detection

Three modes via `--prompt`:

| --prompt= | mode | how |
|-----------|------|-----|
| *(not set)* | repeat | 5 empty Enters → 5 identical lines → done |
| `"(gdb)"` | exact | match prompt string |
| `./hook.py` | hook | stdin lines → hook exit → done |

Hook protocol: k feeds ANSI-stripped lines to stdin. Hook exits = frame end. Path must contain `/`.

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
| timeout keeps lock | lock marked `timed_out`; only `k int` / `k kill` releases |
| orphan recovery | bg PID in lock, poll checks `os.kill(pid, 0)` (POSIX) |
| no line-wrap skew | tmux width 10000 |
| atomic send | per-session named paste-buffer `k_{session}` |
| ctrl-c safe | kills watcher, writes result, re-sends frame enters (repeat only) |
| session name validation | `[A-Za-z0-9_.-]+`, no `..`, no path traversal |
| idempotent pipe restart | pipe-pane replaced on every fire/run |
| no output classification | "done" = prompt appeared, not success |

## Testing

```bash
bash test.sh           # 34 tests, covers all edge cases
```

## Files

```
scripts/k      main script
scripts/km     event monitor
test.sh        test suite (34 tests)
SKILL.md                  agent reference
EXAMPLES.md               patterns + philosophy
```
