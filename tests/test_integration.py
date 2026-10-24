"""Real scripts and real tmux, exclusively against disposable test state."""

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The packaging check points these at the two Nix packages. The source, like a
# TPM clone, holds both halves, and its tmux half lists it in
# CLAUDE_CODE_PLUGIN_DIRS.
TMUX_PLUGIN = Path(os.environ.get("TEST_TMUX_PLUGIN", ROOT))
CLAUDE_PLUGIN = Path(os.environ.get("TEST_CLAUDE_PLUGIN", ROOT))
OPENCODE_PLUGIN = Path(os.environ.get("TEST_OPENCODE_PLUGIN", ROOT))
SEPARATE = TMUX_PLUGIN != CLAUDE_PLUGIN
PLUGIN_FILES = [
    "tmux-agent-resurrect.tmux",
    "bin",
    "lib",
    "hooks",
    "skills/check",
    ".claude-plugin",
    "opencode",
]
BIN = TMUX_PLUGIN / "bin"
ENTRY = TMUX_PLUGIN / "tmux-agent-resurrect.tmux"
BASH = shutil.which("bash")
TMUX = shutil.which("tmux")
PYTHON = shutil.which("python3")
GIT = shutil.which("git")
RESURRECT = os.environ.get("RESURRECT_SOURCE")
TPM = os.environ.get("TPM_SOURCE")
OH_MY_TMUX = os.environ.get("OH_MY_TMUX_SOURCE")
CLAUDE = os.environ.get("CLAUDE_BINARY")
OPENCODE_1 = os.environ.get("OPENCODE_1_BINARY")
OPENCODE_2 = os.environ.get("OPENCODE_2_BINARY")
VERSION = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"]
ID = "11111111-2222-3333-4444-555555555555"


def system_plugins():
    try:
        return "@plugin" in Path("/etc/tmux.conf").read_text()
    except OSError:
        return False


