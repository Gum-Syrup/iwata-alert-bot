"""Discord Webhook notifier with @everyone mention and rich embeds."""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from rules import AlertDecision
from scraper import RivalHorse

VENUE_SHORT = {
    "札幌": "札",
    "函館": "函",
    "福島": "福",
    "新潟": "新",
    "東京": "東",
    "中山": "中",
    "中京": "名",
    "京都": "京",
    "阪神": "阪",
    "小倉": "小",
}

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
            "｜各レース発走1時間前に最新オッズ＋Gemini貼るだけプロンプトを送ります！"
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


def build_gemini_research_prompt(
    decision: AlertDecision,
    runners: List[RivalHorse],
    max_chars: int = 1920,
) -> str:
    """Build a 1-tap copy-paste prompt for Gemini (Neon Extra) that ALWAYS fits in 1 Discord message."""
    e = decision.entry
    date_fmt = f"{e.date[:4]}/{e.date[4:6]}/{e.date[6:8]}"
    surf_name = "ダート" if e.surface == "ダ" else "芝"
    baba_str = e.baba or "未発表"
    odds_str = f"{e.popularity}人気/{e.odds:.1f}倍" if (e.popularity and e.odds) else "未確定"
    rank_label = "Sランク" if decision.rank == "S" else "Aランク"
    short_boosts = " / ".join(b.split("（")[0] for b in decision.boosts[:5])

    header = (
        f"以下のJRAレースで岩田望来騎乗【{e.umaban}番 {e.horse_name}（{odds_str}）】を軸に馬券を組みます。\n"
        "Web検索で当日の馬場傾向・調教・直前情報も補完しつつ、展開予想と望来の相手馬"
        "（○対抗・▲単穴・△連下2〜3頭・☆穴1頭）を根拠つきで厳選し、馬連・ワイド・3連複の買い目案を簡潔に出してください。\n\n"
        f"■レース：{date_fmt} {e.venue}{e.race_num}R {e.race_name} {surf_name}{e.distance}m({baba_str}) {e.race_class} {e.field_size}頭 {e.post_time}発走\n"
        f"■望来判定：{rank_label}（{decision.pillar}｜{short_boosts}）\n"
        f"■出馬表：{e.shutuba_url}\n"
        "■全出走馬（馬番 馬名 性齢/騎手/斤量/人気・単勝/脚質｜近走）\n"
    )

    def _format_runners(max_runs: int, include_agari: bool) -> str:
        lines: List[str] = []
        for r in runners:
            mark = "★" if r.umaban == e.umaban else ""
            op_s = f"{r.popularity}人{r.odds:.1f}" if (r.popularity and r.odds) else "-"
            st_s = f"/{r.style}" if r.style else ""
            wt_s = r.kinryo.rstrip("0").rstrip(".") if r.kinryo else ""
            meta = f"{r.sex_age}/{r.jockey}/{wt_s}/{op_s}{st_s}"
            runs_s = []
            for pr in r.past_runs[:max_runs]:
                v = VENUE_SHORT.get(pr.venue, pr.venue[:1])
                d = f"{pr.surface}{pr.distance // 100}" if pr.distance else ""
                rk = f"{pr.rank}着" if pr.rank.isdigit() else pr.rank
                mg = f"({pr.margin})" if pr.margin else ""
                extras = []
                if pr.tsuka:
                    extras.append(pr.tsuka)
                if include_agari and pr.agari:
                    extras.append(f"上{pr.agari}")
                ex_s = f"[{'/'.join(extras)}]" if extras else ""
                runs_s.append(f"{v}{d}{pr.baba}{rk}{mg}{ex_s}")
            past_txt = " ".join(runs_s) if runs_s else "初出走"
            lines.append(f"{mark}{r.umaban} {r.horse_name}({meta}) {past_txt}")
        return header + "\n".join(lines)

    # Progressively tighten detail if field is large so it NEVER exceeds Discord's 2000-char limit
    for max_runs, inc_agari in ((3, True), (3, False), (2, True), (2, False), (1, False)):
        candidate = _format_runners(max_runs, inc_agari)
        if len(candidate) <= max_chars:
            return candidate

    return _format_runners(1, False)[:max_chars]


def send_discord_text(
    webhook_url: str,
    content: str,
    dry_run: bool = False,
) -> None:
    """Send a plain-text / code-block message (no @everyone) to Discord."""
    if dry_run:
        print("-" * 72)
        print(f"[DRY-RUN COPY-PASTE PROMPT] (len={len(content)})")
        print(content)
        print("-" * 72)
        return

    if not webhook_url:
        raise ValueError("DISCORD_WEBHOOK_URL is not set.")

    payload = {
        "content": content[:2000],
        "allowed_mentions": {"parse": []},
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
