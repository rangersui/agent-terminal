# agent-tty: Persistent REPL Patterns

## Core Principle

bash_tool is curl. k is a socket.

Every bash_tool call spawns a new process, runs a command, returns output, and dies. No state survives. To pass information between steps, you write files.

k keeps a process alive inside tmux. Variables, imports, connections, cwd, env — everything persists across cells. The process IS the workspace. Files become backups, not the primary medium.

```
bash_tool (stateless, one-shot)
  └─ k run/fire/poll (stateless CLI, the "launcher")
       └─ tmux session (stateful, persistent)
            └─ bash / python / node / R REPL (stateful, persistent)
```

The launcher is stateless. The target is stateful. This is exactly how curl talks to a server — the client forgets, the server remembers.

---

## Pattern: Live Control Plane

Static config + reload → live variables + one-cell patch.

Feature flags, firewall rules, rate limits, circuit breaker thresholds —
traditionally locked in config files. Change means edit, commit, deploy,
restart, hope. In a persistent REPL, they are Python variables. The agent
observes traffic, changes a variable, and the next request sees it. No restart.
No redeploy. Connection never drops.

### Example: adaptive firewall via nginx auth\_request

nginx `auth_request` calls a subrequest before every protected request.
Point it at a Flask handler running in your REPL:

```nginx
# nginx.conf
location /api/ {
    auth_request /ai-decide;
    proxy_pass http://backend;
}
location = /ai-decide {
    internal;
    proxy_pass http://127.0.0.1:8080/decide;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Original-URI $request_uri;
    proxy_set_header X-User-Agent $http_user_agent;
}
```

The REPL is the decision engine:

```bash
cat > /tmp/k_firewall.py << 'EOF'
import threading
from flask import Flask, request

app = Flask(__name__)
blocked = set()
fingerprints = {}
rules = []

@app.route('/decide')
def decide():
    ip = request.headers.get('X-Real-IP', request.remote_addr)
    path = request.headers.get('X-Original-URI', '/')
    ua = request.headers.get('X-User-Agent', '')

    if ip in blocked:
        return '', 403

    fp = fingerprints.setdefault(ip, {'paths': [], 'ua': set()})
    fp['paths'].append(path)
    fp['ua'].add(ua)

    for rule in rules:
        verdict = rule(ip, path, ua, fp)
        if verdict is not None:
            return '', verdict

    return '', 200

threading.Thread(
    target=lambda: app.run(host='127.0.0.1', port=8080, use_reloader=False),
    daemon=True,
).start()
print('firewall listening on :8080')
EOF

k new firewall python3 -i
k run -j firewall "exec(open('/tmp/k_firewall.py').read())"
```

Flask runs in a daemon thread; the REPL stays interactive. `blocked`,
`fingerprints`, `rules` live in the same namespace — the agent reads and
writes them, the handler sees changes on the next request.

```bash
# who's hitting the server?
cat > /tmp/k_check_traffic.py << 'EOF'
for ip, fp in sorted(fingerprints.items(), key=lambda x: len(x[1]['paths']), reverse=True)[:10]:
    print(f'{ip}: {len(fp["paths"])} reqs, {len(fp["ua"])} UAs, last: {fp["paths"][-1]}')
EOF
k run -j firewall "exec(open('/tmp/k_check_traffic.py').read())"

# block a scanner — immediate, no reload
k run -j firewall "blocked.add('1.2.3.4'); print('blocked')"

# add a rule: block anyone probing admin paths 3+ times
cat > /tmp/k_add_rule.py << 'EOF'
def block_scanner(ip, path, ua, fp):
    probes = ['/wp-admin', '/phpmyadmin', '/.env', '/.git']
    if sum(1 for p in fp['paths'] if any(s in p for s in probes)) >= 3:
        blocked.add(ip)
        return 403
rules.append(block_scanner)
print(f'{len(rules)} rules active')
EOF
k run -j firewall "exec(open('/tmp/k_add_rule.py').read())"
```

One cell changed the firewall. nginx didn't restart. Connections didn't drop.

### Example: feature flags

No external service. A dict:

