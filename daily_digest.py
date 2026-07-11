#!/usr/bin/env python3
"""
前日の日次ダイジェストをメール送信するスクリプト（朝6時専用）
run_morning.sh から呼ばれる

データソース:
  - diary/YYYY-MM-DD.md  (メモ・記録・ルーチン・写真参照)
  - plaud/YYYY-MM-DD.md  (生トランスクリプト)
  - diary/images/        (写真ファイル)
  - biometrics.db        (Oura/WHOOP)
  - triathlon.db         (Garmin/Strava)

引数:
  --date YYYY-MM-DD   対象日を指定（省略時は前日）
  --send addr1,addr2  送信先
"""

import os
import re
import sys
import base64
import smtplib
import sqlite3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from datetime import date, timedelta

import anthropic
from dotenv import load_dotenv
from diary_utils import DIARY_DIR

sys.path.insert(0, str(Path(__file__).parent.parent))
from slack_notify import send_slack_report

load_dotenv(Path(__file__).parent / '.env')
load_dotenv(Path(__file__).parent.parent / 'ai-news-mailer/.env', override=False)

PLAUD_DIR     = Path.home() / 'Documents/NeoBrain/plaud'
IMAGES_DIR    = DIARY_DIR / 'images'
BIOMETRICS_DB = Path(__file__).parent / 'data/biometrics.db'
TRIATHLON_DB  = Path.home() / 'AI Dev/Triathlon/data/triathlon.db'

SPORT_JA = {
    'Run': 'ラン', 'Swim': 'スイム', 'Ride': 'バイク',
    'WeightTraining': '筋トレ', 'Walk': 'ウォーク', 'Hike': 'ハイク',
}


# ── データ取得 ───────────────────────────────────────────────────────────────

def get_biometrics(date_str: str) -> dict:
    if not BIOMETRICS_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(BIOMETRICS_DB)
        row = conn.execute("""
            SELECT oura_readiness, oura_hrv, oura_sleep_hours,
                   whoop_recovery, whoop_hrv, whoop_sleep_hours, whoop_strain
            FROM biometrics WHERE date = ?
        """, (date_str,)).fetchone()
        conn.close()
    except Exception:
        return {}
    if not row:
        return {}
    return {
        'oura_readiness': row[0], 'oura_hrv': row[1], 'oura_sleep_h': row[2],
        'whoop_recovery': row[3], 'whoop_hrv': row[4], 'whoop_sleep_h': row[5],
        'whoop_strain': row[6],
    }


def get_fitness(date_str: str) -> dict:
    if not TRIATHLON_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(TRIATHLON_DB)
        row = conn.execute("""
            SELECT acute_training_load, chronic_training_load,
                   training_readiness_score, recovery_time
            FROM garmin_daily WHERE date = ?
        """, (date_str,)).fetchone()
        conn.close()
    except Exception:
        return {}
    if not row:
        return {}
    atl, ctl, readiness, rec = row
    acwr = round(atl / ctl, 2) if atl and ctl and ctl > 0 else None
    tsb  = round(ctl - atl, 0) if atl is not None and ctl is not None else None
    return {'atl': atl, 'ctl': ctl, 'acwr': acwr, 'tsb': tsb,
            'readiness': readiness, 'recovery_time': rec}


def get_training(date_str: str) -> list[dict]:
    if not TRIATHLON_DB.exists():
        return []
    try:
        conn = sqlite3.connect(TRIATHLON_DB)
        rows = conn.execute("""
            SELECT sport_type, distance, moving_time, average_heartrate, activity_training_load
            FROM garmin_activities WHERE date(start_date) = ?
            ORDER BY start_date
        """, (date_str,)).fetchall()
        conn.close()
    except Exception:
        return []
    result = []
    for sport, dist, t, hr, load in rows:
        result.append({
            'label': SPORT_JA.get(sport, sport),
            'dist_km': (dist or 0) / 1000,
            'time_min': int((t or 0) / 60),
            'hr': hr, 'load': load,
        })
    return result


def get_diary_text(date_str: str) -> dict:
    """日記から メモ・記録・ルーチン を取得"""
    path = DIARY_DIR / f'{date_str}.md'
    if not path.exists():
        return {}
    content = path.read_text(encoding='utf-8')

    def extract(header):
        for h in ([header] if isinstance(header, str) else header):
            idx = content.find(h)
            if idx >= 0:
                start = idx + len(h)
                end = content.find('\n## ', start)
                return (content[start:end] if end > 0 else content[start:]).strip()
        return ''

    return {
        'memo':    extract(['## メモ', '## 口頭メモ']),
        'record':  extract('## 記録'),
        'routine': extract('## ルーチン'),
    }


