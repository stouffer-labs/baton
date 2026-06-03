# Design history: the tmux-wrapped shadow menu (Baton's predecessor)

> **Historical.** This documents the original tmux-based approach that Baton
> replaced. Baton resumes sessions **natively** (`claude --resume` /
> `codex resume`) with no multiplexer — see the README. Kept here because the
> tmux findings (mouse forwarding, grouped-session gotchas) are still useful
> reference.

**Goal:** run claude/codex inside tmux automatically, with working mouse-wheel
scrollback, and shadow those sessions from another machine (SSH from Windows)
via an arrow-key menu — without disrupting the view already running on the Mac.

**Date:** 2026-06-01

## Components

### 1. `~/.tmux.conf` (new)
The mouse-scroll fix. There was no tmux config before, so `mouse` defaulted to
**off** — that is why scrolling "didn't do that" previously.

- `set -g mouse on` — the missing piece.
- `history-limit 50000` — long agent scrollback.
- `aggressive-resize on` — each client sizes the window it is viewing, so a
  remote client doesn't force the Mac to reflow unless both view the same window.
- Smart wheel binding: if the focused pane requested mouse reporting
  (claude/codex/vim/less → `#{mouse_any_flag}` true), forward the wheel so the
  **app** scrolls its own history; otherwise enter tmux copy-mode and scroll
  tmux's scrollback. This is the key to scroll working in both the TUI and at a
  plain shell prompt.
- `terminal-features ",*:RGB"` — truecolor for the agent UIs.

### 2. `agent-tmux.sh` (this dir, sourced from `~/.bashrc`)
Defines `claude()` and `codex()` shell functions:
- **Outside tmux** → creates a uniquely named session `claude-<dir>` /
  `codex-<dir>` (appends `-2`, `-3`… on collision) and runs the tool inside it,
  then `exec $SHELL -l` so the pane stays open for scrollback after exit.
- **Inside tmux** → runs the tool directly (no nested sessions).
- `claude` needs no bypass flag (handled by `permissions.defaultMode:
  bypassPermissions` in `~/.claude/settings.json`); `codex` keeps
  `--dangerously-bypass-approvals-and-sandbox`.
- Starts with `unalias claude codex` so a shell that already loaded the old
  `alias codex=...` doesn't hit a syntax error while parsing the function defs
  (alias expansion happens per-line as the file is read).

Wired into `~/.bashrc` (replaced the old `alias codex=...`) with:
`source "<project-dir>/agent-tmux.sh"`

### 3. `agents` (this dir, symlinked to `~/Scripts/agents`, on PATH)
fzf-based menu, run after SSHing into the Mac. Just type `agents`.
- Lists sessions as `tool · dir`, hides internal `*__shadow_<pid>` sessions.
- **Live preview pane** of each session via `capture-pane` (last 40 lines).
- Keys: `enter`/type-filter attach · `ctrl-n` new · `ctrl-x` kill · `esc` quit.
- Empty-state prompt when no sessions exist.
- **TTY guard:** if stdin/stdout aren't a terminal (i.e. `ssh mac agents`
  without `-t`), it errors with the hint to use `ssh -t <host> agents`.
- **Reaps stale shadows** at startup (any `__shadow_<pid>` whose pid is dead).

### 4. Shadow-attach (inside `agents`)
The "don't disrupt" core (final, corrected design):
```
tmux new-session -d -t "=<target>" -s "<target>__shadow_<pid>"   # grouped, unique
# standalone client:
tmux attach-session -t "=<target>__shadow_<pid>"   # blocks until detach
tmux kill-session   -t "=<target>__shadow_<pid>"   # explicit cleanup after
# inside tmux already:
tmux switch-client  -t "=<target>__shadow_<pid>"   # reaper cleans up later
```
Grouped session shares the target's windows but has independent current-window
focus + size. Cleanup is **explicit** (kill after the client leaves), NOT
`destroy-unattached` — see the gotcha below for why that option is unusable
here. The `<pid>` suffix lets multiple watchers of the same agent coexist
without disconnecting each other. Killing a shadow never harms the agent as long
as the target session still exists (verified).

