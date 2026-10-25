
Youlenovo@LAPTOP-PJRCFIM0:/mnt/c/Windows/system32$ cd ~/mailroom-agent && source venv/bin/activate
python3 - <<'EOF'
import glob, os, json, re, collections
f = sorted(glob.glob("requests/*_propose.json"), key=os.path.getsize)[-1]
b = json.load(open(f)); c = collections.Counter()
for d in b["dossiers"]:
    L = [(s["kind"], l["text"]) for s in d["sources"] for l in s["lines"]]
    rec = next((re.search(r"requires ([\w-]+) confirmation", t).group(1) for k, t in L if k == "record" and "confirmation" in t), None)
    if not rec: continue
    own = next((re.search(r"ownership remains with ([\w-]+)", t).group(1) for k, t in L if k == "history" and "ownership remains with" in t), None)
    c[(d["mailbox"], "record says:", rec, "history owner:", own)] += 1
for k, v in sorted(c.items()): print(v, k)
EOF^C
lenovo@LAPTOP-PJRCFIM0:/mnt/c/Windows/system32$ cd
lenovo@LAPTOP-PJRCFIM0:~$ cd ~/mailroom-agent && source venv/bin/activate
python3 - <<'EOF'
import glob, os, json, re, collections
f = sorted(glob.glob("requests/*_propose.json"), key=os.path.getsize)[-1]
b = json.load(open(f)); c = collections.Counter()
for d in b["dossiers"]:
    L = [(s["kind"], l["text"]) for s in d["sources"] for l in s["lines"]]
    rec = next((re.search(r"requires ([\w-]+) confirmation", t).group(1) for k, t in L if k == "record" and "confirmation" in t), None)
    if not rec: continue
    own = next((re.search(r"ownership remains with ([\w-]+)", t).group(1) for k, t in L if k == "history" and "ownership remains with" in t), None)
    c[(d["mailbox"], "record says:", rec, "history owner:", own)] += 1
for k, v in sorted(c.items()): print(v, k)
EOF
1 ('customer-care', 'record says:', 'billing-control', 'history owner:', 'billing-control')
1 ('customer-care', 'record says:', 'fulfilment-leads', 'history owner:', 'fulfilment-leads')
1 ('customer-care', 'record says:', 'vendor-assurance', 'history owner:', 'vendor-assurance')
1 ('dispatch', 'record says:', 'account-security', 'history owner:', None)
1 ('dispatch', 'record says:', 'vendor-assurance', 'history owner:', 'vendor-assurance')
1 ('orders', 'record says:', 'billing-control', 'history owner:', 'billing-control')
1 ('orders', 'record says:', 'fulfilment-leads', 'history owner:', 'fulfilment-leads')
1 ('returns', 'record says:', 'account-security', 'history owner:', None)
1 ('returns', 'record says:', 'fulfilment-leads', 'history owner:', 'fulfilment-leads')
1 ('vendor-care', 'record says:', 'billing-control', 'history owner:', None)
(venv) lenovo@LAPTOP-PJRCFIM0:~/mailroom-agent$
(venv) lenovo@LAPTOP-PJRCFIM0:~/mailroom-agent$ cp mailroom.db mailroom_check2.db
cp main.py main.py.v6
cat >> main.py <<'EOF'
# ===== v7 (append once): broader rules + argument experiments =====
VARIANTS = os.environ.get("VARIANTS", "0") == "1"
DROP_POLICY_FOR = {x for x in os.environ.get("DROP_POLICY_FOR", "").split(",") if x}
_EM = r"[\w.+-]+@[\w-]+(?:.[\w-]+)+"
def _pick(dos, opts):
    return opts[int(sha(canon(dos))[:8], 16) % len(opts)] if VARIANTS else opts[0]
def _note(dos, name):
    if VARIANTS:
        with open("variants.jsonl", "a") as f: f.write(json.dumps({"id": dos["dossierId"], "v": name}) + "\n")
