"""The supervisor's own logic, on the host's Python (no image needed)."""
import importlib.machinery
import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "channel-gateway"
loader = importlib.machinery.SourceFileLoader("channel_gateway", str(SCRIPT))
spec = importlib.util.spec_from_loader("channel_gateway", loader)
cg = importlib.util.module_from_spec(spec)
loader.exec_module(cg)


class ReadEnv(unittest.TestCase):
    def test_parses_like_a_dotenv_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("# comment\n\nTELEGRAM_BOT_TOKEN='123:abc'\nexport FEISHU_APP_ID=\"cli_x\"\n"
                         "EMPTY=\nNOEQUALS\nA = b \n")
            self.assertEqual(cg.read_env(p), {
                "TELEGRAM_BOT_TOKEN": "123:abc", "FEISHU_APP_ID": "cli_x", "EMPTY": "", "A": "b"})

    def test_missing_file_is_empty(self):
        self.assertEqual(cg.read_env(Path("/nonexistent/.env")), {})


class Configured(unittest.TestCase):
    def test_an_app_counts_only_with_every_variable_it_needs(self):
        self.assertEqual(cg.configured({"TELEGRAM_BOT_TOKEN": "t"}), ["telegram"])
        self.assertEqual(cg.configured({"FEISHU_APP_ID": "a"}), [])
        self.assertEqual(cg.configured({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "s"}), ["feishu"])
        self.assertEqual(cg.configured({"SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": ""}), [])
        self.assertEqual(cg.configured({}), [])


class Settings(unittest.TestCase):
    def test_defaults_to_the_local_agent_and_its_gateway_token(self):
        s = cg.settings({"HOME": "/home/a", "OPENCLAW_GATEWAY_TOKEN": "gw"})
        self.assertEqual(s, {"home": "/home/a/.channel-gateway", "url": "http://127.0.0.1:18789", "key": "gw"})

    def test_explicit_values_win(self):
        s = cg.settings({"CHANNEL_GATEWAY_HOME": "/data", "CHANNEL_GATEWAY_AGENT_URL": "http://a:1/",
                         "CHANNEL_GATEWAY_AGENT_KEY": "k", "OPENCLAW_GATEWAY_TOKEN": "gw"})
        self.assertEqual(s, {"home": "/data", "url": "http://a:1", "key": "k"})

    def test_refuses_a_non_http_agent_url(self):
        with self.assertRaises(SystemExit):
            cg.settings({"CHANNEL_GATEWAY_AGENT_URL": "file:///etc/passwd"})


class ChildEnv(unittest.TestCase):
    def test_the_agents_secrets_do_not_reach_the_gateway(self):
        environ = {"HOME": "/home/a", "OPENAI_API_KEY": "sk-secret", "PRIVY_APP_SECRET": "p",
                   "TELEGRAM_BOT_TOKEN": "from-env-not-file", "HTTPS_PROXY": "http://proxy:3128", "TZ": "UTC"}
        env = cg.child_env(environ, {"home": "/data", "url": "http://127.0.0.1:18789", "key": "k"})
        for secret in ("OPENAI_API_KEY", "PRIVY_APP_SECRET", "TELEGRAM_BOT_TOKEN"):
            self.assertNotIn(secret, env)
        self.assertEqual(env["HERMES_HOME"], "/data")
        self.assertEqual(env["GATEWAY_PROXY_URL"], "http://127.0.0.1:18789")
        self.assertEqual(env["GATEWAY_PROXY_KEY"], "k")
        self.assertEqual(env["HERMES_GATEWAY_NO_SUPERVISE"], "1")
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy:3128")
        self.assertEqual(env["TZ"], "UTC")


def load_config(home):
    text = (home / "config.yaml").read_text()
    return json.loads(text.split("\n", 1)[1])


class WriteConfig(unittest.TestCase):
    def test_queues_messages_and_makes_nobody_a_hermes_admin(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            cg.write_config(home)
            config = load_config(home)
            self.assertEqual(config["display"]["busy_input_mode"], "queue")
            for name in cg.PLATFORMS:
                self.assertEqual(config["platforms"][name]["extra"]["allow_admin_from"], cg.NO_ADMIN)
            self.assertEqual((home / "config.yaml").stat().st_mode & 0o777, 0o600)

    def test_extra_is_merged_over_the_base(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "config.extra.json").write_text(json.dumps(
                {"platforms": {"telegram": {"extra": {"base_url": "http://x/bot"}}}, "display": {"x": 1}}))
            cg.write_config(home)
            config = load_config(home)
            extra = config["platforms"]["telegram"]["extra"]
            self.assertEqual(extra["base_url"], "http://x/bot")
            self.assertEqual(extra["allow_admin_from"], cg.NO_ADMIN)
            self.assertEqual(config["display"], {"busy_input_mode": "queue", "x": 1})

    def test_a_broken_extra_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "config.extra.json").write_text("platforms: [")
            with self.assertRaisesRegex(ValueError, "config.extra.json"):
                cg.write_config(home)


