#!/usr/bin/env python3
"""
Jarvis ไทย — "น้องจาง" ผู้ช่วยสั่งงาน macOS ด้วยเสียงภาษาไทย แบบ full duplex
(ฟังตลอดเวลา ไม่ต้องกดปุ่ม เรียก "น้องจาง" แล้วสั่งได้เลย พูดแทรกตอนน้องจางกำลังพูดได้)

ท่อการทำงาน
  ไมค์ ─► ตัดเสียงสะท้อน (AEC) ─► VAD ตัดประโยค ─► Whisper ในเครื่อง (ภาษาไทย) ─► ได้ยิน "น้องจาง" ไหม
       ─► Local LLM ใน LM Studio ตัดสินใจและตอบคำถาม
       ─► สั่ง macOS (osascript/shell) | ท่องเว็บด้วย Playwright (browser_agent.py) | ถาม Local LLM
       ─► พูดตอบด้วย say -v Kanya (เล่นผ่านลำโพงเอง จึงหยุดได้ทันทีเมื่อถูกพูดแทรก)

วิธีใช้
  python jarvis.py                          ฟังไมค์ (full duplex) เรียก "น้องจาง ..." ก่อนสั่ง
  python jarvis.py --text "เปิดสปอติฟาย"      สั่งด้วยข้อความ
  python jarvis.py --dry-run                ไม่สั่งเครื่องจริง แค่ print ว่าจะทำอะไร
  python jarvis.py --eval                   วัดความแม่นกับชุดประโยคทดสอบภาษาไทย
  python jarvis.py --wav file.wav           ป้อนไฟล์เสียงแทนไมค์ (ไว้ทดสอบ VAD + STT)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import random
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv

# ════════════════════════════════════════════════════════════════════════════
# 1) ปรับแต่งได้ง่าย: แอป / คำสั่ง / ระดับเสียง / ประโยคตอบ
#    คำอธิบายเขียนเป็นอังกฤษ (Local AI ถนัดสุด) แล้วแนบคำไทยที่คนใช้เรียกไว้ในวงเล็บ
# ════════════════════════════════════════════════════════════════════════════

# "ชื่อแอปจริง (ใช้กับ open -a)": ("ชื่อที่ Jarvis พูด", "คำอธิบายให้ Local AI")
APPS = {
    "Spotify":            ("สปอติฟาย", "สปอติฟาย"),
    "Google Chrome":      ("กูเกิลโครม", "โครม"),
    "Safari":             ("ซาฟารี", "ซาฟารี"),
    "LINE":               ("ไลน์", "ไลน์"),
    "Slack":              ("สแล็ก", "สแล็ก"),
    "Visual Studio Code": ("วีเอสโค้ด", "VS Code, วีเอสโค้ด"),
    "Terminal":           ("เทอร์มินัล", "เทอร์มินัล"),
    "Finder":             ("ไฟน์เดอร์", "ไฟน์เดอร์"),
    "Notes":              ("โน้ต", "โน้ต"),
    "Calendar":           ("ปฏิทิน", "ปฏิทิน"),
    "Mail":               ("เมล", "เมล"),
    "Calculator":         ("เครื่องคิดเลข", "เครื่องคิดเลข"),
    "System Settings":    ("การตั้งค่าระบบ", "ตั้งค่า"),
    "none":               ("", "No app or website is named"),
}

# เว็บยอดนิยม: "เปิดยูทูบ" = เปิดเว็บตรงๆ เลย (เร็วกว่าและไม่เปลือง token เท่าให้เอเจนต์ท่องเว็บ)
# "ชื่อ": ("ชื่อที่น้องจางพูด", "คำอธิบายให้ Local AI", "URL")
SITES = {
    "YouTube":     ("ยูทูบ", "ยูทูบ", "https://www.youtube.com"),
    "Facebook":    ("เฟซบุ๊ก", "เฟซบุ๊ก, เฟส", "https://www.facebook.com"),
    "Gmail":       ("จีเมล", "จีเมล", "https://mail.google.com"),
    "Google Maps": ("กูเกิลแมป", "กูเกิลแมป, แผนที่", "https://www.google.com/maps"),
    "Shopee":      ("ช้อปปี้", "ช้อปปี้", "https://shopee.co.th"),
    "Lazada":      ("ลาซาด้า", "ลาซาด้า", "https://www.lazada.co.th"),
    "Netflix":     ("เน็ตฟลิกซ์", "เน็ตฟลิกซ์", "https://www.netflix.com"),
}

# คำสั่งที่ Jarvis ทำได้: ชื่อ action → คำอธิบายให้ Local AI
ACTIONS = {
    "open_app":       "Open or switch to a named app or a popular website like YouTube (Thai: เปิด..., ขอ..., เข้า...). "
                      "Not for music, sound or searching",
    "quit_app":       "Quit or close a named app (Thai: ปิด..., ออกจาก...)",
    "volume_up":      "Make the Mac louder or unmute it (Thai: เพิ่มเสียง, ดังขึ้น, เปิดเสียง)",
    "volume_down":    "Make the Mac quieter (Thai: ลดเสียง, เบาเสียง)",
    "volume_mute":    "Mute all sound (Thai: ปิดเสียง, มิวต์)",
    "volume_set":     "Set the volume to a stated level (Thai: ตั้งเสียง..., เสียงครึ่งหนึ่ง, ดังสุด)",
    "music_play":     "Play or resume music (Thai: เล่นเพลง, เปิดเพลง, เล่นต่อ)",
    "music_pause":    "Pause or stop music (Thai: หยุดเพลง, ปิดเพลง)",
    "music_next":     "Skip to the next song (Thai: เพลงถัดไป, ข้ามเพลง)",
    "music_previous": "Go back to the previous song (Thai: เพลงก่อนหน้า, ย้อนเพลง)",
    "dark_mode":      "Toggle dark or light mode (Thai: โหมดมืด, โหมดสว่าง)",
    "lock_screen":    "Lock the screen (Thai: ล็อกจอ)",
    "sleep":          "Put the Mac to sleep (Thai: พักเครื่อง, สลีป)",
    "tell_time":      "Tell the current time (Thai: กี่โมงแล้ว)",
    "screenshot":     "Take a screenshot (Thai: แคปหน้าจอ)",
    "web_task":       "Search or look up live info on the web: places, prices, reviews, weather, news, or find something "
                      "inside a website (Thai: หา..., ค้นหา..., เช็คราคา..., หาเพลง...ในยูทูบ)",
    "read_screen":    "Read or describe what is on the screen right now (Thai: อ่านจอ, บนจอมีอะไร, หน้าจอเขียนว่าอะไร)",
    "computer_task":  "Operate the Mac screen directly in any app: click a button, type into an app, scroll, move the "
                      "mouse, use a menu or shortcut (Thai: คลิก..., กดปุ่ม..., พิมพ์ว่า..., เลื่อนลง, เลื่อนเมาส์, กดเซฟ)",
    "stop_talking":   "Tell the assistant itself to stop talking or cancel what it is doing "
                      "(Thai: หยุดพูด, เงียบก่อน, พอแล้ว, ยกเลิก, ไม่ต้องหาแล้ว). Not about music",
    "none":           "No Mac action: a general question, chit-chat, or noise",
}

# ประเภทของประโยค (คำถามแรกของ fan-out)
CATEGORIES = {
    "command": "The speaker tells the assistant to do something on this Mac or on the web, including polite "
               "requests (Thai: เปิด..., ปิด..., เพิ่มเสียง, หยุดเพลง, ล็อกจอ, กี่โมงแล้ว, หาห้องพักให้หน่อย, "
               "ค้นหา..., ช่วย...ให้หน่อย, ...ได้ไหม)",
    "question": "The speaker asks a general-knowledge question or chats with the assistant, and it can be "
                "answered from general knowledge without searching the web or doing anything on the Mac "
                "(Thai: ...คืออะไร, ทำไม..., อย่างไร, ช่วยอธิบาย, เล่าให้ฟังหน่อย, สวัสดี)",
    "noise": "Not meant for the assistant: speech addressed to another person (e.g. calling แม่, พ่อ, พี่, ลูก "
             "and asking them something), TV or video audio, filler sounds (อืม, เอ่อ), cut-off words, "
             "or speech-recognition junk such as 'ขอบคุณที่รับชม' or 'ซับไตเติ้ลโดย'",
}

# ระดับเสียงสำหรับ Score: (เปอร์เซ็นต์ที่ตั้งจริง, คำอธิบายให้ Local AI)
# ระดับเสียงเริ่มที่ 10% ส่วน "ปิดเสียง" ใช้ action volume_mute
VOLUME_LEVELS = [
    (10, "10 percent (สิบ)"), (20, "20 percent"), (30, "30 percent"), (40, "40 percent"),
    (50, "50 percent, half (ครึ่ง)"), (60, "60 percent"), (70, "70 percent"), (80, "80 percent"),
    (90, "90 percent"), (100, "100 percent, maximum (ดังสุด, เต็ม)"),
]

# ประโยคตอบสำเร็จรูป (สุ่มหนึ่งแบบ) คุยแบบเพื่อน สั้นๆ ไวๆ · {app} = ชื่อแอปภาษาไทย, {vol} = เปอร์เซ็นต์เสียง
REPLIES = {
    "open_app":       ["ได้เลย เปิด{app}ให้แล้ว", "จัดไป {app}มาแล้ว", "โอเค {app}เปิดแล้วนะ"],
    "quit_app":       ["ปิด{app}ให้แล้ว", "โอเค ปิด{app}แล้วนะ"],
    "volume_up":      ["ดังขึ้นละ {vol} เปอร์เซ็นต์", "เพิ่มให้แล้ว {vol} เปอร์เซ็นต์นะ"],
    "volume_down":    ["เบาลงละ เหลือ {vol} เปอร์เซ็นต์", "ลดให้แล้ว {vol} เปอร์เซ็นต์นะ"],
    "volume_mute":    ["ปิดเสียงนะ", "เงียบละนะ"],
    "volume_set":     ["เสียง {vol} เปอร์เซ็นต์ละ", "ตั้งไว้ {vol} เปอร์เซ็นต์แล้วนะ"],
    "music_play":     ["เปิดเพลงละ", "จัดเพลงให้เลย"],
    "music_pause":    ["หยุดเพลงแล้ว", "พักเพลงไว้ก่อนนะ"],
    "music_next":     ["เพลงต่อไปเลย", "ข้ามให้ละ"],
    "music_previous": ["ย้อนให้แล้ว", "กลับไปเพลงเมื่อกี้ละ"],
    "dark_mode":      ["สลับธีมให้แล้ว", "เปลี่ยนโหมดจอให้ละ"],
    "lock_screen":    ["ล็อกจอนะ", "ล็อกให้เลย"],
    "sleep":          ["พักเครื่องนะ", "ไปพักก่อนนะ"],
    "screenshot":     ["แคปจอเก็บไว้บนเดสก์ท็อปแล้ว", "แคปให้ละนะ"],
    "web_task":       ["ได้ เดี๋ยวหาให้", "แป๊บนึงนะ กำลังหา", "โอเค ขอค้นแป๊บ"],
    "read_screen":    ["แป๊บนะ ขอดูจอก่อน", "ขออ่านจอแป๊บ"],
    "computer_task":  ["ได้ เดี๋ยวจัดการให้", "โอเค ขอคุมเครื่องแป๊บนะ"],
    "none":           ["อันนี้เรายังทำไม่เป็นอะ"],
    # ── ประโยคของระบบ ──
    "ready":          ["น้องจางมาแล้ว เรียกได้เลย", "พร้อมละ ว่าไงดี"],
    "wake":           ["ว่าไง", "ครับ", "ว่ามาเลย"],
    "which_app":      ["เปิดแอปไหนนะ หาชื่อนั้นไม่เจออะ", "แอปอะไรนะ ขอชื่ออีกทีได้ไหม"],
    "retry":          ["เมื่อกี้ว่าไงนะ", "ไม่ค่อยชัดอะ ขออีกทีได้ไหม", "ขอโทษ ไม่ทันได้ยิน พูดอีกรอบนะ"],
    "skip":           ["งั้นข้ามก่อนนะ", "ยังจับไม่ได้ ข้ามไปก่อนละกัน"],
    "fail":           ["อ้าว ทำไม่สำเร็จอะ", "ขอโทษ มีอะไรผิดพลาดนิดนึง"],
    "no_llm":         ["ยังไม่ได้ตั้งค่าสมองส่วนคุยเลยอะ ตอบไม่ได้"],
    "llm_error":      ["ขอโทษ ตอนนี้คิดไม่ออก ลองใหม่อีกทีนะ"],
    "llm_quota":      ["โควตาของแอลแอลเอ็มหมดแล้วอะ ไว้ลองใหม่นะ"],
    "router_error":      ["ขอโทษ ระบบตัดสินใจขัดข้อง ลองใหม่อีกทีนะ"],
    "web_cancel":     ["โอเค หยุดละ", "ได้ ไม่ทำต่อแล้ว"],
    "web_fail":       ["หาไม่สำเร็จอะ ลองใหม่อีกทีนะ"],
    "web_no_llm":     ["ต้องตั้งค่าสมองส่วนคุยก่อน ถึงจะท่องเว็บให้ได้"],
}

# คุยเล่นแบบที่ตอบได้ทันที: Local AI จัดประเภทไปพร้อมกับการตัดสินใจรอบแรก (ไม่เพิ่มเวลา) แล้วตอบจากชุดนี้
# ไม่ต้องรอ LLM ในเครื่อง (~0.7 วิ) · อะไรที่ต้องคิด/ต้องใช้ข้อมูล ให้เป็น none → LLM ตอบเหมือนเดิม
CHAT_INTENTS = {
    "greeting":    "Says hi or checks the assistant is there (สวัสดี, หวัดดี, ว่าไง, อยู่ไหม, ตื่นยัง)",
    "thanks":      "Thanks the assistant (ขอบคุณ, ขอบใจ, แต๊งกิ้ว, ขอบคุณมาก)",
    "praise":      "Praises the assistant (เก่งมาก, เยี่ยม, สุดยอด, ดีมาก, เก่งจัง)",
    "complaint":   "Complains the assistant is slow, wrong, useless or dumb (ช้าจัง, ทำไมไม่ทำ, ไม่ได้เรื่อง, โง่)",
    "how_are_you": "Asks how the assistant is doing (เป็นไงบ้าง, สบายดีไหม, วันนี้เป็นไง)",
    "who_are_you": "Asks who or what the assistant is, or its name (เป็นใคร, ชื่ออะไร, เป็นคนหรือเปล่า)",
    "abilities":   "Asks what the assistant can do or how to use it (ทำอะไรได้บ้าง, ช่วยอะไรได้บ้าง, ใช้ยังไง)",
    "tired":       "Says they are tired or sleepy (เหนื่อย, ง่วง, เพลีย, อยากนอน)",
    "hungry":      "Says they are hungry (หิว, หิวข้าว)",
    "bored":       "Says they are bored (เบื่อ, เซ็ง, ไม่มีอะไรทำ)",
    "sad":         "Says they feel sad, stressed or down (เศร้า, เครียด, ท้อ, ไม่สบายใจ)",
    "good_news":   "Shares that they are happy or something good happened (ดีใจ, สำเร็จแล้ว, ได้งานแล้ว)",
    "laugh":       "Laughs or finds something funny (555, ฮ่าๆ, ขำ)",
    "sorry":       "Apologizes to the assistant (ขอโทษ, โทษที)",
    "wait":        "Asks the assistant to wait or hold on (เดี๋ยวนะ, รอแป๊บ, แป๊บนึง)",
    "ok":          "Just acknowledges with no new request (โอเค, ได้, อืม, เข้าใจแล้ว, ไม่เป็นไร)",
    "retry":       "Tells the assistant to try the previous task again or go ahead with it now "
                   "(ลองดูสิ, ลองใหม่, ลองอีกที, ทำเลย, เอาเลย, ลองเล่นดูสิ)",
    "goodbye":     "Says goodbye or good night (ไปก่อนนะ, บาย, ฝันดี, ราตรีสวัสดิ์)",
    "none":        "Anything else: a real question that needs facts or an explanation, a request, an answer to "
                   "something the assistant asked, or talk not directed at the assistant",
}
CHAT_REPLIES = {
    "greeting":    ["ว่าไง อยู่นี่ละ", "หวัดดี มีอะไรให้ช่วยไหม", "ว่าไงนาย"],
    "thanks":      ["ยินดีเลย", "ไม่เป็นไรเลย", "ได้เสมอ"],
    "praise":      ["ขอบคุณนะ ดีใจเลย", "เขินเลยอะ ขอบใจนะ"],
    "complaint":   ["ขอโทษนะ เดี๋ยวเราปรับให้ดีขึ้น บอกอีกทีได้ไหมว่าอยากให้ทำอะไร",
                    "ขอโทษจริงๆ ลองบอกใหม่อีกทีนะ เราจะตั้งใจฟัง"],
    "how_are_you": ["สบายดี พร้อมช่วยอยู่ นายล่ะเป็นไงบ้าง", "ดีเลย วันนี้นายเป็นไงบ้าง"],
    "who_are_you": ["เราน้องจาง ผู้ช่วยบนเครื่องแมคของนายไง", "น้องจางไง เพื่อนที่คอยช่วยใช้คอมให้"],
    "abilities":   ["เปิดปิดแอป ปรับเสียง คุมเพลง หาของในเว็บ อ่านจอ แล้วก็คลิกหรือพิมพ์ให้ได้ เรียกน้องจางแล้วสั่งมาเลย"],
    "tired":       ["พักบ้างนะ ดื่มน้ำด้วย", "เหนื่อยก็พักก่อน เดี๋ยวค่อยลุยต่อ"],
    "hungry":      ["หาอะไรกินก่อนเลย", "ไปกินข้าวก่อนนะ เดี๋ยวค่อยลุยต่อ"],
    "bored":       ["เปิดเพลงฟังไหม", "พักสายตาแป๊บนึงก็ได้นะ"],
    "sad":         ["เราอยู่ตรงนี้นะ อยากเล่าก็เล่าได้", "ไม่เป็นไรนะ ค่อยๆ ไป"],
    "good_news":   ["ดีใจด้วยเลย", "เยี่ยมไปเลย ยินดีด้วยนะ"],
    "laugh":       ["ขำด้วยเลย", "ฮ่าๆ"],
    "sorry":       ["ไม่เป็นไรเลย", "สบายมาก ไม่ต้องขอโทษ"],
    "wait":        ["ได้ รออยู่นะ", "โอเค ไม่รีบ"],
    "ok":          ["โอเค", "ได้เลย"],
    "retry":       ["ลองอะไรดี บอกคำสั่งมาได้เลย"],
    "goodbye":     ["ไว้เจอกันนะ", "บาย พักผ่อนเยอะๆ"],
}
CHAT_CONF = 0.7                       # Local AI มั่นใจเท่านี้ถึงตอบจากชุดคุยเล่น ไม่งั้นให้ LLM คิดเอง
# บอกอารมณ์พร้อมถามด้วย ("หิวจัง กินอะไรดี") → ให้ LLM ตอบคำถาม ไม่ใช่แค่ปลอบ
MOOD_INTENTS = {"tired", "hungry", "bored", "sad"}
ASKS_RE = re.compile(r"อะไร|ยังไง|อย่างไร|ทำไม|ไหม|มั้ย|หรือเปล่า|กี่|ที่ไหน|เท่าไ|ช่วย|แนะนำ|ดี\s*$")

# คำใบ้ให้ Whisper: ประโยคไทยปนชื่อแอปอังกฤษ ช่วยให้สะกดชื่อแอปและคำปลุกถูก
WHISPER_PROMPT = "น้องจาง เปิด Spotify ปิด LINE เปิด Slack เปิด Google Chrome เปิด VS Code เพิ่มเสียง ลดเสียง หยุดเพลง หาห้องพัก กี่โมงแล้ว"

# รูปที่ Whisper มักถอดคำว่า "น้องจาง" ออกมา: น้องจาง น้อง จาง น้องจ้าง น้องจ๋าง น้องจาน น้องจัง น้องจาก นองจ่าง
# ต้องมีตัวสะกด ง/น (หรือ ก ที่จบคำ) เสมอ กันคำทั่วไปอย่าง "น้องจ๋า", "น้องจัด", "น้องจับ" ปลุกผิด
NONG_JANG_RE = r"(?:น้?อง|ด้อง|ท่าน)[\s,]*จ[่้๊๋]?[ัา][่้๊๋]?(?:[งน]|ก(?=[\s,.!?]|$))"

# ข้อความผีที่ Whisper ชอบแต่งขึ้นเองตอนเงียบ/มีเสียงรบกวน → ทิ้งเลย ไม่ต้องถาม Local AI
HALLUCINATIONS = ("ขอบคุณที่รับชม", "ขอบคุณสำหรับการรับชม", "ขอบคุณสำหรับชม", "ขอบคุณติดตาม", "ขอบคุณที่ติดตาม",
                  "ขอบคุณสำหรับความสุข", "ซับไตเติ้ล", "subtitle", "โปรดติดตามตอนต่อไป",
                  "กดไลค์", "กดไลก์", "กดติดตาม", "subscribe")

# ════════════════════════════════════════════════════════════════════════════
# 2) ค่าตั้งจาก .env
# ════════════════════════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except ValueError:
        print(f"⚠️  {name} ไม่ใช่ตัวเลข ใช้ค่าเริ่มต้น {default}")
        return default


LMSTUDIO_BASE_URL = env_str("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")
LMSTUDIO_MODEL = env_str("LMSTUDIO_MODEL", "")
LLM_PROVIDER = "LM Studio"
LLM_URL = f"{LMSTUDIO_BASE_URL}/chat/completions"
LLM_KEY = "lm-studio"


def _discover_lmstudio_model() -> str:
    if LMSTUDIO_MODEL:
        return LMSTUDIO_MODEL
    try:
        r = requests.get(f"{LMSTUDIO_BASE_URL}/models", timeout=3)
        r.raise_for_status()
        models = r.json().get("data", [])
        if models:
            return str(models[0]["id"])
    except (requests.RequestException, ValueError, KeyError, TypeError):
        pass
    return ""


def _active_model(preferred: str = "") -> str:
    return preferred or _discover_lmstudio_model()


LLM_MODEL = LMSTUDIO_MODEL
AGENT_MODEL = env_str("AGENT_MODEL", "")
CONF_MIN = env_float("CONF_MIN", 0.65)
TTS_VOICE = env_str("TTS_VOICE", "Kanya")
TTS_RATE = int(env_float("TTS_RATE", 230))
# ระดับเสียง (pitch base ของ say) เว้นว่าง = เสียงปกติ · 28 ≈ ทุ้มแบบผู้ชาย (Kanya 211 Hz → ~110 Hz)
TTS_PITCH = env_str("TTS_PITCH", "28")
BARGE_IN = env_str("BARGE_IN", "auto").lower()          # auto | on | off
SILENCE_MS = env_float("SILENCE_MS", 550)
VAD_THRESHOLD = env_float("VAD_THRESHOLD", 0.5)
def _installed_apps() -> dict:
    """แอปทั้งหมดที่ติดตั้งในเครื่อง → ใส่เป็นตัวเลือกของ Local AI ให้สั่งเปิด/ปิดได้ทุกแอป (EXTRA_APPS=off เพื่อปิด)
    จำกัดไว้ที่ 220 เพื่อไม่ให้ prompt ใหญ่เกินไป"""
    if env_str("EXTRA_APPS", "auto").lower() in ("off", "0", "no"):
        return {}
    dirs = ["/Applications", "/System/Applications", "/System/Applications/Utilities", Path.home() / "Applications"]
    paths = {p.stem: p for d in dirs if Path(d).is_dir() for p in Path(d).glob("*.app")}
    names = sorted(set(paths) - set(APPS) - set(SITES))[:220]
    try:                                    # ชื่อที่ macOS แสดงเป็นภาษาไทย (เช่น Chess → หมากรุก) ให้ Local AI จับคำพูดไทยได้
        from Foundation import NSFileManager
        fm = NSFileManager.defaultManager()
        local = {n: str(fm.displayNameAtPath_(str(paths[n]))).removesuffix(".app") for n in names}
    except Exception:
        local = {}
    out = {}
    for n in names:
        th = local.get(n, n)
        th = th if th and th != n else ""
        out[n] = (th or n, f"{n} (Thai name: {th}) app installed on this Mac" if th else f"{n} (app installed on this Mac)")
    return out


EXTRA_APPS = _installed_apps()
MENUBAR = env_str("MENUBAR", "on").lower() not in ("off", "0", "no")   # ไอคอนเปิด/ปิดการฟังบนแถบเมนู
HUD = env_str("HUD", "on").lower() not in ("off", "0", "no")           # หน้าจอ J.A.R.V.I.S ลอยบนจอ (ต้องมีเมนูบาร์)

# คำปลุก: ค่าเริ่มต้น "น้องจาง" (คั่นหลายคำด้วย ,) ตั้ง WAKE_WORD=off เพื่อฟังทุกประโยคโดยไม่ต้องเรียกชื่อ
_wake = env_str("WAKE_WORD", "น้องจาง")
WAKE_WORDS = [] if _wake.lower() in ("off", "none", "-") else [w.strip() for w in _wake.split(",") if w.strip()]
WAKE_RE = re.compile("|".join(NONG_JANG_RE if w == "น้องจาง" else re.escape(w) for w in WAKE_WORDS)) \
    if WAKE_WORDS else None

RATE = 16000                          # ทุกอย่างเป็น 16 kHz mono
APP_ACTIONS = {"open_app", "quit_app"}
RISKY_ACTIONS = {"quit_app", "lock_screen", "sleep"}   # ทำพลาดแล้วน่ารำคาญ → ต้องมั่นใจกว่าปกติ
CONF_RISKY = max(CONF_MIN, 0.8)
WEB_ROUTE_CONF = max(CONF_MIN, 0.8)
COMPOUND_MIN = env_float("COMPOUND_MIN", 0.5)   # สเปก: ≥ 0.5 ถามรอบสอง (ทีมวัดแล้วแนะนำ 0.75 กันประโยคเดี่ยวถูกแยกผิด)
HISTORY_IDLE_SEC = 180                # เงียบนานเกิน 3 นาที = เริ่มเรื่องใหม่ ล้างประวัติ (ไม่ส่งบทเก่าให้ LLM เปลือง token)   # category=question แต่ action=web_task มั่นใจเท่านี้ → ค้นเว็บแทนตอบเอง
SPEAK_FIRST = {"volume_mute", "lock_screen", "sleep"}  # พูดให้จบก่อนค่อยทำ ไม่งั้นจะไม่ได้ยิน
VOLUME_STEP = 10
FOLLOWUP_SEC = env_float("CONVO_SEC", 30)   # หลังคุยกันแล้ว คุยต่อได้เรื่อยๆ โดยไม่ต้องเรียก "น้องจาง" ซ้ำภายในกี่วินาที
BROWSER_PROFILE = ROOT / ".browser-profile"   # โปรไฟล์ Chrome แยกของน้องจาง (ไม่ยุ่งกับโปรไฟล์หลัก)
MLX_WHISPER_REPO = env_str("MLX_WHISPER_REPO", "mlx-community/whisper-large-v3-turbo")
FASTER_WHISPER_MODEL = "large-v3-turbo"
# คำใบ้ให้ Whisper: auto = ใช้กับ Whisper ทั่วไป แต่ไม่ใช้กับรุ่นที่จูนไทยมาแล้ว (Typhoon) ซึ่งคำใบ้ทำให้วน
_prompt_mode = env_str("WHISPER_PROMPT_MODE", "auto").lower()
USE_WHISPER_PROMPT = _prompt_mode == "on" or (_prompt_mode == "auto" and "typhoon" not in MLX_WHISPER_REPO.lower())
SAVE_UTTERANCES = env_str("SAVE_UTTERANCES")           # ใส่โฟลเดอร์ = เก็บเสียงแต่ละประโยคเป็น WAV ไว้ทดสอบ STT
# เสียงคนคุยกันที่ไม่ได้เรียกน้องจาง: ปกติไม่พิมพ์ข้อความลง log (log ของแอปเก็บเป็นไฟล์) · all = พิมพ์ไว้ดีบัก
LOG_HEARD = env_str("LOG_HEARD", "").lower()
# ความเห็นที่สอง: ถ้าเรียกชื่อแล้วแต่ Local AI ยังไม่มั่นใจ ให้ถอดเสียงเดิมซ้ำด้วยรุ่นที่จูนภาษาไทยก่อนขอให้พูดใหม่
# auto = ใช้ถ้าโมเดลอยู่ในเครื่องแล้ว (ไม่ดาวน์โหลดเอง), off = ปิด, หรือใส่ชื่อ repo MLX เอง
STT_SECOND = env_str("STT_SECOND_OPINION", "auto")
TYPHOON_REPO = "chayapats/typhoon-whisper-turbo-mlx"
# ตัวถอดเสียงหลัก: auto = ตัวถอดเสียงภาษาไทยในเครื่องของ Apple (macOS 26+) ถ้าใช้ได้ ไม่งั้น Whisper
# วัดกับเสียงจริงของผู้ใช้ 139 ประโยค: Apple ถอดดีกว่า 69 · เท่ากัน 34 · Whisper ดีกว่า 1 · ไวกว่า ~2 เท่า
STT_ENGINE = env_str("STT_ENGINE", "auto").lower()
APPLE_STT_SRC = ROOT / "macos" / "nongjang_stt.swift"
APPLE_STT_BIN = ROOT / "bin" / "nongjang-stt"

# ════════════════════════════════════════════════════════════════════════════
# 3) ตัวช่วยทั่วไป
# ════════════════════════════════════════════════════════════════════════════

_TH_DIGIT = ["", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า"]
_TH_WEEKDAY = ["วันจันทร์", "วันอังคาร", "วันพุธ", "วันพฤหัสบดี", "วันศุกร์", "วันเสาร์", "วันอาทิตย์"]
_THAI_NUMERALS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def thai_number(n: int) -> str:
    """อ่านเลข 0-99 เป็นคำไทย เช่น 21 → ยี่สิบเอ็ด"""
    if n == 0:
        return "ศูนย์"
    tens, units = divmod(n, 10)
    text = "" if tens == 0 else "สิบ" if tens == 1 else "ยี่สิบ" if tens == 2 else _TH_DIGIT[tens] + "สิบ"
    if units:
        text += "เอ็ด" if (units == 1 and tens) else _TH_DIGIT[units]
    return text


def thai_hour(h: int) -> str:
    """ชั่วโมงแบบภาษาพูด 6 ชั่วโมง: ตีหนึ่ง, เจ็ดโมงเช้า, บ่ายสองโมง, หนึ่งทุ่ม ..."""
    if h == 0:
        return "เที่ยงคืน"
    if h <= 5:
        return "ตี" + thai_number(h)
    if h <= 11:
        return thai_number(h) + "โมงเช้า"
    if h == 12:
        return "เที่ยง"
    if h == 13:
        return "บ่ายโมง"
    if h <= 15:
        return "บ่าย" + thai_number(h - 12) + "โมง"
    if h <= 18:
        return thai_number(h - 12) + "โมงเย็น"
    return thai_number(h - 18) + "ทุ่ม"


def thai_time(now: datetime | None = None) -> str:
    now = now or datetime.now()
    m = now.minute
    minute = "ตรง" if m == 0 else "ครึ่ง" if m == 30 else thai_number(m) + "นาที"
    return f"ตอนนี้{thai_hour(now.hour)}{minute}"


def pick(key: str, **kw) -> str:
    """สุ่มประโยคตอบหนึ่งแบบจาก REPLIES แล้วเติมค่า"""
    text = random.choice(REPLIES.get(key) or [""]).format(**kw)
    CANNED.add(text)                  # ประโยคสำเร็จรูป → เก็บเสียงไว้ใช้ซ้ำได้
    return text


CANNED: set[str] = set()              # ประโยคสำเร็จรูป (ไม่มีข้อมูลของผู้ใช้) เก็บเสียงลงดิสก์ได้


# จากเสียงจริงของผู้ใช้: Whisper ได้ยิน "น้องจาง" ต้นประโยคเป็น "ต้องจาง", "ต้องกลาง", "กล้องกลาง"
# → รับรูปสัมผัส "_อง _าง" ที่ต้นประโยค เฉพาะพยัญชนะที่ถูกได้ยินสลับจริง (ไม่รับ "ท้องว่าง", "ต้องการ", "ของกลาง")
_WAKE_RHYME = re.compile(r"^\s*(?:น|ต|ด|ก|กล)[่้๊๋]?อง[\s,]*(?:จ|ก|กล)[่้๊๋]?า[งน]")


def _fuzzy_wake_prefix(text: str) -> int:
    """ต้นประโยคฟังคล้าย "น้องจาง" ไหม คืนความยาวที่ต้องตัดออก หรือ 0"""
    if "น้องจาง" not in WAKE_WORDS:
        return 0
    m = _WAKE_RHYME.match(text)
    return m.end() if m else 0


# จากเสียงจริงของผู้ใช้: ตัวถอดเสียงของ Apple ได้ยิน "น้องจาง" ต้นประโยคเป็น "ล้างจาน" "น้องแจง" "ต้องการ" "น้องจารย์"
# "อาจารย์" ฯลฯ (น ของผู้ใช้ฟังคล้าย ล) → รับรูปเหล่านี้เฉพาะเมื่อตามด้วยคำสั่งทันที (กัน "ต้องจ่ายค่าไฟ" "ล้างจานยัง")
# วัดแล้ว: เจอคำปลุก 56/139 (Whisper 55) · ใช้กับข้อความจาก Apple เท่านั้น
_APPLE_WAKE_RE = re.compile(
    r"^\s*(?:(?:น้อง|ต้อง)\s*(?:จ[่้]?า(?:รย์|ง|น|ก)?|แจง|ตาล|กานต์|จัง)|ล้าง\s*จาน"
    r"|ต้องการ|นอกจาก|หลังจาก|จ้าง|จาน|อาจารย์)"
    r"(?=\s*(?:เปิด|ปิด|หา|ค้น|ช่วย|ดู|ทำ|ฟัง|อยาก|อ่าน|เล่น|หยุด|เพิ่ม|ลด|ขอ|บอก|คลิก|พิมพ์|ตั้ง|ปรับ|ก็|วันนี้|ตอนนี้|$))")


def fix_apple_wake(text: str) -> str:
    """แก้คำปลุกที่ Apple ได้ยินเพี้ยนให้เป็น "น้องจาง" (ขั้นต่อไปจะได้ทำงานเหมือนเดิมทุกอย่าง)"""
    if "น้องจาง" not in WAKE_WORDS or has_wake(text):
        return text
    m = _APPLE_WAKE_RE.match(text)
    return f"น้องจาง {text[m.end():].lstrip()}".strip() if m else text


def has_wake(text: str) -> bool:
    return WAKE_RE is None or bool(WAKE_RE.search(text)) or _fuzzy_wake_prefix(text) > 0


def strip_wake(text: str) -> str:
    """ตัดคำปลุกออกก่อนส่งให้ Local AI เช่น "น้องจาง เปิดไลน์" → "เปิดไลน์" """
    if WAKE_RE is None:
        return text.strip()
    if WAKE_RE.search(text):
        text = WAKE_RE.sub(" ", text, count=1)
    else:
        text = text[_fuzzy_wake_prefix(text):]
    return re.sub(r"^[\s,.!?ๆ]+|[\s,.!?]+$", "", text).strip()


def only_wake(text: str) -> bool:
    """เรียกชื่อเฉยๆ ไม่ได้สั่งอะไร เช่น "น้องจาง" / "น้องจางครับ" / "สวัสดีน้องจาง" """
    rest = re.sub(r"(ครับผม|ครับ|คับ|ค่ะ|ค่า|คะ|ขา|จ้ะ|จ้า|ฮะ|นะ|หน่อย|สวัสดี|หวัดดี|เฮ้|ฮัลโหล|ว่าไง|อืม|เอ่อ)", "",
                  strip_wake(text))
    return WAKE_RE is not None and has_wake(text) and normalize(rest) == ""


_NUM_DIGIT = {"ศูนย์": 0, "หนึ่ง": 1, "เอ็ด": 1, "สอง": 2, "ยี่": 2, "สาม": 3, "สี่": 4, "ห้า": 5,
              "หก": 6, "เจ็ด": 7, "แปด": 8, "เก้า": 9}
_NUM_WORD = "ศูนย์|หนึ่ง|เอ็ด|สอง|ยี่|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า|ร้อย|สิบ"
_NUM_RUN = re.compile(rf"(?:{_NUM_WORD})(?:\s?(?:{_NUM_WORD}))*")
# คำที่ขึ้นต้นเหมือนตัวเลขแต่ไม่ใช่ (ภาษาไทยไม่เว้นวรรคระหว่างคำ)
_NOT_NUMBER = re.compile(r"สามารถ|สามี|สามัญ|ห้าง|ห้าม|เก้าอี้|ครึ่งหนึ่ง|หนึ่งเดียว")
_VOLUME_RE = re.compile(r"(?:เสียง|วอลลุ่ม|วอลุ่ม|volume)\D{0,20}?(\d{1,3})|(\d{1,3})\s*(?:%|เปอร์เซ็น)", re.I)


def _thai_number_value(run: str) -> int | None:
    total, digit = 0, None
    for tok in re.findall(_NUM_WORD, run):
        if tok in ("ร้อย", "สิบ"):
            total += (1 if digit is None else digit) * (100 if tok == "ร้อย" else 10)
            digit = None
        elif digit is not None:          # "สองสาม" = ประมาณสองถึงสาม ไม่ใช่ตัวเลขเดียว
            return None
        else:
            digit = _NUM_DIGIT[tok]
    return total + (digit or 0)


def thai_digits(text: str) -> str:
    """แปลงคำอ่านตัวเลขไทยเป็นเลข: 'ตั้งเสียงสามสิบห้าเปอร์เซ็นต์' → 'ตั้งเสียง 35 เปอร์เซ็นต์'"""
    text = text.translate(_THAI_NUMERALS)
    masked = _NOT_NUMBER.sub(lambda m: "\0" * len(m.group()), text)
    out, last = [], 0
    for m in _NUM_RUN.finditer(masked):
        value = _thai_number_value(m.group())
        if value is not None:
            out += [text[last:m.start()], f" {value} "]
            last = m.end()
    out.append(text[last:])
    return re.sub(r"\s+", " ", "".join(out)).strip()


def number_in_text(text: str) -> int | None:
    """ระดับเสียง 0-100 ที่พูดมา (ตัวเลขหรือคำไทย) โดยเลือกเลขหลังคำว่า "เสียง" หรือหน้า "%" ก่อน
    (Local AI ไม่ถนัดตัวเลข docs แนะนำให้โค้ดทำ; "เปิดเพลงที่ 2 แล้วตั้งเสียง 60" → 60 ไม่ใช่ 2)"""
    m = _VOLUME_RE.search(thai_digits(text))
    if m:
        value = int(m.group(1) or m.group(2))
        return value if value <= 100 else None
    return None


def normalize(text: str) -> str:
    """ตัดช่องว่าง/เครื่องหมาย และรวมสระอำที่ถูกแยก ไว้เทียบข้อความ"""
    return re.sub(r"[\s\.,!?'\"“”‘’()\-–—…]+", "", text.replace("ํา", "ำ")).lower()


def coverage(part: str, whole: str) -> float:
    """สัดส่วนของ part ที่พบใน whole (ไว้จับเสียงสะท้อนเป็นท่อนๆ)"""
    if not part or not whole:
        return 0.0
    blocks = SequenceMatcher(None, part, whole, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(part)


EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def speakable(text: str) -> str:
    """ลบ markdown/อีโมจิ ให้เหลือข้อความที่อ่านออกเสียงได้"""
    text = re.sub(r"^\s*(?:[-*•]|\d+\.)\s+", "", text, flags=re.M)
    text = re.sub(r"[*_#`>|~\[\]]", "", text)
    text = re.sub(r"(?:ค่ะ|คะ)(?=[\s.!?,]|$)", "ครับ", text)   # น้องจางเป็นผู้ชาย (ไม่แตะ "คะแนน")
    text = re.sub(r"ดิฉัน", "เรา", text)
    return " ".join(EMOJI_RE.sub("", text).split())


def warm_local_llm() -> None:
    """ตรวจว่า LM Studio Local Server พร้อม และ warm up โมเดลหนึ่งครั้ง"""
    try:
        model = _active_model()
        if not model:
            raise RuntimeError("LM Studio ยังไม่มีโมเดลที่โหลดอยู่")
        llm_chat([{"role": "user", "content": "สวัสดี"}], max_tokens=4, timeout=60)
        print(f"  🧠 LM Studio พร้อมแล้ว · {model}")
    except Exception as e:
        print(f"  ⚠️  LM Studio ยังไม่พร้อม: {e}")


def llm_chat(messages: list, model: str = "", tools: list | None = None,
             max_tokens: int = 400, temperature: float = 0.6, timeout: float = 25) -> dict:
    """เรียก Local LLM ผ่าน OpenAI-compatible API ของ LM Studio เท่านั้น"""
    active = _active_model(model or (AGENT_MODEL if tools else LLM_MODEL))
    if not active:
        raise RuntimeError("LM Studio ยังไม่มีโมเดลที่โหลดอยู่ — เปิด LM Studio > Developer > Start Server และ Load Model")
    body = {"model": active, "messages": messages, "max_tokens": max_tokens,
            "temperature": min(temperature, 0.2)}
    if tools:
        body |= {"tools": tools, "tool_choice": "auto"}
    try:
        r = requests.post(LLM_URL, json=body, timeout=timeout,
                          headers={"Authorization": "Bearer lm-studio", "X-Title": "Jarvis Local AI"})
    except (requests.Timeout, requests.ConnectionError) as e:
        raise RuntimeError(f"เชื่อมต่อ LM Studio ไม่สำเร็จ: {e}") from e
    if not r.ok:
        raise RuntimeError(f"LM Studio HTTP {r.status_code}: {_api_error_message(r)}")
    data = r.json()
    return data["choices"][0]["message"]


class LLMQuotaError(RuntimeError):
    """ผู้ให้บริการ LLM ตอบ 429 ทุกโมเดล (เช่น โควตา Gemini รุ่นฟรีหมด)"""


def _retry_delay(r) -> float | None:
    """วินาทีที่ผู้ให้บริการขอให้รอ (header Retry-After หรือ retryDelay ใน error ของ Google)"""
    if r.headers.get("retry-after", "").replace(".", "", 1).isdigit():
        return float(r.headers["retry-after"])
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', r.text)
    return float(m.group(1)) if m else None


def _api_error_message(r) -> str:
    """ดึงเฉพาะข้อความ error บรรทัดเดียว (ไม่พิมพ์ JSON ยาวๆ)"""
    try:
        data = r.json()
        data = data[0] if isinstance(data, list) else data
        return str(data.get("error", {}).get("message", ""))[:120] or r.text[:120]
    except ValueError:
        return r.text[:120]


# ════════════════════════════════════════════════════════════════════════════
# 4) Local Decision Engine: ให้ LM Studio คืน structured JSON แล้ว validate ฝั่ง Python
# ════════════════════════════════════════════════════════════════════════════

class LocalDecisionError(Exception):
    pass


@dataclass
class Step:
    action: str
    app: str = "none"
    volume: int | None = None
    conf: float = 1.0


@dataclass
class Decision:
    text: str
    category: str
    conf: float
    compound: float = 0.0
    steps: list[Step] = field(default_factory=list)
    ms: float = 0.0
    calls: int = 0
    tokens: int = 0
    chat: str = "none"
    chat_conf: float = 0.0


APP_CRITERIA = {name: v[1] for name, v in (APPS | SITES | EXTRA_APPS).items()}


def _json_object(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise LocalDecisionError("Local AI ไม่ได้คืน JSON")
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise LocalDecisionError(f"JSON จาก Local AI ไม่ถูกต้อง: {e}") from e


class LocalDecisionEngine:
    def decide(self, text: str, extra: str = "") -> Decision:
        actions = list(ACTIONS)
        apps = list(APP_CRITERIA)
        chats = list(CHAT_INTENTS)
        system = f"""You are the local intent router for a Thai macOS voice assistant named น้องจาง.
