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

note() { printf "  %s%s%s\n" "$DIM" "$1" "$RESET"; }

finish() {
  local url="${1:-}"
  printf "\n  %s✓ %s installed successfully.%s\n" "$GREEN" "$INSTALLER_NAME" "$RESET"
  [[ -n "$url" ]] && printf "    %s\n" "$url"
  printf "\n"
}
