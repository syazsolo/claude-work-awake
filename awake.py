#!/usr/bin/env python3
"""Keep the Mac awake exactly while Claude works, in every session.

  awake.py start   UserPromptSubmit: Syaz sent a message, so work begins. Start
                   `caffeinate -dims -w <claude pid>` unless this session's is alive.
  awake.py stop    Stop: the turn ended. If no background Bash or agent of this
                   session is still running, the turn is waiting on Syaz: stop it.
                   If one is still running and none is alive, start it, since a
                   turn a task notice began had no prompt to start it.

The caffeinate's PID is kept in ~/.claude/state/awake/<session>.pid. `-w` ties it to
the claude process, so it never outlives the session. A caffeinate Syaz starts
himself is never touched.

Background work is read from the transcript: a Bash run in the background or an
async agent (or one resumed by SendMessage) is running from its launch until a
task notification for its id arrives or a TaskStop on it succeeds, since a stopped
task sends no notification. An agent's `completed` notice saying it is waiting on
its own background work is not its end: it keeps working after it.

A server is not work. A background Bash whose command or description names a dev
server, a file server or a watcher (SERVER below) waits for a visit and never
ends on its own, so it never keeps the Mac awake; only runs that will come back
with a result do.

AWAKE_DRY_RUN=1 prints the decision instead of acting.
"""
import json
import os
import re
import signal
import subprocess
import sys

STATE = os.path.expanduser("~/.claude/state/awake")
NOTIFICATION = re.compile(r"<task-notification>.*?</task-notification>", re.S)
TASK_ID = re.compile(r"<task-id>([A-Za-z0-9]+)</task-id>")
ENDED = re.compile(r"<status>(completed|failed|killed|stopped|cancelled)</status>")
# An agent that stops while its own background run continues reports `completed` too.
STILL_WAITING = "waiting on its own background work"
QUEUED_FOR = re.compile(r"delivery to ([A-Za-z0-9]+)")
SERVER = re.compile(
    r"\b(npm run (dev|start)|pnpm (dev|start)|yarn (dev|start)|next dev|expo start|npx expo"
    r"|artisan serve|php -S|vite|http-server|http\.server|tail -f|--watch)\b"
    r"|\b(serve|server|watch|watcher|watching)\b",
    re.I,
)


def running_tasks(transcript_path):
    state = {}
    names = {}
    awake_uses = set()
    awake_ids = set()
    with open(transcript_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            kind = entry.get("type")
            if kind == "assistant":
                for block in entry.get("message", {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        names[block["id"]] = block["name"]
                        inp = block.get("input") or {}
                        command = inp.get("command", "")
                        if block["name"] == "Bash" and (
                            command.strip().startswith("caffeinate")
                            or SERVER.search(command + " " + inp.get("description", ""))
                        ):
                            awake_uses.add(block["id"])
            elif kind == "user":
                result = entry.get("toolUseResult")
                if not isinstance(result, dict):
                    continue
                use_id = None
                for block in entry.get("message", {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        use_id = block.get("tool_use_id")
                task = result.get("backgroundTaskId")
                if task:
                    state[task] = "running"
                    if use_id in awake_uses:
                        awake_ids.add(task)
                if result.get("status") == "async_launched" and result.get("agentId"):
                    state[result["agentId"]] = "running"
                if names.get(use_id) == "TaskStop" and result.get("task_id"):
                    state[result["task_id"]] = "done"
                if names.get(use_id) == "SendMessage" and result.get("success"):
                    match = QUEUED_FOR.search(str(result.get("message", "")))
                    if match:
                        state[match.group(1)] = "running"
            elif kind in ("queue-operation", "attachment"):
                # A notice that lands mid-turn is kept as a queued_command attachment instead.
                text = json.dumps(entry).replace("\\n", "\n")
                for block in NOTIFICATION.findall(text):
                    task = TASK_ID.search(block)
                    if task and ENDED.search(block):
                        state[task.group(1)] = "running" if STILL_WAITING in block else "done"
    return sorted(t for t, s in state.items() if s == "running" and t not in awake_ids)


def claude_pid():
    pid = os.getppid()
    while pid > 1:
        out = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.strip()
        if not out:
            return None
        ppid, command = out.split(None, 1)
        if os.path.basename(command.split()[0]) == "claude":
            return pid
        pid = int(ppid)
    return None


def command_of(pid):
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def recorded(session):
    try:
        with open(os.path.join(STATE, f"{session}.pid")) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return pid if command_of(pid).startswith("caffeinate -dims") else None


def start(hook):
    session = hook.get("session_id")
    root = claude_pid()
    if not session or not root:
        return
    if recorded(session):
        return
    if os.environ.get("AWAKE_DRY_RUN"):
        print(json.dumps({"would_start": ["caffeinate", "-dims", "-w", str(root)]}))
        return
    child = subprocess.Popen(["caffeinate", "-dims", "-w", str(root)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, f"{session}.pid"), "w") as f:
        f.write(str(child.pid))


def stop(hook):
    session = hook.get("session_id")
    transcript = hook.get("transcript_path")
    if not session or not transcript or not os.path.exists(transcript):
        return
    busy = running_tasks(transcript)
    pid = recorded(session)
    if os.environ.get("AWAKE_DRY_RUN"):
        print(json.dumps({"running": busy, "would_kill": None if busy else pid,
                          "would_start": bool(busy and not pid)}))
        return
    if busy:
        # A turn a task notice began had no prompt to start it; work still running re-arms it.
        if not pid:
            start(hook)
        return
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        os.remove(os.path.join(STATE, f"{session}.pid"))
    except OSError:
        pass


def main():
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        return 0
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "start":
        start(hook)
    elif action == "stop":
        stop(hook)
    return 0


if __name__ == "__main__":
    sys.exit(main())
