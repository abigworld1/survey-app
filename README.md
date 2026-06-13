# survey-app

MAPF / MAPD / 倉庫ロボティクス向けの **論文サーベイ自動要約サイト**。
登録キーワードに沿って関連論文を毎日 *k* 本取得し、**日本語の落合フォーマット**で要約して、
静的 HTML を `https://abigworld1.github.io/survey-app/<username>/<paper-id>.html` に生成します。

```
取得(sources) → 名寄せ(dedup) → 既出除外(seen) → 要約(ローカルLLM) → HTML生成 → git push → GitHub Pages
```

## 構成

| 要素 | 内容 |
|---|---|
| フロント | 生成された静的 HTML（`index.html`, `<username>/...`）。秘密情報なし |
| 取得 | arXiv / Semantic Scholar / OpenAlex / DBLP（`pipeline/sources/`） |
| 要約 | **ローカル vLLM（OpenAI互換）を直接呼ぶ**。`pipeline/summarize.py` |
| 実行 | GPUサーバー上の systemd timer（`deploy/`）。vLLM と同じ Docker ネットワーク |
| 状態 | `data/seen.json`（既出論文の記録。再要約を防止） |

要約エンジンは研究室の **Open WebUI(:3000) の背後にある vLLM**（Gemma 4 26B-A4B FP8）。
Open WebUI 経由ではなく **vLLM を直接** 叩きます（OpenAI互換 `http://vllm:8000/v1`、LAN内）。

## ローカルでの動作確認（ネット/LLM 不要）

```bash
cd survey-app
python -m pip install -r requirements.txt
python -m pipeline.run --offline     # サンプル論文＋スタブ要約で HTML を生成
# 生成物: index.html, mapf/index.html, mapf/<id>.html ... をブラウザで開く
```

その他のモード:
```bash
python -m pipeline.run --stub        # 実際に論文取得、要約だけスタブ（ネット必要）
python -m pipeline.run --dry-run     # 取得・要約するが書き込み/seen更新はしない
python -m pipeline.run               # 本番（vLLM で要約）
```

## モデルIDの決定順（`pipeline/summarize.py`）

1. 環境変数 `LLM_MODEL` があれば最優先
2. 無ければ起動時に `GET {LLM_BASE_URL}/models` の **先頭 id を自動採用**
3. それも取れなければ `RedHatAI/gemma-4-26B-A4B-it-FP8-Dynamic`

関連環境変数: `LLM_BASE_URL`（既定 `http://vllm:8000/v1`）, `LLM_API_KEY`（既定 `dummy`）,
`S2_API_KEY`（任意。Semantic Scholar のレート緩和）。

## 購読の追加

`subscriptions.yml` を編集（自分専用なので直接編集で可）:
```yaml
subscriptions:
  - username: mapf            # 公開URLになる。実名・PIIは入れない
    keywords: ["multi-agent path finding", "MAPF"]
    k: 5                      # 1日あたり最大ページ数（上限20）
    sources: [arxiv, semanticscholar, openalex]
```

## デプロイ（GPUサーバー）

1. **GitHubに `abigworld1/survey-app` を作成** → Pages を有効化（Settings → Pages → Branch: `main` / root）。
2. **fine-grained PAT** を発行（対象: survey-app のみ / Contents: Read and write）。
   `.env` に `GIT_REMOTE=https://<PAT>@github.com/abigworld1/survey-app.git` を置く（コミットしない）。
3. `deploy/docker-compose.snippet.yml` を既存の vLLM/Open WebUI の compose に追記
   （`survey-app` を **vLLM と同じネットワーク**に載せる）。
4. systemd を設定:
   ```bash
   sudo cp deploy/survey.service deploy/survey.timer /etc/systemd/system/
   sudo systemctl enable --now survey.timer
   systemctl list-timers survey.timer
   ```
5. `abigworld1.github.io` の `research.html` に survey-app へのリンクを1行追加。

## 安全設計（要点）

- **秘密情報は静的サイトに出さない**（LLMはLAN内、PATは `.env`/timer のみ）。
- 出力ファイル名は **安定ID**（arXiv ID / DOIハッシュ）。`username`/ID は slugify 済み。
- 論文の abstract/keywords は **信頼できないデータ**として扱い、要約出力は **HTMLエスケープ**
  （プロンプトインジェクション対策）。
- 各ページに **「AI自動生成・要約（誤りの可能性あり）」+ 原典リンク** を明記。
- 暴走/肥大化防止の上限: `MAX_K=20`, `FETCH_CAP=40`, `MAX_PAGES_PER_RUN=100`（`pipeline/run.py`）。
- 要約のみを掲載し、**論文全文は転載しない**（著作権）。
