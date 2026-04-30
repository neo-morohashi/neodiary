#!/usr/bin/env python3
"""
昨日のトレーニングアクティビティを日記に書き込むスクリプト

データソース（優先順）:
  1. garmin_activities — Garmin Connect（詳細データ: 負荷・HR zones・AE/AnE）
  2. activities        — Strava（Garminにない場合のみ補完）

引数:
  --date YYYY-MM-DD   対象日を指定（省略時は前日）
"""

import sys
import sqlite3
from pathlib import Path
from diary_utils import DIARY_DIR, ensure_diary
from datetime import date, timedelta, datetime

TRIATHLON_DB = Path.home() / 'AI Dev/Triathlon/data/triathlon.db'
SECTION_HEADER = '## 🏃 トレーニング記録'

SPORT_EMOJI = {
    'Run':           '🏃',
    'Swim':          '🏊',
    'Ride':          '🚴',
    'VirtualRide':   '🚴',
    'WeightTraining':'💪',
    'Walk':          '🚶',
    'Hike':          '🥾',
    'Multi Sport':   '🏅',
    'Mountaineering':'🧗',
}

SPORT_JA = {
    'Run':           'ラン',
    'Swim':          'スイム',
    'Ride':          'バイク',
    'VirtualRide':   'バーチャルバイク',
    'WeightTraining':'筋トレ',
    'Walk':          'ウォーク',
    'Hike':          'ハイク',
    'Multi Sport':   'マルチスポーツ',
    'Mountaineering':'登山',
}


# ── フォーマットユーティリティ ────────────────────────────────────────────────

def fmt_time(seconds: float) -> str:
    """秒 → h:mm:ss or mm:ss"""
    if not seconds:
        return '—'
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f'{h}:{m:02d}:{sec:02d}' if h else f'{m}:{sec:02d}'


def fmt_pace_run(moving_time_sec: float, distance_km: float) -> str:
    """ランペース min:sec/km"""
    if not moving_time_sec or not distance_km or distance_km < 0.1:
        return '—'
    pace_sec = moving_time_sec / distance_km
    m, s = divmod(int(pace_sec), 60)
    return f'{m}:{s:02d}/km'


def fmt_pace_swim(moving_time_sec: float, distance_km: float) -> str:
    """スイムペース min:sec/100m"""
    if not moving_time_sec or not distance_km or distance_km < 0.01:
        return '—'
    pace_sec = moving_time_sec / (distance_km * 10)  # per 100m
    m, s = divmod(int(pace_sec), 60)
    return f'{m}:{s:02d}/100m'


def fmt_zones(z1, z2, z3, z4, z5) -> str:
    """HR zones を % 表示"""
    vals = [z1 or 0, z2 or 0, z3 or 0, z4 or 0, z5 or 0]
    total = sum(vals)
    if total < 60:  # 1分未満は無視
        return ''
    pcts = [round(v / total * 100) for v in vals]
    parts = []
    labels = ['Z1', 'Z2', 'Z3', 'Z4', 'Z5']
    for label, pct in zip(labels, pcts):
        if pct >= 5:
            parts.append(f'{label} {pct}%')
    return '  '.join(parts)


# ── データ取得 ───────────────────────────────────────────────────────────────

def get_garmin_activities(conn, date_str: str) -> list[dict]:
    rows = conn.execute("""
        SELECT
            garmin_id, name, sport_type, start_date,
            distance, moving_time, elapsed_time,
            average_heartrate, max_heartrate,
            average_cadence, average_watts, kilojoules, calories,
            aerobic_training_effect, anaerobic_training_effect,
            activity_training_load,
            hr_time_in_zone_1, hr_time_in_zone_2, hr_time_in_zone_3,
            hr_time_in_zone_4, hr_time_in_zone_5,
            total_elevation_gain
        FROM garmin_activities
        WHERE date(start_date) = ?
        ORDER BY start_date
    """, (date_str,)).fetchall()

    keys = ['id', 'name', 'sport_type', 'start_date',
            'distance', 'moving_time', 'elapsed_time',
            'avg_hr', 'max_hr', 'cadence', 'watts', 'kj', 'calories',
            'ae', 'ane', 'load',
            'z1', 'z2', 'z3', 'z4', 'z5', 'elevation']
    return [dict(zip(keys, r)) for r in rows]


def get_strava_activities(conn, date_str: str) -> list[dict]:
    rows = conn.execute("""
        SELECT
            id, name, type, start_date,
            distance, moving_time,
            average_heartrate, max_heartrate,
            suffer_score, calories, total_elevation_gain
        FROM activities
        WHERE date(start_date) = ?
        ORDER BY start_date
    """, (date_str,)).fetchall()

    keys = ['id', 'name', 'sport_type', 'start_date',
            'distance', 'moving_time',
            'avg_hr', 'max_hr', 'suffer_score', 'calories', 'elevation']
    acts = []
    for r in rows:
        d = dict(zip(keys, r))
        # VirtualRide → Ride に統一
        if d['sport_type'] == 'VirtualRide':
            d['sport_type'] = 'Ride'
        acts.append(d)
    return acts


def deduplicate(garmin_acts: list[dict], strava_acts: list[dict]) -> list[dict]:
    """Strava から Garmin と重複しているものを除去（±5分以内の同種アクティビティ）"""
    def parse_dt(s):
        for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(s[:19], fmt)
            except ValueError:
                continue
        return None

    garmin_times = [(parse_dt(a['start_date']), a['sport_type']) for a in garmin_acts]

    result = list(garmin_acts)
    for sa in strava_acts:
        st = parse_dt(sa['start_date'])
        if st is None:
            continue
        matched = any(
            gt is not None
            and abs((st - gt).total_seconds()) <= 300
            for gt, _ in garmin_times
        )
        if not matched:
            result.append(sa)

    # start_date でソート
    result.sort(key=lambda a: a.get('start_date', ''))
    return result


