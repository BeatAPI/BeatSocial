"""[INPUT] A post's current version text, its account platform/lang, and its media slots.
[OUTPUT] A list of flags: {code, level, msg}. level is block (cannot approve), price (needs a
         person to confirm prices on approval) or warn (shown, does not block).
[POS] Pre-approval checks. Computed on every read, never stored, so nobody can clear a flag by hand.
[PROTOCOL] Extra competitor names live in <data>/memory/lint.json {"competitors": [...]}.
"""
from __future__ import annotations

import json
import os
import re

# Competing platforms, not model names: Kling, Runway, Veo etc. are models we sell, and our own price for them is fine.
DEFAULT_COMPETITORS = [
    "fal", "kie", "sume", "replicate", "higgsfield", "openrouter", "pollo",
    "wavespeed", "aimlapi", "官方价", "官网价", "公式価格",
]

PLACEHOLDER_RE = re.compile(r"\[待[补核定]|【待[补核定]|【N】|\bTODO\b|\bTBD\b|\bX{3,}\b|\{\{")
PRICE_RE = re.compile(
    r"(\$\s?\d|\d\s?(美元|美金|元|円|руб|₽)|¥\s?\d|免费|免費|\bfree\b|無料|бесплатн)",
    re.IGNORECASE,
)
MONEY_RE = re.compile(r"(\$\s?\d|\d\s?(美元|美金|元|円|руб|₽)|¥\s?\d)")
URL_RE = re.compile(r"https?://|\b[a-z0-9-]+\.(io|com|ai|dev)/", re.IGNORECASE)
LIMITS = {"x": 280, "threads": 500, "linkedin": 3000, "telegram": 4096}
# Most images one post can carry on each platform (a post may have several media slots; one is the usual case).
MEDIA_MAX = {"x": 4, "threads": 20, "linkedin": 20, "telegram": 10, "instagram": 10}


def competitor_names(data_dir: str | None) -> list:
    names = list(DEFAULT_COMPETITORS)
    if data_dir:
        path = os.path.join(data_dir, "memory", "lint.json")
        if os.path.exists(path):
            try:
                names += json.load(open(path, encoding="utf-8")).get("competitors", [])
            except (ValueError, OSError):
                pass
    return [n.lower() for n in names if n]


def x_length(text: str) -> int:
    """X counts CJK and full-width characters as 2."""
    total = 0
    for ch in text:
        total += 2 if ord(ch) > 0x10FF else 1
    return total


def check(text: str, reply: str, platform: str, lang: str, zh_ref: str, media: list,
          data_dir: str | None = None, media_root: str | None = None) -> list:
    flags = []
    body = text or ""
    both = body + "\n" + (reply or "")

    if not body.strip():
        flags.append({"code": "empty", "level": "block", "msg": "正文是空的"})

    m = PLACEHOLDER_RE.search(both)
    if m:
        flags.append({"code": "placeholder", "level": "block", "msg": f"还有占位符未补：{m.group(0)}"})

    names = competitor_names(data_dir)
    for line in both.splitlines():
        low = line.lower()
        hit = next((n for n in names if re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", low)), None)
        if hit and MONEY_RE.search(line):
            flags.append({"code": "competitor_price", "level": "block",
                          "msg": f"写了第三方价格（{hit}）：默认只写自家价，改稿后再批"})
            break

    if PRICE_RE.search(both):
        flags.append({"code": "price", "level": "price", "msg": "含价格或“免费”：批准时需确认已对过当天官方价格"})

    for slot in media:
        f = slot.get("file")
        if not f:
            flags.append({"code": "media_missing", "level": "block",
                          "msg": f"媒体槽 {slot['slot']}（{slot.get('kind')}）还没有文件：{slot.get('spec') or ''}".strip()})
        else:
            path = f if os.path.isabs(f) or not media_root else os.path.join(media_root, f)
            if not os.path.exists(path):
                flags.append({"code": "media_missing", "level": "block",
                              "msg": f"媒体槽 {slot['slot']} 的文件找不到：{f}"})

    cap = MEDIA_MAX.get(platform)
    if cap and len(media) > cap:
        flags.append({"code": "too_many_media", "level": "block",
                      "msg": f"{platform} 一条最多 {cap} 个图片/视频，现在有 {len(media)} 个"})
    if platform == "x" and len(media) > 1 and any(m.get("kind") == "video" for m in media):
        flags.append({"code": "x_video_mix", "level": "block", "msg": "X 一条帖子不能同时放视频和其他图片"})

    limit = LIMITS.get(platform)
    if limit:
        n = x_length(body) if platform == "x" else len(body)
        if n > limit:
            flags.append({"code": "too_long", "level": "warn", "msg": f"正文 {n} 字，超过 {platform} 上限 {limit}"})

    if platform == "x" and URL_RE.search(body):
        flags.append({"code": "link_in_body", "level": "warn", "msg": "X 正文里有链接：按规则链接放第一条回复或图上"})

    if lang and not lang.startswith("zh") and body.strip() and not (zh_ref or "").strip():
        flags.append({"code": "no_zh_ref", "level": "warn", "msg": "非中文稿还没有中文参考（zh_ref）"})

    return flags


def blocking(flags: list) -> list:
    return [f for f in flags if f["level"] == "block"]


def needs_price(flags: list) -> bool:
    return any(f["level"] == "price" for f in flags)
