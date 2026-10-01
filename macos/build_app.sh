#!/bin/zsh
# สร้าง Jarvis.app (แอป "น้องจาง") ไว้ที่โฟลเดอร์โปรเจกต์
# สิทธิ์ไมค์ · บันทึกหน้าจอ · Accessibility · Automation จะผูกกับแอปนี้ ไม่ใช่ Terminal หรือ Claude
# หมายเหตุ: build ใหม่ = ลายเซ็นเปลี่ยน macOS อาจถามสิทธิ์ใหม่ (ปกติไม่ต้อง build ซ้ำ ยกเว้นย้ายโฟลเดอร์โปรเจกต์)
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$PROJECT/Jarvis.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
clang -O2 -Wall -DPROJECT_DIR="\"$PROJECT\"" -o "$APP/Contents/MacOS/Jarvis" "$PROJECT/macos/launcher.c"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>          <string>local.nongjang.jarvis</string>
  <key>CFBundleName</key>                <string>น้องจาง</string>
  <key>CFBundleDisplayName</key>         <string>น้องจาง</string>
  <key>CFBundleExecutable</key>          <string>Jarvis</string>
  <key>CFBundlePackageType</key>         <string>APPL</string>
  <key>CFBundleShortVersionString</key>  <string>1.0</string>
  <key>CFBundleVersion</key>             <string>1</string>
  <key>LSMinimumSystemVersion</key>      <string>13.0</string>
  <key>LSUIElement</key>                 <true/>
  <key>NSHighResolutionCapable</key>     <true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>น้องจางต้องฟังเสียงเพื่อรับคำสั่งภาษาไทย (ถอดเสียงในเครื่อง ไม่ส่งเสียงออกไปไหน)</string>
  <key>NSAppleEventsUsageDescription</key>
  <string>น้องจางสั่งงานแอปอื่นตามที่คุณพูด เช่น เล่น/หยุดเพลง ปรับเสียง เปิดปิดแอป</string>
  <key>NSSpeechRecognitionUsageDescription</key>
  <string>น้องจางใช้ระบบถอดเสียงของ Apple ในเครื่อง</string>
</dict>
</plist>
PLIST

# ลายเซ็นแบบ ad-hoc (ไม่ต้องมีบัญชีนักพัฒนา) — macOS ใช้ลายเซ็นนี้จำว่าให้สิทธิ์แอปไหนไปแล้ว
codesign --force --sign - --identifier local.nongjang.jarvis "$APP"
echo "✅ สร้าง $APP แล้ว"