Return ONE JSON object only. Never execute anything and never invent actions/apps outside the allowed lists.
Speech-to-text may contain Thai misspellings or English app names written in Thai.
Allowed categories: command, question, chat, noise.
Allowed actions: {json.dumps(actions, ensure_ascii=False)}
Allowed apps/sites: {json.dumps(apps, ensure_ascii=False)}
Allowed chat intents: {json.dumps(chats, ensure_ascii=False)}
Schema:
{{"category":"command|question|chat|noise","confidence":0.0,"compound":0.0,
 "chat":"none","chat_confidence":0.0,
 "steps":[{{"action":"allowed action","app":"allowed app or none","volume":null,"confidence":0.0}}]}}
For volume_set, volume must be integer 0..100. For non-command, steps must be [].
Questions needing live web information should be command with action web_task. Questions about current screen should use read_screen.
Use multiple steps only when the user clearly requests multiple actions. Confidence must be 0..1.
"""
        user = text + (f"\nContext: {extra}" if extra else "")
        t0 = time.perf_counter()
        try:
            msg = llm_chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                           max_tokens=500, temperature=0.0, timeout=30)
            data = _json_object(msg.get("content") or "")
        except (RuntimeError, requests.RequestException, KeyError, TypeError, ValueError, LocalDecisionError) as e:
            raise LocalDecisionError(f"Local AI ตัดสินใจไม่สำเร็จ: {e}") from e
        ms = (time.perf_counter() - t0) * 1000
        category = str(data.get("category", "noise"))
        if category not in {"command", "question", "chat", "noise"}:
            category = "noise"
        conf = max(0.0, min(1.0, float(data.get("confidence", 0.0) or 0.0)))
        compound = max(0.0, min(1.0, float(data.get("compound", 0.0) or 0.0)))
        d = Decision(text, category, conf, compound=compound, ms=ms, calls=1,
                     chat=str(data.get("chat", "none")),
                     chat_conf=max(0.0, min(1.0, float(data.get("chat_confidence", 0.0) or 0.0))))
        if d.chat not in CHAT_INTENTS:
            d.chat = "none"
        if category != "command":
            return d
        for raw in data.get("steps", [])[:4]:
            action = str(raw.get("action", "none"))
            app = str(raw.get("app", "none"))
            if action not in ACTIONS or action == "none":
                continue
            if app not in APP_CRITERIA:
                app = "none"
            step_conf = max(0.0, min(1.0, float(raw.get("confidence", conf) or 0.0)))
            volume = None
            if action == "volume_set":
                spoken = number_in_text(text)
                if spoken is not None:
                    volume = spoken
                else:
                    try:
                        volume = max(0, min(100, int(raw.get("volume"))))
                    except (TypeError, ValueError):
                        step_conf = 0.0
            if action in APP_ACTIONS and app == "none":
                step_conf = 0.0
            d.steps.append(Step(action, app, volume, step_conf))
        if not d.steps:
            d.conf = 0.0
        else:
            d.conf = min([d.conf] + [x.conf for x in d.steps])
        print(f"  🧠 Local AI · {describe(d)} · conf {d.conf:.2f} · {d.ms:.0f} ms")
        return d


def required_conf(d: Decision) -> float:
    return CONF_RISKY if any(s.action in RISKY_ACTIONS for s in d.steps) else CONF_MIN


def describe(d: Decision) -> str:
    if d.category != "command":
        return d.category
    return " + ".join(describe_step(s) for s in d.steps) or "command"


def describe_step(s: Step) -> str:
    if s.action in APP_ACTIONS:
        return f"{s.action}({s.app})"
    if s.action == "volume_set":
        return f"volume_set({s.volume}%)"
    return s.action


# ════════════════════════════════════════════════════════════════════════════
# 5) คำสั่ง macOS (osascript / shell บรรทัดเดียว)
# ════════════════════════════════════════════════════════════════════════════

def osa(script: str) -> list[str]:
    return ["osascript", "-e", script]


@dataclass
class Plan:
    reply: str                                             # ประโยคที่จะพูดเมื่อสำเร็จ
    cmds: list[list[str]] = field(default_factory=list)    # คำสั่งทางเลือก: ลองตัวแรก ถ้าล้มเหลวค่อยตัวถัดไป
    speak_first: bool = False                              # พูดให้จบก่อนค่อยทำ
    job: str = ""                                          # งานเบื้องหลัง: web / computer / screen
    fallback_web: str = ""                                 # ทำไม่สำเร็จ → ส่งต่อให้เอเจนต์เว็บด้วยคำขอนี้
    fail_reply: str = ""                                   # พูดเมื่อทำไม่สำเร็จ (ว่าง = ประโยคกลาง)
    func: object = None                                    # งานที่ทำในโปรแกรมเอง (แทนคำสั่ง shell) คืน True = สำเร็จ


# ── Chrome ของผู้ใช้ vs Chrome ของน้องจาง ──────────────────────────────────────────────────────
# เอเจนต์เว็บเปิด Chrome อีกตัวด้วยโปรไฟล์แยก (.browser-profile) เพราะ Chrome ไม่ยอมให้คุมโปรไฟล์หลักผ่าน CDP
# สองตัวเป็นแอปเดียวกัน → "open -a Google Chrome" / "open <ลิงก์>" อาจไปโผล่ในตัวของน้องจาง (ไม่มีบุ๊กมาร์ก/ล็อกอินของผู้ใช้)
# open -n = เปิดโปรเซสใหม่ด้วยโปรไฟล์ปกติ → ถ้า Chrome ของผู้ใช้เปิดอยู่ มันส่งต่อไปให้ตัวนั้นเอง (ไม่แตะตัวของน้องจาง)
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _default_browser() -> str:
    """bundle id ของเบราว์เซอร์หลัก (อ่านจากการตั้งค่า LaunchServices)"""
    import plistlib
    path = Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    try:
        for h in plistlib.loads(path.read_bytes()).get("LSHandlers", []):
            if h.get("LSHandlerURLScheme") == "https":
                return str(h.get("LSHandlerRoleAll", "")).lower()
    except (OSError, ValueError):
        pass
    return "com.apple.safari"


def open_url_cmd(url: str) -> list[str]:
    """เปิดลิงก์ในเบราว์เซอร์ประจำของผู้ใช้ (ถ้าเป็น Chrome ต้องไม่ไปโผล่ในหน้าต่างของน้องจาง)"""
    if _default_browser() == "com.google.chrome" and Path(CHROME_BIN).exists():
        from computer_agent import user_chrome_cmd
        return user_chrome_cmd(url)
    return ["open", url]


def user_chrome_pids() -> list[int]:
    """โปรเซสหลักของ Chrome ที่ผู้ใช้ใช้เอง (ไม่นับตัวของเอเจนต์เว็บ)"""
    out = read_cmd(["ps", "-axo", "pid=,command="])
    pids = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if cmd.startswith(CHROME_BIN) and "Helper" not in cmd and str(BROWSER_PROFILE) not in cmd:
            pids.append(int(pid))
    return pids


def quit_user_chrome() -> bool:
    """ปิดเฉพาะ Chrome ของผู้ใช้ (ส่งคำสั่ง quit ปกติ เหมือนกด cmd+Q) ไม่แตะตัวของน้องจาง"""
    import AppKit
    ok = False
    for pid in user_chrome_pids():
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        ok = bool(app is not None and app.terminate()) or ok
    return ok


def read_cmd(cmd: list[str]) -> str:
    """รันคำสั่งแบบอ่านอย่างเดียว (ไม่เปลี่ยนสถานะเครื่อง) ใช้ได้แม้ใน dry-run"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def app_running(app: str) -> bool:
    # เช็คแบบนี้ไม่ส่ง Apple Event จึงไม่ต้องขอสิทธิ์ และไม่เปิดแอปขึ้นมาเอง
    return read_cmd(osa(f'application "{app}" is running')) == "true"


