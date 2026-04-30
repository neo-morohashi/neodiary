#!/usr/bin/env python3
"""
diary-web inbox puller
GitHub の inbox/{YYYY-MM-DD_HHMMSS}.txt を読み取り、
Claude で処理して NeoBrain/diary/ に追記する。
workmemo/{YYYY-MM-DD_HHMMSS}.txt も処理して
NeoBrain/context/work/ に保存する。
"""
import os
import re
import base64
from collections import defaultdict
from pathlib import Path
from datetime import date as date_type
import requests
import anthropic
from dotenv import load_dotenv
from diary_utils import DIARY_DIR, TEMPLATE, ensure_diary

load_dotenv(Path(__file__).parent / '.env')

GITHUB_TOKEN  = os.environ['GITHUB_TOKEN']
GITHUB_REPO   = os.environ['GITHUB_REPO']
IMAGES_DIR    = Path.home() / 'Documents/NeoBrain/diary/images'
WORK_DIR      = Path.home() / 'Documents/NeoBrain/context/work'
BOOKMARK_DIR  = Path.home() / 'Documents/NeoBrain/bookmark'
BOOKMARK_FILE = BOOKMARK_DIR / 'bookmark.md'
ANTHROPIC_KEY = os.environ['ANTHROPIC_API_KEY']

GH_API = 'https://api.github.com'
HEADERS = {
    'Authorization': f'Bearer {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github+json',
}

# ファイル名パターン: YYYY-MM-DD_HHMMSS.txt
FILENAME_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})_\d{6}\.txt$')
# 画像ファイル名パターン: YYYY-MM-DD_HHMMSS_N.{ext}
IMAGE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})_\d{6}_\d+\.(png|jpe?g|gif|webp)$', re.IGNORECASE)
URL_RE = re.compile(r'https?://[^\s\)\]>]+')

JOURNAL_PROMPT = """\
あなたは日記アシスタントです。{date}の日記テキストを解析し、各セクションに振り分けてください。
コードブロックや説明文なしでJSONのみ返してください。

## 入力テキスト
{input}

## 出力JSON
{{
  "memo": "音声メモ・出来事・感想をそのまま記述（整形しない）",
  "tags": [],
  "energy": 3,
  "output_candidate": false,
  "routine": {{
    "wakeup":         "起床時刻（例: 07:00）または空文字",
    "sleep":          "睡眠時間（例: 6.5h）または空文字",
    "exercise":       "運動内容（例: ラン 5km）または空文字",
    "breakfast":      "朝食内容または空文字",
    "lunch":          "ランチ内容または空文字",
    "dinner":         "夕食内容または空文字",
    "energy_morning": "朝のエネルギー 1〜5 または空文字",
    "energy_night":   "夜のエネルギー 1〜5 または空文字"
  }}
}}

## ルール
- 言及がないフィールドは空文字 "" にする
- memo には上記フィールドに含まれない内容のみ入れる（重複禁止）
- tags は次から複数選択: work/mercer, work/client/名前, work/bd, work/research, work/thought_leadership, personal/triathlon, personal/book, personal/dog, personal/realestate, personal/reflection
- energy: 全体的なエネルギーレベル（1=低調, 5=充実）
- output_candidate: thought_leadershipネタ・note記事候補があれば true
"""

URL_SUMMARY_PROMPT = """\
以下のWebページの内容を日本語で3〜5行に要約してください。説明文は不要で、要約本文のみ返してください。

{content}
"""

BOOKMARK_CLASSIFY_PROMPT = """\
以下のブックマーク情報を分析し、JSONのみ返してください。

タイトル: {title}
サマリー: {summary}

{{
  "topic": ["AI", "人事組織"],
  "output_target": "newsletter"
}}

## ルール
- topic: [AI, 人事組織, スポーツ健康] の中から該当するものを複数選択（最低1つ）
- output_target: newsletter | note | book | idea-pool の中から1つ選択
  - newsletter: 広く共有したい時事的な情報
  - note: 深掘りしたい専門的な内容
  - book: 書籍執筆に活用できる知見
  - idea-pool: アイデアの種になりそうなもの
"""



