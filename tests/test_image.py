"""The built image, end to end: Telegram (a stand-in Bot API) to the agent (a stand-in) and back.

CHANNEL_GATEWAY_IMAGE names the image (default channel-gateway:test). Needs Docker and host
networking (Linux).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from fakes import FakeAgent, FakeTelegram

IMAGE = os.environ.get("CHANNEL_GATEWAY_IMAGE", "channel-gateway:test")
TOKEN = "123456:TEST-token"
OWNER, STRANGER = 4242, 9999
KEY = "agent-key-" + uuid.uuid4().hex


def docker(*args, check=True, **kw):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, **kw)


def wait_for(predicate, seconds, what):
    end = time.time() + seconds
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


class Image(unittest.TestCase):
    def test_the_platform_table_matches_hermes(self):
        """Each app the supervisor knows needs exactly what its Hermes plugin requires."""
        script = (
            "import importlib.machinery, importlib.util, sys\n"
            "from pathlib import Path\n"
            "from ruamel.yaml import YAML\n"
            "l = importlib.machinery.SourceFileLoader('cg', '/opt/channels/bin/channel-gateway')\n"
            "s = importlib.util.spec_from_loader('cg', l); cg = importlib.util.module_from_spec(s); l.exec_module(cg)\n"
            "bad = []\n"
            "for name, need in cg.PLATFORMS.items():\n"
            "    d = YAML(typ='safe').load(Path(f'/opt/channels/hermes/plugins/platforms/{name}/plugin.yaml'))\n"
            "    req = tuple(x if isinstance(x, str) else x['name'] for x in d.get('requires_env') or [])\n"
            "    if set(req) != set(need): bad.append((name, need, req))\n"
            "print(bad); sys.exit(1 if bad else 0)\n"
        )
        r = docker("run", "--rm", "--entrypoint", "/opt/channels/venv/bin/python", IMAGE, "-c", script, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_each_app_s_library_imports(self):
        mods = ["telegram", "discord", "slack_bolt", "lark_oapi", "dingtalk_stream", "aiohttp"]
        r = docker("run", "--rm", "--entrypoint", "/opt/channels/venv/bin/python", IMAGE,
                   "-c", "import " + ", ".join(mods), check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_reports_its_hermes_commit(self):
        r = docker("run", "--rm", IMAGE, "--version")
        self.assertRegex(r.stdout.strip(), r"^[0-9a-f]{40}$")


class Gateway(unittest.TestCase):
    """A running image with Telegram configured, against the stand-ins. No tests of its own."""
    allowed = f"TELEGRAM_ALLOWED_USERS={OWNER}\n"

    def setUp(self):
        self.telegram = FakeTelegram(TOKEN)
        self.agent = FakeAgent(KEY, answer="Decision: BUY NVDA", seconds=4)
        self.home = Path(tempfile.mkdtemp(prefix="cg-home-"))
        os.chmod(self.home, 0o700)
        (self.home / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\n" + self.allowed)
        (self.home / "config.extra.json").write_text(
            json.dumps({"platforms": {"telegram": {"extra": {"base_url": self.telegram.base_url}}}}))
        self.name = "cg-test-" + uuid.uuid4().hex[:8]
        docker("run", "-d", "--name", self.name, "--network", "host",
               "--user", f"{os.getuid()}:{os.getgid()}",
               "-v", f"{self.home}:/data",
               "-e", "CHANNEL_GATEWAY_HOME=/data", "-e", "HOME=/data",
               "-e", f"CHANNEL_GATEWAY_AGENT_URL={self.agent.url}",
               "-e", f"CHANNEL_GATEWAY_AGENT_KEY={KEY}",
               "-e", "CHANNEL_GATEWAY_POLL_SECONDS=1",
               "-e", "OPENAI_API_KEY=sk-must-not-leak",
               IMAGE)

    def tearDown(self):
        docker("rm", "-f", self.name, check=False)
        self.telegram.close()
        self.agent.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def logs(self):
        r = docker("logs", self.name, check=False)
        return r.stdout + r.stderr


class Telegram(Gateway):
    def test_owner_is_answered_by_the_agent_and_a_stranger_is_not_forwarded(self):
        wait_for(lambda: "getUpdates" in self.telegram.calls, 120, "the gateway to poll Telegram\n" + self.logs())
        self.telegram.message_from(STRANGER, "hello from a stranger")
        self.telegram.message_from(OWNER, "analyze NVDA")

        def answered():
            return any("Decision: BUY NVDA" in t for t in self.telegram.texts_to(OWNER))
        try:
            wait_for(answered, 90, "the agent's answer in Telegram")
        except AssertionError:
            raise AssertionError(f"sent: {self.telegram.sent}\nagent: {self.agent.requests}\n{self.logs()}")

        self.assertEqual(self.agent.user_messages(), ["analyze NVDA"])
        self.assertTrue(all(r["auth"] == f"Bearer {KEY}" for r in self.agent.requests))
        self.assertFalse(any("Decision" in t for t in self.telegram.texts_to(STRANGER)))
        owner_text = "\n".join(self.telegram.texts_to(OWNER))
        self.assertNotIn("still running", owner_text)  # heartbeats are not chat text
        self.assertNotIn("home channel", owner_text)  # Hermes's own setup prompts are off
        self.assertNotIn("sk-must-not-leak", self.logs())

        # Hermes's own commands are not the owner's to run here, and never reach the agent.
        before = len(self.telegram.texts_to(OWNER))
        self.telegram.message_from(OWNER, "/sethome")
        wait_for(lambda: len(self.telegram.texts_to(OWNER)) > before, 30, "a reply to /sethome")
        time.sleep(2)
        self.assertEqual(self.agent.user_messages(), ["analyze NVDA"])
        self.assertFalse((self.home / ".env").read_text().count("TELEGRAM_HOME_CHANNEL"))

        # No credential left: the gateway stops, the supervisor stays.
        (self.home / ".env").write_text("")
        wait_for(lambda: "no chat app is configured" in self.logs(), 30, "the gateway to stop")
        r = docker("inspect", "-f", "{{.State.Running}}", self.name)
        self.assertEqual(r.stdout.strip(), "true")

        start = time.time()
        docker("stop", "-t", "30", self.name)
        self.assertLess(time.time() - start, 25)
        r = docker("inspect", "-f", "{{.State.ExitCode}}", self.name)
        self.assertEqual(r.stdout.strip(), "0")



class Pairing(Gateway):
    """No allowlist: a new user is sent a code, and is answered once the owner approves it."""
    allowed = ""

    def test_a_paired_user_is_answered(self):
        wait_for(lambda: "getUpdates" in self.telegram.calls, 120, "the gateway to poll Telegram\n" + self.logs())
        self.telegram.message_from(OWNER, "hello")

        def code():
            for text in self.telegram.texts_to(OWNER):
                m = re.search(r"\b([A-Z0-9]{8})\b", text.replace("\\", ""))
                if m:
                    return m.group(1)
        found = wait_for(code, 60, "a pairing code\n" + str(self.telegram.sent))
        self.assertEqual(self.agent.requests, [])

        r = docker("exec", self.name, "/opt/channels/bin/channel-gateway", "pair", "telegram", found, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(str(OWNER), r.stdout + r.stderr)  # ClawUp reads the approved user from this

        self.telegram.message_from(OWNER, "analyze NVDA")
        wait_for(lambda: any("Decision: BUY NVDA" in t for t in self.telegram.texts_to(OWNER)), 90,
                 "the agent's answer after pairing\n" + self.logs())
        self.assertEqual(self.agent.user_messages(), ["analyze NVDA"])


if __name__ == "__main__":
    unittest.main()