def app_installed(app: str) -> bool:
    dirs = ["/Applications", "/System/Applications", "/System/Applications/Utilities",
            "/System/Library/CoreServices", str(Path.home() / "Applications")]
    return any(Path(d, f"{app}.app").exists() for d in dirs)


def current_volume() -> int | None:
    out = read_cmd(osa("output volume of (get volume settings)"))
    return int(out) if out.isdigit() else None   # บางอุปกรณ์ (HDMI) ได้ "missing value"


def host_app() -> str:
    """แอปที่รัน Jarvis อยู่ (ห้ามปิด ไม่งั้น Jarvis ตายไปด้วย)"""
    return {"Apple_Terminal": "Terminal", "vscode": "Visual Studio Code",
            "iTerm.app": "iTerm"}.get(os.getenv("TERM_PROGRAM", ""), "")


def music_player() -> str:
    """ใช้ Spotify ถ้ามี ไม่งั้นใช้ Music ของ Apple (สคริปต์อ้าง Spotify จะ compile ไม่ผ่านถ้าไม่ได้ติดตั้ง)"""
    return "Spotify" if app_installed("Spotify") else "Music"


def make_plan(step: Step) -> Plan:
    """แปลงหนึ่งขั้นตอนเป็นคำสั่ง shell + ประโยคตอบ"""
    a, app = step.action, step.app
    name = (APPS | SITES | EXTRA_APPS).get(app, (app,))[0] or app

    if a == "open_app" and app in SITES:        # เว็บยอดนิยม → เปิดในเบราว์เซอร์หลักตรงๆ
        return Plan(pick(a, app=name), [open_url_cmd(SITES[app][2])])
    if a == "open_app" and app == "Google Chrome":    # Chrome ตัวที่ผู้ใช้ใช้ประจำ ไม่ใช่ตัวของน้องจาง
        from computer_agent import user_chrome_cmd
        return Plan(pick(a, app=name), [user_chrome_cmd()], fail_reply=f"เปิด{name}ไม่ได้ครับ")
    if a == "open_app":
        return Plan(pick(a, app=name), [["open", "-a", app]], fail_reply=f"ไม่พบแอป{name}ในเครื่องครับ")
    if a == "quit_app" and app in SITES:
        return Plan(f"{name}เป็นเว็บ ปิดแท็บเองได้เลยครับ")
    if a == "quit_app":
        if app == host_app():
            return Plan(f"ปิด{name}ไม่ได้ครับ เพราะผมทำงานอยู่ในนั้น")
        if app == "Finder":   # Finder ปิดแล้วจะเปิดตัวเองใหม่ → ปิดหน้าต่างแทน
            return Plan("ปิดหน้าต่างไฟน์เดอร์ให้แล้วครับ", [osa('tell application "Finder" to close every window')])
        if app == "Google Chrome":                     # ปิดเฉพาะตัวของผู้ใช้ (ตัวของน้องจางปิดเองตอนเลิกใช้)
            if not user_chrome_pids():
                return Plan(f"{name}ไม่ได้เปิดอยู่ครับ")
            return Plan(pick(a, app=name), func=quit_user_chrome, fail_reply=f"{name}ยังไม่ปิดครับ")
        if not app_running(app):
            return Plan(f"{name}ไม่ได้เปิดอยู่ครับ")
        # รอแค่ 5 วิ: ถ้าแอปถามยืนยัน (เช่น มีไฟล์ยังไม่เซฟ) จะได้ไม่ค้างทั้งระบบ
        return Plan(pick(a, app=name), [["osascript", "-e", "with timeout of 5 seconds",
                                         "-e", f'tell application "{app}" to quit', "-e", "end timeout"]],
                    fail_reply=f"{name}ยังไม่ปิดครับ อาจกำลังถามยืนยันอยู่")

    if a in ("volume_up", "volume_down"):
        now = current_volume()
        if now is None:
            return Plan("อุปกรณ์เสียงตอนนี้ปรับระดับไม่ได้ครับ")
        new = min(100, now + VOLUME_STEP) if a == "volume_up" else max(0, now - VOLUME_STEP)
        return Plan(pick(a, vol=new), [osa(f"set volume output volume {new} without output muted")])
    if a == "volume_mute":
        return Plan(pick(a), [osa("set volume with output muted")], speak_first=True)
    if a == "volume_set":
        vol = max(0, min(100, step.volume if step.volume is not None else 50))
        if vol == 0:
            return Plan(pick("volume_mute"), [osa("set volume with output muted")], speak_first=True)
        return Plan(pick(a, vol=vol), [osa(f"set volume output volume {vol} without output muted")])

    if a.startswith("music_"):
        player = music_player()
        verb = {"music_play": "play", "music_pause": "pause",
                "music_next": "next track", "music_previous": "previous track"}[a]
        if a != "music_play" and not app_running(player):
            return Plan("ตอนนี้ไม่ได้เปิดเพลงอยู่นะ")
        if a == "music_play":   # สั่งเล่นแล้วเช็คว่าเล่นจริงไหม (คลังว่าง = สั่งผ่านแต่เงียบ)
            script = ["osascript", "-e", f'tell application "{player}" to play', "-e", "delay 0.8",
                      "-e", f'tell application "{player}" to if player state is not playing then error "ไม่มีเพลงให้เล่น"']
            return Plan(pick(a), [script], fail_reply=f"ใน{'สปอติฟาย' if player == 'Spotify' else 'แอป Music'}ไม่มีเพลงอะ "
                                                      "เดี๋ยวเปิดในยูทูบให้นะ",
                        fallback_web="เปิดเพลงตามคำขอนี้ในยูทูบให้เล่นเลย (ถ้าไม่ได้ระบุเพลง ให้เปิดเพลงฮิตไทย)")
        return Plan(pick(a), [osa(f'tell application "{player}" to {verb}')])

    if a == "dark_mode":
        return Plan(pick(a), [osa('tell application "System Events" to tell appearance preferences '
                                  'to set dark mode to not dark mode')])
    if a == "lock_screen":
        # 1) ล็อกทันทีผ่าน login.framework ไม่ต้องขอสิทธิ์ (วิธีเดียวกับ rgcr/m-cli, MIT)
        # 2) Ctrl+Cmd+Q ด้วย key code 12 (ต้องมีสิทธิ์ Accessibility)  3) ปิดจอ (ล็อกถ้าตั้งให้ขอรหัสทันที)
        lock = ("import ctypes; ctypes.CDLL('/System/Library/PrivateFrameworks/login.framework/"
                "Versions/Current/login').SACLockScreenImmediate()")
        return Plan(pick(a), [[sys.executable, "-c", lock],
                              osa('tell application "System Events" to key code 12 using {control down, command down}'),
                              ["pmset", "displaysleepnow"]], speak_first=True)
    if a == "sleep":
        return Plan(pick(a), [["pmset", "sleepnow"]], speak_first=True)
    if a == "screenshot":
        path = Path.home() / "Desktop" / f"Jarvis-{datetime.now():%Y%m%d-%H%M%S}.png"
        return Plan(pick(a), [["screencapture", "-x", str(path)]])
    if a == "tell_time":
        return Plan(thai_time())
    if a == "web_task":
        return Plan(pick(a), job="web")
    if a in ("computer_task", "read_screen"):
        return Plan(pick(a), job="computer" if a == "computer_task" else "screen")
    if a == "stop_talking":   # การพูดแทรกหยุดเสียงไปแล้ว ไม่ต้องพูดอะไรเพิ่ม (Assistant จะยกเลิกงานเว็บให้)
        return Plan("")
    return Plan(pick("none"))


