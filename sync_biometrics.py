#!/usr/bin/env python3
"""
Oura + WHOOP バイオメトリクス同期
前日のデータを取得し、SQLite に保存して diary に書き込む。

必要な .env 変数:
  OURA_TOKEN            # Oura Personal Access Token
  WHOOP_CLIENT_ID       # WHOOP OAuth2 Client ID
  WHOOP_CLIENT_SECRET   # WHOOP OAuth2 Client Secret
  WHOOP_REFRESH_TOKEN   # WHOOP OAuth2 Refresh Token (whoop_auth.py で取得)
"""
import os
import re
import json
import sqlite3
import requests
from pathlib import Path
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / '.env')

OURA_TOKEN          = os.environ.get('OURA_TOKEN', '')
WHOOP_CLIENT_ID     = os.environ.get('WHOOP_CLIENT_ID', '')
WHOOP_CLIENT_SECRET = os.environ.get('WHOOP_CLIENT_SECRET', '')
WHOOP_REFRESH_TOKEN = os.environ.get('WHOOP_REFRESH_TOKEN', '')

DIARY_DIR = Path.home() / 'Documents/NeoBrain/diary'
DB_PATH   = Path(__file__).parent / 'data/biometrics.db'

OURA_BASE  = 'https://api.ouraring.com/v2/usercollection'
WHOOP_BASE = 'https://api.prod.whoop.com/developer/v1'
WHOOP_TOKEN_URL = 'https://api.prod.whoop.com/oauth/oauth2/token'


# ── SQLite ──────────────────────────────────────────────────────────────────

