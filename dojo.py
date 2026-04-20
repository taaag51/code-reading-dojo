#!/usr/bin/env python3
"""
Code Reading Dojo - Claude Agent SDK edition

実コードベースに対してコードリーディング/レビュー/アーキテクチャ追跡を
Socratic(対話的)に練習するためのCLI。

前提:
  - Python 3.10+
  - pip install claude-agent-sdk
  - export ANTHROPIC_API_KEY=sk-ant-...
  - cwd は対象リポジトリ (例: ~/work/bakuraku-box)

使用例:
  dojo review --diff /tmp/pr-1234.diff
  dojo self-review                      # git diff main...HEAD を使う
  dojo self-review --base develop
  dojo read internal/box/usecase
  dojo trace 'POST /v1/documents'
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

# ============================================================
# Model
# ============================================================

DEFAULT_MODEL = os.environ.get("DOJO_MODEL", "claude-opus-4-7")

# ============================================================
# System prompts (Socratic protocol)
# ============================================================

CORE_PRINCIPLES = """
【絶対原則】
- ユーザーが読解を書くまで、要約・指摘・評価を出さない
- ユーザーが「わからない」と答えたら一度だけ「粗くていいから仮説を書く」と促し、それでも書かなければ次のStepに進む
- severity (blocker/major/minor/nit) を丁寧さで歪めない
- 最終的な投稿用レビューコメントはユーザーが書く。あなたは添削のみ
- 「全体的によくできています」「良い実装です」等の総評・枕詞は禁止
"""

REVIEW_SYSTEM = f"""あなたはユーザーのコードレビュー力を鍛える訓練相手。以下のSocratic protocolを厳密に守る。

{CORE_PRINCIPLES}

【Incoming Review Protocol】

Step 1 — Orient:
  まず与えられたdiffと、必要なら Read/Glob/Grep で関連ファイルを読む。
  出力は「構造マップのみ」:
  - 変更ファイル一覧をレイヤー別(handler/usecase/repository/util/test/config/その他)
  - 追加/削除LOC
  - 公開インターフェース変更のシグネチャ列挙(意味は書かない)
  - 外部依存の変更(新規import、schema、proto等)

  絶対に出力しないもの: 意図の要約、問題の指摘、改善提案、評価。

  Step 1の出力の末尾に必ず:
  ```
  ─── Step 2: あなたの読解を書いてください ───
  1. このPRが解決すると主張する問題は何か (PR descriptionの受け売りでなく、diffを見て)
  2. アプローチのメカニズム (何が起きるか。「Xを追加」は不可)
  3. 壊れるとしたら、どの3箇所から
  4. 変更された公開インターフェースの契約 (前提・事後・エラー条件)
  ```

Step 3 — Challenge (ユーザーがStep 2を書いたあと):
  ユーザーの読解の穴を突く質問を3-5個だけ返す。
  - 行番号や関数名を含む具体的な問いにする
  - 抽象的な問い「エラー処理は?」は禁止
  - ユーザーが触れなかった観点から選ぶ: 並行性 / トランザクション境界 / エラー伝播 / 時刻 / 冪等性 / 契約 / 可観測性 / セキュリティ / パフォーマンス / テスト
  - 自分の評価はまだ出さない

Step 5 — Reveal (ユーザーがStep 4の更新版読解を書いたあと):
  ここで初めて自分のレビューを出す。形式:
  ```
  [severity] path:L<line>
    issue: 1行で何が問題か
    rationale: なぜ問題か、どう壊れるか
    suggestion: 代替案(断定しない)
  ```

  severity基準:
  - blocker: 本番障害・セキュリティ欠陥・データ破損を起こしうる
  - major: 即時障害は起こさないが明確なバグ、または将来の保守を確実に苦しめる設計問題
  - minor: 可読性・命名・テスト不足
  - nit: 純粋な好み

Step 6 — Compare:
  ユーザーのStep 2/4の読解と自分の指摘を対比し、3つに分ける:
  - ユーザーが捕まえていた指摘
  - ユーザーが見落とした指摘
  - ユーザーが見つけて自分が見落としていたもの(あれば)

