#!/usr/bin/env python3
"""
Oura + WHOOP + Google Health バイオメトリクス同期
前日のデータを取得し、SQLite に保存して diary に書き込む。

必要な .env 変数:
  OURA_TOKEN             # Oura Personal Access Token
  WHOOP_CLIENT_ID        # WHOOP OAuth2 Client ID
  WHOOP_CLIENT_SECRET    # WHOOP OAuth2 Client Secret
  WHOOP_REFRESH_TOKEN    # WHOOP OAuth2 Refresh Token (whoop_auth.py で取得)
  GHEALTH_CLIENT_ID      # Google OAuth2 Client ID
  GHEALTH_CLIENT_SECRET  # Google OAuth2 Client Secret
  GHEALTH_REFRESH_TOKEN  # Google OAuth2 Refresh Token (google_health_auth.py で取得)

Google Health API (2026-03-24 GA) は Fitbit Web API の次世代版。Fitbit / Pixel Watch /
サードパーティ機器を1本の統合ストリームで返す。Google Fit REST API は2026年末停止・
2024年5月以降は新規登録不可、Health Connect は Android 端末内ローカル API なので
どちらも Mac の launchd からは使えない。
"""
import os
import re
import sys
import json
import time
import sqlite3
import requests
import ghealth
from pathlib import Path
from diary_utils import DIARY_DIR, ensure_diary
from datetime import date, timedelta
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / '.env'
load_dotenv(ENV_PATH)

OURA_TOKEN           = os.environ.get('OURA_TOKEN', '')
WHOOP_CLIENT_ID      = os.environ.get('WHOOP_CLIENT_ID', '')
WHOOP_CLIENT_SECRET  = os.environ.get('WHOOP_CLIENT_SECRET', '')
WHOOP_REFRESH_TOKEN  = os.environ.get('WHOOP_REFRESH_TOKEN', '')
WHOOP_ACCESS_TOKEN   = os.environ.get('WHOOP_ACCESS_TOKEN', '')
# Google Health の認証情報は ghealth.py 側が .env から読む

DB_PATH   = Path(__file__).parent / 'data/biometrics.db'

OURA_BASE  = 'https://api.ouraring.com/v2/usercollection'
WHOOP_BASE = 'https://api.prod.whoop.com/developer/v2'
WHOOP_TOKEN_URL = 'https://api.prod.whoop.com/oauth/oauth2/token'


def _set_env(key: str, val: str):
    """.env の1行を書き換える。キーが無ければ末尾に足す。"""
    text = ENV_PATH.read_text(encoding='utf-8') if ENV_PATH.exists() else ''
    if re.search(rf'^{key}=', text, re.MULTILINE):
        text = re.sub(rf'^{key}=.*', f'{key}={val}', text, flags=re.MULTILINE)
    else:
        text = text.rstrip('\n') + f'\n{key}={val}\n'
    ENV_PATH.write_text(text, encoding='utf-8')


# ── SQLite ──────────────────────────────────────────────────────────────────

# (カラム名, 型, fetch_ghealth() の戻り値キー) — DB migration と INSERT の両方で使う単一の正本
GHEALTH_FIELDS = [
    ('ghealth_rhr',            'INTEGER', 'rhr'),
    ('ghealth_hrv',            'REAL',    'hrv'),
    ('ghealth_sleep_hours',    'REAL',    'sleep_hours'),
    ('ghealth_sleep_eff',      'REAL',    'sleep_eff'),
    ('ghealth_spo2',           'REAL',    'spo2'),
    ('ghealth_breathing_rate', 'REAL',    'breathing_rate'),
    ('ghealth_skin_temp',      'REAL',    'skin_temp'),
    ('ghealth_vo2max',         'REAL',    'vo2max'),
]
GHEALTH_COLUMNS = [(name, typ) for name, typ, _ in GHEALTH_FIELDS]


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
    # 既存DBへの後付けカラム（Google Health）。CREATE TABLE は既存DBでは走らないので
    # PRAGMA で実列を見てから足りない分だけ ALTER する。
    have = {row[1] for row in conn.execute('PRAGMA table_info(biometrics)')}
    for name, typ in GHEALTH_COLUMNS:
        if name not in have:
            conn.execute(f'ALTER TABLE biometrics ADD COLUMN {name} {typ}')
    conn.commit()
    return conn