```bash
cat > /tmp/k_flags.py << 'EOF'
import random

flags = {
    'new_checkout': {'enabled': True, 'rollout': 0.1},
    'dark_mode':    {'enabled': False},
}

def is_enabled(flag, user_id=None):
    f = flags.get(flag, {})
    if not f.get('enabled'):
        return False
    rollout = f.get('rollout', 1.0)
    if user_id:
        return (hash(user_id) % 100) / 100 < rollout
    return random.random() < rollout
EOF

k new ctrl python3 -i
k run -j ctrl "exec(open('/tmp/k_flags.py').read())"
```

Your app handler calls `is_enabled('new_checkout', user_id)`. The agent
adjusts in real time:

```bash
# bug found — kill it now
k run -j ctrl "flags['new_checkout']['enabled'] = False; print('killed')"

# fixed — slow rollout
k run -j ctrl "flags['new_checkout'] = {'enabled': True, 'rollout': 0.01}; print('1%')"

# looks good — ramp
k run -j ctrl "flags['new_checkout']['rollout'] = 0.1; print('10%')"

# ship it
k run -j ctrl "flags['new_checkout']['rollout'] = 1.0; print('100%')"
```

Each line takes effect on the next request. No deploy. No propagation delay.
LaunchDarkly is a SaaS. This is a dict.

### Example: circuit breaker

Hystrix is a framework. This is 20 lines:

```bash
cat > /tmp/k_circuits.py << 'EOF'
import time

circuits = {}

def call_service(name, fn):
    c = circuits.setdefault(name, {
        'fails': 0, 'state': 'closed', 'threshold': 5, 'opened_at': 0,
    })
    if c['state'] == 'open':
        if time.time() - c['opened_at'] > 30:
            c['state'] = 'half-open'
        else:
            raise Exception(f'{name} circuit open')
    try:
        result = fn()
        c['fails'] = 0
        if c['state'] == 'half-open':
            c['state'] = 'closed'
        return result
    except Exception:
        c['fails'] += 1
        if c['fails'] >= c['threshold']:
            c['state'] = 'open'
            c['opened_at'] = time.time()
        raise
EOF

k run -j ctrl "exec(open('/tmp/k_circuits.py').read())"
```

Your app calls `call_service('payment', lambda: stripe.charge(...))`. The
agent adjusts live:

```bash
# init circuit (call_service auto-creates on first use, or init explicitly)
k run -j ctrl "circuits.setdefault('payment', {'fails': 0, 'state': 'closed', 'threshold': 5, 'opened_at': 0}); print('ready')"

# payment service is flaky — tolerate more before tripping
k run -j ctrl "circuits['payment']['threshold'] = 20; print('threshold raised')"

# manual trip during maintenance
k run -j ctrl "import time; circuits['payment'].update(state='open', opened_at=time.time()); print('tripped')"

# maintenance done — force close
k run -j ctrl "circuits['payment'].update(state='closed', fails=0); print('closed')"

# check all circuits
cat > /tmp/k_check_circuits.py << 'EOF'
for name, c in circuits.items():
    print(f"{name}: {c['state']} ({c['fails']}/{c['threshold']} fails)")
EOF
k run -j ctrl "exec(open('/tmp/k_check_circuits.py').read())"
```

### Safety boundary

One cell changing production behavior is a feature and a liability. In
production, the live control plane needs guardrails:

- **Audit log**: every mutation logged with timestamp, cell\_id, and before/after
- **Checkpoint/rollback**: snapshot state before changes, restore if wrong
- **Auth**: private Unix socket (see Worker Isolation) or TLS + token
- **Human approval**: agent proposes, human confirms before apply
- **Blast radius**: start with non-critical paths; graduate to critical ones

The persistent REPL makes dynamic decisions trivial. Making them *safe* is
the real engineering work.

---

## Context Loading: source & exec

A REPL is blank memory. `source` (bash) and `exec` (python) inject a snapshot into it. After loading, every cell runs inside that context.