WORKMEMO_PROMPT = """\
あなたは仕事メモアシスタントです。以下のメモを整理してください。
説明文・コードブロック記法は不要です。以下の形式でJSONのみ返してください。

## 入力メモ
{input}

## 出力フォーマット（厳守）
{{
  "project": "案件名・トピック（推定）",
  "summary": "1〜2文の要旨",
  "content": "整理したメモ本文（箇条書き、Markdown）",
  "next_actions": "ネクストアクションがあれば箇条書き、なければ空文字",
  "output_candidate": false
}}
"""


def list_inbox_images():
    """inbox/images/ 配下の画像ファイル一覧を取得"""
    url = f'{GH_API}/repos/{GITHUB_REPO}/contents/inbox/images'
    res = requests.get(url, headers=HEADERS)
    if res.status_code == 404:
        return []
    res.raise_for_status()
    return [f for f in res.json() if IMAGE_RE.match(f['name'])]


def download_image(path: str, filename: str, date_str: str) -> Path:
    """GitHubから画像をダウンロードしてローカルに保存"""
    url = f'{GH_API}/repos/{GITHUB_REPO}/contents/{path}'
    res = requests.get(url, headers=HEADERS)
    res.raise_for_status()
    data = res.json()
    img_bytes = base64.b64decode(data['content'])
    dest_dir = IMAGES_DIR / date_str
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    dest.write_bytes(img_bytes)
    return dest


def append_images_to_diary(diary_path: Path, image_paths: list):
    """日記ファイルの末尾に写真セクションを追記（重複防止）"""
    if not image_paths:
        return
    content = diary_path.read_text(encoding='utf-8')
    lines = []
    for p in image_paths:
        # NeoBrain/diary/ からの相対パス
        rel = p.relative_to(DIARY_DIR)
        lines.append(f'![{p.name}]({rel})')
    new_links = '\n'.join(lines)

    if '## 📷 写真' in content:
        # すでにセクションがある場合は末尾に追記
        content = content.rstrip() + '\n' + new_links + '\n'
    else:
        content = content.rstrip() + f'\n\n## 📷 写真\n\n{new_links}\n'
    diary_path.write_text(content, encoding='utf-8')


def list_inbox_files():
    """inbox/ 配下のファイル一覧を取得"""
    url = f'{GH_API}/repos/{GITHUB_REPO}/contents/inbox'
    res = requests.get(url, headers=HEADERS)
    if res.status_code == 404:
        return []
    res.raise_for_status()
    return [f for f in res.json() if FILENAME_RE.match(f['name'])]


def list_workmemo_files():
    """workmemo/ 配下の .txt ファイル一覧を取得"""
    url = f'{GH_API}/repos/{GITHUB_REPO}/contents/workmemo'
    res = requests.get(url, headers=HEADERS)
    if res.status_code == 404:
        return []
    res.raise_for_status()
    return [f for f in res.json() if FILENAME_RE.match(f['name'])]


def parse_workmemo_headers(content: str) -> dict:
    """[CLIENT:], [TAGS:], [TIME:], [FILE:] ヘッダーを解析してメタデータを返す"""
    client = 'internal'
    tags = []
    file_urls = []
    lines = content.splitlines()
    body_lines = []
    for line in lines:
        m_client = re.match(r'^\[CLIENT:\s*(.*?)\]', line)
        m_tags   = re.match(r'^\[TAGS:\s*(.*?)\]', line)
        m_file   = re.match(r'^\[FILE:\s*(.*?)\]', line)
        if m_client:
            client = m_client.group(1).strip() or 'internal'
        elif m_tags:
            tags = [t.strip() for t in m_tags.group(1).split(',') if t.strip()]
        elif m_file:
            u = m_file.group(1).strip()
            if u:
                file_urls.append(u)
        elif re.match(r'^\[TIME:', line):
            pass  # 時刻情報は無視
        else:
            body_lines.append(line)
    body = '\n'.join(body_lines).strip()
    return {'client': client, 'tags': tags, 'file_urls': file_urls, 'body': body}