def get_plaud_text(date_str: str) -> str:
    """plaud の生トランスクリプトを取得（セッションヘッダー + 本文抜粋）"""
    path = PLAUD_DIR / f'{date_str}.md'
    if not path.exists():
        return ''
    content = path.read_text(encoding='utf-8')
    sections = re.split(r'\n## ', content)
    parts = []
    for sec in sections[1:]:
        lines = sec.strip().splitlines()
        header = lines[0] if lines else ''
        body_lines = [l for l in lines[1:]
                      if l.strip() and not re.match(r'^\d{2}:\d{2}:\d{2}$', l.strip())]
        body = ' '.join(body_lines)[:300]
        if body:
            parts.append(f'[{header}] {body}')
    return '\n'.join(parts)[:2000]


def get_diary_images(date_str: str) -> list[Path]:
    """日記に添付された画像ファイルリストを返す"""
    path = DIARY_DIR / f'{date_str}.md'
    if not path.exists():
        return []
    content = path.read_text(encoding='utf-8')
    refs = re.findall(r'!\[.*?\]\((images/[^\)]+)\)', content)
    return [DIARY_DIR / ref for ref in refs if (DIARY_DIR / ref).exists()]


# ── Claude サマリー生成 ───────────────────────────────────────────────────────

def generate_summary(target: date, diary: dict, plaud: str, images: list[Path]) -> str:
    memo   = diary.get('memo', '')
    record = diary.get('record', '')
    if not memo and not plaud and not images:
        return ''

    day_ja = ['月', '火', '水', '木', '金', '土', '日'][target.weekday()]
    prompt = (
        f'{target.isoformat()}（{day_ja}）の記録を3〜4文で簡潔にまとめてください。'
        f'出来事・気づき・感情の動きを中心に、文章で。写真があればその内容も触れてください。\n\n'
    )
    if memo:
        prompt += f'## メモ\n{memo[:800]}\n\n'
    if record:
        prompt += f'## 記録\n{record[:400]}\n\n'
    if plaud:
        prompt += f'## 音声記録\n{plaud[:800]}\n\n'

    content: list = [{'type': 'text', 'text': prompt}]

    # 画像を追加（最大4枚）
    for img_path in images[:4]:
        try:
            ext = img_path.suffix.lower().lstrip('.')
            media_type = {
                'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp',
            }.get(ext, 'image/jpeg')
            b64 = base64.b64encode(img_path.read_bytes()).decode()
            content.append({
                'type': 'image',
                'source': {'type': 'base64', 'media_type': media_type, 'data': b64},
            })
        except Exception:
            pass

    client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=400,
        messages=[{'role': 'user', 'content': content}],
    )
    return msg.content[0].text.strip()


# ── HTML 生成 ─────────────────────────────────────────────────────────────────

def fmt(v, unit='', default='—'):
    if v is None:
        return default
    return f'{v:.1f}{unit}' if isinstance(v, float) else f'{v}{unit}'


def table_rows(pairs: list[tuple]) -> str:
    return ''.join(
        f'<tr><td style="padding:5px 14px 5px 0;color:#888;font-size:13px;white-space:nowrap;">{label}</td>'
        f'<td style="padding:5px 0;font-size:13px;font-weight:600;color:#1a1a1a;">{val}</td></tr>'
        for label, val in pairs if val != '—'
    )


