"""Tests for multi_agent.py. Run with `python3 -m unittest discover tests`.

Every test runs against tests/fake-pi in temporary state, session and project
directories, so no model is called and nothing outside the temporary
directories is touched.
"""

import glob
import importlib.util
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "skills", "multi-agent", "scripts", "multi_agent.py")
FAKE_PI = os.path.join(HERE, "fake-pi")
# Scales the script's timeouts and grace periods, in the commands the tests
# run and in `ma` below, so tests that wait them out stay short. Tests compute
# their expectations from the scaled constants, such as ma.PI_EXIT_GRACE_S.
TIME_SCALE = "0.1"
os.environ["MULTI_AGENT_TIME_SCALE"] = TIME_SCALE

_spec = importlib.util.spec_from_file_location("multi_agent", SCRIPT)
ma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ma)


def wait_until(predicate, timeout=15, interval=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def alive(pid):
    """Whether pid is a live process; a zombie counts as gone where there is /proc."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except OSError:
        return not os.path.isdir("/proc/self")


def pid_gone(pid):
    return not alive(pid)


def cpu_seconds(pid):
    """User plus system CPU time of pid, from /proc."""
    with open(f"/proc/{pid}/stat") as fh:
        fields = fh.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="multi-agent-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = os.path.join(self.tmp, "project")
        self.record = os.path.join(self.tmp, "record")
        self.agent_dir = os.path.join(self.tmp, "agent")
        for path in (self.project, self.record, self.agent_dir):
            os.makedirs(path)
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("MULTI_AGENT_", "FAKE_PI_", "PI_", "HERDR_"))
        }
        env.update(
            {
                "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                "PI_CODING_AGENT_DIR": self.agent_dir,
                "MULTI_AGENT_PI_BIN": FAKE_PI,
                "MULTI_AGENT_TIME_SCALE": TIME_SCALE,
                "FAKE_PI_RECORD_DIR": self.record,
                "PWD": self.project,
                "HERDR_ENV": "1",
                "HERDR_PANE_ID": "pane",
                "HERDR_SOCKET_PATH": "/nonexistent/herdr.sock",
            }
        )
        self.env = env
        self.addCleanup(self.kill_leftovers)

    def kill_leftovers(self):
        # Each runner and fake-pi has a path under self.tmp in its command line.
        for record in self.records("start"):
            for pid in (record["ppid"], record["pid"]):
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as fh:
                        if os.fsencode(self.tmp) not in fh.read():
                            continue
                    os.kill(pid, signal.SIGCONT)
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def cli(self, *args, env=None, input=None, timeout=60, cwd=None):
        full_env = dict(self.env, **(env or {}))
        return subprocess.run(
            [sys.executable, SCRIPT] + list(args),
            cwd=cwd or self.project,
            env=full_env,
            input=input,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )

    def popen(self, *args, env=None):
        return subprocess.Popen(
            [sys.executable, SCRIPT] + list(args),
            cwd=self.project,
            env=dict(self.env, **(env or {})),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
        )

    def job_dir(self, name):
        matches = glob.glob(
            os.path.join(self.tmp, "state", "pi-multi-agent", "*", "jobs", name)
        )
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def state(self, name, file_name):
        return ma.read_text(os.path.join(self.job_dir(name), file_name), "")

    def records(self, kind):
        path = os.path.join(self.record, kind + ".jsonl")
        text = ma.read_text(path, "") or ""
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def start_record(self, index=-1):
        records = wait_until(lambda: self.records("start"))
        return records[index]

    def runner_pid(self, index=-1):
        """The runner's pid, which is the parent of the fake-pi it started."""
        return self.start_record(index)["ppid"]

    def exit_reasons(self):
        return [r["reason"] for r in self.records("exit")]

    def hold(self):
        """Return env that keeps fake-pi's run from settling, and a function that lets it settle."""
        path = tempfile.mktemp(prefix="release-", dir=self.tmp)

        def release():
            open(path, "w").close()

        return {"FAKE_PI_HOLD": path}, release

    def run_bg(self, name, prompt="task", env=None, extra=()):
        result = self.cli(
            "run", "--background", "--name", name, *extra, prompt, env=env
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result


# --- profiles and arguments ----------------------------------------------------


class FrontmatterTest(unittest.TestCase):
    def test_fields(self):
        text = (
            "---\n"
            'name: "Quoted \\"name\\""\n'
            "description: 'it''s fine'\n"
            "enabled: false\n"
            'tools: [read, "bash", grep]\n'
            "disallowed_tools: write, edit\n"
            "nested:\n"
            "  child: ignored\n"
            "# a comment\n"
            "unknown_field: kept\n"
            "---\n"
            "\n"
            "Body line 1\n"
            "Body line 2\n"
        )
        fields, body = ma.parse_frontmatter(text)
        self.assertEqual(fields["name"], 'Quoted "name"')
        self.assertEqual(fields["description"], "it's fine")
        self.assertIs(fields["enabled"], False)
        self.assertEqual(fields["tools"], ["read", "bash", "grep"])
        self.assertEqual(ma.tool_value(fields["tools"]), "read,bash,grep")
        self.assertEqual(ma.tool_value(fields["disallowed_tools"]), "write,edit")
        self.assertNotIn("child", fields)
        self.assertEqual(fields["unknown_field"], "kept")
        self.assertEqual(body, "Body line 1\nBody line 2")

    def test_no_frontmatter(self):
        self.assertEqual(ma.parse_frontmatter("just a prompt\n"), ({}, "just a prompt"))

    def test_tool_patterns_pass_through(self):
        self.assertEqual(
            ma.tool_value("+codemode, -write, mcp__radius__*"),
            "+codemode,-write,mcp__radius__*",
        )


class ProfileTest(Base):
    def setUp(self):
        Base.setUp(self)
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_profile(self, directory, file_name, text):
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, file_name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_builtin_profiles(self):
        profiles = ma.load_profiles(self.project)
        explore = profiles["explore"]
        self.assertEqual(explore["name"], "Explore")
        self.assertEqual(explore["scope"], "builtin")
        self.assertEqual(explore["tools"], "read,bash,grep,find,ls")
        self.assertEqual(explore["prompt_mode"], "replace")
        self.assertIn("READ-ONLY MODE", explore["body"])
        self.assertIsNone(explore["model"])
        general = profiles["general-purpose"]
        self.assertEqual(general["prompt_mode"], "append")
        self.assertIsNone(general["tools"])
        self.assertEqual(general["body"], "")

    def test_discovery_order_and_whole_file_replacement(self):
        user_dir = os.path.join(self.agent_dir, "agents")
        project_dir = os.path.join(self.project, ".pi", "agents")
        self.write_profile(
            user_dir, "Explore.md", "---\nmodel: user/model\n---\nUser prompt\n"
        )
        self.write_profile(
            user_dir, "only-user.md", "---\ndescription: from user\n---\n"
        )
        explore = ma.load_profiles(self.project)["explore"]
        self.assertEqual(explore["scope"], "user")
        self.assertEqual(explore["model"], "user/model")
        # Nothing is merged from the built-in file.
        self.assertIsNone(explore["tools"])
        self.assertEqual(explore["prompt_mode"], "append")
        self.assertEqual(explore["body"], "User prompt")

        self.write_profile(
            project_dir, "explore.md", "---\nname: Explore\nthinking: low\n---\n"
        )
        profiles = ma.load_profiles(self.project)
        self.assertEqual(profiles["explore"]["scope"], "project")
        self.assertIsNone(profiles["explore"]["model"])
        self.assertEqual(profiles["explore"]["thinking"], "low")
        self.assertEqual(profiles["only-user"]["description"], "from user")

    def test_enabled_false_hides_lower_profiles(self):
        self.write_profile(
            os.path.join(self.agent_dir, "agents"),
            "Explore.md",
            "---\nenabled: false\n---\n",
        )
        with self.assertRaises(ma.CommandError):
            ma.find_profile(self.project, "Explore")
        result = self.cli("profiles")
        self.assertNotIn("Explore", result.stdout)
        self.assertIn("general-purpose", result.stdout)

    def test_case_insensitive_lookup_and_unknown(self):
        self.assertEqual(ma.find_profile(self.project, "EXPLORE")["name"], "Explore")
        with self.assertRaises(ma.CommandError) as ctx:
            ma.find_profile(self.project, "nope")
        self.assertIn("general-purpose", str(ctx.exception))

    def test_profile_args(self):
        prompt_file = os.path.join(self.tmp, "system-prompt.md")
        profile = ma.make_profile(
            "p",
            {
                "tools": "read, bash",
                "disallowed_tools": ["write"],
                "prompt_mode": "replace",
            },
            "Replace prompt",
            "x.md",
            "user",
        )
        args = ma.profile_args(profile, "prov/m", "high", prompt_file)
        self.assertEqual(
            args,
            [
                "--model",
                "prov/m",
                "--thinking",
                "high",
                "--tools",
                "read,bash",
                "-xt",
                "write",
                "--system-prompt",
                prompt_file,
                "-nc",
            ],
        )
        self.assertEqual(ma.read_text(prompt_file), "Replace prompt\n")

        self.assertTrue(
            ma.append_prompt(profile, "job-1").startswith(
                "<multi-agent>\nYou are an agent named job-1,"
            )
        )

        append = ma.make_profile("p", {}, "Extra", "x.md", "user")
        self.assertEqual(ma.profile_args(append, None, None, prompt_file), [])
        self.assertTrue(
            ma.append_prompt(append, "job-1").startswith("Extra\n\n<multi-agent>\n")
        )

    def test_model_and_thinking_precedence(self):
        profile = ma.make_profile(
            "p", {"model": "profile/m", "thinking": "low"}, "", "x.md", "user"
        )
        bare = ma.make_profile("p", {}, "", "x.md", "user")
        os.environ.update(
            {"PI_PROVIDER": "env", "PI_MODEL": "m", "PI_REASONING_LEVEL": "high"}
        )
        self.assertEqual(ma.resolve_model("cli/m", profile), "cli/m")
        self.assertEqual(ma.resolve_model(None, profile), "profile/m")
        self.assertEqual(ma.resolve_model(None, bare), "env/m")
        self.assertEqual(ma.resolve_thinking("max", profile), "max")
        self.assertEqual(ma.resolve_thinking(None, profile), "low")
        self.assertEqual(ma.resolve_thinking(None, bare), "high")
        for key in ("PI_PROVIDER", "PI_MODEL", "PI_REASONING_LEVEL"):
            del os.environ[key]
        self.assertIsNone(ma.resolve_model(None, bare))
        self.assertIsNone(ma.resolve_thinking(None, bare))

    def test_invalid_prompt_mode(self):
        self.write_profile(
            os.path.join(self.agent_dir, "agents"),
            "bad.md",
            "---\nprompt_mode: merge\n---\n",
        )
        result = self.cli("run", "--profile", "bad", "x")
        self.assertEqual(result.returncode, 1)
        self.assertIn("prompt_mode", result.stderr)


class NamesAndStateTest(Base):
    def setUp(self):
        Base.setUp(self)
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_name_rules(self):
        for good in ("a", "explore-3f9a", "A.b_c-9", "x" * 64):
            ma.validate_name(good)
        for bad in ("", "-a", "a-", "a b", "a/b", "x" * 65, ".a", "a."):
            with self.assertRaises(ma.CommandError):
                ma.validate_name(bad)
        name = ma.default_name("Explore")
        self.assertRegex(name, r"^explore-[0-9a-f]{4}$")
        ma.validate_name(ma.default_name("My Profile!"))

    def test_slug(self):
        self.assertEqual(
            ma.project_slug("/home/me/my project.x"), "home-me-my-project-x"
        )

    def make_job(self, name, **files):
        job_dir = os.path.join(ma.jobs_dir(self.project), name)
        os.makedirs(job_dir)
        with open(os.path.join(job_dir, "job.json"), "w") as fh:
            json.dump(
                {"name": name, "profile": "general-purpose", "session_dir": self.tmp},
                fh,
            )
        files.setdefault("lock", "")
        for key, value in files.items():
            with open(os.path.join(job_dir, key), "w") as fh:
                fh.write(value)
        return job_dir

    def test_died_detection(self):
        self.assertEqual(ma.job_state(self.make_job("a", status="running\n")), "died")
        self.assertEqual(
            ma.job_state(self.make_job("b", status="killed\n", exit_code="1\n")),
            "killed",
        )
        self.assertEqual(
            ma.job_state(self.make_job("c", status="done\n", exit_code="0\n")), "done"
        )
        # Only the exit code makes a status final.
        self.assertEqual(ma.job_state(self.make_job("d", status="done\n")), "died")
        # Without a lock file, the files decide.
        no_lock = self.make_job("e", status="done\n", exit_code="0\n")
        os.remove(os.path.join(no_lock, "lock"))
        self.assertEqual(ma.job_state(no_lock), "done")
        # Running while anyone holds the lock exclusively, whatever the files say.
        locked = self.make_job("f", status="done\n", exit_code="0\n")
        fd = ma.job_lock(locked, "busy")
        try:
            self.assertEqual(ma.job_state(locked), "running")
            with (
                mock.patch.object(ma, "LOCK_TIMEOUT_S", 0.2),
                self.assertRaises(ma.CommandError) as ctx,
            ):
                ma.job_lock(locked, "job f is running")
            self.assertEqual(str(ctx.exception), "job f is running")
        finally:
            os.close(fd)
        self.assertEqual(ma.job_state(locked), "done")

    def test_lock_of_a_removed_job(self):
        job_dir = self.make_job("removed", status="done\n", exit_code="0\n")
        shutil.rmtree(job_dir)
        with self.assertRaises(ma.CommandError) as ctx:
            ma.job_lock(job_dir, "busy")
        self.assertIn("was removed", str(ctx.exception))

    def test_lock_of_a_job_removed_while_waiting(self):
        job_dir = self.make_job("removed", status="done\n", exit_code="0\n")
        holder = ma.job_lock(job_dir, "busy")
        remover = mock.patch.object(
            ma.time, "sleep", lambda _s: (shutil.rmtree(job_dir), os.close(holder))
        )
        with remover, self.assertRaises(ma.CommandError) as ctx:
            ma.job_lock(job_dir, "busy")
        self.assertIn("was removed", str(ctx.exception))

    def test_conflict_without_timestamps(self):
        self.make_job("untimed")
        result = self.cli("run", "--name", "untimed", "x")
        self.assertEqual(result.returncode, 1)
        self.assertIn("(died); pick another --name.", result.stderr)
        self.assertFalse(self.records("start"))

    def test_rm_all_when_a_job_starts(self):
        busy = self.make_job("a-busy", status="done\n", exit_code="0\n")
        finished = self.make_job("z-done", status="done\n", exit_code="0\n")
        holder = ma.job_lock(busy, "busy")
        self.addCleanup(os.close, holder)
        output = io.StringIO()
        with (
            mock.patch.object(ma, "logical_cwd", return_value=self.project),
            mock.patch.object(ma, "job_state", return_value="done"),
            mock.patch.object(ma, "LOCK_TIMEOUT_S", 0.01),
            mock.patch.object(ma.sys, "stdout", output),
        ):
            self.assertEqual(ma.cmd_rm(["--all"]), 1)
        self.assertEqual(
            output.getvalue(),
            "error: job a-busy is running; kill it first\nremoved: z-done\n",
        )
        self.assertTrue(os.path.isdir(busy))
        self.assertFalse(os.path.exists(finished))

    def test_socket_path(self):
        job_dir = os.path.join(ma.jobs_dir(self.project), "x")
        path = ma.socket_path(job_dir)
        self.assertEqual(
            os.path.dirname(path),
            os.path.join(self.tmp, "state", "pi-multi-agent", "sockets"),
        )
        self.assertRegex(os.path.basename(path), r"^[0-9a-f]{16}\.sock$")
        self.assertNotEqual(path, ma.socket_path(job_dir + "y"))

    def test_read_only_commands_on_hand_made_jobs(self):
        self.make_job(
            "done-job",
            status="done\n",
            exit_code="0\n",
            started_at="2026-01-01T00:00:00Z\n",
            ended_at="2026-01-01T00:01:24Z\n",
            model="anthropic/claude-x\n",
            **{
                "output.txt": "The answer.\n",
                "progress.log": "".join(f"10:00:{i:02d} bash ls\n" for i in range(30)),
            },
        )
        result = self.cli("result", "done-job")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout,
            "The answer.\n\n--- multi-agent ---\nname: done-job\nstatus: done\nprofile: general-purpose\n"
            "model: anthropic/claude-x\nduration_s: 84\n",
        )
        peek = self.cli("peek", "done-job", "-n", "3")
        self.assertEqual(
            peek.stdout.splitlines(),
            ["10:00:27 bash ls", "10:00:28 bash ls", "10:00:29 bash ls"],
        )
        self.assertEqual(len(self.cli("peek", "done-job").stdout.splitlines()), 20)
        status = self.cli("status", "done-job")
        self.assertIn("status: done\n", status.stdout)
        self.assertIn("--session-id done-job", status.stdout)
        # Ended long ago: hidden from list unless --all.
        self.assertNotIn("done-job", self.cli("list").stdout)
        self.assertIn("done-job", self.cli("list", "--all").stdout)

        self.make_job("dead-job", status="running\n")
        self.assertIn("status: died", self.cli("status", "dead-job").stdout)
        result = self.cli("wait", "dead-job")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status: died", result.stdout)

    def test_lookup_across_projects(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self.make_job("elsewhere", status="done\n", exit_code="0\n")
        result = self.cli("status", "elsewhere", cwd=other, env={"PWD": other})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("name: elsewhere", result.stdout)
        result = self.cli("status", "missing")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown job", result.stderr)

    def test_usage_errors(self):
        self.assertEqual(self.cli().returncode, 64)
        self.assertEqual(self.cli("bogus").returncode, 64)
        self.assertEqual(self.cli("run").returncode, 64)
        self.assertEqual(self.cli("run", "--nope", "x").returncode, 64)
        self.assertEqual(self.cli("run", "--timeout", "abc", "x").returncode, 64)
        self.assertEqual(self.cli("run", "--name", "bad name", "x").returncode, 64)
        self.assertEqual(self.cli("peek", "a", "-n", "x").returncode, 64)


# --- running jobs ----------------------------------------------------------------


class ForegroundTest(Base):
    def test_result_format_start_line_and_exit_code(self):
        result = self.cli("run", "--name", "fg1", "hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("multi-agent: started job fg1\n", result.stderr)
        self.assertTrue(
            result.stdout.startswith(
                "reply to: hello (turn 1)\n\n--- multi-agent ---\n"
            )
        )
        self.assertIn(
            "\nname: fg1\nstatus: done\nprofile: general-purpose\nmodel: fake/fake-model\n",
            result.stdout,
        )
        self.assertRegex(result.stdout, r"duration_s: \d+\n$")
        self.assertEqual(self.state("fg1", "exit_code"), "0\n")
        usage = json.loads(self.state("fg1", "usage.json"))
        self.assertEqual(usage["totalTokens"], 18)

    def test_default_name(self):
        result = self.cli("run", "--profile", "explore", "hello")
        self.assertRegex(result.stderr, r"started job explore-[0-9a-f]{4}\n")

    def test_prompt_from_stdin(self):
        result = self.cli("run", "--name", "stdin1", "-", input="line one\nline two\n")
        self.assertIn("reply to: line one\nline two (turn 1)", result.stdout)
        self.assertEqual(self.state("stdin1", "prompt.txt"), "line one\nline two")

    def test_pi_arguments_and_child_environment(self):
        result = self.cli(
            "run",
            "--name",
            "args1",
            "--profile",
            "Explore",
            "--model",
            "p/m",
            "--thinking",
            "low",
            "x",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        start = self.start_record()
        argv = start["argv"]
        job_dir = self.job_dir("args1")
        session_dir = os.path.join(
            self.agent_dir, "multi-agent-sessions", ma.project_slug(self.project)
        )
        self.assertEqual(
            argv[:6],
            ["--mode", "rpc", "--session-dir", session_dir, "--session-id", "args1"],
        )
        self.assertIn("--model p/m", " ".join(argv))
        self.assertIn("--thinking low", " ".join(argv))
        self.assertIn("--tools read,bash,grep,find,ls", " ".join(argv))
        index = argv.index("--system-prompt")
        self.assertEqual(argv[index + 1], os.path.join(job_dir, "system-prompt.md"))
        self.assertEqual(argv[index + 2], "-nc")
        self.assertEqual(
            argv[-2:],
            ["--append-system-prompt", os.path.join(job_dir, "append-prompt.md")],
        )
        self.assertNotIn("-e", argv)
        # Explore replaces the prompt, so only the multi-agent block is appended.
        self.assertTrue(
            self.state("args1", "append-prompt.md").startswith(
                "<multi-agent>\nYou are an agent named args1,"
            )
        )

        env = start["env"]
        self.assertEqual(env["MULTI_AGENT_JOB"], job_dir)
        self.assertEqual(env["MULTI_AGENT_DEPTH"], "1")
        self.assertEqual(env["PI_MINIMAL_PERMISSION_SYSTEM_NONINTERACTIVE"], "1")
        self.assertEqual(env["PI_REQUEST_USER_INPUT_NONINTERACTIVE"], "1")
        self.assertFalse([k for k in env if k.startswith("HERDR_")])
        # pi leads its own session, apart from the runner's process group.
        self.assertEqual(start["sid"], start["pid"])
        self.assertEqual(start["pgid"], start["pid"])
        self.assertEqual(start["cwd"], os.path.realpath(self.project))

    def test_caller_model_is_inherited(self):
        self.cli(
            "run",
            "--name",
            "inherit",
            "x",
            env={
                "PI_PROVIDER": "prov",
                "PI_MODEL": "mod",
                "PI_REASONING_LEVEL": "high",
            },
        )
        argv = " ".join(self.start_record()["argv"])
        self.assertIn("--model prov/mod", argv)
        self.assertIn("--thinking high", argv)

    def test_parent_and_depth_of_nested_job(self):
        parent = os.path.join(self.tmp, "state", "parent-job")
        result = self.cli(
            "run",
            "--name",
            "child",
            "x",
            env={"MULTI_AGENT_JOB": parent, "MULTI_AGENT_DEPTH": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.start_record()["env"]["MULTI_AGENT_DEPTH"], "2")
        self.assertIn("parent: parent-job", self.cli("status", "child").stdout)

    def test_depth_limit(self):
        result = self.cli("run", "x", env={"MULTI_AGENT_DEPTH": "2"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("MULTI_AGENT_MAX_DEPTH", result.stderr)
        self.assertFalse(self.records("start"))
        result = self.cli(
            "run", "x", env={"MULTI_AGENT_DEPTH": "2", "MULTI_AGENT_MAX_DEPTH": "3"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_stop_reason(self):
        result = self.cli(
            "run",
            "--name",
            "err",
            "x",
            env={
                "FAKE_PI_STOP_REASON": "error",
                "FAKE_PI_ERROR_MESSAGE": "rate limited",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: rate limited", result.stdout)
        self.assertIn("status: failed", result.stdout)

    def test_rejected_prompt(self):
        result = self.cli(
            "run", "--name", "rej", "x", env={"FAKE_PI_REJECT_PROMPT": "1"}
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: fake rejection", result.stdout)
        self.assertIn("stdin_closed", self.exit_reasons())

    def test_handled_prompt(self):
        result = self.cli(
            "run", "--name", "handled", "/cmd", env={"FAKE_PI_HANDLED": "1"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status: done", result.stdout)

    def test_pi_exits_early(self):
        result = self.cli(
            "run", "--name", "early", "x", env={"FAKE_PI_EXIT_EARLY": "1"}
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "pi exited before the run finished (exit status 3)", result.stdout
        )
        self.assertIn("fake-pi: crashed on purpose", result.stdout)
        self.assertIn("status: failed", result.stdout)

    def test_missing_pi(self):
        missing = {"MULTI_AGENT_PI_BIN": "no-such-pi"}
        result = self.cli("run", "--name", "nopi", "x", env=missing)
        self.assertEqual(result.returncode, 1)
        self.assertIn('cannot find the pi program "no-such-pi"', result.stderr)
        self.assertIn("MULTI_AGENT_PI_BIN", result.stderr)
        self.assertFalse(
            glob.glob(os.path.join(self.tmp, "state", "*", "*", "jobs", "nopi"))
        )
        self.assertEqual(self.cli("run", "--name", "nopi", "x").returncode, 0)
        result = self.cli("resume", "nopi", "y", env=missing)
        self.assertEqual(result.returncode, 1)
        self.assertIn("MULTI_AGENT_PI_BIN", result.stderr)
        session_cmd = f"{shlex.quote(FAKE_PI)} --session-dir"
        self.assertIn(session_cmd, self.cli("status", "nopi").stdout)

    def test_unicode_line_separators(self):
        env = {"FAKE_PI_TOOL": "echo a b", "FAKE_PI_REPLY": "first second third"}
        result = self.cli("run", "--name", "u2028", "x", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("first second third\n"))
        self.assertIn("bash echo a b", self.state("u2028", "progress.log"))

    def test_dialog_requests_are_cancelled(self):
        for method in ("confirm", "select", "input", "editor"):
            result = self.cli(
                "run", "--name", "dialog-" + method, "x", env={"FAKE_PI_DIALOG": method}
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "denied: Fake dialog", self.state("dialog-" + method, "progress.log")
            )
        responses = [r["response"] for r in self.records("dialog")]
        self.assertEqual(len(responses), 4)
        for response in responses:
            self.assertEqual(
                response,
                {"type": "extension_ui_response", "id": "dialog-1", "cancelled": True},
            )

    def test_timeout(self):
        start = time.monotonic()
        result = self.cli(
            "run",
            "--name",
            "slow",
            "--timeout",
            "0.2",
            "x",
            env={"FAKE_PI_DELAY": "30"},
        )
        self.assertLess(time.monotonic() - start, 15)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status: timeout", result.stdout)
        self.assertIn("reply to: x (turn 1)", result.stdout)
        self.assertIn("timeout after 0.2s", self.state("slow", "progress.log"))

    @unittest.skipUnless(os.path.isdir("/proc/self"), "needs /proc")
    def test_timeout_when_pi_does_not_settle(self):
        env = {"FAKE_PI_DELAY": "120", "FAKE_PI_IGNORE_ABORT": "1"}
        self.run_bg("unsettled", env=env, extra=("--timeout", "0.2"))
        runner = self.runner_pid()
        wait_until(
            lambda: "timeout after 0.2s" in self.state("unsettled", "progress.log")
        )
        # The runner waits for pi to settle without spinning. The sample ends
        # well before SETTLE_TIMEOUT_S, while the runner still exists.
        self.assertGreater(ma.SETTLE_TIMEOUT_S, 2)
        before = cpu_seconds(runner)
        time.sleep(1)
        self.assertLess(cpu_seconds(runner) - before, 0.2)
        result = self.cli("wait", "unsettled", "--timeout", "45")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("status: timeout", result.stdout)
        self.assertTrue(result.stdout.startswith("(no output)\n"))
        self.assertIn(
            "agent did not settle after the abort",
            self.state("unsettled", "progress.log"),
        )
        self.assertEqual(self.records("exit")[-1]["reason"], "stdin_closed")

    def test_socket_path_too_long(self):
        state = os.path.join(self.tmp, "s" * 80)
        result = self.cli("run", "--name", "long", "x", env={"XDG_STATE_HOME": state})
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "the socket path for this job is too long; set XDG_STATE_HOME to a shorter path",
            result.stderr,
        )
        self.assertFalse(
            glob.glob(os.path.join(state, "pi-multi-agent", "*", "jobs", "*"))
        )
        self.assertFalse(self.records("start"))

    def test_existing_name_is_refused(self):
        self.assertEqual(self.cli("run", "--name", "dup", "x").returncode, 0)
        ended = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 86400)
        )
        ma.write_atomic(os.path.join(self.job_dir("dup"), "ended_at"), ended + "\n")
        result = self.cli("run", "--name", "dup", "y")
        self.assertEqual(result.returncode, 1)
        self.assertIn("(done, ended 3d ago); pick another --name.", result.stderr)
        guidance = "Run `resume dup` or `rm dup` only for a job you started or the user asked about."
        self.assertIn(guidance, result.stderr)
        self.assertEqual(len(self.records("start")), 1)

        hold, release = self.hold()
        self.run_bg("busy-dup", env=hold)
        wait_until(lambda: len(self.records("start")) == 2)
        result = self.cli("run", "--name", "busy-dup", "y")
        self.assertEqual(result.returncode, 1)
        self.assertRegex(
            result.stderr, r"\(running, started \d+s ago\); pick another --name\."
        )
        self.assertIn(
            "Run `resume busy-dup` or `rm busy-dup` only for a job you started or the user asked about.",
            result.stderr,
        )
        self.assertEqual(len(self.records("start")), 2)
        release()
        self.assertEqual(self.cli("wait", "busy-dup").returncode, 0)

    def test_signals_stop_the_run(self):
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            name = f"sig-{int(signum)}"
            proc = self.popen("run", "--name", name, "x", env={"FAKE_PI_DELAY": "60"})
            wait_until(
                lambda: (
                    os.path.exists(os.path.join(self.record, "start.jsonl"))
                    and len(self.records("start")) == len(self.records("exit")) + 1
                )
            )
            pi_pid = self.start_record()["pid"]
            time.sleep(0.3)
            proc.send_signal(signum)
            out, _err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("status: killed", out)
            self.assertEqual(self.state(name, "status"), "killed\n")
            wait_until(lambda pi_pid=pi_pid: pid_gone(pi_pid))
            self.assertEqual(
                self.records("exit")[-1], {"reason": "stdin_closed", "pid": pi_pid}
            )
            self.assertIn(
                "stopped: the caller's command ended", self.state(name, "progress.log")
            )

    def test_runner_is_detached_from_the_caller(self):
        hold, release = self.hold()
        proc = self.popen("run", "--name", "detached", "x", env=hold)
        start = self.start_record()
        runner = start["ppid"]
        self.assertNotEqual(runner, proc.pid)
        # The runner leads its own session, apart from the foreground
        # command's and from pi's.
        self.assertEqual(os.getsid(runner), runner)
        self.assertNotEqual(os.getsid(runner), os.getsid(proc.pid))
        self.assertNotIn(start["sid"], (os.getsid(0), os.getsid(proc.pid), runner))
        release()
        proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0)

    def caller_in_own_group(self, *args, env=None):
        """Start a foreground run the way a bash tool does: in its own process group."""
        return subprocess.Popen(
            [sys.executable, SCRIPT] + list(args),
            cwd=self.project,
            env=dict(self.env, **(env or {})),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def test_sigkill_to_the_caller_stops_the_run(self):
        caller = self.caller_in_own_group(
            "run", "--name", "orphaned", "x", env={"FAKE_PI_DELAY": "60"}
        )
        self.addCleanup(caller.communicate)
        pi_pid = self.start_record()["pid"]
        runner = self.runner_pid()
        os.killpg(caller.pid, signal.SIGKILL)
        wait_until(lambda: pid_gone(pi_pid))
        wait_until(lambda: pid_gone(runner))
        self.assertEqual(
            self.records("exit")[-1], {"reason": "stdin_closed", "pid": pi_pid}
        )
        self.assertEqual(self.state("orphaned", "status"), "killed\n")
        self.assertIn(
            "stopped: the caller's command ended",
            self.state("orphaned", "progress.log"),
        )

    def test_nested_foreground_runs_stop_together(self):
        # The outer agent runs the inner foreground run as its tool call, and
        # kills that call's process group when its stdin closes, as pi does.
        inner = f"env -u FAKE_PI_RUN FAKE_PI_DELAY=60 {shlex.quote(sys.executable)} {shlex.quote(SCRIPT)} run --name inner x"
        caller = self.caller_in_own_group(
            "run", "--name", "outer", "x", env={"FAKE_PI_RUN": inner}
        )
        self.addCleanup(caller.communicate)
        starts = wait_until(
            lambda: len(self.records("start")) == 2 and self.records("start")
        )
        pis = [record["pid"] for record in starts]
        inner_caller = self.records("tool")[0]["tool_pid"]
        self.assertEqual(self.start_record()["env"]["MULTI_AGENT_DEPTH"], "2")
        runners = [record["ppid"] for record in starts]
        os.killpg(caller.pid, signal.SIGKILL)
        for pid in pis + runners + [inner_caller]:
            wait_until(lambda pid=pid: pid_gone(pid))
        self.assertEqual(
            sorted(r["reason"] for r in self.records("exit")),
            ["stdin_closed", "stdin_closed"],
        )
        self.assertEqual(self.state("outer", "status"), "killed\n")
        self.assertEqual(self.state("inner", "status"), "killed\n")


class BackgroundTest(Base):
    def test_detach_wait_and_result(self):
        hold, release = self.hold()
        result = self.run_bg("bg1", env=hold)
        self.assertEqual(result.stdout, "job: bg1\nstatus: running\ntimeout_s: 3600\n")
        self.assertIn("status: running", self.cli("status", "bg1").stdout)
        running = self.cli("result", "bg1")
        self.assertEqual(running.returncode, 1)
        self.assertIn("still running", running.stderr)
        waited = self.cli("wait", "bg1", "--timeout", "0.2")
        self.assertEqual(waited.returncode, 2)
        self.assertIn("status: running", waited.stdout)
        release()
        done = self.cli("wait", "bg1")
        self.assertEqual(done.returncode, 0)
        self.assertIn("reply to: task (turn 1)", done.stdout)
        self.assertIn("status: done", done.stdout)
        self.assertEqual(self.cli("result", "bg1").stdout, done.stdout)
        self.assertIn("mode: background", self.cli("status", "bg1").stdout)

    def test_survives_the_callers_exit(self):
        # The caller runs in its own process group, which is then killed.
        hold, release = self.hold()
        caller = subprocess.Popen(
            [sys.executable, SCRIPT, "run", "--background", "--name", "orphan", "x"],
            cwd=self.project,
            env=dict(self.env, **hold),
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        caller.communicate(timeout=30)
        try:
            os.killpg(caller.pid, signal.SIGKILL)
        except OSError:
            pass
        self.assertTrue(alive(self.runner_pid()))
        release()
        result = self.cli("wait", "orphan")
        self.assertEqual(result.returncode, 0)
        self.assertIn("status: done", result.stdout)

    def test_timeout_flag(self):
        result = self.run_bg("t0", extra=("--timeout", "0"))
        self.assertIn("timeout_s: none", result.stdout)
        self.cli("wait", "t0")

    def test_steer_and_not_running(self):
        hold, release = self.hold()
        self.run_bg("st", env=hold)
        # Sent at once: the runner is still starting, and the request waits
        # in the socket's listen backlog until the runner accepts it.
        steer = self.cli("steer", "st", "-", input="look at Y too\n")
        self.assertEqual(steer.returncode, 0, steer.stderr)
        self.assertEqual(steer.stdout, "job: st\nsteer: queued\n")
        release()
        result = self.cli("wait", "st")
        self.assertIn("| steered: look at Y too", result.stdout)
        self.assertIn("steer: look at Y too", self.state("st", "progress.log"))
        late = self.cli("steer", "st", "more")
        self.assertEqual(late.returncode, 1)
        self.assertIn("job is not running; use resume", late.stderr)
        # The runner removes its socket when it exits.
        self.assertFalse(os.path.exists(ma.socket_path(self.job_dir("st"))))

    def test_abort_keeps_output(self):
        self.run_bg("ab", env={"FAKE_PI_DELAY": "60"})
        wait_until(lambda: self.records("start"))
        result = self.cli("abort", "ab")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "job: ab\nabort: requested\n")
        waited = self.cli("wait", "ab", "--timeout", "15")
        self.assertEqual(waited.returncode, 1)
        self.assertIn("reply to: task (turn 1)", waited.stdout)
        self.assertIn("status: aborted", waited.stdout)
        late = self.cli("abort", "ab")
        self.assertEqual(late.returncode, 1)
        self.assertIn("job is not running; use resume", late.stderr)

    def test_resume_and_rm(self):
        self.run_bg("res")
        self.cli("wait", "res")
        result = self.cli("resume", "res", "second")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reply to: second (turn 2)", result.stdout)
        self.assertEqual(self.state("res", "runs"), "2\n")
        argv = self.records("start")[-1]["argv"]
        self.assertNotIn("--session", argv)
        self.assertIn("--session-id", argv)
        background = self.cli(
            "resume", "res", "--background", "--timeout", "100", "third"
        )
        self.assertIn("timeout_s: 100", background.stdout)
        self.assertIn("(turn 3)", self.cli("wait", "res").stdout)

        session_dir = os.path.join(
            self.agent_dir, "multi-agent-sessions", ma.project_slug(self.project)
        )
        self.assertEqual(len(ma.find_session_file(session_dir, "res")), 1)
        removed = self.cli("rm", "res")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(ma.find_session_file(session_dir, "res"), [])
        self.assertFalse(
            glob.glob(
                os.path.join(self.tmp, "state", "pi-multi-agent", "*", "jobs", "res")
            )
        )

    def test_output_comes_from_this_run(self):
        # A resumed run that ends before the agent's first message has no
        # output, rather than the previous run's answer.
        self.run_bg("early", env={"FAKE_PI_REPLY": "first answer"})
        self.assertIn("first answer", self.cli("wait", "early").stdout)
        self.cli(
            "resume", "early", "--background", "again", env={"FAKE_PI_DELAY": "60"}
        )
        wait_until(lambda: len(self.records("start")) == 2)
        self.cli("kill", "early")
        result = self.cli("result", "early")
        self.assertTrue(result.stdout.startswith("(no output)\n"), result.stdout)
        self.assertIn("status: killed", result.stdout)

        # Nor does a forked run that ends early report the caller's answer.
        source = os.path.join(self.tmp, "caller.jsonl")
        with open(source, "w") as fh:
            fh.write(
                json.dumps({"prompt": "earlier"})
                + "\n"
                + json.dumps({"reply": "the caller's answer"})
                + "\n"
            )
        self.cli(
            "run",
            "--background",
            "--fork",
            "--name",
            "forked-early",
            "x",
            env={"PI_SESSION_FILE": source, "FAKE_PI_DELAY": "60"},
        )
        wait_until(lambda: len(self.records("start")) == 3)
        self.cli("kill", "forked-early")
        result = self.cli("result", "forked-early")
        self.assertTrue(result.stdout.startswith("(no output)\n"), result.stdout)

    def test_list_while_a_resume_starts(self):
        # A starter resets the job's files under the lock, so no reader ever
        # sees a run that has no runner yet.
        self.run_bg("starting")
        self.cli("wait", "starting")
        job_dir = self.job_dir("starting")
        for turn in range(2, 5):
            hold, release = self.hold()
            proc = self.popen("resume", "starting", "--background", "again", env=hold)
            states = set()
            while proc.poll() is None or len(self.records("start")) < turn:
                states.add(ma.job_state(job_dir))
                time.sleep(0.001)
            _out, err = proc.communicate()
            self.assertEqual(proc.returncode, 0, err)
            self.assertIn("starting", self.cli("list").stdout)
            release()
            self.assertIn(
                f"reply to: again (turn {turn})", self.cli("wait", "starting").stdout
            )
            self.assertLessEqual(states, {"done", "running"})

    def test_concurrent_resumes_start_one_run(self):
        self.run_bg("twice")
        self.cli("wait", "twice")
        procs = [
            self.popen(
                "resume", "twice", "--background", "again", env={"FAKE_PI_DELAY": "60"}
            )
            for _ in range(2)
        ]
        outputs = [proc.communicate(timeout=30) for proc in procs]
        codes = sorted(proc.returncode for proc in procs)
        self.assertEqual(codes, [0, 1], outputs)
        self.assertIn("is running", "".join(err for _out, err in outputs))
        time.sleep(1)
        self.assertEqual(len(self.records("start")), 2)
        self.assertEqual(self.state("twice", "runs"), "2\n")
        self.cli("kill", "twice")

    def test_resume_without_its_directory(self):
        sub = os.path.join(self.project, "sub")
        os.makedirs(sub)
        self.run_bg("gone", extra=("--cwd", "sub"))
        done = self.cli("wait", "gone").stdout
        os.rmdir(sub)
        result = self.cli("resume", "gone", "again")
        self.assertEqual(result.returncode, 1)
        self.assertIn("working directory no longer exists", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        # The previous result is untouched.
        self.assertEqual(self.cli("result", "gone").stdout, done)

    def test_runner_that_cannot_start(self):
        self.run_bg("nostart")
        self.cli("wait", "nostart")
        job_dir = self.job_dir("nostart")
        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            mock.patch.object(
                ma.subprocess, "Popen", side_effect=OSError("no such program")
            ),
            mock.patch.object(ma, "logical_cwd", return_value=self.project),
            self.assertRaises(ma.CommandError) as ctx,
        ):
            ma.cmd_resume(["nostart", "again"])
        self.assertIn("could not start the runner", str(ctx.exception))
        self.assertEqual(ma.job_state(job_dir), "failed")
        result = self.cli("result", "nostart")
        self.assertIn("status: failed", result.stdout)
        self.assertIn(
            "error: could not start the runner: no such program", result.stdout
        )

    def test_resume_or_rm_of_running_job_is_refused(self):
        self.run_bg("busy", env={"FAKE_PI_DELAY": "60"})
        result = self.cli("resume", "busy", "again")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is running", result.stderr)
        self.assertEqual(self.cli("rm", "busy").returncode, 1)
        self.cli("kill", "busy")

    def test_fork(self):
        result = self.cli("run", "--fork", "x")
        self.assertEqual(result.returncode, 1)
        self.assertIn("PI_SESSION_FILE", result.stderr)
        source = os.path.join(self.tmp, "caller.jsonl")
        with open(source, "w") as fh:
            fh.write(json.dumps({"prompt": "earlier"}) + "\n")
        result = self.cli(
            "run", "--fork", "--name", "forked", "x", env={"PI_SESSION_FILE": source}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(turn 2)", result.stdout)
        argv = self.start_record()["argv"]
        self.assertEqual(argv[argv.index("--fork") + 1], source)
        # A resumed fork continues its own session.
        self.cli("resume", "forked", "y")
        self.assertNotIn("--fork", self.records("start")[-1]["argv"])

    def test_list(self):
        self.run_bg("l1", env={"FAKE_PI_DELAY": "60"})
        self.cli("run", "--name", "l2", "x")
        rows = self.cli("list").stdout.splitlines()
        self.assertEqual(rows[0].split(), ["NAME", "STATUS", "MODE", "PARENT", "AGE"])
        self.assertEqual(rows[1].split()[:4], ["l1", "running", "background", "-"])
        self.assertEqual(rows[2].split()[:4], ["l2", "done", "foreground", "-"])
        self.cli("kill", "l1")


class KillTest(Base):
    def test_kill_name(self):
        self.run_bg("k1", env={"FAKE_PI_DELAY": "60"})
        pi_pid = self.start_record()["pid"]
        result = self.cli("kill", "k1")
        self.assertEqual(result.stdout, "job: k1\nstatus: killed\n")
        self.assertTrue(pid_gone(pi_pid))
        self.assertIn("stdin_closed", self.exit_reasons())
        self.assertEqual(self.state("k1", "exit_code"), "1\n")
        again = self.cli("kill", "k1")
        self.assertEqual(again.returncode, 0)
        self.assertIn("not running", again.stdout)

    def test_kill_foreground_job(self):
        proc = self.popen("run", "--name", "kf", "x", env={"FAKE_PI_DELAY": "60"})
        self.start_record()
        result = self.cli("kill", "kf")
        self.assertEqual(result.stdout, "job: kf\nstatus: killed\n")
        out, _err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("status: killed", out)

    def test_kill_all(self):
        for name in ("ka1", "ka2"):
            self.run_bg(name, env={"FAKE_PI_DELAY": "60"})
        self.cli("run", "--name", "ka3", "x")
        wait_until(lambda: len(self.records("start")) == 3)
        start = time.monotonic()
        result = self.cli("kill", "--all")
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(
            result.stdout, "job: ka1\nstatus: killed\njob: ka2\nstatus: killed\n"
        )
        self.assertEqual(self.state("ka3", "status"), "done\n")
        self.assertEqual(
            self.cli("kill", "--all").stdout, "no running jobs in this project\n"
        )

    def sigkill_runner(self, name):
        """Start a background job and SIGKILL its runner, which leaves its socket file behind."""
        count = len(self.records("start"))
        self.run_bg(name, env={"FAKE_PI_DELAY": "60"})
        wait_until(lambda: len(self.records("start")) > count)
        pi_pid = self.start_record()["pid"]
        os.kill(self.runner_pid(), signal.SIGKILL)
        wait_until(lambda: pid_gone(pi_pid))
        self.assertEqual(
            self.records("exit")[-1], {"reason": "stdin_closed", "pid": pi_pid}
        )
        self.assertTrue(os.path.exists(ma.socket_path(self.job_dir(name))))

    def test_sigkill_to_runner(self):
        self.sigkill_runner("sk")
        self.assertIn("status: died", self.cli("status", "sk").stdout)
        result = self.cli("wait", "sk")
        self.assertEqual(result.returncode, 1)
        self.assertIn("status: died", result.stdout)
        # The leftover socket file is never trusted.
        for args in (("steer", "sk", "more"), ("abort", "sk")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 1)
            self.assertIn("job is not running; use resume", result.stderr)
        self.assertEqual(
            self.cli("kill", "sk").stdout, "job: sk\nstatus: died (not running)\n"
        )
        # resume binds over the leftover socket file.
        hold, release = self.hold()
        result = self.cli("resume", "sk", "--background", "again", env=hold)
        self.assertEqual(result.returncode, 0, result.stderr)
        steer = self.cli("steer", "sk", "and this")
        self.assertEqual(steer.stdout, "job: sk\nsteer: queued\n", steer.stderr)
        release()
        result = self.cli("wait", "sk")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("reply to: again (turn 2) | steered: and this", result.stdout)

    def test_rm(self):
        self.run_bg("rm-busy", env={"FAKE_PI_DELAY": "60"})
        self.start_record()
        start = time.monotonic()
        result = self.cli("rm", "rm-busy")
        self.assertEqual(result.returncode, 1)
        self.assertIn("job rm-busy is running; kill it first", result.stderr)
        self.assertLess(time.monotonic() - start, 2 * ma.LOCK_TIMEOUT_S)
        self.assertIn("status: running", self.cli("status", "rm-busy").stdout)
        self.cli("kill", "rm-busy")

        self.sigkill_runner("rm-died")
        sock = ma.socket_path(self.job_dir("rm-died"))
        result = self.cli("rm", "rm-died")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(sock))

    def test_rm_all(self):
        self.assertEqual(
            self.cli("rm", "--all").stdout, "no jobs to remove in this project\n"
        )
        self.assertEqual(self.cli("run", "--name", "rm-done", "x").returncode, 0)
        self.assertEqual(
            self.cli(
                "run", "--name", "rm-failed", "x", env={"FAKE_PI_EXIT_EARLY": "1"}
            ).returncode,
            1,
        )
        done_dir = self.job_dir("rm-done")
        failed_dir = self.job_dir("rm-failed")
        session_dir = json.loads(self.state("rm-done", "job.json"))["session_dir"]
        sessions = [
            path
            for name in ("rm-done", "rm-failed")
            for path in ma.find_session_file(session_dir, name)
        ]
        self.assertEqual(len(sessions), 2)
        other = os.path.join(self.tmp, "other-project")
        os.makedirs(other)
        self.assertEqual(
            self.cli("run", "--name", "rm-done", "x", cwd=other).returncode, 0
        )
        other_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(done_dir))),
            ma.project_slug(other),
            "jobs",
            "rm-done",
        )
        other_sessions = ma.find_session_file(
            json.loads(ma.read_text(os.path.join(other_dir, "job.json")))[
                "session_dir"
            ],
            "rm-done",
        )
        self.assertEqual(len(other_sessions), 1)
        hold, release = self.hold()
        self.run_bg("rm-running", env=hold)
        running_dir = self.job_dir("rm-running")

        result = self.cli("rm", "--all", "rm-done")
        self.assertEqual(result.returncode, 64)
        self.assertIn("rm takes a job name or --all, not both", result.stderr)
        self.assertTrue(os.path.isdir(done_dir))
        result = self.cli("rm", "--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "removed: rm-done\nremoved: rm-failed\n")
        for path in (done_dir, failed_dir, *sessions):
            self.assertFalse(os.path.exists(path), path)
        for path in (running_dir, other_dir, *other_sessions):
            self.assertTrue(os.path.exists(path), path)
        self.assertEqual(ma.job_state(running_dir), "running")
        result = self.cli("rm", "--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "no jobs to remove in this project\n")
        release()
        self.assertEqual(self.cli("wait", "rm-running").returncode, 0)

    def test_stuck_runner(self):
        self.run_bg("stuck", env={"FAKE_PI_DELAY": "60"})
        pi_pid = self.start_record()["pid"]
        runner = self.runner_pid()
        os.kill(runner, signal.SIGSTOP)
        start = time.monotonic()
        result = self.cli("kill", "stuck")
        self.assertLess(time.monotonic() - start, 1.5 * ma.ACK_TIMEOUT_S)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "the runner of job stuck is not responding; to stop it, run: pkill -KILL -f ",
            result.stderr,
        )
        self.assertIn("status: running", self.cli("status", "stuck").stdout)
        # The printed command stops the runner, and nothing else.
        command = result.stderr.split("run: ", 1)[1].strip()
        self.assertEqual(
            subprocess.run(["/bin/sh", "-c", command], check=False).returncode, 0
        )
        wait_until(lambda: pid_gone(runner))
        wait_until(lambda: pid_gone(pi_pid))
        self.assertIn("status: died", self.cli("status", "stuck").stdout)

    def test_signals_to_the_runner(self):
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            name = f"rsig-{int(signum)}"
            self.run_bg(name, env={"FAKE_PI_DELAY": "60"})
            pi_pid = wait_until(
                lambda: (
                    len(self.records("start")) == len(self.records("exit")) + 1
                    and self.start_record()["pid"]
                )
            )
            os.kill(self.runner_pid(), signum)
            wait_until(lambda name=name: self.state(name, "status") == "killed\n")
            wait_until(lambda pi_pid=pi_pid: pid_gone(pi_pid))
            self.assertEqual(
                self.records("exit")[-1], {"reason": "stdin_closed", "pid": pi_pid}
            )
            self.assertIn(
                f"stopped by {signal.Signals(signum).name}",
                self.state(name, "progress.log"),
            )

    def test_kill_waits_for_a_slow_shutdown(self):
        # pi's shutdown outlasts PI_EXIT_GRACE_S, and pi ignores SIGTERM, so
        # it ends before the SIGKILL only if kill keeps reading its stdout.
        # kill must not take that reader away before pi has finished.
        slow_exit = ma.PI_EXIT_GRACE_S + ma.PI_TERM_GRACE_S / 2
        env = {
            "FAKE_PI_DELAY": "60",
            "FAKE_PI_SLOW_EXIT": str(slow_exit),
            "FAKE_PI_IGNORE_SIGTERM": "1",
        }
        self.run_bg("slow-exit", env=env)
        pi_pid = self.start_record()["pid"]
        start = time.monotonic()
        result = self.cli("kill", "slow-exit", timeout=60)
        self.assertGreater(time.monotonic() - start, ma.PI_EXIT_GRACE_S)
        self.assertEqual(result.stdout, "job: slow-exit\nstatus: killed\n")
        self.assertTrue(pid_gone(pi_pid))
        self.assertEqual(
            self.records("exit")[-1], {"reason": "stdin_closed", "pid": pi_pid}
        )

    def test_kill_escalates_when_pi_ignores_everything(self):
        env = {
            "FAKE_PI_DELAY": "120",
            "FAKE_PI_IGNORE_SIGTERM": "1",
            "FAKE_PI_IGNORE_EOF": "1",
        }
        self.run_bg("stubborn", env=env)
        pi_pid = self.start_record()["pid"]
        result = self.cli("kill", "stubborn", timeout=60)
        self.assertEqual(result.stdout, "job: stubborn\nstatus: killed\n")
        wait_until(lambda: pid_gone(pi_pid), timeout=5)


if __name__ == "__main__":
    unittest.main()
