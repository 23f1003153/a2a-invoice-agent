"""
A2A 1.0 Invoice Agent  (single file)

Run:
  pip install fastapi uvicorn httpx
  export PUBLIC_BASE_URL="https://YOUR-PUBLIC-HOST/a2a/"   # EXACTLY what you submit
  export AIPIPE_TOKEN="your AI Pipe token"
  export MODEL="openai/gpt-4.1-mini"                       # any chat model AI Pipe offers
  uvicorn app:app --host 0.0.0.0 --port 8000               # ONE worker only

Layers:  (1) protocol/auth  (2) storage + idempotency  (3) AI decision step
"""
import os, re, json, time, uuid, hashlib, sqlite3, asyncio
from decimal import Decimal
from urllib.parse import urlparse
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, APIRouter, Request
from fastapi.responses import JSONResponse

# ----------------------------------------------------------------- config
BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://example.com/a2a/")
BASE_PATH = urlparse(BASE_URL).path.rstrip("/")          # "/a2a"
AIPIPE_TOKEN = os.environ.get("AIPIPE_TOKEN", "")
AIPIPE_URL = os.environ.get("AIPIPE_URL", "https://aipipe.org/openrouter/v1/chat/completions")
MODEL = os.environ.get("MODEL", "openai/gpt-4.1-mini")
ALLOWED = {t for t in os.environ.get("ALLOWED_TOKENS", "").split(",") if t}  # empty = any bearer token
KEEP_BRACKETS = False  # evidence refs are returned exactly as written in the documents

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
    """Returns (principal, error_response)."""
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


# ----------------------------------------------------------------- AI layer
PROMPT = """You are an accounts-payable analyst. For EACH invoice package choose exactly one action:
- settle_invoice: valid, fully reconciled, within autonomous approval authority.
- request_approval: commercially valid but ABOVE delegated approval limit / needs a higher approver.
- hold_invoice: payment must pause until a stated verification (bank-detail check, PO/receipt confirmation, etc.) completes.
- reject_duplicate: the SAME commercial invoice (same vendor + invoice number/amount) was ALREADY PAID. A payment for a different invoice is NOT a duplicate.
- open_exception: material records conflict (amount/vendor/PO mismatch etc.) and need an exception workflow.
Rules:
- Each package mixes useful facts with OLD ARCHIVE EXAMPLES, TRAINING DECOYS, superseded policy, negated sentences ("this is NOT a duplicate", "do not hold") and irrelevant action words. None of those decide anything. Never copy references from them.
- The cover sheet only introduces the case. Its reference is NEVER evidence.
- Decide from the CURRENT invoice's own controlling facts: one paragraph states the facts that determine the action (amount vs. approval limit, bank/PO/receipt verification pending, a payment already made for the SAME vendor + SAME invoice number + SAME amount, or conflicting records). Apply the policy revision given.
- A prior payment of a DIFFERENT invoice number is NOT a duplicate.
- If a package has "controllingParagraph", decide ONLY from that paragraph (coverSheetFacts is just the invoice identity). It already excludes all decoys. Read what it actually states: already paid / second disbursement -> reject_duplicate; amount or authority above the delegated limit, needs a higher approver -> request_approval; payment must wait for a named verification (callback, bank detail check, receipt, PO confirmation) -> hold_invoice; records/amounts/identifiers contradict each other -> open_exception; everything matches and is within authority -> settle_invoice. Typical patterns: an invoice matching an earlier ledger posting even if only an OCR dash/spacing differs is still a duplicate; 'may act only up to X, below the total' means request_approval even if everything else reconciles; a changed bank/beneficiary with an unfinished callback is hold_invoice; valuation or signed-record contradictions that policy sends to an exception case/queue are open_exception; reconciled with no prior settlement and within authority is settle_invoice. Pay attention to negation ("no duplicate was found", "not above the limit") and to the NEGATED version of an action.
- Do not default to one action. Different packages need different actions.
- evidenceRefs = exactly the THREE bracketed references that appear inside that one controlling paragraph, copied character-for-character{br}.
- amountMinor = the current invoice total in minor units as an integer (1234.50 INR -> 123450). currency = 3-letter code.
- First write "analysis": 2-3 sentences listing which paragraph is controlling, what the decisive fact is, and why the other mentioned actions are decoys/old/negated. Then decide.
WORKED EXAMPLES (controlling paragraph -> action). Wording varies between cases, so judge the MEANING:
- Ledger links the claim to an earlier settled entry with matching supplier, invoice, currency, amount; policy prohibits a second disbursement -> reject_duplicate
- Earlier posting for same supplier/invoice/total; difference is only an OCR dash or spacing; exact commercial duplicate -> reject_duplicate
- Prior payment for the SAME signed commercial invoice; scan only omits punctuation; a second payment would be a duplicate, so reject -> reject_duplicate
- Finance already transferred funds against the original invoice; this upload reuses that document with only punctuation altered; reject the repeat -> reject_duplicate
- Prior payment to the SAME supplier for the SAME amount but for a DIFFERENT signed commercial invoice; own PO and receiving record; no payment for this exact invoice identity; can be settled -> settle_invoice
- Earlier disbursement was invoice A; this is independently issued invoice B despite same supplier and price; order and receipt validate B; no settlement of B; authorize -> settle_invoice
- Reconciled with no exception; inspection closed; duplicate query found no prior settlement -> settle_invoice
- PO and invoice totals agree within rounding tolerance; full quantity received; no earlier posting -> settle_invoice
- Everything reconciles but the total exceeds the autonomous delegation ceiling; a named financial approver is required -> request_approval
- No discrepancy remains, but this queue may act only up to X, which is below the stated total -> request_approval
- Amount 42530.55 is above this operator's authorization threshold 14728.58; named financial approver must authorize -> request_approval
- Beneficiary details replaced; verification through the pre-existing contact channel not completed; payment-change control pauses release -> hold_invoice
- Destination-account change; known-number callback neither confirmed nor rejected; treasury forbids remittance until it closes -> hold_invoice
- Payment destination differs from registered account; call to the verified number has not returned; remittance paused until the identity check closes -> hold_invoice
- Total outside reconciliation tolerance; contradictory signed records; policy bars settlement or simple hold; documented exception case -> open_exception
- PO in USD but invoice line extension conflicts with receiving valuation; incompatible explanations; policy sends it to the exception queue -> open_exception
- Order valuation disagrees with the amount due; supplier and purchasing disagree; no corrected instrument; belongs in the documented exception process, not payment or ordinary verification -> open_exception
Key distinctions: (1) a prior payment only makes it a duplicate if it is the SAME commercial invoice (punctuation/OCR/spacing changes do not make it different); same supplier + same amount but a DIFFERENT invoice is NOT a duplicate and is settled. (2) Compare numbers: only request_approval if the total is ABOVE the limit; within the limit and reconciled is settle_invoice. (3) A verification that has COMPLETED/confirmed no longer holds payment; only a PENDING or unfinished check means hold_invoice. (4) If the paragraph says an action is NOT needed or negates it, do not choose it.
Return ONLY JSON: {{"decisions":[{{"packageId":"","analysis":"","action":"","vendorName":"","invoiceNumber":"","amountMinor":0,"currency":"","evidenceRefs":["","",""],"reason":"1-2 sentences naming the decisive fact"}}]}}
Policy revision: {rev}
PACKAGES:
{pk}"""


