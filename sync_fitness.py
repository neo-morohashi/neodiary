#!/usr/bin/env python3
"""
フィットネス状態を計算して日記に書き込むスクリプト

データソース:
  - Garmin  : ~/AI Dev/Triathlon/data/triathlon.db
              (CTL/ATL/TSB・Training Status・Readiness・HRV・アクティビティ負荷)
  - Oura/WHOOP: data/biometrics.db
              (Readiness・HRV・Recovery・Strain)

引数:
  --date YYYY-MM-DD   対象日を指定（省略時は前日）
"""

import sys
import sqlite3
from pathlib import Path
from diary_utils import DIARY_DIR, ensure_diary
from datetime import date, timedelta
from statistics import mean

TRIATHLON_DB   = Path.home() / 'AI Dev/Triathlon/data/triathlon.db'
BIOMETRICS_DB  = Path(__file__).parent / 'data/biometrics.db'
SECTION_HEADER = '## 🏋️ フィットネス状態'


# ── データ取得 ───────────────────────────────────────────────────────────────

def get_garmin_day(conn, date_str: str) -> dict:
    row = conn.execute("""
        SELECT acute_training_load, chronic_training_load,
               training_readiness_score, training_status,
               hrv_last_night, recovery_time
        FROM garmin_daily WHERE date = ?
    """, (date_str,)).fetchone()
    if not row:
        return {}
    keys = ['atl', 'ctl', 'readiness', 'status', 'hrv', 'recovery_time']
    return {k: v for k, v in zip(keys, row)}


def get_bio_day(conn, date_str: str) -> dict:
    row = conn.execute("""
        SELECT oura_readiness, oura_hrv,
               whoop_recovery, whoop_hrv, whoop_strain,
               oura_sleep_hours, whoop_sleep_hours
        FROM biometrics WHERE date = ?
    """, (date_str,)).fetchone()
    if not row:
        return {}
    keys = ['oura_readiness', 'oura_hrv', 'whoop_recovery', 'whoop_hrv', 'whoop_strain',
            'oura_sleep_hours', 'whoop_sleep_hours']
    return {k: v for k, v in zip(keys, row)}


def get_hrv_history(tri_conn, bio_conn, days: int = 30) -> list[float]:
    """過去N日のHRV値リスト（Oura優先、なければGarmin）"""
    today = date.today()
    values = []
    for i in range(1, days + 1):
        d = (today - timedelta(days=i)).isoformat()
        r = bio_conn.execute("SELECT oura_hrv FROM biometrics WHERE date=?", (d,)).fetchone()
        g = tri_conn.execute("SELECT hrv_last_night FROM garmin_daily WHERE date=?", (d,)).fetchone()
        hrv = (r[0] if r and r[0] else None) or (g[0] if g and g[0] else None)
        if hrv:
            values.append(float(hrv))
    return values


def get_weekly_loads(tri_conn, end_date_str: str, weeks: int = 4) -> list[dict]:
    """過去N週のアクティビティ負荷合計（直近週が index 0）"""
    end = date.fromisoformat(end_date_str)
    result = []
    for w in range(weeks):
        week_end   = end - timedelta(days=w * 7)
        week_start = week_end - timedelta(days=6)
        row = tri_conn.execute("""
            SELECT COALESCE(SUM(activity_training_load), 0),
                   COUNT(CASE WHEN activity_training_load IS NOT NULL THEN 1 END)
            FROM garmin_activities
            WHERE date(start_date) BETWEEN ? AND ?
        """, (week_start.isoformat(), week_end.isoformat())).fetchone()
        result.append({
            'total':    row[0] or 0,
            'sessions': row[1] or 0,
            'start':    week_start.isoformat(),
            'end':      week_end.isoformat(),
        })
    return result


# ── 指標計算 ─────────────────────────────────────────────────────────────────

def calc_acwr(atl, ctl):
    if not atl or not ctl or ctl == 0:
        return None
    return round(atl / ctl, 2)


def calc_tsb(atl, ctl):
    if atl is None or ctl is None:
        return None
    return round(ctl - atl, 1)


def calc_sleep_hours(oura_h, whoop_h):
    vals = [float(v) for v in [oura_h, whoop_h] if v is not None]
    return round(mean(vals), 1) if vals else None


def calc_recovery_composite(oura_r, whoop_r, garmin_r, sleep_hours=None):
    scores = [float(s) for s in [oura_r, whoop_r, garmin_r] if s is not None]
    if not scores:
        return None
    base = mean(scores)
    # 睡眠時間が6h未満なら最大-10点のペナルティ（0.5h不足につき2.5点）
    if sleep_hours is not None and sleep_hours < 6.0:
        penalty = min((6.0 - sleep_hours) * 5, 10)
        base = max(0, base - penalty)
    return round(base, 1)


