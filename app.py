# """
# A2A 1.0 Invoice Agent  (single file) - FINAL SAFE VERSION
# Run:
#   pip install fastapi uvicorn httpx
#   export PUBLIC_BASE_URL="https://YOUR-PUBLIC-HOST/a2a/"
#   export AIPIPE_TOKEN="your AI Pipe token"
#   export MODEL="openai/gpt-4.1-mini"
#   export KEEP_BRACKETS=1
#   uvicorn app:app --host 0.0.0.0 --port 8000
# """
# import os, re, json, time, uuid, hashlib, sqlite3, asyncio
# from decimal import Decimal
# from urllib.parse import urlparse
# from datetime import datetime, timezone
# from collections import Counter
# import httpx
# from fastapi import FastAPI, APIRouter, Request
# from fastapi.responses import JSONResponse

# # ----------------------------------------------------------------- config
# BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://example.com/a2a/")
# BASE_PATH = urlparse(BASE_URL).path.rstrip("/")
# AIPIPE_TOKEN = os.environ.get("AIPIPE_TOKEN", "")
# AIPIPE_URL = os.environ.get("AIPIPE_URL", "https://aipipe.org/openrouter/v1/chat/completions")
# MODEL = os.environ.get("MODEL", "openai/gpt-4.1-mini")
# ALLOWED = {t for t in os.environ.get("ALLOWED_TOKENS", "").split(",") if t}
# KEEP_BRACKETS = os.environ.get("KEEP_BRACKETS", "0") == "1"

# A2A = "application/a2a+json"
# IN_MODE = "application/vnd.ga5.invoice-claim-batch+json"
# PROP_MODE = "application/vnd.ga5.invoice-action-proposals+json"
# RECEIPT_MODE = "application/vnd.ga5.invoice-action-receipts+json"
# RESULT_MODE = "application/vnd.ga5.invoice-action-results+json"
# ACTIONS = {"settle_invoice", "request_approval", "hold_invoice", "reject_duplicate", "open_exception"}
# TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED", "TASK_STATE_REJECTED"}

# # ----------------------------------------------------------------- storage
# db = sqlite3.connect(os.environ.get("DB_PATH", "agent.db"), check_same_thread=False, isolation_level=None)
# db.executescript("""
# create table if not exists tasks(id text primary key, principal text, state text, body text, proposals text, created real);
# create table if not exists msgs(principal text, message_id text, hash text, task_id text, primary key(principal, message_id));
# create table if not exists cache(key text primary key, decision text);
# """)
# locks: dict = {}

# def canon(o) -> str:
#     return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

# def sha(s: str) -> str:
#     return hashlib.sha256(s.encode()).hexdigest()

# def now() -> str:
#     return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

# def get_task(tid, principal):
#     r = db.execute("select body, proposals from tasks where id=? and principal=?", (tid, principal)).fetchone()
#     return (json.loads(r[0]), json.loads(r[1])) if r else (None, None)

# def save_task(principal, task, proposals, created=None):
#     db.execute("insert or replace into tasks values(?,?,?,?,?,?)",
#                (task["id"], principal, task["status"]["state"], json.dumps(task), json.dumps(proposals),
#                 created or time.time()))

# # ----------------------------------------------------------------- http helpers
# def out(status, obj, headers=None):
#     return JSONResponse(obj, status_code=status, media_type=A2A, headers=headers)

# def err(status, msg, reason=None):
#     e = {"code": status, "message": msg}
#     if reason:
#         e["reason"] = reason
#     body = {"error": e}
#     if reason:
#         body["code"] = reason
#     return out(status, body)

# def guard(request: Request, post=False):
#     m = re.fullmatch(r"Bearer (\S+)", request.headers.get("authorization", ""))
#     if not m or (ALLOWED and m.group(1) not in ALLOWED):
#         return None, out(401, {"error": {"code": 401, "message": "Unauthorized"}}, {"WWW-Authenticate": "Bearer"})
#     if request.headers.get("a2a-version") != "1.0":
#         return None, err(400, "Unsupported or missing A2A-Version", "VERSION_NOT_SUPPORTED")
#     acc = request.headers.get("accept", "*/*")
#     if not any(x in acc for x in (A2A, "application/json", "*/*", "application/*")):
#         return None, err(406, "Not acceptable")
#     if post:
#         ct = request.headers.get("content-type", "").split(";")[0].strip().lower()
#         if ct != A2A:
#             return None, err(415, "Unsupported media type")
#     return sha(m.group(1))[:32], None