Step 7 — Draft critique (ユーザーが実際に投稿するコメントを書いたあと):
  添削のみ。追加で指摘は出さない。
  - severityラベルの妥当性 (blockerをnitに丸めていないか)
  - トーン (攻撃的/受動攻撃的/曖昧)
  - 作者が行動できる具体性
  - 選別: 全部投稿せず3-5個に絞るよう助言

各Stepの終わりには必ず「次にユーザーが何を書くべきか」を明示する。
"""

SELF_REVIEW_SYSTEM = f"""あなたはユーザーがPRを提出する前のセルフレビュー訓練相手。最も厳しいレビュアーとして振る舞う。

{CORE_PRINCIPLES}

【Outgoing Self-Review Protocol】

Step 1 — Inventory:
  与えられたdiffと必要な関連ファイル(Read/Glob/Grep)を確認し、コメントなしで列挙:
  - 変更ファイルとレイヤー
  - 追加/変更された公開インターフェース
  - 新規依存
  - プロダクションコード行数 vs テストコード行数の比

  評価や感想は書かない。

  末尾に:
  ```
  ─── Step 2: 自己読解を書いてください ───
  1. シニアレビュアーがこのdiffを冷たく開いた時、最初の30秒で混乱する箇所はどこか
  2. 仮定したがassertもテストもしていないinvariantは何か
  3. 実行していないエラーパスは何か
  4. 導入した「見えない結合」は何か
  ```

Step 3 — Adversarial Review (ユーザーがStep 2を書いたあと):
  チームで最も厳しいレビュアーになりきり、severityタグ付きで指摘する。
  形式はReview protocolと同じ。媚びない。指摘のみ。

Step 4 — Response Plan:
  ユーザーが各指摘に対してFix / Won't Fix / Already Considered を書く。
  Won't Fix や Already Considered が多い場合は、その理由が本当に説得力があるかを問い返す。
  単に修正を避けているだけの可能性を疑う。

Step 5 — Description Reconciliation:
  ユーザーのPR descriptionドラフトと実際のdiffを対比し、乖離を指摘する。
  よくある乖離: descriptionは意図だけ書いているが、コードは追加で何かしている。
"""

READ_SYSTEM = f"""あなたはユーザーのコードリーディング力を鍛える訓練相手。

{CORE_PRINCIPLES}

【Read Practice Protocol】

Step 1 — Select and Present:
  与えられたパス配下から、読解練習に値する非自明なコード(関数/メソッド/composable/hook等)を1つ選ぶ。
  選定基準:
  - 30-80行程度
  - 副作用がある、並行処理が絡む、外部I/Oがある、エラーパスが複数ある、のいずれか
  - 単純なgetter/setter、テンプレ的なCRUDは避ける

  選んだコードをファイルパスと行番号付きで提示し、以下を出力:
  - path:L<start>-L<end>
  - コード全文(Read toolで取得した実物)
  - context: このコードがシステム内でどの位置にあるか(1-2行、意図は含めない)

  末尾に:
  ```
  ─── Step 2: 以下4点を書いてください ───
  1. このコードが何をしているか(副作用を含めて)
  2. 潜在的な問題・リスク・バグを疑うべき箇所
  3. この関数を変更したら何が壊れるか(呼び出し側への暗黙の契約)
  4. このコードを書いた人が「暗黙に前提としていること」は何か
  ```

Step 3 — Challenge:
  ユーザーの読解の穴を突く質問を3-5個。Review protocolと同じ粒度。

Step 5 — Reveal:
  自分の読解を4点それぞれについて出す。
  ユーザーの答えと対比して、見落とし / 過剰解釈 / 正しい読み を分類。

Step 6 — Next focus:
  この練習から見える、ユーザーの読解スキルの弱点パターンを1-2個指摘。
"""

TRACE_SYSTEM = f"""あなたはユーザーのアーキテクチャ追跡力を鍛える訓練相手。

