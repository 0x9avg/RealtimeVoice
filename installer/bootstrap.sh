#!/usr/bin/env bash
# Мастер установки RealtimeVoice — ЕДИНСТВЕННАЯ команда, которую нужно
# выполнить вручную на роботе. Дальше всё происходит в браузере: этот
# скрипт только доводит систему до состояния "мастер настройки открывается
# по ссылке" — сам мастер (app/installer) уже занимается выбором робота,
# ключом Gemini, звуком, словами пробуждения и systemd-юнитом основного
# голосового сервиса.
#
# Предполагается, что этот файл уже лежит внутри развёрнутого на роботе
# кода (см. DEPLOYMENT.md §2 — git clone/rsync в ~/realtimevoice-r1 или
# ~/realtimevoice-g1), и запускается из корня этого каталога:
#
#   cd ~/realtimevoice-r1 && bash installer/bootstrap.sh
#
# Требования, которые этот скрипт НЕ решает (см. DEPLOYMENT.md §1): робот
# уже должен быть с установленным вендорским ПО (Unitree SDK, DDS) — сборка
# unitree_sdk2py/CycloneDDS под конкретную аппаратуру робота — отдельная,
# заранее выполненная интегратором задача, не то, что можно один раз
# автоматизировать здесь для любой машины.
set -euo pipefail

# 2026-09-06 audit finding: under `set -e`, any failing step here used to
# just exit with whatever raw error the failing command printed (a curl
# network error, an apt/uv message) and nothing else — for a non-technical
# user watching a terminal for the first time, that reads as "it broke",
# with no indication of what to do next. This trap adds one plain-Russian
# line pointing at the actual cause before the script exits, without
# hiding the original error output above it.
trap 'echo; echo "== Установка прервана (шаг: ${CURRENT_STEP:-неизвестен}) =="; \
      echo "Причина — в сообщении об ошибке выше. Проверьте интернет-соединение и повторите"; \
      echo "команду ещё раз — уже выполненные шаги не придётся повторять. Если не"; \
      echo "получится разобраться, обратитесь к тому, кто устанавливал систему."' ERR

cd "$(dirname "$0")/.."   # каталог проекта (там, где pyproject.toml)
PROJECT_DIR="$(pwd)"

echo "== RealtimeVoice — установка =="
echo "Каталог проекта: ${PROJECT_DIR}"
echo

CURRENT_STEP="установка uv"
if ! command -v uv >/dev/null 2>&1; then
    echo "-> Устанавливаю uv (менеджер пакетов Python)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    echo "-> uv уже установлен: $(command -v uv)"
fi

CURRENT_STEP="установка зависимостей Python (uv sync)"
echo "-> Устанавливаю зависимости Python (может занять пару минут при первом запуске)…"
# --inexact: на роботе unitree_sdk2py/cyclonedds/opencv-python и т.п. стоят
# вручную, в обход pyproject.toml — обычный `uv sync` их бы снёс (см.
# DEPLOYMENT.md §2).
uv sync --inexact

CURRENT_STEP="настройка автозапуска мастера (systemd)"
echo "-> Настраиваю автозапуск мастера настройки (порт 8090)…"
mkdir -p "$HOME/.config/systemd/user"
UNIT_SRC="deploy/realtimevoice-setup.service"
UNIT_DST="$HOME/.config/systemd/user/realtimevoice-setup.service"
sed "s#^WorkingDirectory=.*#WorkingDirectory=${PROJECT_DIR}#" "$UNIT_SRC" > "$UNIT_DST"

# Пользовательский systemd переживает выход из SSH-сессии и перезагрузку
# только если для пользователя включён linger — без sudo, см. DEPLOYMENT.md §6.
loginctl enable-linger "$(whoami)" 2>/dev/null || true

systemctl --user daemon-reload
systemctl --user enable --now realtimevoice-setup.service

CURRENT_STEP="определение адреса робота"
# 2026-09-06 audit finding: `hostname -I` lists every interface's address
# in whatever order the kernel enumerates them, which can put a robot's
# dedicated DDS/vendor-SDK interface (see app/config.py's
# ROBOT_MIC_INTERFACE_IP/ROBOT_SPEAKER_IFACE — this project already
# assumes a multi-NIC robot) ahead of the actual LAN one a phone/laptop
# can reach, printing an unreachable address for the customer to open.
# `ip route get` instead asks the kernel which LOCAL address it would
# actually use to route outbound — the same reasoning
# app/installer/network.py's detect_lan_ip() uses in Python (a route
# lookup, no packet actually sent).
IP="$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' | head -n1)" || true
IP="${IP:-<адрес-робота>}"

echo
echo "======================================================================"
echo " Готово! Откройте в браузере (с любого устройства в той же сети):"
echo
echo "     http://${IP}:8090"
echo
echo " Там мастер проведёт по всем оставшимся шагам настройки."
echo "======================================================================"
