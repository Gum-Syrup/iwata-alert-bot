"""Netkeiba scraper for JRA race schedules, entries (shutuba), live odds, and horse history.
Uses only Python standard library so GitHub Actions runs fast with zero pip installs.
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import re
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

JOCKEY_ID_MIRAI = "01174"

VENUE_CODE_MAP = {
    "01": "札幌",
    "02": "函館",
    "03": "福島",
    "04": "新潟",
    "05": "東京",
    "06": "中山",
    "07": "中京",
    "08": "京都",
    "09": "阪神",
    "10": "小倉",
}

LEFT_VENUES = {"東京", "中京", "新潟"}
KANTO_VENUES = {"東京", "中山"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass
class RaceSummary:
    race_id: str
    venue: str
    race_num: int
    race_name: str
    post_time: str  # "HH:MM"
    surface: str  # "芝" or "ダ" or "障"
    distance: int
    venue_turf_baba: str = ""
    venue_dirt_baba: str = ""


@dataclass
class HorseHistory:
    career_starts: int = 0
    switch_type: str = "不明"  # "テン乗り", "手綱戻り", "継続騎乗", "新馬"
    is_switch: bool = False
    prev_jockey: str = ""
    prev_rank: Optional[int] = None
    prev_pop: Optional[int] = None
    prev_surface: str = ""
    prev_distance: int = 0
    prev_pos4: Optional[int] = None
    prev_tsuka: str = ""
    days_since: Optional[int] = None
    same_venue_top3: bool = False
    same_venue_win: bool = False
    is_tataki2: bool = False


@dataclass
class MiraiEntry:
    date: str  # "YYYYMMDD"
    race_id: str
    venue: str
    race_num: int
    race_name: str
    race_class: str  # "新馬", "未勝利", "1勝クラス", "2勝クラス", "3勝クラス", "OP/L", "重賞"
    post_time: str  # "HH:MM"
    surface: str  # "芝" or "ダ" or "障"
    distance: int
    baba: str  # "良", "稍", "重", "不", or ""
    field_size: int
    waku: int
    umaban: int
    horse_id: str
    horse_name: str
    sex_age: str
    kinryo: str
    trainer: str  # e.g. "美浦 西田"
    stable_region: str  # "美浦" or "栗東" or ""
    odds: Optional[float] = None
    popularity: Optional[int] = None
    odds_time: str = ""
    history: HorseHistory = field(default_factory=HorseHistory)
    shutuba_url: str = ""


def _http_get(url: str, referer: Optional[str] = None, retries: int = 2) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except Exception as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def _decode_jp(raw: bytes) -> str:
    # Check meta charset hint first
    head = raw[:2500].lower()
    if b"charset=euc-jp" in head:
        encodings = ("euc-jp", "utf-8")
    elif b"charset=utf-8" in head:
        encodings = ("utf-8", "euc-jp")
    else:
        encodings = ("utf-8", "euc-jp")
    for enc in encodings:
        try:
            text = raw.decode(enc)
            if any(k in text for k in ("競馬", "レース", "発走", "東京", "京都", "阪神", "race_id")):
                return text
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_lib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_baba(raw_baba: str) -> str:
    if not raw_baba:
        return ""
    if "不良" in raw_baba or raw_baba == "不":
        return "不"
    if "稍" in raw_baba:
        return "稍"
    if "重" in raw_baba:
        return "重"
    if "良" in raw_baba:
        return "良"
    return ""


def fetch_day_races(date_yyyymmdd: str) -> List[RaceSummary]:
    """Fetch all JRA races scheduled on YYYYMMDD from netkeiba's race_list_sub fragment."""
    url = f"https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={date_yyyymmdd}"
    text = _decode_jp(_http_get(url))

    # Split by venue blocks (<dl class="RaceList_DataList">)
    blocks = re.findall(
        r'<dl[^>]*class="[^"]*RaceList_DataList[^"]*"[^>]*>(.*?)</dl>',
        text,
        re.S,
    )
    results: List[RaceSummary] = []
    seen_ids = set()

    for blk in blocks:
        turf_m = re.search(r'class="Shiba"[^>]*>([^<]+)<', blk)
        dirt_m = re.search(r'class="Da"[^>]*>([^<]+)<', blk)
        venue_turf_baba = _normalize_baba(turf_m.group(1)) if turf_m else ""
        venue_dirt_baba = _normalize_baba(dirt_m.group(1)) if dirt_m else ""

        items = re.findall(
            r'<li[^>]*class="[^"]*RaceList_DataItem[^"]*"[^>]*>(.*?)</li>',
            blk,
            re.S,
        )
        for item in items:
            rid_m = re.search(r"race_id=(\d{12})", item)
            if not rid_m:
                continue
            rid = rid_m.group(1)
            if rid in seen_ids:
                continue
            seen_ids.add(rid)

            venue_code = rid[4:6]
            venue = VENUE_CODE_MAP.get(venue_code, "")
            race_num = int(rid[10:12])

            title_m = re.search(r'class="ItemTitle"[^>]*>(.*?)</span>', item, re.S)
            race_name = _strip_tags(title_m.group(1)) if title_m else ""

            time_m = re.search(r'class="RaceList_Itemtime"[^>]*>(.*?)</span>', item, re.S)
            post_time = ""
            if time_m:
                tm = re.search(r"(\d{1,2}:\d{2})", time_m.group(1))
                if tm:
                    hh, mm = tm.group(1).split(":")
                    post_time = f"{int(hh):02d}:{mm}"

            long_m = re.search(r'class="[^"]*RaceList_ItemLong[^"]*"[^>]*>(.*?)</span>', item, re.S)
            surface = ""
            distance = 0
            if long_m:
                dm = re.search(r"(芝|ダ|障)(\d+)", _strip_tags(long_m.group(1)))
                if dm:
                    surface = dm.group(1)
                    distance = int(dm.group(2))

            results.append(
                RaceSummary(
                    race_id=rid,
                    venue=venue,
                    race_num=race_num,
                    race_name=race_name,
                    post_time=post_time,
                    surface=surface,
                    distance=distance,
                    venue_turf_baba=venue_turf_baba,
                    venue_dirt_baba=venue_dirt_baba,
                )
            )

    results.sort(key=lambda r: (r.post_time or "99:99", r.race_id))
    return results