# ── Oura ────────────────────────────────────────────────────────────────────

def _get(url, **kwargs):
    """タイムアウト・接続エラー時に None を返す"""
    try:
        return requests.get(url, timeout=30, **kwargs)
    except Exception as e:
        print(f'    [warn] GET {url} 失敗: {e}')
        return None


def fetch_oura(date_str: str) -> dict:
    if not OURA_TOKEN:
        print('  [Oura] OURA_TOKEN が未設定。スキップ。')
        return {}
    headers = {'Authorization': f'Bearer {OURA_TOKEN}'}
    params  = {'start_date': date_str, 'end_date': date_str}
    result  = {}

    # Readiness score + body temperature deviation
    r = _get(f'{OURA_BASE}/daily_readiness', headers=headers, params=params)
    if r and r.ok:
        data = r.json().get('data', [])
        if data:
            d = data[0]
            result['readiness']  = d.get('score')
            result['body_temp']  = d.get('temperature_deviation')

    # Sleep score
    r = _get(f'{OURA_BASE}/daily_sleep', headers=headers, params=params)
    if r and r.ok:
        data = r.json().get('data', [])
        if data:
            result['sleep_score'] = data[0].get('score')

    # Detailed sleep: HRV (ms), RHR (bpm), total duration (sec)
    # Oura /sleep returns empty when start_date == end_date; query from day-1 and filter
    target_date = date.fromisoformat(date_str)
    r = _get(f'{OURA_BASE}/sleep', headers=headers, params={
        'start_date': (target_date - timedelta(days=1)).isoformat(),
        'end_date': date_str,
    })
    if r and r.ok:
        data = [x for x in r.json().get('data', []) if x.get('day') == date_str]
        if data:
            # 複数ある場合は最長の long_sleep を優先
            long_sleeps = [x for x in data if x.get('type') == 'long_sleep']
            main = max(long_sleeps or data, key=lambda x: x.get('total_sleep_duration') or 0)
            result['hrv']         = main.get('average_hrv')
            result['rhr']         = main.get('lowest_heart_rate')
            total_sec = main.get('total_sleep_duration') or 0
            result['sleep_hours'] = round(total_sec / 3600, 1) if total_sec else None

    return result


# ── WHOOP ────────────────────────────────────────────────────────────────────

_whoop_refreshed = False


def get_whoop_access_token() -> str:
    """Access Token を取得。Refresh Token で更新し、失敗時は保存済み Access Token を使う。

    WHOOP の refresh_token は使うたびにローテートするので、1プロセス内では
    最初の1回だけ更新して以後は使い回す。--from の一括同期で日数ぶん
    ローテートさせると、途中で1回失敗しただけでトークンが壊れる。"""
    global WHOOP_REFRESH_TOKEN, WHOOP_ACCESS_TOKEN, _whoop_refreshed
    env_path = Path(__file__).parent / '.env'

    if _whoop_refreshed and WHOOP_ACCESS_TOKEN:
        return WHOOP_ACCESS_TOKEN

    if WHOOP_REFRESH_TOKEN:
        r = requests.post(WHOOP_TOKEN_URL, data={
            'grant_type':    'refresh_token',
            'client_id':     WHOOP_CLIENT_ID,
            'client_secret': WHOOP_CLIENT_SECRET,
            'refresh_token': WHOOP_REFRESH_TOKEN,
        }, timeout=10)
        if r.ok:
            data = r.json()
            new_access  = data.get('access_token', '')
            new_refresh = data.get('refresh_token', '')
            if new_access:
                WHOOP_ACCESS_TOKEN = new_access
                env_text = env_path.read_text(encoding='utf-8')
                env_text = re.sub(r'WHOOP_ACCESS_TOKEN=.*', f'WHOOP_ACCESS_TOKEN={new_access}', env_text)
                if new_refresh:
                    WHOOP_REFRESH_TOKEN = new_refresh
                    env_text = re.sub(r'WHOOP_REFRESH_TOKEN=.*', f'WHOOP_REFRESH_TOKEN={new_refresh}', env_text)
                env_path.write_text(env_text, encoding='utf-8')
                _whoop_refreshed = True
                return new_access

    # Refresh 失敗 → 保存済み Access Token で試みる
    if WHOOP_ACCESS_TOKEN:
        print('  [WHOOP] Refresh Token 無効。保存済み Access Token を使用。')
        return WHOOP_ACCESS_TOKEN

    raise RuntimeError('WHOOP token 取得失敗。whoop_auth.py を再実行してください。')


