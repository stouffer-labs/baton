# Baton

> Pass the baton between machines — a terminal menu to list and resume your
> `claude` and `codex` CLI sessions, grouped by project, with a live preview.

[![CI](https://github.com/stouffer-labs/baton/actions/workflows/ci.yml/badge.svg)](https://github.com/stouffer-labs/baton/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

`baton` scans the session transcripts that Claude Code and Codex CLI already
write to disk, shows them in an `fzf` picker grouped by the directory each was
started in, and resumes the one you pick **natively** in your terminal
(`claude --resume <id>` / `codex resume <id>`). No tmux, no multiplexer — so
mouse selection, copy/paste, and scrolling stay fully native on both macOS and
Linux, locally or over SSH.

The original goal — and the name: start a session on a Mac, then **hand it off**
to another machine over SSH (or vice-versa). Because both tools persist each
conversation to a local file keyed by a session id, "shadowing" becomes "resume
the same conversation wherever you are."

## Install

### macOS / Linux (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/stouffer-labs/baton/main/scripts/install.sh | bash
```

Copies the script to `~/.local/share/baton`, links `baton` into `~/.local/bin`
(plus an `agents` alias), and is **self-contained** afterward (no checkout
required). Re-run any time to update.

The installer also **wires shell integration** automatically: it adds a managed,
idempotent block (`eval "$(baton shell-init …)"`) to each of `~/.zshrc`,
`~/.bashrc`, `~/.bash_profile` that exists, so `baton` becomes a shell function.
That's what lets baton leave you **in the session's project directory** after the
session ends (see [Usage](#usage)). Open a new terminal (or `source` your rc) once
after installing. Pass `--no-modify-rc` to skip this — baton still resumes, it
just can't move your shell; the line to add yourself is printed instead.

Install from a local checkout instead with
`scripts/install.sh --from-source PATH`. Override locations with
`BATON_INSTALL_DIR` / `BATON_BIN_DIR`, or the rc target with `BATON_RC_FILE`.

### Requirements

- `python3` (transcript parsing)
- `fzf` (the menu) — `brew install fzf` or `apt install fzf`
- `claude` and/or `codex` on `PATH`

If `~/.local/bin` isn't on your `PATH`, add:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

## Usage

```bash
baton          # active sessions + the 10 most recent closed ones
baton --all    # every recent session (up to a cap)
```

From another machine: `ssh -t <host> baton` (the `-t` allocates a terminal).

In the menu:

- **type to filter**, arrow keys to move
- **Enter** — resume the highlighted session (cd's to its original directory first)
- **Esc** — quit
- The right pane previews the session as a clean Q&A transcript (your questions
  and the AI's answers — no thinking blocks, tool calls, or narration).

A `●` marks a session with a **live** process. Resuming a live session prompts to
end that process first, so the same conversation isn't driven from two places at
once.

### You land in the project directory afterward

With shell integration installed (the default — see [Install](#install)), `baton`
runs as a shell function: it `cd`s your shell into the picked session's directory,
resumes, and when the session ends **your shell is still in that directory**. No
more getting dropped back at `~` and having to re-navigate to resume again — just
run `baton` again, or `claude --resume` right where you are.

This needs a shell function because a normal command is a *child process* and a
child can't change its parent shell's directory; only code running **in** your
shell can. The installer wires that one line for you. Without the integration
(e.g. `ssh -t host baton` on a box that wasn't set up, or after `--no-modify-rc`),
baton still lists and resumes — it just can't move your shell, so you land back
where you started.

> The `agents` command is installed as an alias for `baton` (a matching `agents`
> shell function is defined too), so existing muscle memory keeps working.

> **Note:** if you `Ctrl-Z` (suspend) *during* a resumed session, the shell may
> leave it as a stopped background job rather than returning cleanly — you're
> still left in the right directory. Normal exit is unaffected.

## How it works

- **Session discovery:** reads `~/.claude/projects/<slug>/<id>.jsonl` and
  `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.
- **Original directory:** recovered from the transcript (`cwd`). Resume is
  directory-scoped — both CLIs only find a session id when run from where it
  started — so `baton` cd's there before resuming.
- **Live detection:** finds running `claude`/`codex` leaf processes and maps each
  to its working directory (`/proc/<pid>/cwd` on Linux, `lsof` on macOS), then
  matches that to a session group.

## Caveats

- This is a **handoff** model: resume in one place at a time. Driving the same
  session simultaneously on two machines would diverge the conversation.
- Live detection by working directory can't distinguish two processes started in
  the *same* directory; it marks the N newest there as active (N = process count).
- Session files live on local disk, so resuming from another machine works when
  you SSH **into** the host that holds them.

## Contributing

Contributions are welcome! See the
[Contributing Guide](https://github.com/stouffer-labs/.github/blob/main/CONTRIBUTING.md)
for details.

## License

This project is licensed under the Apache License 2.0 — see [LICENSE](LICENSE)
for details.