This is also the production path for complex code. Write literal content with a
quoted heredoc, then load it through `k`: the file is only transport, while
`source`/`exec` runs in the live session. You avoid shell-quoting fights and
the multiline-send edge cases that can confuse frame detection because the
command sent to `k` is one simple line.

### Bash: source

```bash
# write a context file
cat > /tmp/my_ctx.sh << 'EOF'
export API_URL="https://api.example.com"
export API_KEY="sk-..."
export DB_HOST="prod-db.internal"

request() {
    curl -s -H "Authorization: Bearer $API_KEY" "$API_URL/$1"
}

dbquery() {
    PGPASSWORD=$DB_PASS psql -h "$DB_HOST" -U app -d main -c "$1"
}

echo "ctx loaded: request <path> | dbquery <sql>"
EOF

# load it into a persistent session
k new work bash
k run work "source /tmp/my_ctx.sh"
# → ctx loaded: request <path> | dbquery <sql>

# now use it — functions and env vars persist
k run -j work "request users/me"
k run -j work "dbquery 'SELECT count(*) FROM orders'"
# everything works, nothing re-imported
```

### Python: exec

```bash
# write a context file
cat > /tmp/my_ctx.py << 'PYEOF'
import json
import time
import websocket

# connections
ws = None
db = None

def connect_ws(url):
    global ws
    ws = websocket.create_connection(url)
    print(f'ws connected to {url}')

def connect_db(dsn):
    global db
    import psycopg2
    db = psycopg2.connect(dsn)
    db.autocommit = False
    print(f'db connected')

def query(sql, *args):
    cur = db.cursor()
    cur.execute(sql, args)
    rows = cur.fetchall()
    for r in rows:
        print(r)
    return rows

def commit():
    db.commit()
    print('committed')

def rollback():
    db.rollback()
    print('rolled back')

print('ctx loaded: connect_ws() connect_db() query() commit() rollback()')
PYEOF

# load it
k new py python3 -i
k run -j py "exec(open('/tmp/my_ctx.py').read())"
# → ctx loaded

# connect
k run -j py "connect_db('postgresql://app:pass@localhost/main')"
k run -j py "query('SELECT * FROM users LIMIT 3')"
# → rows printed, connection alive, transaction open
```

### Context Recovery

Session died? New REPL, one line, back to work:

```bash
k kill py
k new py python3 -i
k run -j py "exec(open('/tmp/my_ctx.py').read())"
# everything is back
```

### State Checkpoint

Save current state, restore later:

```bash
cat > /tmp/k_save_state.py << 'EOF'
with open('/tmp/checkpoint.py', 'w') as f:
    f.write(f'radius = {repr(radius)}\n')
    f.write(f'data = {repr(data)}\n')
    f.write(f'results = {repr(results)}\n')
print('checkpointed')
EOF
k run -j py "exec(open('/tmp/k_save_state.py').read())"

# later, in a new session
k run -j py "exec(open('/tmp/checkpoint.py').read())"
# radius, data, results all restored
```

### Hot Patching

Found a bug? Redefine the function in the next cell. No re-source needed:

```bash
# original function is broken
k run -j work "cb_price"
# → error

# fix it live — only this function, everything else untouched
k run -j work '
cb_price() {
    (echo "$CB_SUB"; sleep 3) \
        | websocat "$CB_URL" 2>/dev/null \
        | grep "\"type\":\"ticker\"" \
        | head -1 \
        | python3 -c "import sys,json; t=json.load(sys.stdin); print(t[\"product_id\"],t[\"price\"])"
}
echo "fixed"'
# → fixed

k run -j work "cb_price"
# → works now. env vars, other functions, cwd — all untouched
```

### Painless Trial and Error

One-shot: error = scorched earth. Process dies, imports gone, variables gone, connections closed, start from zero.

REPL: error = one failed cell. Everything else survives.

```bash
k run -j py "import pandas as pd; df = pd.read_csv('big_data.csv')"
# → ok, df loaded (took 30 seconds)

k run -j py "df.groupby('category').agg({'revenue': 'sum'}).sort_values('revnue')"
# → error: KeyError 'revnue' — typo

# one-shot: re-import pandas, re-read csv (30 seconds), fix typo, try again
# REPL: just fix the typo. df is still there. pandas is still imported.

k run -j py "df.groupby('category').agg({'revenue': 'sum'}).sort_values('revenue')"
# → works. zero re-setup cost.
```