def execute(plan: Plan, dry_run: bool) -> bool:
    """รันคำสั่งของแผน คืน True ถ้าสำเร็จ (หรือไม่มีอะไรต้องรัน)"""
    if plan.func is not None:
        if dry_run:
            print(f"  🧪 [dry-run] {plan.func.__name__}()")
            return True
        ok = bool(plan.func())
        print(f"  ⚙️  {plan.func.__name__}() → {'สำเร็จ' if ok else 'ไม่สำเร็จ'}")
        return ok
    for i, cmd in enumerate(plan.cmds):
        label = "สำรอง: " if i else ""
        if dry_run:
            print(f"  🧪 [dry-run] {label}{shlex.join(cmd)}")
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"  ⚙️  {label}{shlex.join(cmd)} → ล้มเหลว: {e}")
            continue
        print(f"  ⚙️  {label}{shlex.join(cmd)} → rc={r.returncode}")
        if r.returncode == 0:
            return True
        if r.stderr.strip():
            print(f"     {r.stderr.strip()[:200]}")
            if re.search(r"-1743|-1719|-25211|\(1002\)|Not authorized|assistive", r.stderr):
                print("     🔐 macOS ยังไม่ให้สิทธิ์: System Settings › Privacy & Security › Automation / Accessibility "
                      "แล้วเปิดให้แอปเทอร์มินัลที่รันน้องจาง")
    return dry_run or not plan.cmds


