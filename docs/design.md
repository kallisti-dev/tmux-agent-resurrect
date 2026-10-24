# Design

tmux-resurrect restores the layout and restarts ordinary programs; this plugin
restarts each agent pane's conversation exactly. It has two halves:

- A plugin for each harness reports the session its pane runs. The Claude Code
  plugin does this with session hooks; the OpenCode plugin with OpenCode's
  plugin API.
- The tmux plugin registers callbacks with tmux-resurrect and tmux. Saving
  adds the reported sessions to the resurrect snapshot, restoring prepares a
  resume command for each pane, and attaching a client types those commands
  into their panes.

The only interface between the halves is a set of pane options, described in
[tmux options](#tmux-options). Each pane's `@agent-resurrect-session` holds
the harness and session ID it runs, such as `claude 1111…`.

## Loading

tmux runs `tmux-agent-resurrect.tmux`, which runs setup and shows a failure
on attached clients, since TPM discards its output. Setup registers the
callbacks described under [hook registration](#hook-registration) and records
the plugin's directory and version in `@agent-resurrect-dir` and
`@agent-resurrect-version`.

The repository is also a Claude Code plugin and holds the OpenCode plugin.
When the plugin directory has `hooks/hooks.json`, setup adds the directory to
`CLAUDE_CODE_PLUGIN_DIRS` in tmux's global environment, and Claude Code loads
it in place as the session-only plugin `tmux-resurrect@inline`. When it has an
`opencode` directory, setup adds that directory to the `plugin` list in the
global `OPENCODE_CONFIG_CONTENT`, keeping the rest of that JSON, and OpenCode
loads it beside the plugins in its configuration files. The harness
plugins then run from the same files as the tmux callbacks. When the plugin
moves, setup replaces the entry it added and keeps entries that something else
added.

The Nix packages split the halves instead. The tmux plugin package has neither
harness plugin, so setup leaves both variables alone, and each harness plugin
package is installed where its harness loads plugins: Claude Code's `skills`
directory, or OpenCode's configuration file.

A process keeps the environment it started with. Harnesses started in panes
opened before the plugin first loaded don't load it, so setup reports that
when it first loads into a server that already has panes. OpenCode 2 reads
`OPENCODE_CONFIG_CONTENT` in a background server that every OpenCode shares,
so it loads the plugin only if the OpenCode that started the server had it
listed there. A pane opened before the plugin moved, such as to a new Nix store
path, starts Claude with the earlier copy. At SessionStart, the Claude hook
compares its own manifest version with `@agent-resurrect-version` when it runs
from another directory, and reports a difference once per pair of versions;
this also covers separately installed packages of different versions.

## Recording sessions

`record_session` in `lib/tmux-agent-resurrect.sh` sets the session's pane
options, setting `@agent-resurrect-session` after its directory and launch
details so that a reader that sees the session also sees them, then clears any
pending resume for the pane.
`end_session` clears them only while the pane still records the ending session,
comparing and clearing in a single tmux command, so a late end cannot clear a
newer session.

Claude Code runs `tmux-agent-resurrect-claude-hook` on SessionStart and
SessionEnd. On SessionStart, the hook records the session ID together with the
launch details that save adds to the session's record: the session's
`CLAUDE_CONFIG_DIR`, since its transcript is under that directory and resuming
needs the same one, and the options a resume repeats. It reads the options from
the command line of `CLAUDE_PID`, the Claude process:

- It repeats those that configure the session and that `--resume` does not
  restore, such as `--settings` and `--add-dir`. It leaves out those that
  choose or create a session, apply only with `--print`, or set the permission
  mode, which the resumed session restores. Save adds
  `--dangerously-skip-permissions` from the transcript instead. The values of
  `--model` and `--effort` are kept apart from the options, since save prefers
  later ones from the transcript.
- Relative paths are resolved against the directory Claude started in. A
  `--settings` or `--mcp-config` value that isn't a file is JSON, which may
  hold secrets, so it is not repeated.
- It stops at the first argument whose meaning is uncertain, such as an option
  it doesn't know or the prompt. The options it knows are Claude Code
  2.1.300's.
- Linux keeps the arguments apart in `/proc`. On macOS, `ps` joins them with
  spaces, so a path is rejoined only where the joined words name an existing
  path, and text such as a system prompt ends the options.

The OpenCode plugin runs `tmux-agent-resurrect-record start opencode ID
DIRECTORY` for the root session its TUI is in, ignoring subagent sessions, and
`tmux-agent-resurrect-record end opencode ID` when OpenCode shuts down cleanly.
The `opencode` directory holds a plugin for each version of OpenCode, which
share `shared.js`:

- OpenCode 1 loads `tmux-agent-resurrect.js`, the main file in the
  directory's `package.json`. OpenCode tells it which session the TUI shows
  only when that session is created or gets a prompt, so a new session is
  reported at its first prompt, a session reopened with `-s` or switched to at
  its next prompt, and an unclean exit not at all. The TUI runs plugins in a
  worker thread with the pane's environment; `opencode run`, `serve`, and
  `web` run them on the main thread, where the plugin does nothing.
- OpenCode 2 loads `server.js` in its background server, which the TUIs in
  every pane share, and `tui.js` in each TUI, with that TUI's environment. The
  TUI part follows the TUI's route, so it reports a session as soon as the TUI
  shows it. The server part does nothing, but without it OpenCode doesn't load
  the TUI part.

The OpenCode plugin also adds the command `/tmux-resurrect-check`. When it
runs, the plugin first records the session, since in OpenCode 1 the command
may be its first prompt, then runs `tmux-agent-resurrect-check --session ID`
and has OpenCode explain the output: OpenCode 1 appends it to the command's
prompt, and OpenCode 2 sends it to the session as a prompt. Before OpenCode 2
shows a session, the command shows the check's output in a dialog instead. The
check gives up on `claude` after 10 seconds for each command, and the plugin
gives up on the check after 30, so the command always reaches OpenCode.

## Saving

tmux-resurrect runs `tmux-agent-resurrect-save SNAPSHOT` after writing its
layout. For each pane line in the snapshot, save finds the tmux pane and
whether it runs a harness: its `#{pane_current_command}` or, failing that,
its command line names one. macOS names a process after the file a symlink
resolves to, such as `versions/2.1.300` for Claude's native installer, and an
npm installation runs as `node`.

A pane that records a session gets a [snapshot record](#snapshot-records):

- For Claude, the directory is the latest absolute `cwd` in the session's
  transcript, or the pane's directory if the transcript has none; a directory
  under `REPO/.claude/worktrees/NAME` also sets the worktree. The record's
  options gain the session's settings, described below.
- For OpenCode, the directory is the one the plugin reported. OpenCode may not
  report an end, so its session is saved only while the pane still runs
  OpenCode.

`--resume` doesn't restore a Claude session's model or effort level, and
Claude's settings can't keep max effort as the default. Unless
`@agent-resurrect-harness-resume` is `on`, save adds the settings the session
last used to the record's options, from its transcript:

- `--dangerously-skip-permissions` if the latest permission mode is
  `bypassPermissions`. Claude restores the other modes itself.
- `--model` and `--effort` with the `requestedModel` and `effort` of the latest
  response, or else the values Claude was started with. A skill can set its
  own model or effort level for the rest of its turn, which `/model` and
  `/effort` don't override, so the responses made in skills since the latest
  response outside one don't count when they all have one value that differs
  from that response's, unless a typed `/model` or `/effort` has come since.
  Responses after a fallback to another model don't count until the turn ends.
- `--effort ultracode` instead if ultracode was on: Claude adds an
  `ultra_effort_enter` attachment while it is on and an `ultra_effort_exit` one
  when it goes off.

With the option `on`, the options gain only the model and effort level Claude
was started with.

A pane waiting for its resume, or whose resume was dispatched but whose
harness hasn't reported a session yet, keeps its pending record, so a save
between restoring and resuming loses nothing. A pane that runs Claude without
a recorded session is reported, once each time that set of panes changes,
since automatic saves repeat. A pane running OpenCode without one is not
reported, because before its first prompt there is nothing to resume.

Save then sets the command field of each agent pane's line to `:`, which stops
resurrect from restarting the harness itself, appends the records, and replaces
the snapshot through a file created with mode 0600.

## Restoring

Resurrect switches between sessions while it rebuilds the layout, which would
run the attach hooks. `tmux-agent-resurrect-pre-restore` sets
`@agent-resurrect-restoring`, which stops dispatch, and
`tmux-agent-resurrect-post-restore` clears it when it has finished.

Post-restore reads the snapshot that resurrect restored, by default its `last`
link, under the same directory that resurrect uses. A new restore replaces the
pending resumes from an earlier one, except for panes already dispatched. For
each valid record whose pane exists and has neither a recorded session nor a
dispatched resume, it builds the resume command:

- Claude: `cd -- DIR && claude --resume ID`, with `CLAUDE_CONFIG_DIR=…` before
  `claude` if the session had one, and the repeated options after it. For a
  worktree, the command runs from the main repository and adds
  `--worktree NAME`.
- OpenCode: `cd -- DIR && opencode -s ID`.

There is no fallback to a session picker. A Claude session is skipped if its
transcript is missing, ambiguous, or empty, or if its worktree no longer
exists in an existing repository, since worktrees are routinely removed. A
session whose directory, main repository, or Claude configuration directory
does not exist may be on a volume that is not mounted yet. It stays pending,
and so in later snapshots, until a dispatch finds the directory; closing its
pane discards it.

Post-restore stores the record and command on the pane, marks the pane's
session as pending, reports skipped and waiting sessions in one notice, and
dispatches the sessions that have attached clients.

## Dispatching

`tmux-agent-resurrect-arm` runs when a client attaches to or switches to a
tmux session, and after a restore. It does nothing while a restore is running
or for a session without attached clients, so a detached restore starts no
harnesses. A per-session tmux lock serializes clients that attach at once.

For each pending pane, it rebuilds the resume command, so a directory that has
appeared since the restore is found, and types it into the pane with
`send-keys -l`, followed by Enter. It types only at a shell prompt, which it
recognizes by `bash`, `zsh`, `sh`, `dash`, or `ksh` as the pane's command, and
not in copy mode; other panes wait for a later attach, with a notice. The
command does not look for the harness itself; the pane's shell, whose
configuration can extend its `PATH`, finds it.

`@agent-resurrect-launched` then marks the pane for its lifetime, so later
attachments and restores never type into it again. The pane keeps its record
until its harness reports a session, so saves keep a conversation whose
command failed in the shell; see [troubleshooting](troubleshooting.md#a-dispatched-command-failed).

## Hook registration

Setup registers the scripts with resurrect's `post-save-layout`,
`pre-restore-all`, and `post-restore-all` hooks, and runs dispatch from tmux's
`client-attached` and `client-session-changed` hooks. A tmux lock serializes
setup runs.

An empty resurrect hook option runs the script directly. If an option already
has a command, setup combines it with the script in one inline shell function
that forwards the hook's arguments and fails if either command fails. The
pre-restore guard runs first; existing post-restore and save commands run
before the plugin's. The existing command runs in a subshell, so an `exit`
cannot prevent the plugin's from running, and its directory and environment
changes stay there. Setup records the previous and combined values; a reload
reuses them while the option still holds the combined value, and fails with a
request to reset the option if something wrapped it.

For tmux's hook arrays, setup takes a free index and records it with tmux's
canonical text for the handler. A reload reuses that index only while it still
holds that handler, and otherwise takes another free one.

Each registered command first checks that its script still exists, and
otherwise succeeds without running anything. A tmux server keeps its hooks
until it exits, and reloading a configuration does not remove them, so this
keeps hooks left behind by an uninstall from failing while other callbacks
still run. Scheduled notices check the same way, since tmux shows a failed
delayed command in view mode in the current pane, where pending resumes wait.

`tmux-agent-resurrect-setup --remove` removes the tmux hook handlers and the
environment entries, and restores the previous resurrect hook values where the
current values are still setup's.

## Notices

Problems that need the user's attention go to stderr and to every attached
client's message line:

- setup failures
- the first load into a server that already has panes
- a global `OPENCODE_CONFIG_CONTENT` that setup can't add the plugin to
- Claude panes without a recorded session when saving
- sessions a restore can't resume, with the first reason, and sessions waiting
  for a directory
- pending resumes left for a later attach because a pane is not at a shell
  prompt or is in copy mode
- a Claude session that loaded a plugin with another version

tmux-resurrect shows its own message after running its hooks, so a notice is
shown one second after the event, for five seconds. If no client is attached
by then, it is kept in `@agent-resurrect-notices` and shown on the next client
that attaches.

## Snapshot records

Each saved session is a snapshot line of `agent-resurrect`, a tab, and a JSON
object:

| Field | Value |
| --- | --- |
| `version` | `1` |
| `agent` | `claude` or `opencode` |
| `session`, `window`, `pane` | The pane's tmux session name and window and pane indexes |
| `session_id` | The harness's session ID |
| `cwd` | The absolute directory the session resumes in |
| `worktree` | The Claude worktree name, otherwise empty |
| `config_dir` | Claude only: the session's `CLAUDE_CONFIG_DIR`, empty if it was unset |
| `options` | Claude only: the options the resume command passes |

For a record without `config_dir`, restore looks for the transcript under the
tmux server's `CLAUDE_CONFIG_DIR`, and the resume command sets none.
Restore validates each record before using it, and rejects control characters,
which resurrect's line and tab format cannot hold.

The only harness files read are Claude transcripts, at
`CONFIG_DIR/projects/*/SESSION_ID.jsonl` under the session's configuration
directory, by default `~/.claude`. No transcript, worktree, or harness
configuration file is modified, and transcript contents are never copied into
snapshots. The records can reveal project names and paths, and resurrect's own
pane titles, commands, and optional pane contents may reveal more, so keep the
snapshot directory private. The plugin has no network service or telemetry.

## tmux options

| Option | Scope | Purpose |
| --- | --- | --- |
| `@agent-resurrect-session` | Pane | The harness and session ID running in the pane, maintained by its plugin |
| `@agent-resurrect-cwd` | Pane | The session's directory, from harnesses whose session files save does not read |
| `@agent-resurrect-launch` | Pane | JSON from which save builds the session's record, such as Claude's `config_dir`, `options`, `model`, and `effort` |
| `@agent-resurrect-resume-data` | Pane | The record waiting for dispatch, or dispatched and not yet reported by the harness |
| `@agent-resurrect-resume-cmd` | Pane | The resume command, kept for inspection |
| `@agent-resurrect-launched` | Pane | A resume was typed into the pane |
| `@agent-resurrect-pending` | Session | At least one pane waits for dispatch |
| `@agent-resurrect-restoring` | Global | Dispatch is stopped while a restore runs |
| `@agent-resurrect-notices` | Global | Notices raised while no client was attached |
| `@agent-resurrect-unrecorded` | Global | The Claude panes without a session at the last save that reported them |
| `@agent-resurrect-mismatch` | Global | The plugin versions in the last mismatch reported at SessionStart |
| `@agent-resurrect-dir`, `@agent-resurrect-version` | Global | The loaded plugin's directory and version |
| `@agent-resurrect-harness-resume` | Global | Set by the user to `on` to resume sessions as the harness does |

Setup also keeps its registrations in global options: `-previous-PHASE` and
`-managed-PHASE` for each resurrect hook, and `-EVENT-slot` and
`-EVENT-handler` for each tmux hook, all beginning `@agent-resurrect`. These
describe the running server. Removing a hook from the configuration file alone
does not clear a value setup captured; see
[replacing shared hooks](troubleshooting.md#replacing-or-removing-a-shared-hook).

## Components

| Component | Responsibility |
| --- | --- |
| `tmux-agent-resurrect.tmux` | tmux entry point: run setup and show its failures |
| `bin/tmux-agent-resurrect-setup [--remove]` | Register or remove the callbacks and the harness plugin entries |
| `.claude-plugin/plugin.json`, `hooks/hooks.json` | Claude Code plugin manifest and session hooks |
| `bin/tmux-agent-resurrect-claude-hook` | Claude's session hook: record the session and its launch details, or clear it |
| `opencode/tmux-agent-resurrect.js`, `opencode/package.json` | OpenCode 1 plugin: report the TUI's root session, and add `/tmux-resurrect-check`, which adds the check's result to its prompt |
| `opencode/tui.js`, `opencode/server.js` | OpenCode 2 plugin: report the root session the TUI shows, and add `/tmux-resurrect-check`, which sends the check's result to the session |
| `opencode/shared.js` | What the OpenCode plugins share: recording sessions and running the check |
| `bin/tmux-agent-resurrect-record start\|end AGENT ID [DIR]` | Record or clear a session on the calling pane, for the OpenCode plugin |
| `bin/tmux-agent-resurrect-save SNAPSHOT` | Add session records to a snapshot and stop resurrect restarting agent panes |
| `bin/tmux-agent-resurrect-pre-restore` | Stop dispatch while resurrect restores the layout |
| `bin/tmux-agent-resurrect-post-restore [SNAPSHOT]` | Prepare resume commands and dispatch them for attached sessions |
| `bin/tmux-agent-resurrect-arm SESSION_ID [CLIENT]` | Type pending resume commands for a tmux session, and show kept notices on an attaching client |
| `bin/tmux-agent-resurrect-notify MESSAGE` | Show a scheduled notice, or keep it for the next attach |
| `bin/tmux-agent-resurrect-check [--session ID]` | Report problems with the tmux server, the calling shell, Claude Code, and agent panes |
| `skills/check/SKILL.md` | `/tmux-resurrect:check`: have Claude run the check and explain its result |
| `lib/tmux-agent-resurrect.sh` | Shared functions: recording sessions, agent detection, record validation, resume commands, and the snapshot path |