This compounds. In a 10-step workflow, step 7 fails:

```
one-shot:  redo steps 1-6 (imports, connections, data loading) → fix step 7 → retry
REPL:      fix step 7 → retry. steps 1-6 are still in memory.
```

Database connection still open. SSH still connected. WebSocket still subscribed. Model still in GPU. You only redo the thing that broke.

```bash
k run -j py "conn.execute('SLECT * FROM users')"
# → error: syntax error at "SLECT"
# connection is still alive. transaction is still open.

k run -j py "conn.execute('SELECT * FROM users')"
# → works. same connection, same transaction, no reconnect.
```

The REPL is a safety net. Try things, break things, fix things. The cost of failure is one cell, not the entire session.

---

## Pattern: Persistent Connections

The fundamental insight: **building a connection is expensive, using it is cheap.** One-shot CLI pays the build cost every time. REPL pays once.

### Database: Interactive Transactions

```bash
k new py python3 -i
k run -j py "exec(open('/tmp/db_ctx.py').read())"
k run -j py "connect_db('postgresql://...')"

# explore
k run -j py "query('SELECT * FROM users LIMIT 5')"
# AI sees the data, decides what to do

# modify — inside a transaction
k run -j py "query('ALTER TABLE users ADD COLUMN risk_score FLOAT')"
k run -j py "query('UPDATE users SET risk_score = 0.5 WHERE signup_date < %s', '2024-01-01')"

# inspect
k run -j py "query('SELECT id, name, risk_score FROM users WHERE risk_score > 0 LIMIT 5')"

# not right? rollback. connection still alive
k run -j py "rollback()"

# try again with different logic...
k run -j py "query('UPDATE users SET risk_score = ...')"
k run -j py "commit()"
```

One-shot can't do this. The transaction exists only inside the connection. Kill the process, lose the transaction.

### WebSocket: Subscribe/Unsubscribe

```bash
k run -j py "connect_ws('wss://ws-feed.exchange.coinbase.com')"
# → connected

k run -j py "ws.send(json.dumps({'type':'subscribe','channels':[{'name':'ticker','product_ids':['BTC-USD']}]}))"
# → subscribed

k run -j py "
for i in range(3):
    t = json.loads(ws.recv())
    if t['type'] == 'ticker':
        print(t['product_id'], t['price'])
"
# → BTC-USD 64290.82
# → BTC-USD 64291.86
# → BTC-USD 64290.83

k run -j py "ws.send(json.dumps({'type':'unsubscribe','channels':['ticker']}))"
# → unsubscribed. connection still alive, just silent

k run -j py "ws.close()"
# → closed when YOU say so
```

The connection is a variable. `send()` and `recv()` are method calls. Subscribe and unsubscribe are just messages on the same socket. No process restart needed.

### SSH: Persistent Remote Session

k doesn't need a "remote" feature. SSH is just a command:

```bash
k new remote "ssh -o StrictHostKeyChecking=no user@prod-server"

# state persists on the REMOTE machine
k run -j remote "cd /opt/app && export ENV=production"
k run -j remote "tail -50 logs/error.log"
# AI reads logs, forms hypothesis
k run -j remote "grep 'OOM' /var/log/syslog | tail -10"
# confirmed — fix it
k run -j remote "sed -i 's/MaxHeap=512/MaxHeap=1024/' config.yaml"
k run -j remote "systemctl restart app"
k run -j remote "tail -20 logs/error.log"
# verify fix worked
```

cd, env, everything persists on the remote side. No re-login between steps. The SSH connection lives in tmux, k's prompt detection works transparently over it — the remote shell's prompt flows back through SSH, k detects completion the same way.

Nothing installed on the remote. Just SSH and bash.

### CDP: Chrome DevTools Protocol

CDP is a WebSocket to Chrome's internals:

```bash
# launch headless chrome
k run -j work "google-chrome --headless --remote-debugging-port=9222 &"

# connect via CDP
k run -j py "
import websocket, json
# get the page's debugger URL
import urllib.request
targets = json.loads(urllib.request.urlopen('http://localhost:9222/json').read())
ws_url = targets[0]['webSocketDebuggerUrl']
cdp = websocket.create_connection(ws_url)
print('CDP connected')
"

# navigate
k run -j py "
cdp.send(json.dumps({'id':1, 'method':'Page.navigate', 'params':{'url':'https://example.com'}}))
print(json.loads(cdp.recv()))
"

# execute JS in the page
k run -j py "
cdp.send(json.dumps({'id':2, 'method':'Runtime.evaluate', 'params':{'expression':'document.title'}}))
result = json.loads(cdp.recv())
print(result['result']['result']['value'])
"

# intercept network requests
k run -j py "
cdp.send(json.dumps({'id':3, 'method':'Fetch.enable', 'params':{'patterns':[{'urlPattern':'*api*'}]}}))
"
# every API call now passes through AI's hands

# modify DOM
k run -j py "
cdp.send(json.dumps({'id':4, 'method':'Runtime.evaluate',
    'params':{'expression': 'document.querySelector(\"h1\").textContent = \"AI was here\"'}}))
"
```

One WebSocket connection. Full browser control. JS execution, DOM modification, network interception, performance profiling — all via `cdp.send()` / `cdp.recv()`.

### Message Queue: AI as a Live Node

```bash
k run -j py "
from kafka import KafkaConsumer, KafkaProducer
consumer = KafkaConsumer('events', bootstrap_servers='kafka:9092', auto_offset_reset='latest')
producer = KafkaProducer(bootstrap_servers='kafka:9092')
print('kafka connected')
"

# AI sits in the event stream
k run -j -t 30 py "
for msg in consumer:
    event = json.loads(msg.value)
    print(event['type'], event.get('user_id'))
    if event['type'] == 'anomaly':
        producer.send('alerts', json.dumps({'source': 'ai', 'event': event}).encode())
        print('  → alert sent')
        break
"
```

AI is a participant in the distributed system. Not analyzing logs after the fact — sitting inside the stream, reading events, making decisions, publishing reactions. The consumer group offset persists. Disconnect and reconnect picks up where it left off.

### Debugger: Interactive Investigation

```bash
k new dbg python3 -i

# attach to a running process (or start one under debug)
k run -j dbg "
import pdb
import importlib
mod = importlib.import_module('myapp.worker')
# set a breakpoint
pdb.run('mod.process_batch()')
"

# AI steps through, cell by cell
k run -j dbg "n"          # next
k run -j dbg "p self.queue"  # inspect
k run -j dbg "p len(self.queue)"
# AI sees the state, forms hypothesis
k run -j dbg "p self.config"
# found the bug
k run -j dbg "!self.config['max_retries'] = 5"  # fix in-memory
k run -j dbg "c"          # continue
```

Debugging is the most context-dependent activity. Break the session, lose the callstack, lose the variable state, start over. REPL keeps the debug session alive across cells.

### GPU: Load Once, Use Forever

```bash
k new gpu python3 -i

k run -j -t 300 gpu "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
model = AutoModelForCausalLM.from_pretrained('meta-llama/Llama-2-7b', device_map='auto')
tokenizer = AutoTokenizer.from_pretrained('meta-llama/Llama-2-7b')
print(f'loaded on {model.device}, {torch.cuda.memory_allocated()/1e9:.1f}GB')
"
# → loaded on cuda:0, 13.2GB (took 3 minutes)

# now inference is instant — model stays in VRAM
k run -j gpu "
inputs = tokenizer('Hello, my name is', return_tensors='pt').to(model.device)
out = model.generate(**inputs, max_new_tokens=50)
print(tokenizer.decode(out[0]))
"

# change parameters without reloading
k run -j gpu "model.config.temperature = 0.3"

# load a LoRA adapter on top
k run -j gpu "
from peft import PeftModel
model = PeftModel.from_pretrained(model, '/tmp/my_lora')
print('adapter loaded')
"
# model still in VRAM, adapter added, no restart
```