def _parse_race_class(
    race_name_raw_html: str,
    race_data02_text: str,
    fallback_race_name: str = "",
) -> str:
    """Accurately classify JRA race class from shutuba.html RaceName + RaceData02."""
    d2 = (
        race_data02_text.replace("１", "1")
        .replace("２", "2")
        .replace("３", "3")
    )
    if "新馬" in d2:
        return "新馬"
    if "未勝利" in d2:
        return "未勝利"
    if "1勝クラス" in d2 or "500万" in d2:
        return "1勝クラス"
    if "2勝クラス" in d2 or "1000万" in d2:
        return "2勝クラス"
    if "3勝クラス" in d2 or "1600万" in d2:
        return "3勝クラス"

    # Graded stakes icons on netkeiba:
    # Icon_GradeType1 = G1, Icon_GradeType2 = G2, Icon_GradeType3 = G3
    # Icon_GradeType15 = L (Listed), Icon_GradeType5 = OP
    if re.search(r"Icon_GradeType[123]\b", race_name_raw_html):
        return "重賞"
    if re.search(r"\((G1|G2|G3|GI|GII|GIII)\)", race_name_raw_html):
        return "重賞"
    if "オープン" in d2 or re.search(r"Icon_GradeType(5|15)\b", race_name_raw_html):
        return "OP/L"

    # Fallback: check normalized race title when RaceData02 omits class labels
    title_norm = (
        (_strip_tags(race_name_raw_html) + " " + fallback_race_name)
        .replace("１", "1")
        .replace("２", "2")
        .replace("３", "3")
    )
    if "新馬" in title_norm:
        return "新馬"
    if "未勝利" in title_norm:
        return "未勝利"
    if "1勝クラス" in title_norm:
        return "1勝クラス"
    if "2勝クラス" in title_norm:
        return "2勝クラス"
    if "3勝クラス" in title_norm:
        return "3勝クラス"
    return "その他"