def save_utterance(audio: np.ndarray, text: str) -> None:
    """เก็บเสียงแต่ละประโยคเป็น WAV (ชื่อไฟล์มีข้อความที่ถอดได้) ไว้เทียบโมเดล STT ด้วย --wav"""
    folder = Path(SAVE_UTTERANCES).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\wก-๙]+", "_", text)[:40] or "empty"
    with wave.open(str(folder / f"{datetime.now():%H%M%S}_{name}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


# ════════════════════════════════════════════════════════════════════════════
# 6) สมองกลาง: ข้อความ → Local AI → สั่งเครื่อง / ถาม LLM / ข้าม → พูดตอบ
# ════════════════════════════════════════════════════════════════════════════

LLM_SYSTEM_PROMPT = """คุณคือ "น้องจาง" ผู้ช่วยเสียงบนเครื่อง Mac ของผู้ใช้ เป็นผู้ชาย คุยกับผู้ใช้เหมือนเพื่อนสนิท เป็นกันเอง ตอบไว
- ตอบสั้นมาก 1-2 ประโยค เหมือนคุยโทรศัพท์กับเพื่อน ตอบตรงคำถามก่อนเสมอ
- แทนตัวเองว่า "เรา" เท่านั้น (ห้ามใช้ "ฉัน" "ดิฉัน" "ผม") เรียกผู้ใช้ว่า "นาย" หรือไม่ต้องใช้สรรพนาม
- ลงท้ายด้วย "นะ" "อะ" "ครับ" ตามธรรมชาติ สุภาพแบบเพื่อน ห้ามใช้คำหยาบ ห้ามใช้ "กู" "มึง"
- ไม่ต้องถามกลับทุกครั้ง ถามต่อเฉพาะตอนที่ชวนคุยได้จริงหรือคำตอบยังไม่ครบ
- ถ้าผู้ใช้หงุดหงิดหรือบ่นว่าเราทำไม่ได้ ให้ขอโทษสั้นๆ อย่างจริงใจ แล้วถามว่าจะให้ช่วยอะไร ไม่ต้องเล่นมุก
- คำตอบจะถูกอ่านออกเสียง ห้ามใช้ markdown บูลเล็ต เลขข้อ หรืออีโมจิ
- ข้อความของผู้ใช้มาจากการถอดเสียง อาจสะกดผิด (เช่น ร/ล สลับกัน) ให้เดาความหมายที่น่าจะเป็น
- ถ้าไม่แน่ใจข้อเท็จจริง บอกตรงๆ ว่าไม่แน่ใจ ห้ามเดาเรื่องยา สุขภาพ เงิน หรือตัวเลขสำคัญ
- ตอนคุยแบบนี้เรามองไม่เห็นจอและยังไม่ได้ทำอะไรบนเครื่อง ห้ามแต่งว่าเห็นอะไรบนจอหรือทำอะไรให้แล้ว
  ถ้าถามว่าทำได้ไหม ให้บอกว่าทำได้และบอกวิธีสั่ง เช่น "น้องจาง อ่านจอให้ฟังหน่อย" "น้องจาง เปิดสปอติฟาย"
  "น้องจาง หาห้องพักแถวเมืองทองให้หน่อย" "น้องจาง คลิกปุ่มบันทึกให้หน่อย"
ตอนนี้คือ {now}"""


class Assistant:
    def __init__(self, speaker, dry_run: bool = False, background_web: bool = False):
        self.router = LocalDecisionEngine()
        self.speaker = speaker
        self.dry_run = dry_run
        self.background_web = background_web   # โหมดไมค์: ท่องเว็บเบื้องหลัง คุยต่อได้ระหว่างรอ
        self.misses = 0                    # นับพลาดติดกัน (confidence ต่ำ)
        self.history = deque(maxlen=6)     # บทสนทนา 6 รอบล่าสุด (รวมคำสั่งที่ทำไปแล้ว) ให้คุยต่อเนื่องได้
        self.last_active = 0.0             # เวลาที่คุยกันล่าสุด (พูดต่อได้โดยไม่ต้องเรียกชื่อ)
        self.last_text = ""                # ได้ยินอะไรล่าสุด (โชว์ในเมนูบาร์)
        self.last_request = ""             # คำขอล่าสุดที่ทำให้ (เป็นบริบทให้คำสั่งต่อเนื่อง เช่น "ลองค้นดูสิ")
        self.thinking = False              # กำลังคิด/ทำงานอยู่ (ไอคอนเมนูบาร์)
        self._web_agent = None
        self._pc_agent = None
        self._web_bg = None
        self._job_kind = ""
        self.hud_rect = None               # กรอบหน้าต่าง HUD บนจอ (x, y, w, h) — เอเจนต์คุมคอมต้องไม่คลิกทับ
        self.last_failed, self.last_failed_at = "", 0.0   # งานล่าสุดที่ทำไม่สำเร็จ ("ลองดูสิ" = ลองงานนี้ใหม่)
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._stt_pool = ThreadPoolExecutor(max_workers=1)   # ถอดเสียงซ้ำ (ความเห็นที่สอง) ไปพร้อมกับ Local AI
        self._early = None

    @property
    def browsing(self) -> bool:
        return self.job != ""

    def handle(self, text: str, second_opinion=None) -> None:
        """second_opinion() = ถอดเสียงเดิมซ้ำด้วยอีกรุ่น (เรียกเฉพาะตอนเรียกชื่อแล้วแต่ Local AI ยังไม่มั่นใจ)"""
        self.last_text, self.thinking = text, True
        try:
            self._handle(text, second_opinion)
        finally:
            self.thinking = False

    def _decide(self, text: str, context: str = "") -> Decision | None:
        try:
            d = self.router.decide(text, context)
        except LocalDecisionError as e:
            print(f"  ❌ {e}")
            self.speaker.say(pick("router_error"))
            return None
        print(f"  ⇒ {describe(d)} · conf {d.conf:.2f} (เกณฑ์ {required_conf(d):.2f}) · Local AI {d.calls} ครั้ง {d.ms:.0f} ms")
        return d

    def _handle(self, text: str, second_opinion=None) -> None:
        if only_wake(text):                # เรียก "น้องจาง" เฉยๆ → ขานรับแล้วรอฟังคำสั่ง
            self.speaker.say(pick("wake"))
            self.last_active = time.monotonic()
            return
        woke = WAKE_RE is None or has_wake(text)
        text = strip_wake(text)
        if self.history and time.monotonic() - self.last_active > HISTORY_IDLE_SEC:
            self.history.clear()
        # บริบทการคุย: ถ้าเพิ่งคุยกันอยู่ บอก Local AI ว่าน้องจางเพิ่งพูดอะไรไป (ผู้ใช้อาจกำลังตอบกลับ)
        chatting = self.history and time.monotonic() - self.last_active < FOLLOWUP_SEC
        context = f"The assistant just said: \"{self.history[-1][1][:120]}\". The speaker may be replying to it." \
            if chatting else ""
        if woke:
            context += " The speaker called the assistant by name, so this is addressed to the assistant."
        # LLM ในเครื่องเริ่มคิดคำตอบไปพร้อมกับ Local AI เลย ถ้าเป็นคำถามจะตอบได้ไวขึ้น ~0.5 วิ (ถ้าเป็นคำสั่งก็ทิ้งไป)
        early, early_text = None, text
        if LLM_PROVIDER == "ในเครื่อง":
            if self._early is not None:
                self._early.cancel()               # อันเก่าที่ยังไม่เริ่ม ไม่ต้องคิดแล้ว
            early = self._early = self._pool.submit(self._think, text)
        # เรียกชื่อมา → เริ่มถอดเสียงซ้ำด้วยอีกรุ่นไปพร้อมกับ Local AI เลย (ถ้า Local AI ไม่มั่นใจจะได้ไม่ต้องรอเพิ่ม ~0.5 วิ)
        spec2 = self._stt_pool.submit(second_opinion) if woke and second_opinion is not None else None
        d = self._decide(text, context.strip())
        if d is None:
            return
        need = required_conf(d)
        # เรียกชื่อแล้วแต่ไม่มั่นใจ → ใช้ผลถอดเสียงซ้ำจากรุ่นที่จูนภาษาไทย ก่อนจะรบกวนให้ผู้ใช้พูดใหม่
        if woke and d.category != "noise" and d.conf < need and spec2 is not None:
            try:
                alt = strip_wake(spec2.result(timeout=15) or "")
            except Exception as e:
                print(f"  ⚠️  ถอดเสียงซ้ำไม่สำเร็จ: {e}")
                alt = ""
            if alt and normalize(alt) != normalize(text):
                print(f"  🔁 ความเห็นที่สอง: {alt}")
                d2 = self._decide(alt, context.strip())
                if d2 is not None and d2.category != "noise" and d2.conf >= required_conf(d2):
                    d, text, need = d2, alt, required_conf(d2)

        # คุยเหมือนเพื่อน: เรียกชื่อมาแล้ว (หรือกำลังคุยกันอยู่) แต่ไม่ใช่คำสั่งชัดๆ → คุยตอบ แทนการเงียบหรือขอให้พูดใหม่
        clear_command = d.category == "command" and d.conf >= need and any(s.action != "none" for s in d.steps)
        vague_command = d.category == "command" and any(s.action not in ("none", "web_task") for s in d.steps)
        # กำลังคุยกันอยู่ → คุยตอบ ยกเว้น Local AI บอกว่าเป็นเสียงอื่น (ไม่รับ noise แม้ไม่แน่ใจ:
        # ตอนเปิดวิดีโอ/เพลง ไมค์ได้ยินเสียงจากลำโพงที่ AEC ไม่รู้จัก แล้วจะไปคุยตอบเสียงในวิดีโอ)
        chat_reply = chatting and d.category != "noise"
        if not clear_command and not vague_command and (woke or chat_reply):
            asks = d.chat in MOOD_INTENTS and ASKS_RE.search(text)
            retry = self.last_failed if d.chat == "retry" and time.monotonic() - self.last_failed_at < 120 else ""
            if retry and d.chat_conf >= CHAT_CONF:           # "ลองดูสิ" หลังงานที่ทำไม่สำเร็จ → ลองงานเดิมอีกครั้ง
                print(f"  ↻ ลองคำขอเดิมอีกครั้ง: {retry}")
                self.last_failed = ""
                self._handle(retry)
                return
            if d.chat in CHAT_REPLIES and d.chat_conf >= CHAT_CONF and d.category != "command" and not asks:
                print(f"  💬 คุยเล่น ({d.chat} {d.chat_conf:.2f}) → ตอบทันทีจาก Local AI ไม่ต้องรอ LLM")
                self._canned_reply(text, d.chat, early)
                return
            print("  💬 คุยเล่น/ถามทั่วไป → ตอบแบบเพื่อน")
            self._chat_reply(text, early, early_text)
            return
        if d.category == "noise":
            print("  🔇 ไม่ได้พูดกับน้องจาง — ข้าม")
            return
        # confidence gate: ไม่มั่นใจ → ขอพูดใหม่, พลาด 2 ครั้งติด → ข้าม
        if d.conf < need and not woke:     # ไม่ได้เรียกชื่อ (ช่วงคุยต่อ) แล้วยังไม่ชัด → น่าจะไม่ได้คุยกับเรา
            print("  🔇 ไม่ชัดและไม่ได้เรียกชื่อ — ข้ามเงียบๆ")
            return
        if d.conf < need:
            self.misses += 1
            if self.misses >= 2:
                self.misses = 0
                print("  ⏭  พลาด 2 ครั้งติด — ข้าม")
                self.speaker.say(pick("skip"))
            else:
                print("  ↻  ความมั่นใจต่ำ — ขอให้พูดใหม่")
                unknown_app = any(s.action in APP_ACTIONS and s.app == "none" for s in d.steps)
                self.speaker.say(pick("which_app" if unknown_app else "retry"))
            self.last_active = time.monotonic()
            return
        self.misses = 0
        # คำถามแบบสุภาพ ("ช่วย...ให้หน่อย") ที่ Local AI จัดเป็น command แต่ไม่มีคำสั่งที่ทำได้ → ให้ LLM ตอบแทน
        if d.category == "question" or all(s.action == "none" for s in d.steps):
            self._chat_reply(text, early, early_text)
            return
        else:
            spoken = self.run_steps(d.steps, text)
            if spoken:                         # จำคำสั่งที่ทำไปแล้วด้วย จะได้คุยต่อเนื่องแบบคน
                self.history.append((text, spoken))
        self.last_request = text
        self.last_active = time.monotonic()

    def _canned_reply(self, text: str, intent: str, early=None) -> None:
        """ตอบคุยเล่นจากชุดสำเร็จรูป (เสียงเตรียมไว้แล้ว พูดได้ทันที)"""
        if early is not None:
            early.cancel()                     # ยังไม่เริ่มคิด → ไม่ต้องคิดแล้ว
        answer = random.choice(CHAT_REPLIES[intent])
        CANNED.add(answer)
        self.misses = 0
        self.speaker.say(answer)
        self.history.append((text, answer))
        self.last_request = text
        self.last_active = time.monotonic()

    def _chat_reply(self, text: str, early=None, early_text: str = "") -> None:
        """ตอบแบบคุยกัน (ใช้คำตอบที่คิดล่วงหน้าไว้ถ้าตรงประโยคเดิม) แล้วจำไว้ในประวัติ"""
        self.misses = 0
        if early is not None and early_text == text and not early.cancelled():
            answer, ok = early.result()
        else:
            answer, ok = self._think(text)
        self.speaker.say(answer)
        if ok:
            self.history.append((text, answer))
        self.last_request = text
        self.last_active = time.monotonic()

    def run_steps(self, steps: list[Step], text: str) -> str:
        """ทำคำสั่งทั้งหมด คืนประโยคที่พูดตอบ (ไว้เก็บในประวัติการคุย)"""
        if any(s.action == "stop_talking" for s in steps) and self._web_bg is not None and self._web_bg.cancel():
            print("  🛑 ยกเลิกงานเบื้องหลัง")
            reply = pick("web_cancel")
            self.speaker.say(reply)
            return reply
        plans = [make_plan(s) for s in steps]
        replies, web, task = [], None, text
        jobs = {p.job for p in plans if p.job}
        for p in plans:                       # ทำทันที (ยกเว้นพวกที่ต้องพูดก่อน และงานเบื้องหลัง)
            if p.job:
                if web is None:               # งานเว็บ+คุมคอมในประโยคเดียว → ให้เอเจนต์คุมคอมทำทั้งหมด (ส่งต่อเว็บได้เอง)
                    web = p if len(jobs) == 1 else Plan(p.reply, job="computer")
            elif not p.speak_first:
                ok = execute(p, self.dry_run)
                replies.append(p.reply if ok else (p.fail_reply or pick("fail")))
                if not ok and p.fallback_web and web is None and LLM_KEY:   # เช่น Music ว่าง → เปิดใน YouTube แทน
                    web, task = Plan("", job="web"), f"{p.fallback_web}: {text}"
        later = [p for p in plans if p.speak_first]
        replies += [p.reply for p in later]
        if web is not None:
            replies.append(web.reply if LLM_KEY else pick("web_no_llm"))
        spoken = " ".join(r for r in replies if r)
        self.speaker.say(spoken)
        after = []
        if later and web is not None and LLM_KEY and not self.dry_run:
            after = later                     # ล็อก/สลีประหว่างเอเจนต์ทำงานจะพังงาน → ทำหลังงานเสร็จ
        elif later:                           # ปิดเสียง/ล็อก/สลีป: รอพูดจบก่อน
            if self.speaker.wait():           # ถูกพูดแทรกระหว่างบอก → ไม่ทำคำสั่งเสี่ยง ให้ประโยคใหม่ตัดสินแทน
                print("  ✋ ถูกขัดก่อนทำ — ยกเลิกคำสั่งที่ต้องพูดก่อน")
            else:
                for p in later:
                    execute(p, self.dry_run)
        if web is not None and LLM_KEY:
            self.start_job(web.job, task, after)
        return spoken

    # ── งานเบื้องหลัง: ท่องเว็บ (Playwright) / คุมคอม (OCR + เมาส์ คีย์บอร์ด) / อ่านจอ ─────────────
    @property
    def job(self) -> str:
        """งานเบื้องหลังที่กำลังทำ ("" = ว่าง) ให้เมนูบาร์/HUD แสดงสถานะ"""
        return self._job_kind if self._web_bg is not None and self._web_bg.running else ""

    def start_job(self, kind: str, task: str, after: list | None = None) -> None:
        """after = คำสั่งที่รอทำเมื่องานนี้สำเร็จ (ผูกกับงานนี้ งานถูกยกเลิก = ไม่ทำ)"""
        label = {"web": "🌐 เอเจนต์ท่องเว็บ", "computer": "🖱 เอเจนต์คุมคอม", "screen": "👀 อ่านจอ"}[kind]
        if self.dry_run:
            print(f"  🧪 [dry-run] {label} ({AGENT_MODEL}): «{task}»")
            return
        if self._web_agent is None:
            from browser_agent import BackgroundBrowser, BrowserAgent
            from computer_agent import ComputerAgent
            self._web_agent = BrowserAgent(self._agent_chat, BROWSER_PROFILE)
            # ลูกผสม: งานในแอปทั่วไปคุมผ่านจอ ส่วนงานบนเว็บส่งต่อให้ Playwright (เธรดเดียวกัน)
            self._pc_agent = ComputerAgent(self._agent_chat, web=self._web_agent, exclude=lambda: self.hud_rect)
            if self.background_web:
                self._web_bg = BackgroundBrowser(self._web_agent)
        request = task
        # คำสั่งสั้นๆ ต่อเนื่อง ("ลองค้นดูสิ") มักหมายถึงเรื่องก่อนหน้า → แนบคำขอก่อนหน้าไปเป็นบริบท
        # (เฉพาะงานเว็บ: งานคุมคอมถ้าได้บริบทผิดจะไปคลิก/พิมพ์ผิดที่)
        if kind == "web" and self.last_request and time.monotonic() - self.last_active < 120:
            task = f"{task}\n(คำขอก่อนหน้าของผู้ใช้ อาจเกี่ยวข้อง: {self.last_request})"
        from computer_agent import describe_screen
        runner = {"web": self._web_agent.run, "computer": self._pc_agent.run,
                  "screen": lambda t, cancel=None: describe_screen(self._agent_chat, t,
                                                                   exclude=lambda: self.hud_rect)}[kind]
        print(f"  {label} ({AGENT_MODEL}): «{task}»")
        self._job_kind = kind
        done = lambda summary, error: self._web_done(summary, error, kind, request, after or [])
        if self._web_bg is not None:
            self._web_bg.submit(task, done, runner)
            return
        self.speaker.wait()                   # โหมดข้อความ: รอพูดจบแล้วทำจนเสร็จ
        try:
            summary, error = runner(task, None), None
        except Exception as e:
            summary, error = None, e
        done(summary, error)

    @staticmethod
    def _agent_chat(messages: list, tools: list | None) -> dict:
        """ช่องทางคุยกับ LLM ที่ส่งให้เอเจนต์เว็บ (ใช้ผู้ให้บริการเดียวกับการตอบคำถาม)"""
        return llm_chat(messages, model=AGENT_MODEL, tools=tools, max_tokens=1500, temperature=0.2, timeout=45)

    def _web_done(self, summary: str | None, error: Exception | None, kind: str = "web", request: str = "",
                  after: list | None = None) -> None:
        from computer_agent import AgentIncomplete
        speech = speakable(summary) if summary else ""
        if error is not None:
            print(f"  ❌ {'ท่องเว็บ' if kind == 'web' else 'คุมคอม'}ผิดพลาด: {error}")
            if isinstance(error, (PermissionError, AgentIncomplete)):   # ข้อความบอกผู้ใช้ได้ตรงๆ (ขาดสิทธิ์/ทำไม่จบ)
                said = str(error)
            else:
                said = pick("llm_quota" if isinstance(error, LLMQuotaError) else "web_fail" if kind == "web" else "fail")
            self.speaker.say(said)
            if request:                                       # จำว่าทำไม่สำเร็จ (คุยต่อจะได้ไม่อ้างว่ากำลังทำ)
                self.history.append((request, f"(ทำไม่สำเร็จ) {said}"))
                self.last_failed, self.last_failed_at = request, time.monotonic()
        elif not speech:                                      # เอเจนต์สรุปว่าง → อย่าเงียบ
            if summary is not None:
                print(f"  ⚠️  งาน{kind}ได้สรุปว่าง")
                self.speaker.say(pick("web_fail" if kind == "web" else "fail"))
        else:
            self.speaker.say(speech)
            self.history.append((request or "หาข้อมูลบนเว็บ", speech))
            # งานเสร็จแล้วค่อยล็อก/ปิดเสียง — รอให้เสียงรบกวนที่ทำให้หยุดพูดชั่วคราวผ่านไปก่อน (ถ้าถูกพูดแทรกจริงค่อยยกเลิก)
            if after and not self.speaker.wait(settle=True):
                for p in after:
                    execute(p, self.dry_run)
            elif after:
                print("  ✋ ถูกพูดแทรก — ไม่ทำคำสั่งที่รอไว้")
        self.last_active = time.monotonic()

    def ask_llm(self, question: str) -> str:
        answer, ok = self._think(question)
        if ok:
            self.history.append((question, answer))
        return answer

    def _think(self, question: str) -> tuple[str, bool]:
        """ให้ LLM คิดคำตอบ (ไม่แตะประวัติ) คืน (ประโยคที่จะพูด, สำเร็จไหม)"""
        if not LLM_KEY:
            return pick("no_llm"), False
        now = datetime.now()
        system = LLM_SYSTEM_PROMPT.format(now=f"{_TH_WEEKDAY[now.weekday()]} {now:%d/%m/%Y %H:%M} น.")
        messages = [{"role": "system", "content": system}]
        for q, a in list(self.history):
            messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        messages.append({"role": "user", "content": question})
        t0 = time.perf_counter()
        try:
            answer = speakable(llm_chat(messages).get("content") or "")
        except LLMQuotaError as e:
            print(f"  ❌ LLM โควตาหมด: {e}")
            return pick("llm_quota"), False
        except (RuntimeError, requests.RequestException, KeyError, IndexError, TypeError, ValueError) as e:
            print(f"  ❌ LLM ผิดพลาด: {e}")
            return pick("llm_error"), False
        print(f"  🤖 LLM {LLM_PROVIDER} {_active_model()}"
              f" · {(time.perf_counter() - t0) * 1000:.0f} ms")
        return (answer, True) if answer else (pick("llm_error"), False)


# ════════════════════════════════════════════════════════════════════════════
# 7) เสียงพูด (TTS)
# ════════════════════════════════════════════════════════════════════════════

def say_text(text: str) -> str:
    """ใส่คำสั่งฝังของ say: [[pbas N]] = ระดับเสียง (ทำให้ Kanya ทุ้มแบบผู้ชาย)"""
    return f"[[pbas {TTS_PITCH}]] {text}" if TTS_PITCH else text


def synthesize(text: str) -> np.ndarray | None:
    """ให้ say สังเคราะห์เป็นไฟล์ WAV 16 kHz แล้วอ่านกลับมาเป็น int16"""
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        r = subprocess.run(["say", "-v", TTS_VOICE, "-r", str(TTS_RATE), "-o", path, "--file-format=WAVE",
                            f"--data-format=LEI16@{RATE}", "-f", "-"],
                           input=say_text(text), text=True, capture_output=True, timeout=60)
        if r.returncode != 0:
            print(f"  ⚠️  say ล้มเหลว: {r.stderr.strip()[:200]}")
            return None
        with wave.open(path, "rb") as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    except (OSError, subprocess.TimeoutExpired, wave.Error) as e:
        print(f"  ⚠️  สังเคราะห์เสียงไม่สำเร็จ: {e}")
        return None
    finally:
        os.unlink(path)


# ── แคชเสียงพูด: say ใช้ ~0.43 วิทุกประโยค (เปิดโปรแกรม+โหลดเสียง) ไม่ว่าสั้นยาว ─────────────
# ประโยคสำเร็จรูปเก็บเป็นไฟล์ไว้ (เปิดครั้งหน้าก็ยังเร็ว) ประโยคสั้นอื่นๆ เก็บในหน่วยความจำ
TTS_CACHE_DIR = ROOT / ".tts-cache"
_tts_mem: dict[str, np.ndarray] = {}
_tts_lock = threading.Lock()


def _tts_key(text: str) -> str:
    import hashlib
    return hashlib.sha1(f"{TTS_VOICE}|{TTS_RATE}|{TTS_PITCH}|{text}".encode()).hexdigest()


def speech_audio(text: str) -> np.ndarray | None:
    """เสียงของประโยคนี้: จากแคช (ทันที) หรือสังเคราะห์ใหม่"""
    key = _tts_key(text)
    with _tts_lock:
        hit = _tts_mem.get(key)
    if hit is not None:
        return hit
    path = TTS_CACHE_DIR / f"{key}.npy"
    audio = None
    if path.exists():
        try:
            audio = np.load(path)
        except Exception:                                # ไฟล์ว่าง/เสีย → ลบทิ้งแล้วสังเคราะห์ใหม่
            audio = None
            try:
                path.unlink()
            except OSError:
                pass
    if audio is None:
        audio = synthesize(text)
        if audio is not None and text in CANNED:        # ไม่เก็บคำตอบจาก LLM ลงดิสก์ (อาจมีเรื่องส่วนตัว)
            tmp = path.with_name(f"{key}.{threading.get_ident()}.tmp")
            try:
                TTS_CACHE_DIR.mkdir(exist_ok=True)
                with open(tmp, "wb") as f:
                    np.save(f, audio)
                os.replace(tmp, path)                    # เขียนเสร็จค่อยสลับชื่อ → ไม่มีใครอ่านเจอไฟล์ครึ่งๆ
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass
    if audio is not None and len(text) <= 80:
        with _tts_lock:
            if len(_tts_mem) >= 800:
                _tts_mem.pop(next(iter(_tts_mem)))
            _tts_mem[key] = audio
    return audio


def canned_texts() -> list[str]:
    """ประโยคสำเร็จรูปทั้งหมดที่คาดว่าจะได้พูด (เรียงจากใช้บ่อยก่อน) ไว้สังเคราะห์ล่วงหน้า"""
    texts = [t for key in ("ready", "wake", "retry", "which_app", "skip", "fail") for t in REPLIES[key]]
    texts += [t for ts in CHAT_REPLIES.values() for t in ts]
    names = [v[0] for v in (APPS | SITES).values() if v[0]]
    for key in ("open_app", "quit_app"):
        texts += [t.format(app=n) for t in REPLIES[key] for n in names]
    for key in ("volume_up", "volume_down", "volume_set"):
        texts += [t.format(vol=v) for t in REPLIES[key] for v in range(0, 101, 10)]
    texts += [t for ts in REPLIES.values() for t in ts if "{" not in t]
    return list(dict.fromkeys(texts))


def warm_tts() -> None:
    """สังเคราะห์ประโยคสำเร็จรูปเก็บไว้ล่วงหน้า (เบื้องหลัง ครั้งแรก ~2 นาที ครั้งต่อไปโหลดจากไฟล์ทันที)"""
    todo = canned_texts()
    CANNED.update(todo)
    made = 0
    for text in todo:
        if not (TTS_CACHE_DIR / f"{_tts_key(text)}.npy").exists():
            made += 1
        speech_audio(text)
    if made:
        print(f"  🗣️  เตรียมเสียงประโยคสำเร็จรูปไว้แล้ว {made} ประโยค (ตอบได้ทันที ไม่ต้องรอสังเคราะห์)")


class SaySpeaker:
    """พูดด้วยคำสั่ง say ตรงๆ — ใช้ในโหมดข้อความ หรือเมื่อเปิดลำโพงเองไม่ได้"""
    on_played = None
    dac_lead = 0.0
    in_warmup = False
    _last_sound = 0.0

    def pause(self) -> None:        # say หยุดชั่วคราวไม่ได้ → หยุดเลย
        self.stop()

    def resume(self) -> None:
        pass

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.proc: subprocess.Popen | None = None
        self.recent: deque = deque(maxlen=5)

    def say(self, text: str) -> None:
        if not text:
            return
        print(f"  🔊 {text}")
        self.recent.append((time.monotonic(), text))
        if not self.enabled:
            return
        self.stop()
        self.proc = subprocess.Popen(["say", "-v", TTS_VOICE, "-r", str(TTS_RATE), "-f", "-"],
                                     stdin=subprocess.PIPE, text=True)
        self.proc.stdin.write(say_text(text))
        self.proc.stdin.close()

    def stop(self) -> None:
        if self.playing:
            self.proc.terminate()

    @property
    def playing(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    busy = playing

    def wait(self, timeout: float = 60, settle: bool = False) -> None:
        if self.proc is not None:
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                pass

    def close(self) -> None:
        pass


class Speaker:
    """
    เล่นเสียงพูดผ่าน OutputStream ของเราเอง (ไม่ปล่อยให้ say เล่นเอง) เพื่อ
      1) หยุดกลางคันได้ทันทีเมื่อผู้ใช้พูดแทรก
      2) ส่งเสียงที่ออกลำโพงจริงให้ตัวตัดเสียงสะท้อน (AEC) ใช้เป็นสัญญาณอ้างอิง
    """
    BLOCK = 480      # 30 ms ต่อรอบ (หารด้วยเฟรม AEC 10 ms ลงตัว)
    TAIL = 0.3       # หลังเสียงสุดท้ายกี่วินาทีที่ยังถือว่า "กำลังพูด" (เผื่อเสียงก้องห้อง)
    WARMUP = 0.8     # ช่วงต้นของแต่ละประโยคที่ AEC ยังปรับตัวไม่ทัน → ยังไม่รับการพูดแทรก

    def __init__(self):
        import sounddevice as sd
        self.on_played = None                     # callback(เสียงที่เพิ่งเล่น) → ป้อน AEC
        self.recent: deque = deque(maxlen=5)      # (เวลา, ข้อความ) ที่เพิ่งพูด ไว้กันเสียงสะท้อน
        self.dac_lead = 0.04                      # วินาทีจากตอนเติมเสียงถึงตอนออกลำโพงจริง (วัดจาก time_info)
        self._lock = threading.Lock()
        self._chunks: deque = deque()             # ก้อนเสียง int16 ที่รอเล่น
        self._jobs: queue.Queue = queue.Queue()   # (รุ่น, ข้อความ) ที่รอสังเคราะห์
        self._gen = 0                             # เพิ่มทุกครั้งที่ถูกขัด → ทิ้งเสียงรุ่นเก่า
        self._pending = 0
        self._paused = False                      # หยุดชั่วคราวเพราะได้ยินเสียงแทรก (ยังพูดต่อได้)
        self._last_sound = 0.0
        self._reply_start = 0.0
        self.stream = sd.OutputStream(samplerate=RATE, channels=1, dtype="int16",
                                      blocksize=self.BLOCK, callback=self._on_output)
        self.stream.start()
        threading.Thread(target=self._synth_loop, daemon=True).start()

    @property
    def in_warmup(self) -> bool:
        return time.monotonic() - self._reply_start < self.WARMUP

    def pause(self) -> None:
        """หยุดชั่วคราว (เก็บเสียงที่เหลือไว้) — ถ้าปรากฏว่าไม่ใช่ผู้ใช้พูดแทรกจริง จะ resume() ต่อได้"""
        with self._lock:
            self._paused = bool(self._chunks)

    def resume(self) -> None:
        with self._lock:
            if self._paused:
                self._paused = False
                self._reply_start = time.monotonic()   # เริ่มพูดต่อ = ให้ AEC ได้ช่วงปรับตัวอีกครั้ง

    def say(self, text: str) -> None:
        if not text:
            return
        print(f"  🔊 {text}")
        with self._lock:
            self._pending += 1
            gen = self._gen
        self.recent.append((time.monotonic(), text))
        self._jobs.put((gen, text))

    def stop(self) -> None:
        """หยุดพูดทันที และทิ้งประโยคที่กำลังสังเคราะห์อยู่"""
        with self._lock:
            self._gen += 1
            self._chunks.clear()
            self._paused = False

    @property
    def playing(self) -> bool:
        with self._lock:
            return (bool(self._chunks) and not self._paused) or time.monotonic() - self._last_sound < self.TAIL

    @property
    def busy(self) -> bool:
        with self._lock:
            waiting = bool(self._chunks) and not self._paused   # ที่พักไว้ไม่นับ (ไม่งั้น wait() ค้าง)
        return self._pending > 0 or waiting or self.playing

    def wait(self, timeout: float = 60, settle: bool = False) -> bool:
        """รอพูดจบ คืน True ถ้าถูกขัด (พักไว้หรือถูกหยุด) → ผู้เรียกไม่ควรทำคำสั่งเสี่ยงต่อ
        settle=True = ถ้าหยุดพูดชั่วคราวเพราะเสียงรบกวน ให้รอจนรู้ผล (พูดต่อ = ไม่นับว่าถูกขัด)
        ใช้ได้เฉพาะเธรดที่ไม่ใช่ลูปฟัง (ลูปฟังเป็นตัวสั่งพูดต่อ/หยุด)"""
        gen, end = self._gen, time.monotonic() + timeout
        while (self.busy or (settle and self._paused)) and self._gen == gen and time.monotonic() < end:
            time.sleep(0.05)
        return self._paused or self._gen != gen

    def close(self) -> None:
        self.stream.stop()
        self.stream.close()

    def _synth_loop(self) -> None:
        while True:
            gen, text = self._jobs.get()
            try:
                audio = speech_audio(text) if gen == self._gen else None
                with self._lock:
                    if audio is not None and gen == self._gen:
                        self._chunks.append(audio)
            except Exception as e:                       # ประโยคเดียวพังต้องไม่ทำให้พูดไม่ได้อีกเลย
                print(f"  ⚠️  เตรียมเสียงไม่สำเร็จ: {e}")
            finally:
                with self._lock:
                    self._pending -= 1

    def _on_output(self, outdata, frames, time_info, status) -> None:
        self.dac_lead = max(0.0, time_info.outputBufferDacTime - time_info.currentTime)
        out = np.zeros(frames, dtype=np.int16)
        with self._lock:
            filled = 0
            while filled < frames and self._chunks and not self._paused:
                head = self._chunks[0]
                n = min(frames - filled, len(head))
                out[filled:filled + n] = head[:n]
                if n == len(head):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = head[n:]
                filled += n
            if filled:
                now = time.monotonic()
                if now - self._last_sound > self.TAIL:   # เริ่มประโยคใหม่
                    self._reply_start = now
                self._last_sound = now
        outdata[:, 0] = out
        self.level = float(np.sqrt(np.mean(out.astype(np.float32) ** 2))) / 32768   # ความดังที่พูด (HUD)
        if self.on_played is not None:
            self.on_played(out)


# ════════════════════════════════════════════════════════════════════════════
# 8) หูฟัง: ไมค์ → AEC → VAD → ตัดประโยค (full duplex)
# ════════════════════════════════════════════════════════════════════════════

AEC_FRAME = 160      # 10 ms: ขนาดเฟรมที่ WebRTC AEC ต้องการ
VAD_CHUNK = 512      # 32 ms: ขนาดที่ Silero VAD ต้องการ


class Segmenter:
    """ตัดเสียงต่อเนื่องเป็นประโยคจากค่า VAD ทีละก้อน 32 ms (ไม่มี I/O จึงใช้กับไฟล์ได้ด้วย)"""
    CHUNK_MS = 32

    def __init__(self):
        ms = self.CHUNK_MS
        self.start_chunks = 3                        # พูดต่อเนื่อง ~0.1 วิ = เริ่มประโยค
        self.barge_chunks = 10                       # ตอนน้องจางพูดอยู่ ต้องพูดชัดๆ ~0.3 วิ ถึงนับว่าพูดแทรก
        self.barge_threshold = max(VAD_THRESHOLD, 0.6)
        self.end_chunks = max(1, int(SILENCE_MS / ms))
        self.max_chunks = int(15_000 / ms)           # ยาวสุด 15 วิ
        self.min_voiced = int(300 / ms)              # ต้องมีเสียงพูดรวม ≥ 0.3 วิ
        # เก็บเสียงก่อนเริ่มพูด ~0.5 วิ บวกช่วงที่ใช้ตัดสินว่าพูดแทรก กันหัวคำ ("น้องจาง") หาย
        # (เดิมเท่ากับ barge_chunks พอดี → ตอนพูดแทรกไม่เหลือเสียงก่อนหน้าเลย; แนวคิดจาก pipecat, BSD-2)
        self.preroll: deque = deque(maxlen=int(500 / ms) + self.barge_chunks)
        self.reset()

    def reset(self) -> None:
        self.active, self.chunks, self.run, self.silence, self.voiced = False, [], 0, 0, 0

    def feed(self, chunk: np.ndarray, prob: float, playing: bool = False,
             allow_barge: bool = True) -> tuple[np.ndarray | None, bool]:
        """คืน (เสียงของประโยคที่จบแล้ว หรือ None, เพิ่งเริ่มพูดแทรกตอนน้องจางพูดอยู่หรือไม่)"""
        self.preroll.append(chunk)
        if not self.active:
            if playing and not allow_barge:          # ช่วงต้นประโยคของน้องจาง: ยังไม่รับการแทรก
                self.run = 0
                return None, False
            threshold = self.barge_threshold if playing else VAD_THRESHOLD
            self.run = self.run + 1 if prob >= threshold else 0
            if self.run < (self.barge_chunks if playing else self.start_chunks):
                return None, False
            self.active, self.chunks, self.voiced, self.silence = True, list(self.preroll), self.run, 0
            return None, playing
        self.chunks.append(chunk)
        if prob >= VAD_THRESHOLD * 0.7:              # เกณฑ์จบต่ำกว่าเกณฑ์เริ่ม กันประโยคขาดกลางคำ
            self.voiced, self.silence = self.voiced + 1, 0
        else:
            self.silence += 1
        if self.silence < self.end_chunks and len(self.chunks) < self.max_chunks:
            return None, False
        audio = np.concatenate(self.chunks) if self.voiced >= self.min_voiced else None
        self.reset()
        self.preroll.clear()                         # ไม่เอาเสียงที่ส่งไปแล้วมาต่อหัวประโยคถัดไปซ้ำ
        return audio, False


def make_echo_canceller():
    """WebRTC AEC ผ่าน livekit (ถ้าติดตั้ง) — ตัดเสียง Jarvis ที่ย้อนเข้าไมค์ได้ ~38 dB"""
    try:
        from livekit import rtc
    except ImportError:
        return None, None
    return rtc, rtc.AudioProcessingModule(echo_cancellation=True, high_pass_filter=True)


class Ears:
    BLOCK = 480      # 30 ms ต่อรอบ

    def __init__(self, speaker):
        import sounddevice as sd
        from pysilero_vad import SileroVoiceActivityDetector
        self.speaker = speaker
        self.vad = SileroVoiceActivityDetector()
        self.seg = Segmenter()
        # ประโยคที่พูดจบแล้ว: (เสียง float32, เริ่มตอนน้องจางพูดอยู่ไหม, ทำให้น้องจางหยุดชั่วคราวไหม)
        self.utterances: queue.Queue = queue.Queue()
        self._blocks: queue.Queue = queue.Queue()
        self.rtc, self.apm = make_echo_canceller() if isinstance(speaker, Speaker) else (None, None)
        self._apm_lock = threading.Lock()
        self._adc_lag = 0.05          # วินาทีจากเสียงเข้าไมค์จริงถึงตอน callback (วัดจาก time_info)
        self.barge_in = BARGE_IN == "on" or (BARGE_IN == "auto" and self.apm is not None)
        self.listening = True
        self.level = 0.0                 # ความดังล่าสุดของไมค์ 0-1 (ให้ HUD ขยับตามเสียง)
        self._talk = 0
        self._overlap = self._interrupted = False
        speaker.on_played = self._reference
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                                     blocksize=self.BLOCK, callback=self._on_input)

    def start(self) -> None:
        self.stream.start()
        threading.Thread(target=self._loop, daemon=True).start()

    def set_listening(self, on: bool) -> None:
        """เปิด/ปิดการฟัง (ปิด = หยุดไมค์จริง ไฟไมค์สีส้มของ macOS จะดับ)"""
        if on == self.listening:
            return
        self.listening = on
        if on:
            self.stream.start()
        else:
            self.stream.stop()
            self.seg.reset()
            self.vad.reset()
            self.speaker.resume()
        print(f"\n{'🎙  เปิดการฟังแล้ว' if on else '🔇 ปิดการฟังแล้ว'}")

    def close(self) -> None:
        self.stream.stop()
        self.stream.close()
        self.speaker.on_played = None
        with self._apm_lock:
            self.apm = None      # ปล่อย AEC ก่อนปิดโปรแกรม (กัน error ตอน livekit ปิดตัว)

    @property
    def delay_ms(self) -> int:
        """ดีเลย์จากตอนป้อนเสียงลงลำโพงถึงตอนเสียงสะท้อนมาถึง callback ของไมค์ (WebRTC รับได้ไม่เกิน 500)"""
        return int(min(400, max(0, (self.speaker.dac_lead + self._adc_lag) * 1000 + 5)))

    def _frames(self, samples: np.ndarray):
        for i in range(0, len(samples) - AEC_FRAME + 1, AEC_FRAME):
            yield i, self.rtc.AudioFrame(bytearray(samples[i:i + AEC_FRAME].tobytes()), RATE, 1, AEC_FRAME)

    def _aec_failed(self, e: Exception) -> None:
        """AEC พัง → ปิด AEC แล้วถอยไปแบบ half duplex (ไม่ให้ callback เสียงตายทั้งระบบ)"""
        self.apm = None
        self.barge_in = BARGE_IN == "on"
        print(f"\n⚠️  ตัวตัดเสียงสะท้อนผิดพลาด ({e}) — ปิด AEC และไม่ฟังระหว่างน้องจางพูด")

    def _reference(self, played: np.ndarray) -> None:
        """เสียงที่เพิ่งออกลำโพง → ป้อนให้ AEC รู้ว่าอะไรคือเสียงสะท้อน"""
        with self._apm_lock:
            if self.apm is not None:
                try:
                    for _, frame in self._frames(played):
                        self.apm.process_reverse_stream(frame)
                except Exception as e:
                    self._aec_failed(e)

    def _on_input(self, indata, frames, time_info, status) -> None:
        self._adc_lag = max(0.0, time_info.currentTime - time_info.inputBufferAdcTime)
        block = indata[:, 0].copy()
        with self._apm_lock:
            if self.apm is not None:
                try:
                    delay = self.delay_ms
                    for i, frame in self._frames(block):
                        self.apm.set_stream_delay_ms(delay)
                        self.apm.process_stream(frame)      # แก้ข้อมูลในเฟรมโดยตรง
                        block[i:i + AEC_FRAME] = np.frombuffer(frame.data, dtype=np.int16)
                except Exception as e:
                    self._aec_failed(e)
        self._blocks.put(block)

    def _loop(self) -> None:
        pending = np.zeros(0, dtype=np.int16)
        heard, started, warned = 0, time.monotonic(), False
        while True:
            pending = np.concatenate([pending, self._blocks.get()])
            while len(pending) >= VAD_CHUNK:
                chunk, pending = pending[:VAD_CHUNK], pending[VAD_CHUNK:]
                heard = max(heard, int(np.abs(chunk).max()))
                self._on_chunk(chunk)
            if not warned and heard == 0 and time.monotonic() - started > 3:
                warned = True
                print("⚠️  ไมค์ส่งมาแต่ความเงียบสนิท — อาจยังไม่ได้ให้สิทธิ์ Microphone กับแอปเทอร์มินัล")

    def _on_chunk(self, chunk: np.ndarray) -> None:
        if not self.listening:
            return
        playing = self.speaker.playing
        if playing and not self.barge_in:            # half duplex: ไม่ฟังตอนตัวเองพูด
            if self.seg.active or self.seg.run:
                self.seg.reset()
                self.vad.reset()
            return
        self.level = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2))) / 32768   # ความดังไมค์ (HUD)
        prob = self.vad.process_chunk(chunk.tobytes())
        # ผู้ใช้พูดค้างอยู่ตอนน้องจางเริ่มตอบ → พักไว้ก่อน (ต้องพูดต่อเนื่อง 4 ก้อน และพ้นช่วง warm-up
        # ไม่งั้นเสียงสะท้อนช่วงต้นประโยคจะทำให้น้องจางหยุดตัวเอง)
        talking = playing and self.seg.active and not self.speaker.in_warmup and prob >= self.seg.barge_threshold
        self._talk = self._talk + 1 if talking else 0
        if self._talk >= 4:
            self.speaker.pause()
            self._interrupted = True
        was_active = self.seg.active
        audio, barged = self.seg.feed(chunk, prob, playing, allow_barge=not self.speaker.in_warmup)
        if self.seg.active and not was_active:       # เพิ่งเริ่มประโยค: จำว่าเริ่มตอนน้องจางพูดอยู่ไหม
            self._overlap = playing                  # playing รวมช่วง TAIL หลังเสียงสุดท้ายแล้ว
            self._interrupted = False
        if barged:
            # หยุดชั่วคราวก่อน ถ้าถอดความแล้วไม่ใช่ผู้ใช้เรียกน้องจางจริง จะพูดต่อจากเดิม
            self.speaker.pause()
            self._interrupted = True
            print("\n✋ ได้ยินเสียงแทรก — หยุดฟังก่อน")
        if was_active and not self.seg.active:       # ประโยคจบแล้ว
            self.vad.reset()                         # Silero จำสถานะ ต้องล้างหลังจบประโยค
            if audio is None:                        # สั้นเกินไป ไม่ใช่คำพูด → พูดต่อ
                if self._interrupted:
                    self.speaker.resume()
                return
            self.utterances.put((audio.astype(np.float32) / 32768.0, self._overlap, self._interrupted))


