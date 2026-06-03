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

usage() {
  cat <<'EOF'
Install the `baton` claude/codex session menu.

Usage:
  scripts/install.sh                      # download latest from GitHub main
  scripts/install.sh --from-source PATH   # install from a local checkout

Environment:
  BATON_GITHUB_OWNER  (default: stouffer-labs)
  BATON_GITHUB_REPO   (default: baton)
  BATON_GITHUB_REF    (default: main)
  BATON_INSTALL_DIR   (default: ~/.local/share/baton)
  BATON_BIN_DIR       (default: ~/.local/bin)
EOF
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: required command not found: $1" >&2; exit 1; }
}

FROM_SOURCE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-source)
      [[ -n "${2:-}" ]] || { echo "error: --from-source requires a path argument" >&2; exit 2; }
      FROM_SOURCE="$2"; shift 2 ;;
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

echo "done. run 'baton' to list/resume claude & codex sessions."
