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

# --------------------------------------------------------------------------
# Configuration (override with environment variables)
# --------------------------------------------------------------------------
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.getenv("LLM_MODEL", "qwen3.5:9b")
DB_PATH = os.getenv("DB_PATH", "chat_logs.sqlite3")

# Fixed generation settings, recorded with every turn for reproducibility.
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.7"))
TOP_P = float(os.getenv("LLM_TOP_P", "0.9"))
SEED = int(os.getenv("LLM_SEED", "42"))
NUM_CTX = int(os.getenv("LLM_NUM_CTX", "8192"))
MAX_REPLY_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "400"))
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

SYSTEM_PROMPT_TEMPLATE = """You are a helpful assistant embedded in a research survey.
Keep replies short (2-4 sentences) and in plain language.
Do not claim to be a human. Do not ask for personal identifying information.

The participant is currently looking at this survey page:
---
{page_context}
---
Experimental condition: {condition}
"""

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
            created_at     TEXT
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
    allow_headers=["Content-Type", "X-API-Key"],
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def gen_settings():
    return {
        "model": MODEL,
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "seed": SEED,
        "num_ctx": NUM_CTX,
        "num_predict": MAX_REPLY_TOKENS,
        "think": THINK,
    }


# --------------------------------------------------------------------------
# API models
# --------------------------------------------------------------------------
class ChatIn(BaseModel):
    response_id: str = Field(..., max_length=100)
    session_id: Optional[str] = Field(None, max_length=100)
    survey_id: Optional[str] = Field("", max_length=100)
    condition: Optional[str] = Field("", max_length=200)
    page_context: Optional[str] = ""
    message: str


class ChatOut(BaseModel):
    session_id: str
    reply: str
    turn: int
    latency_ms: int
    model: str


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/health")
async def health():
    try:
        r = await http.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        return {"ok": True, "model": MODEL, "model_available": MODEL in models}
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
    session_id = body.session_id or str(uuid.uuid4())
    async with db_lock:
        row = conn.execute(
            "SELECT system_prompt, response_id FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()

        if row is None:
            page_context = (body.page_context or "")[:MAX_CONTEXT_CHARS]
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                page_context=page_context or "(no page context provided)",
                condition=body.condition or "none",
            )
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                (session_id, body.response_id, body.survey_id, body.condition,
                 page_context, system_prompt, now_iso()),
            )
            conn.commit()
        else:
            system_prompt, stored_rid = row
            if stored_rid != body.response_id:
                raise HTTPException(403, "Session does not match response")

        history = conn.execute(
            "SELECT role, content FROM messages "
            "WHERE session_id=? AND error IS NULL ORDER BY id",
            (session_id,),
        ).fetchall()

    turn = sum(1 for r, _ in history if r == "user") + 1
    if turn > MAX_TURNS:
        raise HTTPException(429, "Turn limit reached")

    messages = [{"role": "system", "content": system_prompt}]
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
    user_ts = now_iso()
    async with db_lock:
        conn.execute(
            "INSERT INTO messages (session_id, response_id, turn, role, content,"
            " created_at, error) VALUES (?,?,?,?,?,?,?)",
            (session_id, body.response_id, turn, "user", message, user_ts, err),
        )
        conn.execute(
            "INSERT INTO messages (session_id, response_id, turn, role, content,"
            " created_at, latency_ms, model, gen_settings, prompt_tokens,"
            " reply_tokens, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, body.response_id, turn, "assistant", reply, now_iso(),
             latency_ms, MODEL, json.dumps(settings), p_tok, r_tok, err),
        )
        conn.commit()

    if err or not reply:
        # Failed turns are logged but excluded from future history.
        raise HTTPException(502, "The model did not respond")

    return ChatOut(session_id=session_id, reply=reply, turn=turn,
                   latency_ms=latency_ms, model=MODEL)
