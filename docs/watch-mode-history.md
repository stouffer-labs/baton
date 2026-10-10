# Watch mode: how it was built

**Date:** 2026-10-09
**Status:** Shipped. The local commits are `32a1646` (spec), `d69e86e` (code and tests) and `4d9b6f0` (README). They were published to `stouffer-labs/baton` as `66f0d29`, and CI passed on Ubuntu and macOS.

This is the record of how Ctrl-O watch mode was added. It covers the research and the decisions first. Then it covers the code, the reviews and the testing. The user-facing instructions are in the README under "Watching a session without taking it over".

## The goal

Eric often SSHes into his Mac from a Windows PC and runs baton to check on a session that's running in a Terminal.app window. Resume was the only action. It offers to end the local process and continue the conversation in the SSH terminal. That's wrong when the session is doing GUI work on the Mac, like driving a browser. The copy in the SSH terminal can't reach the Mac's screen, and the local process is gone.

He wanted a way to watch such a session from the baton menu without any way to control it. It had to work for both Claude Code and Codex.

## Using it

- In the baton menu, press **Ctrl-O** on a session or subagent row.
- The view starts with the activity since your second-to-last prompt. It then streams new activity as it lands. That means prompts and replies plus one line per tool call as it starts. A failure gets a line of its own.
- The last line is a status line. It shows whether the session is busy, idle or waiting. For Claude it also says what the session is waiting on. It names the running tool and since when, and it says when the session ends.
- Press **q** (or Esc or Ctrl-C) to go back to the list where you left it.
- From another machine run `ssh -t <mac> baton` as usual.

## What was checked first

Every one of these was measured on Eric's Mac or read from docs, source code or the installed binaries before any design work. The versions were Claude Code 2.1.295, Codex 0.162 and fzf 0.74.1.

- **Neither CLI has a passive viewer.** `claude attach` and `claude logs` only work on background (`--bg`) sessions. Claude's Remote Control is a control channel that needs claude.ai sign-in. Codex's `--remote` and app-server clients can send input, so they're control channels too.
- **A true screen copy isn't practical without a multiplexer.** Baton's predecessor watched sessions through tmux (see `design-history.md`). That was dropped to keep mouse, scrollback and copy native. Terminal.app's AppleScript can return a tab's visible text. From an SSH login macOS asks for an Automation permission that nobody at the Mac is there to grant, so that route was set aside.
- **Claude writes its transcript within seconds.** A running tool call reached the JSONL file (one JSON record per line) about 3 to 5 seconds after its own timestamp. One sample measured 3.2 seconds.
- **Claude's live registry has the status.** Each running session has `~/.claude/sessions/<pid>.json`. Its `status` says whether the session is busy, idle or waiting. The `waitingFor` field names what it waits on, such as a permission prompt. The `sessionId` changes on `/clear`, and a `parkedJobId` appears when the session moves to the background.
- **Codex writes each rollout line at once.** Its recorder writes and flushes every line when the event is recorded. A tool call is written before the tool runs and its outcome when it finishes. `task_started` and `task_complete` mark busy and idle.
- **Codex never writes approval prompts to disk.** A pending approval looks the same as a long-running tool.
- **Codex repeats every message.** Each one appears as a `response_item` and again as an `item_completed`. The completion's `item.id` equals the tool call's `call_id` (650 of 650 locally).
- **fzf hands over the terminal cleanly.** `execute(...)` leaves fzf's alternate screen before running the command. The real picker's output shows `ESC[?1049l` before the viewer starts, so the feed lands in the normal screen and stays in scrollback. fzf comes back with the search text and cursor where they were.
- **Ctrl-O was free.** fzf, Windows Terminal and Terminal.app don't bind it.
- **Reading can't disturb the writer.** Opening a file read-only takes no lock on macOS or Linux.

## Decisions

Eric chose these on 2026-10-09:

- Ctrl-O in the menu is the only way in. There's no watch-only menu and no new choice at the LIVE prompt.
- The feed shows activity. That's prompts and replies plus tool starts and failures. Successful results and thinking are left out.
- The feed streams into the terminal's own scrollback like `tail -f` does.
- There's no attempt to copy the actual screen.

These followed from the evidence or the reviews:

- **Liveness has three states.** They're held, absent and unknown. A failed `ps` or `lsof` call means unknown. So does an unreadable registry entry. "Session ended" appears only after a session that was held goes away.
- **Claude switches only on proof.** The viewer follows a session change only from the watched process's own registry file, and only after re-proving that process every check. A reused pid with a stale file is never followed.
- **Codex never guesses.** One Codex process can hold several threads, so when the window moves on the viewer says so instead of picking one.
- **Failures come only from explicit markers.** For Claude that's `is_error: true` and API error records. For Codex it's the completion status and exit code. Nothing is guessed from text.
- **The terminal mode is stricter than cbreak.** The viewer turns off echo and line editing. It also turns off the signal keys, flow control and the extended input keys. On macOS the extended keys make Ctrl-O discard output, and Ctrl-O is the watch key. Output processing stays on so line endings work.
- **The status line never ticks.** It shows clock times and redraws only when its text changes, so scrolling back stays put.
- **The menu runs full screen.** Baton passes `--no-height --no-popup` to fzf. A height set in `FZF_DEFAULT_OPTS` would put the viewer in the alternate screen, which has no scrollback.

## How it works

The whole feature lives in `bin/baton`, in the embedded Python section that starts with `# ---------- watch: read-only live view ----------`.

- `watch_target(path)` checks that the real path is inside `~/.claude/projects` or `~/.codex`. It works out the tool, the session id and whether the row is a subagent.
- `WatchFeed` turns one transcript record into feed events. It also tracks which tools are still running, whether the model is thinking and whether a Codex turn is open.
- `watch_backfill` reads the tail of the file, widening from 2 to 8 to 32 MB until it holds two prompts. It shows the events from the second-to-last prompt on.
- `WatchFile` follows the file by bytes and only decodes complete lines. It skips a record over 8 MB with one note. It notices a file that was truncated or replaced. It also notices one rewritten in place or briefly missing.
- `WatchLive` checks liveness every 2 seconds with the same proofs the kill check uses.
- `WatchUI` streams lines and keeps the status as the last line.
- `watch_run` sets the terminal mode and restores it on any exit, including SIGTERM and SIGHUP.
- In the bash part, Ctrl-O runs `--key watch`. That prints `execute(baton --watch-session {1} {9})+reload(...)` for session and subagent rows.
- `baton --watch-session PATH [CARD] [--once]` is the internal entry point. With `--once` it prints the backfill and status and exits, which the tests use.

## Reviews

Codex reviewed the work through Comparo five times. Every finding was checked against evidence before acting on it.

- **The design review** (`20261009-081551-21a1a6cc`) agreed with following the transcript. It asked for stronger file following and three-state liveness. It also asked for no guessing on Codex `/new` and for narrower wording about what "read-only" means.
- **The phase 1 review** (`20261009-091001-92a75616`) found two header fields that skipped sanitizing. It also found an API-error suppression that lasted forever and labels that piled up. A backfill could also stop short of the second-to-last prompt. All were fixed. Its claim about the colored label padding was wrong, because Python evaluates the whole right side before it rebinds the name.
- **The phase 2 review** (`20261009-100924-e3360406`) found an `lsof` exit code that was ignored and a tracked pid that wasn't re-proven. It also found a replaced file that could stop the follower for good. All were fixed. It claimed the feed would lose scrollback, but the real picker's output disproved it. It also claimed the truncation test couldn't catch a missing rewrite check, and a mutation test disproved that too.
- **The final review** (`20261009-103431-eb085c49`) found that leftover fzf tracking dropped keys typed right after an action. It also found that a user's fzf height setting would break scrollback. Both were confirmed in the real picker and fixed.
- **A last quick check** (`20261009-202203-2cbf35c8`) said ship. It asked for one hardening of hangup handling, which was done.

## Bugs found along the way