def format_workmemo_with_claude(client: str, tags: list, body: str) -> dict:
    """Claude でワークメモを構造化"""
    import json
    input_text = f'クライアント: {client}\nタグ: {", ".join(tags)}\n\n{body}'
    ai_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = ai_client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=800,
        messages=[{
            'role': 'user',
            'content': WORKMEMO_PROMPT.format(input=input_text)
        }]
    )
    raw = msg.content[0].text.strip()
    # JSONブロックがあれば抽出
    json_m = re.search(r'\{.*\}', raw, re.DOTALL)
    if json_m:
        try:
            return json.loads(json_m.group(0))
        except Exception:
            pass
    return {'project': 'メモ', 'summary': body[:80], 'content': body,
            'next_actions': '', 'output_candidate': False}


def save_workmemo(date_str: str, client: str, tags: list, parsed: dict, body: str, file_urls: list) -> Path:
    """ワークメモを WORK_DIR に保存"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    project = parsed.get('project', 'memo')
    slug = re.sub(r'[^\w\-]', '-', project.lower())[:30].strip('-')
    filename = f'{date_str}-{client.lower()}-{slug}.md'
    filepath = WORK_DIR / filename
    # 重複回避
    counter = 1
    base = str(filepath).replace('.md', '')
    while filepath.exists():
        filepath = Path(f'{base}-{counter}.md')
        counter += 1

    tags_yaml = '\n'.join([f'  - {t}' for t in tags]) if tags else '  []'
    urls_md = '\n'.join([f'- {u}' for u in file_urls]) if file_urls else 'なし'
    output_candidate = parsed.get('output_candidate', False)
    summary = parsed.get('summary', '')
    content = parsed.get('content', body)
    next_actions = parsed.get('next_actions', '')

    frontmatter = f"""---
date: {date_str}
type: context
client: {client}
project: {project}
tags:
{tags_yaml}
output_candidate: {"true" if output_candidate else "false"}
---"""

    body_md = f"""# {project}

{summary}

## メモ