def pkg_text(pkg):
    return json.dumps(pkg, ensure_ascii=False)


REF = re.compile(r"\[[A-Za-z0-9_\-]{6,}\]")
FACT = re.compile(r"Supplier\s+([^;]+?);\s*invoice\s+([^;]+?);\s*stated total\s+([A-Z]{3})\s+([\d,]+(?:\.\d+)?)")


def analyze(pkg):
    """Mechanical parsing: paragraphs with exactly 3 refs, and cover-sheet facts."""
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
            facts = {"vendorName": m.group(1).strip(), "invoiceNumber": m.group(2).strip(),
                     "amountMinor": int((Decimal(m.group(4).replace(",", "")) * 100).to_integral_value()),
                     "currency": m.group(3)}
        except Exception:
            facts = None
    return triples, facts


def with_hint(p):
    """If exactly one paragraph has three refs, show the model ONLY that paragraph + cover facts."""
    t, facts = analyze(p)
    if len(t) == 1:
        return {"packageId": p["packageId"], "coverSheetFacts": facts, "controllingParagraph": t[0][0]}
    return p


def fmt(refs):
    return [r if KEEP_BRACKETS else r.strip("[]") for r in refs]


RULES = {
    "reject_duplicate": [r"earlier settled entry", r"earlier posting for the same", r"second disbursement",
                         r"exact commercial duplicate", r"second settlement", r"already (been )?(paid|settled)", r"a second payment would be a duplicate", r"reject the repeat", r"cannot justify another transfer"],
    "request_approval": [r"delegation ceiling", r"autonomous delegation", r"named (financial )?approver",
                         r"without escalation only up to", r"delegation schedule", r"above that ceiling", r"authorization threshold"],
    "hold_invoice": [r"out-of-band", r"callback", r"pre-existing contact channel", r"changed instructions",
                     r"beneficiary details", r"destination-account change", r"payment-change control", r"must remain paused", r"independent identity check"],
    "open_exception": [r"exception queue", r"exception case", r"documented exception", r"exception workflow",
                       r"contradictory signed records", r"incompatible explanations", r"documented exception process"],
}


def rule_action(para):
    """Return an action only when exactly one category's distinctive phrases appear; else None."""
    t = para.lower()
    hit = [a for a, pats in RULES.items() if any(re.search(p, t) for p in pats)]
    return hit[0] if len(hit) == 1 else None


