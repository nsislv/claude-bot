#!/usr/bin/env bash
#
# start-bot.sh — launch Claude Code Telegram Bot
# Designed to be invoked by Windows Task Scheduler at system startup.
#
# Task Scheduler configuration:
#   Program/script:  "C:\Program Files\Git\bin\bash.exe"
#   Arguments:       --login -c "/c/Users/Administrator/Projects/claude-bot/start-bot.sh"
#   Run as:          Administrator
#   Trigger:         At startup

set -u

# ─── Paths ────────────────────────────────────────────────────────────────────
BOT_HOME="/c/Users/Administrator/Projects/claude-bot"
BOT_EXE="/c/Python/Scripts/claude-telegram-bot.exe"
LOG_FILE="${BOT_HOME}/bot.log"
PID_FILE="${BOT_HOME}/bot.pid"
ENV_FILE="${BOT_HOME}/.env"

export PATH="/c/Users/Administrator/.local/bin:/c/Program Files/nodejs:/c/Python:/c/Python/Scripts:/c/Program Files/Git/cmd:${PATH}"

log() {
  printf '[%s] [start-bot] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${LOG_FILE}"
}

die() {
  log "FATAL: $*"
  exit 1
}

[[ -d "${BOT_HOME}" ]] || die "Bot home not found: ${BOT_HOME}"
[[ -x "${BOT_EXE}"  ]] || die "Bot executable not found: ${BOT_EXE}"
[[ -f "${ENV_FILE}" ]] || die ".env not found: ${ENV_FILE}"

cd "${BOT_HOME}" || die "Cannot cd to ${BOT_HOME}"

log "────────────────────────────────────────"
log "Startup triggered (host=$(hostname) user=${USERNAME:-unknown})"

# Stop any previous instance
if [[ -f "${PID_FILE}" ]]; then
  OLD_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${OLD_PID}" ]] && kill -0 "${OLD_PID}" 2>/dev/null; then
    log "Stopping previous bot (PID=${OLD_PID})"
    taskkill //PID "${OLD_PID}" //F //T >/dev/null 2>&1 || true
    sleep 2
  fi
  rm -f "${PID_FILE}"
fi

# Kill stray claude-telegram-bot.exe processes
STRAYS="$(tasklist //FI 'IMAGENAME eq claude-telegram-bot.exe' //FO CSV //NH 2>/dev/null \
         | awk -F'","' '{gsub(/"/,""); print $2}' | grep -E '^[0-9]+$' || true)"
if [[ -n "${STRAYS}" ]]; then
  log "Killing stray bot processes: ${STRAYS}"
  for p in ${STRAYS}; do taskkill //PID "${p}" //F >/dev/null 2>&1 || true; done
  sleep 2
fi

# Rotate log if >5 MiB
if [[ -f "${LOG_FILE}" ]]; then
  SIZE=$(stat -c '%s' "${LOG_FILE}" 2>/dev/null || echo 0)
  if (( SIZE > 5*1024*1024 )); then
    mv -f "${LOG_FILE}" "${LOG_FILE}.$(date '+%Y%m%d-%H%M%S')"
    log "Rotated previous log (${SIZE} bytes)"
  fi
fi

log "Launching ${BOT_EXE}"
exec "${BOT_EXE}" >> "${LOG_FILE}" 2>&1 &

BOT_PID=$!
echo "${BOT_PID}" > "${PID_FILE}"
log "Bot started (PID=${BOT_PID})"

wait "${BOT_PID}"
RC=$?
log "Bot exited with code ${RC}"
rm -f "${PID_FILE}"
exit "${RC}"
