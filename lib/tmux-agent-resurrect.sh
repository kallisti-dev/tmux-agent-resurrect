# Shared by the entry points; Bash 3.2 or newer.
# shellcheck shell=bash
# SPDX-License-Identifier: BSD-3-Clause

# The plugin's bin directory, as an absolute path.
plugin_bin=$(cd -- "${BASH_SOURCE[0]%/*}/../bin" && pwd)

# Usage: warn MESSAGE...
# Print MESSAGE on stderr, prefixed with the project name.
warn() { printf 'tmux-agent-resurrect: %s\n' "$*" >&2; }

# Usage: guarded QUOTED_PATH
# Print a shell command that runs QUOTED_PATH, a path already quoted for the
# shell, with any arguments appended to the command. Once the path no longer
# exists, the command succeeds without running anything: tmux keeps hook
# registrations until its server exits, and runs delayed commands later, even
# after a plugin manager deletes this directory.
guarded() { printf '[ ! -e %s ] || %s' "$1" "$1"; }

# Usage: notify MESSAGE...
# Warn with MESSAGE, then show it on attached tmux clients a second later:
# tmux-resurrect shows its own message after running hooks, which would
# replace an immediate one.
notify() {
  local command
  warn "$*"
  # tmux shows a delayed command's failure in view mode in the current pane,
  # where resumes wait.
  command="$(guarded "$(quote "$plugin_bin/tmux-agent-resurrect-notify")") $(quote "$*")"
  # run-shell expands formats in its command.
  tmux run-shell -b -d 1 "${command//#/##}" 2>/dev/null || true
}

# Usage: show_notice MESSAGE
# Show MESSAGE on every attached tmux client. With none attached, keep it for
# the next client that attaches.
show_notice() {
  local message="tmux-agent-resurrect: $1" client shown=0 kept
  while IFS= read -r client; do
    [[ -n $client ]] || continue
    # display-message expands formats in its message.
    tmux display-message -c "$client" -d 5000 "${message//#/##}" 2>/dev/null && shown=1
  done < <(tmux list-clients -F '#{client_name}' 2>/dev/null)
  if [[ $shown == 0 ]]; then
    kept=$(tmux show-options -gqv @agent-resurrect-notices)
    tmux set-option -g @agent-resurrect-notices "${kept:+$kept; }$1"
  fi
}

# Usage: show_kept_notices CLIENT
# Show messages kept while no client was attached on CLIENT.
show_kept_notices() {
  local kept
  kept=$(tmux show-options -gqv @agent-resurrect-notices)
  [[ -n $kept ]] || return 0
  tmux set-option -gu @agent-resurrect-notices
  kept="tmux-agent-resurrect: $kept"
  tmux display-message -c "$1" -d 5000 "${kept//#/##}" 2>/dev/null || true
}

# Usage: summarize ITEM...
# Print up to three ITEMs separated by commas, then how many others there are.
summarize() {
  local text='' item shown=0
  for item in "$@"; do
    if [[ $shown == 3 ]]; then
      text+=" and $(($# - 3)) more"
      break
    fi
    text+=${text:+, }$item
    shown=$((shown + 1))
  done
  printf '%s' "$text"
}

# Usage: die MESSAGE...
# Print MESSAGE as warn does and exit with status 1. Inside $(...), this exits
# only that subshell.
die() {
  warn "$*"
  exit 1
}

# Usage: need COMMAND...
# Exit with a diagnostic unless every COMMAND is on PATH.
need() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || die "required command not found on PATH: $tool"
  done
}

# Exit unless tmux and jq are on PATH and TMUX is set. Entry points call this
# before using tmux, so that their tmux commands reach the enclosing server
# rather than the default socket.
require_tmux() {
  need tmux jq
  [[ -n ${TMUX:-} ]] || die 'run inside tmux (TMUX must identify the intended server)'
}

