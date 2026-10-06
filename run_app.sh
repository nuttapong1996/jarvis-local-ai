#!/bin/zsh
# เปิดJarvisเป็นแอปของตัวเอง แล้วโชว์ log สดในเทอร์มินัลนี้ (Ctrl+C = ปิดJarvis)
# ใช้แทน `python jarvis.py` เมื่ออยากให้สิทธิ์ไมค์/อ่านจอ/คุมเครื่อง ผูกกับแอป "Jarvis" แทนแอปเทอร์มินัล
cd "$(dirname "$0")"
[[ -x Jarvis.app/Contents/MacOS/Jarvis ]] || ./macos/build_app.sh
mkdir -p logs
: >> logs/jarvis.log
APP="${PWD:A}/Jarvis.app"                               # :A = ที่อยู่จริง (ไม่ผ่าน symlink)
LAUNCHER="$APP/Contents/MacOS/Jarvis"
LPAT="^$(printf '%s' "$LAUNCHER" | sed 's/[][\\.*^$+?(){}|]/\\&/g')"   # ใช้กับ pgrep/pkill แบบไม่ตีความ . ( ) เป็น regex

# ./run_app.sh --selftest = ทดสอบอ่านจอ/เมาส์/คีย์บอร์ดด้วยสิทธิ์ของแอปJarvis (เปิดอีกตัวชั่วคราว ไม่ยุ่งตัวที่ฟังอยู่)
if [[ "$1" == "--selftest" ]]; then
  rm -f logs/selftest.log
  open -n -W --env JARVIS_SELFTEST=1 "$APP"
  cat logs/selftest.log 2>/dev/null || { echo "ไม่มีผลทดสอบ ดู logs/jarvis.log"; tail -20 logs/jarvis.log; exit 1; }
  ! grep -q "^❌" logs/selftest.log
  exit $?
fi

TAIL=
stop() {
  [[ -n $TAIL ]] && kill $TAIL 2>/dev/null
  pkill -TERM -f "$LPAT" 2>/dev/null
  echo "\n👋 ปิดJarvisแล้ว"
  exit 0
}
trap stop INT TERM HUP

if pgrep -f "$LPAT" >/dev/null; then
  echo "Jarvisเปิดอยู่แล้ว — แสดง log ต่อ"
else
  open "$APP"
fi
tail -n 0 -F logs/jarvis.log &
TAIL=$!
# ถ้าJarvisปิดเอง (เช่น เลือก "ออก" จากเมนูบาร์) ก็เลิกโชว์ log ด้วย
sleep 3
while pgrep -f "$LPAT" >/dev/null; do sleep 1; done
kill $TAIL 2>/dev/null
echo "\n👋 Jarvisปิดแล้ว"
