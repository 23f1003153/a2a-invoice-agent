"""
A2A 1.0 Invoice Agent (single file)

Run:
  pip install -r requirements.txt
  export GEMINI_API_KEY="..."
  export LLM_MODEL="gemini-2.5-flash-lite"
  export PUBLIC_BASE_URL="https://YOUR-HOST/a2a/"
  uvicorn app:app --host 0.0.0.0 --port 8000
"""
from decimal import Decimal
import os, re, json, time, uuid, asyncio, hashlib, sqlite3, threading, datetime, traceback
from urllib.parse import urlparse
import httpx
from fastapi import FastAPI, APIRouter, Request
from fastapi.responses import JSONResponse

# ------------------------------------------------------------------ config
BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://YOUR-HOST/a2a/")
if not BASE_URL.endswith("/"):
    BASE_URL += "/"
PREFIX = urlparse(BASE_URL).path.rstrip("/")
LLM_KEY = os.environ.get("GEMINI_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-2.5-flash-lite")
LLM_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
REQUEST_BUDGET = 38  # seconds; the grader allows 45 per request

MEDIA = "application/a2a+json"
BATCH_T = "application/vnd.ga5.invoice-claim-batch+json"
PROP_T = "application/vnd.ga5.invoice-action-proposals+json"
RES_T = "application/vnd.ga5.invoice-action-results+json"
REC_T = "application/vnd.ga5.invoice-action-receipts+json"
ACTIONS = {"settle_invoice", "request_approval", "hold_invoice", "reject_duplicate", "open_exception"}
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"}
CACHE_VERSION = "v5"  # bump whenever you change a prompt or rule

app = FastAPI()
app_Q2 = app  # alias so old command "uvicorn app:app_Q2" also works
router = APIRouter()