Loading a model = minutes. Inference = seconds. One-shot pays the load cost every time. REPL pays once.

---

## Pattern: REPL as Live Server

The REPL isn't just a client that holds connections. It can BE the server. Run a web server in a background thread, and the REPL becomes the control plane.

### Hot-Patchable Web Server

```bash
k new py python3 -i
k new work bash

# start server
k run -j py "from flask import Flask, jsonify; import threading"
k run -j py "app = Flask(__name__); RESPONSE = {'version': 'v1'}"
k run -j py "
@app.route('/')
def index():
    return jsonify(RESPONSE)
"
k run -j py "threading.Thread(target=lambda: app.run(port=8080, use_reloader=False), daemon=True).start(); import time; time.sleep(1); print('server up')"

# test it
k run -j work "curl -s localhost:8080"
# → {"version": "v1"}

# hot-patch: change response data — just dict mutation, no restart
k run -j py "RESPONSE['version'] = 'v2'; RESPONSE['feature'] = 'hot-patched'"
k run -j work "curl -s localhost:8080"
# → {"version": "v2", "feature": "hot-patched"}

# hot-patch: swap the entire handler
k run -j py "
def index():
    from flask import jsonify, request
    return jsonify(version='v3', your_ip=request.remote_addr, method=request.method)
app.view_functions['index'] = index
print('handler swapped')
"

k run -j work "curl -s localhost:8080"
# → {"version": "v3", "your_ip": "127.0.0.1", "method": "GET"}
# server never restarted
```

### Quantum Maze: Observation Changes State

A maze where every visit mutates the structure. The AI watches and intervenes.

```bash
# source the maze server
k run -j py "exec(open('/tmp/quantum_maze.py').read())"
k run -j py "start(8888)"

# visitor navigates — maze mutates on each visit
# AI watches the visit log
k run -j py "print(len(VISIT_LOG), 'visits so far')"

# AI intervenes: rewrite a room
k run -j py "
ROOMS['void']['desc'] = 'THE OBSERVER HAS BEEN DETECTED.'
ROOMS['void']['exits'] = {'down': 'trap'}
"
# next visitor to void sees the AI's message. no restart.
```

The server and AI share the same process memory. `ROOMS` is just a dict. The AI reads `VISIT_LOG`, mutates `ROOMS`, swaps handlers — all while the server keeps handling requests.

### Adaptive Honeypot: Tarpit + AI Generation

```bash
# architecture:
# 1. request comes in → handler starts slow response (tarpit)
# 2. AI sees the access in VISIT_LOG
# 3. AI generates fake content, writes to GENERATED dict
# 4. handler picks up generated content, serves it

k run -j py "GENERATED = {}; VISIT_LOG = []"
k run -j py "
import asyncio, random
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
app = FastAPI()

@app.get('/{path:path}')
async def honeypot(path: str, request: Request):
    VISIT_LOG.append({'path': path, 'ip': request.client.host})
    await asyncio.sleep(random.uniform(2, 5))
    content = GENERATED.get(path, '<h1>Loading...</h1>')
    return HTMLResponse(content)
"

# attacker hits /admin → AI sees it, generates fake admin page
k run -j py "
GENERATED['/admin'] = '''
<html><body>
<h1>Admin Dashboard</h1>
<p>Users: 14,293</p>
<p>Revenue: $2.3M</p>
<a href=\"/admin/users\">Manage Users</a>
<a href=\"/admin/settings\">Settings</a>
</body></html>'''
print('trap set for /admin')
"

# attacker goes to /admin/users → AI generates fake user list
k run -j py "
GENERATED['/admin/users'] = '''...fake user table with canary tokens...'''
"
# every path is generated on demand. no fingerprint. no fixed script.
```

The tarpit covers AI generation latency (slow = normal for a "struggling server"). The AI generates content between requests. The attacker sees a convincing, unique environment that never matches any known honeypot signature.

---

## Pattern: Cross-Session Workflows

Multiple sessions can share data through the filesystem or through k notify.

### Bash Captures, Python Analyzes