def fetch_whoop(date_str: str) -> dict:
    # 機械固有の無効化スイッチ。.no_whoop マーカーがある機では WHOOP を一切叩かない
    # （WHOOP の refresh_token はローテート＆.env書き戻しのため、複数機で同時に走ると
    #  互いのトークンを無効化し合って破損する。移行期は1機のみで回すための安全装置）。
    if (Path(__file__).parent / '.no_whoop').exists():
        print('  [WHOOP] .no_whoop マーカー検出 — この機では WHOOP 無効（二重ローテート防止）。スキップ。')
        return {}
    if not (WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET and (WHOOP_REFRESH_TOKEN or WHOOP_ACCESS_TOKEN)):
        print('  [WHOOP] 認証情報未設定。スキップ。')
        return {}
    try:
        token = get_whoop_access_token()
    except Exception as e:
        print(f'  [WHOOP] token refresh 失敗: {e}')
        return {}

    headers = {'Authorization': f'Bearer {token}'}
    result  = {}

    # Cycle を取得 (JST+9 基準: UTC前日15:00〜当日14:59)
    d = date.fromisoformat(date_str)
    utc_start = (d - timedelta(days=1)).isoformat() + 'T15:00:00.000Z'
    utc_end   = d.isoformat() + 'T14:59:59.000Z'
    r = _get(f'{WHOOP_BASE}/cycle', headers=headers, params={
        'start': utc_start,
        'end':   utc_end,
        'limit': 25,
    })
    if not r or not r.ok:
        print(f'  [WHOOP] cycle 取得失敗: {r.status_code if r else "timeout"}')
        return {}
    # 完了済み(end != None)のcycleのみ対象
    all_records = [rec for rec in r.json().get('records', []) if rec.get('end')]
    if not all_records:
        print(f'  [WHOOP] {date_str} の完了済みcycleなし')
        return {}
    records = all_records

    cycle_id = records[0]['id']
    result['strain'] = records[0].get('score', {}).get('strain')

    # Recovery (v2: リスト形式、cycle_idで照合)
    r = _get(f'{WHOOP_BASE}/recovery', headers=headers, params={
        'start': utc_start, 'end': utc_end, 'limit': 25,
    })
    if r and r.ok:
        for rec in r.json().get('records', []):
            if rec.get('cycle_id') == cycle_id:
                score = rec.get('score', {})
                result['recovery'] = score.get('recovery_score')
                result['hrv']      = score.get('hrv_rmssd_milli')
                result['rhr']      = score.get('resting_heart_rate')
                break

    # Sleep (v2)
    r = _get(f'{WHOOP_BASE}/activity/sleep', headers=headers, params={
        'start': utc_start, 'end': utc_end, 'limit': 25,
    })
    if r and r.ok:
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


# ── Google Health ────────────────────────────────────────────────────────────

def _gh_date(point: dict) -> str:
    """データポイントの「日付」を取り出す。Google Health は型によって
    civilStartTime（日付オブジェクト）と interval.startTime（RFC3339）が混在するため、
    実際に返ってきた形に合わせて拾う。"""
    for key in ('civilStartTime', 'startTime', 'date'):
        v = point.get(key)
        if isinstance(v, dict) and v.get('year'):
            return f"{v['year']:04d}-{v.get('month', 1):02d}-{v.get('day', 1):02d}"
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    iv = point.get('interval') or {}
    for key in ('startTime', 'start_time'):
        v = iv.get(key)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
        if isinstance(v, dict) and v.get('year'):
            return f"{v['year']:04d}-{v.get('month', 1):02d}-{v.get('day', 1):02d}"
    return ''


def _gh_number(obj):
    """日次ロールアップは value の中に数値が1つ入る形。キー名が型ごとに違うので
    最初に見つかった数値を取る（ネストは浅い）。"""
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return float(obj)
    if isinstance(obj, str):
        try:
            return float(obj)
        except ValueError:
            return None
    if isinstance(obj, dict):
        for v in obj.values():
            n = _gh_number(v)
            if n is not None:
                return n
    if isinstance(obj, list):
        for v in obj:
            n = _gh_number(v)
            if n is not None:
                return n
    return None