{content}
"""
    if next_actions and next_actions.strip():
        body_md += f'\n## ネクストアクション\n\n{next_actions}\n'
    if file_urls:
        body_md += f'\n## 添付ファイル\n\n{urls_md}\n'

    filepath.write_text(frontmatter + '\n\n' + body_md, encoding='utf-8')
    return filepath


def get_file(path: str):
    """ファイル内容と sha を取得"""
    url = f'{GH_API}/repos/{GITHUB_REPO}/contents/{path}'
    res = requests.get(url, headers=HEADERS)
    res.raise_for_status()
    data = res.json()
    content = base64.b64decode(data['content']).decode('utf-8')
    return content, data['sha']


def delete_file(path: str, sha: str, label: str):
    """処理済みファイルを削除"""
    url = f'{GH_API}/repos/{GITHUB_REPO}/contents/{path}'
    payload = {
        'message': f'diary: processed {label}',
        'sha': sha,
    }
    res = requests.delete(url, headers=HEADERS, json=payload)
    res.raise_for_status()


def _extract_page_text(html: str, base_url: str = '') -> tuple[str, str, str, list[str]]:
    """HTMLからタイトル・description・本文・内部リンクを抽出"""
    from urllib.parse import urljoin, urlparse

    title_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    title = re.sub(r'\s+', ' ', title_m.group(1)).strip() if title_m else ''

    desc_m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
                       html, re.IGNORECASE)
    desc = desc_m.group(1).strip() if desc_m else ''
    if not desc:
        og_m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
                         html, re.IGNORECASE)
        desc = og_m.group(1).strip() if og_m else ''

    body = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r'<[^>]+>', ' ', body)
    body = re.sub(r'\s+', ' ', body).strip()[:2000]

    # 同一ドメインの内部リンクを抽出（最大5件）
    links = []
    if base_url:
        base_domain = urlparse(base_url).netloc
        for href in re.findall(r'<a[^>]+href=["\'](.*?)["\']', html, re.IGNORECASE):
            full = urljoin(base_url, href)
            parsed = urlparse(full)
            if parsed.netloc == base_domain and parsed.path not in ('/', '') and full != base_url:
                clean = f'{parsed.scheme}://{parsed.netloc}{parsed.path}'
                if clean not in links:
                    links.append(clean)
            if len(links) >= 5:
                break

    return title, desc, body, links


def fetch_url_data(url: str) -> tuple[str, str]:
    """URLのページ内容を取得し、タイトルとサマリーを返す (title, summary)"""
    ua = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, timeout=10, headers=ua)
        res.raise_for_status()
        title, desc, body, child_links = _extract_page_text(res.text, url)
    except Exception as e:
        return '', f'（URL取得失敗: {e}）'

    # 子ページも取得してコンテンツを補完
    child_texts = []
    for link in child_links:
        try:
            r = requests.get(link, timeout=8, headers=ua)
            r.raise_for_status()
            c_title, c_desc, c_body, _ = _extract_page_text(r.text)
            snippet = ' '.join(filter(None, [c_title, c_desc, c_body]))[:800]
            if snippet.strip():
                child_texts.append(f'[{link}]\n{snippet}')
        except Exception:
            pass

    context_parts = [f'タイトル: {title}', f'説明: {desc}', f'本文: {body}']
    if child_texts:
        context_parts.append('--- 内部ページ ---\n' + '\n\n'.join(child_texts))
    context = '\n'.join(filter(lambda x: x.split(': ', 1)[-1].strip(), context_parts))

    if not context.strip():
        return title, '（ページ内容を取得できませんでした）'

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=500,
        messages=[{
            'role': 'user',
            'content': URL_SUMMARY_PROMPT.format(content=context)
        }]
    )
    return title, msg.content[0].text.strip()


def fetch_url_summary(url: str) -> str:
    """後方互換ラッパー: サマリーのみ返す"""
    _, summary = fetch_url_data(url)
    return summary


def append_link_section(filepath: Path, urls: list, header: str = '## リンク') -> None:
    """URLを取得してサマリーとともに直接ファイルに追記する（Claudeを経由しない）"""
    parts = []
    for url in urls:
        print(f'    URLサマリー取得中: {url}')
        summary = fetch_url_summary(url)
        parts.append(f'🔗 [{url}]({url})\n\n{summary}')
    if not parts:
        return
    section = '\n\n'.join(parts)
    content = filepath.read_text(encoding='utf-8')
    heading = header.lstrip('#').strip()
    if heading in content:
        content = content.rstrip() + f'\n\n{section}\n'
    else:
        content = content.rstrip() + f'\n\n{header}\n\n{section}\n'
    filepath.write_text(content, encoding='utf-8')


def append_link_section_from_data(filepath: Path, url_data_list: list, header: str = '## リンク') -> None:
    """事前取得済みのURLデータ [{url, title, summary}] をファイルに追記"""
    parts = []
    for d in url_data_list:
        parts.append(f'🔗 [{d["url"]}]({d["url"]})\n\n{d["summary"]}')
    if not parts:
        return
    section = '\n\n'.join(parts)
    content = filepath.read_text(encoding='utf-8')
    heading = header.lstrip('#').strip()
    if heading in content:
        content = content.rstrip() + f'\n\n{section}\n'
    else:
        content = content.rstrip() + f'\n\n{header}\n\n{section}\n'
    filepath.write_text(content, encoding='utf-8')


def extract_comment_before_url(text: str, url: str) -> str:
    """URLより前のテキストからコメントを抽出（タイムスタンプ・タグ行を除く）"""
    url_pos = text.find(url)
    if url_pos < 0:
        return ''
    before = text[:url_pos].strip()
    lines = before.splitlines()
    comment_lines = [l for l in lines if not re.match(r'^\[.*\]$', l.strip())]
    return '\n'.join(comment_lines).strip()


def classify_bookmark(title: str, summary: str) -> tuple[list, str]:
    """Claude でブックマークの topic と output_target を分類"""
    import json
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=100,
        messages=[{
            'role': 'user',
            'content': BOOKMARK_CLASSIFY_PROMPT.format(title=title, summary=summary)
        }]
    )
    raw = msg.content[0].text.strip()
    json_m = re.search(r'\{.*\}', raw, re.DOTALL)
    if json_m:
        try:
            d = json.loads(json_m.group(0))
            return d.get('topic', ['AI']), d.get('output_target', 'idea-pool')
        except Exception:
            pass
    return ['AI'], 'idea-pool'


def append_bookmark_entry(date_str: str, time_str: str, comment: str,
                          title: str, summary: str, url: str) -> None:
    """~/Documents/NeoBrain/bookmark/bookmark.md にエントリを追記（新しいものが上）"""
    BOOKMARK_DIR.mkdir(parents=True, exist_ok=True)
    dt = f'{date_str} {time_str}' if time_str else date_str

    topics, output_target = classify_bookmark(title, summary)
    topic_str = ', '.join(topics)

    meta = (
        f'```yaml\n'
        f'type: reference\n'
        f'topic: [{topic_str}]\n'
        f'output-target: {output_target}\n'
        f'status: wip\n'
        f'source: web\n'
        f'image-assets: 無\n'
        f'date-added: {date_str}\n'
        f'```'
    )
    lines = ['\n---\n', f'**{dt}**\n', meta]
    if comment:
        lines.append(f'\n**コメント:** {comment}')
    if title:
        lines.append(f'**タイトル:** {title}')
    lines.append(f'**URL:** [{url}]({url})')
    if summary:
        lines.append(f'\n{summary}')
    entry = '\n'.join(lines) + '\n'

    if BOOKMARK_FILE.exists():
        existing = BOOKMARK_FILE.read_text(encoding='utf-8')
        # ヘッダー行の直後に挿入（新しいものが上）
        header_end = existing.find('\n', existing.find('# Bookmarks'))
        if header_end < 0:
            content = existing.rstrip() + '\n' + entry
        else:
            content = existing[:header_end + 1] + entry + existing[header_end + 1:]
    else:
        content = '# Bookmarks\n' + entry
    BOOKMARK_FILE.write_text(content, encoding='utf-8')




def format_with_claude(date_str: str, raw_entries: str) -> dict:
    """Claude で日記テキストを構造化JSON に整形"""
    import json
    ai_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = ai_client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=1200,
        messages=[{
            'role': 'user',
            'content': JOURNAL_PROMPT.format(date=date_str, input=raw_entries)
        }]
    )
    raw = msg.content[0].text.strip()
    json_m = re.search(r'\{.*\}', raw, re.DOTALL)
    if json_m:
        try:
            return json.loads(json_m.group(0))
        except Exception:
            pass
    # フォールバック: 全文をmemoに
    return {'memo': raw_entries, 'tags': [], 'energy': 3, 'output_candidate': False,
            'routine': {}, 'happy': [], 'want': []}


def fill_diary_template(diary_path: Path, parsed: dict):
    """diary templateのルーチン行に値を埋め込む（__プレースホルダーのみ、上書き禁止）"""
    content = diary_path.read_text(encoding='utf-8')
    routine = parsed.get('routine', {})

    def v(val, default='—'):
        return val.strip() if val and val.strip() else default

    # 行1: 起床 __時 | 睡眠 __時間 | 運動 __
    if re.search(r'^起床 __時 \| 睡眠 __時間 \| 運動 __$', content, re.MULTILINE):
        row1 = f'起床 {v(routine.get("wakeup"))} | 睡眠 {v(routine.get("sleep"))} | 運動 {v(routine.get("exercise"))}'
        content = re.sub(r'^起床 __時 \| 睡眠 __時間 \| 運動 __$', row1, content, flags=re.MULTILINE)

    # 行2: 朝食 — | ランチ — | 夕食 — （テンプレートのデフォルト — のみ上書き）
    if re.search(r'^朝食 — \| ランチ — \| 夕食 —$', content, re.MULTILINE):
        row2 = f'朝食 {v(routine.get("breakfast"))} | ランチ {v(routine.get("lunch"))} | 夕食 {v(routine.get("dinner"))}'
        content = re.sub(r'^朝食 — \| ランチ — \| 夕食 —$', row2, content, flags=re.MULTILINE)

    # 行3: エネルギー 朝__ → 夜__
    if re.search(r'^エネルギー 朝__ → 夜__$', content, re.MULTILINE):
        row3 = f'エネルギー 朝{v(routine.get("energy_morning"))} → 夜{v(routine.get("energy_night"))}'
        content = re.sub(r'^エネルギー 朝__ → 夜__$', row3, content, flags=re.MULTILINE)

    diary_path.write_text(content, encoding='utf-8')


def append_to_memo_section(diary_path: Path, memo_text: str):
    """音声メモセクションに追記（上書きなし）"""
    if not memo_text.strip():
        return
    content = diary_path.read_text(encoding='utf-8')
    # 旧セクション名との後方互換
    header = '## メモ' if '## メモ' in content else '## 口頭メモ'
    idx = content.find(header)
    if idx < 0:
        content = content.rstrip() + f'\n\n## メモ\n\n{memo_text.strip()}\n'
        diary_path.write_text(content, encoding='utf-8')
        return
    insert_pos = idx + len(header)
    next_section = content.find('\n## ', insert_pos)
    new_text = f'\n{memo_text.strip()}'
    if next_section < 0:
        content = content.rstrip() + new_text + '\n'
    else:
        content = content[:next_section] + new_text + content[next_section:]
    diary_path.write_text(content, encoding='utf-8')


def update_diary_frontmatter(diary_path: Path, parsed: dict):
    """frontmatter の tags/energy/output_candidate を更新（tags は既存とマージ）"""
    content = diary_path.read_text(encoding='utf-8')
    new_tags = parsed.get('tags', [])
    new_energy = parsed.get('energy', 3)
    new_oc = parsed.get('output_candidate', False)

    if new_tags:
        # 既存タグを抽出してマージ
        existing = re.findall(r'^\s{2}-\s+(.+)$', content, re.MULTILINE)
        merged = sorted(set(existing) | set(new_tags))
        tags_yaml = '\n'.join([f'  - {t}' for t in merged])
        # tags: [] と tags:\n  - xxx の両パターンを置換
        content = re.sub(
            r'tags:\s*(?:\[\]|\n(?:\s{2}-\s+.+\n?)*)',
            f'tags:\n{tags_yaml}\n',
            content, count=1
        )
    if new_energy and new_energy != 3:
        content = re.sub(r'^energy:\s*3\s*$', f'energy: {new_energy}', content,
                         count=1, flags=re.MULTILINE)
    if new_oc:
        content = re.sub(r'^output_candidate:\s*false\s*$', 'output_candidate: true',
                         content, count=1, flags=re.MULTILINE)
    diary_path.write_text(content, encoding='utf-8')


def process_diary_entry(date_str: str, parsed: dict) -> Path:
    """diary fileを作成/更新: テンプレート埋め込み + memo追記"""
    diary_path = ensure_diary(date_str)
    fill_diary_template(diary_path, parsed)
    append_to_memo_section(diary_path, parsed.get('memo', ''))
    update_diary_frontmatter(diary_path, parsed)
    return diary_path


def main():
    DIARY_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    BOOKMARK_DIR.mkdir(parents=True, exist_ok=True)

    # ── 日記 inbox 処理 ──
    files = list_inbox_files()
    if not files:
        print('inbox は空です。')
    else:
        by_date = defaultdict(list)
        for f in files:
            m = FILENAME_RE.match(f['name'])
            if m:
                by_date[m.group(1)].append(f)

        for date_str, date_files in sorted(by_date.items()):
            print(f'[diary] 処理中: {date_str} ({len(date_files)}件) ...')
            all_entries = []
            file_shas = []
            bookmark_candidates = []  # {url, time_str, comment}
            for f in date_files:
                raw_content, sha = get_file(f['path'])
                all_entries.append(raw_content.strip())
                file_shas.append((f['path'], sha, f['name']))
                # ブックマーク候補: ファイル単位でURLとコメントを抽出
                file_urls = URL_RE.findall(raw_content)
                if file_urls:
                    time_match = re.search(r'^\[(\d{2}:\d{2})\]', raw_content)
                    t_str = time_match.group(1) if time_match else ''
                    for url in file_urls:
                        comment = extract_comment_before_url(raw_content, url)
                        bookmark_candidates.append({'url': url, 'time_str': t_str, 'comment': comment})

            combined = '\n\n'.join(all_entries)
            # URLはClaudeに渡さず、後で直接追記する
            inline_urls = URL_RE.findall(combined)
            parsed = format_with_claude(date_str, combined)
            diary_path = process_diary_entry(date_str, parsed)
            print(f'  → {diary_path} を更新しました')
            if inline_urls:
                # URLデータを一度だけ取得（日記・ブックマーク共用）
                url_data_map = {}
                for url in inline_urls:
                    print(f'    URLデータ取得中: {url}')
                    title, summary = fetch_url_data(url)
                    url_data_map[url] = {'url': url, 'title': title, 'summary': summary}
                # 日記ファイルにリンクセクション追記
                append_link_section_from_data(diary_path, list(url_data_map.values()), '## リンク')
                # ブックマークファイルに追記
                for cand in bookmark_candidates:
                    d = url_data_map.get(cand['url'])
                    if d:
                        append_bookmark_entry(date_str, cand['time_str'], cand['comment'],
                                              d['title'], d['summary'], cand['url'])
                        print(f'    → bookmark.md に追記しました: {cand["url"]}')

            for path, sha, name in file_shas:
                delete_file(path, sha, date_str)
                print(f'  → inbox/{name} を削除しました')

    # ── 画像処理 ──
    image_files = list_inbox_images()
    if not image_files:
        print('inbox/images は空です。')
    else:
        by_date = defaultdict(list)
        for f in image_files:
            m = IMAGE_RE.match(f['name'])
            if m:
                by_date[m.group(1)].append(f)

        for date_str, imgs in sorted(by_date.items()):
            print(f'[images] 処理中: {date_str} ({len(imgs)}枚) ...')
            downloaded = []
            for f in imgs:
                local_path = download_image(f['path'], f['name'], date_str)
                downloaded.append(local_path)
                print(f'  → {local_path} に保存しました')

            # 対応する日記ファイルに写真セクションを追記
            diary_path = ensure_diary(date_str)
            append_images_to_diary(diary_path, downloaded)
            print(f'  → {diary_path} に写真セクションを追記しました')

            # 処理済み画像をGitHubから削除（一覧取得時のSHAを使用）
            for f in imgs:
                delete_file(f['path'], f['sha'], f['name'])
                print(f'  → inbox/images/{f["name"]} を削除しました')

    # ── ワークメモ処理 ──
    memo_files = list_workmemo_files()
    if not memo_files:
        print('workmemo は空です。')
    else:
        for f in memo_files:
            m = FILENAME_RE.match(f['name'])
            if not m:
                continue
            date_str = m.group(1)
            print(f'[workmemo] 処理中: {f["name"]} ...')
            raw_content, sha = get_file(f['path'])
            meta = parse_workmemo_headers(raw_content)

            # URLはClaudeに渡さず、後で直接追記する
            inline_urls = URL_RE.findall(meta['body'])

            # Claude で構造化
            parsed = format_workmemo_with_claude(meta['client'], meta['tags'], meta['body'])

            # ファイル保存
            work_path = save_workmemo(date_str, meta['client'], meta['tags'], parsed,
                                      meta['body'], meta['file_urls'])
            print(f'  → {work_path} に保存しました')
            if inline_urls:
                append_link_section(work_path, inline_urls, '## 参考リンク')

            # 処理済みファイルを削除
            delete_file(f['path'], sha, f['name'])
            print(f'  → workmemo/{f["name"]} を削除しました')

    print('完了。')


if __name__ == '__main__':
    main()
