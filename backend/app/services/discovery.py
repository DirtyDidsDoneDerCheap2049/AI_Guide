"""Editorial starting points and provider photos, never model-invented links."""
from urllib.parse import quote, urlsplit

CITY_STARTERS = {
    '杭州': ['西湖', '灵隐寺', '西溪国家湿地公园'],
    '上海': ['外滩', '武康大楼', '豫园'],
    '北京': ['故宫博物院', '天坛公园', '颐和园'],
    '成都': ['成都大熊猫繁育研究基地', '人民公园', '武侯祠'],
    '西安': ['秦始皇帝陵博物院', '大雁塔', '西安城墙'],
    '广州': ['广州塔', '沙面', '陈家祠'],
}

# Reviewed source pages on 2026-09-19. Links are references, not live opening-hour facts.
SOURCES = [
    {"city": "杭州", "title": "杭州城市漫步：西湖、运河与文化古迹", "kind": "guide", "source": "杭州市文化广电旅游局", "url": "https://wgly.hangzhou.gov.cn/cw/cn/index.html", "published_at": None, "tags": ["西湖", "杭州", "运河", "良渚"]},
    {"city": "北京", "title": "北京景点与游览线路参考", "kind": "guide", "source": "北京旅游网", "url": "https://www.visitbeijing.com.cn/article/47Qoa2XKR8r", "published_at": None, "tags": ["北京", "故宫", "天坛"]},
    {"city": "杭州", "title": "西湖一日游视频", "kind": "video", "source": "B站 · 小喂的味旅程", "url": "https://www.bilibili.com/video/BV1uTZgYwERi", "published_at": "2025-03-28", "tags": ["西湖"]},
    {"city": "杭州", "title": "杭州旅行：西湖、灵隐与小众路线", "kind": "video", "source": "B站", "url": "https://www.bilibili.com/video/BV1wcAEzjELP/", "published_at": "2026-03-20", "tags": ["西湖", "灵隐", "杭州"]},
    {"city": "上海", "title": "人民广场—南京路—外滩漫步路线", "kind": "guide", "source": "上海市文旅推广网", "url": "https://www.meet-in-shanghai.net/cn/shanghai-citywalk-route-recommendation/explore-the-depths-of-huangpu-to-explore-modernity-352163/", "published_at": None, "tags": ["外滩", "南京路", "人民广场"]},
    {"city": "上海", "title": "武康路、安福路步行攻略", "kind": "video", "source": "B站", "url": "https://www.bilibili.com/video/BV1SN41127Ws/", "published_at": None, "tags": ["武康", "安福"]},
]


def resources_for(city: str, query: str = "") -> list[dict]:
    words = f"{city} {query}".strip()
    selected = [{**row, "reviewed_at": "2026-09-19", "is_search": False} for row in SOURCES if row["city"] in city and (not query or any(tag in query or query in tag for tag in row["tags"]))]
    selected.extend([
        {"title": f"搜索「{words}」攻略", "kind": "guide", "source": "必应搜索", "url": f"https://www.bing.com/search?q={quote(words + ' 旅游攻略')}", "is_search": True},
        {"title": f"去 B站搜「{words}」", "kind": "video", "source": "B站搜索", "url": f"https://search.bilibili.com/all?keyword={quote(words + ' 旅游攻略')}", "is_search": True},
    ])
    return selected


def provider_photos(raw: dict) -> list[dict]:
    photos = raw.get("photos") or []
    result = []
    if not isinstance(photos, list):
        return result
    for item in photos[:3]:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        try:
            parsed = urlsplit(item["url"])
            host = (parsed.hostname or "").lower()
            # AMap POI photo hosts; never create an arbitrary server-side fetch.
            if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
                continue
            if not any(host == domain or host.endswith('.' + domain) for domain in ("autonavi.com", "amap.com", "aoscdn.com")):
                continue
            result.append({"url": parsed._replace(scheme="https").geturl(), "title": str(item.get("title") or "地点图片")[:100], "source": "高德地点图片"})
        except ValueError:
            continue
    return result