- **Fast typing after a list action dropped keys.** This one is older than watch mode. After a list action such as `+` or Ctrl-T, fzf kept tracking the row until the cursor moved. A reload while tracking blocks input, so typing `from` quickly registered only `f`. The `change` binding now runs `untrack-current` first. The reload after a watch doesn't track at all.
- **A transcript rewritten in place was missed.** A rewrite that left the file the same size or longer looked like normal growth. The follower now compares the 64 bytes just before its read position whenever the file changes.
- **Older Codex sessions may become unreadable later.** Codex can compress idle rollouts older than 7 days to `.jsonl.zst`. Baton's preview and fallback scan would miss those. None exist on Eric's Mac yet, so this is noted and not fixed.

## Testing

- **Size and runtime.** The suite grew from 103 to 145 tests. It runs in about 150 seconds normally and about 190 seconds under stock `/bin/bash` 3.2.
- **Fixtures.** The new tests use the existing fake homes under `tmp/`, and current Claude 2.1.295 and Codex 0.162 record shapes.
- **Live tests.** The live view runs under a real pseudo-terminal (a fake terminal a test can type into and read from). Those tests cover:
  - streaming, with torn and split lines and a 9 MB record
  - truncation, replacement and a missing file
  - the keys, resize and a hangup
  - terminal restore after q and SIGTERM
  - status changes, retargeting and failed `ps` or `lsof` calls
- **Read-only.** The read-only test watches a real process that logs every catchable signal it receives. It also has a listening socket at the session's messaging path and snapshots the whole fake home. Nothing changes and no signal arrives. Nothing connects to the socket either.
- **The real picker.** Picker tests drive fzf itself. They check Ctrl-O and the alternate-screen handoff. They also check a height setting in `FZF_DEFAULT_OPTS` and typing right after an action.
- **Mutation checks.** Each of these broken versions made a test fail:
  - a removed `isMeta` guard
  - a liveness check moved before the first draw
  - a removed rewrite check
  - a removed `untrack-current`
- **Real sessions.** A live Claude session was watched in a pseudo-terminal. A new tool call appeared within seconds and the status updated. A real `codex exec` run was watched from start to finish. The view showed the prompt and three shell calls in order. It then showed the status going from busy to idle and the session ending.

One testing lesson is worth keeping. Don't switch git branches while a test run is going, because the checkout briefly puts the old files on disk.

## Publishing and install

- The code was committed on `watch-mode` and fast-forwarded into `main`.
- GitHub pushes go through the contents API, because a plain `git push` is blocked on this laptop. Only the three changed files were sent, and their blob SHAs were checked against the local ones.
- CI was started once with `gh workflow run ci.yml --ref main` and passed.
- It was installed with the public one-liner pinned to the new commit, so the raw-file cache couldn't serve the old file:
  ```bash
  SHA=<commit>; curl -fsSL "https://raw.githubusercontent.com/stouffer-labs/baton/$SHA/scripts/install.sh" | BATON_GITHUB_REF=$SHA bash
  ```
- The installed `~/.local/share/baton/bin/baton` was compared with the published file and matched.

## Known limits

- **It's the transcript and not the screen.** Dialogs and spinners in the session's own window don't show. A Claude permission wait shows as a status, and a Codex approval doesn't show at all.
- **Claude lags.** The view is about 3 to 5 seconds behind. Codex is immediate but records a tool's output only when the tool finishes.
- **New Codex sessions show up late.** A brand-new Codex session gets a row only after its first prompt.
- **Slow probes delay q.** Liveness checks are synchronous. On a very slow machine a check can delay q by a few seconds.
- **Both transcript formats are internal to their tools.** They may change between versions. The parsers skip what they don't know.

## Follow-ups

- Eric still needs to check from the Windows PC that `›` and `✗` render and that the mouse wheel scrolls back.
- Read compressed `.jsonl.zst` Codex rollouts once Codex starts writing them.
- Possibly show subagent activity inline. Today the status only counts subagent files written in the last 30 seconds.