def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS biometrics (
            date              TEXT PRIMARY KEY,
            oura_readiness    INTEGER,
            oura_sleep_score  INTEGER,
            oura_sleep_hours  REAL,
            oura_hrv          REAL,
            oura_rhr          INTEGER,
            oura_body_temp    REAL,
            whoop_recovery    INTEGER,
            whoop_sleep_perf  REAL,
            whoop_sleep_hours REAL,
            whoop_hrv         REAL,
            whoop_rhr         INTEGER,
            whoop_strain      REAL
        )
    """)
    conn.commit()
    return conn


# ── Oura ────────────────────────────────────────────────────────────────────

def fetch_oura(date_str: str) -> dict:
    if not OURA_TOKEN:
        print('  [Oura] OURA_TOKEN が未設定。スキップ。')
        return {}
    headers = {'Authorization': f'Bearer {OURA_TOKEN}'}
    params  = {'start_date': date_str, 'end_date': date_str}
    result  = {}

    # Readiness score + body temperature deviation
    r = requests.get(f'{OURA_BASE}/daily_readiness', headers=headers, params=params, timeout=10)
    if r.ok:
        data = r.json().get('data', [])
        if data:
            d = data[0]
            result['readiness']  = d.get('score')
            result['body_temp']  = d.get('temperature_deviation')

    # Sleep score
    r = requests.get(f'{OURA_BASE}/daily_sleep', headers=headers, params=params, timeout=10)
    if r.ok:
        data = r.json().get('data', [])
        if data:
            result['sleep_score'] = data[0].get('score')

    # Detailed sleep: HRV (ms), RHR (bpm), total duration (sec)
    r = requests.get(f'{OURA_BASE}/sleep', headers=headers, params=params, timeout=10)
    if r.ok:
        data = r.json().get('data', [])
        if data:
            # 複数ある場合は最長の sleep を選ぶ
            main = max(data, key=lambda x: x.get('total_sleep_duration') or 0)
            result['hrv']         = main.get('average_hrv')
            result['rhr']         = main.get('lowest_heart_rate')
            total_sec = main.get('total_sleep_duration') or 0
            result['sleep_hours'] = round(total_sec / 3600, 1) if total_sec else None

    return result


# ── WHOOP ────────────────────────────────────────────────────────────────────

def refresh_whoop_token() -> str:
    r = requests.post(WHOOP_TOKEN_URL, data={
        'grant_type':    'refresh_token',
        'client_id':     WHOOP_CLIENT_ID,
        'client_secret': WHOOP_CLIENT_SECRET,
        'refresh_token': WHOOP_REFRESH_TOKEN,
        'scope':         'offline read:recovery read:sleep read:workout',
    }, timeout=10)
    r.raise_for_status()
    return r.json()['access_token']


def fetch_whoop(date_str: str) -> dict:
    if not (WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET and WHOOP_REFRESH_TOKEN):
        print('  [WHOOP] 認証情報未設定。スキップ。')
        return {}
    try:
        token = refresh_whoop_token()
    except Exception as e:
        print(f'  [WHOOP] token refresh 失敗: {e}')
        return {}

    headers = {'Authorization': f'Bearer {token}'}
    result  = {}

    # Cycle を取得 (WHOOP は cycle ベース)
    r = requests.get(f'{WHOOP_BASE}/cycle', headers=headers, params={
        'start': f'{date_str}T00:00:00.000Z',
        'end':   f'{date_str}T23:59:59.000Z',
        'limit': 1,
    }, timeout=10)
    if not r.ok:
        print(f'  [WHOOP] cycle 取得失敗: {r.status_code}')
        return {}
    records = r.json().get('records', [])
    if not records:
        print(f'  [WHOOP] {date_str} のデータなし')
        return {}

    cycle_id = records[0]['id']
    result['strain'] = records[0].get('score', {}).get('strain')

    # Recovery
    r = requests.get(f'{WHOOP_BASE}/recovery/{cycle_id}', headers=headers, timeout=10)
    if r.ok:
        score = r.json().get('score', {})
        result['recovery'] = score.get('recovery_score')
        result['hrv']      = score.get('hrv_rmssd_milli')
        result['rhr']      = score.get('resting_heart_rate')

    # Sleep
    r = requests.get(f'{WHOOP_BASE}/activity/sleep', headers=headers, params={
        'start': f'{date_str}T00:00:00.000Z',
        'end':   f'{date_str}T23:59:59.000Z',
        'limit': 1,
    }, timeout=10)
    if r.ok:
        sleeps = r.json().get('records', [])
        if sleeps:
            score = sleeps[0].get('score', {})
            result['sleep_perf']  = score.get('sleep_performance_percentage')
            stage = score.get('stage_summary', {})
            total_ms = (
                (stage.get('total_light_sleep_time_milli') or 0) +
                (stage.get('total_slow_wave_sleep_time_milli') or 0) +
                (stage.get('total_rem_sleep_time_milli') or 0)
            )
            result['sleep_hours'] = round(total_ms / 3_600_000, 1) if total_ms else None

    return result


# ── Diary 更新 ───────────────────────────────────────────────────────────────

def fmt(v, unit=''):
    return f'{v}{unit}' if v is not None else '—'

def diff_str(a, b, unit=''):
    if a is None or b is None:
        return '—'
    d = round(float(b) - float(a), 1)
    sign = '+' if d > 0 else ''
    return f'{sign}{d}{unit}'

def build_biometrics_block(date_str: str, oura: dict, whoop: dict) -> str:
    rows = []

    def add(label, o_val, w_val, unit='', show_diff=True):
        diff = diff_str(o_val, w_val, unit) if show_diff else '—'
        if o_val is not None or w_val is not None:
            rows.append(f'| {label} | {fmt(o_val, unit)} | {fmt(w_val, unit)} | {diff} |')

    add('総合スコア',  oura.get('readiness'),   whoop.get('recovery'),   show_diff=False)
    add('HRV',        oura.get('hrv'),          whoop.get('hrv'),         'ms')
    add('安静時心拍',  oura.get('rhr'),          whoop.get('rhr'),         'bpm')
    add('睡眠時間',    oura.get('sleep_hours'),  whoop.get('sleep_hours'), 'h')
    add('睡眠スコア',  oura.get('sleep_score'),  whoop.get('sleep_perf'),  show_diff=False)

    if oura.get('body_temp') is not None:
        rows.append(f'| 体温偏差 | {fmt(oura.get("body_temp"), "°C")} | — | — |')
    if whoop.get('strain') is not None:
        rows.append(f'| Strain | — | {fmt(whoop.get("strain"))} | — |')

    if not rows:
        return ''

    lines = [
        f'## 💤 バイオメトリクス',
        '',
        '| 指標 | Oura | WHOOP | 差(W-O) |',
        '|------|:----:|:-----:|:-------:|',
    ] + rows
    return '\n'.join(lines)


def update_diary_biometrics(date_str: str, block: str):
    if not block:
        return
    diary_path = DIARY_DIR / f'{date_str}.md'
    if not diary_path.exists():
        print(f'  [diary] {diary_path} が存在しないのでスキップ')
        return

    content = diary_path.read_text(encoding='utf-8')
    header  = '## 💤 バイオメトリクス'

    if header in content:
        # 既存セクションを置換
        start = content.find(header)
        next_sec = content.find('\n## ', start + len(header))
        if next_sec < 0:
            content = content[:start].rstrip() + '\n\n' + block + '\n'
        else:
            content = content[:start] + block + content[next_sec:]
    else:
        content = content.rstrip() + '\n\n' + block + '\n'

    diary_path.write_text(content, encoding='utf-8')
    print(f'  → {diary_path} にバイオメトリクスを書き込みました')


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    target = (date.today() - timedelta(days=1)).isoformat()
    print(f'バイオメトリクス同期: {target}')

    conn = init_db()

    print('  Oura 取得中...')
    oura = fetch_oura(target)
    print(f'    → {oura}')

    print('  WHOOP 取得中...')
    whoop = fetch_whoop(target)
    print(f'    → {whoop}')

    if not oura and not whoop:
        print('データなし。終了。')
        return

    conn.execute("""
        INSERT OR REPLACE INTO biometrics
        (date,
         oura_readiness, oura_sleep_score, oura_sleep_hours, oura_hrv, oura_rhr, oura_body_temp,
         whoop_recovery, whoop_sleep_perf, whoop_sleep_hours, whoop_hrv, whoop_rhr, whoop_strain)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        target,
        oura.get('readiness'),  oura.get('sleep_score'), oura.get('sleep_hours'),
        oura.get('hrv'),        oura.get('rhr'),         oura.get('body_temp'),
        whoop.get('recovery'),  whoop.get('sleep_perf'), whoop.get('sleep_hours'),
        whoop.get('hrv'),       whoop.get('rhr'),        whoop.get('strain'),
    ))
    conn.commit()
    conn.close()
    print('  → DB 保存完了')

    block = build_biometrics_block(target, oura, whoop)
    update_diary_biometrics(target, block)
    print('完了。')


if __name__ == '__main__':
    main()
