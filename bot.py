"""Main runner for the Mirai Iwata (岩田望来) JRA Discord Alert Bot."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from notifier import (
    build_clean_prompt_txt,
    build_morning_embeds,
    build_prerace_embed,
    send_discord_webhook,
)
from rules import AlertDecision, evaluate_entry
from scraper import (
    MiraiEntry,
    fetch_day_races,
    fetch_horse_history,
    fetch_live_odds,
    fetch_mirai_entry_from_shutuba,
)

JST = timezone(timedelta(hours=9))


def _now_jst() -> datetime:
    return datetime.now(JST)


def _minutes_until_post(now_jst: datetime, date_yyyymmdd: str, post_time: str) -> Optional[int]:
    if not post_time or ":" not in post_time:
        return None
    try:
        hh, mm = post_time.split(":")
        post_dt = datetime(
            int(date_yyyymmdd[:4]),
            int(date_yyyymmdd[4:6]),
            int(date_yyyymmdd[6:8]),
            int(hh),
            int(mm),
            tzinfo=JST,
        )
        return int(round((post_dt - now_jst).total_seconds() / 60.0))
    except ValueError:
        return None


def _load_sent_ids(state_path: Path) -> Set[str]:
    if not state_path.exists():
        return set()
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x) for x in data}
    except Exception:
        pass
    return set()


def _save_sent_ids(state_path: Path, sent_ids: Set[str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(sorted(sent_ids), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _enrich_entry(entry: MiraiEntry) -> Dict[int, Tuple[float, int]]:
    odds_map, odds_time = fetch_live_odds(entry.race_id)
    if entry.umaban in odds_map:
        entry.odds, entry.popularity = odds_map[entry.umaban]
        entry.odds_time = odds_time
    time.sleep(0.2)
    entry.history = fetch_horse_history(entry.horse_id, entry.venue, entry.date)
    time.sleep(0.2)
    return odds_map


def run_morning(date_yyyymmdd: str, webhook_url: str, dry_run: bool) -> int:
    print(f"[morning] Checking JRA schedule for {date_yyyymmdd}...")
    summaries = fetch_day_races(date_yyyymmdd)
    if not summaries:
        print(f"[morning] No JRA races scheduled on {date_yyyymmdd}. Exiting.")
        return 0

    print(f"[morning] Found {len(summaries)} JRA races. Scanning for Mirai Iwata (01174)...")
    mirai_entries: List[MiraiEntry] = []
    for s in summaries:
        entry = fetch_mirai_entry_from_shutuba(s, date_yyyymmdd)
        if entry is not None:
            print(f"  -> {entry.venue}{entry.race_num}R ({entry.post_time}) {entry.surface}{entry.distance} {entry.horse_name}")
            mirai_entries.append(entry)
        time.sleep(0.2)

    if not mirai_entries:
        print(f"[morning] Mirai Iwata has no rides on {date_yyyymmdd}. Exiting.")
        return 0

    decisions: List[AlertDecision] = []
    for entry in mirai_entries:
        _enrich_entry(entry)
        decisions.append(evaluate_entry(entry, is_morning=True))

    content, embeds = build_morning_embeds(date_yyyymmdd, decisions)
    send_discord_webhook(webhook_url, content, embeds, dry_run=dry_run)
    print(
        f"[morning] Done. Targets={sum(1 for d in decisions if d.should_notify)} / Total rides={len(decisions)}"
    )
    return 0


def run_pre_race(
    date_yyyymmdd: str,
    webhook_url: str,
    dry_run: bool,
    window_min: int,
    window_max: int,
    state_dir: Path,
    force_all: bool,
) -> int:
    now_jst = _now_jst()
    print(
        f"[pre_race] Checking {date_yyyymmdd} at {now_jst.strftime('%Y-%m-%d %H:%M JST')} "
        f"(window: {window_min}-{window_max} min before post, force_all={force_all})"
    )

    summaries = fetch_day_races(date_yyyymmdd)
    if not summaries:
        print(f"[pre_race] No JRA races scheduled on {date_yyyymmdd}. Exiting.")
        return 0

    state_path = state_dir / f"sent_{date_yyyymmdd}.json"
    sent_ids = set() if force_all else _load_sent_ids(state_path)

    candidates = []
    for s in summaries:
        if s.race_id in sent_ids:
            continue
        mins = _minutes_until_post(now_jst, date_yyyymmdd, s.post_time)
        if force_all:
            candidates.append((s, mins))
        elif mins is not None and window_min <= mins <= window_max:
            candidates.append((s, mins))

    if not candidates:
        print("[pre_race] No unsent races in the current 1-hour-before window. Exiting.")
        return 0

    print(f"[pre_race] Inspecting {len(candidates)} candidate races in window...")
    alerts_sent = 0
    for s, mins in candidates:
        entry = fetch_mirai_entry_from_shutuba(s, date_yyyymmdd)
        time.sleep(0.2)
        if entry is None:
            continue

        odds_map = _enrich_entry(entry)
        decision = evaluate_entry(entry, is_morning=False)
        print(
            f"  -> {entry.venue}{entry.race_num}R {entry.surface}{entry.distance}({entry.baba}) "
            f"{entry.horse_name} ({entry.popularity}人気/{entry.odds}倍) => notify={decision.should_notify} ({decision.rank})"
        )

        if decision.should_notify:
            rank_label = "🔥Sランク" if decision.rank == "S" else "✅Aランク"
            left_note = f"（発走まであと約{mins}分）" if (mins is not None and mins > 0) else ""
            content = (
                f"@everyone 🚨 **【出走1時間前アラート｜{rank_label}】** "
                f"**{entry.venue}{entry.race_num}R（{entry.post_time}発走）**{left_note}\n"
                f"岩田望来：{entry.umaban}番 **{entry.horse_name}**"
            )
            embed = build_prerace_embed(decision, minutes_left=mins if (mins is not None and mins > 0) else None)
            prompt_txt = build_clean_prompt_txt(decision)
            filename = f"prompt_{entry.date}_{entry.venue}{entry.race_num}R.txt"
            send_discord_webhook(
                webhook_url,
                content,
                [embed],
                dry_run=dry_run,
                attachment_filename=filename,
                attachment_text=prompt_txt,
            )

            sent_ids.add(s.race_id)
            alerts_sent += 1
            time.sleep(0.5)

    if alerts_sent > 0 and not dry_run:
        _save_sent_ids(state_path, sent_ids)

    print(f"[pre_race] Done. Alerts sent={alerts_sent}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirai Iwata (岩田望来) JRA Discord Alert Bot")
    parser.add_argument(
        "--mode",
        choices=("morning", "pre_race"),
        required=True,
        help="Notification mode: 'morning' (daily summary) or 'pre_race' (1 hour before post)",
    )
    parser.add_argument(
        "--date",
        default="",
        help="Target race date YYYYMMDD (defaults to today in JST)",
    )
    parser.add_argument(
        "--window-min",
        type=int,
        default=45,
        help="Minimum minutes before post time for pre_race alert (default: 45)",
    )
    parser.add_argument(
        "--window-max",
        type=int,
        default=75,
        help="Maximum minutes before post time for pre_race alert (default: 75)",
    )
    parser.add_argument(
        "--state-dir",
        default=".state",
        help="Directory to store sent race_id state for deduplication",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Discord messages to stdout without actually posting to webhook",
    )
    parser.add_argument(
        "--force-all",
        action="store_true",
        help="[pre_race testing] Ignore time window and evaluate all races on --date",
    )
    args = parser.parse_args()

    date_yyyymmdd = args.date.strip() or _now_jst().strftime("%Y%m%d")
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    if args.mode == "morning":
        return run_morning(date_yyyymmdd, webhook_url, dry_run=args.dry_run)
    return run_pre_race(
        date_yyyymmdd=date_yyyymmdd,
        webhook_url=webhook_url,
        dry_run=args.dry_run,
        window_min=args.window_min,
        window_max=args.window_max,
        state_dir=Path(args.state_dir),
        force_all=args.force_all,
    )


if __name__ == "__main__":
    raise SystemExit(main())
