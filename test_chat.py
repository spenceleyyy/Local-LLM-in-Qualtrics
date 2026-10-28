#!/usr/bin/env python3
"""
Test the chat locally, without Qualtrics.

Talks to your running server on 127.0.0.1:8000, so it exercises the real
code path: the same prompts, the same sentence trimming, the same leak
detection. Only the survey front-end is skipped.

    # see every condition's opening line for every possible prior answer
    python3 test_chat.py --batch

    # hold a conversation yourself
    python3 test_chat.py --condition assertive --answer "(3) only"

    # same, but let it pick the first option as the prior answer
    python3 test_chat.py --condition process

Requires the server to be running. API key is read from $API_KEY, or pass
--key. Test rows land in chat_logs.sqlite3 with response_id starting
"LOCALTEST", so you can exclude them later:

    DELETE FROM messages WHERE response_id LIKE 'LOCALTEST%';
    DELETE FROM sessions WHERE response_id LIKE 'LOCALTEST%';
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

DEFAULT_URL = os.getenv("TEST_SERVER_URL", "http://127.0.0.1:8000")
DEFAULT_KEY = os.getenv("API_KEY", "")

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m")


def post(url, key, payload, timeout=180):
    req = urllib.request.Request(
        url.rstrip("/") + "/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        raise SystemExit(f"{RED}HTTP {e.code}{RESET} — {body}")
    except urllib.error.URLError as e:
        raise SystemExit(
            f"{RED}Could not reach {url}{RESET} — {e.reason}\n"
            "Is uvicorn running?")


def get_health(url):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        raise SystemExit(f"{RED}Server not reachable at {url}{RESET} — {e}")


def load_items(path="items.json"):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def leak_flag(reply, item, target):
    """Rough local check: did the reply quote an option verbatim?"""
    hay = " ".join(reply.lower().split())
    opts = list(item.get("options") or [])
    if target and target not in opts:
        opts.append(target)
    for o in sorted(opts, key=lambda x: -len(str(x))):
        n = " ".join(str(o).lower().split())
        if len(n) >= 4 and n in hay:
            return str(o)
    return ""


def one_opening(url, key, item_id, item, condition, prior):
    target = (item.get("targets") or {}).get(prior, item.get("default_target", ""))
    data = post(url, key, {
        "response_id": "LOCALTEST-" + uuid.uuid4().hex[:8],
        "item_id": item_id,
        "condition": condition,
        "prior_answer": prior,
        "message": "",
        "opening": True,
    })
    return target, data


def batch(args, items):
    health = get_health(args.url)
    print(f"{DIM}model {health.get('model')} · "
          f"items {health.get('items_loaded')}{RESET}\n")

    item_id = args.item or next(iter(items))
    item = items[item_id]
    options = item.get("options") or []
    conditions = args.condition.split(",") if args.condition else \
        ["assertive", "process"]

    leaks = total = 0
    for cond in conditions:
        print(f"{BOLD}{'='*70}\n{cond.upper()}\n{'='*70}{RESET}")
        for prior in options:
            for i in range(args.n):
                target, data = one_opening(
                    args.url, key_for(args), item_id, item, cond, prior)
                reply = data["reply"]
                leaked = leak_flag(reply, item, target)
                total += 1
                if leaked:
                    leaks += 1
                correct = item.get("correct")
                tag = "CORRECT" if prior == correct else "wrong  "
                print(f"\n{DIM}they said [{tag}]: {prior}"
                      f"   → steering to: {target}{RESET}")
                print(f"  {reply}")
                print(f"  {DIM}{len(reply.split())} words{RESET}", end="")
                if leaked:
                    print(f"   {RED}LEAK: quoted \"{leaked}\"{RESET}")
                else:
                    print(f"   {GREEN}clean{RESET}")
    print(f"\n{BOLD}{leaks}/{total} replies quoted an option verbatim.{RESET}")
    if leaks:
        print(f"{YELLOW}Verbatim quotes are only the obvious leaks. Read the "
              f"replies above for ones that give the answer in words.{RESET}")


def key_for(args):
    return args.key if args.key is not None else DEFAULT_KEY


def interactive(args, items):
    get_health(args.url)
    item_id = args.item or next(iter(items))
    item = items[item_id]
    options = item.get("options") or []
    prior = args.answer or (options[0] if options else "")
    cond = args.condition or "assertive"

    print(f"{BOLD}{item.get('question','')}{RESET}")
    for o in options:
        mark = " <- their answer" if o == prior else ""
        print(f"  - {o}{mark}")
    target = (item.get("targets") or {}).get(prior, item.get("default_target"))
    print(f"\n{DIM}condition: {cond} · steering toward: {target}{RESET}")
    print(f"{DIM}Type a message and press Enter. Ctrl+C to quit.{RESET}\n")

    session = None
    rid = "LOCALTEST-" + uuid.uuid4().hex[:8]

    target_, data = one_opening(args.url, key_for(args), item_id, item,
                                cond, prior)
    session = data["session_id"]
    print(f"{DIM}you:{RESET} My answer is: {prior}")
    print(f"{BOLD}ai :{RESET} {data['reply']}")
    leaked = leak_flag(data["reply"], item, target_)
    if leaked:
        print(f"     {RED}LEAK: quoted \"{leaked}\"{RESET}")

    while True:
        try:
            msg = input(f"\n{DIM}you:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not msg:
            continue
        data = post(args.url, key_for(args), {
            "response_id": rid,
            "session_id": session,
            "item_id": item_id,
            "condition": cond,
            "prior_answer": prior,
            "message": msg,
        })
        session = data["session_id"]
        print(f"{BOLD}ai :{RESET} {data['reply']}")
        leaked = leak_flag(data["reply"], item, target_)
        info = f"{len(data['reply'].split())} words · {data['latency_ms']}ms"
        print(f"     {DIM}{info}{RESET}", end="")
        print(f"   {RED}LEAK: quoted \"{leaked}\"{RESET}" if leaked else "")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batch", action="store_true",
                    help="print the opening line for every condition x answer")
    ap.add_argument("--n", type=int, default=1,
                    help="samples per cell in batch mode (default 1)")
    ap.add_argument("--condition", help="assertive | process | control "
                                        "(batch: comma-separated)")
    ap.add_argument("--answer", help="the participant's unassisted answer")
    ap.add_argument("--item", help="item id from items.json")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--key", default=None, help="defaults to $API_KEY")
    ap.add_argument("--items-file", default="items.json")
    args = ap.parse_args()

    try:
        items = load_items(args.items_file)
    except FileNotFoundError:
        sys.exit(f"{RED}No {args.items_file} here. Run this from the folder "
                 f"holding app.py.{RESET}")
    if not items:
        sys.exit(f"{RED}{args.items_file} has no items in it.{RESET}")

    if args.batch:
        batch(args, items)
    else:
        interactive(args, items)


if __name__ == "__main__":
    main()
