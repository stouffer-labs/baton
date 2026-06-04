#!/usr/bin/env bash
set -euo pipefail

# Install the `baton` session menu into a local, PATH-independent runtime.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/stouffer-labs/baton/main/scripts/install.sh | bash
#   scripts/install.sh                      # download latest from GitHub main
#   scripts/install.sh --from-source PATH   # install from a local checkout (dev)
#
# Environment:
#   BATON_GITHUB_OWNER  GitHub owner (default: stouffer-labs)
#   BATON_GITHUB_REPO   Repo name    (default: baton)
#   BATON_GITHUB_REF    Branch/tag   (default: main)
#   BATON_INSTALL_DIR   Runtime dir  (default: ~/.local/share/baton)
#   BATON_BIN_DIR       Launcher dir (default: ~/.local/bin)
#
# The runtime is COPIED (not symlinked to a checkout), so `baton` keeps working
# even if the source dir goes away. Installs the `baton` command plus an
# `agents` alias for backward compatibility. Re-run any time to update.
#
# By default it also WIRES SHELL INTEGRATION: it adds a managed, idempotent block
# (`eval "$(baton shell-init ...)"`) to your shell rc file(s) so `baton` becomes a
# shell function that leaves you IN the session's project directory after the
# session ends (instead of back at ~). It wires every rc among ~/.zshrc,
# ~/.bashrc, ~/.bash_profile that exists. Use --no-modify-rc to skip this (the
# line to add yourself is printed instead).

usage() {
  cat <<'EOF'
Install the `baton` claude/codex session menu.

Usage:
  scripts/install.sh                      # download latest from GitHub main
  scripts/install.sh --from-source PATH   # install from a local checkout
  scripts/install.sh --no-modify-rc       # don't edit shell rc; print the line instead

Environment:
  BATON_GITHUB_OWNER  (default: stouffer-labs)
  BATON_GITHUB_REPO   (default: baton)
  BATON_GITHUB_REF    (default: main)
  BATON_INSTALL_DIR   (default: ~/.local/share/baton)
  BATON_BIN_DIR       (default: ~/.local/bin)
  BATON_RC_FILE       (default: auto — wire ~/.zshrc, ~/.bashrc, ~/.bash_profile
                       that exist; set to wire exactly one explicit file instead)
EOF
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: required command not found: $1" >&2; exit 1; }
}

FROM_SOURCE=""
MODIFY_RC=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-source)
      [[ -n "${2:-}" ]] || { echo "error: --from-source requires a path argument" >&2; exit 2; }
      FROM_SOURCE="$2"; shift 2 ;;
    --no-modify-rc) MODIFY_RC=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

OWNER="${BATON_GITHUB_OWNER:-stouffer-labs}"
REPO="${BATON_GITHUB_REPO:-baton}"
REF="${BATON_GITHUB_REF:-main}"
INSTALL_DIR="${BATON_INSTALL_DIR:-$HOME/.local/share/baton}"
BIN_DIR="${BATON_BIN_DIR:-$HOME/.local/bin}"

