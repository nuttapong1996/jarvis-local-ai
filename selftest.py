"""ทดสอบว่าน้องจางอ่านจอ คุมเมาส์ และคีย์บอร์ดได้จริงไหม — โดยไม่แตะงานของผู้ใช้เลย

เปิดหน้าต่างทดสอบของตัวเองกลางจอ (ป้าย + ช่องพิมพ์ + ปุ่ม) แล้วทดสอบกับหน้าต่างนี้เท่านั้น:
  1) สิทธิ์ที่ macOS ให้แอปที่รับผิดชอบ (ควรเป็น "Jarvis" เมื่อเปิดผ่าน ./run_app.sh --selftest)
  2) อ่านจอ: OCR ทั้งจอแล้วหาป้ายทดสอบ (รายงานแค่จำนวน ไม่บันทึกข้อความบนจอของผู้ใช้)
  3) เมาส์: ขยับไปที่ปุ่มแล้วคลิก — คลิกเฉพาะเมื่อหน้าต่างทดสอบอยู่บนสุดตรงจุดนั้นจริง
  4) คีย์บอร์ด: พิมพ์ไทย+อังกฤษ และ cmd+a — พิมพ์เฉพาะเมื่อหน้าต่างทดสอบเป็นหน้าต่างหลักอยู่
  5) Accessibility: อ่านรายการปุ่มของหน้าต่าง แล้วกดปุ่มโดยไม่ขยับเมาส์
จบแล้วคืนเมาส์ที่เดิมและปิดหน้าต่าง ผลเขียนลง logs/selftest.log
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import computer_agent as ca

LABEL = "JARVIS SELFTEST 4821"
BUTTON = "กดทดสอบ"
LOG = Path(__file__).resolve().parent / "logs" / "selftest.log"


def run_selftest() -> bool:
    import AppKit
    import ApplicationServices as AS
    import Quartz
    from Foundation import NSDate, NSObject

    lines: list[str] = []
    results: list[bool | None] = []

    def report(name: str, ok: bool | None, detail: str = "") -> None:
        results.append(ok)
        mark = "✅" if ok else "⏭ " if ok is None else "❌"
        lines.append(f"{mark} {name}{' — ' + detail if detail else ''}")
        print(lines[-1], flush=True)

    import objc
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    # เมนู Edit ให้ปุ่มลัดมาตรฐาน (cmd+a/c/v) ทำงานในช่องพิมพ์ — แอปทั่วไปมีเมนูนี้อยู่แล้ว
    menubar, edit_item = AppKit.NSMenu.alloc().init(), AppKit.NSMenuItem.alloc().init()
    edit = AppKit.NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (("Select All", "selectAll:", "a"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v")):
        edit.addItemWithTitle_action_keyEquivalent_(title, action, key)
    edit_item.setSubmenu_(edit)
    menubar.addItem_(edit_item)
    app.setMainMenu_(menubar)
    app.finishLaunching()
    seen_mouse: list[str] = []                        # เหตุการณ์เมาส์ที่หน้าต่างทดสอบได้รับจริง (ไว้วินิจฉัย)

    def pump(seconds: float) -> None:
        """ให้หน้าต่างทดสอบรับเหตุการณ์ (ไม่ได้เรียก app.run() จึงต้องดึงเหตุการณ์เอง)"""
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            ev = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                AppKit.NSEventMaskAny, NSDate.dateWithTimeIntervalSinceNow_(min(left, 0.05)),
                AppKit.NSDefaultRunLoopMode, True)
            if ev is not None:
                app.sendEvent_(ev)
        app.updateWindows()

    def in_thread(fn, timeout: float = 8.0):
        """เรียก Accessibility กับหน้าต่างของตัวเองต้องทำในเธรดอื่น (เธรดหลักต้องว่างไว้ตอบคำขอ)"""
        box: dict = {}
        th = threading.Thread(target=lambda: box.__setitem__("r", fn()), daemon=True)
        th.start()
        end = time.monotonic() + timeout
        while th.is_alive() and time.monotonic() < end:
            pump(0.05)
        return box.get("r")

    # ── 1) สิทธิ์ ──
    lines.append(f"🧪 ทดสอบการคุมเครื่อง {time.strftime('%Y-%m-%d %H:%M:%S')} · แอปที่ถือสิทธิ์: {ca.responsible_app()}")
    print(lines[-1], flush=True)
    ax_ok, screen_ok = ca.accessibility_ok(), bool(Quartz.CGPreflightScreenCaptureAccess())
    post_ok = bool(Quartz.CGPreflightPostEventAccess())
    report("สิทธิ์ Accessibility (อ่านปุ่ม/คุมเครื่อง)", ax_ok)
    report("สิทธิ์ Screen & System Audio Recording (อ่านจอ)", screen_ok)
    report("สิทธิ์ส่งเหตุการณ์เมาส์/คีย์บอร์ด", post_ok)

    # ── หน้าต่างทดสอบกลางจอ ลอยเหนือหน้าต่างอื่น ──
    clicks = [0]

    class SelfTestTarget(NSObject):
        def clicked_(self, sender):
            clicks[0] += 1

    class FirstMouseButton(AppKit.NSButton):
        def acceptsFirstMouse_(self, event):          # คลิกเดียวติด แม้หน้าต่างทดสอบยังไม่ใช่หน้าต่างหลัก
            return True

    class FirstMouseField(AppKit.NSTextField):
        def acceptsFirstMouse_(self, event):
            return True

    class TestPanel(AppKit.NSPanel):
        def sendEvent_(self, event):
            if event.type() in (AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeLeftMouseUp):
                seen_mouse.append("down" if event.type() == AppKit.NSEventTypeLeftMouseDown else "up")
            objc.super(TestPanel, self).sendEvent_(event)

        def canBecomeKeyWindow(self):
            return True

    target = SelfTestTarget.alloc().init()
    vis = AppKit.NSScreen.mainScreen().visibleFrame()
    w, h = 460, 230
    win = TestPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(vis.origin.x + (vis.size.width - w) / 2, vis.origin.y + (vis.size.height - h) / 2, w, h),
        AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskNonactivatingPanel,
        AppKit.NSBackingStoreBuffered, False)
    win.setFloatingPanel_(True)
    win.setBecomesKeyOnlyIfNeeded_(False)
    win.setTitle_("น้องจาง · ทดสอบคุมเครื่อง")
    win.setLevel_(AppKit.NSStatusWindowLevel + 1)
    win.setReleasedWhenClosed_(False)
    label = AppKit.NSTextField.labelWithString_(LABEL)
    label.setFont_(AppKit.NSFont.boldSystemFontOfSize_(26))
    label.setFrame_(AppKit.NSMakeRect(20, 160, 420, 40))
    field = FirstMouseField.alloc().initWithFrame_(AppKit.NSMakeRect(20, 100, 420, 30))
    button = FirstMouseButton.alloc().initWithFrame_(AppKit.NSMakeRect(20, 30, 200, 40))
    button.setTitle_(BUTTON)
    button.setBezelStyle_(AppKit.NSBezelStyleRounded)
    button.setTarget_(target)
    button.setAction_("clicked:")
    button.setFrame_(AppKit.NSMakeRect(20, 30, 200, 40))
    for v in (label, field, button):
        win.contentView().addSubview_(v)
    win.orderFrontRegardless()
    pump(0.8)

    top = AppKit.NSScreen.screens()[0].frame().size.height

    def center(view) -> tuple[float, float]:
        r = win.convertRectToScreen_(view.convertRect_toView_(view.bounds(), None))
        return r.origin.x + r.size.width / 2, top - (r.origin.y + r.size.height / 2)

    def topmost_pid(x: float, y: float) -> tuple[int | None, str]:
        """หน้าต่างบนสุดตรงจุดนี้เป็นของใคร (ลำดับจากหน้าไปหลัง)"""
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        for info in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            b = info.get("kCGWindowBounds") or {}
            if info.get("kCGWindowOwnerName") == "Window Server":    # ตัวชี้เมาส์เอง ไม่ใช่หน้าต่างที่บัง
                continue
            if info.get("kCGWindowAlpha", 1) > 0.01 and \
                    b.get("X", 0) <= x <= b.get("X", 0) + b.get("Width", 0) and \
                    b.get("Y", 0) <= y <= b.get("Y", 0) + b.get("Height", 0):
                return int(info["kCGWindowOwnerPID"]), str(info.get("kCGWindowOwnerName", ""))
        return None, ""

    def ours_on_top(x: float, y: float) -> bool:
        return topmost_pid(x, y)[0] == os.getpid()

    probe: list[int] = []
    monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
        AppKit.NSEventMaskFlagsChanged, lambda ev: (probe.append(1), ev)[1])

    def keyboard_is_ours() -> bool:
        """ลองกด Shift เปล่าๆ (ไม่มีผลกับแอปไหนเลย) แล้วดูว่าหน้าต่างทดสอบได้รับไหม
        ถามระบบว่าโฟกัสอยู่ที่ใครไม่พอ (เคยตอบว่าเป็นเรา แต่ปุ่มกลับไปโผล่ในแอปหน้าสุด) → ต้องพิสูจน์ด้วยปุ่มจริง"""
        probe.clear()
        down = Quartz.CGEventCreateKeyboardEvent(None, 56, True)
        Quartz.CGEventSetFlags(down, Quartz.kCGEventFlagMaskShift)
        up = Quartz.CGEventCreateKeyboardEvent(None, 56, False)
        Quartz.CGEventSetFlags(up, 0)
        ca._post(down)
        ca._post(up)
        pump(0.3)
        return bool(probe) and bool(win.isKeyWindow())

    def text_now() -> str:
        ed = field.currentEditor()
        return str(ed.string() if ed is not None else field.stringValue())

    orig = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    try:
        # ── 2) อ่านจอ ──
        if screen_ok:
            texts = ca.ocr_screen(max_items=600)
            found = any("4821" in t["text"] or "SELFTEST" in t["text"].upper() for t in texts)
            report("อ่านจอด้วย OCR (เจอป้ายของหน้าต่างทดสอบ)", found, f"อ่านได้ {len(texts)} ข้อความบนจอ (ไม่บันทึกเนื้อหา)")
        else:
            report("อ่านจอด้วย OCR", None, "ข้าม — ยังไม่มีสิทธิ์บันทึกหน้าจอ")

        # ── 3) เมาส์ + 4) คีย์บอร์ด ──
        bx, by = center(button)
        fx, fy = center(field)
        if not (ax_ok and post_ok):
            report("เมาส์และคีย์บอร์ด", None, "ข้าม — ยังไม่มีสิทธิ์ Accessibility")
        elif not ours_on_top(fx, fy):
            report("เมาส์และคีย์บอร์ด", None, f"ข้าม — หน้าต่างของ {topmost_pid(fx, fy)[1]} บังจุดทดสอบ (กันคลิกโดนงานของผู้ใช้)")
        else:
            ca.mouse_move(fx, fy)
            pump(0.25)
            pos = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            report("ขยับเมาส์", abs(pos.x - fx) < 3 and abs(pos.y - fy) < 3, f"ไปที่ ({fx:.0f}, {fy:.0f})")

            # คีย์บอร์ด: คลิกช่องพิมพ์ แล้วพิสูจน์ด้วย Shift ก่อนพิมพ์ทุกครั้ง — ไม่ผ่าน = ไม่พิมพ์เลย
            kb = False
            for _ in range(2):
                if not ours_on_top(fx, fy):
                    break
                ca.mouse_click(fx, fy)
                pump(0.4)
                if keyboard_is_ours():
                    kb = True
                    break
            if not kb:
                report("พิมพ์ด้วยคีย์บอร์ด", None, "ข้าม — ปุ่มทดสอบ (Shift) ไปไม่ถึงหน้าต่างทดสอบ จึงไม่พิมพ์ (กันพิมพ์ใส่แอปอื่น)")
            else:
                typed = "สวัสดีน้องจาง Jarvis 123"
                ca.type_unicode(typed)
                pump(0.6)
                report("พิมพ์ข้อความไทย+อังกฤษ", text_now() == typed, f"ได้ «{text_now()}»")
                if text_now() == typed and keyboard_is_ours():
                    ca.press_keys("cmd+a")
                    pump(0.3)
                    if keyboard_is_ours():
                        ca.type_unicode("OK")
                        pump(0.5)
                report("ปุ่มลัด cmd+a แล้วพิมพ์ทับ", text_now() == "OK", f"ได้ «{text_now()}»")

            # คลิกปุ่ม
            posted, stop_why = 0, ""
            for _ in range(3):
                if clicks[0]:
                    break
                if not ours_on_top(bx, by):
                    stop_why = f" · หยุดเพราะ {topmost_pid(bx, by)[1]} บังอยู่"
                    break
                ca.mouse_click(bx, by)
                posted += 1
                pump(0.5)
            report("คลิกเมาส์ (กดปุ่มในหน้าต่างทดสอบ)", clicks[0] > 0,
                   f"ปุ่มถูกกด {clicks[0]} ครั้ง · ส่งคลิก {posted} ครั้ง · หน้าต่างได้รับ {' '.join(seen_mouse) or 'ไม่มี'}{stop_why}")

        # ── 5) Accessibility: อ่านปุ่มแล้วกดโดยไม่ขยับเมาส์ ──
        if ax_ok:
            title, elements = in_thread(lambda: ca.ax_elements(os.getpid())) or ("", [])
            btn = next((e for e in elements if e["name"] == BUTTON), None)
            report("อ่านปุ่ม/ช่องผ่าน Accessibility", btn is not None, f"เจอ {len(elements)} ชิ้นในหน้าต่าง «{title}»")
            if btn is not None:
                before = clicks[0]
                in_thread(lambda: AS.AXUIElementPerformAction(btn["el"], "AXPress"))
                pump(0.3)
                report("กดปุ่มผ่าน Accessibility (ไม่ขยับเมาส์)", clicks[0] > before)
        else:
            report("อ่าน/กดปุ่มผ่าน Accessibility", None, "ข้าม — ยังไม่มีสิทธิ์")
    finally:
        if ax_ok and post_ok:
            ca.mouse_move(orig.x, orig.y)           # คืนเมาส์ที่เดิม
        AppKit.NSEvent.removeMonitor_(monitor)
        win.orderOut_(None)
        pump(0.2)

    passed, failed = results.count(True), results.count(False)
    skipped = results.count(None)
    lines.append(f"สรุป: ผ่าน {passed} · ไม่ผ่าน {failed} · ข้าม {skipped}")
    print(lines[-1], flush=True)
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return failed == 0 and skipped == 0
