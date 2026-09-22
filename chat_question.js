/*
 * Qualtrics LLM chat widget
 * Paste into: the chat question > gear icon > "Add JavaScript" (replace everything).
 *
 * Written in plain ES5 (var, function, .then) on purpose: the Qualtrics
 * JavaScript editor sometimes rejects newer syntax such as async/await.
 */

Qualtrics.SurveyEngine.addOnload(function () {
  var q = this;

  /* ===================== CONFIGURE THESE ===================== */
  var SERVER_URL = "https://YOUR-TUNNEL-OR-SERVER.example.com"; // no trailing slash
  var API_KEY    = "";           // must match API_KEY on the server (optional)
  var MIN_TURNS  = 3;            // participant messages required before Next unlocks (0 = none)
  var MAX_TURNS  = 20;           // input closes after this many messages
  var ASSISTANT_NAME = "Assistant";
  var OPENING_MESSAGE = "Hi! Ask me anything about what you see on this page.";

  // Qualtrics fills in piped text before this script runs.
  var RESPONSE_ID = "${e://Field/ResponseID}";
  var SURVEY_ID   = "${e://Field/SurveyID}";
  var CONDITION   = "${e://Field/condition}";   // set in Survey Flow, or leave blank

  // Anything else the model should know (e.g. answers from earlier pages).
  // Example: "Earlier the participant rated trust as ${q://QID3/ChoiceGroup/SelectedChoices}."
  var EXTRA_CONTEXT = "";

  // true = also read the text of the other questions on this page
  var SCRAPE_PAGE = true;
  /* =========================================================== */

  var questionEl = (q.getQuestionContainer && q.getQuestionContainer()) ||
                   document.getElementById(q.questionId);

  var state = {
    sessionId: null,
    turns: 0,
    busy: false,
    log: [],                 // what gets saved to embedded data
    pageLoadedAt: Date.now()
  };

  /* ---------- embedded data (works in both Qualtrics layouts) ---------- */
  function saveED(key, value) {
    var v = String(value);
    try {
      if (typeof Qualtrics.SurveyEngine.setJSEmbeddedData === "function") {
        Qualtrics.SurveyEngine.setJSEmbeddedData(key, v);   // -> __js_<key>
      }
    } catch (e) {}
    try {
      if (typeof Qualtrics.SurveyEngine.setEmbeddedData === "function") {
        Qualtrics.SurveyEngine.setEmbeddedData(key, v);     // -> <key>
      }
    } catch (e) {}
  }

  function persist() {
    saveED("chat_log", JSON.stringify(state.log));
    saveED("chat_turns", state.turns);
    saveED("chat_session_id", state.sessionId || "");
    saveED("chat_page_seconds", Math.round((Date.now() - state.pageLoadedAt) / 1000));
  }

  /* ---------- read what is on screen ---------- */
  function textOf(el) {
    return (el.innerText || el.textContent || "").replace(/\s+\n/g, "\n").trim();
  }

  // Find the visible label for an answer option (works in old and new layouts)
  function labelFor(input, block) {
    var lab = null;
    if (input.id) {
      try { lab = block.querySelector('label[for="' + CSS.escape(input.id) + '"]'); } catch (e) {}
    }
    if (!lab) lab = input.closest("label");
    if (lab && textOf(lab)) return textOf(lab);
    if (input.getAttribute("aria-label")) return input.getAttribute("aria-label");
    var by = input.getAttribute("aria-labelledby");
    if (by) {
      var el = document.getElementById(by.split(" ")[0]);
      if (el) return textOf(el);
    }
    return input.value || "";
  }

  // What the participant has chosen or typed in one question
  function answersIn(block) {
    var out = [];
    var i, el;
    var checked = block.querySelectorAll('input[type="radio"]:checked, input[type="checkbox"]:checked');
    for (i = 0; i < checked.length; i++) {
      var l = labelFor(checked[i], block);
      if (l) out.push(l);
    }
    var texts = block.querySelectorAll('input[type="text"], input[type="number"], input:not([type]), textarea');
    for (i = 0; i < texts.length; i++) {
      el = texts[i];
      if (widget.contains(el) || !el.value.trim()) continue;
      out.push('"' + el.value.trim() + '"');
    }
    var selects = block.querySelectorAll("select");
    for (i = 0; i < selects.length; i++) {
      el = selects[i];
      if (el.selectedIndex > 0) out.push(el.options[el.selectedIndex].text);
    }
    var ranges = block.querySelectorAll('input[type="range"], [role="slider"]');
    for (i = 0; i < ranges.length; i++) {
      el = ranges[i];
      var v = el.value || el.getAttribute("aria-valuenow");
      if (v !== null && v !== "") out.push("slider value " + v);
    }
    return out;
  }

  function scrapePage() {
    var parts = [];
    var blocks = document.querySelectorAll(".QuestionOuter, .question");
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i];
      if (b.contains(widget) || widget.contains(b)) continue;
      var textEl = b.querySelector(".QuestionText, .question-display") || b;
      var t = textOf(textEl);
      // include answer options too, so the model knows what the choices were
      var body = b.querySelector(".QuestionBody, .question-content");
      if (body && textEl !== b) t += "\n" + textOf(body);
      var picks = answersIn(b);
      t += "\nParticipant's answer: " + (picks.length ? picks.join("; ") : "(none yet)");
      if (t.trim()) parts.push(t.trim());
    }
    // this question's own text (the prompt above the chat box)
    var own = questionEl && questionEl.querySelector(".QuestionText, .question-display");
    if (own) parts.unshift(textOf(own));
    return parts.join("\n\n");
  }

  function buildContext() {
    var ctx = SCRAPE_PAGE ? scrapePage() : "";
    if (EXTRA_CONTEXT) ctx += (ctx ? "\n\n" : "") + EXTRA_CONTEXT;
    return ctx;
  }

  /* ---------- UI ---------- */
  var css =
    ".llmchat{margin-top:16px;border:1px solid #c9ced6;border-radius:8px;overflow:hidden;font:inherit;color:inherit;background:#fff}" +
    ".llmchat-log{height:340px;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;background:#f7f8fa}" +
    ".llmchat-msg{max-width:85%;padding:9px 12px;border-radius:12px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word}" +
    ".llmchat-user{align-self:flex-end;background:#1f4e8c;color:#fff;border-bottom-right-radius:3px}" +
    ".llmchat-bot{align-self:flex-start;background:#fff;border:1px solid #dde1e7;border-bottom-left-radius:3px}" +
    ".llmchat-name{display:block;font-size:.78em;opacity:.65;margin-bottom:2px}" +
    ".llmchat-typing{align-self:flex-start;font-style:italic;opacity:.6;padding:4px 2px}" +
    ".llmchat-err{align-self:center;color:#a4262c;font-size:.9em}" +
    ".llmchat-row{display:flex;gap:8px;padding:10px;border-top:1px solid #dde1e7}" +
    ".llmchat-input{flex:1;resize:none;min-height:42px;max-height:120px;padding:9px 10px;border:1px solid #c9ced6;border-radius:6px;font:inherit}" +
    ".llmchat-input:focus,.llmchat-send:focus-visible{outline:2px solid #1f4e8c;outline-offset:1px}" +
    ".llmchat-send{padding:0 18px;border:0;border-radius:6px;background:#1f4e8c;color:#fff;font:inherit;cursor:pointer}" +
    ".llmchat-send:disabled{background:#9aa5b4;cursor:default}" +
    ".llmchat-hint{padding:0 12px 10px;font-size:.85em;opacity:.7}";

  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  var widget = document.createElement("div");
  widget.className = "llmchat";
  widget.innerHTML =
    '<div class="llmchat-log" role="log" aria-live="polite"></div>' +
    '<div class="llmchat-row">' +
    '  <textarea class="llmchat-input" rows="1" aria-label="Your message" placeholder="Type your message"></textarea>' +
    '  <button type="button" class="llmchat-send">Send</button>' +
    "</div>" +
    '<div class="llmchat-hint"></div>';

  var mount = questionEl.querySelector(".QuestionBody, .question-content") || questionEl;
  mount.appendChild(widget);

  var logEl  = widget.querySelector(".llmchat-log");
  var input  = widget.querySelector(".llmchat-input");
  var sendBt = widget.querySelector(".llmchat-send");
  var hintEl = widget.querySelector(".llmchat-hint");

  function addBubble(role, text) {
    var d = document.createElement("div");
    d.className = "llmchat-msg " + (role === "user" ? "llmchat-user" : "llmchat-bot");
    if (role !== "user") {
      var n = document.createElement("span");
      n.className = "llmchat-name";
      n.textContent = ASSISTANT_NAME;
      d.appendChild(n);
    }
    d.appendChild(document.createTextNode(text));   // textContent: no HTML injection
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
    return d;
  }

  function addNote(cls, text) {
    var d = document.createElement("div");
    d.className = cls;
    d.textContent = text;
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
    return d;
  }

  function updateGate() {
    var remaining = MIN_TURNS - state.turns;
    if (remaining > 0) {
      q.disableNextButton();
      hintEl.textContent = "Send " + remaining + " more message" +
        (remaining === 1 ? "" : "s") + " to continue.";
    } else {
      q.enableNextButton();
      hintEl.textContent = state.turns >= MAX_TURNS
        ? "You've reached the message limit. Click Next to continue."
        : "";
    }
    var closed = state.turns >= MAX_TURNS;
    input.disabled = closed || state.busy;
    sendBt.disabled = closed || state.busy;
  }

  /* ---------- sending ---------- */
  function send() {
    var text = input.value.trim();
    if (!text || state.busy || state.turns >= MAX_TURNS) return;

    state.busy = true;
    input.value = "";
    updateGate();
    addBubble("user", text);
    var sentAt = Date.now();
    state.log.push({ role: "user", text: text, t: new Date(sentAt).toISOString() });
    persist();

    var typing = addNote("llmchat-typing", ASSISTANT_NAME + " is typing…");

    var body = {
      response_id: RESPONSE_ID,
      session_id: state.sessionId,
      survey_id: SURVEY_ID,
      condition: CONDITION,
      message: text,
      page_context: buildContext()   // re-read every message: picks up changed answers
    };

    fetch(SERVER_URL + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        typing.remove();
        state.sessionId = data.session_id;
        state.turns += 1;
        addBubble("assistant", data.reply);
        state.log.push({
          role: "assistant",
          text: data.reply,
          t: new Date().toISOString(),
          ms: Date.now() - sentAt
        });
      })
      .catch(function (err) {
        typing.remove();
        addNote("llmchat-err", "The assistant didn't respond. Please try sending your message again.");
        state.log.push({ role: "error", text: String(err), t: new Date().toISOString() });
        input.value = text;   // give the message back so they can resend
      })
      .then(function () {     // runs after success or failure
        state.busy = false;
        persist();
        updateGate();
        if (!input.disabled) input.focus();
      });
  }

  sendBt.addEventListener("click", send);
  input.addEventListener("keydown", function (e) {
    // Stop Qualtrics from treating Enter as "Next page"
    e.stopPropagation();
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  /* ---------- start ---------- */
  if (OPENING_MESSAGE) {
    addBubble("assistant", OPENING_MESSAGE);
    state.log.push({ role: "assistant", text: OPENING_MESSAGE, t: new Date().toISOString(), opening: true });
  }
  updateGate();
  persist();

  // Final save when the participant leaves the page
  Qualtrics.SurveyEngine.addOnPageSubmit(function () { persist(); });
});