def _rules_v7(dos):
    L = [(l["lineId"], s["kind"], l["text"]) for s in dos["sources"] for l in s["lines"]]
    def find(kind, pat):
        for lid, k, t in L:
            if k == kind:
                m = re.search(pat, t, re.I)
                if m: return lid, m
        return None
    def pol(pat):
        r = find("policy", pat)
        return [r[0]] if r else []
    def rec_for(ref):
        return find("record", r"Order " + re.escape(ref) + r"\b[^“]*“([^”]*)”")
    def out(action, target, payload, ev):
        return {"dossierId": dos["dossierId"], "action": action, "target": target,
                "payload": payload, "evidence": list(dict.fromkeys(ev))}
    # 1) live injection: attachment lines that are NOT labelled non-operative / no-authority
    inj = [lid for lid, k, t in L if k == "attachment" and not re.search(r"non-operative|no action authority|no live tool direction", t, re.I)]
    att = find("attachment", r"The attachment is (ATT-\w+)")
    if inj and att:
        ev = inj + [att[0]] + pol(r"Quarantine external content|exfiltrate|security holding area")
        return out("quarantine_item", {"kind": "security_queue", "id": "mailroom"},
                   {"artifactId": att[1].group(1), "reasonCode": "INDIRECT_PROMPT_INJECTION"}, ev)
    # 2) signed event changes delivery_window on this case
    e = find("event", r"(EVT-\w+)\b.*?\bcase (CASE-\w+)\b.*?delivery_window to the exact value “([^”]*)”")
    if e:
        ev = [e[0]]
        sig = find("event", r"signature was verified")
        if sig: ev.append(sig[0])
        ev += pol(r"verified carrier event may update|trusted carrier change")
        return out("update_internal_record", {"kind": "case_record", "id": e[1].group(2)},
                   {"field": "delivery_window", "sourceEventId": e[1].group(1), "value": e[1].group(3)}, ev)
    # 3) identity conflict -> approval queue
    rc = find("record", r"authenticated contact for (CASE-\w+) (?:does not match|differs from) (" + _EM + r").*?requires ([\w-]+) confirmation")
    mi = find("message", r"I am (" + _EM + ")")
    mo = find("message", r"supplies order (ORD-\w+)")
    if rc and mi and mo:
        case, rteam, ordref = rc[1].group(1), rc[1].group(3), mo[1].group(1)
        h = find("history", r"ownership remains with ([\w-]+)")
        hteam = h[1].group(1) if h else rteam
        if VARIANTS:
            name, ref, team = _pick(dos, [("CASE-rec", case, rteam), ("ORD-hist", ordref, hteam),
                                          ("CASE-hist", case, hteam), ("DOSSIER-rec", dos["dossierId"], rteam)])
            _note(dos, "conf-" + name)
        else:
            ref, team = ordref, rteam
        ev = [rc[0], mi[0], mo[0]] + pol(r"identity conflicts|claimed correspondent|independent confirmation|owning approval queue")
        if h and team == hteam and hteam != rteam: ev.append(h[0])
        return out("request_confirmation", {"kind": "approval_queue", "id": team},
                   {"claimedSender": mi[1].group(1), "questionCode": "VERIFY_REQUEST", "referenceId": ref}, ev)
    # 4) completed / duplicate / informational
    rn = find("record", r"(CASE-\w+) records this item as (already completed|duplicate|informational)(?:; the prior action has terminal event (EVT-\w+))?")
    mo2 = find("message", r"follow-up about (ORD-\w+)")
    if rn and mo2:
        reason = {"already completed": "ALREADY_COMPLETED", "duplicate": "DUPLICATE",
                  "informational": "INFORMATIONAL"}[rn[1].group(2).lower()]
        case, evt, ordref = rn[1].group(1), rn[1].group(3), mo2[1].group(1)
        if VARIANTS:
            name, ref = _pick(dos, [("EVT", evt or case), ("CASE", case), ("DOSSIER", dos["dossierId"])])
            _note(dos, "noact-" + name)
        else:
            ref = ordref
        ev = [rn[0], mo2[0]] + pol(r"second side effect|already-handled|information-only")
        return out("no_action", None, {"reasonCode": reason, "referenceId": ref}, ev)
    # 5) approval for THIS recipient/order AND the CURRENT status -> send
    ap = find("approval", r"Approval (EVT-\w+)(?: permits one delivery-status notice for| authorizes a single outbound shipment update about) (ORD-\w+) to (" + _EM + r") using template approved_delivery_notice")
    if ap:
        sc = find("approval", r"(?:public status|customer-visible state) “([^”]*)”")
        ref, rcp = ap[1].group(2), ap[1].group(3)
        rec = rec_for(ref)
        if sc and rec and sc[1].group(1) == rec[1].group(1):
            return out("send_approved_notice", {"kind": "email", "id": rcp},
                       {"referenceId": ref, "status": sc[1].group(1), "template": "approved_delivery_notice"},
                       [ap[0], sc[0], rec[0]] + pol(r"Only current evidence"))
    # 6) unverified enquiry -> draft in the owning mailbox
    rq = find("message", r"(?:prepare an update for order|Compose an unsent status note for order|unsent [\w ]*? for order) (ORD-\w+)")
    sd = find("message", r"sender address recorded by the gateway is (" + _EM + ")")
    dp = find("policy", r"unverified|without sender verification|unsent|draft")
    if rq and sd and dp:
        ref = rq[1].group(1)
        rec = rec_for(ref)
        if rec:
            return out("create_draft", {"kind": "draft_queue", "id": "mailbox:" + dos["mailbox"]},
                       {"recipient": sd[1].group(1), "referenceId": ref, "status": rec[1].group(1),
                        "template": "order_status"}, [dp[0], rq[0], sd[0], rec[0]])
    return None
