#!/usr/bin/env bash
# Arkive installer shared library: polished step UI, logging, and resume support.
#
# Usage from an installer:
#   source "$SCRIPT_DIR/lib.sh"
#   init_installer "Arkive Cloud" "/var/lib/arkive/install-state"
#   require_root
#   step "Installing system packages" install_os_deps
#   finish "https://vault.arkive.life"
#
# Each step's stdout/stderr is hidden and captured to $LOG; only a clean
# progress line is shown. Completed steps are recorded so a re-run resumes
# where it left off. Set CV_FORCE=1 to re-run every step. Set CV_VERBOSE=1 to
# stream command output instead of the spinner.

set -Eeuo pipefail

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
  GREEN=$'\033[32m'; RED=$'\033[31m'; CYAN=$'\033[36m'; YELLOW=$'\033[33m'
else
  BOLD=""; DIM=""; RESET=""; GREEN=""; RED=""; CYAN=""; YELLOW=""
fi

LOG=""
STATE_DIR=""
INSTALLER_NAME=""
_STEP_NO=0

init_installer() {
  INSTALLER_NAME="$1"
  STATE_DIR="${2:-/var/lib/arkive/install-state}"
  mkdir -p "$STATE_DIR"
  LOG="/var/log/arkive-install-$(date +%Y%m%d-%H%M%S).log"
  : > "$LOG" 2>/dev/null || LOG="/tmp/arkive-install.log"
  trap '_on_error $LINENO' ERR
  printf "\n  %s%s Installer%s\n" "$BOLD" "$INSTALLER_NAME" "$RESET"
  printf "  %slog: %s%s\n\n" "$DIM" "$LOG" "$RESET"
}

require_root() {
  if [[ $EUID -ne 0 ]]; then
    printf "  %s✗%s Please run as root (sudo).\n" "$RED" "$RESET"
    exit 1
  fi
}

_slug() {
  echo "$1" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-'
}

# Set the system hostname (idempotent, best-effort — never aborts the install).
# Sanitizes the requested name to a valid hostname, applies it via hostnamectl
# (falling back to /etc/hostname + hostname), and keeps the /etc/hosts 127.0.1.1
# line in sync so local resolution still works.
set_system_hostname() {
  local desired short current
  desired="$(printf '%s' "${1:-}" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -e 's/[^a-z0-9.-]\{1,\}/-/g' -e 's/^[-.]\{1,\}//' -e 's/[-.]\{1,\}$//')"
  [[ -z "$desired" ]] && return 0
  short="${desired%%.*}"
  current="$(hostnamectl --static 2>/dev/null || cat /etc/hostname 2>/dev/null || hostname 2>/dev/null || echo '')"
  current="$(printf '%s' "$current" | tr -d '[:space:]')"
  [[ "$current" == "$desired" ]] && return 0
  if command -v hostnamectl >/dev/null 2>&1; then
    hostnamectl set-hostname "$desired" 2>/dev/null || true
  else
    printf '%s\n' "$desired" > /etc/hostname 2>/dev/null || true
    hostname "$desired" 2>/dev/null || true
  fi
  if [[ -f /etc/hosts ]]; then
    if grep -q '^127\.0\.1\.1' /etc/hosts 2>/dev/null; then
      sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${desired} ${short}/" /etc/hosts 2>/dev/null || true
    else
      printf '127.0.1.1\t%s %s\n' "$desired" "$short" >> /etc/hosts 2>/dev/null || true
    fi
  fi
  echo "system hostname set to ${desired}"
}

_on_error() {
  # Safety net for failures outside a managed step.
  printf "\n  %s✗ Unexpected error near line %s%s\n" "$RED" "${1:-?}" "$RESET"
  printf "  See %s\n" "$LOG"
  exit 1
}