# # ----------------------------------------------------------------- AI / decision layer
# PROMPT = """You are an accounts-payable analyst. For EACH invoice package choose exactly one action:
# - settle_invoice: valid, fully reconciled, within autonomous approval authority.
# - request_approval: commercially valid but ABOVE delegated approval limit / needs a higher approver.
# - hold_invoice: payment must pause until a stated verification (bank-detail check, PO/receipt confirmation, etc.) completes.
# - reject_duplicate: the SAME commercial invoice (same vendor + invoice number/amount) was ALREADY PAID. A payment for a different invoice is NOT a duplicate.
# - open_exception: material records conflict (amount/vendor/PO mismatch etc.) and need an exception workflow.
# Rules:
# - Each package mixes useful facts with OLD ARCHIVE EXAMPLES, TRAINING DECOYS, superseded policy, negated sentences and irrelevant action words. None of those decide anything. Never copy references from them.
# - The cover sheet only introduces the case. Its reference is NEVER evidence.
# - Decide from the CURRENT invoice's own controlling facts: one paragraph states the facts that determine the action.
# - A prior payment of a DIFFERENT invoice number is NOT a duplicate.
# - If a package has "controllingParagraph", decide ONLY from that paragraph.
# - evidenceRefs = exactly the THREE bracketed references that appear inside that one controlling paragraph, copied character-for-character{br}.
# - amountMinor = the current invoice total in minor units as an integer. currency = 3-letter code.
# Return ONLY JSON: {{"decisions":[{{"packageId":"","analysis":"","action":"","vendorName":"","invoiceNumber":"","amountMinor":0,"currency":"","evidenceRefs":["","",""],"reason":"1-2 sentences naming the decisive fact"}}]}}
# Policy revision: {rev}
# PACKAGES:
# {pk}"""

# REF = re.compile(r"\[[A-Za-z0-9_\-]{6,}\]")
# FACT = re.compile(r"Supplier\s+([^;]+?);\s*invoice\s+([^;]+?);\s*stated total\s+([A-Z]{3})\s+([\d,]+(?:\.\d+)?)")

# def pkg_text(pkg):
#     return json.dumps(pkg, ensure_ascii=False)

# def analyze(pkg):
#     paras = []
#     for d in (pkg.get("documents", []) if isinstance(pkg, dict) else []):
#         paras += str(d.get("text", "")).split("\n\n")
#     triples = []
#     for para in paras:
#         refs = list(dict.fromkeys(REF.findall(para)))
#         if len(refs) == 3:
#             triples.append((para, refs))
#     facts = None
#     m = FACT.search("\n\n".join(paras))
#     if m:
#         try:
#             facts = {
#                 "vendorName": m.group(1).strip(),
#                 "invoiceNumber": m.group(2).strip(),
#                 "amountMinor": int((Decimal(m.group(4).replace(",", "")) * 100).to_integral_value()),
#                 "currency": m.group(3)
#             }
#         except Exception:
#             facts = None
#     return triples, facts

# def with_hint(p):
#     t, facts = analyze(p)
#     if len(t) == 1:
#         return {"packageId": p["packageId"], "coverSheetFacts": facts, "controllingParagraph": t[0][0]}
#     return p

# def fmt(refs):
#     return [("[" + r.strip("[] ") + "]") if KEEP_BRACKETS else r.strip("[] ") for r in refs]

# RULES = {
#     "reject_duplicate": [
#         r"earlier settled entry", r"earlier posting for the same", r"second disbursement",
#         r"exact commercial duplicate", r"second settlement", r"already (been )?(paid|settled)",
#         r"a second payment would be a duplicate", r"reject the repeat", r"cannot justify another transfer",
#         r"finance has already transferred funds", r"reuses that original document"
#     ],
#     "request_approval": [
#         r"delegation ceiling", r"autonomous delegation", r"named (financial )?approver",
#         r"without escalation only up to", r"delegation schedule", r"above that ceiling",
#         r"authorization threshold", r"outside the operator'?s .* authority",
#         r"requires the financial-approval workflow"
#     ],
#     "hold_invoice": [
#         r"out-of-band", r"callback", r"pre-existing contact channel", r"changed instructions",
#         r"beneficiary details", r"destination-account change", r"payment-change control",
#         r"must remain paused", r"independent identity check", r"known-number callback",
#         r"treasury forbids remittance", r"payment-detail changes until"
#     ],
#     "open_exception": [
#         r"exception queue", r"exception case", r"documented exception", r"exception workflow",
#         r"contradictory signed records", r"incompatible explanations", r"documented exception process",
#         r"outside the permitted reconciliation tolerance", r"bars settlement or simple hold"
#     ],
# }

# def rule_action(para: str):
#     t = para.lower()
#     hits = [a for a, pats in RULES.items() if any(re.search(p, t) for p in pats)]
#     return hits[0] if len(hits) == 1 else None

# def normalize(pkg, d):
#     try:
#         triples, facts = analyze(pkg)
#         text = pkg_text(pkg)
#         if d.get("action") not in ACTIONS:
#             return None
#         if len(triples) == 1:
#             refs = triples[0][1]
#         else:
#             refs = []
#             for r in d.get("evidenceRefs", []):
#                 r = "[" + str(r).strip("[] ") + "]"
#                 if r in text and r not in refs:
#                     refs.append(r)
#             if len(refs) < 2:
#                 if triples:
#                     refs = triples[0][1]
#                 else:
#                     return None
#         if facts is None:
#             facts = {
#                 "vendorName": str(d["vendorName"]),
#                 "invoiceNumber": str(d["invoiceNumber"]),
#                 "amountMinor": int(round(float(d["amountMinor"]))),
#                 "currency": str(d["currency"]).upper()
#             }
#         action = d["action"]
#         # Force rule-based override when model disagrees
#         if len(triples) == 1:
#             ra = rule_action(triples[0][0])
#             if ra and ra != action:
#                 print(f"OVERRIDE model={action} → rule={ra}  pkg={pkg.get('packageId')}")
#                 action = ra
#         return {
#             "action": action,
#             "facts": facts,
#             "evidenceRefs": list(refs[:3]),
#             "reason": str(d.get("reason", ""))[:700]
#         }
#     except Exception:
#         return None

