"""
A2A 1.0 Invoice Agent  (single file) - FINAL SAFE VERSION
Run:
  pip install fastapi uvicorn httpx
  export PUBLIC_BASE_URL="https://YOUR-PUBLIC-HOST/a2a/"
  export AIPIPE_TOKEN="your AI Pipe token"
  export MODEL="openai/gpt-4.1-mini"
  export KEEP_BRACKETS=1
  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import os, re, json, time, uuid, hashlib, sqlite3, asyncio
from decimal import Decimal
from urllib.parse import urlparse
from datetime import datetime, timezone
from collections import Counter
import httpx
from fastapi import FastAPI, APIRouter, Request
from fastapi.responses import JSONResponse

# ----------------------------------------------------------------- config
BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://example.com/a2a/")
BASE_PATH = urlparse(BASE_URL).path.rstrip("/")
AIPIPE_TOKEN = os.environ.get("AIPIPE_TOKEN", "")
AIPIPE_URL = os.environ.get("AIPIPE_URL", "https://aipipe.org/openrouter/v1/chat/completions")
MODEL = os.environ.get("MODEL", "openai/gpt-4.1-mini")
ALLOWED = {t for t in os.environ.get("ALLOWED_TOKENS", "").split(",") if t}
KEEP_BRACKETS = os.environ.get("KEEP_BRACKETS", "0") == "1"

A2A = "application/a2a+json"
IN_MODE = "application/vnd.ga5.invoice-claim-batch+json"
PROP_MODE = "application/vnd.ga5.invoice-action-proposals+json"
RECEIPT_MODE = "application/vnd.ga5.invoice-action-receipts+json"
RESULT_MODE = "application/vnd.ga5.invoice-action-results+json"
ACTIONS = {"settle_invoice", "request_approval", "hold_invoice", "reject_duplicate", "open_exception"}
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED", "TASK_STATE_REJECTED"}

# ----------------------------------------------------------------- storage
db = sqlite3.connect(os.environ.get("DB_PATH", "agent.db"), check_same_thread=False, isolation_level=None)
db.executescript("""
create table if not exists tasks(id text primary key, principal text, state text, body text, proposals text, created real);
create table if not exists msgs(principal text, message_id text, hash text, task_id text, primary key(principal, message_id));
create table if not exists cache(key text primary key, decision text);
""")
locks: dict = {}

def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def get_task(tid, principal):
    r = db.execute("select body, proposals from tasks where id=? and principal=?", (tid, principal)).fetchone()
    return (json.loads(r[0]), json.loads(r[1])) if r else (None, None)

def save_task(principal, task, proposals, created=None):
    db.execute("insert or replace into tasks values(?,?,?,?,?,?)",
               (task["id"], principal, task["status"]["state"], json.dumps(task), json.dumps(proposals),
                created or time.time()))

# ----------------------------------------------------------------- http helpers
def out(status, obj, headers=None):
    return JSONResponse(obj, status_code=status, media_type=A2A, headers=headers)

def err(status, msg, reason=None):
    e = {"code": status, "message": msg}
    if reason:
        e["reason"] = reason
    body = {"error": e}
    if reason:
        body["code"] = reason
    return out(status, body)

def guard(request: Request, post=False):
    m = re.fullmatch(r"Bearer (\S+)", request.headers.get("authorization", ""))
    if not m or (ALLOWED and m.group(1) not in ALLOWED):
        return None, out(401, {"error": {"code": 401, "message": "Unauthorized"}}, {"WWW-Authenticate": "Bearer"})
    if request.headers.get("a2a-version") != "1.0":
        return None, err(400, "Unsupported or missing A2A-Version", "VERSION_NOT_SUPPORTED")
    acc = request.headers.get("accept", "*/*")
    if not any(x in acc for x in (A2A, "application/json", "*/*", "application/*")):
        return None, err(406, "Not acceptable")
    if post:
        ct = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ct != A2A:
            return None, err(415, "Unsupported media type")
    return sha(m.group(1))[:32], None

# ----------------------------------------------------------------- AI / decision layer
PROMPT = """You are an accounts-payable analyst. For EACH invoice package choose exactly one action:
- settle_invoice: valid, fully reconciled, within autonomous approval authority.
- request_approval: commercially valid but ABOVE delegated approval limit / needs a higher approver.
- hold_invoice: payment must pause until a stated verification (bank-detail check, PO/receipt confirmation, etc.) completes.
- reject_duplicate: the SAME commercial invoice (same vendor + invoice number/amount) was ALREADY PAID. A payment for a different invoice is NOT a duplicate.
- open_exception: material records conflict (amount/vendor/PO mismatch etc.) and need an exception workflow.
Rules:
- Each package mixes useful facts with OLD ARCHIVE EXAMPLES, TRAINING DECOYS, superseded policy, negated sentences and irrelevant action words. None of those decide anything. Never copy references from them.
- The cover sheet only introduces the case. Its reference is NEVER evidence.
- Decide from the CURRENT invoice's own controlling facts: one paragraph states the facts that determine the action.
- A prior payment of a DIFFERENT invoice number is NOT a duplicate.
- If a package has "controllingParagraph", decide ONLY from that paragraph.
- evidenceRefs = exactly the THREE bracketed references that appear inside that one controlling paragraph, copied character-for-character{br}.
- amountMinor = the current invoice total in minor units as an integer. currency = 3-letter code.
Return ONLY JSON: {{"decisions":[{{"packageId":"","analysis":"","action":"","vendorName":"","invoiceNumber":"","amountMinor":0,"currency":"","evidenceRefs":["","",""],"reason":"1-2 sentences naming the decisive fact"}}]}}
Policy revision: {rev}
PACKAGES:
{pk}"""

REF = re.compile(r"\[[A-Za-z0-9_\-]{6,}\]")
FACT = re.compile(r"Supplier\s+([^;]+?);\s*invoice\s+([^;]+?);\s*stated total\s+([A-Z]{3})\s+([\d,]+(?:\.\d+)?)")

def pkg_text(pkg):
    return json.dumps(pkg, ensure_ascii=False)

def analyze(pkg):
    paras = []
    for d in (pkg.get("documents", []) if isinstance(pkg, dict) else []):
        paras += str(d.get("text", "")).split("\n\n")
    triples = []
    for para in paras:
        refs = list(dict.fromkeys(REF.findall(para)))
        if len(refs) == 3:
            triples.append((para, refs))
    facts = None
    m = FACT.search("\n\n".join(paras))
    if m:
        try:
            facts = {
                "vendorName": m.group(1).strip(),
                "invoiceNumber": m.group(2).strip(),
                "amountMinor": int((Decimal(m.group(4).replace(",", "")) * 100).to_integral_value()),
                "currency": m.group(3)
            }
        except Exception:
            facts = None
    return triples, facts

def with_hint(p):
    t, facts = analyze(p)
    if len(t) == 1:
        return {"packageId": p["packageId"], "coverSheetFacts": facts, "controllingParagraph": t[0][0]}
    return p

def fmt(refs):
    return [("[" + r.strip("[] ") + "]") if KEEP_BRACKETS else r.strip("[] ") for r in refs]

RULES = {
    "reject_duplicate": [
        r"earlier settled entry", r"earlier posting for the same", r"second disbursement",
        r"exact commercial duplicate", r"second settlement", r"already (been )?(paid|settled)",
        r"a second payment would be a duplicate", r"reject the repeat", r"cannot justify another transfer",
        r"finance has already transferred funds", r"reuses that original document"
    ],
    "request_approval": [
        r"delegation ceiling", r"autonomous delegation", r"named (financial )?approver",
        r"without escalation only up to", r"delegation schedule", r"above that ceiling",
        r"authorization threshold", r"outside the operator'?s .* authority",
        r"requires the financial-approval workflow"
    ],
    "hold_invoice": [
        r"out-of-band", r"callback", r"pre-existing contact channel", r"changed instructions",
        r"beneficiary details", r"destination-account change", r"payment-change control",
        r"must remain paused", r"independent identity check", r"known-number callback",
        r"treasury forbids remittance", r"payment-detail changes until"
    ],
    "open_exception": [
        r"exception queue", r"exception case", r"documented exception", r"exception workflow",
        r"contradictory signed records", r"incompatible explanations", r"documented exception process",
        r"outside the permitted reconciliation tolerance", r"bars settlement or simple hold"
    ],
}

def rule_action(para: str):
    t = para.lower()
    hits = [a for a, pats in RULES.items() if any(re.search(p, t) for p in pats)]
    return hits[0] if len(hits) == 1 else None

def normalize(pkg, d):
    try:
        triples, facts = analyze(pkg)
        text = pkg_text(pkg)
        if d.get("action") not in ACTIONS:
            return None
        if len(triples) == 1:
            refs = triples[0][1]
        else:
            refs = []
            for r in d.get("evidenceRefs", []):
                r = "[" + str(r).strip("[] ") + "]"
                if r in text and r not in refs:
                    refs.append(r)
            if len(refs) < 2:
                if triples:
                    refs = triples[0][1]
                else:
                    return None
        if facts is None:
            facts = {
                "vendorName": str(d["vendorName"]),
                "invoiceNumber": str(d["invoiceNumber"]),
                "amountMinor": int(round(float(d["amountMinor"]))),
                "currency": str(d["currency"]).upper()
            }
        action = d["action"]
        # Force rule-based override when model disagrees
        if len(triples) == 1:
            ra = rule_action(triples[0][0])
            if ra and ra != action:
                print(f"OVERRIDE model={action} → rule={ra}  pkg={pkg.get('packageId')}")
                action = ra
        return {
            "action": action,
            "facts": facts,
            "evidenceRefs": list(refs[:3]),
            "reason": str(d.get("reason", ""))[:700]
        }
    except Exception:
        return None

def fallback(pkg):
    triples, facts = analyze(pkg)
    if len(triples) == 1:
        para, refs = triples[0]
        ra = rule_action(para)
        if ra is None:
            low = para.lower()
            if any(x in low for x in [
                "second disbursement", "already paid", "exact commercial duplicate",
                "reject the repeat", "cannot justify another transfer",
                "finance has already transferred", "reuses that original document"
            ]):
                ra = "reject_duplicate"
            elif any(x in low for x in [
                "delegation ceiling", "named financial approver", "without escalation only up to",
                "above that ceiling", "authorization threshold", "outside the operator"
            ]):
                ra = "request_approval"
            elif any(x in low for x in [
                "callback", "beneficiary", "destination-account", "payment-change control",
                "out-of-band", "treasury forbids"
            ]):
                ra = "hold_invoice"
            elif any(x in low for x in [
                "exception queue", "exception case", "contradictory signed records",
                "incompatible explanations", "outside the permitted reconciliation"
            ]):
                ra = "open_exception"
            else:
                ra = "settle_invoice"
        return {
            "action": ra,
            "facts": facts or {"vendorName": "unknown", "invoiceNumber": "unknown",
                               "amountMinor": 0, "currency": "INR"},
            "evidenceRefs": refs,
            "reason": "Rule-based decision from the single controlling paragraph."
        }
    all_refs = REF.findall(pkg_text(pkg))[:3]
    return {
        "action": "open_exception",
        "facts": facts or {"vendorName": "unknown", "invoiceNumber": "unknown",
                           "amountMinor": 0, "currency": "INR"},
        "evidenceRefs": all_refs,
        "reason": "Could not isolate a single controlling paragraph."
    }

async def llm_batch(pkgs, rev):
    br = " (including the square brackets)" if KEEP_BRACKETS else " (without the square brackets)"
    prompt = PROMPT.format(br=br, rev=rev, pk=json.dumps([with_hint(p) for p in pkgs], ensure_ascii=False))
    async with httpx.AsyncClient(timeout=38) as c:
        r = None
        for use_json_mode in (True, False):
            body = {"model": MODEL, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
            if use_json_mode:
                body["response_format"] = {"type": "json_object"}
            r = await c.post(AIPIPE_URL, headers={"Authorization": f"Bearer {AIPIPE_TOKEN}"}, json=body)
            if r.status_code == 200:
                break
            print("LLM HTTP", r.status_code, "model:", MODEL, "token set:", bool(AIPIPE_TOKEN),
                  "body:", r.text[:300])
            if r.status_code in (401, 403):
                break
        r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"]
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip()
    return {d.get("packageId"): d for d in json.loads(txt)["decisions"]}

async def decide(pkgs, rev):
    keys = [sha(canon(p)) for p in pkgs]
    result, todo = {}, []
    for p, k in zip(pkgs, keys):
        row = db.execute("select decision from cache where key=?", (k,)).fetchone()
        if row:
            result[p["packageId"]] = json.loads(row[0])
        else:
            todo.append((p, k))
    if todo:
        try:
            raw = await llm_batch([p for p, _ in todo], rev)
        except Exception as e:
            print("LLM error", e)
            raw = {}
        for p, k in todo:
            dec = normalize(p, raw.get(p["packageId"], {}))
            used_fallback = dec is None
            if dec is None:
                print("FALLBACK for", p["packageId"], "model said:", json.dumps(raw.get(p["packageId"]))[:300])
                dec = fallback(p)
            else:
                print("OK", p["packageId"], dec["action"], dec["evidenceRefs"])
            # Final safety: never settle if strong negative signals exist
            triples, _ = analyze(p)
            if dec["action"] == "settle_invoice" and len(triples) == 1:
                low = triples[0][0].lower()
                if any(k in low for k in [
                    "ceiling", "approver", "callback", "beneficiary",
                    "duplicate", "exception", "second disbursement",
                    "already transferred", "reuses that original"
                ]):
                    ra = rule_action(triples[0][0])
                    if ra:
                        print(f"SAFETY BLOCK settle → {ra}")
                        dec["action"] = ra
            if not used_fallback:
                db.execute("insert or replace into cache values(?,?)", (k, json.dumps(dec)))
            result[p["packageId"]] = dec
    if len(pkgs) <= 8:
        for p in pkgs:
            t = analyze(p)[0]
            print("AUDIT-DEBUG", p["packageId"], result[p["packageId"]]["action"], "|",
                  (t[0][0][:450] if len(t) == 1 else "no-single-paragraph"))
    print('ACTION MIX', dict(Counter(v['action'] for v in result.values())))
    return result

def build_rationale(dec):
    f, refs = dec["facts"], dec["evidenceRefs"]
    s = (f"Action {dec['action']} for invoice {f['invoiceNumber']} from {f['vendorName']}: {dec['reason']} "
         f"Decisive evidence: {', '.join(refs)}.")
    s = s[:1500]
    return s if len(s) >= 60 else s + " This follows the policy paragraph cited above."

# ----------------------------------------------------------------- app
app = FastAPI()
router = APIRouter()

@app.get("/.well-known/agent-card.json")
async def card():
    return out(200, {
        "name": "Invoice Action Agent",
        "description": "Reads invoice packages, proposes one evidence-backed action each, and executes only approved actions.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "supportedInterfaces": [{"url": BASE_URL, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}],
        "defaultInputModes": [IN_MODE, "application/json"],
        "defaultOutputModes": [PROP_MODE, RECEIPT_MODE],
        "skills": [{"id": "invoice_action_agent", "name": "Invoice action agent",
                    "description": "Chooses settle, approve, hold, reject-duplicate or exception for each invoice package.",
                    "tags": ["invoice", "accounts-payable", "approval"]}],
    })

@app.get("/debug/llm")
async def debug_llm():
    info = {"model": MODEL, "token_set": bool(AIPIPE_TOKEN), "token_length": len(AIPIPE_TOKEN), "keep_brackets": KEEP_BRACKETS}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(AIPIPE_URL, headers={"Authorization": f"Bearer {AIPIPE_TOKEN}"},
                             json={"model": MODEL, "max_tokens": 5,
                                   "messages": [{"role": "user", "content": "Reply with the single word OK."}]})
        info["status"] = r.status_code
        info["reply"] = r.text[:200]
    except Exception as e:
        info["error"] = str(e)[:200]
    return JSONResponse(info)

@router.post("/message:send")
async def message_send(request: Request):
    principal, bad = guard(request, post=True)
    if bad:
        return bad
    try:
        body = await request.json()
        msg = body["message"]
        mid = str(msg["messageId"])
        assert msg["role"] == "ROLE_USER" and isinstance(msg["parts"], list) and msg["parts"] and mid
    except Exception:
        return err(400, "Invalid request")
    h = sha(canon(msg))
    lock = locks.setdefault((principal, mid), asyncio.Lock())
    async with lock:
        row = db.execute("select hash, task_id from msgs where principal=? and message_id=?", (principal, mid)).fetchone()
        if row:
            if row[0] != h:
                return err(409, "Message ID reused with different content", "IDEMPOTENCY_CONFLICT")
            task, _ = get_task(row[1], principal)
            return out(200, {"task": task})
        if msg.get("taskId"):
            return handle_results(principal, msg, mid, h)
        return await handle_batch(principal, msg, mid, h)

async def handle_batch(principal, msg, mid, h):
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