_spinner() {
  local pid=$1 desc=$2 frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' i=0
  while kill -0 "$pid" 2>/dev/null; do
    i=$(((i + 1) % ${#frames}))
    printf "\r  %s%s%s %s" "$CYAN" "${frames:$i:1}" "$RESET" "$desc"
    sleep 0.1
  done
}

# step "Description" command [args...]
# The command may be a shell function; its output goes only to $LOG.
step() {
  local desc="$1"; shift
  _STEP_NO=$((_STEP_NO + 1))
  local marker="$STATE_DIR/$(_slug "$desc").done"

  if [[ -f "$marker" && "${CV_FORCE:-0}" != "1" ]]; then
    printf "  %s✓%s %s %s(already done)%s\n" "$GREEN" "$RESET" "$desc" "$DIM" "$RESET"
    return 0
  fi

  echo "=== step $_STEP_NO: $desc ===" >> "$LOG"

  if [[ "${CV_VERBOSE:-0}" == "1" || ! -t 1 ]]; then
    printf "  %s•%s %s\n" "$CYAN" "$RESET" "$desc"
    if "$@" >>"$LOG" 2>&1; then
      touch "$marker"
    else
      _step_failed "$desc"; return 1
    fi
    return 0
  fi

  # TTY: run in background with a spinner, hide output.
  # Clear the ERR trap inside the subshell so an intentional non-zero return
  # from a step function is reported once (via wait), not twice.
  ( trap - ERR; "$@" >>"$LOG" 2>&1 ) &
  local pid=$!
  _spinner "$pid" "$desc"
  if wait "$pid"; then
    printf "\r  %s✓%s %s\033[K\n" "$GREEN" "$RESET" "$desc"
    touch "$marker"
  else
    printf "\r  %s✗%s %s\033[K\n" "$RED" "$RESET" "$desc"
    _step_failed "$desc"; return 1
  fi
}

_step_failed() {
  local desc="$1"
  printf "\n  %sStep failed:%s %s\n" "$RED" "$RESET" "$desc"
  printf "  %sLast 25 lines of %s:%s\n" "$DIM" "$LOG" "$RESET"
  tail -n 25 "$LOG" 2>/dev/null | sed 's/^/    /'
  printf "\n  %sThe installer is resumable.%s Fix the issue above and re-run the\n" "$YELLOW" "$RESET"
  printf "  same command — completed steps are skipped automatically.\n"
  printf "  (Use %sCV_FORCE=1%s to redo every step, %sCV_VERBOSE=1%s to see live output.)\n\n" \
    "$BOLD" "$RESET" "$BOLD" "$RESET"
  exit 1
}

# step_always: like step but never cached. Use for deploy/verify steps (code
# copy, build, service install, health check) so re-running always redeploys
# the latest code and re-checks — never silently skipped by a stale marker.
step_always() {
  local desc="$1"; shift
  local _saved="${CV_FORCE:-0}"
  CV_FORCE=1
  step "$desc" "$@"
  local rc=$?
  CV_FORCE="$_saved"
  return $rc
}

# A content fingerprint of the given files/dirs (build inputs). Excludes derived
# and vendored trees so only real source changes matter.
_fingerprint() {
  {
    local p
    for p in "$@"; do
      if [[ -d "$p" ]]; then
        find "$p" -type f \
          -not -path '*/node_modules/*' -not -path '*/.venv/*' \
          -not -path '*/dist/*' -not -path '*/__pycache__/*' \
          -not -name '*.pyc' 2>/dev/null
      elif [[ -e "$p" ]]; then
        echo "$p"
      fi
    done | LC_ALL=C sort | while IFS= read -r f; do
      sha256sum "$f" 2>/dev/null
    done
  } | sha256sum | awk '{print $1}'
}

# step_if_changed "Description" "path1 path2 ..." command [args...]
# Runs the command only when the fingerprint of the given input paths changed
# since the last successful run (or when CV_FORCE=1). Use for expensive,
# input-driven steps (web build, dependency install) so unchanged redeploys skip
# them. Keep cheap steps as step_always.
step_if_changed() {
  local desc="$1" paths="$2"; shift 2
  local slug hashfile newhash
  slug="$(_slug "$desc")"
  hashfile="$STATE_DIR/$slug.inputhash"
  # shellcheck disable=SC2086
  newhash="$(_fingerprint $paths)"
  if [[ "${CV_FORCE:-0}" != "1" && -f "$hashfile" && "$(cat "$hashfile" 2>/dev/null)" == "$newhash" ]]; then
    _STEP_NO=$((_STEP_NO + 1))
    printf "  %s✓%s %s %s(unchanged)%s\n" "$GREEN" "$RESET" "$desc" "$DIM" "$RESET"
    return 0
  fi
  step_always "$desc" "$@"
  local rc=$?
  [[ $rc -eq 0 ]] && printf '%s' "$newhash" > "$hashfile"
  return $rc
}

note() { printf "  %s%s%s\n" "$DIM" "$1" "$RESET"; }

# Verify genuine post-quantum crypto in a venv. Args: python_bin oqs_prefix.
pq_selftest() {
  OQS_INSTALL_PATH="$2" "$1" - <<'PY' 2>&1
import sys
try:
    import oqs
    oqs.KeyEncapsulation("ML-KEM-768")
    oqs.Signature("ML-DSA-65")
except BaseException as e:
    print("pq_selftest error:", repr(e))
    sys.exit(1)
print("pq_selftest OK")
PY
}

# Ensure real liboqs + liboqs-python (same version) in a venv, verifying ML-KEM
# and ML-DSA. Writes a .pq-ok marker on success. Honors CV_ALLOW_CLASSICAL_FALLBACK.
# Args: venv_dir liboqs_version [install_prefix]
ensure_liboqs() {
  local venv="$1" version="$2" prefix="${3:-/usr/local}"
  local pip="$venv/bin/pip" py="$venv/bin/python" mark="$venv/.pq-ok"

  if pq_selftest "$py" "$prefix"; then
    printf '%s' "$version" > "$mark"
    echo "liboqs already functional (real post-quantum crypto active)"
    return 0
  fi

  # The unrelated PyPI package named 'oqs' shadows the real binding.
  "$pip" uninstall -y oqs 2>/dev/null || true
  apt-get install -y cmake ninja-build gcc g++ libssl-dev git

  # (Re)build native liboqs at the pinned version so its ABI matches the binding.
  local src="/opt/liboqs-src"
  rm -rf "$src"
  git clone --depth 1 --branch "$version" \
    https://github.com/open-quantum-safe/liboqs.git "$src"
  cmake -S "$src" -B "$src/build" -GNinja \
    -DBUILD_SHARED_LIBS=ON -DOQS_BUILD_ONLY_LIB=ON -DCMAKE_INSTALL_PREFIX="$prefix"
  cmake --build "$src/build" --parallel
  cmake --install "$src/build"
  ldconfig

  "$pip" install --force-reinstall --no-deps "liboqs-python==${version}" \
    || "$pip" install "git+https://github.com/open-quantum-safe/liboqs-python.git@${version}"

  if pq_selftest "$py" "$prefix"; then
    printf '%s' "$version" > "$mark"
    echo "liboqs OK — ML-KEM-768 / ML-DSA-65 available (quantum-safe active)"
    return 0
  fi

  echo "!! liboqs verification FAILED — real post-quantum crypto is NOT active."
  if [[ "${CV_ALLOW_CLASSICAL_FALLBACK:-0}" == "1" ]]; then
    echo "CV_ALLOW_CLASSICAL_FALLBACK=1 set; continuing with the flagged classical fallback."
    return 0
  fi
  echo "Refusing to continue without quantum-safe crypto."
  echo "Set CV_ALLOW_CLASSICAL_FALLBACK=1 to install anyway (NOT quantum-safe)."
  return 1
}

finish() {
  local url="${1:-}"
  printf "\n  %s✓ %s installed successfully.%s\n" "$GREEN" "$INSTALLER_NAME" "$RESET"
  [[ -n "$url" ]] && printf "    %s\n" "$url"
  printf "\n"
}