def calc_hrv_trend(history: list[float]):
    if len(history) < 7:
        return None, None, None
    avg7  = round(mean(history[:7]),  1)
    avg30 = round(mean(history[:min(30, len(history))]), 1)
    delta = round(avg7 - avg30, 1)
    return avg7, avg30, delta


def readiness_recommendation(acwr, rc, tsb) -> tuple[str, str]:
    if acwr is None or rc is None:
        return '—', '—'

    if acwr > 1.5:
        return (
            '休養推奨',
            f'急性負荷が高すぎる（ACWR={acwr}）。'
            f'傷害リスクが急増している状態。アクティブリカバリーか完全休養を。'
        )
    if rc < 55:
        return (
            '休養推奨',
            f'回復スコアが低い（{rc:.0f}点）。体が疲弊しているサイン。'
            f'今日は休養または軽いストレッチのみ。'
        )
    if rc >= 75 and acwr <= 1.3:
        if acwr < 0.8:
            return (
                '積極的にトレーニング推奨',
                f'回復は良好（{rc:.0f}点）でフレッシュな状態。'
                f'負荷が低すぎる（ACWR={acwr}）ので今日はしっかり追い込んでOK。'
            )
        return (
            'ハードトレーニングOK',
            f'回復スコア{rc:.0f}点・負荷バランスも良好（ACWR={acwr}）。'
            f'高強度インターバルや長距離にも対応できる状態。'
        )
    if rc >= 60 and acwr <= 1.3:
        return (
            '中強度OK（Zone 2メイン）',
            f'回復は普通（{rc:.0f}点）。有酸素Zone 2が最適。'
            f'無理な高強度は避けた方が回復を早める。'
        )
    return (
        '軽め〜中強度',
        f'回復スコア{rc:.0f}点・ACWR={acwr}。様子を見ながら軽めに留める。'
    )


# ── ラベル ───────────────────────────────────────────────────────────────────

def acwr_label(v) -> tuple[str, str]:
    if v is None: return '—', '—'
    if v < 0.8:   return str(v), '過少負荷'
    if v < 1.3:   return str(v), 'スイートスポット ✓'
    if v < 1.5:   return str(v), '注意'
    return str(v), '危険（傷害リスク）'


def tsb_label(v) -> tuple[str, str]:
    if v is None: return '—', '—'
    if v > 25:    phase = 'フレッシュ'
    elif v > 5:   phase = '最適'
    elif v > -10: phase = 'やや疲労'
    elif v > -30: phase = '疲労蓄積'
    else:          phase = 'オーバーロード'
    return f'{v:+.0f}', phase


def hrv_trend_label(delta) -> tuple[str, str]:
    if delta is None: return '—', '—'
    if delta > 3:    return f'{delta:+.1f} ms', '上昇（適応中）'
    if delta > -3:   return f'{delta:+.1f} ms', '横ばい（安定）'
    return f'{delta:+.1f} ms', '低下（疲労累積に注意）'


GARMIN_STATUS_JA = {
    'MAINTAINING':  'メンテナンス',
    'RECOVERY':     'リカバリー中',
    'PRODUCTIVE':   '生産的（トレーニング効果あり）',
    'OVERREACHING': 'オーバーリーチング（要注意）',
    'DETRAINING':   'デトレーニング（負荷不足）',
    'UNPRODUCTIVE': '非生産的（負荷は高いが適応なし）',
}


# ── Markdownブロック生成 ─────────────────────────────────────────────────────