# Guard: INSTALL_DIR must be an absolute path (also rejects empty/whitespace).
# This protects the `rm -rf "${INSTALL_DIR}"` below from a misconfigured value.
case "$INSTALL_DIR" in
  /*) : ;;
  *) echo "error: BATON_INSTALL_DIR must be an absolute path: '${INSTALL_DIR}'" >&2; exit 1 ;;
esac

# Markers delimiting the block we manage in a user's rc file. Kept stable so
# re-running the installer REPLACES the block rather than appending duplicates.
RC_BEGIN="# >>> baton shell integration >>>"
RC_END="# <<< baton shell integration <<<"

# wire_rc <rc_file> <shell: bash|zsh>
# Idempotently install the managed integration block into <rc_file>. Replaces an
# existing block (matched by markers) or appends a new one. Uses awk + a temp
# file (portable; avoids macOS/GNU `sed -i` differences).
wire_rc() {
  local rc_file="$1" shell="$2"
  local installed_bin="${INSTALL_DIR}/bin/baton"
  local block
  # The `$(...)` MUST be written literally to the rc file (it's an eval-time
  # substitution, run when the rc loads), so single quotes / no-expansion is
  # intentional here. %q safely quotes the binary path for the rc.
  # shellcheck disable=SC2016
  printf -v block '%s\neval "$(%q shell-init %s)"\n%s' \
    "$RC_BEGIN" "$installed_bin" "$shell" "$RC_END"

  mkdir -p "$(dirname "$rc_file")"
  [[ -f "$rc_file" ]] || : >"$rc_file"

  if grep -qF "$RC_BEGIN" "$rc_file" 2>/dev/null; then
    # Replace the existing managed block in place.
    local tmp_rc
    tmp_rc="$(mktemp -t baton-rc.XXXXXX)"
    awk -v b="$RC_BEGIN" -v e="$RC_END" -v repl="$block" '
      $0==b {inblk=1; print repl; next}
      inblk && $0==e {inblk=0; next}
      !inblk {print}
    ' "$rc_file" >"$tmp_rc"
    cat "$tmp_rc" >"$rc_file"
    rm -f "$tmp_rc"
    echo "updated baton integration in ${rc_file}"
  else
    # Append, ensuring a separating blank line if the file already has content.
    # Decide on the separator BEFORE opening the file for append (don't stat and
    # write the same file in one pipeline).
    local sep=""
    [[ -s "$rc_file" ]] && sep=$'\n'
    printf '%s%s\n' "$sep" "$block" >>"$rc_file"
    echo "added baton integration to ${rc_file}"
  fi
}

# Decide which rc files to wire. Default: every standard rc that exists (so baton
# works in both bash and zsh). BATON_RC_FILE overrides with one explicit target.
# Pairs each file with the right shell so `shell-init` emits matching syntax.
collect_rc_targets() {
  if [[ -n "${BATON_RC_FILE:-}" ]]; then
    case "$BATON_RC_FILE" in
      *zsh*) printf '%s\t%s\n' "$BATON_RC_FILE" "zsh" ;;
      *)     printf '%s\t%s\n' "$BATON_RC_FILE" "bash" ;;
    esac
    return
  fi
  local any=0
  [[ -f "$HOME/.zshrc" ]]        && { printf '%s\t%s\n' "$HOME/.zshrc" "zsh";         any=1; }
  [[ -f "$HOME/.bashrc" ]]       && { printf '%s\t%s\n' "$HOME/.bashrc" "bash";       any=1; }
  [[ -f "$HOME/.bash_profile" ]] && { printf '%s\t%s\n' "$HOME/.bash_profile" "bash"; any=1; }
  # Nothing exists yet → create the rc for the shell the installer runs under.
  if [[ "$any" -eq 0 ]]; then
    case "${SHELL:-}" in
      *zsh*) printf '%s\t%s\n' "$HOME/.zshrc" "zsh" ;;
      *)     printf '%s\t%s\n' "$HOME/.bashrc" "bash" ;;
    esac
  fi
}

# Runtime needs python3 and fzf; warn (don't fail) if fzf is missing at install
# time so the curl|bash flow still completes on a fresh box.
need_cmd python3
command -v fzf >/dev/null 2>&1 || \
  echo "warn: fzf not found — install it before running 'baton' (brew install fzf / apt install fzf)"

# Resolve the baton script: from a local checkout, or downloaded from GitHub.
tmp_dir=""
# `return 0` so a no-op cleanup (e.g. --from-source, where tmp_dir is empty)
# doesn't make the EXIT trap's last command non-zero and exit the script 1.
cleanup() { [[ -n "$tmp_dir" ]] && rm -rf -- "$tmp_dir"; return 0; }
trap cleanup EXIT

if [[ -n "$FROM_SOURCE" ]]; then
  src_root="${FROM_SOURCE%/}"
  [[ -f "${src_root}/bin/baton" ]] || { echo "error: ${src_root}/bin/baton not found" >&2; exit 1; }
  baton_src="${src_root}/bin/baton"
  echo "baton installer: source = local path ${src_root}"
else
  need_cmd curl
  tmp_dir="$(mktemp -d -t baton-install.XXXXXX)"
  url="https://raw.githubusercontent.com/${OWNER}/${REPO}/${REF}/bin/baton"
  echo "baton installer: source = ${url}"
  curl -fsSL "$url" -o "${tmp_dir}/baton" || { echo "error: download failed: $url" >&2; exit 1; }
  # Sanity check: the fetched file is the script, not an HTML 404 page.
  head -n1 "${tmp_dir}/baton" | grep -q '^#!/usr/bin/env bash' || {
    echo "error: downloaded file is not the baton script (bad ref or repo?)" >&2; exit 1; }
  baton_src="${tmp_dir}/baton"
fi

echo "installing runtime to ${INSTALL_DIR}"
rm -rf "${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}/bin"
cp "${baton_src}" "${INSTALL_DIR}/bin/baton"
chmod +x "${INSTALL_DIR}/bin/baton"

mkdir -p "${BIN_DIR}"
ln -sf "${INSTALL_DIR}/bin/baton" "${BIN_DIR}/baton"
ln -sf "${INSTALL_DIR}/bin/baton" "${BIN_DIR}/agents"   # backward-compat alias

echo "linked ${BIN_DIR}/baton  -> ${INSTALL_DIR}/bin/baton"
echo "linked ${BIN_DIR}/agents -> ${INSTALL_DIR}/bin/baton  (alias)"
if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
  echo "hint: add ${BIN_DIR} to your PATH, e.g.:"
  echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
fi

# Shell integration: make `baton` a function so it leaves you in the session's
# project directory after the session ends. Without it, baton still resumes but
# (being a child process) can't move your shell, so you land back where you were.
wired_files=()
if [[ "$MODIFY_RC" -eq 1 ]]; then
  while IFS=$'\t' read -r rc_file rc_shell; do
    [[ -n "$rc_file" ]] || continue
    wire_rc "$rc_file" "$rc_shell"
    wired_files+=("$rc_file")
  done < <(collect_rc_targets)
fi

echo
if [[ "$MODIFY_RC" -eq 1 && ${#wired_files[@]} -gt 0 ]]; then
  echo "shell integration wired. Activate it now with:"
  echo "  source ${wired_files[0]}      # or just open a new terminal"
  echo "Then run 'baton' — pick a session, and you'll be left in its directory after."
else
  echo "shell integration NOT wired (--no-modify-rc). Add this line to your shell rc"
  echo "to be left in the session's directory after it ends:"
  echo "  eval \"\$(\"${INSTALL_DIR}/bin/baton\" shell-init bash)\"   # use 'zsh' for ~/.zshrc"
  echo "Without it, 'baton' still lists & resumes sessions, but won't move your shell."
fi
