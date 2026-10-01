"""สร้างภาพประกอบ README (docs/images/*.png) — วาดด้วย HTML แล้วถ่ายด้วย Chrome แบบไม่มีหน้าต่าง

    .venv/bin/python docs/make_images.py

ภาพหน้าจอ J.A.R.V.I.S มาจาก hud.html ตัวจริง (ตัวเดียวกับที่ลอยบนจอตอนใช้งาน)
"""
from __future__ import annotations

import base64
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
FONT = "'Sukhumvit Set', 'Thonburi', 'Helvetica Neue', sans-serif"

STATES = [  # (สถานะ, ชื่อไทย, ตัวอย่างที่ได้ยิน, ตัวอย่างที่ตอบ)
    ("listen", "ฟังอยู่", "น้องจาง", ""),
    ("think", "กำลังคิด", "น้องจาง หยุดเพลงแล้วเปิดสแล็ก", ""),
    ("speak", "กำลังพูด", "น้องจาง ขอบคุณนะ", "ยินดีเลย"),
    ("web", "ท่องเว็บ", "น้องจาง หาโรงแรมแถวเชียงใหม่", "แป๊บนึงนะ กำลังหา"),
    ("control", "คุมเครื่อง", "น้องจาง เปิดโน้ตแล้วสร้างโน้ตใหม่", "โอเค ขอคุมเครื่องแป๊บนะ"),
]

BASE_CSS = f"""
* {{ box-sizing: border-box; margin: 0; }}
body {{ font-family: {FONT}; color: #e8f7ff; background: transparent; }}
.card {{ background: radial-gradient(120% 140% at 80% 10%, #10324a 0%, #07131f 55%, #050b12 100%);
        border-radius: 28px; padding: 48px 56px; border: 1px solid #1b4660; }}
.cyan {{ color: #36e3ff; }}
"""