# def fallback(pkg):
#     triples, facts = analyze(pkg)
#     if len(triples) == 1:
#         para, refs = triples[0]
#         ra = rule_action(para)
#         if ra is None:
#             low = para.lower()
#             # Strict priority order – never guess settle when unsure
#             if any(x in low for x in [
#                 "second disbursement", "already paid", "exact commercial duplicate",
#                 "reject the repeat", "cannot justify another transfer",
#                 "finance has already transferred", "reuses that original document",
#                 "already been paid", "second settlement"
#             ]):
#                 ra = "reject_duplicate"
#             elif any(x in low for x in [
#                 "delegation ceiling", "named financial approver", "without escalation only up to",
#                 "above that ceiling", "authorization threshold", "outside the operator",
#                 "requires the financial-approval"
#             ]):
#                 ra = "request_approval"
#             elif any(x in low for x in [
#                 "callback", "beneficiary", "destination-account", "payment-change control",
#                 "out-of-band", "treasury forbids", "known-number callback"
#             ]):
#                 ra = "hold_invoice"
#             elif any(x in low for x in [
#                 "exception queue", "exception case", "contradictory signed records",
#                 "incompatible explanations", "outside the permitted reconciliation",
#                 "bars settlement or simple hold"
#             ]):
#                 ra = "open_exception"
#             else:
#                 # When no strong signal → safer to escalate than to settle
#                 ra = "open_exception"
#         return {
#             "action": ra,
#             "facts": facts or {"vendorName": "unknown", "invoiceNumber": "unknown",
#                                "amountMinor": 0, "currency": "INR"},
#             "evidenceRefs": refs,
#             "reason": "Rule-based decision from the single controlling paragraph."
#         }

#     # No clean paragraph → safest default
#     all_refs = REF.findall(pkg_text(pkg))[:3]
#     return {
#         "action": "open_exception",
#         "facts": facts or {"vendorName": "unknown", "invoiceNumber": "unknown",
#                            "amountMinor": 0, "currency": "INR"},
#         "evidenceRefs": all_refs,
#         "reason": "Could not isolate a single controlling paragraph."
#     }
# async def llm_batch(pkgs, rev):
#     br = " (including the square brackets)" if KEEP_BRACKETS else " (without the square brackets)"
#     prompt = PROMPT.format(br=br, rev=rev, pk=json.dumps([with_hint(p) for p in pkgs], ensure_ascii=False))
#     async with httpx.AsyncClient(timeout=38) as c:
#         r = None
#         for use_json_mode in (True, False):
#             body = {"model": MODEL, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
#             if use_json_mode:
#                 body["response_format"] = {"type": "json_object"}
#             r = await c.post(AIPIPE_URL, headers={"Authorization": f"Bearer {AIPIPE_TOKEN}"}, json=body)
#             if r.status_code == 200:
#                 break
#             print("LLM HTTP", r.status_code, "model:", MODEL, "token set:", bool(AIPIPE_TOKEN),
#                   "body:", r.text[:300])
#             if r.status_code in (401, 403):
#                 break
#         r.raise_for_status()
#     txt = r.json()["choices"][0]["message"]["content"]
#     txt = re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip()
#     return {d.get("packageId"): d for d in json.loads(txt)["decisions"]}

# async def decide(pkgs, rev):
#     keys = [sha(canon(p)) for p in pkgs]
#     result, todo = {}, []
#     for p, k in zip(pkgs, keys):
#         row = db.execute("select decision from cache where key=?", (k,)).fetchone()
#         if row:
#             result[p["packageId"]] = json.loads(row[0])
#         else:
#             todo.append((p, k))
#     if todo:
#         try:
#             raw = await llm_batch([p for p, _ in todo], rev)
#         except Exception as e:
#             print("LLM error", e)
#             raw = {}
#         for p, k in todo:
#             dec = normalize(p, raw.get(p["packageId"], {}))
#             used_fallback = dec is None
#             if dec is None:
#                 print("FALLBACK for", p["packageId"], "model said:", json.dumps(raw.get(p["packageId"]))[:300])
#                 dec = fallback(p)
#             else:
#                 print("OK", p["packageId"], dec["action"], dec["evidenceRefs"])
#             # Final safety: never settle if strong negative signals exist
#             # Final safety net
#         triples, _ = analyze(p)
#         if len(triples) == 1:
#             low = triples[0][0].lower()
#             ra = rule_action(triples[0][0])
        
