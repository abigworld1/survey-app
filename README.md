# survey-app

[私](https://abigworld1.github.io/)の研究に関連する論文を、**毎日自動で**／**任意のタイミングで手動で**、
日本語で要約して静的サイトに公開するツールです。論文をセクション単位で精読する**多段要約**を行い、
落合フォーマット＋セクション別の詳細要約を生成します。数式は MathJax で組版されます。

公開先: **https://abigworld1.github.io/survey-app/**

```
取得(arXiv/Semantic Scholar/OpenAlex) → 名寄せ → 重要論文1本＋新着論文1本を採用 → 本文取得(arXiv HTML/OA PDF)
  → セクション分割 → 各セクションをLLM要約 → 落合フォーマット合成 → HTML生成 → git push → GitHub Pages
```

要約エンジンは研究室サーバー（sankaku01, LAN内）の **vLLM（Gemma 4 26B-A4B FP8, OpenAI互換）** を
`http://localhost:8000/v1` で直接呼びます。要約のみを公開し、論文全文は転載しません。

---

## 使い方

### 1. 毎日の自動更新（cron）

`subscriptions.yml` の各分野について、毎日 *k* 本の関連論文を要約して公開します。
既定の `k: 2` では、被引用数を主指標にした重要論文を1本、投稿日が新しい新着論文を1本採用します。
sankaku01 では user cron に登録済みで、追加操作は不要です（毎朝 6:00）。

手動で1回まわす場合:
```bash
cd ~/survey-app
LLM_BASE_URL=http://localhost:8000/v1 LLM_API_KEY=dummy .venv/bin/python -m pipeline.run
git add -A && git commit -m "update" && git push origin main
```

主なオプション:
| オプション | 意味 |
|---|---|
| （なし） | 本番。各分野で新着 *k* 本を要約・生成 |
| `--reset` | 既存ページと `seen` を消してから作り直す（分野やキーワードを変えた後の再構築） |
| `--offline` | ネット/LLM 不要。サンプル＋スタブ要約で動作確認 |
| `--stub` | 論文は実際に取得し、要約だけスタブ |
| `--dry-run` | 取得・要約はするが、ファイル生成と `seen` 更新をしない |
| `--render-indexes-only` | 取得・要約をせず、既存の `seen` から一覧HTMLだけ再生成 |
| `--limit N` | 今回生成する総ページ数の上限 |

品質管理:
- 自動更新では、関連キーワードが弱い候補、本文が取れずアブストラクトのみの候補、要約が短すぎる候補、不明項目が多い候補は公開せず、次候補を試します。
- 実行ごとに `data/runs/YYYY-MM-DD.json` と `data/runs/YYYY-MM-DD.html` に、追加・スキップ・取得失敗・LLM失敗の概要を残します。
- 一覧ページでは、本文/アブストラクトの区別、被引用数、関連度、読む価値、選定枠（重要論文/新着論文）を表示します。ブラウザ上だけで「既読」「あとで」「お気に入り」「非表示」も管理できます。

### 2. 読みたい論文を手動で追加（`add_paper`）

日次更新とは独立に、**任意の論文を1枚だけ**要約・公開できます。

```bash
cd ~/survey-app
# arXiv ID（最も手軽。HTML本文を使うので PyMuPDF 不要）
LLM_BASE_URL=http://localhost:8000/v1 LLM_API_KEY=dummy \
  .venv/bin/python -m pipeline.add_paper --arxiv 2606.12345

# 手元の PDF（PyMuPDF で本文抽出）
LLM_BASE_URL=http://localhost:8000/v1 LLM_API_KEY=dummy \
  .venv/bin/python -m pipeline.add_paper --pdf ~/papers/foo.pdf --title "論文タイトル"

# PDF の URL
LLM_BASE_URL=http://localhost:8000/v1 LLM_API_KEY=dummy \
  .venv/bin/python -m pipeline.add_paper --url https://example.org/paper.pdf

# 生成後に公開
git add -A && git commit -m "add paper" && git push origin main
```

**追加先の分野**（指定しなければ「個別に読んだ論文」）:
| 指定 | 追加先 |
|---|---|
| （なし） | `reading`（個別に読んだ論文） |
| `--mapf` | 自動倉庫の MAPF/MAPD 分野（`mapf-mapd-warehouse`） |
| `--rag` | 文書構造解析・RAG 分野（`doc-structure-rag`） |
| `--field <slug>` | 任意の分野スラッグ |

補足:
- 入力の指定（`--arxiv` / `--pdf` / `--url`）はいずれか1つ必須。追加先（`--mapf` / `--rag` / `--field`）は排他。
- PDF からタイトルが取れないときは `--title` で指定（無指定なら PDF メタデータや先頭行から推定）。
- `--pdf` / `--url` は **PyMuPDF** が必要（導入済み）。`--arxiv` は不要。
- `reading` 分野は日次 cron が触らないので、勝手に論文が増えることはありません。

### 3. 生成済みページに追加質問を追記（`ask_paper`）

生成済みHTMLの内容をGemmaに読ませ、追加質問への回答をページ末尾の「追加質問」欄に対話形式で追記できます。

```bash
cd ~/survey-app

./ask.py \
  --paper "Priority Inheritance with Backtracking for Iterative Multi-agent Path Finding" \
  --question "PIBTはどの条件で完全性を保証している？"
```

`ask.py` は `LLM_BASE_URL=http://localhost:8000/v1` と `LLM_API_KEY=dummy` を既定で使い、対象HTMLを更新した後に
`git add` / `git commit` / `git push origin main` まで自動で行います。

主な指定:
| 指定 | 意味 |
|---|---|
| `--paper "<title>"` | 論文タイトルで検索して対象HTMLを選ぶ |
| `--mapf` / `--rag` / `--reading` | 対象分野 |
| `--field <slug>` | 任意の分野スラッグ |
| `--question "..."` | 追記する質問。複数指定可 |
| `--dry-run` | 回答だけ表示し、HTMLを書き換えない |
| `--stub` | LLMを呼ばずスタブ回答で動作確認 |
| `--no-push` | commitまで行い、pushしない |
| `--message "..."` | commit messageを指定 |

回答は対象HTMLに含まれる要約・セクション要約・既存の追加質問だけを根拠にします。根拠がない場合は不明と答えるようにしています。
細かく対象を指定したい場合は、従来通り `python -m pipeline.ask_paper --file ...` も使えます。

### 4. 分野・キーワードの設定（`subscriptions.yml`）

自分専用なので直接編集します。`username` が公開URLのスラッグ、`label` が表示名です。
```yaml
subscriptions:
  - username: mapf-mapd-warehouse          # /survey-app/<username>/ になる（ASCII・PII不可）
    label: 自動倉庫におけるマルチエージェント計画・タスク形成最適化   # 画面に出る分野名
    keywords:                              # OR 検索 ＋ 関連度判定に使う
      - "Multi-Agent Path Finding"
      - "MAPF"
      - "Warehouse Robotics"
    k: 2                                   # 1日あたり最大ページ数（上限20）
    sources: [arxiv, semanticscholar, openalex]

  - username: reading                      # 手動追加専用（add_paper の既定の置き場）
    label: 個別に読んだ論文
    manual: true                           # 日次 cron はこの分野を自動取得しない
```
分野やキーワードを変えたら `--reset` で作り直すと綺麗です。

### 5. ローカルでの動作確認（ネット/LLM 不要）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pipeline.run --offline   # サンプル＋スタブで HTML を生成 → ブラウザで確認
```

---

## 仕組み（要約の流れ）

1. **取得**: 各 `sources` から候補を集める（arXiv は新着順、他はキーワード検索）。
2. **名寄せ**: DOI / arXiv ID / 正規化タイトルで重複排除（`pipeline/dedup.py`）。
3. **採用**: `k: 2` では重要論文1本＋新着論文1本を採用。
   重要論文は `関連度 → 被引用数 → 本文の取りやすさ → 新しさ` の順、新着論文は `関連度 → 本文の取りやすさ → 新しさ` の順。
   関連度＝キーワードのタイトル一致(×3)＋アブストラクト一致(×1)。略語は単語境界判定（`RAG`が`storage`に誤マッチしない）。
   適合0の論文は除外し、*k* 本に満たない時は次候補を試す。
4. **本文取得**: arXiv HTML を優先（`arxiv.org/html/<id>`）。無ければ OA PDF（Unpaywall/OpenAlex → PyMuPDF）。取れなければ abstract。
   自動更新では abstract のみの候補も低品質ページ防止のため公開せず、次候補を試す。
5. **多段要約**: 本文を主要セクション（Introduction / Methods / Experiments …）に分割し、
   各セクションを個別に詳しく要約 → それらを根拠に落合フォーマット5項目を合成（`pipeline/summarize.py`）。
6. **品質判定・読む価値評価**: 要約後に短すぎる要約や「提供された情報からは不明」が多い要約を除外し、LLMで読む価値（1〜5）を評価。
7. **生成**: 各ページに「落合5項目＋セクション別の詳細要約＋選定理由＋情報源・原典リンク・AI自動生成の注記」を出力。数式は MathJax で描画。

---

## 環境変数

| 変数 | 既定 | 用途 |
|---|---|---|
| `LLM_BASE_URL` | `http://vllm:8000/v1` | vLLM の OpenAI互換エンドポイント（sankaku01 は `http://localhost:8000/v1`） |
| `LLM_API_KEY` | `dummy` | vLLM 用（LAN内なのでダミー可） |
| `LLM_MODEL` | （未設定） | 指定すれば最優先。未設定なら `/models` の先頭 id を自動採用、それも無ければ `RedHatAI/gemma-4-26B-A4B-it-FP8-Dynamic` |
| `S2_API_KEY` | （未設定） | 任意。Semantic Scholar のレート制限(429)緩和 |

秘密情報は `.env`（gitignore 済み）に置きます。`deploy/run-daily.sh` が起動時に読み込みます:
```bash
# ~/survey-app/.env （コミットされない）
S2_API_KEY=xxxxxxxx
```

---

## デプロイ / 運用（sankaku01）

Docker も sudo も使いません（vLLM はサーバー上で稼働中、`localhost:8000` で到達可能）。

1. **GitHub Pages**: `abigworld1/survey-app` の Settings → Pages → *Deploy from branch* → `main` / root。
   Jekyll を無効化する `.nojekyll` をコミット済み。
2. **venv**: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`（依存は PyYAML と PyMuPDF）。
3. **push 認証**: fine-grained PAT（survey-app のみ / Contents: Read and write）。初回 `git push` 時に
   ユーザー名 `abigworld1`／パスワードに PAT を入力（`git config credential.helper store` で以後は非対話）。
4. **日次自動化（user cron, sudo不要）**:
   ```bash
   crontab -e
   # 毎日 06:00（サーバーTZ）:
   0 6 * * * /bin/bash /home/hirayama/survey-app/deploy/run-daily.sh >> /home/hirayama/survey-app/cron.log 2>&1
   ```
   `deploy/run-daily.sh` が「`.env`読込 → `pipeline.run` → 変更があれば commit & push」を行います。
5. `abigworld1.github.io` の `research.html` に survey-app へのリンクを設置済み。

> `deploy/` 配下の Docker/systemd 用ファイルは未使用（参考用）。実運用は上記の venv＋user cron です。

---

## 安全設計（要点）

- **秘密情報は静的サイトに出さない**（LLM は LAN内 localhost、PAT は `.env`/認証ストアのみ）。
- 出力ファイル名は **安定ID**（arXiv ID / DOIハッシュ / タイトルスラッグ）。`username`/ID は slugify 済みでパストラバーサル不可。
- 論文の本文/キーワードは **信頼できないデータ**として扱い（プロンプトインジェクション対策）、出力は HTML エスケープ。
- 各ページに **「AI自動生成・要約（誤りの可能性あり）」＋ 原典リンク** を明記。
- 暴走/肥大化防止の上限: `MAX_K=20`, `FETCH_CAP=40`, `MAX_PAGES_PER_RUN=100`（`pipeline/run.py`）。
- **要約のみを掲載し、論文全文は転載しない**（著作権）。ペイウォールはバイパスしない（arXiv・OA・購読範囲のみ）。
</content>
