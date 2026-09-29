# Baton

> A terminal menu that lists your `claude` and `codex` CLI sessions by project and resumes the one you pick. Start a session on one machine and pick it up on another over SSH.

[![CI](https://github.com/stouffer-labs/baton/actions/workflows/ci.yml/badge.svg)](https://github.com/stouffer-labs/baton/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

`baton` reads the session transcripts that Claude Code and Codex CLI already write to disk. It shows them in an `fzf` picker grouped by the folder each one was started in. When you pick one it resumes it natively in your terminal with `claude --resume <id>` or `codex resume <id>`. There's no tmux or other multiplexer involved. Mouse selection, copy and paste and scrolling all stay native on macOS and Linux. That holds over SSH too.

The name comes from the original goal. You start a session on a Mac and then hand it off to another machine over SSH, or the other way around. Both tools save each conversation to a local file keyed by a session id, so handing off just means resuming the same conversation wherever you are.

## Install

### macOS / Linux (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/stouffer-labs/baton/main/scripts/install.sh | bash
```

This copies the script to `~/.local/share/baton` and links `baton` into `~/.local/bin`, plus an `agents` alias. After that it doesn't need the checkout. Re-run it any time to update.

The installer also wires up shell integration. It adds a managed block (`eval "$(baton shell-init …)"`) to each of `~/.zshrc`, `~/.bashrc` and `~/.bash_profile` that exists. Running it again doesn't duplicate the block. That block makes `baton` a shell function, which is what lets baton leave you in the session's project folder after the session ends (see [Usage](#usage)). Open a new terminal or `source` your rc once after installing. Pass `--no-modify-rc` to skip this. Baton still resumes sessions without it but can't move your shell. The installer then prints the line to add yourself.

To install from a local checkout use `scripts/install.sh --from-source PATH`. You can override the locations with `BATON_INSTALL_DIR` and `BATON_BIN_DIR`, and the rc file with `BATON_RC_FILE`.

### Requirements

- `python3` 3.8 or newer, for reading the transcripts
- `fzf` 0.74 or newer, for the menu (`brew install fzf` or your package manager)
- `claude` and/or `codex` on your `PATH`
- `ripgrep` (`rg`) is optional. It powers searching inside conversations.

If `~/.local/bin` isn't on your `PATH`, add it:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

## Usage

```bash
baton                      # your sessions, live ones first, sized to the terminal
baton --tool claude        # start filtered to claude (or codex, or both)
baton --all                # start with automated sessions shown too
baton --stats              # how many sessions of each kind baton found
```

From another machine run `ssh -t <host> baton`. The `-t` gives it a terminal.

### Which sessions are listed

Baton lists the sessions you actually drove yourself. It hides the same things `claude --resume` and `codex resume` hide.

- **Automated runs** are hidden. That covers Agent SDK sessions and `claude -p` runs on the Claude side, and `codex exec` runs on the Codex side. Other tools and hooks that drive Claude or Codex start these, and on a busy machine there are thousands of them. Press **Ctrl-A** to show them. They're tagged, for example `[sdk-py]` or `[exec]`.
- **Subagents** are folded under the session that started them. That means Claude Task-tool agents and Codex spawned or guardian threads. A parent row shows how many it has, like `⤷5`. Press **→** to unfold them and **←** to fold them again. Pressing Enter on a subagent resumes its parent.
- **Continued sessions** are hidden when Claude has moved the conversation on into a newer session file. Ctrl-A shows them tagged `[cont.]`.

Each session is titled the way the native pickers title it. For Claude it uses your `/rename` name if you set one. Otherwise it falls back through Claude's generated title and your last prompt to your first real prompt. For Codex it's the thread name, and if there isn't one it's your first real message.

### Keys

- **Type to search.** One search box matches session titles and prompts, plus branches and folders. It also searches the text of the conversations. Results stay grouped under their folders. Searching inside conversations needs ripgrep.
- **Enter** resumes the highlighted session. Baton changes to the session's original folder first.
- **`+`** (or **`=`**) expands the highlighted folder to show all of its sessions. Press it again to collapse. While you're typing they're just characters, so you can search for `a=b` or `C++`.
- **→ / ←** unfold and fold the highlighted session's subagents. While you're typing they move the cursor.
- **Ctrl-A** shows or hides automated and continued sessions. The prompt reads `both+all>` while they're shown.
- **Ctrl-T** cycles the tool filter from both to claude to codex. The prompt shows the current filter.
- **Esc** clears the search, or quits when the search is empty.

The top line of the list says how many sessions are hidden. The right pane starts with a short description card for the highlighted session. It shows the title and whether the session is running. It also has the branch and folder, and when it started and last changed. Then come your first and last ask and its subagents. Below the card is a readable transcript of the latest turns. It has your questions and the AI's answers with `You:` and `Claude:` or `Codex:` labels. Thinking, tool calls and the narration between tool calls are left out.

### Default view

With an empty search box the list pins every live session, marked `●`. A yellow dot means it's busy and a green one means it's idle. The folder you started baton from always comes first, marked `· here`, so you can see straight away what you last ran there and whether it was claude or codex. That holds even when its sessions are older than everything else, and when nothing was ever run there the header says so. Live sessions come next. Then it fills the rest of the terminal with your most recent sessions, grouped under their folders with the newest first. A folder with more history shows a `(+N older)` hint, and `+` opens it up. To reach an old project that isn't shown, type its name.

### Live sessions

Resuming a session that is still running somewhere would let the conversation split into two. So when you pick a live session baton offers to end that process first. It only does that when it can prove which process holds the session.

- For Claude it uses Claude's own registry of running sessions in `~/.claude/sessions/`. It checks that the process is alive and started at the recorded time, and that it's really `claude`. It checks all of that again every time you press Enter and right before ending anything, and it only resumes once the old process has actually exited.
- For Codex it looks for the process that holds the session's lock file in `~/.codex/thread-writer-locks/`.
- When a Codex process is running in a session's folder but baton can't tell which session it holds, the row gets a dim `◌`. Baton only warns you in that case and never ends the process.

### You land in the project folder afterward

Shell integration is installed by default, and with it `baton` runs as a shell function. It changes your shell into the session's folder and resumes. When the session ends your shell is still in that folder, so you can run `baton` again or `claude --resume` right there.

This needs a shell function because a normal command runs as a child process. A child can't change its parent shell's folder, and only code running inside your shell can. The installer wires up that one line for you. Without it baton still lists and resumes sessions. That happens with `ssh -t host baton` on a machine that wasn't set up, for example. It just can't move your shell.

> The `agents` command is installed as an alias for `baton` and gets a matching shell function too. Old muscle memory keeps working.

> **Note:** if you suspend with Ctrl-Z during a resumed session, the shell may keep it as a stopped background job instead of returning cleanly. You're still left in the right folder, and a normal exit isn't affected.

## How it works

- **Finding sessions.** Claude sessions come from `~/.claude/projects/<project>/<id>.jsonl`, and their subagents from `<id>/subagents/`. Codex sessions come from Codex's own state database in `~/.codex/state_N.sqlite`, opened read-only. If that database is missing or has a layout baton doesn't know, baton reads the rollout files in `~/.codex/sessions/` instead and shows a warning. In that mode it can't tell which sessions you archived.
- **Reading them quickly.** Like `claude --resume`, baton reads only the first and last 64 KB of each Claude transcript. What it learns about each file is cached in `~/.cache/baton/index.json` (readable only by you), so later launches only re-read files that changed.
- **Original folder.** Resume only finds a session id when it runs from the folder the session started in, so baton changes to that folder before resuming.
- **Safety.** Titles and transcript text come from files that other programs wrote. Baton strips terminal escape sequences out of them before showing anything.

Run `python3 -m unittest discover -s tests` to run the test suite. It builds fake Claude and Codex homes under `tmp/` and never touches your real sessions.

## Caveats

- This is a handoff model. Resume a session in one place at a time. Driving the same session on two machines at once would split the conversation.
- The session files live on local disk, so resuming from another machine works when you SSH into the machine that holds them.

## Contributing

Contributions are welcome. See the [Contributing Guide](https://github.com/stouffer-labs/.github/blob/main/CONTRIBUTING.md) for details.

## License

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.
