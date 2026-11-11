"""[INPUT] nothing.
[OUTPUT] Shared vocabularies: statuses, platforms, countries, metric fields, labels.
[POS] The single place that defines BeatSocial's standard values; every other module imports from here.
[PROTOCOL] Adding a value here is a schema change: update docs/架构.md and the web labels together.
"""
from __future__ import annotations

# Post lifecycle. Internal code -> Chinese label shown to people.
STATUSES = {
    "todo": "待写",
    "review": "待审",
    "rejected": "打回",
    "approved": "已批准",
    "published": "已发布",
    "measured": "已回测",
    "cancelled": "取消",
}
STATUS_ORDER = ["todo", "review", "rejected", "approved", "published", "measured", "cancelled"]

# Platforms: code -> display name.
PLATFORMS = {
    "x": "X",
    "threads": "Threads",
    "linkedin": "LinkedIn",
    "youtube": "YouTube",
    "telegram": "Telegram",
    "qiita": "Qiita",
    "note": "note",
    "velog": "Velog",
    "geeknews": "GeekNews",
    "naver": "Naver 博客",
    "tistory": "Tistory",
    "habr": "Habr",
    "dzen": "Dzen",
    "vcru": "vc.ru",
    "rutube": "Rutube / VK Video",
    "blog": "官网博客",
    "bilibili": "B站",
    "douyin": "抖音 / 小红书 / 视频号",
    "xiaohongshu": "小红书",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "reddit": "Reddit",
    "other": "其他",
}

# Markets (the column is still called "country" in the database for compatibility).
# A market is a language audience with its own marketing focus, not a nation.
COUNTRIES = {
    "GLOBAL": "英文市场",
    "JP": "日文市场",
    "KR": "韩文市场",
    "TW": "繁中市场",
    "RU": "俄语市场",
    "CN": "中文市场（大陆）",
}
# Markets BeatSocial manages. CN (mainland) stays in the vocabulary for old data but is run by the owner elsewhere.
MARKET_ORDER = ["GLOBAL", "JP", "KR", "TW", "RU"]

# Markets start empty; each team writes its own focus with `beatsocial market set`.
MARKET_DEFAULTS = {code: {"focus": "", "audience": "", "cta": ""} for code in COUNTRIES}
def _owner():
    """The person who approves. Set BEATSOCIAL_OWNER or "owner" in ~/.beatsocial.json; defaults to 主人."""
    import json
    import os

    if os.environ.get("BEATSOCIAL_OWNER"):
        return os.environ["BEATSOCIAL_OWNER"]
    try:
        with open(os.path.expanduser("~/.beatsocial.json"), encoding="utf-8") as fh:
            return json.load(fh).get("owner") or "主人"
    except (OSError, ValueError):
        return "主人"


OWNER = _owner()

ACCOUNT_STATUSES = {"active": "运营中", "paused": "暂停", "pending": "待开通"}

# Standard metric fields. Unknown stays NULL, never 0.
METRIC_FIELDS = ["impressions", "likes", "replies", "reposts", "quotes", "bookmarks", "clicks", "followers_delta"]
METRIC_WINDOWS = ["h24", "d7", "d30"]
METRIC_LABELS = {
    "impressions": "曝光",
    "likes": "点赞",
    "replies": "回复",
    "reposts": "转发",
    "quotes": "引用",
    "bookmarks": "收藏",
    "clicks": "点击",
    "followers_delta": "涨粉",
}

MEDIA_KINDS = {"image": "图片", "video": "视频"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}

RULE_STATUSES = {"proposed": "待确认", "active": "生效中", "rejected": "不采纳", "retired": "已停用"}

# Approved posts more than this many hours past their slot are "overdue".
OVERDUE_HOURS = 6
# A claim older than this can be taken over by another bot.
CLAIM_TTL_HOURS = 2


def media_kind_for(path: str) -> str:
    import os

    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    return "image"