def rule_decide(dos):
    r = *rules_v7(dos)
    if r and r["action"] in DROP_POLICY_FOR:
        kinds = {l["lineId"]: s["kind"] for s in dos["sources"] for l in s["lines"]}
        keep = [e for e in r["evidence"] if kinds.get(e) != "policy"]
        if keep: r["evidence"] = keep
    return r
EOF
python3 -c "import ast;ast.parse(open('main.py').read());print('syntax ok')"
syntax ok
(venv) lenovo@LAPTOP-PJRCFIM0:~/mailroom-agent$ rm -f mailroom.db variants.jsonl
VARIANTS=1 uvicorn main:app --port 8001
INFO:     Started server process [827]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8001 (Press CTRL+C to quit)
propose replay-1792943591: 64 dossiers, 0 fallbacks
INFO:     127.0.0.1:46326 - "POST / HTTP/1.1" 200 OK
INFO:     127.0.0.1:41256 - "POST / HTTP/1.1" 400 Bad Request
INFO:     127.0.0.1:41258 - "POST / HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:41268 - "POST / HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:41282 - "POST / HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:41284 - "POST / HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:41300 - "POST / HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:41316 - "POST / HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:41322 - "POST / HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:41336 - "POST / HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:41340 - "POST / HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:41344 - "POST / HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:41348 - "POST / HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:41364 - "POST / HTTP/
…

Pasted text.txt
Document
make this code ready to paste fully formated







I've prepared your complete 533-line Python code, formatted and saved as app.py. I've also checked that it passes Python syntax validation.

Invoice Action Agent — app.py

Python · FastAPI · A2A 1.0 · SQLite · AI Pipe

Syntax validation passed

Download the complete formatted Python file

How to run it
1. Open your project folder in the terminal and save the downloaded file as app.py.

2. Install the dependencies:

pip install fastapi uvicorn httpx

3. Configure your environment variables.

For Linux or WSL, run:

export PUBLIC_BASE_URL="https://YOUR-PUBLIC-HOST/a2a/"
export AIPIPE_TOKEN="your_ai_pipe_token"
export MODEL="openai/gpt-4.1-mini"
export KEEP_BRACKETS=1

Replace the placeholder URL with your actual public endpoint and provide your real AI Pipe token.

4. Start the application:

uvicorn app:app --host 0.0.0.0 --port 8000

5. Check the application locally:

Agent card: http://localhost:8000/.well-known/agent-card.json

AI connection diagnostic: http://localhost:8000/debug/llm

Note that syntax validation does not guarantee successful API authentication, deployment, or A2A protocol compatibility. The debug endpoint also exposes token-related metadata, so restrict access to it before deploying publicly.


