#!/usr/bin/env bash
# Поднять SigmaSteamBot на VM после /stopbot: включает и запускает задачу планировщика.
# (после /stopbot задача ОТКЛЮЧЕНА, поэтому нужен именно Enable, потом Start.)
#
# Переменные окружения:
#   SIGMA_VM_HOST, SIGMA_VM_USER, SIGMA_VM_PASS — адрес VM и учётка Windows (обязательно)
#   SIGMA_TASK     (SigmaSteamBot)
#   SIGMA_BASE_DIR (%USERPROFILE%\sigmabot) — только для подсказки про логи
set -euo pipefail

HOST="${SIGMA_VM_HOST:?задайте SIGMA_VM_HOST — адрес VM}"
USER_="${SIGMA_VM_USER:?задайте SIGMA_VM_USER — пользователь Windows на VM}"
PASS="${SIGMA_VM_PASS:?задайте SIGMA_VM_PASS — его пароль}"
TASK="${SIGMA_TASK:-SigmaSteamBot}"
BASE="${SIGMA_BASE_DIR:-%USERPROFILE%\\sigmabot}"

command -v sshpass >/dev/null || { echo "нужен sshpass (apt install sshpass)"; exit 1; }

echo ">> ${USER_}@${HOST}: включаю и запускаю ${TASK}"
sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "${USER_}@${HOST}" \
  "powershell -NoProfile -Command \"Enable-ScheduledTask -TaskName '$TASK' | Out-Null; Start-ScheduledTask -TaskName '$TASK'; Start-Sleep -Seconds 3; Get-ScheduledTask -TaskName '$TASK' | Format-Table TaskName,State -AutoSize\""

echo ">> посмотреть лог:"
echo "   sshpass -p '***' ssh ${USER_}@${HOST} 'powershell -NoProfile -Command \"Get-Content ${BASE}\\logs\\supervisor.log -Tail 15\"'"