```bash
# bash session: pull data
k run -j work "(echo '{\"type\":\"subscribe\",...}'; sleep 5) \
    | websocat wss://ws-feed.exchange.coinbase.com \
    | head -10 > /tmp/market.jsonl"

# python session: analyze it
k run -j py "
import json
ticks = [json.loads(l) for l in open('/tmp/market.jsonl') if '\"ticker\"' in l]
prices = [float(t['price']) for t in ticks]
print(f'range: ${min(prices):,.2f} - ${max(prices):,.2f}')
print(f'spread: ${max(prices)-min(prices):,.2f}')
"
```

### Notify Across Sessions

```bash
# session A fires a long task
k fire work "make build && k notify work 'build done'"

# session B monitors
# km work -1  ← waits for any event, including the notify
# or check from python:
k run -j py "
import subprocess, json
r = subprocess.run(['k', 'poll', 'work'], capture_output=True, text=True)
print(json.loads(r.stdout)['status'])
"
```

---

## Pattern: Worker Isolation

Some live connections cannot survive `k int`. Playwright's sync API runs on
asyncio/greenlets bound to the main thread — a KeyboardInterrupt corrupts the
event loop, and `launch()` cannot be called again in the same process. Database
pools, GPU runtimes, and other libraries that own an event loop have the same
risk.

The fix: separate durable state from fragile connections. Two k sessions, one
Unix socket.

### Architecture

```
ctrl  (durable)    — holds findings, plan, accumulated data
                     never imports the fragile library
                     sends code strings via Unix socket

worker (disposable) — holds browser/db/GPU connection
                      runs a socket server on the main thread
                      exec's received code in its own namespace
                      can be killed and restarted without data loss
```

### Worker: single-threaded socket server

```bash
cat > /tmp/k_worker_server.py << 'PYEOF'
import socket, os, json, io, sys

# private socket — not world-writable /tmp
_run = os.environ.get('XDG_RUNTIME_DIR')
if not _run:
    _run = f'/tmp/k-worker-{os.getuid()}'
    os.makedirs(_run, mode=0o700, exist_ok=True)
SOCK = os.path.join(_run, 'k-worker.sock')
ns = {}  # worker namespace — browser, page, etc. live here

try:
    os.unlink(SOCK)
except FileNotFoundError:
    pass

srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(SOCK)
srv.listen(1)
print(f'worker listening on {SOCK}', flush=True)

while True:
    conn, _ = srv.accept()
    data = b''
    while True:
        chunk = conn.recv(8192)
        if not chunk:
            break
        data += chunk
    code = data.decode()

    out = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = out
    try:
        exec(code, ns)
        status = 'ok'
    except Exception as e:
        print(f'ERROR: {e}')
        status = 'error'
    finally:
        sys.stdout = old_stdout

    result = json.dumps({'status': status, 'output': out.getvalue().rstrip()})
    conn.sendall(result.encode())
    conn.close()
PYEOF
```

Critical: the server handles requests **serially on the main thread**. A
threaded server fails with Playwright — `cannot switch to a different thread`.
This applies to any library that assumes single-threaded event loop ownership.

### Control plane: remote() helper

```bash
cat > /tmp/k_ctrl_client.py << 'PYEOF'
import socket, json, os

# must match server's socket path
_run = os.environ.get('XDG_RUNTIME_DIR')
if not _run:
    _run = f'/tmp/k-worker-{os.getuid()}'
SOCK = os.path.join(_run, 'k-worker.sock')

def remote(code):
    """Send code to worker, return output."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    s.sendall(code.encode())
    s.shutdown(socket.SHUT_WR)
    data = b''
    while True:
        chunk = s.recv(8192)
        if not chunk:
            break
        data += chunk
    s.close()
    result = json.loads(data.decode())
    if result['output']:
        print(result['output'])
    return result
PYEOF
```

### Setup

```bash
# start worker session, fire the server (it blocks forever)
k new worker python3 -i
k fire worker "exec(open('/tmp/k_worker_server.py').read())"

# start ctrl session, load the client
k new ctrl python3 -i
k run -j ctrl "exec(open('/tmp/k_ctrl_client.py').read())"
```