def wait_until(predicate, message, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError(message)


class Integration(unittest.TestCase):
    def setUp(self):
        # A short socket path also works within Darwin's sockaddr_un limit.
        self.tmp = tempfile.TemporaryDirectory(prefix="ctr-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.cwd = self.home / "project"
        self.cwd.mkdir()
        self.snapdir = self.home / "snapshots"
        self.snapdir.mkdir()
        self.snapshot = self.snapdir / "tmux_resurrect_test.txt"
        self.stubdir = self.base / "stub"
        self.stubdir.mkdir()
        self.log = self.base / "launches.jsonl"
        self.socket = self.base / "tmux.sock"
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.stubdir}:{BIN}:{os.environ['PATH']}",
            "TERM": "xterm-256color",
            "SHELL": BASH,
            "LC_ALL": "C",
            "TMPDIR": str(self.base),
            "CLAUDE_TEST_LOG": str(self.log),
            # Pane shells would otherwise write their history into HOME when
            # kill-server hangs up on them, which it does not wait for, while
            # cleanup removes the directory.
            "HISTFILE": "",
        }
        # The only executables named claude and opencode available before the
        # inherited PATH are these stubs. They log argv/cwd, with the program
        # for opencode and any CLAUDE_CONFIG_DIR, and hold the pane until
        # teardown.
        for program, extra in [("claude", ""), ("opencode", ", 'program': 'opencode'")]:
            stub = self.stubdir / program
            stub.write_text(
                f"#!{PYTHON}\n"
                "import json, os, sys, time\n"
                f"entry = {{'argv': sys.argv[1:], 'cwd': os.getcwd(){extra}}}\n"
                "if 'CLAUDE_CONFIG_DIR' in os.environ:\n"
                "    entry['config'] = os.environ['CLAUDE_CONFIG_DIR']\n"
                "with open(os.environ['CLAUDE_TEST_LOG'], 'a') as f:\n"
                "    f.write(json.dumps(entry) + '\\n')\n"
                "time.sleep(120)\n"
            )
            stub.chmod(0o755)
        config = self.base / "tmux.conf"
        config.write_text(
            f"set -g default-shell {shlex.quote(BASH)}\n"
            f"set -g default-command {shlex.quote(BASH + ' --noprofile --norc')}\n"
            # Snapshot comparisons need names independent of transient commands.
            "set -g automatic-rename off\n"
            "set -g status off\n"
            "set -g exit-empty off\n"
            "set -g remain-on-exit off\n"
            "set -g base-index 0\n"
            "set -g pane-base-index 0\n"
        )
        self.run_command(
            [
                TMUX,
                "-S",
                str(self.socket),
                "-f",
                str(config),
                "new-session",
                "-d",
                "-s",
                "work",
                "-c",
                str(self.cwd),
            ]
        )
        self.addCleanup(self.stop_server)
        # No script or upstream process ever receives the caller's TMUX value.
        self.env["TMUX"] = f"{self.socket},0,0"
        self.env["TMUX_PANE"] = "%0"
        self.tmux("set", "-g", "@resurrect-dir", str(self.snapdir))
        self.pane = self.tmux(
            "display", "-p", "-t", "work", "#{pane_id}"
        ).stdout.strip()
        self.session = self.tmux(
            "display", "-p", "-t", "work", "#{session_id}"
        ).stdout.strip()
        self.clients = []

    def run_command(self, args, *, data=None, env=None, check=True, timeout=15):
        result = subprocess.run(
            args,
            input=data,
            text=True,
            capture_output=True,
            env=self.env if env is None else env,
            cwd=ROOT,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode:
            self.fail(
                f"Command failed ({result.returncode}): {args!r}\n{result.stdout}\n{result.stderr}"
            )
        return result

    def tmux(self, *args, **kwargs):
        return self.run_command([TMUX, "-S", str(self.socket), *args], **kwargs)

    def script(self, name, *args, data=None, env=None, check=True, timeout=15):
        # Each agent runs its recorder from its own plugin; tmux runs the rest.
        bin = {
            "tmux-agent-resurrect-claude-hook": CLAUDE_PLUGIN / "bin",
            "tmux-agent-resurrect-record": OPENCODE_PLUGIN / "bin",
        }.get(name, BIN)
        return self.run_command(
            [str(bin / name), *map(str, args)],
            data=data,
            env=env,
            check=check,
            timeout=timeout,
        )

    def stop_server(self):
        # Never issue kill-server without the explicit test socket.
        assert self.socket.parent == self.base and self.base.name.startswith("ctr-")
        self.tmux("kill-server", check=False)
        for client in getattr(self, "clients", []):
            try:
                client.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                client.kill()
                client.communicate()

    def transcript(self, cwd=None, session_id=ID):
        path = (
            self.home
            / ".claude"
            / "projects"
            / "-synthetic-project"
            / f"{session_id}.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = (ROOT / "tests/fixtures/transcript.jsonl").read_text().splitlines()
        lines[2] = json.dumps({"type": "system", "cwd": str(cwd or self.cwd)})
        path.write_text("\n".join(lines) + "\n")
        return path

    def hook(self, event, session_id=ID, **kwargs):
        return self.script(
            "tmux-agent-resurrect-claude-hook",
            data=json.dumps({"hook_event_name": event, "session_id": session_id}),
            **kwargs,
        )

    def option(self, name, pane=None):
        return self.tmux("show", "-pqv", "-t", pane or self.pane, name).stdout.strip()

    def write_snapshot(self, command="claude", full="claude --resume", session="work"):
        # Includes an unrelated process and an empty title, exercising exact
        # tab positions rather than whitespace-collapsing shell reads.
        self.snapshot.write_text(
            "\t".join(
                [
                    "pane",
                    session,
                    "0",
                    "1",
                    ":*",
                    "0",
                    "",
                    ":" + str(self.cwd),
                    "1",
                    command,
                    ":" + full,
                ]
            )
            + "\n"
            + "pane\tother\t0\t1\t:*\t0\tvim\t:/synthetic\t1\tvim\t:vim notes\n"
        )
        return self.snapshot

    def records(self, snapshot=None):
        return [
            json.loads(line.split("\t", 1)[1])
            for line in (snapshot or self.snapshot).read_text().splitlines()
            if line.startswith("agent-resurrect\t")
        ]

    def save(self, cwd=None, session="work"):
        self.transcript(cwd)
        self.hook("SessionStart")
        self.write_snapshot(session=session)
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.hook("SessionEnd")

    def claude_process(self, *args):
        # A process with Claude's command line, which the hook finds through
        # CLAUDE_PID.
        fake = self.base / "fake claude/claude"
        fake.parent.mkdir(exist_ok=True)
        fake.write_text(f"#!{PYTHON}\nimport time\ntime.sleep(60)\n")
        fake.chmod(0o755)
        process = subprocess.Popen([str(fake), *args])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def saved_options(self, *entries, process=None):
        # The options save gives a session whose transcript ends with entries,
        # started as process if given.
        with self.transcript().open("a") as stream:
            for entry in entries:
                stream.write(json.dumps(entry) + "\n")
        env = (
            self.env if process is None else dict(self.env, CLAUDE_PID=str(process.pid))
        )
        self.hook("SessionStart", env=env)
        self.write_snapshot()
        self.script("tmux-agent-resurrect-save", self.snapshot)
        return self.records()[0].get("options", [])

    def attach(self, session="work"):
        env = {k: v for k, v in self.env.items() if k not in {"TMUX", "TMUX_PANE"}}
        # Control-mode output, including %message lines, is kept for messages().
        output = self.base / f"client-{len(self.clients)}.out"
        with output.open("w") as stream:
            client = subprocess.Popen(
                [TMUX, "-S", str(self.socket), "-C", "attach-session", "-t", session],
                stdin=subprocess.PIPE,
                stdout=stream,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        self.clients.append(client)
        wait_until(
            lambda: (
                self.tmux(
                    "display", "-p", "-t", "=" + session, "#{session_attached}"
                ).stdout.strip()
                != "0"
            ),
            "client did not attach",
        )
        return client

    def messages(self):
        # display-message reaches control-mode clients as %message lines.
        return [
            line.removeprefix("%message ")
            for path in sorted(self.base.glob("client-*.out"))
            for line in path.read_text().splitlines()
            if line.startswith("%message ")
        ]

    def launches(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def expect_launches(self, count):
        wait_until(lambda: len(self.launches()) >= count, f"expected {count} launches")
        self.assertEqual(len(self.launches()), count)
        return self.launches()

    def load_plugin(self, entry=ENTRY):
        # run-shell expands formats in its command before the shell runs it.
        self.tmux("run-shell", shlex.quote(str(entry)).replace("#", "##"))

    def global_option(self, name):
        return self.tmux("show", "-gqv", name).stdout.strip()

    def plugin_dirs(self):
        result = self.tmux(
            "show-environment", "-g", "CLAUDE_CODE_PLUGIN_DIRS", check=False
        )
        _, separator, value = result.stdout.rstrip("\n").partition("=")
        return value if result.returncode == 0 and separator else None

    def opencode_config(self):
        result = self.tmux(
            "show-environment", "-g", "OPENCODE_CONFIG_CONTENT", check=False
        )
        _, separator, value = result.stdout.rstrip("\n").partition("=")
        return json.loads(value) if result.returncode == 0 and separator else None

    def native_hooks(self, event):
        return self.tmux("show-hooks", "-g", event).stdout.splitlines()

    def execute_resurrect_hook(self, phase, *args, check=True):
        # Match upstream execute_hook: quote each argument, then evaluate the
        # configured command with those arguments appended.
        return self.run_command(
            [
                BASH,
                "-c",
                'hook=$1; shift; args=""; '
                'if [ "$#" -gt 0 ]; then printf -v args "%q " "$@"; fi; '
                'eval "$hook $args"',
                "resurrect-hook",
                self.global_option("@resurrect-hook-" + phase),
                *map(str, args),
            ],
            check=check,
        )

    def previous_hook(self, phase, *, status=0):
        callback = self.base / "previous hook.py"
        if not callback.exists():
            callback.write_text(
                "import json, os, subprocess, sys\n"
                "phase, output, status = sys.argv[1:4]\n"
                "gate = subprocess.check_output(\n"
                "    ['tmux', 'show', '-gqv', '@agent-resurrect-restoring'], text=True).strip()\n"
                "with open(output, 'a') as stream:\n"
                "    stream.write(json.dumps({'phase': phase, 'args': sys.argv[4:],\n"
                "                             'gate': gate}) + '\\n')\n"
                "sys.exit(int(status))\n"
            )
        return shlex.join(
            [
                PYTHON,
                str(callback),
                phase,
                str(self.base / "previous.jsonl"),
                str(status),
            ]
        )

    def previous_calls(self):
        return [
            json.loads(line)
            for line in (self.base / "previous.jsonl").read_text().splitlines()
        ]

    def run_with_git(self, *args):
        # Keep host Git configuration out of test repositories and clones.
        return self.run_command(
            [str(arg) for arg in args],
            env=dict(
                self.env,
                GIT_CONFIG_NOSYSTEM="1",
                GIT_AUTHOR_NAME="Test",
                GIT_AUTHOR_EMAIL="test@example.invalid",
                GIT_COMMITTER_NAME="Test",
                GIT_COMMITTER_EMAIL="test@example.invalid",
            ),
        )

    def git(self, directory, *args):
        return self.run_with_git(GIT, "-C", directory, *args)

    def copy_plugin(self, destination):
        # Copies both halves into one directory, as in a TPM clone. A Nix
        # wrapper runs its wrapped script from the store, so copy the wrapped
        # script itself; the copy then runs from where it is.
        for root in dict.fromkeys([TMUX_PLUGIN, CLAUDE_PLUGIN, OPENCODE_PLUGIN]):
            for name in PLUGIN_FILES:
                source = root / name
                if not source.exists():
                    continue
                for path in [source] if source.is_file() else sorted(source.iterdir()):
                    if path.name.startswith("."):
                        continue
                    wrapped = path.with_name(f".{path.name}-wrapped")
                    copied = destination / path.relative_to(root)
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(wrapped if wrapped.is_file() else path, copied)
                    copied.chmod(0o755 if os.access(path, os.X_OK) else 0o644)

    def copy_tree(self, source, destination):
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
        # Sources from the Nix store are read-only.
        for path in [destination, *destination.rglob("*")]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o200)
        return destination

    def test_setup_registers_guarded_commands_and_rejects_recapturing_them(self):
        self.load_plugin()
        for phase, script in [
            ("post-save-layout", "tmux-agent-resurrect-save"),
            ("pre-restore-all", "tmux-agent-resurrect-pre-restore"),
            ("post-restore-all", "tmux-agent-resurrect-post-restore"),
        ]:
            path = str(BIN / script)
            self.assertEqual(
                shlex.split(self.global_option("@resurrect-hook-" + phase)),
                ["[", "!", "-e", path, "]", "||", path],
            )
        option = "@resurrect-hook-post-save-layout"
        external = self.global_option(option) + "; : another-action"
        self.tmux("set", "-g", option, external)
        result = self.script("tmux-agent-resurrect-setup", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(option, result.stderr)
        self.assertIn("reset", result.stderr)
        self.assertEqual(self.global_option(option), external)

    def test_setup_preserves_hooks_and_reloads_without_duplicates(self):
        original = self.previous_hook("save")
        self.tmux("set", "-g", "@resurrect-hook-post-save-layout", original)
        foreign = {}
        for event in ["client-attached", "client-session-changed"]:
            self.tmux(
                "set-hook",
                "-g",
                event + "[731]",
                "set-option -g @foreign-handler yes",
            )
            foreign[event] = self.native_hooks(event)
        self.load_plugin()
        installed_hooks = {event: self.native_hooks(event) for event in foreign}
        installed_save = self.global_option("@resurrect-hook-post-save-layout")
        for event, handlers in installed_hooks.items():
            self.assertEqual(len(handlers), len(foreign[event]) + 1)
            self.assertTrue(set(foreign[event]).issubset(handlers))
        self.load_plugin()
        self.assertEqual(
            self.global_option("@resurrect-hook-post-save-layout"), installed_save
        )
        for event, handlers in installed_hooks.items():
            self.assertEqual(self.native_hooks(event), handlers)

        replacements = {}
        for event, handlers in installed_hooks.items():
            owned = set(handlers) - set(foreign[event])
            self.assertEqual(len(owned), 1)
            slot = owned.pop().split(" ", 1)[0]
            self.tmux(
                "set-hook",
                "-g",
                slot,
                "set-option -g @replacement-handler yes",
            )
            replacements[event] = self.native_hooks(event)
        self.load_plugin()
        recovered = {event: self.native_hooks(event) for event in foreign}
        for event, handlers in recovered.items():
            self.assertEqual(len(handlers), len(replacements[event]) + 1)
            self.assertTrue(set(replacements[event]).issubset(handlers))
        self.load_plugin()
        for event, handlers in recovered.items():
            self.assertEqual(self.native_hooks(event), handlers)

        self.transcript()
        self.hook("SessionStart")
        self.write_snapshot()
        self.execute_resurrect_hook("post-save-layout", self.snapshot)
        self.assertEqual(self.records()[0]["session_id"], ID)
        self.assertEqual(
            self.previous_calls(),
            [{"phase": "save", "args": [str(self.snapshot)], "gate": ""}],
        )
        self.hook("SessionEnd")
        self.execute_resurrect_hook("post-restore-all", self.snapshot)
        self.attach()
        self.assertEqual(self.expect_launches(1)[0]["argv"], ["--resume", ID])

    def test_setup_composes_arguments_and_finishes_restore_after_previous_failure(self):
        self.transcript()
        self.hook("SessionStart")
        self.write_snapshot()
        unusual = self.snapdir / "layout ' $(touch INJECTED) `false` $ #.txt"
        self.snapshot.rename(unusual)
        self.snapshot = unusual
        for phase, label, status in [
            ("post-save-layout", "save", 0),
            ("pre-restore-all", "pre", 0),
            ("post-restore-all", "post", 7),
        ]:
            self.tmux(
                "set",
                "-g",
                "@resurrect-hook-" + phase,
                self.previous_hook(label, status=status),
            )
        self.load_plugin()
        self.execute_resurrect_hook("post-save-layout", self.snapshot)
        self.assertEqual(self.records()[0]["session_id"], ID)
        self.hook("SessionEnd")
        self.execute_resurrect_hook("pre-restore-all")
        self.assertEqual(self.global_option("@agent-resurrect-restoring"), "1")
        result = self.execute_resurrect_hook(
            "post-restore-all", self.snapshot, check=False
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.global_option("@agent-resurrect-restoring"), "")
        self.assertIn(ID, self.option("@agent-resurrect-resume-cmd"))
        self.assertFalse((ROOT / "INJECTED").exists())
        self.assertEqual(
            self.previous_calls(),
            [
                {"phase": "save", "args": [str(self.snapshot)], "gate": ""},
                {"phase": "pre", "args": [], "gate": "1"},
                {"phase": "post", "args": [str(self.snapshot)], "gate": "1"},
            ],
        )

    def test_setup_remove_restores_previous_settings_and_preserves_foreign_hooks(self):
        previous = {}
        foreign = {}
        for phase in ["post-save-layout", "pre-restore-all", "post-restore-all"]:
            previous[phase] = self.previous_hook(phase)
            self.tmux("set", "-g", "@resurrect-hook-" + phase, previous[phase])
        for event in ["client-attached", "client-session-changed"]:
            self.tmux(
                "set-hook",
                "-g",
                event + "[731]",
                "set-option -g @foreign-handler yes",
            )
            foreign[event] = self.native_hooks(event)
        self.load_plugin()
        self.script("tmux-agent-resurrect-setup", "--remove")
        self.script("tmux-agent-resurrect-setup", "--remove")
        for phase, original in previous.items():
            self.assertEqual(self.global_option("@resurrect-hook-" + phase), original)
        for event, handlers in foreign.items():
            self.assertEqual(self.native_hooks(event), handlers)

        self.load_plugin()
        external = "set-option -g @external-handler yes"
        self.tmux("set", "-g", "@resurrect-hook-post-save-layout", external)
        for event in foreign:
            owned = set(self.native_hooks(event)) - set(foreign[event])
            self.assertEqual(len(owned), 1)
            slot = owned.pop().split(" ", 1)[0]
            self.tmux("set-hook", "-g", slot, external)
        changed = {event: self.native_hooks(event) for event in foreign}
        self.script("tmux-agent-resurrect-setup", "--remove")
        self.assertEqual(
            self.global_option("@resurrect-hook-post-save-layout"), external
        )
        for event, handlers in changed.items():
            self.assertEqual(self.native_hooks(event), handlers)

    def test_moving_the_plugin_replaces_its_registrations(self):
        self.tmux(
            "set", "-g", "@resurrect-hook-post-save-layout", self.previous_hook("save")
        )
        self.tmux("set-environment", "-g", "CLAUDE_CODE_PLUGIN_DIRS", "/other/plugins")
        first = self.base / "plugin one ' $ #"
        second = self.base / "plugin two ' $ #"
        installed_hooks = None
        for directory in [first, second]:
            self.copy_plugin(directory)
            self.load_plugin(directory / "tmux-agent-resurrect.tmux")
            hooks = {
                event: [
                    handler.split(" ", 1)[0] for handler in self.native_hooks(event)
                ]
                for event in ["client-attached", "client-session-changed"]
            }
            if installed_hooks is None:
                installed_hooks = hooks
            else:
                self.assertEqual(hooks, installed_hooks)
            self.assertEqual(self.plugin_dirs(), f"/other/plugins:{directory}")
        shutil.rmtree(first)
        self.transcript()
        self.hook("SessionStart")
        self.write_snapshot()
        self.execute_resurrect_hook("post-save-layout", self.snapshot)
        self.assertEqual(self.records()[0]["session_id"], ID)
        self.assertEqual(len(self.previous_calls()), 1)
        self.hook("SessionEnd")
        self.execute_resurrect_hook("post-restore-all", self.snapshot)
        self.attach()
        self.assertEqual(self.expect_launches(1)[0]["argv"], ["--resume", ID])

    def test_registrations_do_nothing_after_the_scripts_are_deleted(self):
        self.tmux(
            "set", "-g", "@resurrect-hook-post-save-layout", self.previous_hook("save")
        )
        copy = self.base / "deleted copy"
        self.copy_plugin(copy)
        self.load_plugin(copy / "tmux-agent-resurrect.tmux")
        shutil.rmtree(copy)
        self.write_snapshot()
        original = self.snapshot.read_bytes()
        self.execute_resurrect_hook("post-save-layout", self.snapshot)
        self.assertEqual(self.snapshot.read_bytes(), original)
        self.assertEqual(len(self.previous_calls()), 1)
        self.execute_resurrect_hook("pre-restore-all")
        self.execute_resurrect_hook("post-restore-all", self.snapshot)
        for event in ["client-attached", "client-session-changed"]:
            handler = shlex.split(
                self.global_option(f"@agent-resurrect-{event}-handler")
            )
            self.assertEqual(handler[:2], ["run-shell", "-b"])
            # Run the registered command synchronously to observe its status.
            self.tmux("run-shell", handler[2])
        # The first-load notice runs a second after loading, without its
        # script, and must not leave the pane in view mode, where resumes wait.
        time.sleep(1.5)
        self.assertEqual(
            self.tmux("display", "-p", "-t", self.pane, "#{pane_mode}").stdout.strip(),
            "",
        )

    @unittest.skipIf(SEPARATE, "the packaged tmux plugin does not hold the hooks")
    def test_new_panes_list_the_plugin_beside_other_plugin_directories(self):
        self.tmux("set-environment", "-g", "CLAUDE_CODE_PLUGIN_DIRS", "/other/plugins")
        self.load_plugin()
        self.load_plugin()
        listed = f"/other/plugins:{TMUX_PLUGIN}"
        self.assertEqual(self.plugin_dirs(), listed)
        self.assertEqual(self.global_option("@agent-resurrect-dir"), str(TMUX_PLUGIN))
        output = self.base / "pane environment"
        self.tmux(
            "new-window",
            "-d",
            f"printenv CLAUDE_CODE_PLUGIN_DIRS > {shlex.quote(str(output))}",
        )
        wait_until(
            lambda: output.is_file() and output.read_text().endswith("\n"),
            "new pane did not write its environment",
        )
        self.assertEqual(output.read_text(), listed + "\n")
        self.script("tmux-agent-resurrect-setup", "--remove")
        self.assertEqual(self.plugin_dirs(), "/other/plugins")
        self.assertEqual(self.global_option("@agent-resurrect-dir"), "")

    @unittest.skipIf(SEPARATE, "the packaged tmux plugin does not hold the hooks")
    def test_remove_unsets_a_plugin_list_with_only_this_plugin(self):
        self.load_plugin()
        self.assertEqual(self.plugin_dirs(), str(TMUX_PLUGIN))
        self.script("tmux-agent-resurrect-setup", "--remove")
        self.assertIsNone(self.plugin_dirs())

    def test_a_tmux_plugin_without_the_claude_hooks_is_not_listed(self):
        self.tmux("set-environment", "-g", "CLAUDE_CODE_PLUGIN_DIRS", "/other/plugins")
        with_hooks = self.base / "with hooks"
        self.copy_plugin(with_hooks)
        self.load_plugin(with_hooks / "tmux-agent-resurrect.tmux")
        self.assertEqual(self.plugin_dirs(), f"/other/plugins:{with_hooks}")
        # Moving to a copy without them, as when switching to the Nix
        # packages, removes the earlier entry.
        without = self.base / "without hooks"
        self.copy_plugin(without)
        shutil.rmtree(without / "hooks")
        self.load_plugin(without / "tmux-agent-resurrect.tmux")
        self.assertEqual(self.plugin_dirs(), "/other/plugins")
        self.assertEqual(self.global_option("@agent-resurrect-dir"), str(without))
        self.assertEqual(self.global_option("@agent-resurrect-version"), VERSION)

    def test_new_panes_load_the_opencode_plugin_beside_other_configuration(self):
        user = {"autoupdate": False, "plugin": ["/user/plugin.js"]}
        self.tmux("set-environment", "-g", "OPENCODE_CONFIG_CONTENT", json.dumps(user))
        first, second = self.base / "first copy", self.base / "second copy"
        for directory in [first, second]:
            self.copy_plugin(directory)
            entry = directory / "tmux-agent-resurrect.tmux"
            self.load_plugin(entry)
            self.load_plugin(entry)
            plugin = str(directory / "opencode")
            self.assertEqual(
                self.opencode_config(), dict(user, plugin=[*user["plugin"], plugin])
            )
        # A copy without the OpenCode plugin, as when switching to the Nix
        # packages, removes the earlier entry.
        shutil.rmtree(first / "opencode")
        self.load_plugin(first / "tmux-agent-resurrect.tmux")
        self.assertEqual(self.opencode_config(), user)
        self.load_plugin(second / "tmux-agent-resurrect.tmux")
        self.run_command([str(second / "bin/tmux-agent-resurrect-setup"), "--remove"])
        self.assertEqual(self.opencode_config(), user)

    def test_remove_unsets_an_opencode_configuration_with_only_this_plugin(self):
        copy = self.base / "plugin copy"
        self.copy_plugin(copy)
        self.load_plugin(copy / "tmux-agent-resurrect.tmux")
        plugin = str(copy / "opencode")
        self.assertEqual(self.opencode_config(), {"plugin": [plugin]})
        self.run_command([str(copy / "bin/tmux-agent-resurrect-setup"), "--remove"])
        self.assertIsNone(self.opencode_config())

    def test_opencode_configuration_that_is_not_a_plugin_object_is_left_alone(self):
        copy = self.base / "plugin copy"
        self.copy_plugin(copy)
        for content in ["not json", "[]", '{"plugin": "one.js"}']:
            with self.subTest(content=content):
                self.tmux("set-environment", "-g", "OPENCODE_CONFIG_CONTENT", content)
                result = self.run_command(
                    [str(copy / "bin/tmux-agent-resurrect-setup")]
                )
                self.assertIn("OPENCODE_CONFIG_CONTENT", result.stderr)
                value = self.tmux("show-environment", "-g", "OPENCODE_CONFIG_CONTENT")
                self.assertEqual(value.stdout, f"OPENCODE_CONFIG_CONTENT={content}\n")

    @unittest.skipUnless(SEPARATE, "checks the packaged tmux plugin")
    def test_packaged_tmux_plugin_leaves_the_plugin_list_alone(self):
        self.assertFalse((TMUX_PLUGIN / "hooks").exists())
        self.assertFalse((TMUX_PLUGIN / "opencode").exists())
        self.attach()
        self.load_plugin()
        self.assertIsNone(self.plugin_dirs())
        self.assertIsNone(self.opencode_config())
        self.assertEqual(self.global_option("@agent-resurrect-dir"), str(TMUX_PLUGIN))
        self.assertEqual(self.global_option("@agent-resurrect-version"), VERSION)
        self.expect_no_new_messages(0)

    def test_plugin_directory_with_a_colon_is_rejected(self):
        copy = self.base / "plugin:copy"
        self.copy_plugin(copy)
        result = self.run_command(
            [str(copy / "bin/tmux-agent-resurrect-setup")], check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contains ':'", result.stderr)
        self.assertIsNone(self.plugin_dirs())
        self.assertEqual(self.global_option("@resurrect-hook-post-save-layout"), "")

    def test_entry_shows_setup_failures_on_attached_clients(self):
        self.attach()
        option = "@resurrect-hook-post-save-layout"
        self.tmux("set", "-g", option, "__tmux_agent_resurrect_post_save_layout")
        result = self.tmux("run-shell", shlex.quote(str(ENTRY)), check=False)
        self.assertNotEqual(result.returncode, 0)
        message = wait_until(self.messages, "setup failure was not displayed")
        self.assertEqual(len(message), 1)
        self.assertIn(f"tmux-agent-resurrect: {option} wraps", message[0])

    def expect_message(self, text):
        return wait_until(
            lambda: [message for message in self.messages() if text in message],
            f"no message containing {text!r}",
        )

    def expect_no_new_messages(self, count):
        # Notices are shown a second after the event that raises them.
        time.sleep(1.5)
        self.assertEqual(len(self.messages()), count)

    def load_with_client(self):
        # Return the number of messages once the first-load notice, which only
        # a plugin that lists itself in CLAUDE_CODE_PLUGIN_DIRS shows, is in.
        self.load_plugin()
        self.attach()
        if not SEPARATE:
            self.expect_message("loaded")
        return len(self.messages())

    @unittest.skipIf(SEPARATE, "the packaged tmux plugin does not hold the hooks")
    def test_first_load_notice_is_kept_until_a_client_attaches(self):
        self.load_plugin()
        notices = "@agent-resurrect-notices"
        wait_until(lambda: self.global_option(notices), "notice was not kept")
        self.attach()
        [message] = self.expect_message("OpenCode in a new pane")
        self.assertTrue(message.startswith("tmux-agent-resurrect: loaded"))
        self.assertEqual(self.global_option(notices), "")
        self.load_plugin()
        self.expect_no_new_messages(1)

    def test_save_warns_once_about_claude_panes_without_a_session_id(self):
        shown = self.load_with_client()
        self.write_snapshot()
        self.script("tmux-agent-resurrect-save", self.snapshot)
        [message] = self.expect_message("no session recorded for Claude")
        self.assertIn("in work:0.0;", message)
        self.write_snapshot()
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.expect_no_new_messages(shown + 1)
        self.hook("SessionStart")
        self.write_snapshot()
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.assertEqual(self.global_option("@agent-resurrect-unrecorded"), "")
        self.expect_no_new_messages(shown + 1)

    def test_save_finds_claude_panes_by_their_command_line(self):
        # macOS names a process run through a symlink after the file it
        # resolves to, which for Claude's native installer is named for its
        # version. An npm installation runs as node.
        for command, full, found in [
            ("2.1.300", "claude --model haiku", True),
            ("node", "node /usr/local/bin/claude", True),
            ("node", "node build.js claude", False),
        ]:
            with self.subTest(command=command, full=full):
                self.tmux("set", "-gu", "@agent-resurrect-unrecorded")
                self.write_snapshot(command=command, full=full)
                self.script("tmux-agent-resurrect-save", self.snapshot)
                self.assertEqual(
                    self.global_option("@agent-resurrect-unrecorded"),
                    self.pane if found else "",
                )
                saved = self.snapshot.read_text().splitlines()[0].split("\t")[10]
                self.assertEqual(saved, ":" if found else ":" + full)

    def test_restore_reports_sessions_it_cannot_resume(self):
        self.save()
        self.transcript().unlink()
        self.attach()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        [message] = self.expect_message("couldn't resume")
        self.assertEqual(
            message,
            "tmux-agent-resurrect: couldn't resume a saved Claude session: "
            f"missing or ambiguous transcript for {ID}",
        )

    def test_session_start_reports_a_plugin_copy_with_another_version(self):
        shown = self.load_with_client()
        payload = json.dumps({"hook_event_name": "SessionStart", "session_id": ID})
        manifest = json.loads(
            (CLAUDE_PLUGIN / ".claude-plugin/plugin.json").read_text()
        )
        copies = {}
        for name, version in [
            ("same version", VERSION),
            ("listed", "0.0.1"),
            ("installed", "0.0.2"),
        ]:
            copies[name] = self.base / name
            self.copy_plugin(copies[name])
            (copies[name] / ".claude-plugin/plugin.json").write_text(
                json.dumps(dict(manifest, version=version))
            )

        def start(name, **env):
            self.run_command(
                [str(copies[name] / "bin/tmux-agent-resurrect-claude-hook")],
                data=payload,
                env=dict(self.env, **env),
            )

        start("same version")
        # An earlier copy that a pane still lists in CLAUDE_CODE_PLUGIN_DIRS.
        start("listed", CLAUDE_CODE_PLUGIN_DIRS=str(copies["listed"]))
        [message] = self.expect_message("loaded plugin version 0.0.1")
        self.assertIn(f"but tmux runs {VERSION}; start Claude in a new pane", message)
        start("listed", CLAUDE_CODE_PLUGIN_DIRS=str(copies["listed"]))
        self.expect_no_new_messages(shown + 1)
        # A Claude plugin installed separately, as with the Nix packages.
        start("installed")
        [message] = self.expect_message("loaded plugin version 0.0.2")
        self.assertIn("install the same version of both plugins", message)
        self.assertEqual(self.option("@agent-resurrect-session"), f"claude {ID}")

    def fake_claude(self, version="2.1.300", plugins=None, hang=None):
        # Answers the two commands the check runs, except HANG, which it never
        # answers. Without PLUGINS, it loads each directory in
        # CLAUDE_CODE_PLUGIN_DIRS as tmux-resurrect@inline.
        directory = self.base / "fake claude"
        directory.mkdir(exist_ok=True)
        script = directory / "claude"
        script.write_text(
            f"#!{PYTHON}\n"
            "import json, os, sys, time\n"
            f"plugins = {plugins!r}\n"
            f"if sys.argv[1:] == {hang!r}:\n"
            "    time.sleep(120)\n"
            "elif sys.argv[1:] == ['--version']:\n"
            f"    print({version!r} + ' (Claude Code)')\n"
            "elif sys.argv[1:] == ['plugin', 'list', '--json']:\n"
            "    if plugins is None:\n"
            "        dirs = os.environ.get('CLAUDE_CODE_PLUGIN_DIRS', '').split(':')\n"
            "        plugins = [{'id': 'tmux-resurrect@inline', 'enabled': True,\n"
            "                    'installPath': d} for d in dirs if d]\n"
            "    print(json.dumps(plugins))\n"
            "else:\n"
            "    sys.exit(2)\n"
        )
        script.chmod(0o755)
        return dict(
            self.env,
            PATH=f"{directory}:{BIN}:{os.environ['PATH']}",
            CLAUDE_CODE_PLUGIN_DIRS=str(TMUX_PLUGIN),
        )

    def installed(self, version=VERSION):
        # A separately installed Claude plugin, as Home Manager links it.
        return [
            {
                "id": "tmux-resurrect@skills-dir",
                "version": version,
                "enabled": True,
                "installPath": str(CLAUDE_PLUGIN),
            }
        ]

    def check(self, env, *args, timeout=15):
        return self.script(
            "tmux-agent-resurrect-check", *args, env=env, check=False, timeout=timeout
        )

    def test_check_reports_a_working_setup(self):
        self.tmux("set", "-g", "@resurrect-save-script-path", "/resurrect/save.sh")
        self.load_plugin()
        if SEPARATE:
            env, loaded = self.fake_claude(plugins=self.installed()), "skills-dir"
        else:
            env, loaded = self.fake_claude(), "inline"
        result = self.check(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("problem  ", result.stdout)
        self.assertIn(
            f"Claude Code loads the plugin as tmux-resurrect@{loaded}", result.stdout
        )
        self.assertTrue(result.stdout.endswith("No problems found.\n"))

    def test_check_reports_problems(self):
        result = self.check(self.fake_claude())
        self.assertEqual(result.returncode, 1)
        self.assertIn("problem  tmux-resurrect is not loaded", result.stdout)
        self.assertIn("problem  the plugin has not loaded", result.stdout)

        self.load_plugin()
        self.tmux("set", "-g", "@resurrect-hook-post-save-layout", "true")
        env = self.fake_claude(version="2.1.200", plugins=[])
        env.pop("CLAUDE_CODE_PLUGIN_DIRS")
        result = self.check(env)
        self.assertEqual(result.returncode, 1)
        expected = ["@resurrect-hook-post-save-layout was replaced"]
        if SEPARATE:
            expected.append("Claude Code doesn't load the tmux-resurrect plugin")
        else:
            expected += [
                "this shell's CLAUDE_CODE_PLUGIN_DIRS doesn't list the plugin",
                "Claude Code 2.1.200 is older than 2.1.280",
                "Claude Code doesn't load the plugin from",
            ]
        for text in expected:
            self.assertIn(text, result.stdout)

    def test_check_gives_up_on_a_claude_that_does_not_answer(self):
        self.tmux("set", "-g", "@resurrect-save-script-path", "/resurrect/save.sh")
        self.load_plugin()
        for hang in [["--version"], ["plugin", "list", "--json"]]:
            with self.subTest(hang=hang):
                # The check gives up on Claude after 10 seconds.
                result = self.check(self.fake_claude(hang=hang), timeout=40)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    f"problem  claude {' '.join(hang[:2])} didn't answer within 10 seconds",
                    result.stdout,
                )
                self.assertEqual(result.stdout.count("problem  "), 1, result.stdout)

    def test_check_finds_claude_panes_by_their_command_line(self):
        self.load_plugin()
        # Claude's native installer links claude to a file named for its
        # version, which macOS names the process after; Linux names it claude.
        version = self.base / "versions/2.1.300"
        version.parent.mkdir()
        shutil.copy(BASH, version)
        (self.stubdir / "claude").unlink()
        (self.stubdir / "claude").symlink_to(version)
        # The trailing command keeps Bash from replacing itself with sleep.
        self.tmux("send-keys", "-t", self.pane, "claude -c 'sleep 60; :'", "Enter")
        wait_until(
            lambda: (
                self.tmux(
                    "display", "-p", "-t", self.pane, "#{pane_current_command}"
                ).stdout.strip()
                in {"2.1.300", "claude"}
            ),
            "claude not running",
        )
        result = self.check(self.fake_claude())
        self.assertIn(
            "problem  no session recorded for Claude in work:0.0", result.stdout
        )

    def test_check_compares_a_separately_installed_claude_plugin(self):
        self.tmux("set", "-g", "@resurrect-save-script-path", "/resurrect/save.sh")
        copy = self.base / "tmux plugin"
        self.copy_plugin(copy)
        shutil.rmtree(copy / "hooks")
        self.load_plugin(copy / "tmux-agent-resurrect.tmux")
        result = self.check(self.fake_claude(plugins=self.installed()))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("CLAUDE_CODE_PLUGIN_DIRS", result.stdout)
        result = self.check(self.fake_claude(plugins=self.installed("0.0.1")))
        self.assertIn(
            f"loads tmux-resurrect@skills-dir 0.0.1, but tmux runs {VERSION}",
            result.stdout,
        )

    def test_check_compares_this_pane_with_a_claude_session(self):
        self.load_plugin()
        self.hook("SessionStart")
        env = self.fake_claude()
        result = self.check(env, "--session", ID)
        self.assertIn(f"ok       this pane records session {ID}", result.stdout)
        result = self.check(env, "--session", "other-session")
        self.assertIn(
            f"records Claude session {ID}, not this session (other-session)",
            result.stdout,
        )

    def test_hook_start_end_and_out_of_order_end(self):
        self.hook("SessionStart")
        self.assertEqual(self.option("@agent-resurrect-session"), f"claude {ID}")
        self.hook("SessionEnd", "newer-session")
        self.assertEqual(self.option("@agent-resurrect-session"), f"claude {ID}")
        self.hook("SessionEnd")
        self.assertEqual(self.option("@agent-resurrect-session"), "")

    def test_overlapping_session_end_does_not_clear_newer_start(self):
        self.hook("SessionStart")
        # Nix wrappers prepend tmux on PATH. Copy the scripts so both source
        # and package checks can interpose the proxy.
        copied = self.base / "hook-under-test"
        self.copy_plugin(copied)
        hook = copied / "bin/tmux-agent-resurrect-claude-hook"
        marker = self.base / "interleaved-start"
        proxy = self.stubdir / "tmux"
        # Start the new conversation after the old End's first tmux request
        # finishes, before returning its result to the old hook process.
        proxy.write_text(
            f"#!{PYTHON}\n"
            "import subprocess, sys\n"
            "from pathlib import Path\n"
            f"result = subprocess.run([{TMUX!r}, *sys.argv[1:]], capture_output=True)\n"
            f"marker = Path({str(marker)!r})\n"
            "if not marker.exists():\n"
            "    marker.touch()\n"
            f"    subprocess.run([{BASH!r}, {str(hook)!r}], check=True, text=True,\n"
            f"        input={json.dumps({'hook_event_name': 'SessionStart', 'session_id': 'newer-session'})!r})\n"
            "sys.stdout.buffer.write(result.stdout)\n"
            "sys.stderr.buffer.write(result.stderr)\n"
            "sys.exit(result.returncode)\n"
        )
        proxy.chmod(0o755)
        self.run_command(
            [BASH, str(hook)],
            data=json.dumps({"hook_event_name": "SessionEnd", "session_id": ID}),
        )
        self.assertTrue(marker.is_file(), "tmux proxy was not exercised")
        self.assertEqual(
            self.option("@agent-resurrect-session"), "claude newer-session"
        )

    def test_hook_malformed_payloads_do_not_mutate(self):
        self.hook("SessionStart")
        for payload in [
            "{",
            "null",
            "[]",
            "{}",
            "{} {}",
            '{"session_id":null}',
            '{"session_id":42}',
            json.dumps(
                {"hook_event_name": "SessionStart", "session_id": "$(touch bad)"}
            ),
            json.dumps({"hook_event_name": "Stop", "session_id": ID}),
            json.dumps({"hook_event_name": "SessionStart", "session_id": ID + "\n"}),
            json.dumps({"hook_event_name": "SessionStart", "session_id": "--resume"}),
        ]:
            with self.subTest(payload=payload):
                result = self.script(
                    "tmux-agent-resurrect-claude-hook", data=payload, check=False
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("tmux-agent-resurrect:", result.stderr)
                self.assertEqual(
                    self.option("@agent-resurrect-session"), f"claude {ID}"
                )

    def test_hook_outside_tmux_is_noop(self):
        env = {k: v for k, v in self.env.items() if k not in {"TMUX", "TMUX_PANE"}}
        self.script("tmux-agent-resurrect-claude-hook", data="not json", env=env)

    def record(self, *args, **kwargs):
        return self.script("tmux-agent-resurrect-record", *args, **kwargs)

    def test_recorder_records_and_ends_an_agent_session(self):
        self.record("start", "opencode", "ses_1", self.cwd)
        self.assertEqual(self.option("@agent-resurrect-session"), "opencode ses_1")
        self.assertEqual(self.option("@agent-resurrect-cwd"), str(self.cwd))
        self.record("end", "opencode", "ses_other")
        self.assertEqual(self.option("@agent-resurrect-session"), "opencode ses_1")
        self.record("end", "opencode", "ses_1")
        self.assertEqual(self.option("@agent-resurrect-session"), "")
        self.assertEqual(self.option("@agent-resurrect-cwd"), "")
        for args in [
            ["start", "unknown", "ses_1"],
            ["start", "opencode", "$(touch bad)"],
            ["start", "opencode", "ses_1", "relative"],
            ["stop", "opencode", "ses_1"],
        ]:
            with self.subTest(args=args):
                result = self.record(*args, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("tmux-agent-resurrect:", result.stderr)
                self.assertEqual(self.option("@agent-resurrect-session"), "")
        env = {k: v for k, v in self.env.items() if k not in {"TMUX", "TMUX_PANE"}}
        self.record("start", "opencode", "ses_1", env=env)

    def test_opencode_session_is_saved_and_resumed(self):
        directory = self.home / "opencode project"
        directory.mkdir()
        self.record("start", "opencode", "ses_1", directory)
        self.write_snapshot(command="opencode", full="opencode")
        self.script("tmux-agent-resurrect-save", self.snapshot)
        [record] = self.records()
        self.assertEqual(
            [record[key] for key in ["agent", "session_id", "cwd", "worktree"]],
            ["opencode", "ses_1", str(directory), ""],
        )
        self.assertEqual(self.snapshot.read_text().splitlines()[0].split("\t")[10], ":")
        self.record("end", "opencode", "ses_1")
        self.attach()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertEqual(
            self.expect_launches(1)[0],
            {
                "argv": ["-s", "ses_1"],
                "cwd": str(directory.resolve()),
                "program": "opencode",
            },
        )
        # OpenCode reports a reopened session only at its next prompt; until
        # then, saves keep the record that was dispatched.
        self.write_snapshot(command="opencode", full="opencode")
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.assertEqual([record["session_id"] for record in self.records()], ["ses_1"])
        self.record("start", "opencode", "ses_1", directory)
        self.assertEqual(self.option("@agent-resurrect-resume-data"), "")

    def test_recorded_opencode_session_is_saved_only_while_it_runs(self):
        self.record("start", "opencode", "ses_1", self.cwd)
        self.write_snapshot(command="bash", full="bash")
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.assertEqual(self.records(), [])
        # macOS reports at most 15 characters of a process name.
        for command in [
            "opencode",
            ".opencode-wrapp",
            ".opencode-wrapped",
            "opencode.exe",
        ]:
            with self.subTest(command=command):
                self.write_snapshot(command=command, full=command)
                self.script("tmux-agent-resurrect-save", self.snapshot)
                self.assertEqual(
                    [record["session_id"] for record in self.records()], ["ses_1"]
                )

    def test_save_does_not_warn_about_opencode_before_its_first_prompt(self):
        shown = self.load_with_client()
        self.write_snapshot(command="opencode", full="opencode")
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.assertEqual(self.records(), [])
        self.expect_no_new_messages(shown)

    def test_save_changed_cwd_suppresses_generic_restart(self):
        changed = self.home / "changed directory"
        changed.mkdir()
        self.save(changed)
        self.assertEqual(self.records()[0]["cwd"], str(changed))
        self.assertEqual(self.snapshot.read_text().splitlines()[0].split("\t")[10], ":")
        self.assertIn("\t:vim notes", self.snapshot.read_text())
        self.assertEqual(self.snapshot.stat().st_mode & 0o777, 0o600)

    def test_hook_id_detects_node_process(self):
        self.transcript()
        self.hook("SessionStart")
        self.write_snapshot(command="node", full="node cli.js")
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.assertEqual(self.snapshot.read_text().splitlines()[0].split("\t")[10], ":")

    def test_claude_without_metadata_is_suppressed(self):
        self.write_snapshot()
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.snapshot.read_text().splitlines()[0].split("\t")[10], ":")

    def test_save_missing_transcript_falls_back_to_pane_cwd(self):
        self.hook("SessionStart")
        self.write_snapshot()
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.assertEqual(self.records()[0]["cwd"], str(self.cwd.resolve()))

    def test_exact_resume_is_deferred_then_fires_once(self):
        self.save()
        self.load_plugin()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.script("tmux-agent-resurrect-arm", self.session)
        self.assertEqual(self.launches(), [])
        self.assertIn(ID, self.option("@agent-resurrect-resume-cmd"))
        self.attach()
        launch = self.expect_launches(1)[0]
        self.assertEqual(
            launch, {"argv": ["--resume", ID], "cwd": str(self.cwd.resolve())}
        )
        # The resumed session's hook replaces the dispatched record.
        self.assertNotEqual(self.option("@agent-resurrect-resume-cmd"), "")
        self.hook("SessionStart")
        self.assertEqual(self.option("@agent-resurrect-resume-cmd"), "")
        self.attach()
        self.script("tmux-agent-resurrect-arm", self.session)
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        time.sleep(0.15)
        self.assertEqual(len(self.launches()), 1)

    def test_attached_session_is_launched_after_restore(self):
        self.save()
        self.attach()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.expect_launches(1)

    def test_paths_and_session_names_with_spaces_and_shell_metacharacters(self):
        session = "team space ' $(false)"
        self.tmux("rename-session", "-t", self.session, session)
        changed = self.home / "quote' space $(touch INJECTED) `false` \\ path"
        changed.mkdir()
        self.save(changed, session=session)
        self.load_plugin()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.attach(session)
        self.assertEqual(self.expect_launches(1)[0]["cwd"], str(changed.resolve()))
        self.assertFalse((self.cwd / "INJECTED").exists())

    def test_named_worktree_resume_from_main_repository(self):
        worktree = self.cwd / ".claude/worktrees/feature space/subdir"
        worktree.mkdir(parents=True)
        self.save(worktree)
        self.attach()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertEqual(
            self.expect_launches(1)[0],
            {
                "argv": ["--resume", ID, "--worktree", "feature space"],
                "cwd": str(self.cwd.resolve()),
            },
        )

    def test_missing_transcript_skips_without_picker(self):
        self.save()
        self.transcript().unlink()
        result = self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertIn("missing or ambiguous transcript", result.stderr)
        self.assertEqual(self.option("@agent-resurrect-resume-cmd"), "")
        self.assertEqual(self.launches(), [])

    def test_ambiguous_transcript_is_not_chosen_arbitrarily(self):
        self.save()
        duplicate = self.home / ".claude/projects/duplicate" / f"{ID}.jsonl"
        duplicate.parent.mkdir()
        duplicate.write_text(self.transcript().read_text())
        result = self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertIn("ambiguous", result.stderr)
        self.assertEqual(self.option("@agent-resurrect-resume-cmd"), "")

    def test_session_waits_for_a_missing_directory(self):
        self.save()
        expected = self.records()
        # As when the directory is on a volume that is not mounted yet.
        unmounted = self.home / "unmounted"
        self.cwd.rename(unmounted)
        shown = self.load_with_client()
        result = self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertIn(f"saved directory does not exist: {self.cwd}", result.stderr)
        self.expect_message(
            f"Claude in work:0.0 resumes once {self.cwd} exists; close the pane"
        )
        self.assertNotEqual(self.option("@agent-resurrect-resume-data"), "")
        self.write_snapshot(command="bash", full="bash")
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.assertEqual(self.records(), expected)
        # An attach before it exists is quiet and launches nothing.
        self.script("tmux-agent-resurrect-arm", self.session)
        self.expect_no_new_messages(shown + 1)
        self.assertEqual(self.launches(), [])
        unmounted.rename(self.cwd)
        self.script("tmux-agent-resurrect-arm", self.session)
        self.assertEqual(self.expect_launches(1)[0]["cwd"], str(self.cwd.resolve()))

    def test_missing_worktree_and_empty_transcript_skip(self):
        worktree = self.cwd / ".claude/worktrees/feature"
        worktree.mkdir(parents=True)
        self.save(worktree)
        worktree.rmdir()
        self.assertIn(
            "worktree no longer exists",
            self.script("tmux-agent-resurrect-post-restore", self.snapshot).stderr,
        )
        self.assertEqual(self.option("@agent-resurrect-resume-data"), "")
        worktree.mkdir()
        self.transcript(worktree).write_text("")
        self.assertIn(
            "empty transcript",
            self.script("tmux-agent-resurrect-post-restore", self.snapshot).stderr,
        )
        self.assertEqual(self.option("@agent-resurrect-resume-data"), "")

    def test_invalid_snapshot_metadata_and_missing_panes(self):
        self.save()
        good = self.records()[0]
        bad = [
            None,
            {},
            dict(good, version=9),
            dict(good, session_id="../../bad"),
            dict(good, cwd="relative"),
            dict(good, cwd="/bad\nline"),
            dict(good, worktree="../bad"),
        ]
        self.snapshot.write_text(
            "agent-resurrect\t{broken\n"
            + "".join(
                "agent-resurrect\t" + json.dumps(record) + "\n"
                for record in bad + [dict(good, session="missing")]
            )
        )
        result = self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertIn("invalid session metadata", result.stderr)
        self.assertEqual(self.option("@agent-resurrect-resume-cmd"), "")

    def test_metadata_is_specific_to_snapshot(self):
        self.save()
        earlier = self.snapdir / "earlier.txt"
        shutil.copyfile(self.snapshot, earlier)
        self.transcript(session_id="second-session")
        self.hook("SessionStart", "second-session")
        self.write_snapshot()
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.hook("SessionEnd", "second-session")
        self.assertEqual(self.records()[0]["session_id"], "second-session")
        self.script("tmux-agent-resurrect-post-restore", earlier)
        self.assertIn(ID, self.option("@agent-resurrect-resume-cmd"))

    def test_save_before_attach_preserves_pending_mapping_and_is_idempotent(self):
        self.save()
        expected = self.records()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.write_snapshot(command="bash", full="bash")
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.assertEqual(self.records(), expected)
        original = self.snapshot.read_bytes()
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.assertEqual(self.snapshot.read_bytes(), original)

    def test_restore_gate_ignores_intermediate_session_switches(self):
        self.save()
        self.load_plugin()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.script("tmux-agent-resurrect-pre-restore")
        self.assertEqual(
            self.tmux("show", "-gqv", "@agent-resurrect-restoring").stdout.strip(), "1"
        )
        self.attach()
        self.script("tmux-agent-resurrect-arm", self.session)
        self.assertEqual(self.launches(), [])
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertEqual(
            self.tmux("show", "-gqv", "@agent-resurrect-restoring").stdout.strip(), ""
        )
        self.expect_launches(1)

    def test_switching_client_to_pending_session_launches(self):
        self.save()
        self.load_plugin()
        self.tmux("new-session", "-d", "-s", "other", "-c", str(self.cwd))
        self.attach("other")
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertEqual(self.launches(), [])
        self.tmux("switch-client", "-t", self.session)
        self.expect_launches(1)

    def test_two_simultaneous_arm_calls_launch_once(self):
        self.save()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.attach()
        args = [str(BIN / "tmux-agent-resurrect-arm"), self.session]
        processes = [
            subprocess.Popen(
                args, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            for _ in range(2)
        ]
        for process in processes:
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
        self.expect_launches(1)

    def test_stale_pending_transcript_rechecked_at_attach(self):
        self.save()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.transcript().unlink()
        self.attach()
        self.assertIn(
            "missing or ambiguous",
            self.script("tmux-agent-resurrect-arm", self.session).stderr,
        )
        self.assertEqual(self.option("@agent-resurrect-resume-cmd"), "")
        self.assertEqual(self.launches(), [])

    def test_busy_pane_remains_pending(self):
        self.save()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.tmux("send-keys", "-t", self.pane, "sleep 120", "Enter")
        wait_until(
            lambda: (
                self.tmux(
                    "display", "-p", "-t", self.pane, "#{pane_current_command}"
                ).stdout.strip()
                not in {"bash", "sh", "zsh"}
            ),
            "sleep not running",
        )
        self.attach()
        self.assertIn(
            "once its pane is at a shell prompt",
            self.script("tmux-agent-resurrect-arm", self.session).stderr,
        )
        self.assertNotEqual(self.option("@agent-resurrect-resume-cmd"), "")
        self.assertEqual(self.launches(), [])

    def test_missing_snapshot_and_no_tmux_have_useful_diagnostics(self):
        self.script("tmux-agent-resurrect-pre-restore")
        self.assertIn(
            "snapshot not found",
            self.script("tmux-agent-resurrect-post-restore", self.snapshot).stderr,
        )
        self.assertEqual(
            self.tmux("show", "-gqv", "@agent-resurrect-restoring").stdout.strip(), ""
        )
        env = dict(self.env, TMUX="")
        result = self.script(
            "tmux-agent-resurrect-save", self.snapshot, env=env, check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run inside tmux", result.stderr)

    def test_restore_replaces_stale_pending_set(self):
        self.save()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertNotEqual(self.option("@agent-resurrect-resume-data"), "")
        self.write_snapshot()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertEqual(self.option("@agent-resurrect-resume-data"), "")
        self.assertEqual(self.option("@agent-resurrect-resume-cmd"), "")
        self.attach()
        self.script("tmux-agent-resurrect-arm", self.session)
        self.assertEqual(self.launches(), [])

    def test_copy_mode_remains_pending(self):
        self.save()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.tmux("copy-mode", "-t", self.pane)
        self.attach()
        result = self.script("tmux-agent-resurrect-arm", self.session)
        self.assertIn("copy mode", result.stderr)
        self.assertNotEqual(self.option("@agent-resurrect-resume-data"), "")
        self.assertEqual(self.launches(), [])
        self.tmux("send-keys", "-X", "-t", self.pane, "cancel")
        self.script("tmux-agent-resurrect-arm", self.session)
        self.expect_launches(1)

    def test_each_session_resumes_with_its_claude_config_dir(self):
        custom = self.home / "custom config"
        transcript = custom / "projects/-synthetic-project" / f"{ID}.jsonl"
        transcript.parent.mkdir(parents=True)
        shutil.move(self.transcript(), transcript)
        # Only the Claude session runs with this directory, not the tmux server.
        self.hook("SessionStart", env=dict(self.env, CLAUDE_CONFIG_DIR=str(custom)))
        self.assertEqual(
            json.loads(self.option("@agent-resurrect-launch")),
            {"config_dir": str(custom)},
        )
        self.write_snapshot()
        self.script("tmux-agent-resurrect-save", self.snapshot)
        [record] = self.records()
        self.assertEqual(record["config_dir"], str(custom))
        self.hook("SessionEnd")
        self.assertEqual(self.option("@agent-resurrect-launch"), "")
        self.attach()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertEqual(
            self.expect_launches(1)[0],
            {
                "argv": ["--resume", ID],
                "cwd": str(self.cwd.resolve()),
                "config": str(custom),
            },
        )

    def test_resume_repeats_the_options_claude_was_started_with(self):
        extra = self.home / "extra dir"
        (self.cwd / "lib").mkdir()
        extra.mkdir()
        mcp = self.home / "mcp.json"
        mcp.write_text("{}")
        process = self.claude_process(
            "--model",
            "claude-haiku-5-5",
            "--add-dir",
            str(extra),
            "lib",
            "--effort=low",
            "--dangerously-skip-permissions",
            "-c",
            "--mcp-config",
            str(mcp),
            "--allowedTools",
            "Bash(git log:*)",
            "Edit",
            "--strict-mcp-config",
            "--append-system-prompt",
            "be terse",
            "--settings",
            '{"model": "opus"}',
            "explain --verbose",
        )
        expected = [
            "--add-dir",
            str(extra),
            f"{self.cwd}/lib",
            "--mcp-config",
            str(mcp),
            "--allowedTools",
            "Bash(git log:*)",
            "Edit",
            "--strict-mcp-config",
        ]
        # ps joins the arguments elsewhere, so the extent of text is unknown.
        if Path("/proc/self/cmdline").exists():
            expected += ["--append-system-prompt", "be terse"]
        transcript = self.transcript()
        self.script(
            "tmux-agent-resurrect-claude-hook",
            data=json.dumps(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": ID,
                    "cwd": str(self.cwd),
                }
            ),
            env=dict(self.env, CLAUDE_PID=str(process.pid)),
        )
        launch = json.loads(self.option("@agent-resurrect-launch"))
        self.assertEqual(launch["options"], expected)
        self.assertEqual(
            (launch["model"], launch["effort"]), ("claude-haiku-5-5", "low")
        )
        # Resuming restores the permission mode, except bypassing permissions.
        with transcript.open("a") as stream:
            for mode in ["plan", "bypassPermissions"]:
                stream.write(
                    json.dumps({"type": "permission-mode", "permissionMode": mode})
                    + "\n"
                )
        self.write_snapshot()
        self.script("tmux-agent-resurrect-save", self.snapshot)
        # No response in the transcript has a model or effort level.
        expected += ["--dangerously-skip-permissions", "--model", "claude-haiku-5-5"]
        expected += ["--effort", "low"]
        self.assertEqual(self.records()[0]["options"], expected)
        # A response's model and level replace the ones Claude was started with.
        with transcript.open("a") as stream:
            response = {"type": "assistant", "requestedModel": "claude-opus-5-5"}
            stream.write(json.dumps({**response, "effort": "max"}) + "\n")
        self.write_snapshot()
        self.script("tmux-agent-resurrect-save", self.snapshot)
        expected[-4:] = ["--model", "claude-opus-5-5", "--effort", "max"]
        self.assertEqual(self.records()[0]["options"], expected)
        self.hook("SessionEnd")
        self.attach()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertEqual(
            self.expect_launches(1)[0]["argv"], ["--resume", ID, *expected]
        )

    def test_save_takes_the_model_and_effort_level_the_session_last_used(self):
        def response(model, effort, skill=None):
            entry = {"type": "assistant", "requestedModel": model, "effort": effort}
            return {**entry, "attributionSkill": skill} if skill else entry

        opus, fable, haiku = "claude-opus-5-5", "claude-fable-5-1", "claude-haiku-5-5"
        error = {
            "type": "assistant",
            "isApiErrorMessage": True,
            "message": {"model": "<synthetic>"},
        }
        typed_effort = {
            "type": "user",
            "message": {"content": "<command-name>/effort</command-name>"},
        }
        typed_model = {
            "type": "system",
            "subtype": "local_command",
            "content": "<command-name>/model</command-name>",
        }
        fallback = {"type": "system", "subtype": "model_fallback"}
        turn_end = {"type": "system", "subtype": "turn_duration"}
        ultracode_on = {
            "type": "attachment",
            "attachment": {"type": "ultra_effort_enter"},
        }
        ultracode_off = {
            "type": "attachment",
            "attachment": {"type": "ultra_effort_exit"},
        }
        cases = {
            # Claude records an API error as a response of its own.
            "latest response": (
                [response(opus, "max"), response(fable, "xhigh"), error],
                fable,
                "xhigh",
            ),
            # A skill's own level holds for the rest of its turn.
            "skill's own level": (
                [
                    response(opus, "max"),
                    response(opus, "low", "s"),
                    response(opus, "low", "s"),
                ],
                opus,
                "max",
            ),
            "skill's own model": (
                [response(opus, "max"), response(haiku, "max", "s")],
                opus,
                "max",
            ),
            "changed during a skill": (
                [
                    response(opus, "high"),
                    response(opus, "high", "s"),
                    response(opus, "max", "s"),
                ],
                opus,
                "max",
            ),
            "typed /effort": (
                [response(opus, "high"), typed_effort, response(opus, "max", "loop")],
                opus,
                "max",
            ),
            "typed /model": (
                [response(opus, "high"), typed_model, response(fable, "max", "loop")],
                fable,
                "max",
            ),
            "fallback in the turn": (
                [response(opus, "max"), fallback, response(fable, "high")],
                opus,
                "max",
            ),
            "after the fallback": (
                [
                    response(opus, "max"),
                    fallback,
                    response(fable, "high"),
                    turn_end,
                    response(fable, "xhigh"),
                ],
                fable,
                "xhigh",
            ),
            "ultracode": ([ultracode_on, response(opus, "xhigh")], opus, "ultracode"),
            "ultracode turned off": (
                [
                    ultracode_on,
                    response(opus, "xhigh"),
                    ultracode_off,
                    response(opus, "xhigh"),
                ],
                opus,
                "xhigh",
            ),
        }
        for name, (entries, model, effort) in cases.items():
            with self.subTest(name):
                self.assertEqual(
                    self.saved_options(*entries), ["--model", model, "--effort", effort]
                )

    def test_harness_resume_leaves_the_settings_to_claude(self):
        self.tmux("set", "-g", "@agent-resurrect-harness-resume", "on")
        process = self.claude_process("--model", "claude-haiku-5-5", "--effort", "low")
        options = self.saved_options(
            {"type": "permission-mode", "permissionMode": "bypassPermissions"},
            {"type": "assistant", "requestedModel": "claude-opus-5-5", "effort": "max"},
            {"type": "attachment", "attachment": {"type": "ultra_effort_enter"}},
            process=process,
        )
        self.assertEqual(options, ["--model", "claude-haiku-5-5", "--effort", "low"])

    def test_records_without_a_config_dir_use_the_server_one(self):
        # A session whose CLAUDE_CONFIG_DIR was relative has none.
        self.save()
        record = self.records()[0]
        self.assertEqual(record.pop("config_dir"), "")
        self.snapshot.write_text("agent-resurrect\t" + json.dumps(record) + "\n")
        custom = self.home / "custom config"
        (self.home / ".claude").rename(custom)
        self.env["CLAUDE_CONFIG_DIR"] = str(custom)
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertIn(ID, self.option("@agent-resurrect-resume-cmd"))
        self.assertNotIn(
            "CLAUDE_CONFIG_DIR", self.option("@agent-resurrect-resume-cmd")
        )

    def test_explicit_snapshot_path_with_spaces_and_symlink_rejection(self):
        self.transcript()
        self.hook("SessionStart")
        self.write_snapshot()
        renamed = self.snapdir / "saved layout.txt"
        self.snapshot.rename(renamed)
        self.script("tmux-agent-resurrect-save", renamed)
        self.hook("SessionEnd")
        link = self.snapdir / "last"
        link.symlink_to(renamed.name)
        self.assertIn(
            "not a symlink",
            self.script("tmux-agent-resurrect-save", link, check=False).stderr,
        )
        self.script("tmux-agent-resurrect-post-restore")
        self.assertIn(ID, self.option("@agent-resurrect-resume-cmd"))

    def test_snapshot_directory_is_expanded_as_resurrect_does(self):
        hostname = self.stubdir / "hostname"
        hostname.write_text("#!/bin/sh\necho test-host\n")
        hostname.chmod(0o755)
        self.save()
        saved = self.home / "state/test-host/resurrect"
        saved.mkdir(parents=True)
        self.snapshot.rename(saved / self.snapshot.name)
        (saved / "last").symlink_to(self.snapshot.name)
        self.tmux("set", "-g", "@resurrect-dir", "~/state/$HOSTNAME/resurrect")
        self.script("tmux-agent-resurrect-post-restore")
        self.assertIn(ID, self.option("@agent-resurrect-resume-cmd"))

    def test_inconsistent_worktree_metadata_skips(self):
        self.save()
        record = dict(self.records()[0], worktree="not-this-directory")
        self.snapshot.write_text("agent-resurrect\t" + json.dumps(record) + "\n")
        self.assertIn(
            "inconsistent worktree",
            self.script("tmux-agent-resurrect-post-restore", self.snapshot).stderr,
        )
        self.assertEqual(self.option("@agent-resurrect-resume-cmd"), "")

    def test_dispatch_leaves_finding_the_agent_to_the_pane_shell(self):
        self.save()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.attach()
        # The tmux server's PATH lacks Claude, as when tmux starts before the
        # shell configuration that adds it; the pane's shell finds it.
        sparse = self.base / "without-claude"
        sparse.mkdir()
        for command in ["tmux", "jq"]:
            (sparse / command).symlink_to(shutil.which(command))
        # Run the source entry point directly: no host PATH and no real Claude.
        result = self.run_command(
            [BASH, str(ROOT / "bin/tmux-agent-resurrect-arm"), self.session],
            env=dict(self.env, PATH=str(sparse)),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.expect_launches(1)

    def test_a_failed_dispatch_stays_in_snapshots(self):
        self.save()
        expected = self.records()
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        # The pane's shell does not find Claude.
        (self.stubdir / "claude").rename(self.stubdir / "claude-unavailable")
        self.attach()
        self.script("tmux-agent-resurrect-arm", self.session)
        self.assertEqual(self.option("@agent-resurrect-launched"), "1")
        self.write_snapshot(command="bash", full="bash")
        self.script("tmux-agent-resurrect-save", self.snapshot)
        self.assertEqual(self.records(), expected)
        # Restoring again does not type it a second time.
        (self.stubdir / "claude-unavailable").rename(self.stubdir / "claude")
        self.script("tmux-agent-resurrect-post-restore", self.snapshot)
        self.assertNotEqual(self.option("@agent-resurrect-resume-data"), "")
        time.sleep(0.15)
        self.assertEqual(self.launches(), [])

    def test_missing_jq_reports_dependency(self):
        # Exercise the ordinary source script: Nix wrappers supply jq by design.
        sparse = self.base / "sparse"
        sparse.mkdir()
        (sparse / "tmux").symlink_to(TMUX)
        result = self.run_command(
            [BASH, str(ROOT / "bin/tmux-agent-resurrect-claude-hook")],
            data="{}",
            env=dict(self.env, PATH=str(sparse)),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required command not found on PATH: jq", result.stderr)

    @unittest.skipUnless(CLAUDE, "CLAUDE_BINARY is supplied by the agents check")
    def test_real_claude_records_and_clears_its_session_on_a_new_pane(self):
        self.load_plugin()
        config = self.home / "claude config"
        listed = {"CLAUDE_CODE_PLUGIN_DIRS": str(TMUX_PLUGIN)}
        if SEPARATE:
            # Install the Claude plugin as Home Manager's
            # programs.claude-code.plugins does.
            (config / "skills").mkdir(parents=True)
            (config / "skills/tmux-resurrect").symlink_to(CLAUDE_PLUGIN)
            listed = {}
        # Requests go to a closed local port, so nothing leaves the machine;
        # session hooks run before the first request fails.
        for name, value in {
            "CLAUDE_CONFIG_DIR": str(config),
            "ANTHROPIC_API_KEY": "sk-ant-test-not-a-key",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }.items():
            self.tmux("set-environment", "-g", name, value)
        command = f"{shlex.quote(CLAUDE)} -p hello; sleep 60"
        self.tmux("new-window", "-d", "-n", "claude", command)
        pane = self.tmux("display", "-p", "-t", "work:claude", "#{pane_id}")
        pane = pane.stdout.strip()

        def recorded():
            result = self.tmux(
                "show", "-pqv", "-t", pane, "@agent-resurrect-session", check=False
            )
            return result.stdout.strip()

        session = wait_until(recorded, "Claude did not record its session", 30)
        self.assertRegex(session, r"^claude [0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
        self.tmux("set", "-g", "@resurrect-save-script-path", "/resurrect/save.sh")
        result = self.script(
            "tmux-agent-resurrect-check",
            env=dict(
                self.env,
                PATH=f"{Path(CLAUDE).parent}:{BIN}:{os.environ['PATH']}",
                CLAUDE_CONFIG_DIR=str(config),
                **listed,
            ),
        )
        self.assertIn(
            "ok       every pane running an agent has a recorded session", result.stdout
        )
        self.assertTrue(result.stdout.endswith("No problems found.\n"))
        # Interrupting Claude ends its session while the pane stays open.
        self.tmux("send-keys", "-t", pane, "C-c")
        wait_until(lambda: not recorded(), "Claude did not clear its session", 30)
        self.wait_for_exit(pane, "claude")

    def configure_opencode(self):
        # OpenCode in new panes uses its own directories and a provider on a
        # closed local port, so nothing leaves the machine; a session exists
        # before its first request fails. Return the configuration directory.
        self.load_plugin()
        config = self.home / "opencode config"
        (config / "opencode").mkdir(parents=True)
        settings = {
            "autoupdate": False,
            "share": "disabled",
            "enabled_providers": ["fake"],
            "provider": {
                "fake": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Fake provider",
                    "options": {"baseURL": "http://127.0.0.1:9/v1", "apiKey": "x"},
                    "models": {"fake-model": {"name": "Fake model"}},
                }
            },
            "model": "fake/fake-model",
            "small_model": "fake/fake-model",
        }
        if SEPARATE:
            # Install the OpenCode plugin as Home Manager's
            # programs.opencode.settings.plugin does.
            settings["plugin"] = [str(OPENCODE_PLUGIN / "opencode")]
        (config / "opencode/opencode.json").write_text(json.dumps(settings))
        self.opencode_env = {
            "XDG_CONFIG_HOME": str(config),
            "XDG_DATA_HOME": str(self.home / "opencode data"),
            "XDG_CACHE_HOME": str(self.home / "opencode cache"),
            "XDG_STATE_HOME": str(self.home / "opencode state"),
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
        }
        for name, value in self.opencode_env.items():
            self.tmux("set-environment", "-g", name, value)
        return config

    def open_opencode(self, binary, *args, name="opencode"):
        # The check asks Claude for its version and plugins.
        path = shlex.quote(self.fake_claude()["PATH"])
        command = f"PATH={path} {shlex.join([binary, *args])}; sleep 60"
        self.tmux("new-window", "-d", "-n", name, "-c", str(self.cwd), command)
        pane = self.tmux("display", "-p", "-t", f"work:{name}", "#{pane_id}")
        return pane.stdout.strip()

    def recorded(self, pane):
        result = self.tmux(
            "show", "-pqv", "-t", pane, "@agent-resurrect-session", check=False
        )
        return result.stdout.strip()

    def prompt_opencode(self, pane):
        # OpenCode records a new session at its first prompt. Prompt again until
        # it does, in case the first one comes before the plugin has loaded.
        for _ in range(6):
            self.tmux("send-keys", "-t", pane, "-l", "hello")
            self.tmux("send-keys", "-t", pane, "Enter")
            try:
                session = wait_until(
                    lambda: self.recorded(pane), "OpenCode did not record", 10
                )
                break
            except AssertionError:
                continue
        else:
            self.fail("OpenCode did not record its session")
        self.assertRegex(session, r"^opencode ses_[0-9A-Za-z]+$")
        cwd = self.option("@agent-resurrect-cwd", pane)
        self.assertTrue(cwd.startswith("/"), cwd)
        self.assertEqual(Path(cwd).resolve(), self.cwd.resolve())
        return session

    def check_in_opencode(self, pane, session):
        # The check command adds the check's output for this session to its
        # prompt, which the TUI shows. Interrupt the failing request first;
        # the first Enter takes the completion.
        for _ in range(2):
            self.tmux("send-keys", "-t", pane, "Escape")
            time.sleep(0.5)
        self.tmux("send-keys", "-t", pane, "-l", "/tmux-resurrect-check")
        for _ in range(2):
            time.sleep(1)
            self.tmux("send-keys", "-t", pane, "Enter")
        line = f"this pane records session {session.split()[1]}"
        wait_until(
            lambda: line in self.tmux("capture-pane", "-p", "-t", pane).stdout,
            "OpenCode did not show the check",
            30,
        )

    def quit_opencode(self, pane):
        # Interrupting OpenCode at an empty prompt, after interrupting its
        # request, exits it cleanly.
        for key in ["Escape", "Escape", "C-c"]:
            self.tmux("send-keys", "-t", pane, key)
            time.sleep(0.5)
        wait_until(
            lambda: not self.recorded(pane), "OpenCode did not clear its session", 30
        )
        self.wait_for_exit(pane, "opencode")

    @unittest.skipUnless(
        OPENCODE_1, "OPENCODE_1_BINARY is supplied by the agents check"
    )
    def test_real_opencode_1_records_checks_and_clears_its_session(self):
        config = self.configure_opencode()
        # OpenCode skips installing its plugin package into a configuration
        # directory it cannot write, which needs the network.
        (config / "opencode").chmod(0o555)
        self.addCleanup((config / "opencode").chmod, 0o755)
        pane = self.open_opencode(OPENCODE_1)
        session = self.prompt_opencode(pane)
        self.check_in_opencode(pane, session)
        self.quit_opencode(pane)

    @unittest.skipUnless(
        OPENCODE_2, "OPENCODE_2_BINARY is supplied by the agents check"
    )
    def test_real_opencode_2_records_checks_and_clears_the_session_it_shows(self):
        config = self.configure_opencode()
        # OpenCode starts a background server, which later OpenCode processes
        # share and which outlives them. Give it a port of its own rather than
        # the fixed default, and stop it before cleanup removes its directories.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        (config / "opencode/service.json").write_text(json.dumps({"port": port}))
        self.addCleanup(self.stop_opencode_server)
        pane = self.open_opencode(OPENCODE_2)
        session = self.prompt_opencode(pane)
        self.check_in_opencode(pane, session)
        self.quit_opencode(pane)
        # OpenCode records a session it opens, without a prompt.
        pane = self.open_opencode(OPENCODE_2, "-s", session.split()[1], name="resumed")
        wait_until(
            lambda: self.recorded(pane) == session,
            "OpenCode did not record the session it opened",
            30,
        )
        self.quit_opencode(pane)

    def stop_opencode_server(self):
        # Close the panes first, so that no OpenCode starts the server again.
        self.tmux("kill-server", check=False)
        self.run_command(
            [OPENCODE_2, "service", "stop"],
            env=dict(self.env, **self.opencode_env),
            check=False,
            timeout=30,
        )

    def wait_for_exit(self, pane, agent):
        # The agent still writes to its configuration directory after its
        # session ends, and cleanup would race those writes. Nix wrappers run
        # a binary named .AGENT-wrapped.
        def running():
            command = self.tmux("display", "-p", "-t", pane, "#{pane_current_command}")
            return re.search(rf"(^|/)\.?{agent}($|[-. ])", command.stdout.strip())

        wait_until(lambda: not running(), f"{agent} did not exit", 30)

    def install_tpm(self, plugins=None):
        # TPM and resurrect, where the README's configuration loads them, or
        # in PLUGINS.
        plugins = plugins or self.home / ".tmux/plugins"
        for name, source in [("tpm", TPM), ("tmux-resurrect", RESURRECT)]:
            copied = self.copy_tree(Path(source), plugins / name)
            # TPM treats any Git repository in its directory as installed.
            self.git(copied, "init", "-q")
        return plugins

    def upstream(self, name):
        # A local repository stands in for the GitHub repository.
        upstream = self.base / "upstream" / name
        upstream.mkdir(parents=True)
        self.git(upstream, "init", "-q")
        return upstream

    def commit(self, upstream, message):
        self.git(upstream, "add", "-A")
        self.git(upstream, "commit", "-q", "-m", message)

    def readme_block(self, heading, language):
        # The first LANGUAGE code block in the README section HEADING.
        readme = (ROOT / "README.md").read_text()
        section = readme.split(f"\n### {heading}\n", 1)[1].split("\n### ", 1)[0]
        return section.split(f"```{language}\n", 1)[1].split("```", 1)[0]

    def readme_configuration(self, plugin=None, config=None, plugins=None):
        # The README's TPM configuration, with PLUGIN on its plugin line, or
        # without that line, in CONFIG. TPM reads plugin names from the user's
        # configuration file. With PLUGINS, the configuration runs TPM from
        # there, as the README says to keep one's own run line.
        configuration = self.readme_block("With TPM", "tmux")
        line = "set -g @plugin 'kallisti-dev/tmux-agent-resurrect'\n"
        self.assertIn(line, configuration)
        configuration = configuration.replace(
            line, f"set -g @plugin '{plugin}'\n" if plugin else ""
        )
        if plugins:
            run = "run '~/.tmux/plugins/tpm/tpm'"
            self.assertIn(run, configuration)
            configuration = configuration.replace(
                run, f"run {shlex.quote(str(plugins / 'tpm/tpm'))}"
            )
        config = config or self.home / ".tmux.conf"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(configuration)
        return config

    @unittest.skipUnless(TPM and RESURRECT, "TPM_SOURCE and RESURRECT_SOURCE are set")
    @unittest.skipIf(TMUX_PLUGIN != ROOT, "TPM installs from a Git repository")
    @unittest.skipIf(system_plugins(), "TPM also reads @plugin lines in /etc/tmux.conf")
    def test_tpm_installs_updates_and_cleans_the_readme_configuration(self):
        self.check_tpm_lifecycle()

    @unittest.skipUnless(TPM and RESURRECT, "TPM_SOURCE and RESURRECT_SOURCE are set")
    @unittest.skipIf(TMUX_PLUGIN != ROOT, "TPM installs from a Git repository")
    @unittest.skipIf(system_plugins(), "TPM also reads @plugin lines in /etc/tmux.conf")
    def test_tpm_installs_beside_a_configuration_in_the_xdg_directory(self):
        tmux = self.home / ".config/tmux"
        self.check_tpm_lifecycle(tmux / "tmux.conf", tmux / "plugins")

    def check_tpm_lifecycle(self, config=None, plugins=None):
        # Install, update, and clean the plugin with TPM, from the README's
        # configuration in CONFIG, with TPM and its plugins in PLUGINS.
        directory = self.install_tpm(plugins)
        upstream = self.upstream("tmux-agent-resurrect")
        self.copy_plugin(upstream)
        self.commit(upstream, "Release")
        config = self.readme_configuration(upstream, config, plugins)
        self.tmux("source-file", str(config))
        self.run_with_git(directory / "tpm/bin/install_plugins")
        self.tmux("source-file", str(config))
        installed = directory / "tmux-agent-resurrect"
        self.assertEqual(self.plugin_dirs(), str(installed))
        save = self.global_option("@resurrect-hook-post-save-layout")
        self.assertIn(str(installed / "bin/tmux-agent-resurrect-save"), save)

        (upstream / "UPDATED").touch()
        self.commit(upstream, "Update")
        self.run_with_git(directory / "tpm/bin/update_plugins", "tmux-agent-resurrect")
        self.assertTrue((installed / "UPDATED").is_file())
        self.tmux("source-file", str(config))
        self.assertEqual(self.plugin_dirs(), str(installed))
        self.assertEqual(self.global_option("@resurrect-hook-post-save-layout"), save)

        self.readme_configuration(config=config, plugins=plugins)
        self.run_with_git(directory / "tpm/bin/clean_plugins")
        self.assertFalse(installed.exists())
        self.tmux("source-file", str(config))
        self.write_snapshot()
        original = self.snapshot.read_bytes()
        self.execute_resurrect_hook("post-save-layout", self.snapshot)
        self.assertEqual(self.snapshot.read_bytes(), original)

    @unittest.skipUnless(RESURRECT, "RESURRECT_SOURCE is supplied by Nix")
    @unittest.skipIf(TMUX_PLUGIN != ROOT, "the README clones the Git repository")
    def test_readme_manual_installation_loads_and_updates_a_clone(self):
        upstream = self.upstream("tmux-agent-resurrect")
        self.copy_plugin(upstream)
        self.commit(upstream, "Release")
        url = "https://github.com/kallisti-dev/tmux-agent-resurrect"
        clone = self.readme_block("Manual installation", "sh")
        words = shlex.split(clone)
        self.assertEqual(words[:3], ["git", "clone", url])
        # The README's commands run in a shell, which expands ~ to HOME.
        self.run_with_git(BASH, "-c", clone.replace(url, shlex.quote(str(upstream))))
        installed = self.home / words[3].removeprefix("~/")
        config = self.home / ".tmux.conf"
        config.write_text(
            f"run-shell {shlex.quote(str(Path(RESURRECT) / 'resurrect.tmux'))}\n"
            + self.readme_block("Manual installation", "tmux")
        )
        self.tmux("source-file", str(config))
        self.assertEqual(self.global_option("@agent-resurrect-dir"), str(installed))
        self.assertEqual(self.plugin_dirs(), str(installed))
        save = self.global_option("@resurrect-hook-post-save-layout")
        self.assertIn(str(installed / "bin/tmux-agent-resurrect-save"), save)

        (upstream / "UPDATED").touch()
        self.commit(upstream, "Update")
        self.run_with_git(BASH, "-c", self.readme_block("Updating", "sh"))
        self.assertTrue((installed / "UPDATED").is_file())
        self.assertEqual(self.plugin_dirs(), str(installed))
        self.assertEqual(self.global_option("@resurrect-hook-post-save-layout"), save)

    @unittest.skipUnless(
        OH_MY_TMUX and TPM and RESURRECT,
        "OH_MY_TMUX_SOURCE, TPM_SOURCE, and RESURRECT_SOURCE are set",
    )
    @unittest.skipIf(system_plugins(), "TPM also reads @plugin lines in /etc/tmux.conf")
    def test_oh_my_tmux_loads_the_readme_plugin_lines_from_its_local_configuration(
        self,
    ):
        # Oh my tmux! runs TPM in the background for the @plugin lines in
        # .tmux.conf.local. Installed copies stand in for its downloads.
        plugins = self.install_tpm()
        installed = plugins / "tmux-agent-resurrect"
        self.copy_plugin(installed)
        lines = self.readme_block("With Oh my tmux!", "tmux")
        local = (Path(OH_MY_TMUX) / ".tmux.conf.local").read_text()
        for event in ["launch", "reload"]:
            setting = f"tmux_conf_update_plugins_on_{event}="
            self.assertIn(setting + "true", local)
            local = local.replace(setting + "true", setting + "false")
        (self.home / ".tmux.conf.local").write_text(local + lines)
        config = self.home / ".tmux.conf"
        shutil.copyfile(Path(OH_MY_TMUX) / ".tmux.conf", config)
        # It checks whether GitHub is reachable even when it doesn't update.
        curl = self.stubdir / "curl"
        curl.write_text("#!/bin/sh\nexit 1\n")
        curl.chmod(0o755)
        self.tmux("set-environment", "-g", "GIT_ALLOW_PROTOCOL", "file")
        self.tmux("source-file", str(config))
        wait_until(
            lambda: self.global_option("@agent-resurrect-dir") == str(installed),
            "Oh my tmux! did not load the plugin",
            30,
        )
        # Cleanup would race the rest of its background job.
        wait_until(
            lambda: not self.tmux("show", "-sqv", "@__apply_plugins").stdout.strip(),
            "Oh my tmux! did not finish loading plugins",
            30,
        )
        self.assertTrue(self.global_option("@resurrect-save-script-path"))
        self.assertEqual(self.plugin_dirs(), str(installed))
        save = self.global_option("@resurrect-hook-post-save-layout")
        self.assertIn(str(installed / "bin/tmux-agent-resurrect-save"), save)

    @unittest.skipUnless(
        RESURRECT, "RESURRECT_SOURCE is supplied by nix develop / flake checks"
    )
    def test_real_resurrect_save_restore_and_snapshot_deduplication(self):
        self.transcript()
        self.hook("SessionStart")
        self.load_plugin()
        self.tmux("set", "-g", "@resurrect-processes", ":all:")
        self.run_command([BASH, str(Path(RESURRECT) / "scripts/save.sh"), "quiet"])
        last = self.snapdir / "last"
        self.assertTrue(last.is_file())
        self.assertEqual(self.records(last)[0]["session_id"], ID)
        saved = last.read_bytes()
        # Upstream names snapshots to the second; a same-second save would
        # overwrite its own comparison target. Test dedup across distinct names.
        saved_second = int(last.stat().st_mtime)
        wait_until(lambda: int(time.time()) > saved_second, "clock did not advance")
        self.run_command([BASH, str(Path(RESURRECT) / "scripts/save.sh"), "quiet"])
        self.assertEqual(last.read_bytes(), saved)
        self.hook("SessionEnd")
        # Keep server alive while removing the saved session, then restore it.
        self.tmux("new-session", "-d", "-s", "bootstrap", "-c", str(self.cwd))
        self.tmux("new-window", "-t", "bootstrap")
        self.tmux("kill-session", "-t", self.session)
        result = self.run_command([BASH, str(Path(RESURRECT) / "scripts/restore.sh")])
        self.assertNotIn("tmux-agent-resurrect:", result.stderr)
        self.pane = self.tmux(
            "display", "-p", "-t", "work:0.0", "#{pane_id}"
        ).stdout.strip()
        self.assertIn(ID, self.option("@agent-resurrect-resume-cmd"))
        self.assertEqual(self.launches(), [])
        self.attach()
        self.assertEqual(self.expect_launches(1)[0]["argv"], ["--resume", ID])

    @unittest.skipUnless(RESURRECT, "RESURRECT_SOURCE is supplied by Nix")
    def test_real_resurrect_attached_restore_keeps_other_sessions_deferred(self):
        self.transcript()
        self.hook("SessionStart")
        self.tmux("new-session", "-d", "-s", "later", "-c", str(self.cwd))
        later_pane = self.tmux(
            "display", "-p", "-t", "later", "#{pane_id}"
        ).stdout.strip()
        self.transcript(session_id="later-id")
        self.hook("SessionStart", "later-id", env=dict(self.env, TMUX_PANE=later_pane))
        self.load_plugin()
        self.attach()
        self.run_command([BASH, str(Path(RESURRECT) / "scripts/save.sh"), "quiet"])
        self.assertEqual(len(self.records(self.snapdir / "last")), 2)
        self.tmux("new-session", "-d", "-s", "bootstrap", "-c", str(self.cwd))
        self.tmux("new-window", "-t", "bootstrap")
        self.tmux("switch-client", "-t", "bootstrap")
        self.tmux("kill-session", "-t", self.session)
        self.tmux("kill-session", "-t", "=later")
        self.run_command([BASH, str(Path(RESURRECT) / "scripts/restore.sh")])
        self.assertEqual(self.expect_launches(1)[0]["argv"], ["--resume", ID])
        later_pane = self.tmux(
            "display", "-p", "-t", "later", "#{pane_id}"
        ).stdout.strip()
        self.assertIn(
            "later-id", self.option("@agent-resurrect-resume-cmd", later_pane)
        )
        self.tmux("switch-client", "-t", "=later")
        self.assertEqual(self.expect_launches(2)[1]["argv"], ["--resume", "later-id"])


if __name__ == "__main__":
    unittest.main()
