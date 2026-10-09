#!/usr/bin/env python3
# multi_agent.py - start pi agents as separate `pi --mode rpc` processes and
# manage each one as a named job.
#
# ../SKILL.md explains how an agent uses this script and ../reference.md lists
# every command, option and state file.
#
# Stdlib only, Python 3.9+. Invoke as `python3 multi_agent.py ...` rather than
# relying on the exec bit.

import calendar
import fcntl
import hashlib
import json
import os
import re
import secrets
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback

# Prompts, job state and agent output are UTF-8 whatever the caller's locale.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

SCRIPT_PATH = os.path.abspath(__file__)
SKILL_DIR = os.path.dirname(os.path.dirname(SCRIPT_PATH))
BUILTIN_PROFILES_DIR = os.path.join(SKILL_DIR, "profiles")

RESULT_DELIMITER = "--- multi-agent ---"
DEFAULT_PROFILE = "general-purpose"
NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")
DIALOG_METHODS = ("select", "confirm", "input", "editor")
STOP_SIGNALS = tuple(getattr(signal, n) for n in ("SIGTERM", "SIGHUP", "SIGINT") if hasattr(signal, n))
# Removed from the agent's environment so the herdr extension inside the agent
# does not report state for the caller's pane.
REMOVED_ENV = ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH")
NOT_RUNNING = "job is not running; use resume"

BACKGROUND_TIMEOUT_S = 3600
DEFAULT_MAX_DEPTH = 2
ACK_TIMEOUT_S = 10
SETTLE_TIMEOUT_S = 30
PI_EXIT_GRACE_S = 10
PI_TERM_GRACE_S = 5
# After the runner accepts `kill`, the command waits this long for it to
# exit: the runner's whole stop sequence for pi, plus a margin.
RUNNER_STOP_GRACE_S = PI_EXIT_GRACE_S + PI_TERM_GRACE_S + 5
# Starters hold the job's lock for a moment before they hand it to the
# runner, and readers hold it shared for a moment, so a command that wants it
# exclusively waits this long before it decides that a runner holds it.
LOCK_TIMEOUT_S = 5
# sun_path holds the socket path and its trailing NUL.
SUN_PATH_MAX = 104 if sys.platform == "darwin" or "bsd" in sys.platform else 108
RECENT_S = 24 * 3600

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_STILL_RUNNING = 2
EXIT_USAGE = 64

MULTI_AGENT_BLOCK = """<multi-agent>
You are an agent named {name}, started by another agent to work on one
task. Whoever reads your result sees only your final message, so make it a
complete, self-contained answer to the task. No person is available to
answer questions. If a tool call is blocked because it needs approval, do
not retry it; say in your final message what you could not do and why.
</multi-agent>
"""

USAGE = """\
Usage:
  multi_agent.py run      [options] (PROMPT | -)
  multi_agent.py resume   NAME [--background] [--timeout S] (PROMPT | -)
  multi_agent.py wait     NAME [--timeout S]
  multi_agent.py result   NAME
  multi_agent.py steer    NAME (MESSAGE | -)
  multi_agent.py abort    NAME
  multi_agent.py kill     (NAME | --all)
  multi_agent.py status   NAME
  multi_agent.py peek     NAME [-n N]
  multi_agent.py list     [--all]
  multi_agent.py profiles
  multi_agent.py rm       NAME

Options for run:
  --profile P     Profile name, matched case-insensitively (default general-purpose)
  --name N        Job name (default <profile>-<4 hex digits>)
  --background    Detach the runner and return immediately
  --timeout S     Wall-clock limit in seconds (default 3600 in the background,
                  none in the foreground; 0 means none)
  --fork          Start from a copy of the caller's conversation ($PI_SESSION_FILE)
  --model M       Model pattern passed to `pi --model`
  --thinking L    Thinking level passed to `pi --thinking`
  --cwd DIR       Working directory for the agent (default: the current directory)

A PROMPT or MESSAGE of `-` is read from stdin. See reference.md for details."""


class CommandError(Exception):
    def __init__(self, message, code=EXIT_FAIL):
        Exception.__init__(self, message)
        self.code = code


class NotRunning(CommandError):
    def __init__(self):
        CommandError.__init__(self, NOT_RUNNING)


def usage_error(message):
    return CommandError(message, EXIT_USAGE)


def note(message):
    sys.stderr.write("multi-agent: %s\n" % message)
    sys.stderr.flush()


# --- small helpers ---------------------------------------------------------


def utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_utc(text):
    try:
        return calendar.timegm(time.strptime(text.strip(), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, AttributeError):
        return None


def read_text(path, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def read_json(path, default=None):
    text = read_text(path)
    if text is None:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def read_int(path):
    try:
        return int((read_text(path) or "").strip())
    except ValueError:
        return None


def write_atomic(path, text):
    """Write a state file through a temporary file and a rename."""
    tmp = os.path.join(
        os.path.dirname(path),
        ".%s.tmp-%d-%d" % (os.path.basename(path), os.getpid(), threading.get_ident()),
    )
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def wait_while(check, timeout):
    """Poll check() until it is false; False if it is still true after timeout seconds."""
    end = time.monotonic() + timeout
    while check():
        if time.monotonic() >= end:
            return False
        time.sleep(0.1)
    return True


def format_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def excerpt(text, limit=120):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --- locations ---------------------------------------------------------------


def logical_cwd():
    # Use $PWD when it names the current directory, so symlinked paths keep
    # the name the caller used and the project slug stays stable.
    pwd = os.environ.get("PWD")
    if pwd and os.path.isabs(pwd):
        try:
            if os.path.samefile(pwd, "."):
                return pwd
        except OSError:
            pass
    return os.getcwd()


def project_slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path).lstrip("-")


def state_root():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.abspath(os.path.join(base, "pi-multi-agent"))


def pi_agent_dir():
    return os.path.expanduser(os.environ.get("PI_CODING_AGENT_DIR") or os.path.join("~", ".pi", "agent"))


def jobs_dir(project):
    return os.path.join(state_root(), project_slug(project), "jobs")


def sessions_dir(project):
    return os.path.join(pi_agent_dir(), "multi-agent-sessions", project_slug(project))


def pi_bin():
    return os.environ.get("MULTI_AGENT_PI_BIN") or "pi"


def find_session_file(session_dir, name):
    """pi names a session file `<timestamp>_<id>.jsonl`; the timestamp has no `_`."""
    pattern = re.compile(r"^[^_]+_" + re.escape(name) + r"\.jsonl$")
    try:
        entries = sorted(os.listdir(session_dir))
    except OSError:
        return []
    return [os.path.join(session_dir, e) for e in entries if pattern.match(e)]


# --- profiles ----------------------------------------------------------------


def split_items(text):
    """Split a comma-separated list, keeping commas inside quotes."""
    items, current, quote = [], "", None
    for ch in text:
        if quote:
            current += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            current += ch
        elif ch == ",":
            items.append(current)
            current = ""
        else:
            current += ch
    items.append(current)
    return [item.strip() for item in items if item.strip()]


def parse_scalar(raw):
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if value.startswith("[") and value.endswith("]"):
        return [parse_scalar(item) for item in split_items(value[1:-1])]
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return value


def parse_frontmatter(text):
    """Return (fields, body) for a Markdown file with `---` frontmatter.

    Supports `key: value` lines, quoted strings, true/false and `[a, b]`
    lists. Indented lines (nested YAML) and comments are skipped.
    """
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text.strip()
    fields = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#") or line[0] in " \t":
            continue
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = parse_scalar(value)
    return fields, "\n".join(lines[end + 1 :]).strip()


def tool_value(value):
    """Join a tools field into pi's comma-separated form; None when unset or empty."""
    if value is None or value is True or value is False:
        return None
    items = value if isinstance(value, list) else split_items(str(value))
    joined = ",".join(str(item).strip() for item in items if str(item).strip())
    return joined or None


def text_field(fields, key):
    value = fields.get(key)
    if value is None or isinstance(value, (bool, list)):
        return None
    value = str(value).strip()
    return value or None


def make_profile(name, fields, body, source, scope):
    return {
        "name": name,
        "description": text_field(fields, "description") or name,
        "model": text_field(fields, "model"),
        "thinking": text_field(fields, "thinking"),
        "tools": tool_value(fields.get("tools")),
        "disallowed_tools": tool_value(fields.get("disallowed_tools")),
        "prompt_mode": (text_field(fields, "prompt_mode") or "append").lower(),
        "enabled": fields.get("enabled") is not False,
        "body": body,
        "source": source,
        "scope": scope,
    }


def profile_dirs(cwd):
    return [
        ("project", os.path.join(cwd, ".pi", "agents")),
        ("user", os.path.join(pi_agent_dir(), "agents")),
        ("builtin", BUILTIN_PROFILES_DIR),
    ]


def load_profiles(cwd):
    """Map each lowercased profile name to the profile from the highest directory.

    A profile replaces lower ones of the same name as a whole; a disabled one
    hides them. Disabled entries stay in the map with enabled False.
    """
    found = {}
    for scope, directory in profile_dirs(cwd):
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(directory, entry)
            if not entry.endswith(".md") or not os.path.isfile(path):
                continue
            text = read_text(path)
            if text is None:
                continue
            fields, body = parse_frontmatter(text)
            name = text_field(fields, "name") or entry[: -len(".md")]
            found.setdefault(name.lower(), make_profile(name, fields, body, path, scope))
    return found


def find_profile(cwd, name):
    profiles = load_profiles(cwd)
    profile = profiles.get(name.lower())
    if profile is None or not profile["enabled"]:
        available = sorted(p["name"] for p in profiles.values() if p["enabled"])
        raise CommandError("unknown profile: %s (available: %s)" % (name, ", ".join(available) or "none"))
    if profile["prompt_mode"] not in ("append", "replace"):
        raise CommandError(
            "profile %s in %s: prompt_mode must be append or replace, got %s"
            % (profile["name"], profile["source"], profile["prompt_mode"])
        )
    return profile


def caller_model():
    model = os.environ.get("PI_MODEL") or ""
    provider = os.environ.get("PI_PROVIDER") or ""
    if model and provider:
        return provider + "/" + model
    return model or None


def resolve_model(option, profile):
    return option or profile["model"] or caller_model()


def resolve_thinking(option, profile):
    return option or profile["thinking"] or os.environ.get("PI_REASONING_LEVEL") or None


def profile_args(profile, model, thinking, prompt_file):
    """pi arguments for a profile. In replace mode, writes the profile's prompt to prompt_file.

    In append mode, the prompt reaches pi through append_prompt() instead.
    """
    args = []
    if model:
        args += ["--model", model]
    if thinking:
        args += ["--thinking", thinking]
    if profile["tools"]:
        args += ["--tools", profile["tools"]]
    if profile["disallowed_tools"]:
        args += ["-xt", profile["disallowed_tools"]]
    if profile["prompt_mode"] == "replace":
        write_atomic(prompt_file, profile["body"] + "\n")
        args += ["--system-prompt", prompt_file, "-nc"]
    return args


def append_prompt(profile, name):
    """Text passed with --append-system-prompt: the profile's prompt in append
    mode, then the multi-agent block."""
    parts = []
    if profile["prompt_mode"] == "append" and profile["body"]:
        parts.append(profile["body"])
    parts.append(MULTI_AGENT_BLOCK.format(name=name).strip())
    return "\n\n".join(parts) + "\n"


# --- job state ---------------------------------------------------------------


def validate_name(name):
    if not NAME_RE.match(name):
        raise usage_error(
            "invalid job name: %r (use letters, digits, '.', '_' and '-', start and end with a"
            " letter or digit, at most 64 characters)" % name
        )


def default_name(profile_name):
    base = re.sub(r"[^a-z0-9._-]+", "-", profile_name.lower()).strip("._-")[:59] or "agent"
    return "%s-%s" % (base, secrets.token_hex(2))


def job_path(job_dir, name):
    return os.path.join(job_dir, name)


def state_dir_of(job_dir):
    """The state root that holds job_dir (`<root>/<project>/jobs/<name>`)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(job_dir)))


def job_lock(job_dir, busy_message):
    """Take the job's lock exclusively and return its fd.

    The lock is an flock on the file `lock`. The runner holds it for its
    whole life, so the job is running exactly while the lock is held (see
    job_state). A starter takes it, then hands its fd to the runner it
    starts; `rm` takes it to delete the job. Readers hold it shared for a
    moment, so the lock is retried for LOCK_TIMEOUT_S before this fails
    with busy_message.
    """
    name = os.path.basename(job_dir)
    path = job_path(job_dir, "lock")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except FileNotFoundError:
        raise CommandError("job %s was removed" % name)
    try:
        end = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise CommandError(busy_message)
                time.sleep(0.05)
            except OSError as exc:
                raise CommandError(
                    "flock does not work on %s (%s); set XDG_STATE_HOME to a local file system"
                    % (state_dir_of(job_dir), exc.strerror or exc)
                )
        # `rm` may have removed the job while this command waited.
        try:
            if os.stat(path).st_ino != os.fstat(fd).st_ino:
                raise CommandError("job %s was removed" % name)
        except FileNotFoundError:
            raise CommandError("job %s was removed" % name)
    except BaseException:
        os.close(fd)
        raise
    return fd


def job_state(job_dir):
    """The job's status: `running` while its lock is held, else the end its runner recorded.

    The files are read under a shared lock, so no run can start while they
    are read, and the reading is a consistent snapshot. A run without an
    exit code ended without its runner recording a result: `died`.
    """
    try:
        fd = os.open(job_path(job_dir, "lock"), os.O_RDONLY)
    except OSError:
        fd = None
    try:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return "running"
            except OSError:
                # flock does not work here, so `run` never started a job here.
                pass
        if not os.path.exists(job_path(job_dir, "exit_code")):
            return "died"
        return (read_text(job_path(job_dir, "status"), "") or "").strip() or "unknown"
    finally:
        if fd is not None:
            os.close(fd)


def socket_path(job_dir):
    """The path of the job's control socket.

    It is computed from the job directory, so every command finds it without
    a file that maps names to sockets. The hash keeps its length fixed:
    the state root plus about 30 bytes.
    """
    digest = hashlib.sha256(os.fsencode(job_dir)).hexdigest()[:16]
    return os.path.join(state_dir_of(job_dir), "sockets", digest + ".sock")


def check_socket_path(job_dir):
    if len(os.fsencode(socket_path(job_dir))) >= SUN_PATH_MAX:
        raise CommandError("the socket path for this job is too long; set XDG_STATE_HOME to a shorter path")


def listen_control(job_dir):
    """Bind and listen on the job's control socket; called with the job's lock held.

    A leftover socket file belongs to a runner that is gone, since the lock
    is free, so it is replaced.
    """
    check_socket_path(job_dir)
    path = socket_path(job_dir)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    remove(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(path)
        server.listen(64)
    except OSError as exc:
        server.close()
        raise CommandError("could not create the control socket %s: %s" % (path, exc))
    return server


def find_job(name):
    """Look a job up in the current project, then in all projects."""
    validate_name(name)
    direct = os.path.join(jobs_dir(logical_cwd()), name)
    if os.path.isdir(direct):
        return direct
    matches = []
    try:
        projects = sorted(os.listdir(state_root()))
    except OSError:
        projects = []
    for project in projects:
        candidate = os.path.join(state_root(), project, "jobs", name)
        if os.path.isdir(candidate):
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise CommandError(
            "job %s exists in several projects; run the command from the project's directory:\n  %s"
            % (name, "\n  ".join(matches))
        )
    raise CommandError("unknown job: %s" % name)


def model_label(job_dir, job):
    used = (read_text(job_path(job_dir, "model"), "") or "").strip()
    return used or job.get("model") or "(pi default)"


def duration_s(job_dir):
    started = parse_utc(read_text(job_path(job_dir, "started_at"), "") or "")
    ended = parse_utc(read_text(job_path(job_dir, "ended_at"), "") or "")
    if started is None:
        return None
    return int((ended if ended is not None else time.time()) - started)


def format_result(job_dir, status):
    job = read_json(job_path(job_dir, "job.json"), {})
    output = (read_text(job_path(job_dir, "output.txt"), "") or "").rstrip("\n")
    duration = duration_s(job_dir)
    return "%s\n\n%s\nname: %s\nstatus: %s\nprofile: %s\nmodel: %s\nduration_s: %s\n" % (
        output if output.strip() else "(no output)",
        RESULT_DELIMITER,
        os.path.basename(job_dir),
        status,
        job.get("profile", "?"),
        model_label(job_dir, job),
        "?" if duration is None else duration,
    )


def print_result(job_dir, status):
    try:
        sys.stdout.write(format_result(job_dir, status))
        sys.stdout.flush()
    except OSError:
        # The caller's stdout may be gone when a signal stopped the run.
        pass
    return EXIT_OK if status == "done" else EXIT_FAIL


def append_progress(job_dir, message):
    line = "%s %s\n" % (time.strftime("%H:%M:%S"), message)
    try:
        with open(job_path(job_dir, "progress.log"), "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def record_end(job_dir, status, output=None, detail=None):
    """Record the end of the job's current run; output None keeps output.txt.

    exit_code is written last: once it exists, readers trust the status
    file and the rest of the run's state.
    """
    if output is not None:
        write_atomic(job_path(job_dir, "output.txt"), output + ("\n" if output else ""))
    write_atomic(job_path(job_dir, "ended_at"), utcnow() + "\n")
    write_atomic(job_path(job_dir, "status"), status + "\n")
    write_atomic(job_path(job_dir, "exit_code"), "%d\n" % (EXIT_OK if status == "done" else EXIT_FAIL))
    message = "run %s ended: %s" % (read_int(job_path(job_dir, "runs")), status)
    append_progress(job_dir, message + (" (%s)" % detail if detail else ""))


# --- runner ------------------------------------------------------------------


def child_env(job_dir, job):
    env = dict(os.environ)
    for key in REMOVED_ENV:
        env.pop(key, None)
    env["MULTI_AGENT_JOB"] = job_dir
    env["MULTI_AGENT_DEPTH"] = str(job["depth"])
    env["PI_MINIMAL_PERMISSION_SYSTEM_NONINTERACTIVE"] = "1"
    env["PWD"] = job["cwd"]
    return env


def pi_argv(job_dir, job):
    argv = [pi_bin(), "--mode", "rpc", "--session-dir", job["session_dir"]]
    if job.get("fork") and job.get("run_kind") == "new":
        argv += ["--fork", job["fork"]]
    argv += ["--session-id", job["name"]]
    argv += job["pi_args"]
    argv += ["--append-system-prompt", job_path(job_dir, "append-prompt.md")]
    return argv


def short_args(args):
    if isinstance(args, dict):
        for key in ("command", "path", "file_path", "pattern", "query", "url"):
            value = args.get(key)
            if isinstance(value, str) and value:
                return excerpt(value)
    return excerpt(json.dumps(args, ensure_ascii=False, separators=(",", ":")))


class Runner:
    """Owns one pi RPC process for one run of a job.

    lock_fd holds the job's lock, which the starting command handed over and
    the runner keeps until it exits. server is the job's listening control
    socket. caller_fd is the read end of a pipe whose only write end the
    foreground `run` process holds. End of file on it means that process has
    ended, for any reason, SIGKILL included, and the run stops.
    """

    def __init__(self, job_dir, lock_fd, server, caller_fd=None):
        self.job_dir = job_dir
        self.job = read_json(job_path(job_dir, "job.json"))
        self.prompt = read_text(job_path(job_dir, "prompt.txt"), "")
        self.lock_fd = lock_fd
        self.server = server
        # Accepted connections whose request line is not complete yet.
        self.requests = {}
        self.caller_fd = caller_fd
        self.caller_gone = False
        self.proc = None
        self.out_fd = None
        self.buffer = b""
        self.eof = False
        self.stdin_open = False
        self.selector = selectors.DefaultSelector()
        self.wake_r = None
        self.signum = None
        self.serial = 0
        self.prompt_response = None
        # RPC id of a forwarded steer -> the connection waiting for pi's answer.
        self.pending_acks = {}
        self.kill_requested = False
        self.settled = False
        self.last_stop = None
        self.last_error = None
        # Text of this run's latest assistant message. pi's
        # get_last_assistant_text would also return an earlier run's text,
        # or the caller's after --fork.
        self.last_text = None
        self.model_used = None
        self.usage = read_json(job_path(job_dir, "usage.json"), None) or {}
        self.abort_sent = False
        self.abort_requested = False
        self.timing_out = False
        self.stop_deadline = None
        self.finishing = False

    # -- process and pipes --

    def install_signals(self):
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_r, False)
        os.set_blocking(wake_w, False)
        self.wake_r = wake_r
        signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)
        self.selector.register(wake_r, selectors.EVENT_READ, "wake")

        def on_signal(signum, _frame):
            if self.signum is None:
                self.signum = signum

        for signum in STOP_SIGNALS:
            # An ignored signal stays ignored, so `nohup` keeps working.
            if signal.getsignal(signum) is not signal.SIG_IGN:
                signal.signal(signum, on_signal)

    def watch_caller(self):
        if self.caller_fd is None:
            return
        os.set_inheritable(self.caller_fd, False)
        os.set_blocking(self.caller_fd, False)
        self.selector.register(self.caller_fd, selectors.EVENT_READ, "caller")

    def listen(self):
        # Neither fd may reach pi: a pi that closed the lock fd would make
        # the job look gone while it runs.
        os.set_inheritable(self.lock_fd, False)
        self.server.set_inheritable(False)
        self.server.setblocking(False)
        self.selector.register(self.server, selectors.EVENT_READ, "server")

    def read_caller(self):
        try:
            data = os.read(self.caller_fd, 512)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self.caller_gone = True
            self.selector.unregister(self.caller_fd)
            os.close(self.caller_fd)
            self.caller_fd = None

    def start(self):
        stderr_fh = open(job_path(self.job_dir, "stderr.txt"), "wb")
        try:
            self.proc = subprocess.Popen(
                pi_argv(self.job_dir, self.job),
                cwd=self.job["cwd"],
                env=child_env(self.job_dir, self.job),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_fh,
                # pi gets its own session, so signals for the runner's process
                # group never reach it; the runner stops it through its stdin.
                start_new_session=True,
            )
        finally:
            stderr_fh.close()
        self.stdin_open = True
        self.out_fd = self.proc.stdout.fileno()
        self.selector.register(self.out_fd, selectors.EVENT_READ, "pi")

    def send(self, obj):
        if not self.stdin_open:
            return False
        try:
            self.proc.stdin.write(json.dumps(obj).encode("utf-8") + b"\n")
            self.proc.stdin.flush()
            return True
        except OSError:
            self.stdin_open = False
            return False

    def close_stdin(self):
        if self.proc is not None and self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        self.stdin_open = False

    def new_id(self, kind):
        self.serial += 1
        return "%s-%d" % (kind, self.serial)

    def pump(self, timeout):
        """Wait up to timeout seconds (None: no limit) and handle what arrives.

        That is pi's records, control requests, signals and the end of the
        foreground command.
        """
        for key, _events in self.selector.select(None if timeout is None else max(0.0, timeout)):
            if key.data == "wake":
                try:
                    while os.read(self.wake_r, 512):
                        pass
                except OSError:
                    pass
            elif key.data == "caller":
                self.read_caller()
            elif key.data == "server":
                self.accept()
            elif key.data == "request":
                self.read_request(key.fileobj)
            else:
                self.read_pi()

    def read_pi(self):
        try:
            data = os.read(self.out_fd, 65536)
        except OSError:
            data = b""
        if not data:
            self.eof = True
            self.selector.unregister(self.out_fd)
            if self.buffer:
                self.handle_line(self.buffer)
                self.buffer = b""
            return
        self.buffer += data
        # Records end at LF only. U+2028 and U+2029 are valid inside JSON strings.
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self.handle_line(line)

    def handle_line(self, line):
        line = line.rstrip(b"\r")
        if not line.strip():
            return
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            return
        if isinstance(record, dict):
            self.handle(record)

    def handle(self, record):
        kind = record.get("type")
        if kind == "response":
            rid = record.get("id")
            if rid == "prompt":
                self.prompt_response = record
            elif rid in self.pending_acks:
                conn = self.pending_acks.pop(rid)
                if record.get("success"):
                    disposition = (record.get("data") or {}).get("disposition") or "queued"
                    self.reply(conn, {"ok": True, "disposition": disposition})
                else:
                    self.reply(conn, {"ok": False, "error": record.get("error") or "steer failed"})
        elif kind == "tool_execution_start":
            self.progress("%s %s" % (record.get("toolName", "?"), short_args(record.get("args"))))
        elif kind == "message_end":
            message = record.get("message") or {}
            if message.get("role") == "assistant":
                self.on_assistant_message(message)
        elif kind == "auto_retry_start":
            self.progress(
                "retry %s/%s: %s"
                % (record.get("attempt"), record.get("maxAttempts"), excerpt(record.get("errorMessage") or ""))
            )
        elif kind == "compaction_start":
            self.progress("compaction (%s)" % record.get("reason", "?"))
        elif kind == "extension_ui_request" and record.get("method") in DIALOG_METHODS:
            # No person can answer. Without a reply the extension would wait, often forever.
            self.send({"type": "extension_ui_response", "id": record.get("id"), "cancelled": True})
            self.progress("denied: %s" % excerpt(record.get("title") or record.get("method")))
        elif kind == "agent_settled":
            self.settled = True

    def on_assistant_message(self, message):
        self.last_stop = message.get("stopReason")
        self.last_error = message.get("errorMessage")
        content = message.get("content") or []
        # As in pi's get_last_assistant_text, an aborted message without
        # content keeps the text before it.
        if content or self.last_stop != "aborted":
            parts = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
            text = "".join(p.get("text") or "" for p in parts if isinstance(p, dict) and p.get("type") == "text")
            self.last_text = text.strip() or None
        if message.get("provider") and message.get("model"):
            self.model_used = "%s/%s" % (message["provider"], message["model"])
        elif message.get("model"):
            self.model_used = message["model"]
        usage = message.get("usage") or {}
        for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
            if isinstance(usage.get(key), (int, float)):
                self.usage[key] = self.usage.get(key, 0) + usage[key]
        cost = (usage.get("cost") or {}).get("total")
        if isinstance(cost, (int, float)):
            self.usage["cost"] = round(self.usage.get("cost", 0) + cost, 6)

    def progress(self, message):
        append_progress(self.job_dir, message)

    # -- control socket --

    def accept(self):
        while True:
            try:
                conn, _addr = self.server.accept()
            except OSError:
                # BlockingIOError: no more connections are waiting.
                return
            conn.setblocking(False)
            self.requests[conn] = b""
            self.selector.register(conn, selectors.EVENT_READ, "request")

    def read_request(self, conn):
        """Read from a connection until its request line is complete, then handle it."""
        try:
            data = conn.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            self.requests[conn] += data
            if b"\n" not in self.requests[conn]:
                return
        self.selector.unregister(conn)
        line = self.requests.pop(conn).split(b"\n", 1)[0]
        if not data and not line:
            conn.close()
            return
        try:
            request = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            request = None
        self.handle_request(conn, request)

    def handle_request(self, conn, request):
        kind = request.get("type") if isinstance(request, dict) else None
        if self.finishing or self.settled:
            self.reply(conn, {"ok": False, "error": NOT_RUNNING})
        elif kind == "steer":
            message = str(request.get("message", ""))
            rid = self.new_id("steer")
            if self.send({"id": rid, "type": "steer", "message": message}):
                # Answered when pi answers the steer.
                self.pending_acks[rid] = conn
                self.progress("steer: %s" % excerpt(message))
            else:
                self.reply(conn, {"ok": False, "error": NOT_RUNNING})
        elif kind == "abort":
            self.abort_requested = True
            self.progress("abort requested")
            self.send_abort()
            # Answered at once: pi answers RPC abort only after the agent has
            # settled, when the run is already ending.
            self.reply(conn, {"ok": True, "disposition": "requested"})
        elif kind == "kill":
            # The run loop stops the run as it does for a signal.
            self.kill_requested = True
            self.reply(conn, {"ok": True, "disposition": "stopping"})
        else:
            self.reply(conn, {"ok": False, "error": "unknown request type: %s" % kind})

    def reply(self, conn, obj):
        try:
            conn.settimeout(1)
            conn.sendall(json.dumps(obj).encode("utf-8") + b"\n")
        except OSError:
            # The command gave up waiting.
            pass
        conn.close()

    def close_control(self):
        """Answer every request still open with NOT_RUNNING, then remove the socket.

        Called once the run's end is recorded. The runner still holds the
        job's lock, so no new run has bound the socket path, and removing it
        is safe.
        """
        for fileobj in [self.server] + list(self.requests):
            try:
                self.selector.unregister(fileobj)
            except (KeyError, ValueError):
                pass
        unread = list(self.requests.items())
        while True:
            try:
                conn, _addr = self.server.accept()
            except OSError:
                break
            unread.append((conn, b""))
        for conn, data in unread:
            # Read the request first: closing a socket with unread data makes
            # the command's read fail with ECONNRESET instead of the answer.
            try:
                conn.settimeout(0.5)
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                pass
        for conn in list(self.pending_acks.values()) + [conn for conn, _data in unread]:
            self.reply(conn, {"ok": False, "error": NOT_RUNNING})
        self.pending_acks = {}
        self.requests = {}
        remove(socket_path(self.job_dir))
        self.server.close()

    def send_abort(self):
        if self.abort_sent:
            return
        self.abort_sent = True
        # pi continues with queued messages after an abort unless the queue is cleared.
        self.send({"id": self.new_id("clear_queue"), "type": "clear_queue"})
        self.send({"id": self.new_id("abort"), "type": "abort"})
        self.stop_deadline = time.monotonic() + SETTLE_TIMEOUT_S

    # -- the run --

    def run(self):
        self.install_signals()
        self.watch_caller()
        self.listen()
        try:
            self.start()
        except OSError as exc:
            return self.conclude("failed", "could not start %s: %s" % (pi_bin(), exc))
        self.progress("run %s started (%s)" % (read_int(job_path(self.job_dir, "runs")), self.job["mode"]))
        self.send({"id": "prompt", "type": "prompt", "message": self.prompt})

        timeout = self.job.get("timeout")
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            if self.signum is not None:
                return self.finish_killed("stopped by %s" % signal.Signals(self.signum).name)
            if self.caller_gone:
                return self.finish_killed("stopped: the caller's command ended")
            if self.kill_requested:
                return self.finish_killed("stopped by kill")
            now = time.monotonic()
            if deadline is not None and now >= deadline and not self.timing_out:
                self.timing_out = True
                self.progress("timeout after %ss; aborting" % format_seconds(timeout))
                self.send_abort()
            if self.stop_deadline is not None and now >= self.stop_deadline:
                self.progress("agent did not settle after the abort")
                return self.finish("timeout" if self.timing_out else "aborted")
            if self.settled:
                return self.finish(self.settled_status())
            response = self.prompt_response
            if response is not None and not response.get("success"):
                return self.finish("failed", response.get("error") or "prompt rejected")
            if response is not None and (response.get("data") or {}).get("disposition") == "handled":
                return self.finish("done")
            if self.eof:
                return self.finish_eof()
            # Every event wakes the selector, so it waits for the next
            # deadline only. Once the timeout has fired, only the settle
            # deadline is ahead.
            wait = None
            for limit in (None if self.timing_out else deadline, self.stop_deadline):
                if limit is not None:
                    wait = limit - now if wait is None else min(wait, limit - now)
            self.pump(wait)

    def settled_status(self):
        if self.timing_out:
            return "timeout"
        if self.abort_requested or self.last_stop == "aborted":
            return "aborted"
        if self.last_stop == "error":
            return "failed"
        return "done"

    def finish(self, status, error=None):
        self.finishing = True
        self.stop_pi()
        output = self.last_text or ""
        if status == "failed":
            detail = error or self.last_error
            if detail:
                output = (output + "\n\n" if output else "") + "error: " + detail
        return self.conclude(status, output)

    def finish_eof(self):
        self.finishing = True
        self.stop_pi()
        stderr = read_text(job_path(self.job_dir, "stderr.txt"), "") or ""
        tail = "\n".join(stderr.rstrip("\n").splitlines()[-20:])
        output = "pi exited before the run finished (exit status %s)." % self.proc.returncode
        if tail:
            output += " Last lines of its stderr:\n\n" + tail
        return self.conclude("failed", output)

    def finish_killed(self, reason):
        self.finishing = True
        self.progress(reason)
        self.stop_pi()
        return self.conclude("killed", self.last_text or "")

    def stop_pi(self):
        """Close pi's stdin, which makes it shut down; escalate to SIGTERM and SIGKILL."""
        self.close_stdin()
        if self.proc is None:
            return
        for signum, grace in ((None, PI_EXIT_GRACE_S), (signal.SIGTERM, PI_TERM_GRACE_S), (signal.SIGKILL, 0)):
            if self.proc.poll() is not None:
                break
            if signum is not None:
                try:
                    self.proc.send_signal(signum)
                except OSError:
                    pass
            end = time.monotonic() + grace
            while self.proc.poll() is None and time.monotonic() < end:
                # Keep reading: pi stalls if its stdout pipe fills during
                # shutdown. Requests meanwhile get NOT_RUNNING.
                self.pump(0.1)
        self.proc.wait()

    def conclude(self, status, output):
        self.finishing = True
        write_atomic(job_path(self.job_dir, "usage.json"), json.dumps(self.usage, indent=2) + "\n")
        if self.model_used:
            write_atomic(job_path(self.job_dir, "model"), self.model_used + "\n")
        record_end(self.job_dir, status, output)
        return status

    def emergency_stop(self):
        """Best-effort cleanup after an unexpected error in the runner."""
        self.close_stdin()
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.wait(PI_EXIT_GRACE_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def format_seconds(value):
    return ("%g" % value) if value is not None else "none"


def run_runner(job_dir, lock_fd, server, caller_fd=None):
    """Run one job run to completion and return its final status.

    The job's lock is released when this process exits.
    """
    runner = Runner(job_dir, lock_fd, server, caller_fd)
    try:
        return runner.run()
    except Exception:
        with open(job_path(job_dir, "runner.log"), "a", encoding="utf-8") as fh:
            fh.write("%s\n%s\n" % (utcnow(), traceback.format_exc()))
        runner.emergency_stop()
        return runner.conclude("failed", "the runner failed; see %s" % job_path(job_dir, "runner.log"))
    finally:
        runner.close_control()


# --- starting runs -----------------------------------------------------------


def caller_depth():
    try:
        return int(os.environ.get("MULTI_AGENT_DEPTH") or 0)
    except ValueError:
        return 0


def check_depth():
    depth = caller_depth()
    try:
        limit = int(os.environ.get("MULTI_AGENT_MAX_DEPTH") or DEFAULT_MAX_DEPTH)
    except ValueError:
        limit = DEFAULT_MAX_DEPTH
    if depth >= limit:
        raise CommandError(
            "cannot start an agent: this caller is at depth %d and MULTI_AGENT_MAX_DEPTH is %d"
            % (depth, limit)
        )
    return depth + 1


PREVIOUS_RUN_FILES = ("exit_code", "ended_at", "output.txt", "model")


def start_run(job_dir, lock_fd, server, job, prompt, background, timeout, run_kind):
    """Record a new run of the job and start its runner.

    lock_fd holds the job's lock and server is its listening control socket.
    Both pass to the runner, and this process closes its copies, so the
    runner holds the lock from here on with no gap.

    The runner is always a detached process. In the foreground this process
    stays as the runner's caller: it waits for the runner, prints the result,
    and holds the write end of the pipe whose end of file stops the run.
    """
    try:
        proc, caller_w = start_runner(job_dir, lock_fd, server, job, prompt, background, timeout, run_kind)
    finally:
        server.close()
        os.close(lock_fd)
    if background:
        sys.stdout.write("job: %s\nstatus: running\ntimeout_s: %s\n" % (job["name"], format_seconds(timeout)))
        return EXIT_OK
    note("started job %s" % job["name"])
    return follow_runner(job_dir, proc, caller_w)


def start_runner(job_dir, lock_fd, server, job, prompt, background, timeout, run_kind):
    """Reset the job's state for a new run and start the runner; return it and the caller's pipe end.

    Called with the job's lock held. If the runner cannot start, the run is
    recorded as failed.
    """
    job["mode"] = "background" if background else "foreground"
    job["timeout"] = timeout
    job["run_kind"] = run_kind
    write_atomic(job_path(job_dir, "job.json"), json.dumps(job, indent=2) + "\n")
    write_atomic(job_path(job_dir, "prompt.txt"), prompt)
    for name in PREVIOUS_RUN_FILES:
        remove(job_path(job_dir, name))
    runs = (read_int(job_path(job_dir, "runs")) or 0) + 1
    write_atomic(job_path(job_dir, "runs"), "%d\n" % runs)
    write_atomic(job_path(job_dir, "started_at"), utcnow() + "\n")
    write_atomic(job_path(job_dir, "status"), "running\n")

    # The job directory follows `__runner` and a space, so a person can find
    # a stuck runner with `pkill -f` (see kill_job).
    argv = [sys.executable, SCRIPT_PATH, "__runner", job_dir, str(lock_fd), str(server.fileno())]
    pass_fds = [lock_fd, server.fileno()]
    caller_r = caller_w = None
    if not background:
        # os.pipe() descriptors are not inherited, so only the runner gets
        # the read end and only this process holds the write end.
        caller_r, caller_w = os.pipe()
        argv.append(str(caller_r))
        pass_fds.append(caller_r)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=job["cwd"],
            # Its own session, so the caller's process group and terminal never
            # signal it. The caller's end is the pipe above.
            start_new_session=True,
            # No handle on the caller's stdout or stderr, so a command that runs
            # this script never waits for the runner.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=pass_fds,
        )
    except OSError as exc:
        for fd in (caller_r, caller_w):
            if fd is not None:
                os.close(fd)
        message = "could not start the runner: %s" % exc
        record_end(job_dir, "failed", "error: " + message, message)
        raise CommandError(message)
    if caller_r is not None:
        os.close(caller_r)
    return proc, caller_w


def follow_runner(job_dir, proc, caller_w):
    """Foreground: wait for the runner to end the run and print the result.

    SIGTERM, SIGHUP or SIGINT closes caller_w, so the runner stops the run and
    records it, and this process still prints the result. A second signal
    ends this process at once. Any other end of this process, SIGKILL
    included, also closes caller_w.
    """
    closed = []

    def on_signal(_signum, _frame):
        if not closed:
            closed.append(True)
            os.close(caller_w)
        for signum in STOP_SIGNALS:
            if signal.getsignal(signum) is not signal.SIG_IGN:
                signal.signal(signum, signal.SIG_DFL)

    for signum in STOP_SIGNALS:
        if signal.getsignal(signum) is not signal.SIG_IGN:
            signal.signal(signum, on_signal)
    proc.wait()
    return print_result(job_dir, job_state(job_dir))


def cmd_runner(argv):
    """Internal: the detached runner of a run.

    Its arguments are the job directory, the fd that holds the job's lock,
    the fd of the listening control socket, and, for a foreground run, the
    read end of the caller's pipe.
    """
    job_dir, lock_fd, server_fd = argv[0], int(argv[1]), int(argv[2])
    caller_fd = int(argv[3]) if len(argv) > 3 else None
    run_runner(job_dir, lock_fd, socket.socket(fileno=server_fd), caller_fd)
    return EXIT_OK


# --- argument parsing --------------------------------------------------------


class Args:
    """A small parser for `--opt value`, `--opt=value`, flags and positionals."""

    def __init__(self, argv, with_value, flags):
        self.values = {}
        self.flags = set()
        self.positional = []
        i = 0
        only_positional = False
        while i < len(argv):
            arg = argv[i]
            if only_positional or arg == "-" or not arg.startswith("-"):
                self.positional.append(arg)
            elif arg == "--":
                only_positional = True
            elif arg in ("-h", "--help"):
                self.flags.add("help")
            else:
                name, eq, value = arg.partition("=")
                if name in with_value:
                    if not eq:
                        if i + 1 >= len(argv):
                            raise usage_error("%s needs a value" % name)
                        i += 1
                        value = argv[i]
                    self.values[name] = value
                elif name in flags and not eq:
                    self.flags.add(name)
                else:
                    raise usage_error("unknown option: %s" % arg)
            i += 1


def parse_seconds(value, option):
    try:
        seconds = float(value)
    except ValueError:
        raise usage_error("%s needs a number of seconds, got: %s" % (option, value))
    if seconds < 0 or seconds != seconds:
        raise usage_error("%s needs a number of seconds, got: %s" % (option, value))
    return seconds


def run_timeout(args, background):
    if "--timeout" in args.values:
        seconds = parse_seconds(args.values["--timeout"], "--timeout")
        return seconds or None
    return BACKGROUND_TIMEOUT_S if background else None


def read_message(args, what):
    if len(args.positional) != 1:
        raise usage_error("expected one %s (a string, or - to read stdin)" % what)
    text = args.positional[0]
    if text == "-":
        text = sys.stdin.read().rstrip("\n")
    if not text.strip():
        raise usage_error("empty %s" % what)
    return text


def one_name(args, command):
    if len(args.positional) != 1:
        raise usage_error("%s needs one job name" % command)
    return args.positional[0]


# --- commands ----------------------------------------------------------------


def cmd_run(argv):
    args = Args(
        argv,
        ("--profile", "--name", "--timeout", "--model", "--thinking", "--cwd"),
        ("--background", "--fork"),
    )
    if "help" in args.flags:
        sys.stdout.write(USAGE + "\n")
        return EXIT_OK
    background = "--background" in args.flags
    timeout = run_timeout(args, background)
    project = logical_cwd()
    cwd = project
    if "--cwd" in args.values:
        cwd = os.path.normpath(os.path.join(project, os.path.expanduser(args.values["--cwd"])))
        if not os.path.isdir(cwd):
            raise usage_error("--cwd is not a directory: %s" % args.values["--cwd"])
    prompt = read_message(args, "prompt")
    fork = None
    if "--fork" in args.flags:
        fork = os.environ.get("PI_SESSION_FILE")
        if not fork:
            raise CommandError("--fork needs PI_SESSION_FILE, which is not set in this environment")
        if not os.path.isfile(fork):
            raise CommandError("--fork: PI_SESSION_FILE does not exist: %s" % fork)
    depth = check_depth()
    profile = find_profile(cwd, args.values.get("--profile") or DEFAULT_PROFILE)

    root = jobs_dir(project)
    # The socket path has the same length for every name in root.
    check_socket_path(os.path.join(root, "name"))
    os.makedirs(root, exist_ok=True)
    name = args.values.get("--name")
    if name is not None:
        validate_name(name)
        try:
            os.mkdir(os.path.join(root, name))
        except FileExistsError:
            raise CommandError(
                "job %s already exists in this project; use `resume %s` to continue it, pick"
                " another --name, or `rm %s` first" % (name, name, name)
            )
    else:
        for _attempt in range(100):
            name = default_name(profile["name"])
            try:
                os.mkdir(os.path.join(root, name))
                break
            except FileExistsError:
                continue
        else:
            raise CommandError("could not find a free job name; pass --name")
    job_dir = os.path.join(root, name)

    lock_fd = None
    try:
        # Taken at once, so the job directory never exists without a lock.
        lock_fd = job_lock(job_dir, "job %s is busy; try again" % name)
        model = resolve_model(args.values.get("--model"), profile)
        thinking = resolve_thinking(args.values.get("--thinking"), profile)
        pi_args = profile_args(profile, model, thinking, job_path(job_dir, "system-prompt.md"))
        write_atomic(job_path(job_dir, "append-prompt.md"), append_prompt(profile, name))
        session_dir = sessions_dir(project)
        os.makedirs(session_dir, exist_ok=True)
        job = {
            "name": name,
            "profile": profile["name"],
            "profile_source": profile["source"],
            "cwd": cwd,
            "project": project,
            "session_dir": session_dir,
            "parent": os.environ.get("MULTI_AGENT_JOB") or None,
            "depth": depth,
            "model": model,
            "thinking": thinking,
            "fork": fork,
            "pi_args": pi_args,
            "created_at": utcnow(),
        }
        server = listen_control(job_dir)
    except BaseException:
        shutil.rmtree(job_dir, ignore_errors=True)
        if lock_fd is not None:
            os.close(lock_fd)
        raise
    return start_run(job_dir, lock_fd, server, job, prompt, background, timeout, "new")


def cmd_resume(argv):
    args = Args(argv, ("--timeout",), ("--background",))
    if len(args.positional) < 1:
        raise usage_error("resume needs a job name and a prompt")
    name = args.positional.pop(0)
    background = "--background" in args.flags
    timeout = run_timeout(args, background)
    prompt = read_message(args, "prompt")
    job_dir = find_job(name)
    check_depth()
    # A runner holds the lock while the job runs, so taking it proves that
    # nothing runs, and keeps it so until the new runner holds it.
    lock_fd = job_lock(job_dir, "job %s is running; use steer, or wait for it to finish" % name)
    try:
        job = read_json(job_path(job_dir, "job.json"))
        if not job:
            raise CommandError("job %s has no readable job.json" % name)
        if not os.path.isdir(job["cwd"]):
            raise CommandError("the job's working directory no longer exists: %s" % job["cwd"])
        server = listen_control(job_dir)
    except BaseException:
        os.close(lock_fd)
        raise
    if not find_session_file(job["session_dir"], name):
        note("no session file found for %s; the agent starts a new conversation" % name)
    return start_run(job_dir, lock_fd, server, job, prompt, background, timeout, "resume")


def cmd_wait(argv):
    args = Args(argv, ("--timeout",), ())
    job_dir = find_job(one_name(args, "wait"))
    timeout = parse_seconds(args.values["--timeout"], "--timeout") if "--timeout" in args.values else None
    end = None if timeout is None else time.monotonic() + timeout
    while True:
        status = job_state(job_dir)
        if status != "running":
            return print_result(job_dir, status)
        if end is not None and time.monotonic() >= end:
            lines = (read_text(job_path(job_dir, "progress.log"), "") or "").splitlines()
            sys.stdout.write(
                "job: %s\nstatus: running\nwaited_s: %s\n" % (os.path.basename(job_dir), format_seconds(timeout))
            )
            if lines:
                sys.stdout.write("last_progress: %s\n" % lines[-1])
            return EXIT_STILL_RUNNING
        time.sleep(0.5 if end is None else max(0.0, min(0.5, end - time.monotonic())))


def cmd_result(argv):
    job_dir = find_job(one_name(Args(argv, (), ()), "result"))
    status = job_state(job_dir)
    if status == "running":
        raise CommandError("job %s is still running; use wait" % os.path.basename(job_dir))
    return print_result(job_dir, status)


def connect_runner(job_dir):
    """Connect to the job's runner; NotRunning when no runner listens.

    A refused or missing socket means no runner listens. While the lock is
    held anyway, a starter is between taking the lock and binding the
    socket, or the runner is exiting, so the connection is retried for a
    moment.
    """
    path = socket_path(job_dir)
    end = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            return sock
        except OSError:
            sock.close()
        if job_state(job_dir) != "running":
            raise NotRunning()
        if time.monotonic() >= end:
            raise CommandError("the runner of job %s does not accept requests" % os.path.basename(job_dir))
        time.sleep(0.05)


def send_request(job_dir, request, timeout_message):
    """Send one request to the job's runner and return its reply.

    Returns None when the runner closed the connection without a reply,
    which happens only when it exits. Fails with timeout_message when no
    reply arrives within ACK_TIMEOUT_S.
    """
    sock = connect_runner(job_dir)
    try:
        sock.settimeout(ACK_TIMEOUT_S)
        sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
        line = sock.makefile("rb").readline()
    except socket.timeout:
        raise CommandError(timeout_message)
    except OSError:
        line = b""
    finally:
        sock.close()
    try:
        reply = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    return reply if isinstance(reply, dict) else None


def send_control(job_dir, request):
    name = os.path.basename(job_dir)
    reply = send_request(
        job_dir, request, "the runner of job %s did not reply within %d seconds" % (name, ACK_TIMEOUT_S)
    )
    if reply is None:
        if wait_while(lambda: job_state(job_dir) == "running", 2):
            raise NotRunning()
        raise CommandError("the runner of job %s closed the connection without a reply" % name)
    if not reply.get("ok"):
        raise CommandError(reply.get("error") or "request failed")
    return reply


def cmd_steer(argv):
    args = Args(argv, (), ())
    if len(args.positional) < 1:
        raise usage_error("steer needs a job name and a message")
    name = args.positional.pop(0)
    message = read_message(args, "message")
    job_dir = find_job(name)
    reply = send_control(job_dir, {"type": "steer", "message": message})
    sys.stdout.write("job: %s\nsteer: %s\n" % (os.path.basename(job_dir), reply.get("disposition", "queued")))
    return EXIT_OK


def cmd_abort(argv):
    job_dir = find_job(one_name(Args(argv, (), ()), "abort"))
    reply = send_control(job_dir, {"type": "abort"})
    sys.stdout.write("job: %s\nabort: %s\n" % (os.path.basename(job_dir), reply.get("disposition", "requested")))
    return EXIT_OK


def kill_job(job_dir):
    """Ask the job's runner to stop the run, and wait until it has exited.

    The runner stops pi and records `killed` itself; this writes nothing.
    Whichever run is listening when the request arrives is the run that
    stops, so a run started after this returns is left alone.
    """
    name = os.path.basename(job_dir)
    # `[_]` keeps the pattern from matching the command line of a shell that
    # runs this pkill, as `sh -c` and a bash tool do.
    pattern = "[_]_runner %s " % re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", job_dir)
    stuck = "the runner of job %s is not responding; to stop it, run: pkill -KILL -f %s" % (
        name,
        shlex.quote(pattern),
    )
    try:
        reply = send_request(job_dir, {"type": "kill"}, stuck)
    except NotRunning:
        reply = {"ok": False}
    def running():
        return job_state(job_dir) == "running"

    # A runner that is finishing answers NOT_RUNNING and records its own
    # status; one that exits before it answers leaves no reply.
    if not wait_while(running, RUNNER_STOP_GRACE_S):
        raise CommandError(stuck)
    if reply is not None and not reply.get("ok"):
        return "job: %s\nstatus: %s (not running)\n" % (name, job_state(job_dir))
    return "job: %s\nstatus: %s\n" % (name, job_state(job_dir))


def project_jobs(project):
    root = jobs_dir(project)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [os.path.join(root, n) for n in names if os.path.isdir(os.path.join(root, n))]


def cmd_kill(argv):
    args = Args(argv, (), ("--all",))
    if "--all" in args.flags:
        if args.positional:
            raise usage_error("kill takes a job name or --all, not both")
        running = [d for d in project_jobs(logical_cwd()) if job_state(d) == "running"]
        if not running:
            sys.stdout.write("no running jobs in this project\n")
            return EXIT_OK
        results = {}

        def kill_one(job_dir):
            try:
                results[job_dir] = kill_job(job_dir)
            except CommandError as exc:
                results[job_dir] = "job: %s\nerror: %s\n" % (os.path.basename(job_dir), exc)

        threads = [threading.Thread(target=kill_one, args=(d,)) for d in running]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        sys.stdout.write("".join(results[d] for d in running))
        return EXIT_FAIL if any("\nerror: " in out for out in results.values()) else EXIT_OK
    job_dir = find_job(one_name(args, "kill"))
    sys.stdout.write(kill_job(job_dir))
    return EXIT_OK


def cmd_status(argv):
    job_dir = find_job(one_name(Args(argv, (), ()), "status"))
    job = read_json(job_path(job_dir, "job.json"), {})
    status = job_state(job_dir)
    name = os.path.basename(job_dir)
    session_dir = job.get("session_dir", "")
    sessions = find_session_file(session_dir, name)

    def value(file_name):
        return (read_text(job_path(job_dir, file_name), "") or "").strip() or "-"

    lines = [
        ("name", name),
        ("status", status),
        ("mode", job.get("mode", "-")),
        ("profile", job.get("profile", "-")),
        ("model", model_label(job_dir, job)),
        ("parent", os.path.basename(job["parent"]) if job.get("parent") else "-"),
        ("cwd", job.get("cwd", "-")),
        ("started_at", value("started_at")),
        ("ended_at", value("ended_at")),
        ("runs", value("runs")),
        ("exit_code", value("exit_code")),
        ("output_file", job_path(job_dir, "output.txt")),
        ("session_file", sessions[-1] if sessions else "-"),
        (
            "session_cmd",
            "cd %s && pi --session-dir %s --session-id %s"
            % (shlex.quote(job.get("cwd", ".")), shlex.quote(session_dir), shlex.quote(name)),
        ),
    ]
    if status == "running":
        lines.append(("note", "open the session in pi only after the job stops"))
    sys.stdout.write("".join("%s: %s\n" % pair for pair in lines))
    return EXIT_OK


def cmd_peek(argv):
    args = Args(argv, ("-n",), ())
    count = args.values.get("-n", "20")
    if not count.isdigit():
        raise usage_error("-n needs a whole number of lines, got: %s" % count)
    job_dir = find_job(one_name(args, "peek"))
    lines = (read_text(job_path(job_dir, "progress.log"), "") or "").splitlines()
    tail = lines[-int(count) :] if int(count) else []
    sys.stdout.write("".join(line + "\n" for line in tail))
    return EXIT_OK


def cmd_list(argv):
    args = Args(argv, (), ("--all",))
    if args.positional:
        raise usage_error("list takes no job name")
    rows = []
    now = time.time()
    for job_dir in project_jobs(logical_cwd()):
        job = read_json(job_path(job_dir, "job.json"), {})
        status = job_state(job_dir)
        started = parse_utc(read_text(job_path(job_dir, "started_at"), "") or "")
        ended = parse_utc(read_text(job_path(job_dir, "ended_at"), "") or "")
        since = started if status == "running" else (ended or started)
        if "--all" not in args.flags and status != "running" and (since is None or now - since > RECENT_S):
            continue
        rows.append(
            (
                started or 0,
                os.path.basename(job_dir),
                status,
                job.get("mode", "-"),
                os.path.basename(job["parent"]) if job.get("parent") else "-",
                format_age(now - since) if since is not None else "-",
            )
        )
    if not rows:
        sys.stdout.write("no jobs in this project\n")
        return EXIT_OK
    rows.sort()
    table = [("NAME", "STATUS", "MODE", "PARENT", "AGE")] + [row[1:] for row in rows]
    widths = [max(len(row[i]) for row in table) for i in range(4)]
    for row in table:
        cells = [row[i].ljust(widths[i]) for i in range(4)] + [row[4]]
        sys.stdout.write("  ".join(cells) + "\n")
    return EXIT_OK


def cmd_profiles(argv):
    args = Args(argv, (), ())
    if args.positional:
        raise usage_error("profiles takes no arguments")
    profiles = sorted(
        (p for p in load_profiles(logical_cwd()).values() if p["enabled"]), key=lambda p: p["name"].lower()
    )
    for profile in profiles:
        sys.stdout.write(
            "%s\n  description: %s\n  source: %s\n"
            % (profile["name"], " ".join(profile["description"].split()), profile["source"])
        )
    if not profiles:
        sys.stdout.write("no profiles found\n")
    return EXIT_OK


def cmd_rm(argv):
    job_dir = find_job(one_name(Args(argv, (), ()), "rm"))
    name = os.path.basename(job_dir)
    lock_fd = job_lock(job_dir, "job %s is running; kill it first" % name)
    try:
        job = read_json(job_path(job_dir, "job.json"), {})
        for path in find_session_file(job.get("session_dir", ""), name):
            remove(path)
        remove(socket_path(job_dir))
        shutil.rmtree(job_dir, ignore_errors=True)
    finally:
        os.close(lock_fd)
    sys.stdout.write("removed: %s\n" % name)
    return EXIT_OK


COMMANDS = {
    "run": cmd_run,
    "resume": cmd_resume,
    "wait": cmd_wait,
    "result": cmd_result,
    "steer": cmd_steer,
    "abort": cmd_abort,
    "kill": cmd_kill,
    "status": cmd_status,
    "peek": cmd_peek,
    "list": cmd_list,
    "profiles": cmd_profiles,
    "rm": cmd_rm,
}


def main(argv):
    if sys.platform == "win32":
        note("Windows is not supported")
        return EXIT_FAIL
    if not argv:
        sys.stderr.write(USAGE + "\n")
        return EXIT_USAGE
    command, rest = argv[0], argv[1:]
    if command in ("-h", "--help", "help"):
        sys.stdout.write(USAGE + "\n")
        return EXIT_OK
    if command == "__runner":
        return cmd_runner(rest)
    handler = COMMANDS.get(command)
    if handler is None:
        note("unknown command: %s (try --help)" % command)
        return EXIT_USAGE
    try:
        return handler(rest)
    except CommandError as exc:
        note("error: %s" % exc)
        return exc.code


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        sys.exit(EXIT_FAIL)
    except KeyboardInterrupt:
        sys.exit(130)