def fetch_ghealth(date_str: str) -> dict:
    """Google Health API から1日分を取る。

    Fitbit Web API の後継。Fitbit / Pixel Watch / サードパーティ機器のデータが
    Google アカウントの1本のストリームに統合されて返る。

    日付の絞り込みはフィルタ構文を使わず、直近ぶんを取ってから日付一致で拾う。
    型ごとにフィルタ対象のフィールド名が違い、取り違えると黙って空が返るため。"""
    if (Path(__file__).parent / '.no_ghealth').exists():
        print('  [GHealth] .no_ghealth マーカー検出 — この機では無効。スキップ。')
        return {}
    if not ghealth.configured():
        print('  [GHealth] 認証情報未設定。スキップ。')
        return {}

    result = {}
    # 当日から遡って探す件数。日次同期なら数件で足りるが、--from の遡りにも効くよう
    # 少し多めに取る（sleep は pageSize 上限 25）。
    for key, dtype in ghealth.DATA_TYPES.items():
        size = 25 if dtype == 'sleep' else 60
        data = ghealth.get(dtype, {'pageSize': size})
        if not data:
            continue
        points = data.get('dataPoints') or data.get('rollupDataPoints') or []
        match = next((p for p in points if _gh_date(p) == date_str), None)
        if not match:
            continue

        if key == 'sleep':
            # 睡眠セッションは時間・効率を自前で組み立てる
            val = match.get('value') or match
            asleep_s = _gh_number(val.get('sleepDurationSeconds') or
                                  val.get('totalSleepDuration'))
            in_bed_s = _gh_number(val.get('timeInBedSeconds') or
                                  val.get('totalTimeInBed'))
            if asleep_s:
                result['sleep_hours'] = round(asleep_s / 3600, 1)
                if in_bed_s:
                    result['sleep_eff'] = round(asleep_s / in_bed_s * 100, 1)
            continue

        n = _gh_number(match.get('value', match))
        if n is not None:
            result[key] = round(n, 2) if key != 'rhr' else int(round(n))

    return {k: v for k, v in result.items() if v is not None}


# ── Diary 更新 ───────────────────────────────────────────────────────────────

def fmt(v, unit=''):
    return f'{v}{unit}' if v is not None else '—'

def build_biometrics_block(date_str: str, oura: dict, whoop: dict, gh: dict) -> str:
    rows = []

    def add(label, o_val, w_val, f_val, unit=''):
        if o_val is None and w_val is None and f_val is None:
            return
        rows.append(f'| {label} | {fmt(o_val, unit)} | {fmt(w_val, unit)} '
                    f'| {fmt(f_val, unit)} |')

    add('総合スコア',  oura.get('readiness'),   whoop.get('recovery'),    None)
    add('HRV',        oura.get('hrv'),          whoop.get('hrv'),         gh.get('hrv'),         'ms')
    add('安静時心拍',  oura.get('rhr'),          whoop.get('rhr'),         gh.get('rhr'),         'bpm')
    add('睡眠時間',    oura.get('sleep_hours'),  whoop.get('sleep_hours'), gh.get('sleep_hours'), 'h')
    add('睡眠スコア',  oura.get('sleep_score'),  whoop.get('sleep_perf'),  gh.get('sleep_eff'))
    add('体温偏差',    oura.get('body_temp'),    None,                     gh.get('skin_temp'),   '°C')
    add('呼吸数',      None,                     None,                     gh.get('breathing_rate'), '/分')
    add('SpO2',       None,                     None,                     gh.get('spo2'),        '%')
    add('VO2max',     None,                     None,                     gh.get('vo2max'))
    add('Strain',     None,                     whoop.get('strain'),      None)

    if not rows:
        return ''

    lines = [
        f'## 💤 バイオメトリクス',
        '',
        '| 指標 | Oura | WHOOP | Google Health |',
        '|------|:----:|:-----:|:------------:|',
    ] + rows
    return '\n'.join(lines)


