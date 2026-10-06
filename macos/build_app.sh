#!/bin/zsh
# สร้าง Jarvis.app (แอป "น้องจาง") ไว้ที่โฟลเดอร์โปรเจกต์
# สิทธิ์ไมค์ · บันทึกหน้าจอ · Accessibility · Automation จะผูกกับแอปนี้ ไม่ใช่ Terminal หรือ Claude
# หมายเหตุ: build ใหม่ = ลายเซ็นเปลี่ยน macOS อาจถามสิทธิ์ใหม่ (ปกติไม่ต้อง build ซ้ำ ยกเว้นย้ายโฟลเดอร์โปรเจกต์)
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$PROJECT/Jarvis.app"
ICON_SOURCE="$PROJECT/macos/assets/JarvisIcon.png"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/Jarvis.iconset"
clang -O2 -Wall -DPROJECT_DIR="\"$PROJECT\"" -o "$APP/Contents/MacOS/Jarvis" "$PROJECT/macos/launcher.c"
clang -O2 -Wall -framework AppKit -o "$APP/Contents/Resources/.render_icon" "$PROJECT/macos/render_icon.m"
clang -O2 -Wall -o "$APP/Contents/Resources/.make_icns" "$PROJECT/macos/make_icns.c"

# สร้างไอคอน macOS ทุกขนาดจาก master PNG เพื่อให้คมทั้ง Dock, Finder และ Launchpad
for size in 16 32 128 256 512; do
  "$APP/Contents/Resources/.render_icon" "$ICON_SOURCE" "$size" "$APP/Contents/Resources/Jarvis.iconset/icon_${size}x${size}.png"
  double_size=$((size * 2))
  "$APP/Contents/Resources/.render_icon" "$ICON_SOURCE" "$double_size" "$APP/Contents/Resources/Jarvis.iconset/icon_${size}x${size}@2x.png"
done
"$APP/Contents/Resources/.make_icns" "$APP/Contents/Resources/Jarvis.icns" \
  "$APP/Contents/Resources/Jarvis.iconset/icon_16x16.png" \
  "$APP/Contents/Resources/Jarvis.iconset/icon_32x32.png" \
  "$APP/Contents/Resources/Jarvis.iconset/icon_128x128.png" \
  "$APP/Contents/Resources/Jarvis.iconset/icon_256x256.png" \
  "$APP/Contents/Resources/Jarvis.iconset/icon_512x512.png" \
  "$APP/Contents/Resources/Jarvis.iconset/icon_512x512@2x.png"
rm -rf "$APP/Contents/Resources/Jarvis.iconset"
rm "$APP/Contents/Resources/.render_icon" "$APP/Contents/Resources/.make_icns"

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
  <key>CFBundleIconFile</key>             <string>Jarvis</string>
  <key>CFBundleShortVersionString</key>  <string>1.0</string>
  <key>CFBundleVersion</key>             <string>1</string>
  <key>LSMinimumSystemVersion</key>      <string>13.0</string>
  <!-- แสดงใน Dock เพื่อให้เปิด/สลับ/ปิดได้เหมือนแอปทั่วไป
       (ไอคอนไมค์บนแถบเมนูยังใช้งานได้ตามเดิม) -->
  <key>LSUIElement</key>                 <false/>
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
