"""Discord Webhook notifier with @everyone mention and rich embeds."""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from rules import AlertDecision

COLOR_RANK_S = 0xE74C3C  # Crimson / Hot S-rank
COLOR_RANK_A = 0x2ECC71  # Emerald / Solid A-rank
COLOR_SUMMARY = 0x3498DB  # Blue / Morning overview
COLOR_NONE = 0x95A5A6  # Gray / Quiet note


def _fmt_odds(decision: AlertDecision) -> str:
    e = decision.entry
    if e.popularity is not None and e.odds is not None:
        return f"**{e.popularity}人気 / {e.odds:.1f}倍**"
    return "オッズ未確定"


def _fmt_prev_summary(decision: AlertDecision) -> str:
    h = decision.entry.history
    if h.career_starts == 0:
        return "新馬戦（初出走）"
    rk = f"{h.prev_rank}着" if h.prev_rank is not None else "着外"
    pp = f"({h.prev_pop}人気)" if h.prev_pop is not None else ""
    dist = f"{h.prev_surface}{h.prev_distance}m" if h.prev_distance else ""
    tsuka = f" 通過:{h.prev_tsuka}" if h.prev_tsuka else ""
    return f"{h.switch_type}（前走:{h.prev_jockey} / {rk}{pp} {dist}{tsuka}）"


def build_prerace_embed(decision: AlertDecision, minutes_left: Optional[int] = None) -> Dict[str, Any]:
    e = decision.entry
    rank_icon = "🔥【Sランク】" if decision.rank == "S" else "✅【Aランク】"
    surf_name = "ダート" if e.surface == "ダ" else "芝"
    baba_str = e.baba or "未発表"
    left_str = f"（あと約{minutes_left}分）" if minutes_left is not None else ""

    title = f"{rank_icon}{e.venue}{e.race_num}R {e.race_name}｜{e.umaban}番 {e.horse_name}"
    desc = (
        f"⏰ **発走 {e.post_time}**{left_str}\n"
        f"🏇 **{surf_name}{e.distance}m（{baba_str}）** / {e.race_class} / {e.field_size}頭立て / {e.waku}枠{e.umaban}番\n"
        f"📊 現在オッズ：{_fmt_odds(decision)}（{e.trainer}）\n"
        f"📌 判定：**{decision.pillar}｜{decision.headline_reason}**"
    )

    fields: List[Dict[str, Any]] = [
        {"name": "🐎 前走・騎乗", "value": _fmt_prev_summary(decision), "inline": False},
    ]

    if decision.boosts:
        boost_lines = "\n".join(f"・{b}" for b in decision.boosts)
        fields.append(
            {
                "name": f"🟢 プラスフィルター（{len(decision.boosts)}個）",
                "value": boost_lines[:1024],
                "inline": False,
            }
        )

    if decision.warnings:
        warn_lines = "\n".join(f"・{w}" for w in decision.warnings)
        fields.append(
            {
                "name": "⚠️ 注意ポイント",
                "value": warn_lines[:1024],
                "inline": False,
            }
        )

    if decision.bet_hint:
        fields.append({"name": "💡 買い方目安", "value": decision.bet_hint, "inline": False})

    footer_text = f"オッズ更新: {e.odds_time}" if e.odds_time else "岩田望来アラートBot"
    return {
        "title": title,
        "url": e.shutuba_url,
        "description": desc,
        "color": COLOR_RANK_S if decision.rank == "S" else COLOR_RANK_A,
        "fields": fields,
        "footer": {"text": footer_text},
    }


def build_morning_embeds(
    date_yyyymmdd: str,
    all_decisions: List[AlertDecision],
) -> Tuple[str, List[Dict[str, Any]]]:
    date_fmt = f"{date_yyyymmdd[:4]}/{date_yyyymmdd[4:6]}/{date_yyyymmdd[6:8]}"
    targets = [d for d in all_decisions if d.should_notify]
    watches = [d for d in all_decisions if d.is_morning_watch]

    if targets:
        content = (
            f"@everyone ☀️ **{date_fmt} 岩田望来 朝イチ狙い目まとめ**\n"
            f"本日の条件合致：**{len(targets)}レース**（全{len(all_decisions)}鞍中）"
            "｜各レース発走1時間前にも最新オッズで再通知します！"
        )
    else:
        content = (
            f"@everyone ☀️ **{date_fmt} 岩田望来 朝イチまとめ**\n"
            f"朝時点での条件合致は0件（全{len(all_decisions)}鞍中）。"
            "オッズ変動で条件入りした場合は発走1時間前に通知します。"
        )

    embeds: List[Dict[str, Any]] = []

    # Compact overview embed
    overview_lines: List[str] = []
    for d in all_decisions:
        e = d.entry
        if d.should_notify:
            badge = "🔥S" if d.rank == "S" else "✅A"
        elif d.is_morning_watch:
            badge = "👀監視"
        else:
            badge = "⬜対象外"
        pop_str = f"{e.popularity}人気/{e.odds:.1f}倍" if (e.popularity and e.odds) else "未確定"
        baba_str = e.baba or "?"
        overview_lines.append(
            f"`{e.post_time}` {badge} **{e.venue}{e.race_num}R** "
            f"{e.surface}{e.distance}({baba_str}) {e.race_class}｜{e.umaban}番 {e.horse_name}（{pop_str}）"
        )

    embeds.append(
        {
            "title": f"📋 {date_fmt} 岩田望来 全{len(all_decisions)}鞍スケジュール",
            "description": "\n".join(overview_lines)[:4000] if overview_lines else "騎乗なし",
            "color": COLOR_SUMMARY if targets else COLOR_NONE,
        }
    )

    # Detailed embeds for target races (up to 9 so total <= 10 embeds)
    for d in targets[:9]:
        embeds.append(build_prerace_embed(d))

    # Optional watch note if there are morning watch candidates
    if watches and len(embeds) < 10:
        w_lines = []
        for d in watches:
            e = d.entry
            w_lines.append(
                f"・`{e.post_time}` **{e.venue}{e.race_num}R** {e.surface}{e.distance} "
                f"{e.horse_name} — {d.headline_reason}"
            )
        embeds.append(
            {
                "title": "👀 発走1時間前のオッズ次第で通知対象になる馬",
                "description": "\n".join(w_lines)[:4000],
                "color": COLOR_NONE,
            }
        )

    return content, embeds


def send_discord_webhook(
    webhook_url: str,
    content: str,
    embeds: List[Dict[str, Any]],
    dry_run: bool = False,
) -> None:
    if dry_run:
        print("=" * 72)
        print("[DRY-RUN DISCORD MESSAGE]")
        print("CONTENT:", content)
        for idx, emb in enumerate(embeds, 1):
            print(f"\n--- Embed {idx}: {emb.get('title')} ---")
            if emb.get("url"):
                print("URL:", emb["url"])
            print(emb.get("description", ""))
            for fld in emb.get("fields", []):
                print(f"[{fld['name']}]\n{fld['value']}")
        print("=" * 72)
        return

    if not webhook_url:
        raise ValueError("DISCORD_WEBHOOK_URL is not set.")

    # Discord allows max 10 embeds per webhook request
    chunk_size = 10
    for i in range(0, max(1, len(embeds)), chunk_size):
        chunk = embeds[i : i + chunk_size]
        payload = {
            "content": content if i == 0 else "",
            "embeds": chunk,
            "allowed_mentions": {"parse": ["everyone"]},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "iwata-alert-bot/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Discord webhook returned HTTP {resp.status}")