def update_diary_biometrics(date_str: str, block: str):
    if not block:
        return
    diary_path = DIARY_DIR / f'{date_str}.md'

    # 日記は ~/Documents/NeoBrain 配下 = macOS TCC 保護下。launchd から実行する
    # python に Full Disk Access が無いと PermissionError になる。DB 保存は既に
    # 完了しているのでダッシュボードには影響させず、書き戻しのみ警告して握りつぶす。
    try:
        ensure_diary(date_str)
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
    except PermissionError as e:
        print(f'  ⚠ 日記への書き戻しをスキップ（DB保存は成功済み）: {e}')
        print('    → 実行 python に Full Disk Access を付与すると書き戻しも有効化されます。')


# ── Main ─────────────────────────────────────────────────────────────────────

def sync_one(conn, target: str):
    print(f'\n── {target} ──')
    print('  Oura 取得中...')
    oura = fetch_oura(target)
    print(f'    → {oura}')

    print('  WHOOP 取得中...')
    whoop = fetch_whoop(target)
    print(f'    → {whoop}')

    print('  Google Health 取得中...')
    gh = fetch_ghealth(target)
    print(f'    → {gh}')

    if not oura and not whoop and not gh:
        print('  データなし。スキップ。')
        return

    # INSERT OR REPLACE は行ごと差し替えるので、取得できなかったソースの既存値を
    # NULL で潰してしまう。先に現在の行を読んで、新しい値があるものだけ上書きする。
    base_cols = [
        'oura_readiness', 'oura_sleep_score', 'oura_sleep_hours',
        'oura_hrv', 'oura_rhr', 'oura_body_temp',
        'whoop_recovery', 'whoop_sleep_perf', 'whoop_sleep_hours',
        'whoop_hrv', 'whoop_rhr', 'whoop_strain',
    ]
    cols = base_cols + [name for name, _, _ in GHEALTH_FIELDS]
    row = conn.execute(
        f'SELECT {", ".join(cols)} FROM biometrics WHERE date = ?', (target,)
    ).fetchone()
    vals = dict(zip(cols, row)) if row else {c: None for c in cols}

    fresh = {
        'oura_readiness':   oura.get('readiness'),
        'oura_sleep_score': oura.get('sleep_score'),
        'oura_sleep_hours': oura.get('sleep_hours'),
        'oura_hrv':         oura.get('hrv'),
        'oura_rhr':         oura.get('rhr'),
        'oura_body_temp':   oura.get('body_temp'),
        'whoop_recovery':   whoop.get('recovery'),
        'whoop_sleep_perf': whoop.get('sleep_perf'),
        'whoop_sleep_hours': whoop.get('sleep_hours'),
        'whoop_hrv':        whoop.get('hrv'),
        'whoop_rhr':        whoop.get('rhr'),
        'whoop_strain':     whoop.get('strain'),
    }
    fresh.update({name: gh.get(key) for name, _, key in GHEALTH_FIELDS})
    for k, v in fresh.items():
        if v is not None:
            vals[k] = v

    conn.execute(
        f'INSERT OR REPLACE INTO biometrics (date, {", ".join(cols)}) '
        f'VALUES ({", ".join(["?"] * (len(cols) + 1))})',
        [target] + [vals[c] for c in cols])
    conn.commit()
    print('  → DB 保存完了')

    block = build_biometrics_block(target, oura, whoop, gh)
    update_diary_biometrics(target, block)


def main():
    # 引数: --from YYYY-MM-DD [--to YYYY-MM-DD]
    # 引数なし: 前日のみ
    args = sys.argv[1:]
    if '--from' in args:
        idx = args.index('--from')
        start = date.fromisoformat(args[idx + 1])
        if '--to' in args:
            end = date.fromisoformat(args[args.index('--to') + 1])
        else:
            end = date.today() - timedelta(days=1)
        dates = []
        d = start
        while d <= end:
            dates.append(d.isoformat())
            d += timedelta(days=1)
        print(f'バイオメトリクス一括同期: {dates[0]} 〜 {dates[-1]} ({len(dates)}日)')
    else:
        dates = [(date.today() - timedelta(days=1)).isoformat()]
        print(f'バイオメトリクス同期: {dates[0]}')

    conn = init_db()
    for i, target in enumerate(dates):
        sync_one(conn, target)
        if len(dates) > 1:
            time.sleep(1.0)  # レート制限対策
    conn.close()
    print('\n完了。')


if __name__ == '__main__':
    main()
