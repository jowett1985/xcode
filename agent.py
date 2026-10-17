#!/usr/bin/env python3
"""Minimal coding agent: one file, stdlib only.

WARNING: the bash tool runs arbitrary commands with no sandbox and no confirmation.
"""

import json
import os
import subprocess
import sys
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "https://127.0.0.1:8080/v1")
API_KEY = os.environ.get("API_KEY", "")
MODEL = os.environ.get("MODEL", "gpt-4o-mini")

MAX_ITERATIONS = 100
MAX_CHARS = 20000
MAX_LINES = 2000

SYSTEM_PROMPT = """You are a coding agent working in the current directory.
Read files before editing them.
Prefer edit over write for files that already exist.
Use bash to run code, install things, and run tests.
Be concise: call tools when needed, then answer briefly.
Do not ask for permission; just act."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Return a file's contents with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "1-indexed first line"},
                    "limit": {"type": "integer", "description": "max lines to return"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Create or overwrite a file, making parent directories.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace exactly one occurrence of old_string with new_string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command and return stdout+stderr plus exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "seconds, default 120"},
                },
                "required": ["command"],
            },
        },
    },
]


def resolve(path):
    return os.path.abspath(os.path.expanduser(path))


def read(path, offset=None, limit=None):
    try:
        with open(resolve(path), "r", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"read failed: {e}"
    start = max(0, int(offset) - 1) if offset else 0
    end = start + (int(limit) if limit else MAX_LINES)
    chunk = lines[start:end]
    if not chunk:
        return f"read failed: no lines in range {start + 1}..{end} (file has {len(lines)} lines)"
    out = "\n".join(f"{start + i + 1:>6}\t{l.rstrip()}" for i, l in enumerate(chunk))
    if end < len(lines):
        out += f"\n[truncated: showing lines {start + 1}-{end} of {len(lines)}; use offset/limit]"
    return out


def write(path, content):
    try:
        p = resolve(path)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
        return f"wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"write failed: {e}"


def edit(path, old_string, new_string):
    try:
        p = resolve(path)
        with open(p, "r", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return f"edit failed: {e}"
    if not old_string:
        return "edit failed: old_string is empty"
    count = text.count(old_string)
    if count == 0:
        return f"edit failed: old_string not found in {path}"
    if count > 1:
        return f"edit failed: old_string appears {count} times in {path}; make it unique"
    with open(p, "w") as f:
        f.write(text.replace(old_string, new_string, 1))
    return f"edited {path}: 1 replacement"


def bash(command, timeout=120):
    try:
        r = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=float(timeout or 120),
        )
    except subprocess.TimeoutExpired:
        return f"bash timed out after {timeout}s"
    except Exception as e:
        return f"bash failed: {e}"
    out = (r.stdout or "") + (r.stderr or "")
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS] + f"\n[truncated {len(out) - MAX_CHARS} chars of output]"
    return f"[exit {r.returncode}]\n{out}" or "[exit 0] (no output)"


IMPL = {"read": read, "write": write, "edit": edit, "bash": bash}


def chat(messages):
    body = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS}).encode()
    req = urllib.request.Request(
        BASE_URL.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())


def brief(args):
    parts = []
    for k, v in args.items():
        s = str(v).replace("\n", "\\n")
        if len(s) > 60:
            s = s[:60] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def run(history, task):
    history.append({"role": "user", "content": task})
    for _ in range(MAX_ITERATIONS):
        try:
            data = chat(history)
        except Exception as e:
            print(f"[api error] {e}")
            return
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            answer = msg.get("content") or ""
            history.append({"role": "assistant", "content": answer})
            print(answer)
            return
        history.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except Exception:
                args = {}
            fn = IMPL.get(name)
            result = fn(**args) if fn else f"unknown tool: {name}"
            print(f"  -> {name}({brief(args)})")
            history.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    print("[stopped: hit the 100-iteration limit]")


def main():
    if len(sys.argv) > 1:
        run([], " ".join(sys.argv[1:]))
        return
    history = []
    print(f"agent ready ({MODEL} @ {BASE_URL}) - blank line or Ctrl+C to quit")
    while True:
        try:
            task = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nbye")
            break
        if not task:
            break
        run(history, task)


if __name__ == "__main__":
    main()
