"""Pure parsers retained from the supplied Mikan site; no legacy service imports."""
import html
import re
CN_NUMS={"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
def scope_number(value):
    if str(value).isdigit():return int(value)
    if "十" in value:
        a,b=value.split("十",1);return CN_NUMS.get(a,1)*10+CN_NUMS.get(b,0)
    return CN_NUMS.get(value)
def parse_size_to_bytes(value):
    m=re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?I?B)\s*",str(value),re.I)
    if not m:return None
    unit=m.group(2).upper().replace("I","");return int(float(m.group(1))*1024**{"B":0,"KB":1,"MB":2,"GB":3,"TB":4}[unit])


def parse_episode_info(title: str):
    """
    Parses episode information from an anime torrent title.
    Returns: (episode_start: int | None, episode_end: int | None, is_batch: bool, label: str)
    - If multi-episode batch: (start, end, True, f"E{start:02d}-E{end:02d}")
    - If single episode: (ep, ep, False, f"E{ep:02d}")
    - If cannot determine: (None, None, False, "UNSORTABLE")
    """
    if not title:
        return None, None, False, "UNSORTABLE"

    clean = title.strip()
    sanitized = re.sub(r'(1080|720|2160|480)[pP]', '', clean)
    sanitized = re.sub(r'x26[45]|h26[45]|hevc|avc|10-?bit|8-?bit|aac|opus|flac|mp3', '', sanitized, flags=re.IGNORECASE)
    # Season labels are not episode numbers or the start of a pack range.
    # In particular "Season 2 - 171" denotes a single episode, not 2..171.
    sanitized = re.sub(r'\bSeason\s*\d{1,2}\b|\b\d{1,2}(?:st|nd|rd|th)\s+Season\b|\bS\d{1,2}\b|第\s*[一二三四五六七八九十百\d]+\s*季',
                       ' ', sanitized, flags=re.IGNORECASE)

    # 1. Multi-episode / batch patterns: e.g. 01-12, 01~12, 01 - 12, EP01-EP12, E01-E12, [01-28]
    range_patterns = [
        r'(?:\[|\(|\s|第|\b)(?:EP|E)?(\d{1,3})\s*(?:-|~|–|—|至)\s*(?:EP|E)?(\d{1,3})\s*(?:集|话|話|完|END|v\d)?(?:\]|\)|\s|\b)',
        r'(?:合集|全集|全\s*(\d{1,3})\s*[话話集])'
    ]

    for pat in range_patterns:
        m = re.search(pat, sanitized, re.IGNORECASE)
        if m:
            if len(m.groups()) == 2 and m.group(1) and m.group(2):
                s, e = int(m.group(1)), int(m.group(2))
                if s <= e and e - s <= 200:
                    return s, e, True, f"E{s:02d}-E{e:02d} 合集"
            elif len(m.groups()) == 1 and m.group(1):
                total = int(m.group(1))
                return 1, total, True, f"E01-E{total:02d} 全集"

    if re.search(r'(合集|全集|Complete|BDRip\s+BOX|BD-BOX)', clean, re.IGNORECASE):
        return 1, 999, True, "全季合集"

    # 2. Single episode patterns
    single_patterns = [
        r'\[(\d{1,3})\s*\(\d{1,3}\)\]',                    # [01(49)] source-local plus absolute
        r'S\d{1,2}E(\d{1,3})\b',                              # S01E13
        r'(?:EP|E)(\d{1,3})\b',                               # E13, EP13
        r'第\s*(\d{1,3})\s*(?:话|話|集|期)',                    # 第13话, 第13集
        r'(?:-\s*|\s+)(\d{1,3})(?:v\d+)?(?:\s*\[|\s*\(|\s+)',# - 13 [, - 01v2 (
        r'\[(\d{1,3})(?:v\d+)?\]',                            # [13], [01v2]
        r'【(\d{1,3})(?:v\d+)?】',                            # 【13】
        r'-\s*(\d{1,3})(?:v\d+)?\s*$',                        # ends with - 13
        r'\[(?:[a-zA-Z0-9_]+)\]\s*\[(\d{1,3})(?:v\d+)?\]',    # [Group] [01]
    ]

    for pat in single_patterns:
        m = re.search(pat, sanitized, re.IGNORECASE)
        if m:
            ep = int(m.group(1))
            if 0 < ep < 2000:
                return ep, ep, False, f"E{ep:02d}"

    return None, None, False, "UNSORTABLE"