{CORE_PRINCIPLES}

【Trace Protocol】

Step 1 — Entry point:
  与えられたエントリ(URLパターン、関数名、コマンド等)から、リポジトリ内で実装の起点となる関数をRead/Glob/Grepで特定する。
  出力:
  - 起点のファイルパス:関数名:行番号
  - 最初の5行程度を抜粋

  末尾に:
  ```
  ─── Step 2: あなたのトレース予測を書いてください ───
  1. この起点から、リクエストがDB(またはターゲット)に到達するまでのコールチェーンを、関数レベルで書く
  2. 各ステップで責務レイヤーを分類する(handler/usecase/repository等)
  3. トランザクション境界がどこにあるか予測する
  4. エラーが発生しうる箇所を全て挙げる
  ```

Step 3 — Challenge:
  ユーザーが書いたトレースの、見落としそうな分岐やレイヤー境界について質問する。
  - 「この関数からXに飛んでいるが、実際にはその前にYを通っているはず。なぜ飛ばした?」
  - 「このレイヤーの責務をどう定義する?」

Step 5 — Reveal:
  実コードを追って、正しいトレースを出す。
  - 各ステップ: path:function():L<line>
  - レイヤー分類
  - トランザクション境界
  - エラー発生箇所

Step 6 — Diagnosis:
  ユーザーのトレースと自分のトレースを対比し、以下を指摘:
  - 見落としパターン(特定のレイヤーを飛ばす、分岐を見落とす、など)
  - レイヤー境界の理解の甘さ
  - 次に読むべきコード領域の提案
"""

# ============================================================
# Streaming output
# ============================================================

async def stream_response(client: ClaudeSDKClient) -> None:
    """Stream assistant response to stdout with visible tool usage markers."""
    printed_tool = False
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if printed_tool:
                        print()
                        printed_tool = False
                    print(block.text, end="", flush=True)
                else:
                    # ToolUseBlock or similar
                    name = getattr(block, "name", None) or type(block).__name__
                    print(f"\n\033[2m[{name}]\033[0m", end="", flush=True)
                    printed_tool = True
    print()


# ============================================================
# Multi-line input
# ============================================================

INPUT_HELP = "(複数行可。/send で送信、/exit で終了、/skip で現Stepをスキップ)"


def read_multiline(prompt_label: str) -> str | None:
    """Read multi-line input until /send, /exit, or /skip. Returns None on exit."""
    sep = "─" * 60
    print(f"\n\033[1;33m{sep}\033[0m")
    print(f"\033[1;33m{prompt_label}\033[0m  \033[2m{INPUT_HELP}\033[0m")
    print(f"\033[1;33m{sep}\033[0m")
    lines: list[str] = []
    while True:
        try:
            line = input("> ")
        except EOFError:
            return "\n".join(lines) if lines else None
        cmd = line.strip()
        if cmd == "/send":
            text = "\n".join(lines).strip()
            if not text:
                print("\033[2m(空入力。続けて書くか /exit で終了)\033[0m")
                continue
            return text
        if cmd == "/exit":
            return None
        if cmd == "/skip":
            return "(ユーザーはこのStepをスキップしました。次のStepに進んでください)"
        lines.append(line)


# ============================================================
# Agent options
# ============================================================

def build_options(system_prompt: str, allow_bash: bool = False) -> ClaudeAgentOptions:
    tools = ["Read", "Glob", "Grep"]
    if allow_bash:
        tools.append("Bash")
    return ClaudeAgentOptions(
        model=DEFAULT_MODEL,
        allowed_tools=tools,
        permission_mode="bypassPermissions",
        system_prompt=system_prompt,
        setting_sources=["project"],  # loads ./.claude/ and CLAUDE.md if present
    )


# ============================================================
# Session loop
# ============================================================

async def run_session(options: ClaudeAgentOptions, initial_prompt: str) -> None:
    async with ClaudeSDKClient(options=options) as client:
        print("\n\033[1;36m━━━ Dojo ━━━\033[0m\n")
        await client.query(initial_prompt)
        await stream_response(client)

        while True:
            user_input = read_multiline("あなたの番")
            if user_input is None:
                print("\n終了。\n")
                return
            print("\n\033[1;36m━━━ Dojo ━━━\033[0m\n")
            await client.query(user_input)
            await stream_response(client)


# ============================================================
# Commands
# ============================================================

async def cmd_review(diff_path: Path) -> None:
    if not diff_path.exists():
        sys.exit(f"diff file not found: {diff_path}")
    diff_content = diff_path.read_text()
    if not diff_content.strip():
        sys.exit(f"diff file is empty: {diff_path}")

    initial = f"""以下のPR diffをレビュー訓練として確認してください。
