#!/bin/bash
# Установка/обновление моста. Первый раз: bash install.sh <TOKEN_БОТА>
set -e
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  T="$1"; [ -n "$T" ] || { echo "Нужен токен бота: bash install.sh <TOKEN>"; exit 1; }
  C=$(curl -s "https://api.telegram.org/bot$T/getUpdates" | python3 -c '
import sys, json
for u in reversed(json.load(sys.stdin).get("result", [])):
    for v in u.values():
        if isinstance(v, dict) and v.get("chat", {}).get("type") == "supergroup": print(v["chat"]["id"]); sys.exit()')
  [ -n "$C" ] || { echo "Группа не найдена. Включите в группе «Темы», напишите в неё любое сообщение и повторите."; exit 1; }
  ME=$(curl -s "https://api.telegram.org/bot$T/getMe" | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["id"])')
  curl -s "https://api.telegram.org/bot$T/getChatMember?chat_id=$C&user_id=$ME" | python3 -c '
import sys, json
r = json.load(sys.stdin).get("result", {})
if r.get("status") != "administrator" or not r.get("can_manage_topics"):
    print("Бот должен быть администратором с правом «Управление темами»."); sys.exit(1)'
  umask 077
  printf 'TG_TOKEN=%s\nTG_CHAT=%s\nCHAT_SECRET=%s\nSITE=https://reconcept.ru\n' "$T" "$C" "$(openssl rand -hex 32)" > .env
fi
cp reconcept-chat.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q reconcept-chat
systemctl restart reconcept-chat
sleep 3
systemctl is-active reconcept-chat
echo "CHAT_SECRET для Coolify:"; grep CHAT_SECRET .env | cut -d= -f2