def parse_mikan_table_rows(table_html):
    resources = []
    rows = re.findall(r'<tr\b[^>]*>[\s\S]*?</tr>', table_html)
    for row in rows:
        mag_m = re.search(r'data-magnet=["\'](magnet:[^"\']+)["\']', row)
        link_m = re.search(r'<a[^>]*class=["\']magnet-link-wrap[^"\']*["\'][^>]*href=["\']/Home/Episode/([a-f0-9]+)["\'][^>]*>([\s\S]*?)</a>', row)
        if not link_m:
            continue
        res_hash = link_m.group(1).lower().strip()
        raw_title = re.sub(r'<[^>]+>', '', link_m.group(2)).strip()
        res_title = html.unescape(raw_title)
        magnet_url = html.unescape(mag_m.group(1).strip()) if mag_m else ""
        tor_m = re.search(r'href=["\'](/Download/[^"\']+\.torrent)["\']', row)
        torrent_url = f"https://mikanani.me{tor_m.group(1).strip()}" if tor_m else ""

        tds = re.findall(r'<td[^>]*>([\s\S]*?)</td>', row)
        size_str = ""
        pub_date_str = ""
        for td_content in tds:
            clean_td = re.sub(r'<[^>]+>', '', td_content).strip()
            if re.match(r'^\d+(\.\d+)?\s*(MB|GB|KB|B)$', clean_td, re.IGNORECASE):
                size_str = clean_td
            elif re.match(r'^\d{4}/\d{2}/\d{2}', clean_td):
                pub_date_str = clean_td

        is_batch = bool(re.search(r'(合集|全集|Fin|01-|\d+-\d+|01~|\d+~\d+|BDRip|BD-BOX|BOX)', res_title, re.IGNORECASE))
        resources.append({
            "resourceHash": res_hash,
            "title": res_title,
            "magnetUrl": magnet_url,
            "torrentUrl": torrent_url,
            "size": size_str,
            "sizeBytes": parse_size_to_bytes(size_str),
            "pubDate": pub_date_str,
            "isBatch": is_batch
        })
    return resources

def parse_season_and_clean_title(raw_title):
    raw_title = raw_title.strip()
    if any(k in raw_title for k in ("剧场版", "劇場版", "Movie", "OVA", "特别篇", "SP")):
        clean = re.sub(r'[\s\-_]*(?:剧场版|劇場版|Movie|OVA|特别篇|SP)[\s\-_]*', ' ', raw_title, flags=re.IGNORECASE).strip()
        clean = re.sub(r'\s+', ' ', clean)
        return clean or raw_title, 0, True, "剧场版 / 特别篇"

    m = re.search(r'第([0-9]+|[一二三四五六七八九十]+)[季期]', raw_title, re.IGNORECASE)
    if m:
        num_str = m.group(1)
        season = scope_number(num_str) or CN_NUMS.get(num_str, 1)
        clean = re.sub(r'[\s\-_]*第([0-9]+|[一二三四五六七八九十]+)[季期].*$', '', raw_title).strip()
        return clean or raw_title, season, False, f"第 {season} 季"

    m2 = re.search(r'(?:Season\s*|S)(\d+)', raw_title, re.IGNORECASE)
    if m2:
        season = int(m2.group(1))
        clean = re.sub(r'[\s\-_]*(?:Season\s*|S)\d+.*$', '', raw_title, flags=re.IGNORECASE).strip()
        return clean or raw_title, season, False, f"第 {season} 季"

    m3 = re.search(r'(\d+)(?:nd|rd|th|st)\s*Season', raw_title, re.IGNORECASE)
    if m3:
        season = int(m3.group(1))
        clean = re.sub(r'[\s\-_]*\d+(?:nd|rd|th|st)\s*Season.*$', '', raw_title, flags=re.IGNORECASE).strip()
        return clean or raw_title, season, False, f"第 {season} 季"

    return raw_title, 1, False, "第 1 季"

def _mikan_tag_attributes(tag):
    """Read quoted HTML attributes without depending on their order."""
    return {
        name.lower(): html.unescape(value)
        for name, _quote, value in re.findall(r"([\w:-]+)\s*=\s*([\"'])(.*?)\2", tag, re.S)
    }