class Pair(unittest.TestCase):
    def test_runs_hermes_pairing_approve(self):
        self.assertEqual(cg.pair_command(["telegram", "XKGH5N7P"])[1:],
                         ["pairing", "approve", "telegram", "XKGH5N7P"])

    def test_refuses_an_unknown_platform_or_a_strange_code(self):
        for args in (["irc", "ABC"], ["telegram", "A B"], ["telegram", "$(id)"], ["telegram", ""], ["telegram"]):
            with self.assertRaises(SystemExit, msg=args):
                cg.pair_command(args)


class Tick(unittest.TestCase):
    """The start/stop decisions, with a stand-in for the gateway process."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.home = Path(self.dir.name)
        self.sup = cg.Supervisor({"CHANNEL_GATEWAY_HOME": self.dir.name, "CHANNEL_GATEWAY_AGENT_KEY": "k"})
        self.started, self.stopped = [], []

        class Child:
            def __init__(self):
                self.returncode = None

            def poll(self):
                return self.returncode

        def start(apps, inputs):
            self.sup.child = Child()
            self.sup.started_with, self.sup.started_at = inputs, 0.0
            self.started.append(apps)

        def stop(why):
            self.sup.child = None
            self.stopped.append(why)

        self.sup.start, self.sup.stop = start, stop

    def tearDown(self):
        self.dir.cleanup()

    def env(self, text):
        (self.home / ".env").write_text(text)

    def test_waits_until_an_app_is_configured(self):
        self.sup.tick(0)
        self.assertEqual(self.started, [])
        self.env("TELEGRAM_BOT_TOKEN=t\n")
        self.sup.tick(1)
        self.assertEqual(self.started, [["telegram"]])

    def test_restarts_on_a_changed_credential_and_stops_when_none_is_left(self):
        self.env("TELEGRAM_BOT_TOKEN=t\n")
        self.sup.tick(0)
        self.sup.tick(1)
        self.assertEqual(len(self.started), 1)
        self.env("TELEGRAM_BOT_TOKEN=t2\n")
        self.sup.tick(2)
        self.assertEqual(self.stopped, ["its configuration changed"])
        self.assertEqual(len(self.started), 2)
        self.env("")
        self.sup.tick(3)
        self.assertEqual(self.stopped[-1], "no chat app is configured")
        self.assertIsNone(self.sup.child)

    def test_a_bad_extra_config_waits_for_a_fix_instead_of_crashing(self):
        sup = cg.Supervisor({"CHANNEL_GATEWAY_HOME": self.dir.name, "CHANNEL_GATEWAY_AGENT_KEY": "k"})
        sup.start = lambda apps, inputs: cg.write_config(self.home)  # the real config step
        self.env("TELEGRAM_BOT_TOKEN=t\n")
        (self.home / "config.extra.json").write_text("{broken")
        sup.tick(0)
        self.assertNotEqual(sup.refused, "")
        refused = sup.refused
        sup.tick(1)  # unchanged: not retried
        self.assertEqual(sup.refused, refused)
        (self.home / "config.extra.json").write_text("{}")
        sup.tick(2)  # changed: tried again, and the config is written
        self.assertTrue((self.home / "config.yaml").exists())

    def test_a_crashed_gateway_is_restarted_with_backoff(self):
        self.env("TELEGRAM_BOT_TOKEN=t\n")
        self.sup.tick(0)
        self.sup.child.returncode = 1
        self.sup.tick(5)  # exited after 5s: wait 1s
        self.assertEqual(len(self.started), 1)
        self.sup.tick(6)
        self.assertEqual(len(self.started), 2)
        self.sup.child.returncode = 1
        self.sup.tick(7)  # again: wait 2s
        self.sup.tick(8)
        self.assertEqual(len(self.started), 2)
        self.sup.tick(9)
        self.assertEqual(len(self.started), 3)


if __name__ == "__main__":
    unittest.main()