def build_html(target: date, bio: dict, fitness: dict,
               training: list[dict], summary: str) -> str:

    day_ja = ['月', '火', '水', '木', '金', '土', '日'][target.weekday()]

    # バイオ
    sleep_h  = bio.get('oura_sleep_h') or bio.get('whoop_sleep_h')
    hrv      = bio.get('oura_hrv') or bio.get('whoop_hrv')
    recovery = bio.get('oura_readiness') or bio.get('whoop_recovery')
    bio_html = ''
    rows = table_rows([
        ('睡眠', fmt(sleep_h, 'h')),
        ('HRV', fmt(hrv, 'ms')),
        ('回復スコア', fmt(recovery)),
        ('Strain', fmt(bio.get('whoop_strain'))),
    ])
    if rows:
        bio_html = f'<div style="margin-bottom:20px;"><div class="sec-label">💤 バイオメトリクス</div><table>{rows}</table></div>'

    # フィットネス
    fit_html = ''
    rows = table_rows([
        ('ACWR', fmt(fitness.get('acwr'))),
        ('Form (TSB)', fmt(fitness.get('tsb'))),
        ('Readiness', fmt(fitness.get('readiness'))),
        ('回復残時間', fmt(fitness.get('recovery_time'), 'h')),
    ]) if fitness else ''
    if rows:
        fit_html = f'<div style="margin-bottom:20px;"><div class="sec-label">🏋️ フィットネス</div><table>{rows}</table></div>'

    # トレーニング
    train_html = ''
    if training:
        items = []
        for a in training:
            parts = [f'<strong>{a["label"]}</strong>']
            if a['dist_km'] > 0.05:
                parts.append(f'{a["dist_km"]:.1f}km')
            if a['time_min']:
                parts.append(f'{a["time_min"]}分')
            if a['hr']:
                parts.append(f'HR{a["hr"]:.0f}')
            if a['load']:
                parts.append(f'負荷{a["load"]:.0f}')
            items.append(' &middot; '.join(parts))
        train_html = (
            f'<div style="margin-bottom:20px;"><div class="sec-label">🏃 トレーニング</div>'
            + ''.join(f'<div style="font-size:13px;padding:3px 0;color:#333;">{i}</div>' for i in items)
            + '</div>'
        )

    # サマリー
    summary_html = ''
    if summary:
        summary_html = (
            f'<div style="margin-bottom:20px;padding:14px 16px;background:#f5f3ff;'
            f'border-radius:8px;border-left:3px solid #6366f1;">'
            f'<div class="sec-label">✨ 昨日のまとめ</div>'
            f'<div style="font-size:14px;color:#444;line-height:1.8;">{summary}</div>'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
  .sec-label {{font-size:11px;font-weight:700;color:#999;text-transform:uppercase;
               letter-spacing:0.6px;margin-bottom:8px;}}
</style>
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             max-width:560px;margin:0 auto;padding:24px 20px;background:#fff;color:#333;">
  <h2 style="margin:0 0 20px;font-size:19px;color:#1a1a1a;">
    {target.strftime('%m/%d')}（{day_ja}）のダイジェスト
  </h2>
  {summary_html}
  {bio_html}
  {fit_html}
  {train_html}
  <hr style="border:none;border-top:1px solid #eee;margin:20px 0 12px;">
  <p style="color:#ccc;font-size:11px;margin:0;">NeoBrain 日次ダイジェスト</p>
</body>
</html>"""


# ── メール送信 ────────────────────────────────────────────────────────────────

def send_email(recipients: list[str], subject: str, html: str) -> None:
    gmail_user = os.environ.get('GMAIL_USER', '')
    gmail_pass = os.environ.get('GMAIL_APP_PASSWORD', '')
    if not gmail_user or not gmail_pass:
        print('  ⚠️  GMAIL_USER / GMAIL_APP_PASSWORD 未設定のためスキップ')
        return

    # 画像インライン埋め込みのため multipart/related を使用
    msg_root = MIMEMultipart('related')
    msg_root['Subject'] = subject
    msg_root['From']    = gmail_user
    msg_root['To']      = ', '.join(recipients)

    msg_alt = MIMEMultipart('alternative')
    msg_alt.attach(MIMEText(html, 'html', 'utf-8'))
    msg_root.attach(msg_alt)

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, recipients, msg_root.as_string())

    print(f'  → 日次ダイジェスト送信完了: {", ".join(recipients)}')


# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    target = (date.fromisoformat(args[args.index('--date') + 1])
              if '--date' in args else date.today() - timedelta(days=1))

    recipients = []
    if '--send' in args:
        idx = args.index('--send')
        if idx + 1 < len(args):
            recipients = [r.strip() for r in args[idx + 1].split(',')]

    date_str = target.isoformat()
    day_ja   = ['月', '火', '水', '木', '金', '土', '日'][target.weekday()]
    print(f'日次ダイジェスト生成中: {date_str}（{day_ja}）')

    bio      = get_biometrics(date_str)
    fitness  = get_fitness(date_str)
    training = get_training(date_str)
    diary    = get_diary_text(date_str)
    plaud    = get_plaud_text(date_str)
    images   = get_diary_images(date_str)

    print(f'  plaud: {"あり" if plaud else "なし"} / 写真: {len(images)}枚')
    print('  Claudeにサマリー生成依頼中...')
    summary = generate_summary(target, diary, plaud, images)

    html    = build_html(target, bio, fitness, training, summary)
    subject = f'[NeoBrain] {target.strftime("%m/%d")}（{day_ja}）のダイジェスト'

    if recipients:
        send_email(recipients, subject, html)
    else:
        print('  --send 未指定のためメール送信スキップ')

    print('  Slack 送信中...')
    ok = send_slack_report(
        title=subject,
        html_body=html,
        username='NeoBrain',
        emoji=':memo:',
    )
    print(f'  → Slack {"送信完了" if ok else "送信失敗"}')

    print('完了。')


if __name__ == '__main__':
    main()
