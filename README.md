# survey-mapf — MAPF Paper Survey

[論文サーベイサイト](https://abigworld1.github.io/survey-mapf/)

GitHub Actionsで毎日 **06:00 JST** にMAPF論文を検索し、未処理の論文を **最大2本**選びます。各論文についてGitHub Copilot CLIで日本語版と英語版を同時に要約し、**日本語2ページ＋英語2ページの最大4ページ**をGitHub Pagesへ公開します。自前サーバ、vLLM、OpenAI API、OpenAI API key、長期PATは不要です。追加の有料APIやクラウドへのフォールバックはありません。

## Architecture

```text
GitHub Actions (06:00 JST / 21:00 UTC)
    |
    +-- MAPF paper discovery (arXiv / Semantic Scholar / OpenAlex)
    |      +-- data/seen.json: DOI / arXiv ID / title deduplication
    |      +-- up to 2 unprocessed papers
    |
    +-- paper text extraction (arXiv HTML / ar5iv / OA PDF)
    |
    +-- GitHub Copilot CLI
    |      +-- Japanese + English article JSON
    |      +-- bilingual factual review and corrected JSON
    |
    +-- existing HTML templates + atomic history checkpoint
    |
    +-- validated recovery artifact
    |
    +-- commit / push articles and metadata to main
    |
    +-- static site generation
    |
    +-- GitHub Pages (official Pages Actions)
```

検索・本文抽出・名寄せ・落合フォーマット・ダークテーマ・ブラウザの既読/あとで/お気に入り機能・既存の日本語記事URLを引き継いでいます。新規取得はMAPFと、従来の検索設定に含まれるMAPD/lifelong MAPFのみです。一般的なrobotics、LLM、RAG、multi-agent reinforcement learningへ対象を広げません。

トップ画面と購読設定はMAPF分野だけです。旧 `doc-structure-rag/` と `reading/` はナビゲーション・自動取得・新着一覧から削除済みです。ただし、過去に共有した直リンクを壊さないため、既存の静的HTMLだけは互換コンテンツとしてPages artifactへ残します。

## GitHubで最初に設定すること

1. この変更を `main` へ反映してください。定期実行はデフォルトブランチのworkflowが対象です。このworkflowの公開対象は `main` です。
2. **Settings → Pages → Build and deployment → Source → GitHub Actions** を選択してください。以前の `Deploy from a branch / main / root` から変更します。URLは `https://abigworld1.github.io/survey-mapf/` です。
3. **Settings → Actions → General** でActionsと公式 `actions/*` の利用を許可してください。workflowの `contents: write` がポリシーで禁止されていないこと、`main` の保護ルールが `github-actions[bot]` による記事commitを許すことを確認してください。保護ルールはこのコードから変更しません。
4. リポジトリ所有者のCopilotが有効で、CLIと選択モデルを利用できる必要があります。個人所有リポジトリでは組み込み `GITHUB_TOKEN` による利用分が所有者のCopilot枠へ計上されます。組織へ移す場合は **Allow use of Copilot CLI billed to the organization** ポリシーも確認してください。[GitHub公式の認証・課金説明](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/copilot-cli-in-github-actions)
5. `github-pages` environmentに承認やブランチ制限を設定している場合は、その設定に従って初回deployを許可してください。

**SecretsへのAPIキー/PAT登録は不要**です。要約ステップには `GITHUB_TOKEN: ${{ github.token }}` を渡します。[公式のActions設定例](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli-in-actions)

古いサーバのcronやsystemd timerは、このGit変更だけでは停止できません。二重更新を避けるため、旧環境の `survey-mapf/deploy/run-daily.sh` を呼ぶcron、または `survey.timer` が残っていれば、Actionsへの切替時にそのジョブだけを停止してください。新構成から旧サーバへ接続する処理はありません。

## 初回実行・手動実行

Actions → **Daily MAPF survey** → **Run workflow** → branch `main`:

- 初回確認: `dry_run=true`（既定）、`publish_only=false`。候補検索と本文取得を行い、対象タイトル・原典URL・本文量をログ表示します。Copilot呼び出し、記事保存、commit、Pages更新はありません。
- 既存サイトだけを先に公開: `dry_run=false`、`publish_only=true`。LLM・論文検索なしで一覧を生成し、既存記事をPagesへ公開します。
- 本番: `dry_run=false`、`publish_only=false`。検索→要約→履歴commit→Pages deployを実行します。

以後は `0 21 * * *` UTC（毎日06:00 JST）で動きます。GitHubの混雑で開始が遅れる場合があります。成功済みの記事は同日再実行でも要約せず、不足分だけを処理します。適切な未処理論文や本文がない日は0〜1本で正常終了できます。取得元がすべて障害の場合やLLM失敗はログ・終了状態で明示します。

CLIでも手動起動できます（`gh` に通常のリポジトリ操作用ログインがある場合）:

```bash
gh workflow run daily.yml --ref main -f dry_run=true -f publish_only=false
gh run list --workflow daily.yml
```

## Copilot呼び出しと利用枠

`npm install -g @github/copilot` で各本番実行時に最新版を導入します。Pythonの `CopilotLLM.generate()` がlist形式の引数でsubprocessを呼び出し、UTF-8のstdoutを解析します。検証時の最新版は **1.0.83** でした。主な引数:

```text
copilot -p PROMPT -s --no-color --no-ask-user
  --available-tools= --deny-tool=shell --deny-tool=write --deny-tool=url --disable-builtin-mcps
  --no-custom-instructions --no-auto-update --no-bash-env
  --no-remote --no-remote-export
```

`--available-tools=` でツールを非公開にし、許可フラグを使いません。リポジトリ外の一時cwdと一時 `COPILOT_HOME` を使い、保存済みの設定・会話や他社API用環境変数を引き継ぎません。論文内の命令に従わないことをプロンプトに明記しています。[公式CLIリファレンス](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)

- 1回目: 日本語・英語それぞれのタイトル、TLDR、落合5項目、背景・課題・技術・実験・結果・結論・限界・重要性・推奨読者を1つのJSONでまとめて生成。
- 2回目: 同じ原文抜粋と日英の初稿を照合し、数値、手法名、ベンチマーク、条件、断定、結論との矛盾と翻訳間の不一致を修正。両言語の全項目を再検証してから公開。
- 読む価値スコアはPythonの既存ヒューリスティックで計算。追加のLLM呼び出しはありません。
- 日英を別々に推論しないため、従来どおり **1論文2回、1回の実行で最大4回** のCLI起動です。過去記事を一括翻訳する処理はありません。失敗候補にも上限を適用し、JSON修復ループや日次スクリプト全体の自動再試行はありません。
- CLI起動数とGitHub側の課金単位/AI creditsは同じとは限りません。毎日2本を月末まで処理できるとFree枠で保証するものではありません。所有者の利用状況・モデル・現行プランに依存します。GitHubのCopilot使用状況と支出上限を確認し、追加利用を購入しない設定で運用してください。

**利用枠不足・認証拒否・network failure・timeout時は、その実行のCopilot処理を停止**します。不正JSONも再試行せず、その論文を未処理のまま残します。途中までの要約や事実確認に失敗した初稿は公開しません。先に正常完了した記事は保存・公開し、Actionsには失敗を表示します。他社APIやリモートLLMへ切り替えません。

## 長文・本文品質

既存のarXiv HTML → ar5iv → arXiv PDF → OA PDF取得を再利用します。日次公開は本文が取れた論文だけに限定し、abstractしかない場合はCopilotを呼ばず次候補を探します。

`pipeline/evidence.py` で参考文献・謝辞・ナビゲーション・フッター・HTMLタグ・重複文を除去し、method/experiments/results/conclusionを優先してセクションごとに抜粋します。長い節では冒頭だけでなく数値・結果・限界と末尾も残します。既定24,000文字、証拠全体60,000 UTF-8バイト、CLIプロンプト110,000バイトの上限を持ちます。要約の根拠は選択した本文抜粋であり、全文すべての事実確認ではありません。

JSONはコードフェンス付きでも読み取れます。欠損項目・不正型・過大出力・内容重複・品質不足はPythonで拒否します。生成テキストは既存rendererでHTMLエスケープします。原典URLはモデル出力から採用せず、取得済みメタデータから生成します。

## 履歴・公開・障害復旧

永続データは従来どおり `data/seen.json` と記事HTMLです。DOI、バージョンを除いたarXiv ID、正規化タイトルを全分野の履歴と照合します。上位2本を切り出す前に既処理を除き、深い候補プールから選びます。本文取得は1実行で最大12候補、日次ステップは30分で打ち切り、完了済みcheckpointの保存を試みます。新着1本＋重要1本という既存の選定を維持します。関連候補内では本文リンクのある論文を優先し、取得不能な高適合候補だけで待ち続けないようにします。1日あたりの件数はJSTの日付で数えます。

日本語記事・英語記事・seenはatomic writeし、検証済みの1論文分が揃ってからcheckpointを保存します。過去のseenレコードと記事の削除・上書き、英語版の欠落、旧カテゴリへの新規記事、スタブ公開、1日2論文超過を公開前に拒否します。

workflowは権限を3ジョブに分けています:

| ジョブ | 権限 | 処理 |
| --- | --- | --- |
| generate | `contents: read`, `copilot-requests: write` | 取得・要約・検証・復旧artifact作成 |
| publish | `contents: write` | 生成ファイルの検証・commit/push・静的サイトbuild・Pages artifact upload |
| deploy | `pages: write`, `id-token: write` | 同じworkflow内で公式 `actions/deploy-pages` を実行 |

`actions/upload-pages-artifact` に渡すのは公開HTMLと実行レポートだけです。ソースコード、入力PDF、seen、CLI設定・ログ、tokenはPages artifactに含めません。`concurrency` により日次実行と手動実行の重複を防ぎます。後続workflowがbotのpushで起動することには依存しません。[GitHub Pages公式workflow説明](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)

Git pushは最大3回、fetch/rebaseして通常pushします。競合を自動で片側採用せず、rebaseを中止してPages更新も停止します。復旧用 **survey-results** artifactを30日保持するため、競合時に推論し直す必要はありません。記事が未pushの場合は全workflowを再実行する前にこのartifactを復旧してください。

復旧時はartifactの `manifest.json` にある `base_sha` を専用の作業ブランチにcheckoutし、artifactをリポジトリ外へ展開して次を実行します:

```bash
python -m pipeline.publish apply /path/to/survey-results
python -m pipeline.publish push /path/to/survey-results
```

競合は内容を確認して手動で解消し、保存後 `publish_only=true` でPagesだけを公開できます。force pushは使いません。30日を過ぎると未push生成物の復旧artifactは削除されるため、失敗した実行は放置しないでください。

## ローカル検証

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m compileall -q pipeline tests ask.py add-pdfs.py
python -m unittest discover -s tests -q
python -m pipeline.run --dry-run
python -m pipeline.run --render-indexes-only
python -m pipeline.publish build /tmp/survey-pages-preview
```

最後の出力先は空ディレクトリを指定します。テストは一時ディレクトリとmockを使い、論文履歴やCopilot枠を変更しません。`--render-indexes-only` は既存の一覧HTMLを更新します。`--offline` / `--stub` は開発用で、生成物は公開バリデータが拒否します。本番作業ディレクトリではdry runを使ってください。

日次以外のPDF追加・追加質問ユーティリティも同じCopilotアダプターを使います。Actionsの外で使う場合は適切なCopilot認証が別途必要ですが、アダプターは `GITHUB_TOKEN` 以外へフォールバックしません。日次運用には不要です。`add_paper` の追加先はMAPFのみ、既存記事は既定でスキップします。`regenerate_existing` は明示的なMAPF記事1件を指定する保守用で、日次から呼びません。

## 設定

| 設定 | 既定 | 用途 |
| --- | --- | --- |
| `subscriptions.yml` のMAPF `k` | `2` | 日次最大論文数。各論文の日英2ページを作るため最大4ページ |
| repository variable `COPILOT_MODEL` | 空 | Copilotの既定モデル。指定時は所有者が利用可能なモデル名 |
| `COPILOT_CONTEXT_CHARS` | `24000` | 原文抜粋の文字予算（4,000〜32,000） |
| `COPILOT_TIMEOUT_SECONDS` | `300` | CLI呼び出しのtimeout秒 |
| `GITHUB_TOKEN` | Actions組み込み | `copilot-requests: write` 付き短期token |

Python依存はPyYAMLとPyMuPDFのみです。検索元の無料・無鍵アクセスが制限された場合は警告して利用できる取得元を継続します。追加キーや課金サービスは自動導入しません。

## 移行前の構成と変更点

- エントリーポイントは `deploy/run-daily.sh` → `pipeline.run`。実運用はリモートサーバ上のvenv＋06:00 user cron、Pagesは `main / root` のbranch公開でした。既存Actions workflowはありませんでした。
- `pipeline/summarize.py` がOpenAI互換 `/models` と `/chat/completions` を使い、ローカルモデルへ接続していました。接続コードと `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`、多段の追加推論を撤去しました。
- Docker/compose/systemd参考設定と旧publishスクリプトを削除しました。`run-daily.sh` はPython呼び出しのみになり、サーバのパス、`.env`、PAT、リモートLLM、cron再試行に依存しません。
- RAGと個別読書カテゴリを購読・画面から撤去し、直リンク互換用の既存HTMLだけを公開対象に保持。既存検索・PDF/HTML抽出・名寄せ・renderer・MAPF記事URLを再利用しています。
