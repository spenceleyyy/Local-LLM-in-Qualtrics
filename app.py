"""
Proxy server between a Qualtrics survey and a local LLM running in Ollama.

What it does
  - Receives one participant message at a time from the Qualtrics page.
  - Keeps the authoritative conversation history on the server (the browser
    only sends the new message), so participants can't edit past turns or
    see/alter the system prompt.
  - Injects the on-screen context into the system prompt.
  - Calls Ollama (Qwen3.5 9B by default) and returns the reply.
  - Logs every turn to SQLite, keyed by Qualtrics ResponseID.

Run:
  pip install -r requirements.txt
  uvicorn app:app --host 127.0.0.1 --port 8000
"""

import asyncio
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import prompts

# --------------------------------------------------------------------------
# Configuration (override with environment variables)
# --------------------------------------------------------------------------
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.getenv("LLM_MODEL", "qwen3.5:9b")
DB_PATH = os.getenv("DB_PATH", "chat_logs.sqlite3")

# Fixed generation settings, recorded with every turn for reproducibility.
# Low temperature keeps the manipulation consistent across participants.
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
TOP_P = float(os.getenv("LLM_TOP_P", "0.9"))
TOP_K = int(os.getenv("LLM_TOP_K", "20"))
# Qwen ships with a non-zero presence penalty; set it explicitly so it is
# recorded rather than inherited silently from the model file.
PRESENCE_PENALTY = float(os.getenv("LLM_PRESENCE_PENALTY", "0.5"))
REPEAT_PENALTY = float(os.getenv("LLM_REPEAT_PENALTY", "1.1"))
SEED = int(os.getenv("LLM_SEED", "42"))
NUM_CTX = int(os.getenv("LLM_NUM_CTX", "8192"))
MAX_REPLY_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "220"))

# Word budget written into both condition prompts. Identical by design.
WORD_MIN = int(os.getenv("REPLY_WORD_MIN", "60"))
WORD_MAX = int(os.getenv("REPLY_WORD_MAX", "90"))
ITEMS_PATH = os.getenv("ITEMS_PATH", "items.json")
# Qwen3.5 is a "thinking" model. Turning thinking off gives much faster,
# cleaner replies for a chat task. Set LLM_THINK=true to enable it.
THINK = os.getenv("LLM_THINK", "false").lower() == "true"

MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "2000"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "6000"))
MAX_TURNS = int(os.getenv("MAX_TURNS", "30"))
REQUEST_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_S", "120"))
# How many generations may run at once. A single GPU is usually happiest at
# 1-4; extra requests wait in line instead of all slowing down together.
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))

# Optional shared key. It sits in the survey JavaScript, so it only stops
# casual misuse of your endpoint, not a determined person.
API_KEY = os.getenv("API_KEY", "")

# Qualtrics survey pages are served from *.qualtrics.com
ALLOWED_ORIGIN_REGEX = os.getenv(
    "ALLOWED_ORIGIN_REGEX", r"https://([a-z0-9-]+\.)*qualtrics\.com"
)

