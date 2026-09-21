# Physical AI フロンティア・レーダー

[![daily radar](https://github.com/noteflowai/physical-ai-radar/actions/workflows/daily.yml/badge.svg)](https://github.com/noteflowai/physical-ai-radar/actions/workflows/daily.yml)
[![CI](https://github.com/noteflowai/physical-ai-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/noteflowai/physical-ai-radar/actions/workflows/ci.yml)
[![code: MIT](https://img.shields.io/badge/code-MIT-blue.svg)](LICENSE)
[![content: CC BY 4.0](https://img.shields.io/badge/content-CC%20BY%204.0-lightgrey.svg)](LICENSE-CONTENT)
[![updated daily 01:30 UTC](https://img.shields.io/badge/updated-daily%2001%3A30%20UTC-7300e5.svg)](.github/workflows/daily.yml)

**言語：[中文](README.md) · [English](README.en.md) · 日本語**

フィジカルAI（Physical AI）の最前線を毎日自動更新するレーダーです。やることは三つだけ：**8つの軸に分類する**、**すべての主張に根拠レベルを付ける**、**「なぜ重要か」を中国語・英語・日本語で書く**。

論文リストやニュース集約との違い：

- **根拠が追跡可能で、単位が混ざらない**：`[O]` 一次情報、`[R]` 論文/プレプリント、`[M]` メディア/二次情報。ベンダーの自己報告と査読結果を混ぜません。
- **検証できる数値のみ**：成功率、遅延、Hz、パラメータ数、データ時間を自動抽出。数値のない項目を「ブレイクスルー」として売りません。
- **前瞻的だが曖昧ではない**：自己改善ループ、推論時計算、世界モデルによる評価、実行時安全、規制スケジュール、信頼性の経済性を対象に、必ず原典リンクを添えます。
- **依存ゼロで再現可能**：パイプラインは Python 標準ライブラリのみ、図は手書き SVG。誰でも同じ出力をローカルで再現できます。

<!-- RADAR:START -->
### 本日のレーダー · 2026-09-21

`生成時刻: 2026-09-21 01:40 UTC` ｜ `対象期間: 2026-09-18 → 2026-09-21 (UTC)`

- `[O]` **[Physical AI Takes the Wheel: How the World’s Robotaxi Leaders Are Building With NVIDIA Technologies](https://blogs.nvidia.com/blog/robotaxi-leaders-full-stack-open-platform/)** — システムとオーケストレーション（長期タスク / 複数方策）
  - 長期タスクのシステム形態はオーケストレーターと方策プールであり、運用対象は単一モデルから能力境界の集合になる。
- `[O]` **[Fine-tuning a 350M Model for Better Structured Outputs in 100 GRPO Steps](https://huggingface.co/blog/grpo-with-trl-ifstruct)** — 学習と自己改善（RL / 事後学習）
  - 事後学習と自己改善は、配備後も方策が強くなれるか、運用で介入データを取る必要があるかを決める。
- `[O]` **[NVIDIA Brings Real-Time AI to Broadcast, Sports and Global Streaming at IBC](https://blogs.nvidia.com/blog/ibc-news-2026/)** — エッジとリアルタイム（オンデバイス推論 / 制御周期）
  - オンデバイスの遅延と制御周期は硬い制約：実時間予算を超えるモデルは制御ループに入れない。
- `[O]` **[Quantization-Aware Healing: a compressed, 4-bit model that outperforms its full-precision original](https://huggingface.co/blog/MultiverseComputingCAI/quantization-aware-healing)** — エッジとリアルタイム（オンデバイス推論 / 制御周期）
  - オンデバイスの遅延と制御周期は硬い制約：実時間予算を超えるモデルは制御ループに入れない。
- `[M]` **[The Best Way to Explore Lunar Craters Is a Giant Robot Ball](https://spectrum.ieee.org/moon-ball-robot)** — ハードウェアとサプライチェーン（機体 / コスト）
  - 機体とサプライチェーンがコスト曲線と納入ペースを決め、アクチュエータがBOMの主要部分を占める。

[本日のレーダー ›](radar/daily/2026-09-21.ja.md) · [アーカイブ ›](radar/INDEX.md)

![lane distribution](assets/lane-distribution.ja.svg)

![cadence](assets/cadence.ja.svg)
<!-- RADAR:END -->

## 8つの軸

| 軸 | 追跡対象 | 独立させる理由 |
| --- | --- | --- |
| 基盤モデル（VLA / WAM） | 視覚言語行動モデル、世界行動モデル、公開重み | 下流タスクの出発点と機体横断の転移可否を決める |
| データエンジン | 遠隔操作、一人称人間動画、精査、スケーリング | 新規タスクあたりのデータコストを決める |
| 学習と自己改善 | RL 事後学習、advantage conditioning、機群規模の循環 | 配備後も方策が強くなれるかを決める |
| シミュレーションと評価 | sim2real、real-to-sim、閉ループ成功率、評価基盤 | オフライン指標は閉ループ成功率ではない |
| エッジとリアルタイム | オンデバイス推論、量子化、非同期推論、制御周期 | 実時間予算を超えるモデルは制御ループに入れない |
| 安全・権限・規制 | セーフティフィルタ、CBF、ISO と規制日程 | 認証の空白期間では、安全は証明書でなく設計に依存する |
| システムとオーケストレーション | 長期タスク、オーケストレーター、記憶、方策ルーティング | 運用対象が単一モデルから能力境界の集合になる |
| ハードウェアとサプライチェーン | ヒューマノイド、アクチュエータ、触覚、BOM と産線 | コスト曲線と納入ペースの実際の律速 |

## 読み方

1. **主張より先に根拠タグを見る**。`[R]` の数値は著者の自己報告、`[M]` の出荷台数は媒体間で食い違うことが多く、`[O]` でも「プラットフォーム提供可能」と「顧客が導入済み」は別物です。
2. **形容詞より数値**。各項目は成功率・遅延・データ量を伴うことを目標にしています。数値がなければ、その項目はまだ物語の段階です。
3. **規制は日付で読む**。標準と規制の項目は発効日や段階日を明記し、プロジェクト計画にそのまま転記できます。

## 自動更新の仕組み

```
GitHub Actions（cron 01:30 UTC / 09:30 CST / 10:30 JST）
  └─ fetch    arXiv API（cs.RO の6クエリ）+ 公式 Atom/RSS（AWS / NVIDIA / DeepMind / HF / IEEE）
  └─ distill  8軸に分類 → 定量指標を抽出 → 説明可能なスコア → 軸ごとの上限で選抜
  └─ charts   手書き SVG：軸分布 / 日次推移 / 根拠構成
  └─ render   三言語の日報 + 3つの README へ注入 + アーカイブ索引と latest.json を更新
  └─ commit   変更があるときのみコミット（[skip ci]）
```

実行時刻は arXiv の当日公告後に設定しており、朝いちばんに最新の一群が読めます。

## ローカル実行

```bash
git clone https://github.com/noteflowai/physical-ai-radar.git
cd physical-ai-radar

python3 -m pairadar --offline --out /tmp/radar   # 通信なし・別の場所へ出力し、リポジトリは変更しない
python3 -m pairadar --offline                   # 通信なし・日報と README をその場で書き換える
python3 -m pairadar                             # 当日の新規項目を取得
python3 -m unittest discover -s tests           # テスト
```

pip install も仮想環境も不要。Python 3.10 以上で動きます。

`--out` を付けない実行は、三つの README のレーダー区画・`radar/`・`assets/` を
**その場で書き換えます**（日次ジョブの動作そのものです）。まず内容を確認したい
場合は `--out` を使ってください。どちらの場合も実行ログはリポジトリから読むため、
七日以内の重複除外はそのまま機能します。

## ディレクトリ構成

```
data/        sources.json（出典と重み）· taxonomy.json（軸とシグナル）
             glossary.json（三言語 UI と用語）· baseline.json（キュレーション基準）
pairadar/    fetch / distill / charts / render / cli —— すべて標準ライブラリ
radar/       daily/YYYY-MM-DD.{zh,en,ja}.md · INDEX.md · latest.json · history.json
assets/      自動生成の SVG 図
docs/        METHODOLOGY.md（方法論・翻訳方針・既知の限界）
```

## 既知の限界（先に明示します）

- 要約は**抽出型**です。英語原文の抜粋は引用として示し、機械翻訳しません。その上の分析層は**言語ごとに執筆**し、逐語訳は行いません。多くの日はエージェントが下書きし、各行に下書きであることを明記し、記載する数値はその日の公開パージに既に存在するものに限ります。下書きがない言語は軸ごとのテンプレートが補い、注記は付きません。[下書きの査読と主張できないこと](docs/METHODOLOGY.md)。
- 図は**言語ごとに作成**し、ラベルもその言語で記します。SVG テキストなのでグリフは読者のブラウザが描画し、フォントの同梱はありません。長い名称には `data/taxonomy.json` に短缮形を用意し、それでも入らない場合のみ切り詰めます。現在切り詰めされたラベルがないことはテストで担保しています。
- ランキングは**説明可能な重み付き和**であり、学習された関連度ではありません。重みは `data/` にあり、フォークして調整できます。
- 出典は公開された機械可読エンドポイントに限定し、本文のスクレイピングや有料壁の回避は行いません。

## 関連プロジェクト

レーダーは最前線の主張を追跡します。[**Robot Reel**](https://huggingface.co/spaces/glayguo/robot-reel) はその先を担い、主張の一部を検証できる記録に変えます。

- [SmolVLA Stress Lab](https://noteflowai.github.io/robot-reel/stress/) — 同一タスクを参照照明・減光・カメラ移動の三条件で回した 30 回の実クローズドループ実行（NVIDIA L40S / CUDA 推論）。対応シード、信頼区間、適用アクション、推論時間を個別に記録
- [Butterfly Lab](https://noteflowai.github.io/robot-reel/chaos/) — 0.05° ずつ異なる初期角の Newton 世界 12 個。軌跡は 3D の時間彫刻になります
- [対応結果データセット](https://huggingface.co/datasets/glayguo/robot-reel-paired-outcomes) — 記録された結果を機械可読な形で

「エッジとリアルタイム性」「シミュレーションと評価」レーンの項目には、あちらで実際に開いて確認できる対照実験がある場合が多いです。

**開示**：両プロジェクトは同じ著者が維持しています。そのためキュレーション基準（`data/baseline.json`）に自分たちの成果物は入れておらず、これらのリンクはここにのみ置いています。

## 貢献

PR を歓迎します：機械可読な新規出典、根拠タグの修正、基準項目の追加、三言語表現の改善。まず [CONTRIBUTING.md](CONTRIBUTING.md) と [docs/METHODOLOGY.md](docs/METHODOLOGY.md) をご覧ください。

## ライセンス

- コード：[MIT](LICENSE)
- コンテンツ（キュレーション文と生成ページ）：[CC BY 4.0](LICENSE-CONTENT)

---

*本リポジトリは技術観察であり、投資助言・製品保証・安全認証の結論ではありません。論文およびベンダーの主張は自己報告として扱い、引用前に原典を確認してください。*