#### Why this differs from the first draft (a Codex second-opinion + testing)
The original used `exec tmux attach` + `destroy-unattached on` with a fixed
`__shadow` name. Codex review flagged: no cleanup path on attach failure / when
run inside tmux, fixed name disconnects a 2nd watcher, and a `set -u` preview
crash. Empirical testing then revealed two deeper, mutually-masking bugs:
1. `destroy-unattached on` **self-destructs a _detached_ session the instant it
   is set** — and our shadow is created with `-d` (detached). So it could never
   survive to be attached.
2. `set-option -t "=name"` does **NOT** honor the `=` exact-match prefix (it
   errored "no such session"), so the `destroy-unattached` set silently failed —
   which is the *only* reason the shadow survived in the first draft. The two
   bugs cancelled out, producing confusing non-determinism.
Replacing `destroy-unattached` with explicit post-attach `kill-session` + a
startup reaper removed both. `exec` was also dropped so the menu loops back
after you detach.

## Known limitation (by tmux design)
tmux renders one character grid per window. When the Windows box and the Mac
view the **same** window at once, tmux sizes it to the **smaller** terminal
while both are attached, and auto-restores the Mac to full size the moment the
remote detaches. The agent process is never disrupted; only the Mac's *view*
temporarily reflows. Maximize the Windows terminal to minimize this. True
simultaneous different sizes for one live pane is not possible in tmux.

## tmux gotchas (verified on 3.6a during build)
- **`=` exact-match prefix is honored inconsistently across commands.** Verified:
  `has-session`, `kill-session`, `new-session -t`, `attach-session -t` HONOR it;
  `capture-pane` and `set-option` do NOT (they error "can't find pane" /
  "no such session"). For those two, use a bare session name — tmux resolves it
  to the active pane / prefers exact name matches even when another session name
  is a prefix (`base` is safe when targeting `base__shadow_1`).
- **`destroy-unattached on` deletes a _detached_ session immediately** when set,
  not "on next detach". Unusable for a create-detached-then-attach flow.
- **tmux rewrites `.` and `:` in session names to `_`**, and reads `.` in a
  target as a window separator. Shadow names therefore use `_<pid>`, not
  `.<pid>` — otherwise the name written ≠ the name stored and exact targeting
  silently misses.
- **`attach-session` returns exit 0 on a normal detach** (verified via a pty
  harness), so `[ $rc -eq 0 ] || die` does not false-positive on clean detach.

## Testing performed
- tmux.conf loads on an isolated server (`-L` socket); `mouse on`,
  `history-limit`, `aggressive-resize`, and the wheel binding all verified.
- `claude`/`codex` resolve to functions in a fresh login shell; slug, naming,
  and collision logic verified.
- Shadow grouping verified: shadow joins the target's session group and shares
  windows; list builder hides `__shadow_<pid>`.
- Preview helper returns live pane content; `--preview` with no arg now errors
  cleanly (was a `set -u` crash).
- `unalias … || true` fix verified against a shell with the old alias preloaded
  and `expand_aliases` on (the exact original failure condition).
- Reaper verified: kills a shadow whose pid is dead, spares one whose pid is
  live.
- Killing a shadow leaves the agent process alive (`pane_dead=0`) as long as the
  target exists.
- `attach-session` exit code on normal detach confirmed `0` via a Python pty
  harness (so cleanup logic won't false-error).

**Not yet verified by machine — needs your eyes:** the actual mouse-wheel
behavior in a real terminal, and from the Windows SSH client specifically (some
SSH clients translate wheel events differently). Open a session with `claude`,
scroll up with the wheel inside the conversation, and confirm it scrolls the
agent's history; then scroll at a plain shell prompt and confirm tmux copy-mode
scrollback works.