# --------------------------------------------------------------------------
# Item bank (question text, options, and which answer the model argues for)
# --------------------------------------------------------------------------
def load_items():
    try:
        with open(ITEMS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as e:
        print(f"WARNING: could not load {ITEMS_PATH}: {e}")
        return {}


ITEMS = load_items()


def pick_target(item, prior_answer):
    """Which answer the model will argue for, given what the participant said."""
    targets = item.get("targets") or {}
    key = (prior_answer or "").strip()
    if key in targets:
        return targets[key]
    for k, v in targets.items():                    # case-insensitive retry
        if k.strip().lower() == key.lower():
            return v
    return item.get("default_target", "")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
db_lock = asyncio.Lock()
gen_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


def db_connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id     TEXT PRIMARY KEY,
            response_id    TEXT,
            survey_id      TEXT,
            condition      TEXT,
            page_context   TEXT,
            system_prompt  TEXT,
            created_at     TEXT,
            item_id        TEXT,
            prior_answer   TEXT,
            target_answer  TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     TEXT,
            response_id    TEXT,
            turn           INTEGER,
            role           TEXT,
            content        TEXT,
            created_at     TEXT,
            latency_ms     INTEGER,
            model          TEXT,
            gen_settings   TEXT,
            prompt_tokens  INTEGER,
            reply_tokens   INTEGER,
            error          TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_msg_response ON messages(response_id);
        """
    )
    # Upgrade older databases: store what was on screen at each user turn.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
    if "page_context" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN page_context TEXT")
    if "word_count" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN word_count INTEGER")
    scols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
    for c in ("item_id", "prior_answer", "target_answer"):
        if c not in scols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {c} TEXT")
    conn.commit()
    return conn


conn: Optional[sqlite3.Connection] = None
http: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global conn, http
    conn = db_connect()
    http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
    yield
    await http.aclose()
    conn.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_methods=["POST", "GET", "OPTIONS"],
    # Allow any request header. Listing them explicitly means one stray
    # header in the survey JavaScript makes every request fail CORS with
    # a bare "Load failed" in the browser and nothing in this log at all.
    allow_headers=["*"],
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def gen_settings():
    return {
        "model": MODEL,
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "top_k": TOP_K,
        "presence_penalty": PRESENCE_PENALTY,
        "repeat_penalty": REPEAT_PENALTY,
        "seed": SEED,
        "num_ctx": NUM_CTX,
        "num_predict": MAX_REPLY_TOKENS,
        "think": THINK,
        "word_budget": [WORD_MIN, WORD_MAX],
    }


# --------------------------------------------------------------------------
# API models
# --------------------------------------------------------------------------
class ChatIn(BaseModel):
    response_id: str = Field(..., max_length=100)
    session_id: Optional[str] = Field(None, max_length=100)
    survey_id: Optional[str] = Field("", max_length=100)
    condition: Optional[str] = Field("", max_length=200)
    item_id: Optional[str] = Field("", max_length=100)
    prior_answer: Optional[str] = Field("", max_length=1000)
    page_context: Optional[str] = ""
    message: str


class ChatOut(BaseModel):
    session_id: str
    reply: str
    turn: int
    latency_ms: int
    model: str
    condition: str


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/health")
async def health():
    try:
        r = await http.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        return {
            "ok": True,
            "model": MODEL,
            "model_available": MODEL in models,
            "items_loaded": sorted(ITEMS.keys()),
            "conditions": sorted(prompts.CONDITIONS.keys()),
            "settings": gen_settings(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/chat", response_model=ChatOut)
async def chat(body: ChatIn, request: Request):
    if API_KEY and request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(401, "Invalid key")

    message = body.message.strip()
    if not message:
        raise HTTPException(400, "Empty message")
    message = message[:MAX_MESSAGE_CHARS]

    # ---- find or create the session ----
    # The page context is sent with every message, so the model always sees
    # the participant's *current* answers, not just what was there at first.
    page_context = (body.page_context or "")[:MAX_CONTEXT_CHARS]
    session_id = body.session_id or str(uuid.uuid4())
    user_ts = now_iso()
    async with db_lock:
        row = conn.execute(
            "SELECT response_id, condition, page_context, item_id, "
            "prior_answer, target_answer FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()

        if row is None:
            # First message of this chat: fix the condition, the item and the
            # answer the model will argue for, and store them. Everything that
            # defines the manipulation is pinned here and never recomputed, so
            # the participant cannot change it mid-conversation.
            item_id = (body.item_id or "").strip()
            item = ITEMS.get(item_id, {})
            prior_answer = (body.prior_answer or "").strip()
            target_answer = pick_target(item, prior_answer)
            condition, system_prompt = prompts.build(
                body.condition, item, prior_answer, target_answer,
                WORD_MIN, WORD_MAX,
            )
            conn.execute(
                "INSERT INTO sessions (session_id, response_id, survey_id,"
                " condition, page_context, system_prompt, created_at,"
                " item_id, prior_answer, target_answer)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session_id, body.response_id, body.survey_id, condition,
                 page_context, system_prompt, now_iso(),
                 item_id, prior_answer, target_answer),
            )
            conn.commit()
        else:
            (stored_rid, condition, first_context,
             item_id, prior_answer, target_answer) = row
            if stored_rid != body.response_id:
                raise HTTPException(403, "Session does not match response")
            if not page_context:          # older widget versions: keep first
                page_context = first_context
            item = ITEMS.get(item_id or "", {})
            condition, system_prompt = prompts.build(
                condition, item, prior_answer, target_answer,
                WORD_MIN, WORD_MAX,
            )

        history = conn.execute(
            "SELECT role, content FROM messages "
            "WHERE session_id=? AND error IS NULL ORDER BY id",
            (session_id,),
        ).fetchall()

    turn = sum(1 for r, _ in history if r == "user") + 1
    if turn > MAX_TURNS:
        raise HTTPException(429, "Turn limit reached")

    messages = [{"role": "system", "content": system_prompt}]
    if page_context:
        messages.append({
            "role": "system",
            "content": ("What the participant currently has on screen "
                        "(updated each message):\n" + page_context),
        })
    messages += [{"role": r, "content": c} for r, c in history]
    messages.append({"role": "user", "content": message})

    # ---- call the model ----
    settings = gen_settings()
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "think": THINK,
        "options": {
            "temperature": TEMPERATURE,
            "top_p": TOP_P,
            "top_k": TOP_K,
            "presence_penalty": PRESENCE_PENALTY,
            "repeat_penalty": REPEAT_PENALTY,
            "seed": SEED,
            "num_ctx": NUM_CTX,
            "num_predict": MAX_REPLY_TOKENS,
        },
    }

    t0 = time.perf_counter()
    reply, err, p_tok, r_tok = "", None, None, None
    try:
        async with gen_semaphore:
            r = await http.post(f"{OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
        reply = data.get("message", {}).get("content", "")
        # Safety net in case thinking text leaks into the content.
        reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S).strip()
        p_tok = data.get("prompt_eval_count")
        r_tok = data.get("eval_count")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    latency_ms = int((time.perf_counter() - t0) * 1000)

    # ---- log both sides of the turn ----
    async with db_lock:
        conn.execute(
            "INSERT INTO messages (session_id, response_id, turn, role, content,"
            " created_at, error, page_context) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, body.response_id, turn, "user", message, user_ts, err,
             page_context),
        )
        conn.execute(
            "INSERT INTO messages (session_id, response_id, turn, role, content,"
            " created_at, latency_ms, model, gen_settings, prompt_tokens,"
            " reply_tokens, error, word_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, body.response_id, turn, "assistant", reply, now_iso(),
             latency_ms, MODEL, json.dumps(settings), p_tok, r_tok, err,
             len(reply.split()) if reply else 0),
        )
        conn.commit()

    if err or not reply:
        # Failed turns are logged but excluded from future history.
        raise HTTPException(502, "The model did not respond")

    return ChatOut(session_id=session_id, reply=reply, turn=turn,
                   latency_ms=latency_ms, model=MODEL, condition=condition)