# วนซ้ำ ≥ 3 รอบ (ไม่นับตัวเลข เช่น "ราคา 1000 บาท")
LOOP_RE = re.compile(r"([^\s\d].{0,14}?)(?:\s*\1){2,}")


def collapse_repeats(text: str) -> str:
    """แก้อาการ Whisper พูดวน: "ตั้งเสียง 35 ตั้งเสียง 35 ตั้งเสียง 35 ตั้ง" → "ตั้งเสียง 35" """
    text = " ".join(text.split())
    text = re.sub(r"(.{5,}?)(?:\s*\1)+", r"\1", text)
    head, _, tail = text.rpartition(" ")
    if head and len(tail) >= 2 and head.startswith(tail):   # ท่อนท้ายที่วนค้างไว้ครึ่งเดียว
        text = head
    return text


def is_garbage(text: str) -> bool:
    """ข้อความที่ไม่ควรเสียค่าเรียก Local AI: สั้นเกิน, ข้อความผีของ Whisper, หรือทวนคำใบ้"""
    t = normalize(text)
    if len(t) < 2:
        return True
    if len(t) < 40 and any(normalize(h) in t for h in HALLUCINATIONS):
        return True
    # ทวนคำใบ้ = ยาวเกินครึ่งของคำใบ้และเหมือนคำใบ้เกือบทั้งหมด (คำสั่งสั้นๆ อย่าง "เพิ่มเสียง" จะไม่โดน)
    prompt = normalize(WHISPER_PROMPT)
    return len(t) >= len(prompt) // 2 and coverage(t, prompt) > 0.9


def is_echo(text: str, recent) -> bool:
    """ข้อความนี้คือเสียงของ Jarvis เองที่ย้อนเข้าไมค์หรือไม่ (เทียบกับที่เพิ่งพูดใน 20 วิ)"""
    t, now = normalize(text), time.monotonic()
    return len(t) >= 6 and any(now - ts < 20 and coverage(t, normalize(said)) > 0.8 for ts, said in list(recent))


# ════════════════════════════════════════════════════════════════════════════
# 9) แปลงเสียงเป็นข้อความ (Whisper ในเครื่อง)
# ════════════════════════════════════════════════════════════════════════════

class AppleSTT:
    """ถอดเสียงไทยด้วย SpeechAnalyzer + DictationTranscriber ของ macOS 26 (ในเครื่อง ไม่ต้องขอสิทธิ์ ~50-100 ms)
    คุยกับตัวช่วย Swift (macos/nongjang_stt.swift) ที่เปิดค้างไว้ โมเดลจึงโหลดครั้งเดียว
    ส่ง: [ความยาว uint32][เสียง int16 16 kHz mono] → รับ: JSON หนึ่งบรรทัด {"text": ...}"""
    TIMEOUT = 15.0

    def __init__(self, bias=("น้องจาง",)):
        import json
        import struct
        self.json, self.struct = json, struct
        if not APPLE_STT_BIN.exists() or APPLE_STT_BIN.stat().st_mtime < APPLE_STT_SRC.stat().st_mtime:
            print("⏳ คอมไพล์ตัวถอดเสียงของ Apple (ครั้งแรกครั้งเดียว ~20 วิ) ...")
            APPLE_STT_BIN.parent.mkdir(exist_ok=True)
            subprocess.run(["xcrun", "swiftc", "-O", "-parse-as-library", "-o", str(APPLE_STT_BIN), str(APPLE_STT_SRC)],
                           check=True, capture_output=True, timeout=300)
        self.p = subprocess.Popen([str(APPLE_STT_BIN), *bias], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, bufsize=0)
        hello = self._read()
        if not hello.get("ready"):
            self.close()
            raise RuntimeError(hello.get("error") or "ตัวถอดเสียงของ Apple ไม่พร้อม")
        self.lock = threading.Lock()

    def _read(self) -> dict:
        import select
        if not select.select([self.p.stdout], [], [], self.TIMEOUT)[0]:
            raise RuntimeError("ตัวถอดเสียงของ Apple ไม่ตอบ")
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("ตัวถอดเสียงของ Apple ปิดไปแล้ว")
        return self.json.loads(line)

    def transcribe(self, audio: np.ndarray) -> str:
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        with self.lock:
            self.p.stdin.write(self.struct.pack("<I", len(pcm)) + pcm)
            reply = self._read()
        return fix_apple_wake(str(reply.get("text", "")).strip())

    def close(self) -> None:
        try:
            self.p.stdin.write(self.struct.pack("<I", 0))
            self.p.wait(timeout=1)
        except Exception:
            self.p.kill()


def apple_stt_available() -> bool:
    """macOS 26+ และมีคอมไพเลอร์ Swift (Command Line Tools) หรือคอมไพล์ไว้แล้ว"""
    try:
        major = int(platform.mac_ver()[0].split(".")[0] or 0)
    except ValueError:
        return False
    return platform.system() == "Darwin" and major >= 26 and APPLE_STT_SRC.exists() and \
        (APPLE_STT_BIN.exists() or subprocess.run(["xcrun", "--find", "swiftc"], capture_output=True).returncode == 0)


