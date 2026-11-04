# claude-work-awake

Keeps your Mac awake exactly while Claude Code's work runs, and lets it sleep the moment
Claude is waiting on you. Work-aware, not busy-aware.

Claude Code's own sleep inhibitor watches the terminal spinner: it holds the Mac while a
turn runs and lets go thirty seconds after. That is wrong in both directions. A review, a
build or an agent you sent to the background keeps running after the turn ends, so the Mac
sleeps under it. And the community hooks that fix that count every background process as
work, so a dev server you started for a look keeps the Mac awake all night while Claude
waits for your next word.

This hook reads the session's transcript at the end of every turn and asks one question:
is anything of this session still running that will come back with a result? A background
command or an agent counts until its completion notice arrives. A server or a watcher
never counts. When nothing counts, the hold is released.

## Install

Copy `awake.py` to `~/.claude/hooks/awake.py` and add three hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/awake.py start", "timeout": 10 }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/awake.py stop", "timeout": 10 }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/awake.py end", "timeout": 10 }] }
    ]
  }
}
```

Python 3.8 or later, no dependencies. Claude Code reloads settings on save; no restart.

## How it decides

| Event | Hook | What happens |
|---|---|---|
| You send a message | `start` | Takes the hold, unless this session already holds one. |
| The turn ends | `stop` | Reads the transcript. Background work still running: keeps the hold, or takes it if none is held, since a turn a task notice began had no prompt. Nothing running: releases it. |
| The session ends | `end` | Releases it. |

Background work is a `Bash` call run in the background, an `Agent` launched async, or an
agent resumed with `SendMessage`. Each runs from its launch until its `<task-notification>`
arrives or a `TaskStop` on it succeeds. An agent's `completed` notice that says it is
"waiting on its own background work" is not its end.

A server is not work. A background command whose text or description names a dev server,
a file server or a watcher waits for a visit and never ends on its own, so it never holds
the machine. The built-in list covers `npm run dev`, `next dev`, `expo start`,
`artisan serve`, `php -S`, `vite`, `http-server`, `python -m http.server`, `rails s`,
`flask run`, `uvicorn`, `gunicorn`, `tail -f`, `--watch`, and the words *serve*,
*server*, *watch* and *watcher* in the command or Claude's description of it. Extend it
with a regular expression in `AWAKE_IDLE`.

The hold is one process per session, tied to the Claude process so it can never outlive
it:

- macOS: `caffeinate -ims -w <claude pid>`. Set `AWAKE_DISPLAY=1` to keep the display on
  too (`-dims`).
- Linux: `systemd-inhibit --what=idle:sleep --mode=block tail --pid=<claude pid> -f /dev/null`.
- Windows: nothing yet. The hook exits quietly.

Its pid lives in `~/.claude/state/awake/<session>.pid` (`CLAUDE_CONFIG_DIR` respected).
Several sessions hold several times; each releases its own. A hold you start by hand is
never touched.

## See the decision without acting

```sh
echo '{"session_id":"<id>","transcript_path":"<path>"}' | AWAKE_DRY_RUN=1 python3 ~/.claude/hooks/awake.py stop
```

prints the running task ids, the held pid and the decision: `keep`, `take`, `release` or
`nothing`. The session id and transcript path are what Claude Code passes every hook; the
transcript is under `~/.claude/projects/<project>/<session id>.jsonl`.

## Limits

- `caffeinate` prevents idle sleep. Closing the lid still sleeps a MacBook unless it is on
  power with an external display, as Apple decides.
- The hook knows a server by its name. A server launched as a bare `node x.js` with no
  description naming it counts as work until it is stopped. Describe servers as servers, or
  add their command to `AWAKE_IDLE`.
- The transcript is Claude Code's record and is written asynchronously; the Stop hook reads
  what has landed by then, which has held every time so far.
- Claude Code keeps running its own inhibitor beside this one. Its rotation gap (issue
  81832) is covered while this hold is up, since this hold never rotates.

## Why not the others

Every hook and app found in November 2026 decides on `UserPromptSubmit` and `Stop`, or
on a fifteen-minute idle timer, or on the process tree. None reads background tasks or
agents, and the process-tree one counts a dev server as busy on purpose. Claude Code's
built-in inhibitor has no opt-out (issue 21432), sleeps mid-task at its rotation (issues
81832, 85575), and knows nothing of background work. The right fix is inside Claude Code:
hold while any work of the session runs, release on waiting for the user. Until then,
this.

## Tests

```sh
python3 -m unittest test_awake
```

Each test writes a transcript in the shapes Claude Code writes and reads what runs.

## Licence

MIT.