def _mikan_plain_text(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()

def _mikan_weekday_number(value):
    match = re.search(r"星期([一二三四五六日天])", _mikan_plain_text(value))
    if not match:
        return None
    return {"日": 0, "天": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}[match.group(1)]

def _parse_mikan_weekday_schedule(markup, base_url=""):
    """Parse both the older sk-bangumi feed and Mikan's mobile AJAX fragment."""
    names = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"]
    groups = {day: [] for day in range(7)}
    seen = {day: set() for day in range(7)}
    base_url = str(base_url or "").rstrip("/")

    def append_item(day, bangumi_id, title, image="", date_label=""):
        if day not in groups or not bangumi_id or bangumi_id in seen[day] or not title:
            return
        seen[day].add(bangumi_id)
        if image.startswith("/") and base_url:
            image = base_url + image
        groups[day].append({
            "bangumiId": str(bangumi_id),
            "title": title,
            "image": image,
            "updateDate": date_label,
        })

    markup = markup or ""

    # The desktop/older mobile contract grouped entries in sk-bangumi divs.
    old_blocks = list(re.finditer(
        r'<div\b(?=[^>]*\bclass=["\'][^"\']*\bsk-bangumi\b[^"\']*["\'])'
        r'(?=[^>]*\bdata-dayofweek=["\'](\d+)["\'])[^>]*>',
        markup,
        re.I,
    ))
    for index, block in enumerate(old_blocks):
        try:
            day = int(block.group(1))
        except (TypeError, ValueError):
            continue
        if day not in groups:
            continue
        end = old_blocks[index + 1].start() if index + 1 < len(old_blocks) else len(markup)
        chunk = markup[block.end():end]
        for li_match in re.finditer(r"<li\b([^>]*)>([\s\S]*?)</li>", chunk, re.I):
            li_attrs, li = li_match.group(1), li_match.group(2)
            ident = re.search(r"data-bangumiid=[\"'](\d+)[\"']", li_attrs + " " + li, re.I)
            title = next((
                _mikan_tag_attributes(anchor).get("title")
                for anchor in re.findall(r"<a\b[^>]*>", li, re.I)
                if re.search(r"\ban-text\b", _mikan_tag_attributes(anchor).get("class", ""), re.I)
                and _mikan_tag_attributes(anchor).get("title")
            ), "")
            image = re.search(r"\bdata-src=[\"']([^\"']+)[\"']", li, re.I)
            date = re.search(
                r'<div\b(?=[^>]*\bclass=["\'][^"\']*\bdate-text\b[^"\']*["\'])[^>]*>([\s\S]*?)</div>',
                li, re.I,
            )
            if ident and title:
                append_item(
                    day, ident.group(1), _mikan_plain_text(title),
                    html.unescape(image.group(1)).strip() if image else "",
                    _mikan_plain_text(date.group(1)) if date else "",
                )

    # Mikan's current page loads this structure from
    # /Home/MobileBangumiCoverFlowByDayOfWeek. Each outer item is one weekday.
    mobile_blocks = list(re.finditer(
        r'<div\b(?=[^>]*\bclass=["\'][^"\']*\bm-home-week-item\b[^"\']*["\'])[^>]*>',
        markup,
        re.I,
    ))
    for index, block in enumerate(mobile_blocks):
        end = mobile_blocks[index + 1].start() if index + 1 < len(mobile_blocks) else len(markup)
        chunk = markup[block.end():end]
        weekday = re.search(r"<span\b[^>]*>([\s\S]*?)</span>", chunk, re.I)
        day = _mikan_weekday_number(weekday.group(1)) if weekday else None
        if day is None:
            continue
        for anchor in re.finditer(r"<a\b([^>]*)>([\s\S]*?)</a>", chunk, re.I):
            attrs = _mikan_tag_attributes(anchor.group(0).split(">", 1)[0] + ">")
            href = attrs.get("href", "")
            ident = re.search(r"/Home/Bangumi/(\d+)(?:[/?#]|$)", href, re.I)
            if not ident:
                continue
            inner = anchor.group(2)
            image_tag = re.search(r"<img\b[^>]*>", inner, re.I)
            image_attrs = _mikan_tag_attributes(image_tag.group(0)) if image_tag else {}
            image = image_attrs.get("data-src") or image_attrs.get("src") or ""
            title = attrs.get("title") or image_attrs.get("alt")
            if not title:
                small_title = re.search(
                    r'<div\b(?=[^>]*\bclass=["\'][^"\']*\bsmall-title\b[^"\']*["\'])[^>]*>([\s\S]*?)</div>',
                    chunk, re.I,
                )
                title = _mikan_plain_text(small_title.group(1)) if small_title else ""
            date = re.search(
                r'<[^>]*\bclass=["\'][^"\']*\bdate-text\b[^"\']*["\'][^>]*>([\s\S]*?)</[^>]+>',
                inner, re.I,
            )
            append_item(day, ident.group(1), _mikan_plain_text(title), image,
                        _mikan_plain_text(date.group(1)) if date else "")

    return [{"day": day, "label": names[day], "items": groups[day]} for day in range(7)]