# ── アクティビティ → Markdown ────────────────────────────────────────────────

def activity_to_md(a: dict) -> str:
    sport  = a.get('sport_type', 'Other')
    emoji  = SPORT_EMOJI.get(sport, '🏅')
    ja     = SPORT_JA.get(sport, sport)
    name   = a.get('name') or ja
    dist   = (a.get('distance') or 0) / 1000   # km
    time_s = a.get('moving_time') or 0
    avg_hr = a.get('avg_hr')
    max_hr = a.get('max_hr')
    cals   = a.get('calories')
    elev   = a.get('elevation')
    load   = a.get('load')
    ae     = a.get('ae')
    ane    = a.get('ane')
    watts  = a.get('watts')
    cadence= a.get('cadence')
    suffer = a.get('suffer_score')

    lines = [f'### {emoji} {ja}  *{name}*', '']

    # ── 距離・時間・ペース ──
    row1 = []
    if dist > 0.05:
        row1.append(f'距離 **{dist:.2f} km**')
    row1.append(f'時間 **{fmt_time(time_s)}**')
    if sport == 'Run' and dist > 0.1:
        row1.append(f'ペース **{fmt_pace_run(time_s, dist)}**')
    elif sport == 'Swim' and dist > 0.01:
        row1.append(f'ペース **{fmt_pace_swim(time_s, dist)}**')
    elif sport in ('Ride', 'VirtualRide') and dist > 0.1 and time_s:
        spd = dist / (time_s / 3600)
        row1.append(f'速度 **{spd:.1f} km/h**')
    if elev and elev > 5:
        row1.append(f'獲得標高 {elev:.0f} m')
    lines.append('  '.join(row1))

    # ── 心拍 ──
    row2 = []
    if avg_hr:
        row2.append(f'心拍 avg **{avg_hr:.0f}** / max **{max_hr:.0f}**')
    if cals and cals > 0:
        row2.append(f'消費 {cals:.0f} kcal')
    if watts and watts > 0:
        row2.append(f'出力 {watts:.0f} W')
    if cadence and cadence > 0:
        unit = 'rpm' if sport in ('Ride', 'VirtualRide') else 'spm'
        row2.append(f'ケイデンス {cadence:.0f} {unit}')
    if row2:
        lines.append('  '.join(row2))

    # ── トレーニング効果・負荷 ──
    row3 = []
    if load and load > 0:
        row3.append(f'負荷 **{load:.0f}**')
    if ae and ae > 0:
        row3.append(f'有酸素効果 {ae:.1f}')
    if ane and ane > 0:
        row3.append(f'無酸素効果 {ane:.1f}')
    if suffer and suffer > 0:
        row3.append(f'Relative Effort {suffer:.0f}')
    if row3:
        lines.append('  '.join(row3))

    # ── HR zones ──
    zones_str = fmt_zones(a.get('z1'), a.get('z2'), a.get('z3'), a.get('z4'), a.get('z5'))
    if zones_str:
        lines.append(f'HR zones: {zones_str}')

    lines.append('')
    return '\n'.join(lines)


# ── Markdown ブロック生成 ────────────────────────────────────────────────────

def build_training_block(date_str: str, activities: list[dict]) -> str:
    lines = [SECTION_HEADER, '']

    if not activities:
        lines += ['*休養日*', '']
        return '\n'.join(lines)

    # サマリー行（複数種目の合計）
    total_time = sum(a.get('moving_time') or 0 for a in activities)
    total_cals = sum(a.get('calories') or 0 for a in activities)
    sports = [SPORT_JA.get(a.get('sport_type', ''), a.get('sport_type', '')) for a in activities]
    summary_parts = [f'{len(activities)} セッション（{"・".join(sports)}）']
    if total_time:
        summary_parts.append(f'合計 {fmt_time(total_time)}')
    if total_cals:
        summary_parts.append(f'{total_cals:.0f} kcal')
    lines += ['**' + '  '.join(summary_parts) + '**', '']

    for a in activities:
        lines.append(activity_to_md(a))

    return '\n'.join(lines)


# ── 日記への書き込み ─────────────────────────────────────────────────────────

def update_diary(date_str: str, block: str) -> None:
    diary_path = DIARY_DIR / f'{date_str}.md'
    ensure_diary(date_str)

    content = diary_path.read_text(encoding='utf-8')
    if SECTION_HEADER in content:
        start    = content.find(SECTION_HEADER)
        next_sec = content.find('\n## ', start + len(SECTION_HEADER))
        if next_sec < 0:
            content = content[:start].rstrip() + '\n\n' + block + '\n'
        else:
            content = content[:start] + block + content[next_sec:]
    else:
        content = content.rstrip() + '\n\n' + block + '\n'

    diary_path.write_text(content, encoding='utf-8')
    print(f'  → {diary_path} にトレーニング記録を書き込みました')


# ── メイン ───────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if '--date' in args:
        target = args[args.index('--date') + 1]
    else:
        target = (date.today() - timedelta(days=1)).isoformat()

    print(f'トレーニング記録を書き込み中: {target}')

    conn = sqlite3.connect(TRIATHLON_DB)
    garmin = get_garmin_activities(conn, target)
    strava = get_strava_activities(conn, target)
    conn.close()

    activities = deduplicate(garmin, strava)

    if activities:
        sports = [a.get('sport_type', '?') for a in activities]
        print(f'  {len(activities)} アクティビティ: {sports}')
    else:
        print('  アクティビティなし（休養日）')

    block = build_training_block(target, activities)
    print(block)
    update_diary(target, block)
    print('完了。')


if __name__ == '__main__':
    main()
