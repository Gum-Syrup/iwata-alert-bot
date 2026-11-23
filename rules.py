"""Evaluation rules for Mirai Iwata (岩田望来) alert bot based on 2026 JRA full-season data."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from scraper import KANTO_VENUES, LEFT_VENUES, MiraiEntry

# --- Configuration knobs (edit freely!) ---
DIRT_EXCLUDED_CLASSES = {"1勝クラス", "3勝クラス"}
DIRT_REQUIRE_GOOD_TRACK = True  # 良馬場のみ通知
DIRT_INCLUDE_TOP3_EVEN_IF_CONTINUOUS = True  # 1〜3人気は継続騎乗でも無視せず通知

TURF_EXCLUDED_VENUES = {"東京", "札幌"}
TURF_MAX_POPULARITY = 3  # 1〜3人気のみ通知
# ------------------------------------------


@dataclass
class AlertDecision:
    entry: MiraiEntry
    should_notify: bool
    pillar: str = ""  # "柱① ダート" or "柱② 芝"
    rank: str = ""  # "S" or "A"
    headline_reason: str = ""
    boosts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    bet_hint: str = ""
    is_morning_watch: bool = False  # 朝時点のみ：オッズ次第で1〜3人気浮上の可能性あり


def evaluate_entry(entry: MiraiEntry, is_morning: bool = False) -> AlertDecision:
    if entry.surface == "ダ":
        return _evaluate_dirt(entry, is_morning=is_morning)
    if entry.surface == "芝":
        return _evaluate_turf(entry, is_morning=is_morning)
    return AlertDecision(entry=entry, should_notify=False)


def _evaluate_dirt(entry: MiraiEntry, is_morning: bool = False) -> AlertDecision:
    h = entry.history
    pop = entry.popularity
    odds = entry.odds

    # 1. Track condition filter (良馬場のみ)
    if DIRT_REQUIRE_GOOD_TRACK and entry.baba and entry.baba != "良":
        return AlertDecision(entry=entry, should_notify=False)

    # 2. Class filter (1勝クラス・3勝クラス除外)
    if entry.race_class in DIRT_EXCLUDED_CLASSES:
        return AlertDecision(entry=entry, should_notify=False)

    # 3. Switch OR Top-3 popularity
    is_top3 = pop is not None and pop <= 3
    is_switch = h.is_switch

    if not is_switch:
        if not (DIRT_INCLUDE_TOP3_EVEN_IF_CONTINUOUS and is_top3):
            # In morning mode, if odds are not yet published for a continuous ride, mark as watch
            if is_morning and pop is None and DIRT_INCLUDE_TOP3_EVEN_IF_CONTINUOUS:
                return AlertDecision(
                    entry=entry,
                    should_notify=False,
                    pillar="柱① ダート",
                    is_morning_watch=True,
                    headline_reason="継続騎乗（1時間前に1〜3人気なら通知）",
                )
            return AlertDecision(entry=entry, should_notify=False)

    # Qualified for Pillar ① (Dirt)! Collect boosts & warnings.
    boosts: List[str] = []
    warnings: List[str] = []
    core_hole_boost_count = 0

    if is_switch:
        prev_j = f"（前:{h.prev_jockey}）" if h.prev_jockey else ""
        if h.switch_type == "手綱戻り":
            boosts.append(f"乗り替わり・手綱戻り{prev_j}（5人気以下ダート複勝36.7%）")
        else:
            boosts.append(f"乗り替わり・テン乗り{prev_j}（5人気以下ダート単回336%）")

    kanto_hit = False
    if entry.stable_region == "美浦":
        boosts.append(f"美浦（関東馬：{entry.trainer} / 穴ダート単回575%）")
        kanto_hit = True

    if entry.venue in KANTO_VENUES:
        boosts.append(f"関東開催（{entry.venue}ダート / 穴勝率25.0%）")
        kanto_hit = True

    if kanto_hit:
        core_hole_boost_count += 1

    if h.same_venue_win:
        boosts.append(f"同競馬場（{entry.venue}）勝利歴あり")
        core_hole_boost_count += 1
    elif h.same_venue_top3:
        boosts.append(f"同競馬場（{entry.venue}）馬券内実績あり")
        core_hole_boost_count += 1

    if h.prev_pos4 is not None and h.prev_pos4 <= 4:
        boosts.append(f"前走4角4番手以内（前走通過:{h.prev_tsuka} / 穴複勝36.8%）")
        core_hole_boost_count += 1

    if odds is not None and odds < 5.0:
        boosts.append("単勝4.9倍以下×良×1・3勝除外（勝率42.0%・複勝69.6%・単回124%）")
    elif pop == 3:
        boosts.append("3人気ダート（勝率25.6%・連対46.5%・単回133%）")

    if entry.race_class in ("未勝利", "2勝クラス", "OP/L"):
        cls_note = {
            "未勝利": "1〜3人気 勝率37.3%・複勝64.0%",
            "2勝クラス": "上位連対53%＆穴単回893%の超得意クラス",
            "OP/L": "穴ダート勝率27.3%・単回407%",
        }[entry.race_class]
        boosts.append(f"{entry.race_class}（{cls_note}）")
        if entry.race_class in ("2勝クラス", "OP/L"):
            core_hole_boost_count += 1

    if entry.venue in LEFT_VENUES:
        boosts.append(f"左回りダート（{entry.venue} / 1〜3人気連対54.3%）")

    if entry.field_size and entry.field_size <= 12:
        boosts.append(f"少頭数{entry.field_size}頭（1〜3人気連対54.2%）")

    if entry.race_num >= 9:
        boosts.append(f"後半{entry.race_num}R（穴ダート勝率18.2%・単回459%）")
        core_hole_boost_count += 1

    if h.is_tataki2:
        boosts.append("休み明け叩き2戦目（穴ダート勝率18.2%・単回657%）")

    if "伊藤圭" in entry.trainer:
        boosts.append("特注タッグ：美浦・伊藤圭三厩舎（穴ダート複勝66.7%）")

    # Warnings
    if not entry.baba:
        warnings.append("馬場状態未確定（良馬場か直前に要確認）")
    if h.prev_surface == "芝":
        warnings.append("芝→ダート替わり（ダート継続時よりやや割引）")
    if h.switch_type == "継続騎乗" and is_top3:
        warnings.append("継続騎乗（上位人気のため通知・穴狙いではない）")

    # Determine Rank (S vs A) & Bet Hint
    is_iron_s = odds is not None and odds < 5.0
    is_hole_s = (pop is not None and pop >= 5) and is_switch and core_hole_boost_count >= 2

    if is_iron_s:
        rank = "S"
        headline = "ダート良 × 単勝4.9倍以下（鉄板ゾーン）"
        bet_hint = "単勝・馬連・3連系 すべて軸◎"
    elif is_hole_s:
        rank = "S"
        headline = f"ダート良 × 乗り替わり × 大穴ブースト{core_hole_boost_count}個一致！"
        bet_hint = "単勝アタマ＆馬連・ワイド一撃勝負🔥"
    else:
        rank = "A"
        if is_top3:
            headline = "ダート良 × 上位人気（1〜3人気）"
            bet_hint = "連対約5割の堅実軸◎"
        else:
            headline = "ダート良 × 乗り替わり"
            bet_hint = "単勝・馬連の穴候補（ピンかパー傾向）"

    return AlertDecision(
        entry=entry,
        should_notify=True,
        pillar="柱① ダート",
        rank=rank,
        headline_reason=headline,
        boosts=boosts,
        warnings=warnings,
        bet_hint=bet_hint,
    )


def _evaluate_turf(entry: MiraiEntry, is_morning: bool = False) -> AlertDecision:
    pop = entry.popularity

    # 1. Exclude Tokyo & Sapporo completely
    if entry.venue in TURF_EXCLUDED_VENUES:
        return AlertDecision(entry=entry, should_notify=False)

    # 2. Only 1〜3 popularity
    if pop is None or pop > TURF_MAX_POPULARITY:
        if is_morning and (pop is None or pop <= 5):
            return AlertDecision(
                entry=entry,
                should_notify=False,
                pillar="柱② 芝",
                is_morning_watch=True,
                headline_reason=(
                    f"朝時点{pop}人気（1時間前に1〜3人気へ上がれば通知）"
                    if pop
                    else "朝オッズ未確定（1時間前に1〜3人気なら通知）"
                ),
            )
        return AlertDecision(entry=entry, should_notify=False)

    boosts: List[str] = []
    warnings: List[str] = []

    if pop == 2:
        boosts.append("🔥2人気（東京・札幌以外芝：勝率38.9%・連対59.3%・複勝77.8%・単回149%）")

    if entry.baba in ("稍", "重", "不"):
        boosts.append(f"芝の道悪（{entry.baba} / 1〜4人気 連対55.0%・複勝67.5%・単回132%）")

    if entry.distance == 1400:
        boosts.append("最強距離：芝1400m（1〜3人気 勝率37.0%・複勝77.8%・単回167%）")
    elif entry.distance == 1600:
        boosts.append("芝1600m（1600m以下は1〜3人気 勝率32.2%・単回139%）")

    if entry.venue in ("阪神", "新潟", "中山"):
        venue_note = {
            "阪神": "1〜3人気 連対59.6%・複勝76.9%",
            "新潟": "1〜3人気 勝率50.0%・連対60.0%",
            "中山": "1〜3人気 連対57.1%・複勝71.4%",
        }[entry.venue]
        boosts.append(f"得意コース：{entry.venue}芝（{venue_note}）")

    if entry.race_class in ("重賞", "OP/L", "未勝利", "2勝クラス"):
        cls_note = {
            "重賞": "1〜3人気 勝率41.7%・複勝75.0%・単回190%",
            "OP/L": "1〜3人気 複勝率100%（重賞+OP計82.4%）",
            "未勝利": "1〜3人気 複勝67.7%・単回143%",
            "2勝クラス": "1〜3人気 勝率36.8%・連対52.6%",
        }[entry.race_class]
        boosts.append(f"{entry.race_class}（{cls_note}）")

    if entry.race_num == 12:
        boosts.append("最終12R（東京以外芝 勝率26.9%・単回420%）")

    if entry.waku in (1, 2):
        boosts.append(f"内枠{entry.waku}枠（1〜3人気 複勝70.0%・ヒモ軸◎）")
    elif entry.waku in (7, 8):
        boosts.append(f"外枠{entry.waku}枠（1〜3人気 勝率37.8%・単回148%・頭◎）")

    # Warnings
    if entry.race_class == "1勝クラス":
        warnings.append("1勝クラス（芝も他クラスよりやや不振：複勝51.1%）")
    if pop == 1:
        warnings.append("1人気（複勝65%と堅実だが単回63.7%・単勝過信注意）")

    # Rank: 2人気 is ALWAYS S-rank as requested!
    if pop == 2:
        rank = "S"
        headline = f"東京・札幌以外芝 × 2番人気（Sランク特注！）"
        bet_hint = "単勝・馬連・3連系すべて最強軸🔥"
    else:
        rank = "A"
        headline = f"東京・札幌以外芝 × {pop}番人気"
        bet_hint = "連対約49%・複勝65%の堅実軸◎"

    return AlertDecision(
        entry=entry,
        should_notify=True,
        pillar="柱② 芝",
        rank=rank,
        headline_reason=headline,
        boosts=boosts,
        warnings=warnings,
        bet_hint=bet_hint,
    )
