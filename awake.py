#!/usr/bin/env python3
"""Keep the machine awake exactly while Claude Code's work runs, in every session.

Claude Code holds the Mac awake only while its spinner turns, and lets go half a minute
after. This hook holds it while the session's work runs: the turn, and after the turn any
background command or agent that will come back with a result. It lets go when the turn
waits on you, even while a dev server you started keeps serving, and the hold ends with the
Claude process whatever happens.

  awake.py start   UserPromptSubmit: work begins. Hold, unless this session already holds.
  awake.py stop    Stop: the turn ended. Background work still running: keep the hold, or
                   take it if none is held (a turn a task notice began had no prompt).
                   None running: let go.
  awake.py end     SessionEnd: let go.

Background work is read from the session transcript. A Bash run in the background or an
async agent (or one resumed by SendMessage) runs from its launch until a task notification
for its id arrives or a TaskStop on it succeeds, since a stopped task sends no notice. An
agent's `completed` notice saying it is waiting on its own background work is not its end.

A server is not work. A background command whose text or description names a dev server, a
file server or a watcher (IDLE below, extended by AWAKE_IDLE) waits for a visit and never
ends on its own, so it never holds the machine; only runs that will come back do.

The hold is one process per session, tied to the Claude process so it never outlives it:
macOS `caffeinate -ims -w <pid>` (AWAKE_DISPLAY=1 adds -d, keeping the display on too),
Linux `systemd-inhibit` around `tail --pid=<pid>`. Elsewhere the hook does nothing. Its pid
is kept in <CLAUDE_CONFIG_DIR or ~/.claude>/state/awake/<session>.pid. A hold you start
yourself is never touched.

AWAKE_DRY_RUN=1 prints the decision instead of acting.
"""
import json
import os
import re
import signal
import subprocess
import sys

STATE = os.path.join(
    os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "state", "awake"
)
NOTIFICATION = re.compile(r"<task-notification>.*?</task-notification>", re.S)
TASK_ID = re.compile(r"<task-id>([A-Za-z0-9]+)</task-id>")
ENDED = re.compile(r"<status>(completed|failed|killed|stopped|cancelled)</status>")
# An agent that stops while its own background run continues reports `completed` too.
STILL_WAITING = "waiting on its own background work"
QUEUED_FOR = re.compile(r"delivery to ([A-Za-z0-9]+)")
IDLE = re.compile(
    r"\b(npm (run )?(dev|start|serve)|pnpm (dev|start)|yarn (dev|start)|next dev|expo start"
    r"|npx expo|artisan serve|php -S|vite|http-server|http\.server|rails s(erver)?|flask run"
    r"|uvicorn|gunicorn|tail -f|--watch)\b"
    r"|\b(serve|server|watch|watcher|watching)\b"
    + ("|" + os.environ["AWAKE_IDLE"] if os.environ.get("AWAKE_IDLE") else ""),
    re.I,
)
HOLDERS = ("caffeinate", "systemd-inhibit")


def program_of(command):
    return command.strip().split(" ", 1)[0].rsplit("/", 1)[-1] if command.strip() else ""


def is_idle(block):
    """A tool use that holds nothing: a server, a watcher, or a hold started by hand."""
    if block.get("name") != "Bash":
        return False
    inp = block.get("input") or {}
    command = inp.get("command") or ""
    description = inp.get("description") or ""
    return program_of(command) in HOLDERS or IDLE.search(command + " " + description) is not None


def running_tasks(transcript_path):
    """The ids of background commands and agents launched and not yet ended, servers aside."""
    state = {}
    names = {}
    idle_uses = set()
    idle_ids = set()
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
                        if is_idle(block):
                            idle_uses.add(block["id"])
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
                    if use_id in idle_uses:
                        idle_ids.add(task)
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
    return sorted(t for t, s in state.items() if s == "running" and t not in idle_ids)


def decide(busy, held):
    """What the Stop hook does: keep, take, release the hold, or nothing."""
    if busy:
        return "keep" if held else "take"
    return "release" if held else "nothing"


# ───────────────────────────── the hold ─────────────────────────────


def command_of(pid):
    return subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()


def claude_pid():
    """The Claude Code process this hook runs under: the nearest ancestor named claude,
    else the nearest that is not a shell."""
    pid = os.getppid()
    first_not_shell = None
    while pid > 1:
        out = subprocess.run(
            ["ps", "-o", "ppid=,command=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
        if not out:
            break
        ppid, command = out.split(None, 1)
        program = program_of(command)
        if program == "claude":
            return pid
        if first_not_shell is None and program not in ("sh", "bash", "zsh", "fish", "dash"):
            first_not_shell = pid
        pid = int(ppid)
    return first_not_shell


def hold_command(root, session):
    if sys.platform == "darwin":
        flags = "-dims" if os.environ.get("AWAKE_DISPLAY") else "-ims"
        return ["caffeinate", flags, "-w", str(root)]
    if sys.platform.startswith("linux"):
        return [
            "systemd-inhibit", "--what=idle:sleep", "--who=Claude Code",
            f"--why=session {session} is working", "--mode=block",
            "tail", f"--pid={root}", "-f", "/dev/null",
        ]
    return None


def pid_file(session):
    return os.path.join(STATE, f"{session}.pid")


def recorded(session):
    """This session's hold, if its recorded pid is still one."""
    try:
        with open(pid_file(session)) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return pid if program_of(command_of(pid)) in HOLDERS else None


def prune():
    """Drop the pid files of holds that are gone, so a crashed session leaves no trace."""
    try:
        files = os.listdir(STATE)
    except OSError:
        return
    for name in files:
        if name.endswith(".pid") and recorded(name[:-4]) is None:
            try:
                os.remove(os.path.join(STATE, name))
            except OSError:
                pass


def take(session, dry):
    root = claude_pid()
    command = hold_command(root, session) if root else None
    if command is None:
        return
    if dry:
        print(json.dumps({"would_start": command}))
        return
    child = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    os.makedirs(STATE, exist_ok=True)
    with open(pid_file(session), "w") as f:
        f.write(str(child.pid))


def release(session, pid, dry):
    if dry:
        print(json.dumps({"would_kill": pid}))
        return
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        os.remove(pid_file(session))
    except OSError:
        pass


# ───────────────────────────── the hooks ─────────────────────────────


def start(hook, dry=False):
    session = hook.get("session_id")
    if not session:
        return
    prune()
    if recorded(session):
        return
    take(session, dry)


def stop(hook, dry=False):
    session = hook.get("session_id")
    transcript = hook.get("transcript_path")
    if not session:
        return
    busy = running_tasks(transcript) if transcript and os.path.exists(transcript) else []
    held = recorded(session)
    what = decide(busy, held)
    if dry:
        print(json.dumps({"running": busy, "held": held, "decision": what}))
    if what == "take":
        take(session, dry)
    elif what == "release":
        release(session, held, dry)


def end(hook, dry=False):
    session = hook.get("session_id")
    if session:
        release(session, recorded(session), dry)


def main():
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        return 0
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    dry = bool(os.environ.get("AWAKE_DRY_RUN"))
    if action == "start":
        start(hook, dry)
    elif action == "stop":
        stop(hook, dry)
    elif action == "end":
        end(hook, dry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
