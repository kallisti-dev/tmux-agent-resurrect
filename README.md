# tmux-agent-resurrect

A tmux plugin that restores agent conversations with
[tmux-resurrect](https://github.com/tmux-plugins/tmux-resurrect).

## Requirements

- macOS or Linux
- tmux 3.2 or newer
- tmux-resurrect
- Bash 3.2 or newer on the tmux server's `PATH`
- jq 1.6 or newer on the tmux server's `PATH`

The [Nix packages](#nix) bring their own Bash and jq.

## Supported harnesses

- Claude Code 2.1.280 or newer, or 2.1.157 or newer with the
  [Nix packages](#nix) and Home Manager
- OpenCode 1.18 or newer in the 1.x line, or 2.0.32 or newer

## Installation

Load the plugin after tmux-resurrect, and before tmux-continuum if you use it.
Set any `@resurrect-hook-*` options before the plugin loads.

### With TPM

Add the plugin to your tmux configuration:

```tmux
set -g @plugin 'tmux-plugins/tpm'
set -g @plugin 'tmux-plugins/tmux-resurrect'
set -g @plugin 'kallisti-dev/tmux-agent-resurrect'

# Keep this line at the end of the configuration.
run '~/.tmux/plugins/tpm/tpm'
```

Then press prefix + I to install the plugin. If your TPM is somewhere else,
such as in `~/.config/tmux/plugins` or where a package manager installed it,
keep your own `run` line. TPM-compatible managers such as
[tpack](https://github.com/tmuxpack/tpack) read the same plugin lines.

### With Oh my tmux!

[Oh my tmux!](https://github.com/gpakosz/.tmux) runs TPM itself. Add the
plugin lines to `.tmux.conf.local`:

```tmux
set -g @plugin 'tmux-plugins/tmux-resurrect'
set -g @plugin 'kallisti-dev/tmux-agent-resurrect'
```

Then press prefix + I to install the plugin.

### Manual installation

Clone the repository:

```sh
git clone https://github.com/kallisti-dev/tmux-agent-resurrect ~/clone/path
```

Add this line to your tmux configuration after the line that loads
tmux-resurrect, then reload the configuration, for example with
`tmux source-file ~/.tmux.conf`:

```tmux
run-shell ~/clone/path/tmux-agent-resurrect.tmux
```

To use a release instead of the `main` branch, add its tag to the clone
command, as in `git clone --branch v0.1.0`.

### Starting agents

Start Claude Code or OpenCode as usual in a pane opened after the plugin
loaded. There is nothing to install into either of these agent harnesses: the
plugin includes a plugin for each and lists them in tmux's global environment,
so a harness started in a new pane loads its plugin from the plugin directory:

- Claude Code loads the directory listed in `CLAUDE_CODE_PLUGIN_DIRS` as the
  session-only plugin `tmux-resurrect@inline`.
- OpenCode loads the plugin directory listed in `OPENCODE_CONFIG_CONTENT`, in
  addition to the plugins in its configuration files.

With the [Nix packages](#nix), each harness loads its plugin from its own
configuration instead.

Panes opened before the plugin first loaded don't have these variables, so
agents started in them are not tracked. Start those sessions again in a new
pane, for example with `claude --continue`, then make a fresh resurrect save.
After the next tmux server start, every pane has the variables, except with
Oh my tmux!, which loads plugins after tmux creates the first session, so that
session's first pane never has them.

OpenCode 2 loads plugins through a background server that every OpenCode
shares, and that server reads `OPENCODE_CONFIG_CONTENT` from the OpenCode that
started it. If the server was already running when the plugin first loaded, or
was started outside tmux, run `opencode service restart` in a new pane.

If your shell startup files set `CLAUDE_CODE_PLUGIN_DIRS` or
`OPENCODE_CONFIG_CONTENT`, add to the inherited value instead of replacing it:

```sh
export CLAUDE_CODE_PLUGIN_DIRS="${CLAUDE_CODE_PLUGIN_DIRS:+$CLAUDE_CODE_PLUGIN_DIRS:}$HOME/my-plugins"
```

### Nix

The flake provides the tmux plugin and each harness's plugin as separate
packages, each with its runtime dependencies wrapped in:

- `tmux-plugin`, built with nixpkgs' `tmuxPlugins.mkTmuxPlugin`
- `claude-plugin`, the Claude Code plugin with the session hooks and
  `/tmux-resurrect:check`
- `opencode-plugin`, whose `opencode` directory is the OpenCode plugin

Install each where you configure that program. With Home Manager:

```nix
{ inputs, pkgs, ... }:
let
  tmux-agent-resurrect = inputs.tmux-agent-resurrect.packages.${pkgs.stdenv.hostPlatform.system};
in
{
  programs.tmux.plugins = [
    pkgs.tmuxPlugins.resurrect
    tmux-agent-resurrect.tmux-plugin
  ];
  programs.claude-code.plugins.tmux-resurrect = tmux-agent-resurrect.claude-plugin;
  programs.opencode.settings.plugin = [
    "${tmux-agent-resurrect.opencode-plugin}/opencode"
  ];
}
```

Home Manager links the Claude plugin into Claude Code's `skills` directory,
where Claude Code 2.1.157 or newer loads it in every session, and lists the
OpenCode plugin in OpenCode's configuration file. The tmux plugin then leaves
`CLAUDE_CODE_PLUGIN_DIRS` and `OPENCODE_CONFIG_CONTENT` alone, so a harness
restarted in any pane loads its plugin. Without Home Manager, link the Claude
plugin package to `~/.claude/skills/tmux-resurrect` and list the OpenCode
plugin directory in the `plugin` list of OpenCode's `opencode.json` yourself.

NixOS's `programs.tmux.plugins` takes the same tmux plugin. nix-darwin's
`programs.tmux` has no plugin list, so run each plugin's `rtp` script from its
`extraConfig` instead:

```nix
programs.tmux.extraConfig = ''
  run-shell ${pkgs.tmuxPlugins.resurrect.rtp}
  run-shell ${tmux-agent-resurrect.tmux-plugin.rtp}
'';
```

Home Manager and NixOS load tmux plugins before `programs.tmux.extraConfig`,
so set any `@resurrect-hook-*` options in a plugin entry's `extraConfig` with
Home Manager, or in `programs.tmux.extraConfigBeforePlugins` with NixOS.

Without flakes, `default.nix` builds the same packages with the nixpkgs
revision pinned in `flake.lock`, or with a `pkgs` argument you pass:

```nix
tmux-agent-resurrect = import (builtins.fetchTarball
  "https://github.com/kallisti-dev/tmux-agent-resurrect/archive/main.tar.gz") { inherit pkgs; };
# Then use tmux-agent-resurrect.tmux-plugin, .claude-plugin, and .opencode-plugin.
```

### Checking the setup

#### Claude Code

In a Claude session, run `/tmux-resurrect:check`. Claude runs the
[manual check](#manual) for its session and explains any problem it reports.

#### OpenCode

In an OpenCode session, run `/tmux-resurrect-check`. The plugin records the
session, adds the [manual check](#manual) for it to the prompt, and OpenCode
explains any problem it reports.

#### Manual

From a shell in a tmux pane, run the plugin's check. The plugin records its
directory in a tmux option:

```sh
"$(tmux show-options -gqv @agent-resurrect-dir)"/bin/tmux-agent-resurrect-check
```

### Updating

#### TPM

Press prefix + U. TPM follows the `main` branch. To stay on a release instead,
add its tag to the plugin line:

```tmux
set -g @plugin 'kallisti-dev/tmux-agent-resurrect#v0.1.0'
```

prefix + U leaves a pinned plugin at its tag, and TPM doesn't reinstall a
plugin whose tag changed. To upgrade, change the tag, delete the plugin
directory, which `tmux show-options -gqv @agent-resurrect-dir` prints, and
press prefix + I.

#### Oh my tmux!

Press prefix + u, or reload the configuration, which updates plugins by
default. A tag on the plugin line pins a release as with TPM.

#### Manual

Pull the changes and reload the configuration:

```sh
git -C ~/clone/path pull
tmux source-file ~/.tmux.conf
```

For a clone of a release, fetch the new tag and check it out instead of
pulling.

#### Nix

Update the flake input, for example with
`nix flake update tmux-agent-resurrect`, switch to the new configuration, and
reload tmux's configuration. The harnesses load their plugins from the same
installation, so there is nothing to update separately.

### Uninstalling

The callbacks the plugin registered stay in the running tmux server and do
nothing once its files are deleted; they and its entries in
`CLAUDE_CODE_PLUGIN_DIRS` and `OPENCODE_CONFIG_CONTENT` disappear when the
server exits. To clear them from a running server instead, run this inside it
before removing the plugin:

```sh
"$(tmux show-options -gqv @agent-resurrect-dir)"/bin/tmux-agent-resurrect-setup --remove
```

OpenCode 2's background server keeps a plugin it loaded from
`OPENCODE_CONFIG_CONTENT` until it restarts; after removing the plugin, run
`opencode service restart` in a new pane.

#### TPM

Remove the `@plugin` line and press prefix + alt + u.

#### Oh my tmux!

Remove the `@plugin` line from `.tmux.conf.local` and press prefix + alt + u.

#### Manual

Remove the `run-shell` line and delete the clone.

#### Nix

Remove the packages from your configuration and switch to it.

## Usage

Authenticate the agents before saving sessions. Restored panes must use a
Bourne-compatible shell (`bash`, `zsh`, `sh`, `dash`, or `ksh`).

### Saving and restoring

Use your normal tmux-resurrect save and restore bindings (by default,
prefix + Ctrl-s and prefix + Ctrl-r). During a save, the integration records
exact session mappings and removes those panes from resurrect's generic process
restart. During restore, it prepares commands without launching detached sessions.
An already attached session resumes when restore finishes; other sessions resume
on the first attachment or client switch. Later attachments do not relaunch them.

The plugin reports problems in tmux's message line: for example, when a save
finds Claude running in a pane without a recorded session ID, or a restore
cannot resume a saved session. A session whose directory does not exist, as on
a drive that is not mounted yet, resumes on an attach once it does; close its
pane to discard it.

### Claude Code

Claude Code sessions resume with `claude --resume SESSION_ID`. For a named
worktree at `<repo>/.claude/worktrees/<name>`, the command is run from `<repo>`
and adds `--worktree NAME`. Other sessions use the latest valid absolute `cwd`
in their transcript, falling back to the pane's directory if there is no usable
transcript cwd.

The command repeats the options Claude was started with that resuming does not
restore, such as `--add-dir`, `--mcp-config` and `--settings` files, and tool
lists, but not the prompt. On macOS, the options are read from `ps`, which
joins arguments with spaces, so options after a system prompt, or after another
value with spaces that isn't an existing path, are not repeated.

Resuming doesn't restore the model or effort level either, so a resumed session
would get the default ones, and the default level can't be max. The command
passes the model and effort level the session last used with `--model` and
`--effort`, leaving out those a skill sets for its own turn, and
`--effort ultracode` if ultracode was on. When the transcript doesn't record
them, it repeats the ones Claude was started with. Claude restores the
permission mode itself, except bypassing permissions, so the command adds
`--dangerously-skip-permissions` if the session last bypassed them.
[Harness resume](#harness-resume) turns these off.

### OpenCode

OpenCode sessions resume with `opencode -s SESSION_ID`, run from the session's
directory. OpenCode 2 records the session a pane shows as soon as it shows it,
and while the pane shows none, a save keeps the one it showed last. OpenCode 1
reports a session to the plugin at its first prompt, so a session that has had
no prompt is not saved, and a session you switch to is recorded at its next
prompt; until then, a save keeps the session the pane last prompted.

## Configuration

### Snapshot directory

`@resurrect-dir` sets the snapshot directory. The default is `~/.tmux/resurrect`
if it exists, otherwise `${XDG_DATA_HOME:-$HOME/.local/share}/tmux/resurrect`.

As in tmux-resurrect, `~`, `$HOME`, and `$HOSTNAME` are expanded anywhere in the
path, and no other variables are. Avoid spaces in snapshot directories because
tmux-resurrect does not handle them reliably. Project directories may contain
spaces.

### Existing resurrect callbacks

The plugin keeps existing resurrect callbacks and tmux attach hooks, and
reloading the configuration does not add duplicates. To change or remove a
callback that was set before the plugin loaded, follow the
[troubleshooting steps](docs/troubleshooting.md#replacing-or-removing-a-shared-hook).

### Claude configuration directory

Each Claude session resumes with the `CLAUDE_CONFIG_DIR` it ran with, so panes
can use different configuration directories. Use an absolute path; a session
with a relative one resumes with the `CLAUDE_CONFIG_DIR` in tmux's global
environment, where the save and restore callbacks run.

### Harness resume

By default, a Claude session resumes with the model, effort level, ultracode,
and permission bypass it last used, which `--resume` doesn't restore. To resume
sessions as Claude does, with only the options they were started with, set:

```tmux
set -g @agent-resurrect-harness-resume 'on'
```

The option applies to sessions saved after it is set.

## Documentation

- [Design](docs/design.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Compatibility and limitations](docs/compatibility.md)
- [Contributing](CONTRIBUTING.md)