def png_data(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def shoot_hud(page, state: str, heard: str, reply: str) -> Path:
    """ถ่ายหน้าจอ J.A.R.V.I.S จริงในสถานะที่กำหนด (พื้นหลังโปร่งใส)"""
    page.set_viewport_size({"width": 380, "height": 450})
    page.goto((ROOT / "hud.html").as_uri())
    level = {"speak": 0.09, "listen": 0.05}.get(state, 0.03)
    page.evaluate(f"hud.update({{state: '{state}', level: {level}, heard: {heard!r}, reply: {reply!r}, pinned: false}})")
    page.wait_for_timeout(1600)                     # ให้วงแหวนหมุน/แท่งเสียงขยับก่อนถ่าย
    out = OUT / f"hud_{state}.png"
    page.screenshot(path=str(out), omit_background=True)
    return out


def render(page, html: str, width: int, name: str) -> None:
    page.set_viewport_size({"width": width, "height": 400})
    page.set_content(f"<html><head><meta charset='utf-8'><style>{BASE_CSS}</style></head><body>{html}</body></html>")
    page.wait_for_timeout(300)
    page.locator(".card").screenshot(path=str(OUT / name), omit_background=True)
    print("✅", OUT / name)


def banner(page, hud: Path) -> None:
    chips = "".join(f"<span class='chip'>{c}</span>" for c in
                    ("🎙️ ฟังตลอด ไม่ต้องกดปุ่ม", "⚡ ตอบไวด้วย Jev", "🖱️ เปิดแอป ค้นเว็บ คุมเมาส์", "🔒 ทำงานในเครื่องเป็นหลัก"))
    render(page, f"""
    <style>
      .card {{ width: 1200px; display: flex; align-items: center; gap: 24px; padding: 40px 48px; }}
      h1 {{ font-size: 62px; line-height: 1.15; font-weight: 700; white-space: nowrap; }}
      .sub {{ font-size: 30px; margin-top: 10px; color: #bfe9ff; }}
      .chips {{ margin-top: 30px; display: flex; flex-wrap: wrap; gap: 12px; }}
      .chip {{ font-size: 22px; padding: 9px 18px; border-radius: 999px; background: #0d2b3e;
               border: 1px solid #2a6a8c; color: #dff6ff; }}
      img {{ width: 380px; flex: none; }}
    </style>
    <div class="card">
      <div style="flex:1">
        <h1>น้องจาง <span class="cyan">·</span> J.A.R.V.I.S ไทย</h1>
        <div class="sub">ผู้ช่วยเสียงภาษาไทยบน Mac — เรียก "น้องจาง" แล้วสั่งงานหรือคุยเล่นได้เหมือนเพื่อน</div>
        <div class="chips">{chips}</div>
      </div>
      <img src="{png_data(hud)}">
    </div>""", 1300, "banner.png")


def states(page, huds: dict[str, Path]) -> None:
    cells = "".join(f"""
      <div class="cell"><img src="{png_data(huds[s])}"><div class="name">{th}</div></div>"""
                    for s, th, _, _ in STATES)
    render(page, f"""
    <style>
      .card {{ width: 1500px; padding: 36px 40px; }}
      h2 {{ font-size: 34px; margin-bottom: 18px; }}
      .row {{ display: flex; justify-content: space-between; }}
      .cell {{ width: 270px; text-align: center; }}
      .cell img {{ width: 270px; }}
      .name {{ font-size: 26px; margin-top: 4px; color: #dff6ff; }}
      .note {{ font-size: 21px; color: #9cc7dc; margin-top: 18px; }}
    </style>
    <div class="card">
      <h2>หน้าจอ <span class="cyan">J.A.R.V.I.S</span> เปลี่ยนสีตามสิ่งที่น้องจางกำลังทำ</h2>
      <div class="row">{cells}</div>
      <div class="note">ลากย้ายได้ · ดับเบิลคลิก = หยุด/เริ่มฟัง · คลิกขวา = เมนู · ปักหมุดให้ลอยบนทุกหน้าจอได้</div>
    </div>""", 1600, "states.png")


def flow(page) -> None:
    def box(icon, title, sub, cls=""):
        return f"<div class='box {cls}'><div class='ic'>{icon}</div><div class='t'>{title}</div><div class='s'>{sub}</div></div>"
    top = (box("🎙️", "1. พูด", "\"น้องจาง เปิดยูทูบ\"") + "<div class='arr'>➜</div>" +
           box("📝", "2. ถอดเสียงไทย", "ตัวถอดเสียงของ Apple<br>ในเครื่อง ~0.1 วินาที") + "<div class='arr'>➜</div>" +
           box("🧠", "3. Jev ตัดสินใจ", "เป็นคำสั่งไหม ทำอะไร แอปไหน<br>ถามครั้งเดียว ~0.4 วินาที", "jev"))
    kinds = (box("⚙️", "สั่งเครื่อง", "เปิด/ปิดแอป · เสียง · เพลง<br>โหมดมืด · ล็อกจอ · บอกเวลา") +
             box("💬", "คุยเล่น", "ทักทาย ขอบคุณ บ่นเหนื่อย<br>ตอบทันทีจากชุดคำตอบ") +
             box("🤖", "ตอบคำถาม", "AI ในเครื่อง (Typhoon)<br>ไม่ส่งข้อมูลออกไปไหน") +
             box("🌐", "ค้นเว็บ", "เปิด Chrome หา ข่าว ราคา<br>โรงแรม เพลงในยูทูบ") +
             box("🖱️", "คุมเครื่อง", "อ่านจอ · คลิก · พิมพ์<br>Jev เลือกทีละขั้น"))
    render(page, f"""
    <style>
      .card {{ width: 1500px; padding: 40px 44px; }}
      h2 {{ font-size: 36px; margin-bottom: 26px; }}
      .row {{ display: flex; align-items: stretch; justify-content: center; gap: 14px; }}
      .box {{ background: #0c2536; border: 1px solid #2a6a8c; border-radius: 20px; padding: 18px 20px; text-align: center; flex: 1; }}
      .box.jev {{ border-color: #36e3ff; box-shadow: 0 0 22px #36e3ff55; }}
      .ic {{ font-size: 44px; }}
      .t {{ font-size: 27px; font-weight: 700; margin-top: 6px; }}
      .s {{ font-size: 19px; color: #a9d4e8; margin-top: 6px; line-height: 1.45; }}
      .arr {{ font-size: 40px; color: #36e3ff; align-self: center; }}
      .down {{ text-align: center; font-size: 38px; color: #36e3ff; margin: 10px 0; }}
      .end {{ margin: 0 auto; max-width: 760px; border-color: #8fc8ff; }}
    </style>
    <div class="card">
      <h2>น้องจางทำงานยังไง</h2>
      <div class="row">{top}</div>
      <div class="down">⬇ แยกงานตามที่ Jev ตัดสิน ⬇</div>
      <div class="row">{kinds}</div>
      <div class="down">⬇</div>
      {box("🔊", "4. ตอบด้วยเสียง", "เสียงผู้ชาย พูดเร็ว · พูดแทรกได้ · คุยต่อได้ 30 วินาทีโดยไม่ต้องเรียกชื่อซ้ำ", "end")}
    </div>""", 1600, "flow.png")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(device_scale_factor=1.5)
        huds = {s: shoot_hud(page, s, heard, reply) for s, _, heard, reply in STATES}
        banner(page, huds["speak"])
        states(page, huds)
        flow(page)
        browser.close()
    for f in OUT.glob("hud_*.png"):              # ใช้แค่ประกอบภาพใหญ่ ไม่ต้องเก็บ
        f.unlink()


if __name__ == "__main__":
    main()