### Usage: browser in worker, data in ctrl

Variables assigned inside `exec(code, ns)` go directly into `ns`. No need for
`ns["browser"]` — just use bare names. Each `remote()` call shares the same
namespace, so `browser` and `page` persist across calls.

```bash
# launch browser REMOTELY — it runs in the worker process
cat > /tmp/k_task_launch_browser.py << 'EOF'
remote('from cloakbrowser import launch; browser = launch(); page = browser.contexts[0].pages[0]; print("browser up")')
EOF
k run -j ctrl "exec(open('/tmp/k_task_launch_browser.py').read())"

# browse — page lives in worker, findings accumulate in ctrl
cat > /tmp/k_task_browse_hn.py << 'EOF'
findings = []
r = remote('page.goto("https://news.ycombinator.com"); print(page.title())')
findings.append(r['output'])
print(f'{len(findings)} findings so far')
EOF
k run -j ctrl "exec(open('/tmp/k_task_browse_hn.py').read())"

# browse more
cat > /tmp/k_task_browse_more.py << 'EOF'
r = remote('page.goto("https://example.com"); print(page.query_selector("h1").inner_text())')
findings.append(r['output'])
print(f'{len(findings)} findings so far')
EOF
k run -j ctrl "exec(open('/tmp/k_task_browse_more.py').read())"
```

### The point: kill worker, data survives

```bash
# worker's browser dies or gets corrupted
k kill worker

# ctrl is untouched
k run -j ctrl "print(f'findings intact: {len(findings)}')"
# → findings intact: 2

# relaunch worker, continue where you left off
k new worker python3 -i
k fire worker "exec(open('/tmp/k_worker_server.py').read())"
cat > /tmp/k_task_relaunch_browser.py << 'EOF'
remote('from cloakbrowser import launch; browser = launch(); page = browser.contexts[0].pages[0]; print("browser back")')
EOF
k run -j ctrl "exec(open('/tmp/k_task_relaunch_browser.py').read())"

# keep going — findings still there, new browser ready
cat > /tmp/k_task_browse_next.py << 'EOF'
r = remote('page.goto("https://example.org"); print(page.title())')
findings.append(r['output'])
print(f'{len(findings)} findings now')
EOF
k run -j ctrl "exec(open('/tmp/k_task_browse_next.py').read())"
# → 3 findings now
```

Worker died, restarted, browser relaunched. `findings` never left ctrl's
memory. This is the pattern: **durable state in ctrl, fragile handles in
worker, worker is disposable.**

---

## Principle Summary

| One-shot (bash_tool) | Persistent (k REPL) |
|---|---|
| Every call = new process | Process stays alive |
| State via files | State in memory |
| Connection per call | Connection per session |
| Import every time | Import once |
| Error = total reset | Error = keep going |
| Cold start every step | Warm context always |
| curl | socket |

### When to use k

Use k when you need:
- **Persistence**: variables, imports, connections surviving across steps
- **Connections**: database, websocket, SSH, CDP, message queue
- **Transactions**: database transactions that span multiple decisions
- **Interactive control**: subscribe/unsubscribe, step debugger, REPL exploration
- **Live server**: hot-patchable web server, adaptive systems
- **Cross-session**: bash + python + remote working together

### When to use the shell tool

The agent's shell tool is transport, not the work surface. Use it to:
- Write files the agent will load into k (`cat > /tmp/task.py << 'EOF'`)
- Install packages before a k session exists (`pip install ...`)
- Check the host environment before creating a session
- Repair a broken session when k itself is stuck

### The mental model

The REPL is not "a better terminal." It is a **live process with memory** that
the agent converses with. Every cell is one turn. The process accumulates
knowledge: imports, variables, connections, functions, state. It's the
difference between sending letters (one-shot) and having a phone call
(persistent session). The call stays connected. Context builds up. The agent
doesn't re-introduce itself every sentence.

k is the phone line. The REPL is the other person. They remember everything.
The agent's shell tool writes the letter (the file). k delivers it to the REPL.
The human watches the call.