#             # Never settle if any strong negative signal exists
#             if dec["action"] == "settle_invoice":
#                 if any(k in low for k in [
#                     "ceiling", "approver", "callback", "beneficiary",
#                     "duplicate", "exception", "second disbursement",
#                     "already transferred", "reuses that original",
#                     "outside the permitted", "bars settlement"
#                 ]):
#                     if ra:
#                         print(f"SAFETY BLOCK settle → {ra}")
#                         dec["action"] = ra
#                     else:
#                         print("SAFETY BLOCK settle → open_exception")
#                         dec["action"] = "open_exception"
#             if not used_fallback:
#                 db.execute("insert or replace into cache values(?,?)", (k, json.dumps(dec)))
#             result[p["packageId"]] = dec
#     if len(pkgs) <= 8:
#         for p in pkgs:
#             t = analyze(p)[0]
#             print("AUDIT-DEBUG", p["packageId"], result[p["packageId"]]["action"], "|",
#                   (t[0][0][:450] if len(t) == 1 else "no-single-paragraph"))
#     print('ACTION MIX', dict(Counter(v['action'] for v in result.values())))
#     return result

# def build_rationale(dec):
#     f, refs = dec["facts"], dec["evidenceRefs"]
#     s = (f"Action {dec['action']} for invoice {f['invoiceNumber']} from {f['vendorName']}: {dec['reason']} "
#          f"Decisive evidence: {', '.join(refs)}.")
#     s = s[:1500]
#     return s if len(s) >= 60 else s + " This follows the policy paragraph cited above."

# # ----------------------------------------------------------------- app
# app = FastAPI()
# router = APIRouter()

# @app.get("/.well-known/agent-card.json")
# async def card():
#     return out(200, {
#         "name": "Invoice Action Agent",
#         "description": "Reads invoice packages, proposes one evidence-backed action each, and executes only approved actions.",
#         "version": "1.0.0",
#         "capabilities": {"streaming": False, "pushNotifications": False},
#         "supportedInterfaces": [{"url": BASE_URL, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}],
#         "defaultInputModes": [IN_MODE, "application/json"],
#         "defaultOutputModes": [PROP_MODE, RECEIPT_MODE],
#         "skills": [{"id": "invoice_action_agent", "name": "Invoice action agent",
#                     "description": "Chooses settle, approve, hold, reject-duplicate or exception for each invoice package.",
#                     "tags": ["invoice", "accounts-payable", "approval"]}],
#     })

# @app.get("/debug/llm")
# async def debug_llm():
#     info = {"model": MODEL, "token_set": bool(AIPIPE_TOKEN), "token_length": len(AIPIPE_TOKEN), "keep_brackets": KEEP_BRACKETS}
#     try:
#         async with httpx.AsyncClient(timeout=30) as c:
#             r = await c.post(AIPIPE_URL, headers={"Authorization": f"Bearer {AIPIPE_TOKEN}"},
#                              json={"model": MODEL, "max_tokens": 5,
#                                    "messages": [{"role": "user", "content": "Reply with the single word OK."}]})
#         info["status"] = r.status_code
#         info["reply"] = r.text[:200]
#     except Exception as e:
#         info["error"] = str(e)[:200]
#     return JSONResponse(info)

# @router.post("/message:send")
# async def message_send(request: Request):
#     principal, bad = guard(request, post=True)
#     if bad:
#         return bad
#     try:
#         body = await request.json()
#         msg = body["message"]
#         mid = str(msg["messageId"])
#         assert msg["role"] == "ROLE_USER" and isinstance(msg["parts"], list) and msg["parts"] and mid
#     except Exception:
#         return err(400, "Invalid request")
#     h = sha(canon(msg))
#     lock = locks.setdefault((principal, mid), asyncio.Lock())
#     async with lock:
#         row = db.execute("select hash, task_id from msgs where principal=? and message_id=?", (principal, mid)).fetchone()
#         if row:
#             if row[0] != h:
#                 return err(409, "Message ID reused with different content", "IDEMPOTENCY_CONFLICT")
#             task, _ = get_task(row[1], principal)
#             return out(200, {"task": task})
#         if msg.get("taskId"):
#             return handle_results(principal, msg, mid, h)
#         return await handle_batch(principal, msg, mid, h)

# async def handle_batch(principal, msg, mid, h):
#     part = next((p for p in msg["parts"] if p.get("mediaType") == IN_MODE and isinstance(p.get("data"), dict)), None)
#     if not part:
#         return err(400, "Missing invoice batch part")
#     data = part["data"]
#     pkgs = data.get("packages")
#     if not data.get("batchId") or not isinstance(pkgs, list) or not pkgs \
#             or any(not isinstance(p, dict) or not p.get("packageId") for p in pkgs) \
#             or len({p["packageId"] for p in pkgs}) != len(pkgs):
#         return err(400, "Invalid batch")
#     try:
#         with open("samples.jsonl", "a") as f:
#             f.write(canon(msg)[:60000] + "\n")
#     except Exception:
#         pass
#     decisions = await decide(pkgs, data.get("policyRevision", ""))
#     proposals = []
#     for p in pkgs:
#         d = decisions[p["packageId"]]
#         proposals.append({
#             "packageId": p["packageId"],
#             "actionId": "act_" + sha(f"{principal}|{data['batchId']}|{p['packageId']}|{d['action']}")[:24],
#             "action": d["action"],
#             "facts": d["facts"],
#             "evidenceRefs": fmt(d["evidenceRefs"]),
#             "rationale": build_rationale(dict(d, evidenceRefs=fmt(d["evidenceRefs"])))
#         })
#     tid, ctx = str(uuid.uuid4()), msg.get("contextId") or str(uuid.uuid4())
#     hist = dict(msg, taskId=tid, contextId=ctx)
#     task = {
#         "id": tid,
#         "contextId": ctx,
#         "status": {"state": "TASK_STATE_INPUT_REQUIRED", "timestamp": now()},
#         "artifacts": [{
#             "artifactId": "proposals-" + tid[:8],
#             "name": "Invoice action proposals",
#             "parts": [{"mediaType": PROP_MODE, "data": {"batchId": data["batchId"], "proposals": proposals}}]
#         }],
#         "history": [hist]
#     }
#     save_task(principal, task, {"batchId": data["batchId"], "proposals": proposals})
#     db.execute("insert into msgs values(?,?,?,?)", (principal, mid, h, tid))
#     return out(200, {"task": task})