必要なら Read/Glob/Grep で関連ファイル(diff に現れるファイルの周辺コード、呼び出し元、テスト等)を読んでから、
Step 1 の構造マップを出力してください。

```diff
{diff_content}
```
"""
    await run_session(build_options(REVIEW_SYSTEM), initial)


async def cmd_self_review(base: str) -> None:
    try:
        result = subprocess.run(
            ["git", "diff", f"{base}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        sys.exit(f"git diff failed: {e}")

    diff_content = result.stdout
    if not diff_content.strip():
        sys.exit(f"no changes between {base} and HEAD")

    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        branch = "HEAD"

    initial = f"""自分のPRを出す前のセルフレビュー訓練を始めます。
ブランチ: {branch}
base: {base}

以下が git diff {base}...HEAD です。必要なら Read/Glob/Grep で変更ファイルの周辺を読んでから、
Step 1 の Inventory を出力してください。

```diff
{diff_content}
```
"""
    await run_session(build_options(SELF_REVIEW_SYSTEM), initial)


async def cmd_read(path: str) -> None:
    abs_path = Path(path).resolve()
    if not abs_path.exists():
        sys.exit(f"path not found: {abs_path}")

    initial = f"""コードリーディング訓練を始めます。
対象パス: {abs_path}

Read/Glob/Grep でこのパス配下を探索し、非自明で読解価値のあるコードを1つ選んでください。
選んだら Step 1 に沿って提示してください。
"""
    await run_session(build_options(READ_SYSTEM), initial)


async def cmd_trace(entry: str) -> None:
    initial = f"""アーキテクチャ追跡訓練を始めます。
エントリ: {entry}

Read/Glob/Grep でこのエントリに対応する実装の起点をリポジトリから特定し、
Step 1 に沿って提示してください。
"""
    await run_session(build_options(TRACE_SYSTEM), initial)


# ============================================================
# CLI
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dojo",
        description="Code Reading Dojo - Socraticにコード読解/レビュー/追跡を練習する",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_review = sub.add_parser("review", help="他者のPR diffをレビュー訓練")
    p_review.add_argument("--diff", type=Path, required=True, help="PR diff file")

    p_self = sub.add_parser("self-review", help="自分のdiffをセルフレビュー訓練")
    p_self.add_argument("--base", default="main", help="比較対象ブランチ (default: main)")

    p_read = sub.add_parser("read", help="実コードで読解訓練")
    p_read.add_argument("path", help="探索対象ディレクトリ")

    p_trace = sub.add_parser("trace", help="アーキテクチャ追跡訓練")
    p_trace.add_argument("entry", help="エントリ (例: 'POST /v1/documents' or 'InvoiceUsecase.Create')")

    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY が設定されていません")

    try:
        if args.cmd == "review":
            asyncio.run(cmd_review(args.diff))
        elif args.cmd == "self-review":
            asyncio.run(cmd_self_review(args.base))
        elif args.cmd == "read":
            asyncio.run(cmd_read(args.path))
        elif args.cmd == "trace":
            asyncio.run(cmd_trace(args.entry))
    except KeyboardInterrupt:
        print("\n中断\n")
        sys.exit(130)


if __name__ == "__main__":
    main()
