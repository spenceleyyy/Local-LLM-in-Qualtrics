# Qwen3.5 9B chat inside a Qualtrics survey

```
Participant's browser (Qualtrics page)
        │  HTTPS  POST /chat  {response_id, message, page_context}
        ▼
Public HTTPS URL (Cloudflare Tunnel / ngrok / university server)
        ▼
server/app.py  (FastAPI: system prompt, history, logging → SQLite)
        ▼
Ollama on localhost:11434  (qwen3.5:9b)
```

## 1. Run the model

Install Ollama (https://ollama.com), then:

```bash
ollama pull qwen3.5:9b        # about 6.6 GB
ollama run qwen3.5:9b "hello" # quick test, then /bye
```

A GPU with 8 GB+ VRAM or an Apple Silicon Mac is recommended. On CPU only,
replies can take 20+ seconds.

## 2. Run the proxy server

```bash
cd server
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export API_KEY="pick-a-random-string"               # optional
uvicorn app:app --host 127.0.0.1 --port 8000
```

Check it at http://127.0.0.1:8000/health. You should see `"model_available": true`.

Settings are environment variables at the top of `app.py`: temperature, seed,
max reply length, turn limits, concurrency, and whether Qwen's thinking mode is
on (off by default, which is much faster). Edit `SYSTEM_PROMPT_TEMPLATE` in
`app.py` to set the assistant's instructions for your study.

## 3. Give it a public HTTPS address

Qualtrics runs in the participant's browser, so it can't reach your `localhost`.
Quickest option for piloting:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

It prints a URL like `https://something-random.trycloudflare.com`. Quick tunnel
URLs change each restart, so for the real study use a named Cloudflare tunnel,
ngrok with a reserved domain, or a server with a fixed address.

## 4. Set up the survey

1. **Survey Flow:** add an Embedded Data element at the top with these fields
   (leave values blank). Create both versions of each; which one fills depends on
   your survey's layout, and the other will just stay empty:
   - `chat_log`, `chat_turns`, `chat_session_id`, `chat_page_seconds`
   - `__js_chat_log`, `__js_chat_turns`, `__js_chat_session_id`, `__js_chat_page_seconds`
   - `condition` if you randomize conditions (optional; set it with a Randomizer)
2. **Chat question:** add a Text/Graphic question. Its text is your instructions
   to the participant (e.g. "Use the assistant below to discuss the article
   above."). Put it on the same page as the content the model should see.
3. **JavaScript:** gear icon › Add JavaScript › replace everything with
   `qualtrics/chat_question.js`, then edit the config block at the top:
   `SERVER_URL` (from step 3), `API_KEY`, `MIN_TURNS`, `MAX_TURNS`, and
   `EXTRA_CONTEXT` for piped text from earlier pages.
4. Preview the survey, chat a few turns, finish the survey, and check the
   Data & Analysis tab for the chat fields. (Generated test responses won't
   include chats, since no one actually types into the widget.)

## 5. How "what's on screen" reaches the model

Every time the participant sends a message, the widget re-reads the page and sends:
- the chat question's own text,
- every other question on the same page, with its answer options and a
  `Participant's answer:` line (selected options, typed text, dropdowns, sliders),
- `EXTRA_CONTEXT` (use piped text for answers from previous pages).

Because it's re-read each message, if the participant changes an answer mid-chat,
the model sees the new one. The server logs the exact context used for each turn
in the `page_context` column of `messages`. Set `SCRAPE_PAGE = false` to rely only
on `EXTRA_CONTEXT`.

## 6. Your data

**In Qualtrics:** `chat_log` (or `__js_chat_log`) is a JSON array of every
message with timestamps and reply times. `chat_turns` is the number of participant messages.

**On your server:** `server/chat_logs.sqlite3` is the complete record, including
the exact system prompt, model settings, token counts, latency, and failed calls.
Export it with:

```bash
python export_logs.py   # writes messages.csv and sessions.csv
```

Merge with the Qualtrics export on `response_id` = `ResponseId`. Treat the server
log as the primary source: Qualtrics embedded data can be truncated for very long
chats and is lost if someone closes the tab mid-page.

## Before launching

- Pilot with several people chatting at the same time. `MAX_CONCURRENT` controls
  how many generations run in parallel; others queue.
- Keep the server machine awake and plugged in for the whole collection window.
- The API key is visible in the page source; it only deters casual misuse.
  The turn and length limits on the server are the real protection.
- Describe the chatbot and the server-side logging in your consent form / IRB protocol.
- Record the Ollama version and model tag (`ollama show qwen3.5:9b`) for your methods section.
