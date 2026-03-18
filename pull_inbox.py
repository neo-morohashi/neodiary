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

load_dotenv(Path(__file__).parent / '.env')

GITHUB_TOKEN  = os.environ['GITHUB_TOKEN']
GITHUB_REPO   = os.environ['GITHUB_REPO']
DIARY_DIR     = Path.home() / 'Documents/NeoBrain/diary'
WORK_DIR      = Path.home() / 'Documents/NeoBrain/context/work'
ANTHROPIC_KEY = os.environ['ANTHROPIC_API_KEY']

GH_API = 'https://api.github.com'
HEADERS = {
    'Authorization': f'Bearer {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github+json',
}

# ファイル名パターン: YYYY-MM-DD_HHMMSS.txt
FILENAME_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})_\d{6}\.txt$')
URL_RE = re.compile(r'https?://[^\s\)\]>]+')

JOURNAL_PROMPT = """\
あなたは日記アシスタントです。以下の入力を整形して追記用のMarkdownブロックだけを返してください。説明文・コードブロック記法は不要です。

## ユーザーの入力（複数エントリがある場合はまとめて処理）
{input}

## 出力フォーマット（厳守）
以下の形式で、追記するブロックのみを返す。既存ファイルの内容は含めない。

### 📝 モバイルメモ ({date})

**口頭メモ**
（出来事・感想・食事・運動など箇条書き）

**タグ候補:** (work/mercer, personal/triathlon など該当するもの)
**energy:** (1〜5)
**output_candidate:** (true/false)
"""

URL_SUMMARY_PROMPT = """\
以下のWebページの内容を日本語で3〜5行に要約してください。説明文は不要で、要約本文のみ返してください。

{content}
"""

TEMPLATE = """\
---
date: {date}
type: diary
tags: []
energy: 3
output_candidate: false
---

# {date}

## 口頭メモ

## 📋 今日のルーチン
- [ ] 起床: __時
- [ ] 睡眠: __時間
- [ ] 運動: __km / __分
- [ ] 朝食: __
- [ ] ランチ: __
- [ ] 夕食: __
- [ ] プロジェクト作業: __時間
- [ ] エネルギー（朝）:
- [ ] エネルギー（夜）:

## 😊 3つの嬉しいこと
1.
2.
3.

## 🎯 3つのやりたいこと
1.
2.
3.
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


def fetch_url_summary(url: str) -> str:
    """URLのページ内容を取得し、1階層内部リンクも辿ってClaudeで要約"""
    ua = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, timeout=10, headers=ua)
        res.raise_for_status()
        title, desc, body, child_links = _extract_page_text(res.text, url)
    except Exception as e:
        return f'（URL取得失敗: {e}）'

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
        return '（ページ内容を取得できませんでした）'

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=500,
        messages=[{
            'role': 'user',
            'content': URL_SUMMARY_PROMPT.format(content=context)
        }]
    )
    return msg.content[0].text.strip()


def enrich_with_url_summaries(text: str) -> str:
    """テキスト中のURLを検出してサマリーを付加"""
    urls = URL_RE.findall(text)
    if not urls:
        return text

    summaries = []
    for url in urls:
        print(f'    URLサマリー取得中: {url}')
        summary = fetch_url_summary(url)
        summaries.append(f'🔗 {url}\n> {summary}')

    return text + '\n\n' + '\n\n'.join(summaries)


def format_with_claude(date_str: str, raw_entries: str) -> str:
    """Claude で追記用ブロックに整形"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=1000,
        messages=[{
            'role': 'user',
            'content': JOURNAL_PROMPT.format(date=date_str, input=raw_entries)
        }]
    )
    return msg.content[0].text.strip()


def append_to_diary(date_str: str, block: str) -> Path:
    """既存ファイルがあれば追記、なければテンプレートで新規作成して追記"""
    diary_path = DIARY_DIR / f'{date_str}.md'
    if not diary_path.exists():
        diary_path.write_text(TEMPLATE.format(date=date_str), encoding='utf-8')
    with diary_path.open('a', encoding='utf-8') as f:
        f.write(f'\n{block}\n')
    return diary_path


def main():
    DIARY_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

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
            for f in date_files:
                raw_content, sha = get_file(f['path'])
                all_entries.append(raw_content.strip())
                file_shas.append((f['path'], sha, f['name']))

            combined = '\n\n'.join(all_entries)
            enriched = enrich_with_url_summaries(combined)
            block = format_with_claude(date_str, enriched)
            diary_path = append_to_diary(date_str, block)
            print(f'  → {diary_path} に追記しました')

            for path, sha, name in file_shas:
                delete_file(path, sha, date_str)
                print(f'  → inbox/{name} を削除しました')

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

            # URLサマリーを本文に付加
            enriched_body = enrich_with_url_summaries(meta['body'])
            meta['body'] = enriched_body

            # Claude で構造化
            parsed = format_workmemo_with_claude(meta['client'], meta['tags'], meta['body'])

            # ファイル保存
            work_path = save_workmemo(date_str, meta['client'], meta['tags'], parsed,
                                      meta['body'], meta['file_urls'])
            print(f'  → {work_path} に保存しました')

            # 処理済みファイルを削除
            delete_file(f['path'], sha, f['name'])
            print(f'  → workmemo/{f["name"]} を削除しました')

    print('完了。')


if __name__ == '__main__':
    main()