def build_fitness_block(date_str: str, garmin: dict, bio: dict,
                         hrv_history: list[float], weekly_loads: list[dict]) -> str:
    atl        = garmin.get('atl')
    ctl        = garmin.get('ctl')
    g_ready    = garmin.get('readiness')
    g_status   = garmin.get('status', '')
    g_rec_time = garmin.get('recovery_time')

    oura_r        = bio.get('oura_readiness')
    whoop_r       = bio.get('whoop_recovery')
    strain        = bio.get('whoop_strain')
    oura_sleep_h  = bio.get('oura_sleep_hours')
    whoop_sleep_h = bio.get('whoop_sleep_hours')

    acwr      = calc_acwr(atl, ctl)
    tsb       = calc_tsb(atl, ctl)
    sleep_h   = calc_sleep_hours(oura_sleep_h, whoop_sleep_h)
    rc        = calc_recovery_composite(oura_r, whoop_r, g_ready, sleep_h)
    hrv7, hrv30, hrv_delta = calc_hrv_trend(hrv_history)
    rec_title, rec_detail  = readiness_recommendation(acwr, rc, tsb)

    lines = [SECTION_HEADER, '']

    # ── 今日の推奨バナー ──────────────────────────────────────────────────
    if rec_title and rec_title != '—':
        lines += [f'**今日の推奨: {rec_title}**', f'> {rec_detail}', '']

    # ── メイン指標テーブル ────────────────────────────────────────────────
    lines += [
        '| 指標 | 値 | 評価 |',
        '|------|----|------|',
    ]

    av, al = acwr_label(acwr)
    lines.append(f'| ACWR（急性/慢性負荷比） | {av} | {al} |')

    fv, fl = tsb_label(tsb)
    lines.append(f'| Form（TSB） | {fv} | {fl} |')

    if rc is not None:
        src_parts = []
        if oura_r  is not None: src_parts.append(f'Oura={oura_r:.0f}')
        if whoop_r is not None: src_parts.append(f'WHOOP={whoop_r:.0f}')
        if g_ready is not None: src_parts.append(f'Garmin={g_ready:.0f}')
        if sleep_h is not None:
            sleep_note = f'睡眠{sleep_h}h'
            if sleep_h < 6.0:
                penalty = min((6.0 - sleep_h) * 5, 10)
                sleep_note += f'(-{penalty:.1f}pt)'
            src_parts.append(sleep_note)
        lines.append(f'| 回復スコア（合成） | {rc:.0f} / 100 | {" · ".join(src_parts)} |')

    hv, hl = hrv_trend_label(hrv_delta)
    if hrv7:
        lines.append(f'| HRV トレンド | {hv} | {hl}（7d={hrv7} / 30d={hrv30}） |')

    if g_status:
        base = g_status.split('_')[0]
        status_ja = GARMIN_STATUS_JA.get(base, g_status)
        lines.append(f'| Garmin ステータス | — | {status_ja} |')

    if g_rec_time:
        lines.append(f'| リカバリー残時間 | {g_rec_time:.0f} h | — |')

    if strain is not None:
        lines.append(f'| WHOOP Strain | {strain:.1f} | （最大21） |')

    lines.append('')

    # ── 週次トレーニング量 ────────────────────────────────────────────────
    if weekly_loads and any(w['sessions'] > 0 for w in weekly_loads):
        lines += ['**週次トレーニング量**', '']
        lines += [
            '| 期間 | 活動負荷合計 | セッション数 |',
            '|------|:-----------:|:------------:|',
        ]
        prev_totals = [w['total'] for w in weekly_loads[1:] if w['total'] > 0]
        prev_avg = mean(prev_totals) if prev_totals else 0

        for i, w in enumerate(weekly_loads):
            label = '今週' if i == 0 else f'{i}週前'
            lines.append(f'| {label}（{w["start"]} 〜 {w["end"]}） | {w["total"]:.0f} | {w["sessions"]} 回 |')

        if prev_avg > 0:
            ratio = weekly_loads[0]['total'] / prev_avg
            if ratio > 1.4:
                note = f'今週の負荷が過去平均（{prev_avg:.0f}）の **{ratio:.1f}倍** に急増。ACWRの悪化に注意。'
            elif ratio < 0.6:
                note = f'今週の負荷が過去平均（{prev_avg:.0f}）の **{ratio:.1f}倍** に低下。一貫性の確認を。'
            else:
                note = f'過去平均（{prev_avg:.0f}）と比べ安定（×{ratio:.1f}）。'
            lines += ['', f'> {note}']

        lines.append('')

    # ── 指標の解説 ────────────────────────────────────────────────────────
    lines += [
        '**指標の見方**',
        '',
        '- **ACWR**（急性・慢性トレーニング負荷比）= ATL ÷ CTL。'
        '0.8〜1.3 がスイートスポット。1.5超で傷害リスクが急増する。',
        '- **Form（TSB）** = CTL（長期フィットネス）−ATL（短期疲労）。'
        'プラスが大きいほどフレッシュ。マイナスが深いほど疲労蓄積。',
        '- **回復スコア** = Oura Readiness・WHOOP Recovery・Garmin Training Readinessの平均。'
        '75以上でハード練習OK、55未満は休養推奨。',
        '- **HRV トレンド** = 直近7日平均 − 30日平均。'
        'プラスは体が適応中、マイナスは疲労蓄積のサイン。',
        '',
    ]

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
    print(f'  → {diary_path} にフィットネス状態を書き込みました')


# ── メイン ───────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if '--date' in args:
        target = args[args.index('--date') + 1]
    else:
        target = (date.today() - timedelta(days=1)).isoformat()

    print(f'フィットネス状態を計算中: {target}')

    tri_conn = sqlite3.connect(TRIATHLON_DB)
    bio_conn = sqlite3.connect(BIOMETRICS_DB)

    garmin  = get_garmin_day(tri_conn, target)
    bio     = get_bio_day(bio_conn, target)
    hrv_h   = get_hrv_history(tri_conn, bio_conn, days=30)
    weekly  = get_weekly_loads(tri_conn, target, weeks=4)

    tri_conn.close()
    bio_conn.close()

    if not garmin and not bio:
        print('  データなし。スキップ。')
        return

    block = build_fitness_block(target, garmin, bio, hrv_h, weekly)
    print(block)
    update_diary(target, block)
    print('完了。')


if __name__ == '__main__':
    main()