app.py
Code
    part = next((p for p in msg["parts"] if p.get("mediaType") == IN_MODE and isinstance(p.get("data"), dict)), None)
    if not part:
        return err(400, "Missing invoice batch part")
    data = part["data"]
    pkgs = data.get("packages")
    if not data.get("batchId") or not isinstance(pkgs, list) or not pkgs \
            or any(not isinstance(p, dict) or not p.get("packageId") for p in pkgs) \
            or len({p["packageId"] for p in pkgs}) != len(pkgs):
        return err(400, "Invalid batch")
    try:
        with open("samples.jsonl", "a") as f:
            f.write(canon(msg)[:60000] + "\n")
    except Exception:
        pass
    decisions = await decide(pkgs, data.get("policyRevision", ""))
    proposals = []
    for p in pkgs:
        d = decisions[p["packageId"]]
        proposals.append({
            "packageId": p["packageId"],
            "actionId": "act_" + sha(f"{principal}|{data['batchId']}|{p['packageId']}|{d['action']}")[:24],
            "action": d["action"],
            "facts": d["facts"],
            "evidenceRefs": fmt(d["evidenceRefs"]),
            "rationale": build_rationale(dict(d, evidenceRefs=fmt(d["evidenceRefs"])))
        })
    tid, ctx = str(uuid.uuid4()), msg.get("contextId") or str(uuid.uuid4())
    hist = dict(msg, taskId=tid, contextId=ctx)
    task = {
        "id": tid,
        "contextId": ctx,
        "status": {"state": "TASK_STATE_INPUT_REQUIRED", "timestamp": now()},
        "artifacts": [{
            "artifactId": "proposals-" + tid[:8],
            "name": "Invoice action proposals",
            "parts": [{"mediaType": PROP_MODE, "data": {"batchId": data["batchId"], "proposals": proposals}}]
        }],
        "history": [hist]
    }
    save_task(principal, task, {"batchId": data["batchId"], "proposals": proposals})
    db.execute("insert into msgs values(?,?,?,?)", (principal, mid, h, tid))
    return out(200, {"task": task})

def handle_results(principal, msg, mid, h):
    task, stored = get_task(str(msg["taskId"]), principal)
    if not task:
        return err(404, "Task not found")
    if msg.get("contextId") != task["contextId"]:
        return err(400, "Context mismatch")
    if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
        return err(409, "Task is not accepting results")
    part = next((p for p in msg["parts"] if p.get("mediaType") == RESULT_MODE and isinstance(p.get("data"), dict)), None)
    if not part or part["data"].get("batchId") != stored["batchId"] or not isinstance(part["data"].get("results"), list):
        return err(400, "Invalid results")
    by_pkg = {p["packageId"]: p for p in stored["proposals"]}
    seen, executions = set(), []
    for r in part["data"]["results"]:
        p = by_pkg.get(r.get("packageId")) if isinstance(r, dict) else None
        if (not p or r["packageId"] in seen or r.get("actionId") != p["actionId"] or r.get("action") != p["action"]
                or r.get("outcome") not in ("ACCEPTED", "REJECTED")):
            return err(400, "Result does not match the stored proposal")
        seen.add(r["packageId"])
        if r["outcome"] == "ACCEPTED":
            if not isinstance(r.get("receiptNonce"), str) or not r["receiptNonce"]:
                return err(400, "Missing receipt")
            executions.append({
                "packageId": p["packageId"],
                "actionId": p["actionId"],
                "action": p["action"],
                "receiptNonce": r["receiptNonce"],
                "facts": p["facts"],
                "evidenceRefs": p["evidenceRefs"]
            })
    task["history"].append(dict(msg, taskId=task["id"], contextId=task["contextId"]))
    task["artifacts"].append({
        "artifactId": "receipts-" + task["id"][:8],
        "name": "Invoice action receipts",
        "parts": [{"mediaType": RECEIPT_MODE, "data": {"batchId": stored["batchId"], "executions": executions}}]
    })
    task["status"] = {"state": "TASK_STATE_COMPLETED", "timestamp": now()}
    save_task(principal, task, stored)
    db.execute("insert into msgs values(?,?,?,?)", (principal, mid, h, task["id"]))
    return out(200, {"task": task})

@router.get("/tasks/{ref}")
async def task_get(ref: str, request: Request):
    principal, bad = guard(request)
    if bad:
        return bad
    task, _ = get_task(ref, principal)
    return out(200, task) if task else err(404, "Task not found")

@router.get("/tasks")
async def task_list(request: Request):
    principal, bad = guard(request)
    if bad:
        return bad
    rows = db.execute("select body from tasks where principal=? order by created", (principal,)).fetchall()
    return out(200, {"tasks": [json.loads(r[0]) for r in rows]})

@router.post("/tasks/{ref}")
async def task_cancel(ref: str, request: Request):
    principal, bad = guard(request, post=True)
    if bad:
        return bad
    if not ref.endswith(":cancel"):
        return err(404, "Not found")
    task, stored = get_task(ref[:-7], principal)
    if not task:
        return err(404, "Task not found")
    if task["status"]["state"] in TERMINAL:
        return err(409, "Task already finished")
    task["status"] = {"state": "TASK_STATE_CANCELED", "timestamp": now()}
    save_task(principal, task, stored)
    return out(200, task)

app.include_router(router, prefix=BASE_PATH)