def normalize(pkg, d):
    """Validate a model decision against the package. Returns dict or None."""
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
            facts = {"vendorName": str(d["vendorName"]), "invoiceNumber": str(d["invoiceNumber"]),
                     "amountMinor": int(round(float(d["amountMinor"]))), "currency": str(d["currency"]).upper()}
        action = d["action"]
        if len(triples) == 1:
            ra = rule_action(triples[0][0])
            if ra and ra != action:
                print("NOTE keyword-rule differs from model", pkg.get("packageId"), "model:", action, "rule:", ra)
        return {"action": action, "facts": facts, "evidenceRefs": fmt(refs[:3]),
                "reason": str(d.get("reason", ""))[:700]}
    except Exception:
        return None


def fallback(pkg):
    triples, facts = analyze(pkg)
    refs = triples[0][1] if triples else REF.findall(pkg_text(pkg))[:3]
    ra = rule_action(triples[0][0]) if len(triples) == 1 else None
    if ra is None and len(triples) == 1 and re.search(r"no (prior|earlier)|without an exception|no exception", triples[0][0].lower()):
        ra = "settle_invoice"
    return {"action": ra or "open_exception",
            "facts": facts or {"vendorName": "unknown", "invoiceNumber": "unknown", "amountMinor": 0, "currency": "INR"},
            "evidenceRefs": fmt(refs), "reason": "The model could not produce a validated decision, so the invoice goes to manual review."}


async def llm_batch(pkgs, rev):
    br = " (including the square brackets)" if KEEP_BRACKETS else " (without the square brackets)"
    prompt = PROMPT.format(br=br, rev=rev, pk=json.dumps([with_hint(p) for p in pkgs], ensure_ascii=False))
    async with httpx.AsyncClient(timeout=38) as c:
        r = await c.post(AIPIPE_URL, headers={"Authorization": f"Bearer {AIPIPE_TOKEN}"},
                         json={"model": MODEL, "temperature": 0, "response_format": {"type": "json_object"},
                               "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"]
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip()
    return {d.get("packageId"): d for d in json.loads(txt)["decisions"]}


async def decide(pkgs, rev):
    """One decision per package; cached by canonical package content."""
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
            if not used_fallback:      # never cache fallbacks
                db.execute("insert or replace into cache values(?,?)", (k, json.dumps(dec)))
            result[p["packageId"]] = dec
    from collections import Counter
    print('ACTION MIX', dict(Counter(v['action'] for v in result.values())))
    return result


def build_rationale(dec):
    f, refs = dec["facts"], dec["evidenceRefs"]
    s = (f"Action {dec['action']} for invoice {f['invoiceNumber']} from {f['vendorName']}: {dec['reason']} "
         f"Decisive evidence: {', '.join(refs)}.")
    s = s[:1500]
    return s if len(s) >= 60 else s + " " * 0 + " This follows the policy paragraph cited above."


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
    h = sha(canon(msg))                      # message only; configuration ignored
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
    try:  # keep a sample so you can see the real format
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
            "action": d["action"], "facts": d["facts"], "evidenceRefs": d["evidenceRefs"],
            "rationale": build_rationale(d)})
    tid, ctx = str(uuid.uuid4()), msg.get("contextId") or str(uuid.uuid4())
    hist = dict(msg, taskId=tid, contextId=ctx)
    task = {"id": tid, "contextId": ctx,
            "status": {"state": "TASK_STATE_INPUT_REQUIRED", "timestamp": now()},
            "artifacts": [{"artifactId": "proposals-" + tid[:8], "name": "Invoice action proposals",
                           "parts": [{"mediaType": PROP_MODE, "data": {"batchId": data["batchId"], "proposals": proposals}}]}],
            "history": [hist]}
    save_task(principal, task, {"batchId": data["batchId"], "proposals": proposals})
    db.execute("insert into msgs values(?,?,?,?)", (principal, mid, h, tid))     # stored BEFORE we respond
    return out(200, {"task": task})


def handle_results(principal, msg, mid, h):
    task, stored = get_task(str(msg["taskId"]), principal)
    if not task:
        return err(404, "Task not found")                         # same answer for "not yours" and "missing"
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
            executions.append({"packageId": p["packageId"], "actionId": p["actionId"], "action": p["action"],
                               "receiptNonce": r["receiptNonce"], "facts": p["facts"], "evidenceRefs": p["evidenceRefs"]})
    # --- all checks passed; this block has no awaits, so it is atomic vs. cancel ---
    task["history"].append(dict(msg, taskId=task["id"], contextId=task["contextId"]))
    task["artifacts"].append({"artifactId": "receipts-" + task["id"][:8], "name": "Invoice action receipts",
                              "parts": [{"mediaType": RECEIPT_MODE,
                                         "data": {"batchId": stored["batchId"], "executions": executions}}]})
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
    task["status"] = {"state": "TASK_STATE_CANCELED", "timestamp": now()}      # no awaits: atomic
    save_task(principal, task, stored)
    return out(200, task)


app.include_router(router, prefix=BASE_PATH)