# def handle_results(principal, msg, mid, h):
#     task, stored = get_task(str(msg["taskId"]), principal)
#     if not task:
#         return err(404, "Task not found")
#     if msg.get("contextId") != task["contextId"]:
#         return err(400, "Context mismatch")
#     if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
#         return err(409, "Task is not accepting results")
#     part = next((p for p in msg["parts"] if p.get("mediaType") == RESULT_MODE and isinstance(p.get("data"), dict)), None)
#     if not part or part["data"].get("batchId") != stored["batchId"] or not isinstance(part["data"].get("results"), list):
#         return err(400, "Invalid results")
#     by_pkg = {p["packageId"]: p for p in stored["proposals"]}
#     seen, executions = set(), []
#     for r in part["data"]["results"]:
#         p = by_pkg.get(r.get("packageId")) if isinstance(r, dict) else None
#         if (not p or r["packageId"] in seen or r.get("actionId") != p["actionId"] or r.get("action") != p["action"]
#                 or r.get("outcome") not in ("ACCEPTED", "REJECTED")):
#             return err(400, "Result does not match the stored proposal")
#         seen.add(r["packageId"])
#         if r["outcome"] == "ACCEPTED":
#             if not isinstance(r.get("receiptNonce"), str) or not r["receiptNonce"]:
#                 return err(400, "Missing receipt")
#             executions.append({
#                 "packageId": p["packageId"],
#                 "actionId": p["actionId"],
#                 "action": p["action"],
#                 "receiptNonce": r["receiptNonce"],
#                 "facts": p["facts"],
#                 "evidenceRefs": p["evidenceRefs"]
#             })
#     task["history"].append(dict(msg, taskId=task["id"], contextId=task["contextId"]))
#     task["artifacts"].append({
#         "artifactId": "receipts-" + task["id"][:8],
#         "name": "Invoice action receipts",
#         "parts": [{"mediaType": RECEIPT_MODE, "data": {"batchId": stored["batchId"], "executions": executions}}]
#     })
#     task["status"] = {"state": "TASK_STATE_COMPLETED", "timestamp": now()}
#     save_task(principal, task, stored)
#     db.execute("insert into msgs values(?,?,?,?)", (principal, mid, h, task["id"]))
#     return out(200, {"task": task})

# @router.get("/tasks/{ref}")
# async def task_get(ref: str, request: Request):
#     principal, bad = guard(request)
#     if bad:
#         return bad
#     task, _ = get_task(ref, principal)
#     return out(200, task) if task else err(404, "Task not found")

# @router.get("/tasks")
# async def task_list(request: Request):
#     principal, bad = guard(request)
#     if bad:
#         return bad
#     rows = db.execute("select body from tasks where principal=? order by created", (principal,)).fetchall()
#     return out(200, {"tasks": [json.loads(r[0]) for r in rows]})

# @router.post("/tasks/{ref}")
# async def task_cancel(ref: str, request: Request):
#     principal, bad = guard(request, post=True)
#     if bad:
#         return bad
#     if not ref.endswith(":cancel"):
#         return err(404, "Not found")
#     task, stored = get_task(ref[:-7], principal)
#     if not task:
#         return err(404, "Task not found")
#     if task["status"]["state"] in TERMINAL:
#         return err(409, "Task already finished")
#     task["status"] = {"state": "TASK_STATE_CANCELED", "timestamp": now()}
#     save_task(principal, task, stored)
#     return out(200, task)

# app.include_router(router, prefix=BASE_PATH)
import os, json, hashlib, sqlite3, threading, asyncio, uuid, re, datetime
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import traceback

BASE_URL = "https://luck-fiber-few-bus.trycloudflare.com/a2a/"
LLM_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
LLM_KEY = "GEMINI_API_KEY"
LLM_MODEL = "gemini-3.5-flash-lite"
MEDIA = "application/a2a+json"
BATCH_T = "application/vnd.ga5.invoice-claim-batch+json"
PROP_T = "application/vnd.ga5.invoice-action-proposals+json"
RES_T = "application/vnd.ga5.invoice-action-results+json"
REC_T = "application/vnd.ga5.invoice-action-receipts+json"
ACTIONS = {"settle_invoice", "request_approval", "hold_invoice", "reject_duplicate", "open_exception"}
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"}

