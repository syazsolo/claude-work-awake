"""Each test writes a transcript in the shapes Claude Code writes, then reads what runs.

Run: python3 -m unittest test_awake
"""
import json
import os
import tempfile
import unittest

import awake


def use(block_id, name, **inp):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": block_id, "name": name, "input": inp}]}}


def result(block_id, **tool_use_result):
    return {"type": "user", "toolUseResult": tool_use_result, "message": {"content": [
        {"type": "tool_result", "tool_use_id": block_id, "content": ""}]}}


def notice(task, status="completed", summary="Background command \"x\" completed (exit code 0)"):
    text = (f"<task-notification>\n<task-id>{task}</task-id>\n<status>{status}</status>\n"
            f"<summary>{summary}</summary>\n</task-notification>")
    return {"type": "queue-operation", "operation": "enqueue", "content": text}


def queued(task, status="completed"):
    text = f"<task-notification>\n<task-id>{task}</task-id>\n<status>{status}</status>\n</task-notification>"
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": text}}


def background(block_id, task, command, description=""):
    return [use(block_id, "Bash", command=command, description=description, run_in_background=True),
            result(block_id, stdout="", backgroundTaskId=task)]


class RunningTasks(unittest.TestCase):
    def running(self, *entries):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
            f.write("not json\n")
        try:
            return awake.running_tasks(f.name)
        finally:
            os.remove(f.name)

    def test_a_background_command_runs_from_its_launch(self):
        self.assertEqual(self.running(*background("u1", "t1", "sh gates.sh full")), ["t1"])

    def test_a_task_notification_ends_it(self):
        self.assertEqual(self.running(*background("u1", "t1", "sh gates.sh full"), notice("t1")), [])

    def test_a_failed_or_killed_notification_ends_it_too(self):
        for status in ("failed", "killed", "stopped", "cancelled"):
            self.assertEqual(self.running(*background("u1", "t1", "make"), notice("t1", status)), [])

    def test_a_notification_kept_as_a_queued_attachment_ends_it(self):
        self.assertEqual(self.running(*background("u1", "t1", "make"), queued("t1")), [])

    def test_a_taskstop_ends_it_since_a_stopped_task_sends_no_notice(self):
        entries = background("u1", "t1", "make") + [
            use("u2", "TaskStop", task_id="t1"),
            result("u2", message="Successfully stopped task: t1 (make)", task_id="t1", task_type="local_bash"),
        ]
        self.assertEqual(self.running(*entries), [])

    def test_a_dev_server_never_counts_as_work(self):
        entries = (background("u1", "t1", "cd orderer && npm run dev > dev.log 2>&1")
                   + background("u2", "t2", "php -d x=1 artisan serve --port=8000")
                   + background("u3", "t3", "node bin/www", "Start the API server on port 4000")
                   + background("u4", "t4", "python3 -m http.server 8080")
                   + background("u5", "t5", "sh gates.sh full"))
        self.assertEqual(self.running(*entries), ["t5"])

    def test_the_idle_pattern_extends_by_environment(self):
        os.environ["AWAKE_IDLE"] = r"\bmy-daemon\b"
        try:
            pattern = awake.IDLE
            awake.IDLE = awake.re.compile(pattern.pattern + "|" + os.environ["AWAKE_IDLE"], awake.re.I)
            self.assertEqual(self.running(*background("u1", "t1", "my-daemon --port 9")), [])
        finally:
            awake.IDLE = pattern
            del os.environ["AWAKE_IDLE"]

    def test_a_hold_started_by_hand_is_not_work(self):
        self.assertEqual(self.running(*background("u1", "t1", "caffeinate -i")), [])

    def test_an_async_agent_runs_until_its_notice(self):
        entries = [use("u1", "Agent", prompt="build it"),
                   result("u1", isAsync=True, status="async_launched", agentId="a1")]
        self.assertEqual(self.running(*entries), ["a1"])
        self.assertEqual(self.running(*entries, notice("a1", summary="Agent \"build it\" completed")), [])

    def test_an_agent_waiting_on_its_own_background_work_has_not_ended(self):
        entries = [use("u1", "Agent", prompt="build it"),
                   result("u1", isAsync=True, status="async_launched", agentId="a1"),
                   notice("a1", summary="Agent completed; it is waiting on its own background work")]
        self.assertEqual(self.running(*entries), ["a1"])

    def test_a_message_to_an_agent_resumes_it(self):
        entries = [use("u1", "Agent", prompt="build it"),
                   result("u1", isAsync=True, status="async_launched", agentId="a1"),
                   notice("a1"),
                   use("u2", "SendMessage", to="a1", message="fix K-1"),
                   result("u2", success=True, message="Message queued for delivery to a1")]
        self.assertEqual(self.running(*entries), ["a1"])

    def test_a_foreground_command_is_not_a_task(self):
        self.assertEqual(self.running(use("u1", "Bash", command="ls"), result("u1", stdout="x")), [])


class Decision(unittest.TestCase):
    def test_work_running_keeps_or_takes_the_hold(self):
        self.assertEqual(awake.decide(["t1"], 123), "keep")
        self.assertEqual(awake.decide(["t1"], None), "take")

    def test_nothing_running_releases_it(self):
        self.assertEqual(awake.decide([], 123), "release")
        self.assertEqual(awake.decide([], None), "nothing")


class HoldCommand(unittest.TestCase):
    def test_the_hold_is_tied_to_the_claude_process(self):
        platform = awake.sys.platform
        try:
            awake.sys.platform = "darwin"
            self.assertEqual(awake.hold_command(42, "s"), ["caffeinate", "-ims", "-w", "42"])
            os.environ["AWAKE_DISPLAY"] = "1"
            self.assertEqual(awake.hold_command(42, "s")[1], "-dims")
            del os.environ["AWAKE_DISPLAY"]
            awake.sys.platform = "linux"
            self.assertIn("--pid=42", awake.hold_command(42, "s"))
            awake.sys.platform = "win32"
            self.assertIsNone(awake.hold_command(42, "s"))
        finally:
            awake.sys.platform = platform


if __name__ == "__main__":
    unittest.main()