class STT:
    def __init__(self):
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")   # ไม่ต้องโชว์แถบโหลดทุกประโยค
        self.apple_silicon = platform.system() == "Darwin" and platform.machine() == "arm64"
        self.apple = None
        if STT_ENGINE in ("auto", "apple") and apple_stt_available():
            try:
                self.apple = AppleSTT()
            except Exception as e:
                print(f"⚠️  ใช้ตัวถอดเสียงของ Apple ไม่ได้ ({e}) → ใช้ Whisper แทน")
        elif STT_ENGINE == "apple":
            print("⚠️  ตัวถอดเสียงของ Apple ต้องใช้ macOS 26 ขึ้นไป → ใช้ Whisper แทน")
        self._whisper_ready = False
        # MLX ไม่รับประกันว่าเรียกพร้อมกันจากหลายเธรดได้ (ความเห็นที่สองรันในเธรดแยก + Whisper สำรองในเธรดฟัง)
        self._mlx_lock = threading.Lock()
        if self.apple is None:
            self._load_whisper()
        self.name = "Apple DictationTranscriber · th-TH (ในเครื่อง)" if self.apple else self.whisper_name
        self.second = self._second_repo()

    def _load_whisper(self) -> None:
        """Whisper: เป็นตัวหลักเมื่อไม่มีของ Apple หรือเป็นตัวสำรองเมื่อ Apple ล้ม (โหลดตอนจำเป็นเท่านั้น)"""
        if self._whisper_ready:
            return
        if self.apple_silicon:
            import mlx_whisper
            self._mlx = mlx_whisper
            self.whisper_name = f"mlx-whisper · {MLX_WHISPER_REPO}"
        else:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(FASTER_WHISPER_MODEL, device="cpu", compute_type="int8")
            self.whisper_name = f"faster-whisper · {FASTER_WHISPER_MODEL} int8 CPU"
        self._whisper_ready = True

    def close(self) -> None:
        if self.apple is not None:
            self.apple.close()

    def _second_repo(self) -> str:
        """รุ่นสำหรับความเห็นที่สอง (เฉพาะ Apple Silicon และต้องอยู่ในเครื่องแล้วถ้าเป็น auto)"""
        if not self.apple_silicon or STT_SECOND.lower() in ("off", "0", "no", ""):
            return ""
        repo = TYPHOON_REPO if STT_SECOND.lower() == "auto" else STT_SECOND
        if repo == MLX_WHISPER_REPO:
            return ""
        if STT_SECOND.lower() == "auto":
            from huggingface_hub import try_to_load_from_cache
            if not isinstance(try_to_load_from_cache(repo, "config.json"), str):
                return ""
        return repo

    def warmup(self) -> None:
        """โหลดโมเดลล่วงหน้า (Whisper ครั้งแรกจะดาวน์โหลดจาก Hugging Face ~1.6 GB; ของ Apple อุ่นเครื่องตอนเปิดแล้ว)"""
        if self.apple is None:
            self.transcribe(np.zeros(RATE, dtype=np.float32))
        if self.second:
            self.transcribe_second(np.zeros(RATE, dtype=np.float32))

    def transcribe_second(self, audio: np.ndarray) -> str:
        """ถอดซ้ำด้วยรุ่นที่จูนภาษาไทย (ไม่ใช้คำใบ้ เพราะทำให้รุ่นนี้วน)"""
        if not self.second:
            return ""
        self._load_whisper()
        text = self._run(audio, None, repo=self.second)
        return "" if LOOP_RE.search(text) else collapse_repeats(text)

    @staticmethod
    def normalize(audio: np.ndarray) -> np.ndarray:
        """ปรับระดับเสียงพูดให้ราว -20 dBFS (ไมค์เบา/พูดไกล Whisper จะวนหรือถอดว่างบ่อย)"""
        frames = audio[: len(audio) // 480 * 480].reshape(-1, 480)
        if not len(frames):
            return audio
        rms = np.sqrt((frames ** 2).mean(1))
        speech = rms[rms >= np.percentile(rms, 60)].mean()
        if speech < 1e-4:                                 # เงียบสนิท ไม่ต้องขยาย (กันขยายเสียงรบกวน)
            return audio
        return np.clip(audio * min(0.1 / speech, 20.0), -1, 1).astype(np.float32)

    def _run(self, audio: np.ndarray, prompt: str | None, repo: str = "") -> str:
        with self._mlx_lock:
            return self._run_locked(audio, prompt, repo)

    def _run_locked(self, audio: np.ndarray, prompt: str | None, repo: str = "") -> str:
        audio = self.normalize(audio)
        # จำกัดจำนวน token ตามความยาวเสียง: พูด 2 วิ จะถอดยาวเป็นย่อหน้าไม่ได้ (ตัดอาการวน)
        max_tokens = min(224, int(15 * len(audio) / RATE) + 24)
        if self.apple_silicon:
            result = self._mlx.transcribe(audio, path_or_hf_repo=repo or MLX_WHISPER_REPO, language="th",
                                          initial_prompt=prompt, condition_on_previous_text=False,
                                          temperature=0.0, sample_len=max_tokens, verbose=None)
            segments = [(s["text"], s.get("avg_logprob", 0), s.get("compression_ratio", 0))
                        for s in result.get("segments", [])]
        else:
            segs, _ = self._model.transcribe(audio, language="th", initial_prompt=prompt, beam_size=5,
                                             temperature=0.0, condition_on_previous_text=False,
                                             vad_filter=False, max_new_tokens=max_tokens)
            segments = [(s.text, s.avg_logprob, s.compression_ratio) for s in segs]
        # ทิ้งท่อนที่ Whisper เองก็ไม่มั่นใจ หรือซ้ำซากผิดปกติ (compression ratio สูง = วน)
        return " ".join(t.strip() for t, logprob, ratio in segments if logprob >= -1.0 and ratio <= 2.4).strip()

    def transcribe(self, audio: np.ndarray) -> str:
        if self.apple is not None:
            try:
                return self.apple.transcribe(audio)
            except Exception as e:              # ตัวช่วยล้ม/ค้าง → ใช้ Whisper ต่อทั้งรอบนี้
                print(f"  ⚠️  ตัวถอดเสียงของ Apple ผิดพลาด ({e}) → สลับไปใช้ Whisper")
                self.apple.close()
                self.apple = None
                self._load_whisper()
                self.name = self.whisper_name
        text = self._run(audio, WHISPER_PROMPT if USE_WHISPER_PROMPT else None)
        if USE_WHISPER_PROMPT and LOOP_RE.search(text):     # วนเพราะคำใบ้ → ถอดใหม่แบบไม่มีคำใบ้หนึ่งครั้ง
            text = self._run(audio, None)
        return "" if LOOP_RE.search(text) else collapse_repeats(text)


# ════════════════════════════════════════════════════════════════════════════
# 10) โหมดการทำงาน
# ════════════════════════════════════════════════════════════════════════════

def check_voice() -> None:
    voices = read_cmd(["say", "-v", "?"])
    if not re.search(rf"^{re.escape(TTS_VOICE)}\s", voices, flags=re.M):
        print(f"⚠️  ไม่พบเสียง '{TTS_VOICE}' ในเครื่อง ติดตั้งได้ที่ System Settings › Accessibility › "
              "Spoken Content › System Voice › Manage Voices… › ภาษาไทย (Kanya)")


def run_text(text: str, dry_run: bool) -> None:
    speaker = SaySpeaker(enabled=not dry_run)
    assistant = Assistant(speaker, dry_run)
    print(f"🗣️  {text}")
    assistant.handle(text)
    speaker.wait()


def load_wav(path: str) -> np.ndarray:
    """อ่าน WAV 16-bit เป็น int16 mono 16 kHz"""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit("รองรับเฉพาะ WAV 16-bit")
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        channels, rate = w.getnchannels(), w.getframerate()
    audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != RATE:
        audio = np.interp(np.arange(0, len(audio), rate / RATE), np.arange(len(audio)), audio)
    return audio.astype(np.int16)


def run_wav(path: str, dry_run: bool) -> None:
    """ป้อนไฟล์เสียงผ่าน VAD + ตัดประโยค + STT เหมือนพูดใส่ไมค์ (ไม่มี AEC)"""
    from pysilero_vad import SileroVoiceActivityDetector
    audio = load_wav(path)
    audio = np.concatenate([audio, np.zeros(RATE * 2, dtype=np.int16)])   # เติมความเงียบท้ายไฟล์ให้ประโยคสุดท้ายจบ
    vad, seg = SileroVoiceActivityDetector(), Segmenter()
    utterances = []
    for i in range(0, len(audio) - VAD_CHUNK + 1, VAD_CHUNK):
        chunk = audio[i:i + VAD_CHUNK]
        u, _ = seg.feed(chunk, vad.process_chunk(chunk.tobytes()))
        if u is not None:
            utterances.append(u)
            vad.reset()
    print(f"🎧 {path}: ยาว {len(audio) / RATE:.1f} วิ ตัดได้ {len(utterances)} ประโยค")
    stt = STT()
    speaker = SaySpeaker(enabled=not dry_run)
    assistant = Assistant(speaker, dry_run)
    for u in utterances:
        t0 = time.perf_counter()
        text = stt.transcribe(u.astype(np.float32) / 32768.0)
        print(f"\n🗣️  {text or '(ว่าง)'}  ·  STT {(time.perf_counter() - t0) * 1000:.0f} ms ({len(u) / RATE:.1f} วิ)")
        if should_handle(text, assistant):
            assistant.handle(text)
            speaker.wait()


def private(text: str) -> str:
    """ข้อความที่ไม่ได้พูดกับน้องจาง (เช่น คนในบ้านคุยกัน) ไม่ต้องเก็บเนื้อหา แค่บอกความยาว"""
    return text if LOG_HEARD == "all" else f"({len(text)} ตัวอักษร)"


def should_handle(text: str, assistant: Assistant, recent=(), overlapped: bool = False) -> bool:
    """กรองข้อความที่ได้จากเสียง: ขยะของ Whisper, เสียงน้องจางเองที่ย้อนเข้าไมค์, ไม่ได้เรียก "น้องจาง"
    overlapped = ประโยคนี้เริ่มตอนน้องจางกำลังพูด (อาจเป็นเสียงสะท้อน) → ต้องเรียกชื่อเท่านั้น"""
    if not text or is_garbage(text):
        return False
    if overlapped and is_echo(text, recent):
        print(f"  (ตัดทิ้ง: เสียงของน้องจางเองที่ย้อนเข้าไมค์) {text}")
        return False
    if has_wake(text):
        return True
    if overlapped:
        print(f"  (ได้ยินระหว่างน้องจางพูด แต่ไม่ได้เรียกชื่อ — ข้าม) {private(text)}")
        return False
    # ช่วงคุยต่อโดยไม่ต้องเรียกชื่อ: เฉพาะหลังผู้ใช้คุยด้วยจริง นับจากตอนที่น้องจางพูดตอบจบ
    last = assistant.last_active
    if last and assistant.speaker._last_sound > last:
        last = assistant.speaker._last_sound
    if not last or time.monotonic() - last > FOLLOWUP_SEC:
        print(f"  💤 ไม่ได้เรียกน้องจาง — ข้าม: {private(text)}")
        return False
    return True


def run_mic(dry_run: bool) -> None:
    # ตัวเปิดแอป/run_app.sh ปิดด้วย SIGTERM → ให้เหมือน Ctrl+C (KeyboardInterrupt → finally: shutdown)
    # ไม่งั้นถ้าโดนปิดก่อนเมนูบาร์พร้อม หรือ MENUBAR=off เซิร์ฟเวอร์ LLM ในเครื่อง (~2.5 GB) จะค้าง
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.default_int_handler)
    watch_launcher()
    check_voice()
    print("⏳ กำลังโหลดตัวถอดเสียง ...")
    stt = STT()
    stt.warmup()
    try:
        speaker = Speaker()
    except Exception as e:           # เปิดลำโพงเองไม่ได้ → ใช้ say ตรงๆ แบบไม่มี AEC
        print(f"⚠️  เปิดลำโพงไม่ได้ ({e}) ใช้ say แทน (ไม่มีตัวตัดเสียงสะท้อน)")
        speaker = SaySpeaker()
    ears = Ears(speaker)
    assistant = Assistant(speaker, dry_run, background_web=True)
    ears.start()
    threading.Thread(target=warm_local_llm, daemon=True).start()

    duplex = ("full duplex + ตัดเสียงสะท้อน (AEC)" if ears.apm is not None and ears.barge_in else
              "full duplex ไม่มี AEC (ควรใส่หูฟัง)" if ears.barge_in else
              "half duplex (ไม่ฟังระหว่างน้องจางพูด)")
    llm_label = f"LM Studio · {_active_model() or 'ยังไม่มีโมเดลโหลดอยู่'} (Local AI)"
    wake = f"{', '.join(WAKE_WORDS)} (คุยต่อได้ {FOLLOWUP_SEC:.0f} วิโดยไม่ต้องเรียกซ้ำ)" if WAKE_WORDS \
        else "ไม่ใช้ (ฟังทุกประโยค)"
    print(f"""
╭─ Jarvis ไทย · น้องจาง ─────────────────────────────────
│ STT      : {stt.name}{f" · ความเห็นที่สอง {stt.second}" if stt.second else ""}
│ Router   : LM Studio Local AI · CONF_MIN {CONF_MIN:.2f} (คำสั่งเสี่ยง {CONF_RISKY:.2f})
│ LLM      : {llm_label}
│ เสียง     : {TTS_VOICE} @ {TTS_RATE}{f" ทุ้ม {TTS_PITCH}" if TTS_PITCH else ""} · {duplex}
│ คำปลุก    : {wake}{'  · DRY-RUN' if dry_run else ''}
╰─ พูดได้เลย เช่น "น้องจาง เปิดสปอติฟาย" (Ctrl+C เพื่อออก)""")
    speaker.say(pick("ready"))
    threading.Thread(target=warm_tts, daemon=True).start()   # เตรียมเสียงประโยคสำเร็จรูป (ตอบได้ทันที)

    def listen_loop() -> None:
        """ลูปหลัก: รอประโยคจากไมค์ → Whisper → กรอง → Assistant (ทำงานในเธรดแยกเมื่อมีเมนูบาร์)"""
        while True:
            audio, overlapped, interrupted = ears.utterances.get()
            try:
                t0 = time.perf_counter()
                text = stt.transcribe(audio)
                ms = (time.perf_counter() - t0) * 1000
                if SAVE_UTTERANCES:
                    save_utterance(audio, text)
                if not should_handle(text, assistant, speaker.recent, overlapped):
                    if interrupted:                  # ไม่ใช่ผู้ใช้เรียกจริง → พูดต่อจากที่ค้างไว้
                        speaker.resume()
                    continue
                if interrupted:                      # ผู้ใช้เรียกน้องจางจริง → ทิ้งประโยคที่พูดค้าง
                    speaker.stop()
                print(f"\n🗣️  {text}  ·  STT {ms:.0f} ms ({len(audio) / RATE:.1f} วิ)")
                assistant.handle(text, (lambda a=audio: stt.transcribe_second(a)) if stt.second else None)
                print(f"  ⏱  ตอบใน {(time.perf_counter() - t0) * 1000:.0f} ms นับจากพูดจบ (ถอดเสียง {ms:.0f} ms)")
            except Exception as e:                   # ห้ามให้ลูปฟังตายเงียบๆ
                print(f"  ❌ ผิดพลาดระหว่างประมวลผล: {type(e).__name__}: {e}")
                speaker.resume()

    try:
        if MENUBAR and run_menubar(ears, assistant, listen_loop):
            pass                                     # ออกจากเมนูบาร์ (ปุ่มออก หรือ Ctrl+C)
        else:
            listen_loop()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown(ears)


def watch_launcher() -> None:
    """เปิดผ่าน Jarvis.app: ถ้าตัวเปิดแอปตาย (Force Quit/kill -9) ให้ปิดตาม ไม่งั้นไมค์ค้างเปิดและเปิดซ้ำได้สองตัว"""
    if os.environ.get("JARVIS_APP") != "1":
        return
    ppid = os.getppid()
    if ppid == 1:                                   # ตัวเปิดตายไปก่อนเราจะเริ่ม
        os._exit(0)

    def watch() -> None:
        import select
        try:
            kq = select.kqueue()
            kq.control([select.kevent(ppid, select.KQ_FILTER_PROC, select.KQ_EV_ADD, select.KQ_NOTE_EXIT)], 0)
            kq.control(None, 1)                     # รอจนตัวเปิดแอปจบ
        except OSError:                             # ตัวเปิดหายไปแล้ว
            pass
        print("⚠️  ตัวเปิดแอปหายไป → ปิดน้องจางตาม")
        os.kill(os.getpid(), signal.SIGINT)         # ใช้ทางปิดปกติ (เก็บกวาดครบ)
        time.sleep(6)                               # เธรดหลักค้าง → ปิดแรงๆ
        LocalLLM.stop()
        os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


def shutdown(ears) -> None:
    """ปิดทุกอย่างให้เรียบร้อย (เรียกซ้ำได้): ไมค์ ลำโพง และเซิร์ฟเวอร์ LLM ในเครื่อง"""
    if getattr(shutdown, "done", False):
        return
    shutdown.done = True
    print("\n👋 ไว้คุยกันใหม่นะ")
    for close in (ears.close, ears.speaker.close, LocalLLM.stop):
        try:
            close()
        except Exception:
            pass