# Usage: plugin_version DIR
# Print the version in the plugin manifest under DIR, or nothing.
plugin_version() { jq -r '.version // empty' "$1/.claude-plugin/plugin.json" 2>/dev/null || true; }

# Usage: at_least VERSION MINIMUM
# Succeed if the dotted VERSION is MINIMUM or later. Characters other than
# digits and dots, as in tmux's 3.4a or next-3.6, are ignored.
at_least() {
  local i have=() need=()
  IFS=. read -ra have <<<"${1//[!0-9.]/}"
  IFS=. read -ra need <<<"$2"
  for i in 0 1 2; do
    ((10#${have[i]:-0} > 10#${need[i]:-0})) && return 0
    ((10#${have[i]:-0} < 10#${need[i]:-0})) && return 1
  done
  return 0
}

# Usage: pane_option PANE OPTION
# Print PANE's own value of OPTION, or nothing if it is unset.
pane_option() { tmux show-options -pqv -t "$1" "$2"; }

# The agents whose sessions are restored, as named in pane options and
# snapshot records.
agents='claude opencode'

# Usage: agent_label AGENT
# Print AGENT's name for messages.
agent_label() {
  case $1 in
    claude) printf 'Claude' ;;
    opencode) printf 'OpenCode' ;;
    *) printf '%s' "$1" ;;
  esac
}

# Usage: reports_late AGENT
# Succeed if AGENT reports a session only once the user sends a prompt, and
# may not report its end. OpenCode creates a session at the first prompt,
# reopening one with -s reports nothing until the next prompt, and nothing
# fires when it exits uncleanly.
reports_late() { [[ $1 == opencode ]]; }

# Usage: agent_of_command COMMAND
# Print the agent that a pane's #{pane_current_command} shows is running, or
# nothing. Nix wrappers run a binary named .NAME-wrapped.
agent_of_command() {
  local agent
  for agent in $agents; do
    if [[ $1 =~ (^|/)\.?$agent($|[-.\ ]) ]]; then
      printf '%s' "$agent"
      return
    fi
  done
}

# Usage: agent_of_pane COMMAND FULL_COMMAND
# Print the agent that a pane's foreground process runs, or nothing, from its
# #{pane_current_command} or else its command line, with arguments separated
# by spaces: the program it was started as, or the script that an interpreter
# such as node runs. macOS names a process after the file that a symlink
# resolves to, such as versions/2.1.300 for Claude's native installer.
agent_of_pane() {
  local agent words=()
  agent=$(agent_of_command "$1")
  if [[ -z $agent ]]; then
    read -ra words <<<"${2:-}"
    if [[ ${#words[@]} -gt 0 ]]; then
      agent=$(agent_of_command "${words[0]##*/}")
    fi
    if [[ -z $agent && ${#words[@]} -gt 1 && ${words[0]##*/} =~ ^(node|bun|deno)$ ]]; then
      agent=$(agent_of_command "${words[1]##*/}")
    fi
  fi
  printf '%s' "$agent"
}

# Usage: read_args PID
# Set the array process_args to the command line of process PID, and
# process_args_exact to 1 if each element is exactly one of its arguments.
# Linux keeps them apart in /proc; elsewhere ps joins them with spaces, so
# they are split at spaces and an argument that contains one becomes several.
# Callers read process_args_exact.
# shellcheck disable=SC2034
read_args() {
  local arg
  process_args=() process_args_exact=0
  if [[ -r /proc/$1/cmdline ]]; then
    while IFS= read -r -d '' arg; do
      process_args+=("$arg")
    done <"/proc/$1/cmdline"
    process_args_exact=1
  else
    read -ra process_args <<<"$(ps -o args= -p "$1" 2>/dev/null || true)"
  fi
}

# Usage: foreground_command PID
# Print the command line, with arguments separated by spaces, of the
# foreground process on the terminal of process PID, such as a pane's shell.
foreground_command() {
  local stat fields group
  if [[ -r /proc/$1/stat ]]; then
    # The fields after the parenthesized name are state, ppid, pgrp, session,
    # tty_nr, and tpgid.
    stat=$(<"/proc/$1/stat")
    read -ra fields <<<"${stat##*)}"
    group=${fields[5]:-}
  else
    group=$(ps -o tpgid= -p "$1" 2>/dev/null || true)
    group=${group//[[:space:]]/}
  fi
  [[ $group =~ ^[0-9]+$ ]] || return 0
  read_args "$group"
  printf '%s' "${process_args[*]:-}"
}

# Usage: valid_id ID
# Succeed if ID has the form of an agent session ID: 1 to 128 letters, digits,
# underscores, or hyphens, starting with a letter or digit. valid_record and
# tmux-agent-resurrect-claude-hook repeat this pattern in jq.
valid_id() { [[ $1 =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$ ]]; }

# Usage: pane_session PANE
# Print the session recorded on PANE as AGENT ID, or nothing.
pane_session() { pane_option "$1" @agent-resurrect-session; }

# Usage: record_session PANE AGENT ID [DIRECTORY [LAUNCH]]
# Record that PANE runs AGENT's session ID, in DIRECTORY if given, replacing
# any pending resume. LAUNCH, if given, is the JSON object, describing how the
# agent runs, from which save builds the session's record. Both are set first,
# so a reader that sees the session also sees them.
record_session() {
  if [[ -n ${4:-} ]]; then
    tmux set-option -p -t "$1" @agent-resurrect-cwd "$4"
  else
    tmux set-option -pu -t "$1" @agent-resurrect-cwd
  fi
  if [[ -n ${5:-} ]]; then
    tmux set-option -p -t "$1" @agent-resurrect-launch "$5"
  else
    tmux set-option -pu -t "$1" @agent-resurrect-launch
  fi
  tmux set-option -p -t "$1" @agent-resurrect-session "$2 $3"
  tmux set-option -pu -t "$1" @agent-resurrect-resume-cmd
  tmux set-option -pu -t "$1" @agent-resurrect-resume-data
}

# Usage: end_session PANE AGENT ID
# Clear PANE's recorded session if it is still AGENT's ID. tmux compares and
# clears in one command, so a newer session that has started meanwhile stays.
end_session() {
  tmux if-shell -F -t "$1" "#{==:#{@agent-resurrect-session},$2 $3}" \
    "set-option -pu -t $1 @agent-resurrect-session ; set-option -pu -t $1 @agent-resurrect-cwd ; set-option -pu -t $1 @agent-resurrect-launch"
}

# Usage: quote STRING
# Print STRING as one single-quoted shell word, writing each ' as '\''. Bash
# 3.2 handles quotes in the replacement of a double-quoted
# ${var//pattern/replacement} differently from later versions, so the
# replacement is kept in a variable.
quote() {
  local q="'\\''"
  printf "'%s'" "${1//\'/$q}"
}

# Usage: projects_dir [CONFIG_DIR]
# Print the directory that holds Claude's transcripts under the Claude
# configuration directory CONFIG_DIR, by default CLAUDE_CONFIG_DIR or
# ~/.claude.
projects_dir() { printf '%s/projects\n' "${1:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}"; }

# Usage: claude_projects_dir JSON
# Print projects_dir for the Claude session that JSON, a snapshot record or a
# pane's @agent-resurrect-launch, describes. Its config_dir is the session's
# CLAUDE_CONFIG_DIR, empty when that was unset. A record has none when the
# session's was relative, and the tmux server's CLAUDE_CONFIG_DIR stands in.
claude_projects_dir() {
  local config
  if config=$(jq -er '.config_dir | strings' <<<"$1" 2>/dev/null); then
    projects_dir "${config:-$HOME/.claude}"
  else
    projects_dir
  fi
}

# Usage: find_transcript SESSION_ID [PROJECTS_DIR]
# Print the path of the session's transcript: the only readable
# SESSION_ID.jsonl in a project directory under PROJECTS_DIR, by default
# projects_dir. Fail silently if SESSION_ID is invalid or has no transcript;
# warn and fail if several projects have one.
find_transcript() {
  local candidate found=''
  valid_id "$1" || return 1
  for candidate in "${2:-$(projects_dir)}"/*/"$1".jsonl; do
    [[ -f $candidate && -r $candidate ]] || continue
    if [[ -n $found ]]; then
      warn "ambiguous transcript for session $1; skipping"
      return 1
    fi
    found=$candidate
  done
  [[ -n $found ]] || return 1
  printf '%s\n' "$found"
}

# Usage: valid_record JSON
# Succeed if JSON is exactly one saved pane record, in the format described
# under "Snapshot records" in docs/design.md. Validate before decoding fields
# into shell variables. Control characters are incompatible with
# tmux-resurrect's line/tab-oriented snapshot format.
valid_record() {
  jq -se --arg agents "$agents" '
    def text: type == "string" and length > 0 and
      (test("[\u0000-\u001f\u007f]") | not);
    length == 1 and (.[0] |
    type == "object" and .version == 1 and
    (.agent | IN($agents | split(" ")[])) and
    (.session | text) and (.session | test("[:.]") | not) and
    (.window | type == "string" and test("^[0-9]+\\z")) and
    (.pane | type == "string" and test("^[0-9]+\\z")) and
    (.session_id | type == "string" and test("^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\\z")) and
    (.cwd | text and startswith("/")) and
    (.worktree | type == "string" and (test("[\u0000-\u001f\u007f/]") | not)) and
    (.agent == "claude" or .worktree == "") and
    ((has("config_dir") | not) or
      (.agent == "claude" and (.config_dir == "" or (.config_dir | text and startswith("/"))))) and
    ((has("options") | not) or
      (.agent == "claude" and (.options | type == "array" and
        all(.[]; type == "string" and (test("[\u0000-\u001f\u007f]") | not))))))
  ' >/dev/null 2>&1 <<<"$1"
}

# Usage: record_target RECORD
# Print the tmux target =SESSION:WINDOW.PANE for a record that passed
# valid_record. The = prefix allows only an exact session-name match.
record_target() {
  jq -r '"=" + .session + ":" + .window + "." + .pane' <<<"$1"
}

# Usage: worktree_name DIR
# Print NAME if DIR is REPO/.claude/worktrees/NAME or a directory below it;
# otherwise print nothing.
worktree_name() {
  local name=''
  case "$1" in
    */.claude/worktrees/*)
      name=${1#*/.claude/worktrees/}
      name=${name%%/*}
      ;;
  esac
  printf '%s' "$name"
}

# Usage: resume_command RECORD
# Print the command line that resumes RECORD's conversation from a pane's
# shell. Warn and fail without output if the record is invalid or
# inconsistent, or its session cannot be resumed. Fail with status 2 instead
# while a directory it needs does not exist, as when it is on a volume that is
# not mounted yet; the warning then names that directory, after "does not
# exist: ". Deliberately no picker fallback: restoration must identify one
# exact session.
resume_command() {
  local record=$1 agent
  valid_record "$record" || {
    warn 'invalid session metadata; skipping'
    return 1
  }
  agent=$(jq -r '.agent' <<<"$record")
  case $agent in
    claude) claude_resume_command "$record" ;;
    opencode) opencode_resume_command "$record" ;;
  esac
}

# Usage: opencode_resume_command RECORD
# For a valid OpenCode record: cd to the session's directory and run
# opencode -s SESSION_ID. Fail with status 2 if the directory does not exist.
opencode_resume_command() {
  local dir id
  dir=$(jq -r '.cwd' <<<"$1")
  id=$(jq -r '.session_id' <<<"$1")
  [[ -d $dir ]] || {
    warn "saved directory does not exist: $dir; waiting"
    return 2
  }
  printf 'cd -- %s && opencode -s %s' "$(quote "$dir")" "$(quote "$id")"
}

# Usage: claude_resume_command RECORD
# For a valid Claude record: cd to the saved directory and run
# claude --resume SESSION_ID, or, for a named worktree, cd to the main
# repository and add --worktree NAME, with the session's CLAUDE_CONFIG_DIR if
# it had one and the options it was started with that a resume repeats. Fail
# if its transcript is missing, ambiguous, or empty, or its
# worktree no longer exists. Fail with status 2 if its directory, the
# worktree's main repository, or its configuration directory does not exist.
claude_resume_command() {
  local record=$1 dir id worktree projects transcript root config claude=claude option options=''
  dir=$(jq -r '.cwd' <<<"$record")
  id=$(jq -r '.session_id' <<<"$record")
  worktree=$(jq -r '.worktree' <<<"$record")
  config=$(jq -r '.config_dir // ""' <<<"$record")
  [[ -z $config ]] || claude="CLAUDE_CONFIG_DIR=$(quote "$config") claude"
  while IFS= read -r option; do
    options+=" $(quote "$option")"
  done < <(jq -r '.options // [] | .[]' <<<"$record")
  [[ $worktree == "$(worktree_name "$dir")" ]] || {
    warn "inconsistent worktree metadata for $id; skipping"
    return 1
  }
  [[ $worktree != . && $worktree != .. && $worktree != -* ]] || {
    warn 'invalid worktree name; skipping'
    return 1
  }
  projects=$(claude_projects_dir "$record")
  [[ -d $projects ]] || {
    warn "Claude configuration directory does not exist: ${projects%/projects}; waiting"
    return 2
  }
  transcript=$(find_transcript "$id" "$projects") || {
    warn "missing or ambiguous transcript for $id; skipping"
    return 1
  }
  [[ -s $transcript ]] || {
    warn "empty transcript for $id; skipping"
    return 1
  }
  if [[ -n $worktree ]]; then
    root=${dir%%/.claude/worktrees/*}
    [[ -d $root ]] || {
      warn "main repository does not exist: $root; waiting"
      return 2
    }
    # Worktrees are routinely removed, so one missing from an existing
    # repository is gone.
    [[ -d $dir ]] || {
      warn "worktree no longer exists: $dir; skipping"
      return 1
    }
    printf 'cd -- %s && %s --resume %s --worktree %s%s' "$(quote "$root")" "$claude" "$(quote "$id")" "$(quote "$worktree")" "$options"
  else
    [[ -d $dir ]] || {
      warn "saved directory does not exist: $dir; waiting"
      return 2
    }
    printf 'cd -- %s && %s --resume %s%s' "$(quote "$dir")" "$claude" "$(quote "$id")" "$options"
  fi
}

# Print the path of tmux-resurrect's link to its latest snapshot. The directory
# follows resurrect's defaults: @resurrect-dir, else ~/.tmux/resurrect if it
# exists, else the XDG data directory. As resurrect does, replace every $HOME,
# then $HOSTNAME, then ~; a directory still relative exits through die.
last_snapshot() {
  local dir host=''
  dir=$(tmux show-options -gqv @resurrect-dir)
  if [[ -z $dir ]]; then
    if [[ -d $HOME/.tmux/resurrect ]]; then
      dir=$HOME/.tmux/resurrect
    else
      dir=${XDG_DATA_HOME:-$HOME/.local/share}/tmux/resurrect
    fi
  fi
  dir=${dir//\$HOME/"$HOME"}
  if [[ $dir == *\$HOSTNAME* ]]; then
    host=$(hostname 2>/dev/null) || host=''
  fi
  dir=${dir//\$HOSTNAME/"$host"}
  dir=${dir//\~/"$HOME"}
  [[ $dir == /* ]] || die "@resurrect-dir must be an absolute path"
  printf '%s/last\n' "$dir"
}