app_Q2 = FastAPI()
db = sqlite3.connect("state.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, principal TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS msgs(principal TEXT, mid TEXT, hash TEXT, task_id TEXT, PRIMARY KEY(principal, mid));
CREATE TABLE IF NOT EXISTS cache(h TEXT PRIMARY KEY, body TEXT);
""")
dblock = threading.RLock()      # makes "receipt vs cancel" a fair one-at-a-time race
key_locks = {}                  # makes concurrent duplicate messages wait for the first one

def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def canon(o): return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
def sha(s): return hashlib.sha256(s.encode()).hexdigest()
def reply(obj, code=200): return JSONResponse(obj, status_code=code, media_type=MEDIA)
def err(code, name, msg="Request failed"): return reply({"error": {"code": name, "message": msg}}, code)

# ---------- gatekeeping ----------
def gate(req: Request, post=False):
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not auth[7:].strip():
        return None, err(401, "UNAUTHENTICATED", "Authentication required")
    if req.headers.get("a2a-version") != "1.0":
        return None, err(400, "VERSION_NOT_SUPPORTED", "Unsupported version")
    if post and not req.headers.get("content-type", "").startswith(MEDIA):
        return None, err(415, "UNSUPPORTED_MEDIA_TYPE", "Use application/a2a+json")
    return sha(auth[7:].strip()), None      # principal = hash of the token

# ---------- storage ----------
def load(tid, principal):
    r = db.execute("SELECT body FROM tasks WHERE id=? AND principal=?", (tid, principal)).fetchone()
    return json.loads(r[0]) if r else None
def save(task, principal):
    db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?)", (task["id"], principal, json.dumps(task)))

# ---------- public Agent Card ----------
@app_Q2.get("/.well-known/agent-card.json")
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

# ---------- AI layer (separate from protocol code) ----------
PROMPT = """You are an accounts-payable analyst. For EACH package choose exactly one action:
settle_invoice, request_approval, hold_invoice, reject_duplicate, open_exception.

Decision rules, apply in this order:
1. The SAME invoice (same vendor AND same invoice number) was already paid -> reject_duplicate.
   A payment for a DIFFERENT invoice number is NOT a duplicate.
2. Vendor, amount, PO or receipt records materially conflict -> open_exception.
3. A named verification (bank details, tax ID, goods receipt, etc.) must finish before payment -> hold_invoice.
4. Amount exceeds the autonomous approval limit that applies to this case -> request_approval.
5. ONLY if none of the above apply -> settle_invoice.

How to read the documents:
- Find the ONE paragraph that states the controlling facts of THIS case. Return its three bracketed references,
  copied exactly, brackets included, all from that single paragraph.
- Ignore the cover sheet, archive examples, training material and decoys.
- Negated statements ("not a duplicate", "no hold required") mean that action does NOT apply.
- When unsure between settle_invoice and anything else, do NOT choose settle_invoice.

Also return vendorName, invoiceNumber, amountMinor (integer, smallest currency unit, e.g. 1234.50 -> 123450),
currency, and "reason": one sentence on why the evidence supports the action.
Reply ONLY JSON: {"decisions":[{"packageId":"","action":"","vendorName":"","invoiceNumber":"","amountMinor":0,
"currency":"","evidenceRefs":["","",""],"reason":""}]}"""

CHECKS = ["valid_and_reconciled", "within_authority", "no_pending_verification",
          "not_already_paid", "no_conflicting_records"]

VERIFY = """A colleague proposed settle_invoice for the invoice package below. Be a skeptical reviewer.
Read the paragraph that states the controlling facts of THIS case (ignore cover sheets, archive examples,
training decoys, and negated statements). Answer each check with true or false:
- valid_and_reconciled: invoice, PO and receipt records agree
- within_authority: amount is within the autonomous approval limit that applies
- no_pending_verification: nothing must be verified before payment
- not_already_paid: this exact invoice (same vendor AND invoice number) has not already been paid
- no_conflicting_records: no material conflict between records
Then give the correct action (settle_invoice, request_approval, hold_invoice, reject_duplicate or open_exception)
and the three exact bracketed references from the single controlling paragraph.
Reply ONLY JSON: {"checks":{"valid_and_reconciled":true,"within_authority":true,"no_pending_verification":true,
"not_already_paid":true,"no_conflicting_records":true},"action":"","evidenceRefs":["","",""],"reason":""}"""

def refs_ok(refs, pkg_text):
    return (isinstance(refs, list) and len(refs) == 3 and len(set(refs)) == 3
            and all(isinstance(r, str) and re.fullmatch(r"\[[^\[\]]+\]", r) and r in pkg_text for r in refs))

def valid(d, pkg_text):
    return (d.get("action") in ACTIONS and refs_ok(d.get("evidenceRefs"), pkg_text)
            and isinstance(d.get("amountMinor"), int)
            and isinstance(d.get("vendorName"), str) and d["vendorName"]
            and isinstance(d.get("invoiceNumber"), str) and d["invoiceNumber"])

def settle_confirmed(v, pkg_text):
    """Python, not the AI, makes the final call: every safety check must be exactly True."""
    c = v.get("checks") or {}
    return (v.get("action") == "settle_invoice"
            and all(c.get(k) is True for k in CHECKS)
            and refs_ok(v.get("evidenceRefs"), pkg_text))

async def verify_settle(policy, p, sem):
    async with sem:
        try:
            return await call_llm(VERIFY, f"Policy revision: {policy}\nPackage:\n{canon(p)}", 15)
        except Exception:
            traceback.print_exc()
            return None            # no answer means we will NOT settle

def log_line(obj):
    with open("decisions.jsonl", "a") as f:
        f.write(canon(obj) + "\n")

async def call_llm(system, user, timeout):
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(
            LLM_URL.format(model=LLM_MODEL),
            headers={"Content-Type": "application/json", "X-goog-api-key": LLM_KEY},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{
                    "role": "user", 
                    "parts": [{"text": user}]
                }],
                "generationConfig": {
                    "temperature": 0, 
                    "responseMimeType": "application/json"
                }
            })
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    s, e = text.find("{"), text.rfind("}")
    return json.loads(text[s:e + 1])

async def ask_llm(policy, pkgs):
    res = await call_llm(PROMPT, f"Policy revision: {policy}\nPackages:\n{canon(pkgs)}", 25)
    return res["decisions"]

async def ask_llm_safe(policy, pkgs):
    try:
        return [d for d in await ask_llm(policy, pkgs) if isinstance(d, dict)]
    except Exception:
        traceback.print_exc()
        return []

CACHE_VERSION = "v3"     # bump this number every time you change a prompt or rule

def pkg_key(p):
    body = {k: v for k, v in p.items() if k != "packageId"}
    return sha(CACHE_VERSION + canon(body))

async def decide(policy, packages):
    out, todo, fresh = {}, [], {}
    for p in packages:
        r = db.execute("SELECT body FROM cache WHERE h=?", (pkg_key(p),)).fetchone()
        if r: out[p["packageId"]] = json.loads(r[0])
        else: todo.append(p)
    pending = list(todo)

    # first pass (one retry for anything invalid)
    for attempt in range(2):
        if not todo: break
        got = {d.get("packageId"): d for d in await ask_llm_safe(policy, todo)}
        still = []
        for p in todo:
            d = got.get(p["packageId"])
            if d and valid(d, canon(p)): fresh[p["packageId"]] = d
            else: still.append(p)
        todo = still

    # second pass: verify every settle_invoice
    by_id = {p["packageId"]: p for p in pending}
    settles = [by_id[i] for i, d in fresh.items() if d["action"] == "settle_invoice"]
    sem = asyncio.Semaphore(4)
    verdicts = await asyncio.gather(*[verify_settle(policy, p, sem) for p in settles])
    verdict_of = {p["packageId"]: v for p, v in zip(settles, verdicts)}

    for p in pending:
        pid, text = p["packageId"], canon(p)
        d = fresh.get(pid)
        cacheable = True
        if d is None:                                   # AI failed twice
            d = {"action": "open_exception", "vendorName": "unknown", "invoiceNumber": "unknown",
                 "amountMinor": 0, "currency": "INR", "evidenceRefs": ["", "", ""], "reason": ""}
            cacheable = False
        elif d["action"] == "settle_invoice":
            v = verdict_of.get(pid)
            if v is None:                               # verifier failed -> never settle
                d = {**d, "action": "open_exception"}
                cacheable = False
            elif settle_confirmed(v, text):
                d = {**d, "reason": v.get("reason") or d.get("reason", "")}   # confirmed
            elif v.get("action") in ACTIONS - {"settle_invoice"} and refs_ok(v.get("evidenceRefs"), text):
                d = {**d, "action": v["action"], "evidenceRefs": v["evidenceRefs"], "reason": v.get("reason", "")}
            else:                                       # disagreement we can't resolve
                d = {**d, "action": "open_exception"}
        out[pid] = d
        log_line({"packageId": pid, "first": (fresh.get(pid) or {}).get("action"),
                  "final": d["action"], "refs": d["evidenceRefs"], "verdict": verdict_of.get(pid)})
        if d["action"] == "settle_invoice":             # keep the text of every settle so you can read it
            log_line({"SETTLED_PACKAGE": pid, "text": text})
        if cacheable:
            db.execute("INSERT OR REPLACE INTO cache VALUES(?,?)", (pkg_key(p), json.dumps(d)))
    db.commit()
    return out

def proposal(p, d, policy):
    a, r = d["action"], d["evidenceRefs"]
    rationale = (f"Proposing {a} for invoice {d['invoiceNumber']} from {d['vendorName']}. "
                 f"{d.get('reason', '')} Evidence {r[0]}, {r[1]} and {r[2]} in the controlling paragraph support {a}.")[:1500]
    return {"packageId": p["packageId"], "actionId": "act-" + uuid.uuid4().hex, "action": a,
            "facts": {"vendorName": d["vendorName"], "invoiceNumber": d["invoiceNumber"],
                      "amountMinor": d["amountMinor"], "currency": d["currency"]},
            "evidenceRefs": r, "rationale": rationale}

# ---------- message:send ----------
@app_Q2.post("/a2a/message:send")
async def message_send(req: Request):
    principal, bad = gate(req, post=True)
    if bad: return bad
    try:
        body = await req.json(); msg = body["message"]; mid = msg["messageId"]
        assert isinstance(msg["parts"], list)
    except Exception:
        return err(400, "INVALID_REQUEST")
    h = sha(canon(msg))                         # configuration is ignored on purpose
    lock = key_locks.setdefault((principal, mid), asyncio.Lock())
    async with lock:
        with dblock:
            seen = db.execute("SELECT hash, task_id FROM msgs WHERE principal=? AND mid=?", (principal, mid)).fetchone()
        if seen:
            if seen[0] != h: return err(409, "IDEMPOTENCY_CONFLICT", "Message id reused with different content")
            return reply({"task": load(seen[1], principal)})
        part = msg["parts"][0]
        if msg.get("taskId"):
            return continuation(principal, msg, part, mid, h)
        if part.get("mediaType") != BATCH_T: return err(400, "INVALID_REQUEST")
        data = part["data"]; pkgs = data["packages"]
        decisions = await decide(data["policyRevision"], pkgs)       # the only AI call
        props = [proposal(p, decisions[p["packageId"]], data["policyRevision"]) for p in pkgs]
        tid, cid = str(uuid.uuid4()), str(uuid.uuid4())
        task = {"id": tid, "contextId": cid,
                "status": {"state": "TASK_STATE_INPUT_REQUIRED", "timestamp": now()},
                "artifacts": [{"artifactId": "proposals", "name": "invoice-action-proposals",
                               "parts": [{"mediaType": PROP_T, "data": {"batchId": data["batchId"], "proposals": props}}]}],
                "history": [{**msg, "taskId": tid, "contextId": cid}]}
        with dblock:                                                   # store BEFORE responding
            save(task, principal)
            db.execute("INSERT INTO msgs VALUES(?,?,?,?)", (principal, mid, h, tid))
            db.commit()
        return reply({"task": task})

def continuation(principal, msg, part, mid, h):
    with dblock:
        task = load(msg["taskId"], principal)
        if not task: return err(404, "NOT_FOUND", "Task not found")
        if msg.get("contextId") != task["contextId"]: return err(400, "INVALID_REQUEST")
        if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED": return err(409, "TASK_NOT_ACTIONABLE")
        if part.get("mediaType") != RES_T: return err(400, "INVALID_REQUEST")
        stored = task["artifacts"][0]["parts"][0]["data"]
        data = part["data"]
        if data.get("batchId") != stored["batchId"]: return err(400, "INVALID_REQUEST")
        byid = {p["packageId"]: p for p in stored["proposals"]}
        execs, seen = [], set()
        for r in data.get("results", []):
            p = byid.get(r.get("packageId"))
            if (not p or r["packageId"] in seen or r.get("actionId") != p["actionId"]
                    or r.get("action") != p["action"] or r.get("outcome") not in ("ACCEPTED", "REJECTED")):
                return err(400, "INVALID_REQUEST")
            seen.add(r["packageId"])
            if r["outcome"] == "ACCEPTED":
                if not r.get("receiptNonce"): return err(400, "INVALID_REQUEST")
                execs.append({"packageId": p["packageId"], "actionId": p["actionId"], "action": p["action"],
                              "receiptNonce": r["receiptNonce"], "facts": p["facts"], "evidenceRefs": p["evidenceRefs"]})
        task["artifacts"].append({"artifactId": "receipts", "name": "invoice-action-receipts",
                                  "parts": [{"mediaType": REC_T, "data": {"batchId": stored["batchId"], "executions": execs}}]})
        task["history"].append({**msg})
        task["status"] = {"state": "TASK_STATE_COMPLETED", "timestamp": now()}
        save(task, principal)
        db.execute("INSERT INTO msgs VALUES(?,?,?,?)", (principal, mid, h, task["id"]))
        db.commit()
        return reply({"task": task})

# ---------- reads, list, cancel (NO model calls) ----------
@app_Q2.get("/a2a/tasks/{tid}")
def get_task(tid: str, req: Request):
    principal, bad = gate(req)
    if bad: return bad
    with dblock: t = load(tid, principal)
    return reply(t) if t else err(404, "NOT_FOUND", "Task not found")

MAX_LIST_BYTES = 350_000

@app_Q2.get("/a2a/tasks")
def list_tasks(req: Request):
    principal, bad = gate(req)
    if bad: return bad
    with dblock:
        rows = db.execute(
            "SELECT body FROM tasks WHERE principal=? ORDER BY rowid DESC LIMIT 20",
            (principal,)).fetchall()
    tasks, size = [], 0
    for (b,) in rows:
        t = {k: v for k, v in json.loads(b).items() if k != "history"}   # drop the huge history
        n = len(json.dumps(t))
        if tasks and size + n > MAX_LIST_BYTES:
            break
        tasks.append(t)
        size += n
    return reply({"tasks": tasks})

@app_Q2.post("/a2a/tasks/{tid}:cancel")
def cancel(tid: str, req: Request):
    principal, bad = gate(req)
    if bad: return bad
    with dblock:
        t = load(tid, principal)
        if not t: return err(404, "NOT_FOUND", "Task not found")
        if t["status"]["state"] in TERMINAL: return err(409, "TASK_NOT_CANCELABLE")
        t["status"] = {"state": "TASK_STATE_CANCELED", "timestamp": now()}
        save(t, principal); db.commit()
    return reply(t)