def run_menubar(ears: Ears, assistant: Assistant, listen_loop) -> bool:
    """ไอคอนบนแถบเมนู + HUD แบบ J.A.R.V.I.S ลอยบนจอ
    ลูปฟังย้ายไปเธรดแยก ส่วนเธรดหลักรัน event loop ของ macOS (AppKit ต้องอยู่เธรดหลัก)
    คืน False ถ้าใช้ไม่ได้ (ไม่มี PyObjC) เพื่อให้ไปใช้โหมดเทอร์มินัลอย่างเดียว"""
    try:
        import AppKit
        from Foundation import NSURL, NSObject, NSTimer
        from PyObjCTools import AppHelper
    except ImportError:
        print("ℹ️  ไม่มี PyObjC — ไม่แสดงไอคอนบนแถบเมนู (pip install pyobjc-framework-Cocoa)")
        return False

    symbols = {"off": "mic.slash.fill", "web": "globe", "control": "cursorarrow.click.2", "speak": "waveform",
               "think": "ellipsis.circle", "listen": "mic.fill"}
    labels = {"off": "ปิดการฟังอยู่", "web": "กำลังท่องเว็บ…", "control": "กำลังคุมเครื่อง…", "speak": "กำลังพูด…",
              "think": "กำลังคิด…", "listen": "กำลังฟัง — เรียก \"น้องจาง\" ได้เลย"}

    def current_state() -> str:
        job = assistant.job
        return ("off" if not ears.listening else "web" if job == "web" else "control" if job else
                "speak" if ears.speaker.playing else "think" if assistant.thinking else "listen")

    class Controller(NSObject):
        def toggle_(self, sender):
            ears.set_listening(not ears.listening)
            self.refresh_(None)

        def toggleHud_(self, sender):
            if self.hud is not None:
                self.hud.set_visible(not self.hud.visible)
                self.refresh_(None)

        def togglePin_(self, sender):
            if self.hud is not None:
                self.hud.set_pinned(not self.hud.pinned)
                self.refresh_(None)

        def quit_(self, sender):
            # ปุ่มออกของ AppKit จะปิดโปรเซสทันที (ไม่ผ่าน finally) → เก็บกวาดเองก่อน
            # ไม่งั้นเซิร์ฟเวอร์ LLM ในเครื่องค้างกินแรม ~2.5 GB และตำแหน่ง HUD ไม่ถูกบันทึก
            if self.hud is not None:
                self.hud.save()
            shutdown(ears)
            AppHelper.stopEventLoop()

        def tick_(self, timer):                      # 20 ครั้ง/วิ: ส่งสถานะ + ความดังเสียงให้ HUD
            if self.hud is not None:
                self.hud.push(current_state())

        def refresh_(self, timer):
            state = current_state()
            if state != getattr(self, "state", None):
                self.state = state
                image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbols[state], labels[state])
                image.setTemplate_(True)                 # สีตามธีมของแถบเมนูอัตโนมัติ
                self.item.button().setImage_(image)
                self.item.button().setToolTip_(f"น้องจาง: {labels[state]}")
                self.status.setTitle_(labels[state])
            self.toggle.setTitle_("เริ่มฟัง" if state == "off" else "หยุดฟัง")
            if self.hud is not None:
                self.hud_toggle.setTitle_("ซ่อน J.A.R.V.I.S" if self.hud.visible else "แสดง J.A.R.V.I.S")
                self.pin.setState_(AppKit.NSControlStateValueOn if self.hud.pinned else AppKit.NSControlStateValueOff)
            heard = assistant.last_text[:40] + ("…" if len(assistant.last_text) > 40 else "")
            self.heard.setTitle_(f"ได้ยินล่าสุด: {heard or '-'}")

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)   # ไม่มีไอคอนใน Dock
    ctl = Controller.alloc().init()
    ctl.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
    menu = AppKit.NSMenu.alloc().init()
    menu.setAutoenablesItems_(False)

    def add(title, action=None, key=""):
        item = menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
        item.setTarget_(ctl) if action else item.setEnabled_(False)
        return item

    ctl.status, ctl.heard = add(""), add("")
    menu.addItem_(AppKit.NSMenuItem.separatorItem())
    ctl.toggle = add("หยุดฟัง", "toggle:")
    ctl.hud_toggle = add("ซ่อน J.A.R.V.I.S", "toggleHud:")
    ctl.pin = add("ปักหมุด J.A.R.V.I.S ไว้บนสุด", "togglePin:")
    menu.addItem_(AppKit.NSMenuItem.separatorItem())
    add("ออกจากน้องจาง", "quit:", "q")
    ctl.item.setMenu_(menu)

    ctl.hud = None
    if HUD:
        try:
            ctl.hud = JarvisHUD(AppKit, NSURL, NSObject, ears, assistant, menu)
        except Exception as e:                   # HUD พังไม่ควรทำให้ทั้งระบบพัง
            print(f"⚠️  เปิด HUD ไม่ได้: {e}")
    if ctl.hud is None:
        ctl.hud_toggle.setHidden_(True)
        ctl.pin.setHidden_(True)
    ctl.refresh_(None)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.25, ctl, "refresh:", None, True)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.05, ctl, "tick:", None, True)

    threading.Thread(target=listen_loop, daemon=True).start()
    print("🎛  มีไอคอนไมค์บนแถบเมนูด้านบน" + (" และ J.A.R.V.I.S บนจอ (ลากย้ายได้ · ดับเบิลคลิก = หยุด/เริ่มฟัง"
                                                  " · คลิกขวา = เมนู)" if ctl.hud else ""))
    # Ctrl+C / ปิดแท็บเทอร์มินัล → ออกแบบเก็บกวาด (ตัวดักของ PyObjC ไม่ทำงานเมื่อไม่มี Dock icon)
    # ตัวจับเวลา tick ทำให้เธรดหลักรันโค้ด Python ทุก 50 ms ตัวดักสัญญาณจึงถูกเรียกทันเวลา
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: ctl.quit_(None))
    try:
        AppHelper.runEventLoop()
    finally:
        if ctl.hud is not None:
            ctl.hud.save()
    return True


class JarvisHUD:
    """หน้าต่าง HUD ลอยบนจอ: วงแหวนแบบ J.A.R.V.I.S (hud.html ใน WKWebView) ขยับตามเสียงและสถานะ
    ลากย้ายได้ · ดับเบิลคลิก = หยุด/เริ่มฟัง · คลิกขวา = เมนู · ปักหมุด = ลอยบนสุดทุก Space"""
    SIZE = (380, 450)
    STATE_FILE = ROOT / ".hud.json"                  # จำตำแหน่ง/ปักหมุด/แสดงไว้ข้ามการเปิดครั้งถัดไป

    def __init__(self, AppKit, NSURL, NSObject, ears, assistant, menu):
        import json
        import WebKit
        self.AppKit, self.ears, self.assistant, self.json = AppKit, ears, assistant, json
        saved = self._load()
        self.pinned, self.visible = saved.get("pinned", True), saved.get("visible", True)

        class HUDView(AppKit.NSView):
            """ชั้นบนสุดรับเมาส์ทั้งหมดเอง (ไม่ให้ WebView กิน) จึงลากหน้าต่างได้ทั้งวง"""
            def hitTest_(self, point):
                return self

            def mouseDown_(self, event):
                if event.clickCount() == 2:
                    ears.set_listening(not ears.listening)
                else:
                    self.window().performWindowDragWithEvent_(event)

            def rightMouseDown_(self, event):
                AppKit.NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self)

        w, h = self.SIZE
        screen = AppKit.NSScreen.mainScreen().visibleFrame()
        x = saved.get("x", screen.origin.x + screen.size.width - w - 20)
        y = saved.get("y", screen.origin.y + screen.size.height - h - 10)
        style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
        win = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(x, y, w, h), style, AppKit.NSBackingStoreBuffered, False)
        win.setOpaque_(False)
        win.setBackgroundColor_(AppKit.NSColor.clearColor())
        win.setHasShadow_(False)
        win.setHidesOnDeactivate_(False)
        win.setMovableByWindowBackground_(True)
        view = HUDView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, w, h))
        web = WebKit.WKWebView.alloc().initWithFrame_(view.bounds())
        web.setValue_forKey_(False, "drawsBackground")          # พื้นหลังโปร่งใส
        page = ROOT / "hud.html"
        web.loadFileURL_allowingReadAccessToURL_(NSURL.fileURLWithPath_(str(page)), NSURL.fileURLWithPath_(str(ROOT)))
        view.addSubview_(web)
        win.setContentView_(view)
        self.win, self.web = win, web
        self.set_pinned(self.pinned)
        self.set_visible(self.visible)

    def set_pinned(self, pinned: bool) -> None:
        """ปักหมุด = ลอยเหนือทุกหน้าต่าง อยู่ทุก Space รวมถึงแอปเต็มจอ"""
        A = self.AppKit
        self.pinned = pinned
        self.win.setLevel_(A.NSStatusWindowLevel if pinned else A.NSNormalWindowLevel)
        self.win.setCollectionBehavior_(
            A.NSWindowCollectionBehaviorCanJoinAllSpaces | A.NSWindowCollectionBehaviorFullScreenAuxiliary
            | A.NSWindowCollectionBehaviorStationary if pinned else A.NSWindowCollectionBehaviorDefault)

    def set_visible(self, visible: bool) -> None:
        self.visible = visible
        self.win.orderFrontRegardless() if visible else self.win.orderOut_(None)

    def push(self, state: str) -> None:
        # พิกัดบนจอแบบเดียวกับ OCR/เมาส์ (มุมซ้ายบนของจอหลัก) — เอเจนต์คุมคอมจะไม่อ่าน/ไม่คลิกเมาส์ตรงนี้
        # (เฉพาะตอนอยู่บนจอจริง: ซ่อนอยู่ หรืออยู่คนละ Space ไม่ต้องกันพื้นที่)
        on_screen = self.visible and self.win.isOnActiveSpace() and \
            bool(self.win.occlusionState() & self.AppKit.NSWindowOcclusionStateVisible)
        if on_screen:
            f, top = self.win.frame(), self.AppKit.NSScreen.screens()[0].frame().size.height
            self.assistant.hud_rect = (f.origin.x, top - f.origin.y - f.size.height, f.size.width, f.size.height)
        else:
            self.assistant.hud_rect = None
        if not self.visible:
            return
        ignore = state == "control"                   # ตอนคุมเครื่อง คลิกทะลุ HUD ไปหน้าต่างข้างล่างได้
        if self.win.ignoresMouseEvents() != ignore:
            self.win.setIgnoresMouseEvents_(ignore)
        speaker = self.ears.speaker
        level = getattr(speaker, "level", 0.0) if state == "speak" else self.ears.level
        recent = list(getattr(speaker, "recent", []))
        data = {"state": state, "level": round(level, 4), "pinned": self.pinned,
                "heard": self.assistant.last_text[-80:], "reply": recent[-1][1][:90] if recent else ""}
        self.web.evaluateJavaScript_completionHandler_(f"window.hud && hud.update({self.json.dumps(data)})", None)

    def _load(self) -> dict:
        try:
            return self.json.loads(self.STATE_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        origin = self.win.frame().origin
        try:
            self.STATE_FILE.write_text(self.json.dumps({"x": origin.x, "y": origin.y,
                                                        "pinned": self.pinned, "visible": self.visible}))
        except OSError:
            pass


# ── ชุดทดสอบ --eval: (กลุ่ม, ประโยค, ประเภทที่ถูก, ขั้นตอนที่ถูก [(action, app, volume)]) ──
EVAL_CASES = [
    ("คำสั่งเดี่ยว", "น้องจาง เปิดสปอติฟายให้หน่อย", "command", [("open_app", "Spotify")]),
    ("คำสั่งเดี่ยว", "น้องจ้าง ปิดไลน์ที", "command", [("quit_app", "LINE")]),
    ("คำสั่งเดี่ยว", "เพิ่มเสียงหน่อย", "command", [("volume_up",)]),
    ("คำสั่งเดี่ยว", "เบาเสียงลงหน่อยครับ", "command", [("volume_down",)]),
    ("คำสั่งเดี่ยว", "ตั้งเสียงไว้ที่ห้าสิบเปอร์เซ็นต์", "command", [("volume_set", None, 50)]),
    ("คำสั่งเดี่ยว", "ปิดเสียงเครื่องที", "command", [("volume_mute",)]),
    ("คำสั่งเดี่ยว", "ข้ามเพลงนี้ไปเลย", "command", [("music_next",)]),
    ("คำสั่งเดี่ยว", "เปิดโหมดมืดให้หน่อย", "command", [("dark_mode",)]),
    ("คำสั่งเดี่ยว", "ล็อกหน้าจอเลย", "command", [("lock_screen",)]),
    ("คำสั่งเดี่ยว", "ตอนนี้กี่โมงแล้ว", "command", [("tell_time",)]),
    ("คำสั่งเดี่ยว", "เปิดวีเอสโค้ดหน่อย", "command", [("open_app", "Visual Studio Code")]),
    ("คำสั่งเดี่ยว", "ขอ Google Chrome หน่อย", "command", [("open_app", "Google Chrome")]),
    ("คำสั่งเดี่ยว", "น้องจาง เปิดยูทูบให้หน่อย", "command", [("open_app", "YouTube")]),
    ("คำสั่งเดี่ยว", "ตั้งเสียงสามสิบห้า", "command", [("volume_set", None, 35)]),
    ("คำสั่งซ้อน", "หยุดเพลงแล้วเปิดสแล็ก", "command", [("music_pause",), ("open_app", "Slack")]),
    ("คำสั่งซ้อน", "ปิดซาฟารีแล้วเปิดโน้ต", "command", [("quit_app", "Safari"), ("open_app", "Notes")]),
    ("คำสั่งซ้อน", "เพิ่มเสียงแล้วก็เปลี่ยนเพลงถัดไป", "command", [("volume_up",), ("music_next",)]),
    ("คำสั่งซ้อน", "ปิด Slack แล้วตั้งเสียง 30", "command", [("quit_app", "Slack"), ("volume_set", None, 30)]),
    ("งานบนเว็บ", "น้องจาง หาห้องพักแถวเมืองทองธานีให้หน่อย", "command", [("web_task",)]),
    ("งานบนเว็บ", "ช่วยเช็คราคาไอโฟนรุ่นล่าสุดให้หน่อย", "command", [("web_task",)]),
    ("งานบนเว็บ", "พรุ่งนี้กรุงเทพฝนจะตกไหม", "command", [("web_task",)]),
    ("คุมคอม", "น้องจาง อ่านจอให้ฟังหน่อย", "command", [("read_screen",)]),
    ("คุมคอม", "น้องจาง อ่านจอได้รึเปล่า", "command", [("read_screen",)]),
    ("คุมคอม", "คลิกปุ่มบันทึกให้หน่อย", "command", [("computer_task",)]),
    ("คุมคอม", "เลื่อนลงอีกหน่อย", "command", [("computer_task",)]),
    ("คุมคอม", "พิมพ์คำว่าสวัสดีลงไปในช่องนี้ที", "command", [("computer_task",)]),
    ("คำถามทั่วไป", "เมืองหลวงของญี่ปุ่นคือที่ไหน", "question", []),
    ("คำถามทั่วไป", "ทำไมท้องฟ้าถึงเป็นสีฟ้า", "question", []),
    ("คำถามทั่วไป", "แนะนำวิธีนอนให้หลับง่ายหน่อย", "question", []),
    ("คำถามทั่วไป", "น้องจาง สวัสดี วันนี้เป็นยังไงบ้าง", "question", []),
    ("เสียงรบกวน", "เออ แม่ วันนี้กินข้าวที่ไหนดี", "noise", []),
    ("เสียงรบกวน", "อืม เอ่อ", "noise", []),
    ("เสียงรบกวน", "ขอบคุณที่รับชมค่ะ", "noise", []),
]


def matches(d: Decision, category: str, expected: list[tuple]) -> bool:
    if d.category != category:
        return False
    if category != "command":
        return True
    if len(d.steps) != len(expected):
        return False
    for got, exp in zip(d.steps, expected):
        action, app, vol = (*exp, None, None)[:3]
        if got.action != action or (app and got.app != app) or (vol is not None and got.volume != vol):
            return False
    return True


def describe_expected(category: str, expected: list[tuple]) -> str:
    if category != "command":
        return category
    return " + ".join(describe_step(Step(e[0], (e[1:2] or ["none"])[0] or "none", (*e[2:3], None)[0]))
                      for e in expected)


def run_eval() -> float:
    router = LocalDecisionEngine()
    rows = []   # (กลุ่ม, ถูกไหม, conf, ต่ำกว่าเกณฑ์ไหม, ms, tokens)
    for i, (group, text, category, expected) in enumerate(EVAL_CASES, 1):
        print(f"\n[{i:02d}/{len(EVAL_CASES)}] {group} · 「{text}」")
        try:
            d = router.decide(strip_wake(text))     # ตัดคำปลุกเหมือนตอนใช้งานจริง
        except LocalDecisionError as e:
            print(f"❌ {e}")
            rows.append((group, False, 0.0, True, 0.0, 0))
            continue
        ok, low = matches(d, category, expected), d.conf < required_conf(d)
        rows.append((group, ok, d.conf, low, d.ms, d.tokens))
        print(f"{'✅' if ok else '❌'} ได้ {describe(d)}  ·  conf {d.conf:.2f}{' ⚠ ต่ำกว่าเกณฑ์' if low else ''}"
              + ("" if ok else f"  ·  ที่ถูก: {describe_expected(category, expected)}"))

    n = len(rows)
    correct = sum(r[1] for r in rows)
    accuracy = correct / n * 100
    tokens = sum(r[5] for r in rows)
    print("\n" + "═" * 60)
    print(f"ผลรวม      : ถูก {correct}/{n} = {accuracy:.1f}%")
    for group in dict.fromkeys(r[0] for r in rows):
        g = [r for r in rows if r[0] == group]
        print(f"  {group:<12}: {sum(r[1] for r in g)}/{len(g)}")
    print(f"conf เฉลี่ย  : {sum(r[2] for r in rows) / n:.2f}")
    print(f"ต่ำกว่าเกณฑ์ : {sum(r[3] for r in rows)} ประโยค (CONF_MIN {CONF_MIN:.2f}, คำสั่งเสี่ยง {CONF_RISKY:.2f})")
    print(f"ถูกและผ่านเกณฑ์: {sum(r[1] and not r[3] for r in rows)}/{n}")
    print(f"Local AI เฉลี่ย: {sum(r[4] for r in rows) / n:.0f} ms ต่อประโยค")
    print("═" * 60)
    return accuracy


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis ไทย (น้องจาง) — ผู้ช่วยสั่งงาน macOS ด้วยเสียงภาษาไทย แบบ full duplex")
    parser.add_argument("--text", help="สั่งด้วยข้อความแทนเสียง")
    parser.add_argument("--dry-run", action="store_true", help="ไม่สั่งเครื่องจริง แค่ print ว่าจะทำอะไร")
    parser.add_argument("--eval", action="store_true", help="วัดความแม่นกับชุดประโยคทดสอบภาษาไทย")
    parser.add_argument("--wav", help="ป้อนไฟล์เสียง WAV แทนไมค์ (ไว้ทดสอบ VAD + STT)")
    parser.add_argument("--selftest", action="store_true",
                        help="ทดสอบสิทธิ์ อ่านจอ เมาส์ คีย์บอร์ด กับหน้าต่างทดสอบของตัวเอง (ใช้ผ่าน ./run_app.sh --selftest)")
    args = parser.parse_args()
    if args.selftest or os.environ.get("JARVIS_SELFTEST") == "1":
        from selftest import run_selftest
        sys.exit(0 if run_selftest() else 1)
    try:
        if args.eval:
            run_eval()
        elif args.text:
            run_text(args.text, args.dry_run)
        elif args.wav:
            run_wav(args.wav, args.dry_run)
        else:
            run_mic(args.dry_run)
    except LocalDecisionError as e:
        sys.exit(f"❌ {e}")


if __name__ == "__main__":
    main()