def fetch_mirai_entry_from_shutuba(summary: RaceSummary, date_yyyymmdd: str) -> Optional[MiraiEntry]:
    """Fetch shutuba.html for one race; return MiraiEntry if Mirai Iwata (01174) rides, else None."""
    url = f"https://race.netkeiba.com/race/shutuba.html?race_id={summary.race_id}"
    text = _decode_jp(_http_get(url))

    jockey_marker = f"jockey/result/recent/{JOCKEY_ID_MIRAI}/"
    if jockey_marker not in text:
        return None

    rn_m = re.search(r'<h1[^>]*class="RaceName"[^>]*>(.*?)</h1>', text, re.S)
    rn_raw = rn_m.group(1) if rn_m else ""
    race_name = _strip_tags(rn_raw) or summary.race_name

    d1_m = re.search(r'class="RaceData01"[^>]*>(.*?)</div>', text, re.S)
    d1_txt = _strip_tags(d1_m.group(1)) if d1_m else ""

    d2_m = re.search(r'class="RaceData02"[^>]*>(.*?)</div>', text, re.S)
    d2_txt = _strip_tags(d2_m.group(1)) if d2_m else ""

    race_class = _parse_race_class(rn_raw, d2_txt, summary.race_name)

    # Post time
    post_time = summary.post_time
    tm = re.search(r"(\d{1,2}:\d{2})発走", d1_txt)
    if tm:
        hh, mm = tm.group(1).split(":")
        post_time = f"{int(hh):02d}:{mm}"

    # Surface & distance
    surface = summary.surface
    distance = summary.distance
    dm = re.search(r"(芝|ダ|障)(\d+)m", d1_txt)
    if dm:
        surface = dm.group(1)
        distance = int(dm.group(2))

    # Track condition: prefer live RaceData01, fall back to venue header in race_list_sub
    baba = ""
    bm = re.search(r"(?:馬場:|予報\s*|/\s*)(良|稍重|稍|重|不良|不)(?:\s|/|$)", d1_txt)
    if bm:
        baba = _normalize_baba(bm.group(1))
    if not baba:
        baba = summary.venue_dirt_baba if surface == "ダ" else summary.venue_turf_baba

    # Field size
    fm = re.search(r"(\d+)頭", d2_txt)
    field_size = int(fm.group(1)) if fm else 0

    # Find Mirai's row (must match jockey URL specifically, not trainer ID 01174!)
    rows = re.findall(r'<tr[^>]*class="HorseList[^"]*"[^>]*>(.*?)</tr>', text, re.S)
    for row in rows:
        if jockey_marker not in row:
            continue
        # Skip scratched/cancelled horses
        if "Cancel" in row or "取消" in row or "除外" in row:
            return None

        tds = [_strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        hid_m = re.search(r"horse/(\d+)", row)
        horse_id = hid_m.group(1) if hid_m else ""

        waku = int(tds[0]) if len(tds) > 0 and tds[0].isdigit() else 0
        umaban = int(tds[1]) if len(tds) > 1 and tds[1].isdigit() else 0
        horse_name = tds[3] if len(tds) > 3 else ""
        sex_age = tds[4] if len(tds) > 4 else ""
        kinryo = tds[5] if len(tds) > 5 else ""
        trainer = tds[7] if len(tds) > 7 else ""
        if "美浦" in trainer:
            stable_region = "美浦"
        elif "栗東" in trainer:
            stable_region = "栗東"
        else:
            stable_region = ""

        return MiraiEntry(
            date=date_yyyymmdd,
            race_id=summary.race_id,
            venue=summary.venue,
            race_num=summary.race_num,
            race_name=race_name,
            race_class=race_class,
            post_time=post_time,
            surface=surface,
            distance=distance,
            baba=baba,
            field_size=field_size or len(rows),
            waku=waku,
            umaban=umaban,
            horse_id=horse_id,
            horse_name=horse_name,
            sex_age=sex_age,
            kinryo=kinryo,
            trainer=trainer,
            stable_region=stable_region,
            shutuba_url=url,
        )

    return None


def fetch_live_odds(race_id: str) -> Tuple[Dict[int, Tuple[float, int]], str]:
    """Fetch live win odds & popularity from netkeiba JRA odds API.

    Returns:
        ({umaban: (win_odds, popularity)}, official_datetime_str)
    """
    api_url = (
        f"https://race.netkeiba.com/api/api_get_jra_odds.html"
        f"?race_id={race_id}&type=1&action=init&compress=0"
    )
    referer = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    try:
        raw = _http_get(api_url, referer=referer).decode("utf-8", errors="replace")
        payload = json.loads(raw)
        data = payload.get("data")
        if isinstance(data, str) and data:
            data = json.loads(zlib.decompress(base64.b64decode(data)).decode("utf-8"))
        if isinstance(data, dict) and data.get("odds", {}).get("1"):
            odds_map: Dict[int, Tuple[float, int]] = {}
            for uma_str, vals in data["odds"]["1"].items():
                if not uma_str.isdigit() or not isinstance(vals, list) or len(vals) < 3:
                    continue
                try:
                    odd_val = float(vals[0])
                    pop_val = int(vals[2])
                    if odd_val > 0 and pop_val > 0:
                        odds_map[int(uma_str)] = (odd_val, pop_val)
                except (ValueError, TypeError):
                    continue
            return odds_map, str(data.get("official_datetime", ""))
    except Exception:
        pass

    # Fallback: if race already finished (e.g. testing on a past date), read result.html
    try:
        res_url = f"https://race.netkeiba.com/race/result.html?race_id={race_id}"
        text = _decode_jp(_http_get(res_url))
        odds_map = {}
        for row in re.findall(r'<tr[^>]*class="HorseList[^"]*"[^>]*>(.*?)</tr>', text, re.S):
            tds = [_strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(tds) > 10 and tds[2].isdigit() and tds[9].isdigit():
                try:
                    odds_map[int(tds[2])] = (float(tds[10]), int(tds[9]))
                except ValueError:
                    continue
        if odds_map:
            return odds_map, "確定(過去検証)"
    except Exception:
        pass

    return {}, ""


@dataclass
class PastRun:
    venue: str
    surface: str
    distance: int
    baba: str
    rank: str
    margin: str
    tsuka: str
    agari: str


@dataclass
class RivalHorse:
    waku: int
    umaban: int
    horse_name: str
    sex_age: str
    jockey: str
    kinryo: str
    style: str
    trainer: str
    odds: Optional[float] = None
    popularity: Optional[int] = None
    past_runs: List[PastRun] = field(default_factory=list)


def fetch_race_card_for_prompt(
    race_id: str,
    odds_map: Dict[int, Tuple[float, int]],
) -> List[RivalHorse]:
    """Fetch all runners + recent past runs in 1 request from shutuba_past.html."""
    url = f"https://race.netkeiba.com/race/shutuba_past.html?race_id={race_id}"
    text = _decode_jp(_http_get(url))

    rows = re.findall(r'<tr[^>]*class="HorseList[^"]*"[^>]*>(.*?)</tr>', text, re.S)
    runners: List[RivalHorse] = []

    for row in rows:
        if "Cancel" in row or "取消" in row or "除外" in row:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 6:
            continue

        waku_s = _strip_tags(tds[0])
        uma_s = _strip_tags(tds[1])
        if not (waku_s.isdigit() and uma_s.isdigit()):
            continue

        waku = int(waku_s)
        umaban = int(uma_s)

        # Horse name, trainer, running style from TD[3]
        h2_m = re.search(r'class="Horse02"[^>]*>(.*?)</div>', tds[3], re.S)
        horse_name = _strip_tags(h2_m.group(1)) if h2_m else ""
        td3_txt = _strip_tags(tds[3])
        tr_m = re.search(r"(美浦・\S+|栗東・\S+|地方・\S+|海外・\S+)", td3_txt)
        trainer = tr_m.group(1) if tr_m else ""
        st_m = re.search(r"(?:美浦・\S+|栗東・\S+|地方・\S+|海外・\S+)\s+(逃|先|差|追|マ)", td3_txt)
        style = st_m.group(1) if st_m else ""

        # Sex/age, jockey, weight from TD[4]
        td4_txt = _strip_tags(tds[4])
        sa_m = re.search(r"([牡牝セ]\d+)", td4_txt)
        sex_age = sa_m.group(1) if sa_m else ""
        jk_m = re.search(r"<a[^>]*jockey/(?:result/recent/)?\d+/?[^>]*>(.*?)</a>", tds[4], re.S)
        jockey = _strip_tags(jk_m.group(1)) if jk_m else ""
        wt_m = re.search(r"(\d{2}\.\d)", td4_txt)
        kinryo = wt_m.group(1) if wt_m else ""

        # Parse up to 3 actual past runs from TD[5:] (skipping layoff divider cells)
        past_runs: List[PastRun] = []
        for cell in tds[5:]:
            c_txt = _strip_tags(cell)
            if not re.match(r"^\d{4}\.\d{2}\.\d{2}\s+", c_txt):
                continue
            # Format: "2026.08.29 札幌 5 千歳特別 2勝 ダ1700 1:44.7 稍 12頭 9番 3人 武豊 53.0 2-2-2-1 (37.1) 480(0) 馬名 (0.6)"
            m_head = re.match(r"^\d{4}\.\d{2}\.\d{2}\s+(\S+)\s+(\S+)", c_txt)
            p_venue = m_head.group(1) if m_head else ""
            p_rank = m_head.group(2) if m_head else ""
            m_dist = re.search(r"(芝|ダ|障)(\d+)", c_txt)
            p_surf = m_dist.group(1) if m_dist else ""
            p_dist = int(m_dist.group(2)) if m_dist else 0
            m_baba = re.search(r"[\d:.]+\s+(良|稍|重|不)\b", c_txt)
            p_baba = m_baba.group(1) if m_baba else ""
            m_tsuka = re.search(r"(\d+(?:-\d+)+)\s+\(([\d.]+)\)", c_txt)
            p_tsuka = m_tsuka.group(1) if m_tsuka else ""
            p_agari = m_tsuka.group(2) if m_tsuka else ""
            m_margin = re.search(r"\(([+-]?\d+\.\d+)\)\s*$", c_txt)
            p_margin = m_margin.group(1) if m_margin else ""

            past_runs.append(
                PastRun(
                    venue=p_venue,
                    surface=p_surf,
                    distance=p_dist,
                    baba=p_baba,
                    rank=p_rank,
                    margin=p_margin,
                    tsuka=p_tsuka,
                    agari=p_agari,
                )
            )
            if len(past_runs) >= 3:
                break

        odd_val, pop_val = odds_map.get(umaban, (None, None))
        runners.append(
            RivalHorse(
                waku=waku,
                umaban=umaban,
                horse_name=horse_name,
                sex_age=sex_age,
                jockey=jockey,
                kinryo=kinryo,
                style=style,
                trainer=trainer,
                odds=odd_val,
                popularity=pop_val,
                past_runs=past_runs,
            )
        )

    runners.sort(key=lambda r: r.umaban)
    return runners


def fetch_horse_history(horse_id: str, venue: str, date_yyyymmdd: str) -> HorseHistory:
    """Fetch complete pre-race history for one horse from db.netkeiba.com/horse/result/{id}/."""
    if not horse_id:
        return HorseHistory()

    url = f"https://db.netkeiba.com/horse/result/{horse_id}/"
    text = _decode_jp(_http_get(url))

    m_tbl = re.search(r'<table[^>]*db_h_race_results[^>]*>(.*?)</table>', text, re.S)
    if not m_tbl:
        return HorseHistory(career_starts=0, switch_type="新馬", is_switch=False)

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m_tbl.group(1), re.S)
    if len(rows) <= 1:
        return HorseHistory(career_starts=0, switch_type="新馬", is_switch=False)

    headers = [re.sub(r"\s+", "", _strip_tags(c)) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", rows[0], re.S)]
    cur_dt = datetime.strptime(date_yyyymmdd, "%Y%m%d")

    prev_races: List[Tuple[datetime, Dict[str, str]]] = []
    for row in rows[1:]:
        cols = [_strip_tags(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)]
        if len(cols) < 12:
            continue
        rec = {headers[i]: cols[i] for i in range(min(len(headers), len(cols)))}
        d_str = rec.get("日付", "").strip()
        try:
            pdt = datetime.strptime(d_str, "%Y/%m/%d")
        except ValueError:
            continue
        if pdt < cur_dt:
            prev_races.append((pdt, rec))

    prev_races.sort(key=lambda x: x[0], reverse=True)
    if not prev_races:
        return HorseHistory(career_starts=0, switch_type="新馬", is_switch=False)

    pdt, prev = prev_races[0]
    days_since = (cur_dt - pdt).days
    prev_jockey = prev.get("騎手", "").strip()
    ever_mirai = any("岩田望" in pr.get("騎手", "") for _, pr in prev_races)

    if "岩田望" in prev_jockey:
        switch_type = "継続騎乗"
        is_switch = False
    elif ever_mirai:
        switch_type = "手綱戻り"
        is_switch = True
    else:
        switch_type = "テン乗り"
        is_switch = True

    dm = re.search(r"(芝|ダ|障)(\d+)", prev.get("距離", ""))
    prev_surface = dm.group(1) if dm else ""
    prev_distance = int(dm.group(2)) if dm else 0

    pr_s = prev.get("着順", "").strip()
    prev_rank = int(pr_s) if pr_s.isdigit() else None
    pp_s = prev.get("人気", "").strip()
    prev_pop = int(pp_s) if pp_s.isdigit() else None

    prev_tsuka = prev.get("通過", "").strip()
    pos_nums = [int(x) for x in re.findall(r"\d+", prev_tsuka)]
    prev_pos4 = pos_nums[-1] if pos_nums else None

    same_venue_top3 = any(
        (venue in pr.get("開催", "")) and pr.get("着順", "").strip() in ("1", "2", "3")
        for _, pr in prev_races
    )
    same_venue_win = any(
        (venue in pr.get("開催", "")) and pr.get("着順", "").strip() == "1"
        for _, pr in prev_races
    )

    is_tataki2 = False
    if len(prev_races) >= 2:
        prev_gap = (pdt - prev_races[1][0]).days
        if prev_gap >= 60 and days_since <= 49:
            is_tataki2 = True

    return HorseHistory(
        career_starts=len(prev_races),
        switch_type=switch_type,
        is_switch=is_switch,
        prev_jockey=prev_jockey,
        prev_rank=prev_rank,
        prev_pop=prev_pop,
        prev_surface=prev_surface,
        prev_distance=prev_distance,
        prev_pos4=prev_pos4,
        prev_tsuka=prev_tsuka,
        days_since=days_since,
        same_venue_top3=same_venue_top3,
        same_venue_win=same_venue_win,
        is_tataki2=is_tataki2,
    )
