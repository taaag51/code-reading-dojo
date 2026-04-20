# Code Reading Dojo

Claude Agent SDK を使って、実リポジトリのコードを題材に Socratic 形式でコードリーディング / レビュー / アーキテクチャ追跡を練習する CLI。

「Claude に要約させて終わり」にならないよう、ユーザーが読解を言語化するまで Claude は要約も指摘も出さない。この強制力がツールの本体。

## なぜ作ったか

シニアエンジニアへの階段として、コードリーディング / レビュー力を上げたい。ただし Claude Code に PR を投げて要約させる運用では、要約を受け取っているのはユーザーではなく Claude 側で、読解スキルは伸びない。この CLI は順序を反転させる:

1. Claude は構造マップだけ提示 (要約・指摘なし)
2. ユーザーが読解を書く
3. Claude は読解の穴を突く質問だけを返す
4. ユーザーが読解を更新する
5. ここで初めて Claude が severity 付きで指摘を出す
6. 対比して見落としパターンを記録する

## インストール

```bash
git clone https://github.com/<your-username>/code-reading-dojo.git
cd code-reading-dojo
pip install -e .

export ANTHROPIC_API_KEY=sk-ant-...
```

`pip install -e .` で `dojo` コマンドが PATH に入る。

Python 3.10+ 必須。

## 使い方

**対象リポジトリの中に `cd` してから**実行する。Agent SDK は cwd 配下のファイルを Read/Glob/Grep する。

### 他人の PR をレビュー訓練

```bash
cd ~/work/bakuraku-box
gh pr diff 1234 > /tmp/pr-1234.diff
dojo review --diff /tmp/pr-1234.diff
```

### 自分の PR を出す前のセルフレビュー

```bash
dojo self-review               # main との差分
dojo self-review --base develop
```

### 実コードで読解訓練

```bash
dojo read internal/box/usecase
```

指定ディレクトリ配下から Claude が非自明なコードを1つ選んで提示する。

### アーキテクチャ追跡訓練

```bash
dojo trace 'POST /v1/documents'
dojo trace 'InvoiceUsecase.Create'
```

## 対話操作

各 Step でユーザー入力を求められたら:

- 複数行入力できる
- `/send` で送信
- `/skip` で現 Step をスキップ (学習効果は落ちる)
- `/exit` で終了

## モデル切り替え

デフォルトは `claude-opus-4-7`。

```bash
export DOJO_MODEL=claude-sonnet-4-5-20250929
dojo review --diff /tmp/pr.diff
```

コスト目安: opus 4.7 で 1 セッション数百円〜。sonnet 4.5 なら 1/5 程度。

## 安全性

`permission_mode="bypassPermissions"` + `allowed_tools=["Read", "Glob", "Grep"]` の組み合わせで動く。書き込み系ツールは一切許可していないので、本番リポジトリで回しても副作用はない。

## リポジトリ構成との連携

対象リポジトリに以下を置くと自動で読まれる (Agent SDK の `setting_sources=["project"]`):

- `./CLAUDE.md` — プロジェクト固有の文脈
- `./.claude/skills/<name>/SKILL.md` — カスタムスキル

Bakuraku リポジトリで既に運用している Claude Code 設定がそのまま有効。

## 開発

```bash
python -m py_compile dojo.py       # syntax check
python -c "import dojo"            # import check
python dojo.py --help              # CLI help
```

CI は `.github/workflows/ci.yml` で Python 3.10/3.11/3.12 に対して上記を走らせる。

## 設計ノート

- Socratic protocol はシステムプロンプトに書き込まれている (`dojo.py` の `REVIEW_SYSTEM` など)
- protocol を変更したい場合はシステムプロンプトを編集する
- 4 モード (review / self-review / read / trace) はそれぞれ独立した system prompt を持つ
- 全モードで `ClaudeSDKClient` によるマルチターン対話を使う

## ライセンス

MIT