# ------------------------------------------------------------------ storage
db = sqlite3.connect("state.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, principal TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS msgs(principal TEXT, mid TEXT, hash TEXT, task_id TEXT, PRIMARY KEY(principal, mid));
CREATE TABLE IF NOT EXISTS cache(h TEXT PRIMARY KEY, body TEXT);
""")
dblock = threading.RLock()  # one-at-a-time: makes receipt-vs-cancel a fair race
key_locks = {}              # concurrent equal messages wait for the first one


def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def canon(o): return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
def sha(s): return hashlib.sha256(s.encode()).hexdigest()
def reply(obj, code=200): return JSONResponse(obj, status_code=code, media_type=MEDIA)
def err(code, name, msg="Request failed"): return reply({"error": {"code": name, "message": msg}}, code)


def load(tid, principal):
    with dblock:
        r = db.execute("SELECT body FROM tasks WHERE id=? AND principal=?", (tid, principal)).fetchone()
    return json.loads(r[0]) if r else None


def save(task, principal):
    with dblock:
        db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?)", (task["id"], principal, json.dumps(task)))


# ------------------------------------------------------------------ gate
def gate(req: Request, post=False):
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not auth[7:].strip():
        return None, err(401, "UNAUTHENTICATED", "Authentication required")
    if req.headers.get("a2a-version") != "1.0":
        return None, err(400, "VERSION_NOT_SUPPORTED", "Unsupported version")
    if post and not req.headers.get("content-type", "").startswith(MEDIA):
        return None, err(415, "UNSUPPORTED_MEDIA_TYPE", "Use application/a2a+json")
    return sha(auth[7:].strip()), None  # principal = hash of the token


# ------------------------------------------------------------------ agent card (public)
@app.get("/.well-known/agent-card.json")
def card():
    return reply({
        "name": "Invoice Action Agent",
        "description": "Reads invoice packages, proposes safe business actions with evidence, executes only approved ones.",
        "version": "1.0.0",
        "supportedInterfaces": [{"url": BASE_URL, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": [BATCH_T, RES_T],
        "defaultOutputModes": [PROP_T, REC_T],
        "skills": [{"id": "invoice_action_agent", "name": "Invoice action agent",
                    "description": "Choose settle/approve/hold/reject/exception for each invoice.",
                    "tags": ["invoice", "finance", "approval"]}],
    })


# ------------------------------------------------------------------ AI layer
# Idea: Python finds the paragraphs that contain exactly three [bracketed] refs
# (the controlling paragraph is always one of them). The AI only chooses WHICH one
# is controlling and WHAT action it requires. Python copies the refs itself.
REF = re.compile(r"\[[A-Za-z0-9_\-]{6,}\]")
FACT = re.compile(r"Supplier\s+([^;]+?);\s*invoice\s+([^;]+?);\s*stated total\s+([A-Z]{3})\s+([\d,]+(?:\.\d+)?)")
DECOY = re.compile(r"archive|training|decoy|example|superseded|obsolete|former|legacy|sample|old ", re.I)
CHECKS = ["valid_and_reconciled", "within_authority", "no_pending_verification",
          "not_already_paid", "no_conflicting_records"]

PROMPT = """You are an accounts-payable analyst. For EACH package choose exactly one action:
- settle_invoice: valid, fully reconciled, within autonomous approval authority.
- request_approval: commercially valid, but the amount is ABOVE the delegated limit, so a named/higher approver must approve.
- hold_invoice: payment must pause until a stated verification (callback, changed bank details, receipt check...) completes.
- reject_duplicate: the SAME commercial invoice (same vendor AND same invoice number) was ALREADY PAID.
  A payment for a DIFFERENT invoice number is NOT a duplicate.
- open_exception: material records conflict and need an exception workflow.

Each package contains "candidateParagraphs". Every candidate has exactly three bracketed refs.
EXACTLY ONE candidate is the CONTROLLING paragraph: it states the live facts of THIS invoice and what must happen.
The others are old archive examples, training decoys, superseded policy, or negated statements. Ignore them.
("looksLikeDecoy" is only a hint, not proof.)
Read what the controlling paragraph actually REQUIRES. Action words inside negated or hypothetical sentences
("no callback is needed", "this is not a duplicate") mean that action does NOT apply.
If a package has "package" instead of candidates, read it all, find the single controlling paragraph and return
its three bracketed refs (copied exactly, brackets included) in evidenceRefs.
Use coverSheetFacts for vendorName/invoiceNumber/amountMinor/currency when present.
amountMinor is an integer in minor units (1234.50 -> 123450).
Never choose settle_invoice when unsure.
Reply ONLY JSON: {"decisions":[{"packageId":"","controllingIndex":0,"action":"","vendorName":"",
"invoiceNumber":"","amountMinor":0,"currency":"","evidenceRefs":[],"reason":"one sentence naming the decisive fact"}]}"""

VERIFY = """You are a skeptical reviewer. A colleague proposed settle_invoice for each package below.
Use the controlling paragraph only (the single candidate that states the live facts of THIS invoice; ignore archive
examples, decoys, superseded policy and negated sentences). For each package answer true/false:
valid_and_reconciled, within_authority, no_pending_verification, not_already_paid, no_conflicting_records.
Then give the correct action (settle_invoice, request_approval, hold_invoice, reject_duplicate or open_exception)
and controllingIndex.
Reply ONLY JSON: {"verdicts":[{"packageId":"","controllingIndex":0,"checks":{"valid_and_reconciled":true,
"within_authority":true,"no_pending_verification":true,"not_already_paid":true,"no_conflicting_records":true},
"action":"","reason":""}]}"""


def _strings(o, acc):
    if isinstance(o, str):
        acc.append(o)
    elif isinstance(o, dict):
        for v in o.values():
            _strings(v, acc)
    elif isinstance(o, list):
        for v in o:
            _strings(v, acc)


def analyze(p):
    acc = []
    _strings(p, acc)
    paras = []
    for s in acc:
        paras += [x.strip() for x in re.split(r"\n\s*\n", s) if x.strip()]
    triples, seen = [], set()
    for para in paras:
        refs = list(dict.fromkeys(REF.findall(para)))
        if len(refs) == 3 and para not in seen:
            seen.add(para)
            triples.append((para, refs))
    facts = None
    m = FACT.search("\n\n".join(paras))
    if m:
        try:
            facts = {"vendorName": m.group(1).strip(), "invoiceNumber": m.group(2).strip(),
                     "amountMinor": int((Decimal(m.group(4).replace(",", "")) * 100).to_integral_value()),
                     "currency": m.group(3)}
        except Exception:
            facts = None
    return triples, facts


def reduce_pkg(p):
    triples, facts = analyze(p)
    r = {"packageId": p["packageId"], "coverSheetFacts": facts}
    if triples:
        r["candidateParagraphs"] = [{"index": i, "refs": refs, "text": t, "looksLikeDecoy": bool(DECOY.search(t))}
                                    for i, (t, refs) in enumerate(triples)]
    if not triples or facts is None:
        r["package"] = p  # model needs the full text (to read facts or refs)
    return r, triples, facts


def refs_ok(refs, text):
    return (isinstance(refs, list) and len(refs) == 3 and len(set(refs)) == 3
            and all(isinstance(r, str) and re.fullmatch(r"\[[^\[\]]+\]", r) and r in text for r in refs))


def normalize(p, d, triples, facts):
    """Turn a raw model answer into a trusted decision, or None."""
    try:
        if d.get("action") not in ACTIONS:
            return None
        if triples:
            ci = d.get("controllingIndex")
            if not isinstance(ci, int) or not (0 <= ci < len(triples)):
                return None
            refs = triples[ci][1]
        else:
            ci = None
            refs = d.get("evidenceRefs")
            if not refs_ok(refs, canon(p)):
                return None
        f = facts or {"vendorName": d.get("vendorName"), "invoiceNumber": d.get("invoiceNumber"),
                      "amountMinor": d.get("amountMinor"), "currency": d.get("currency")}
        if (not isinstance(f["vendorName"], str) or not f["vendorName"] or not isinstance(f["invoiceNumber"], str)
                or not f["invoiceNumber"] or not isinstance(f["amountMinor"], int)):
            return None
        return {"action": d["action"], "ci": ci, "evidenceRefs": list(refs), "vendorName": f["vendorName"],
                "invoiceNumber": f["invoiceNumber"], "amountMinor": f["amountMinor"],
                "currency": str(f.get("currency") or "INR").upper(), "reason": str(d.get("reason", ""))[:600]}
    except Exception:
        return None


def log_line(obj):
    try:
        with open("decisions.jsonl", "a") as f:
            f.write(canon(obj) + "\n")
    except Exception:
        pass


async def call_llm(system, user, timeout):
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(
            LLM_URL.format(model=LLM_MODEL),
            headers={"Content-Type": "application/json", "X-goog-api-key": LLM_KEY},
            json={"systemInstruction": {"parts": [{"text": system}]},
                  "contents": [{"role": "user", "parts": [{"text": user}]}],
                  "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}})
    if r.status_code != 200:
        print("LLM HTTP", r.status_code, r.text[:300], flush=True)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    s, e = text.find("{"), text.rfind("}")
    return json.loads(text[s:e + 1])


async def ask_llm_safe(policy, reduced, timeout):
    try:
        res = await call_llm(PROMPT, f"Policy revision: {policy}\nPackages:\n{canon(reduced)}", timeout)
        return [d for d in res["decisions"] if isinstance(d, dict)]
    except Exception:
        traceback.print_exc()
        return []


async def verify_batch(policy, reduced, timeout):
    try:
        res = await call_llm(VERIFY, f"Policy revision: {policy}\nPackages:\n{canon(reduced)}", timeout)
        return {v.get("packageId"): v for v in res["verdicts"] if isinstance(v, dict)}
    except Exception:
        traceback.print_exc()
        return {}  # no answer => we will NOT settle


def pkg_key(p):
    body = {k: v for k, v in p.items() if k != "packageId"}
    return sha(CACHE_VERSION + canon(body))


def fallback_decision(p, triples, facts):
    """AI failed: never settle. Give real refs from the first candidate."""
    refs = triples[0][1] if triples else (list(dict.fromkeys(REF.findall(canon(p))))[:3] + ["[none-0]", "[none-1]", "[none-2]"])[:3]
    f = facts or {"vendorName": "unknown", "invoiceNumber": "unknown", "amountMinor": 0, "currency": "INR"}
    return {"action": "open_exception", "ci": 0 if triples else None, "evidenceRefs": refs,
            "reason": "Automatic analysis was unavailable so this is escalated for manual review.", **f}


async def decide(policy, packages, deadline):
    left = lambda: deadline - time.monotonic()
    out, todo = {}, []
    for p in packages:
        with dblock:
            r = db.execute("SELECT body FROM cache WHERE h=?", (pkg_key(p),)).fetchone()
        if r:
            out[p["packageId"]] = json.loads(r[0])
        else:
            todo.append(p)
    pending = list(todo)
    info = {p["packageId"]: reduce_pkg(p) for p in pending}  # pid -> (reduced, triples, facts)
    fresh = {}

    # pass 1: one batched call on the SMALL reduced inputs (one retry if needed and time allows)
    for _ in range(2):
        if not todo or left() < 10:
            break
        got = {d.get("packageId"): d for d in
               await ask_llm_safe(policy, [info[p["packageId"]][0] for p in todo], max(5, min(26, left() - 8)))}
        still = []
        for p in todo:
            _, triples, facts = info[p["packageId"]]
            dec = normalize(p, got.get(p["packageId"], {}), triples, facts)
            if dec:
                fresh[p["packageId"]] = dec
            else:
                still.append(p)
        todo = still

    # pass 2: skeptical review of every settle, in one batched call
    settles = [p for p in pending if fresh.get(p["packageId"], {}).get("action") == "settle_invoice"]
    verdicts = {}
    if settles and left() > 5:
        verdicts = await verify_batch(policy, [info[p["packageId"]][0] for p in settles], max(4, min(18, left() - 2)))

    for p in pending:
        pid = p["packageId"]
        _, triples, facts = info[pid]
        d, cacheable = fresh.get(pid), True
        if d is None:
            d, cacheable = fallback_decision(p, triples, facts), False
        elif d["action"] == "settle_invoice":
            v = verdicts.get(pid)
            checks = (v or {}).get("checks") or {}
            ok = (v is not None and v.get("action") == "settle_invoice"
                  and all(checks.get(k) is True for k in CHECKS)
                  and (not triples or v.get("controllingIndex") == d["ci"]))
            if v is None:
                d, cacheable = {**d, "action": "open_exception"}, False
            elif not ok:
                va = v.get("action")
                d = {**d, "action": va if va in ACTIONS and va != "settle_invoice" else "open_exception",
                     "reason": v.get("reason") or d["reason"]}
        out[pid] = d
        print("DECISION", pid, "cands=%d" % len(triples), "->", d["action"], d["evidenceRefs"], flush=True)
        log_line({"packageId": pid, "candidates": len(triples), "final": d["action"], "refs": d["evidenceRefs"]})
        if d["action"] == "settle_invoice":
            log_line({"SETTLED_PACKAGE": pid, "text": canon(info[pid][0])})
        if cacheable:
            with dblock:
                db.execute("INSERT OR REPLACE INTO cache VALUES(?,?)", (pkg_key(p), json.dumps(d)))
    with dblock:
        db.commit()
    return out


def proposal(p, d):
    a, r = d["action"], d["evidenceRefs"]
    rationale = (f"Proposing {a} for invoice {d['invoiceNumber']} from {d['vendorName']}. "
                 f"{d.get('reason', '')} Evidence {r[0]}, {r[1]} and {r[2]} in the controlling paragraph support {a}.")[:1500]
    return {"packageId": p["packageId"], "actionId": "act-" + uuid.uuid4().hex, "action": a,
            "facts": {"vendorName": d["vendorName"], "invoiceNumber": d["invoiceNumber"],
                      "amountMinor": d["amountMinor"], "currency": d.get("currency", "INR")},
            "evidenceRefs": r, "rationale": rationale}


# ------------------------------------------------------------------ message:send
@router.post("/message:send")
async def message_send(req: Request):
    t0 = time.monotonic()
    principal, bad = gate(req, post=True)
    if bad:
        return bad
    try:
        body = await req.json()
        msg = body["message"]
        mid = str(msg["messageId"])
        assert isinstance(msg["parts"], list) and msg["parts"] and mid
    except Exception:
        return err(400, "INVALID_REQUEST")
    h = sha(canon(msg))  # configuration ignored on purpose
    lock = key_locks.setdefault((principal, mid), asyncio.Lock())
    async with lock:
        with dblock:
            seen = db.execute("SELECT hash, task_id FROM msgs WHERE principal=? AND mid=?", (principal, mid)).fetchone()
        if seen:
            if seen[0] != h:
                return err(409, "IDEMPOTENCY_CONFLICT", "Message id reused with different content")
            return reply({"task": load(seen[1], principal)})
        part = msg["parts"][0]
        if msg.get("taskId"):
            return continuation(principal, msg, part, mid, h)
        try:
            if part.get("mediaType") != BATCH_T:
                return err(400, "INVALID_REQUEST")
            data = part["data"]
            pkgs = data["packages"]
            assert data["batchId"] and isinstance(pkgs, list) and pkgs
            ids = [p["packageId"] for p in pkgs]
            assert len(set(ids)) == len(ids)
        except Exception:
            return err(400, "INVALID_REQUEST")
        decisions = await decide(data.get("policyRevision", ""), pkgs, t0 + REQUEST_BUDGET)
        props = [proposal(p, decisions[p["packageId"]]) for p in pkgs]
        tid, cid = str(uuid.uuid4()), msg.get("contextId") or str(uuid.uuid4())
        task = {"id": tid, "contextId": cid,
                "status": {"state": "TASK_STATE_INPUT_REQUIRED", "timestamp": now()},
                "artifacts": [{"artifactId": "proposals", "name": "invoice-action-proposals",
                               "parts": [{"mediaType": PROP_T, "data": {"batchId": data["batchId"], "proposals": props}}]}],
                "history": [{**msg, "taskId": tid, "contextId": cid}]}
        with dblock:  # store BEFORE responding
            save(task, principal)
            db.execute("INSERT INTO msgs VALUES(?,?,?,?)", (principal, mid, h, tid))
            db.commit()
        return reply({"task": task})


def continuation(principal, msg, part, mid, h):
    with dblock:
        task = load(str(msg["taskId"]), principal)
        if not task:
            return err(404, "NOT_FOUND", "Task not found")
        if msg.get("contextId") != task["contextId"]:
            return err(400, "INVALID_REQUEST")
        if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
            return err(409, "TASK_NOT_ACTIONABLE")
        if part.get("mediaType") != RES_T or not isinstance(part.get("data"), dict):
            return err(400, "INVALID_REQUEST")
        stored = task["artifacts"][0]["parts"][0]["data"]
        data = part["data"]
        if data.get("batchId") != stored["batchId"] or not isinstance(data.get("results"), list):
            return err(400, "INVALID_REQUEST")
        byid = {p["packageId"]: p for p in stored["proposals"]}
        execs, seen = [], set()
        for r in data["results"]:
            p = byid.get(r.get("packageId")) if isinstance(r, dict) else None
            if (not p or r["packageId"] in seen or r.get("actionId") != p["actionId"]
                    or r.get("action") != p["action"] or r.get("outcome") not in ("ACCEPTED", "REJECTED")):
                return err(400, "INVALID_REQUEST")
            seen.add(r["packageId"])
            if r["outcome"] == "ACCEPTED":
                if not isinstance(r.get("receiptNonce"), str) or not r["receiptNonce"]:
                    return err(400, "INVALID_REQUEST")
                execs.append({"packageId": p["packageId"], "actionId": p["actionId"], "action": p["action"],
                              "receiptNonce": r["receiptNonce"], "facts": p["facts"],
                              "evidenceRefs": p["evidenceRefs"]})
        task["artifacts"].append({"artifactId": "receipts", "name": "invoice-action-receipts",
                                  "parts": [{"mediaType": REC_T,
                                             "data": {"batchId": stored["batchId"], "executions": execs}}]})
        task["history"].append({**msg, "taskId": task["id"], "contextId": task["contextId"]})
        task["status"] = {"state": "TASK_STATE_COMPLETED", "timestamp": now()}
        save(task, principal)
        db.execute("INSERT INTO msgs VALUES(?,?,?,?)", (principal, mid, h, task["id"]))
        db.commit()
        return reply({"task": task})


# ------------------------------------------------------------------ reads, list, cancel (no model calls)
@router.get("/tasks/{tid}")
def get_task(tid: str, req: Request):
    principal, bad = gate(req)
    if bad:
        return bad
    t = load(tid, principal)
    return reply(t) if t else err(404, "NOT_FOUND", "Task not found")


MAX_LIST_BYTES = 350_000


@router.get("/tasks")
def list_tasks(req: Request):
    principal, bad = gate(req)
    if bad:
        return bad
    with dblock:
        rows = db.execute("SELECT body FROM tasks WHERE principal=? ORDER BY rowid DESC LIMIT 20",
                          (principal,)).fetchall()
    tasks, size = [], 0
    for (b,) in rows:
        t = {k: v for k, v in json.loads(b).items() if k != "history"}  # drop the huge history
        n = len(json.dumps(t))
        if tasks and size + n > MAX_LIST_BYTES:
            break
        tasks.append(t)
        size += n
    return reply({"tasks": tasks})


@router.post("/tasks/{tid}:cancel")
def cancel(tid: str, req: Request):
    principal, bad = gate(req)
    if bad:
        return bad
    with dblock:
        t = load(tid, principal)
        if not t:
            return err(404, "NOT_FOUND", "Task not found")
        if t["status"]["state"] in TERMINAL:
            return err(409, "TASK_NOT_CANCELABLE")
        t["status"] = {"state": "TASK_STATE_CANCELED", "timestamp": now()}
        save(t, principal)
        db.commit()
    return reply(t)


app.include_router(router, prefix=PREFIX)
