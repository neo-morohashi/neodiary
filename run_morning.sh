#!/bin/bash
# 朝6時専用: データ同期のみ（日次ダイジェストメールは 2026-08-02 に停止）
cd "/Users/neo/AI Dev/diary-web"
bash run_pull.sh
# 停止済み（Neo の依頼で 2026-08-02 に無効化。復活させる場合は下行のコメントを外す）
# "/Users/neo/AI Dev/diary-web/.venv/bin/python3" daily_digest.py --send neo@mineo.com,neo.morohashi@mercer.com
