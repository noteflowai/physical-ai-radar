# Physical AI フロンティア・レーダー

[![last radar](https://img.shields.io/github/last-commit/noteflowai/physical-ai-radar/main?path=radar%2Flatest.json&label=last%20radar)](radar/INDEX.md)
[![site](https://img.shields.io/badge/site-live-34d399.svg)](https://noteflowai.github.io/physical-ai-radar/ja/)
[![feed: Atom · JSON](https://img.shields.io/badge/feed-Atom%20%C2%B7%20JSON%20Feed-f26522.svg)](https://noteflowai.github.io/physical-ai-radar/radar/feed.ja.xml)
[![CI](https://github.com/noteflowai/physical-ai-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/noteflowai/physical-ai-radar/actions/workflows/ci.yml)
[![code: MIT](https://img.shields.io/badge/code-MIT-blue.svg)](LICENSE)
[![content: CC BY 4.0](https://img.shields.io/badge/content-CC%20BY%204.0-lightgrey.svg)](LICENSE-CONTENT)
[![published daily 01:40 UTC](https://img.shields.io/badge/published-daily%2001%3A40%20UTC-7300e5.svg)](docs/automation.md)

**言語：[中文](README.md) · [English](README.en.md) · 日本語**

フィジカルAI（Physical AI）の最前線を毎日自動更新するレーダーです。やることは三つだけ：**8つの軸に分類する**、**すべての主張に根拠レベルを付ける**、**「なぜ重要か」を中国語・英語・日本語で書く**。

論文リストやニュース集約との違い：

- **根拠が追跡可能で、単位が混ざらない**：`[O]` 一次情報、`[R]` 論文/プレプリント、`[M]` メディア/二次情報。ベンダーの自己報告と査読結果を混ぜません。
- **検証できる数値のみ**：成功率、遅延、Hz、パラメータ数、データ時間を自動抽出。数値のない項目を「ブレイクスルー」として売りません。
- **前瞻的だが曖昧ではない**：自己改善ループ、推論時計算、世界モデルによる評価、実行時安全、規制スケジュール、信頼性の経済性を対象に、必ず原典リンクを添えます。
- **依存ゼロで再現可能**：パイプラインは Python 標準ライブラリのみ、図は手書き SVG。誰でも同じ出力をローカルで再現できます。

**購読：** [Atom（日本語）](https://noteflowai.github.io/physical-ai-radar/radar/feed.ja.xml) · [JSON Feed（英語）](https://noteflowai.github.io/physical-ai-radar/radar/feed.json) · [週間まとめ](radar/INDEX.md)。フィードリーダーに URL を貼るだけで、各項目の軸・根拠区分・主要な数値が届きます。

<!-- RADAR:START -->
### 本日のレーダー · 2026-09-26

`生成時刻: 2026-09-26 01:40 UTC` ｜ `対象期間: 論文 2026-09-22 → 2026-09-26 · ブログ・報道 2026-08-27 → 2026-09-26 · 30 日以内の重複なし (UTC)`

- `[R]` **[RoboRecover: Benchmarking Robot Policy Recovery under Execution Deviations](https://arxiv.org/abs/2609.28952)** — シミュレーションと評価（sim2real / 閉ループ）
  - 査読前のプレプリント。本文に実機での結果、定量データの記載なし。シミュレーション結果が実機性能をどこまで予測できるかに注目。
- `[R]` **[Albireo: Adaptive, Energy-Efficient Inference Framework for Video Object Detection on the Edge](https://arxiv.org/abs/2609.29648)** — エッジとリアルタイム（オンデバイス推論 / 制御周期） ｜ `17.6%` · `14.4%`
  - 査読前のプレプリント。本文に実機での結果、閉ループ評価の結果の記載なし。目標ハードウェアと実際の制御周期で高速化が保たれるかに注目。
- `[R]` **[Rolling-WAM: World Action Models with Rolling Imagination](https://arxiv.org/abs/2609.30247)** — シミュレーションと評価（sim2real / 閉ループ） ｜ `4.5x`
  - 査読前のプレプリント。本文に実機での結果、閉ループ評価の結果、定量データあり。シミュレーション結果が実機性能をどこまで予測できるかに注目。
- `[R]` **[Continuous Online Fault Detection for Mobile Robots via Adaptive Edge Models](https://arxiv.org/abs/2609.29194)** — エッジとリアルタイム（オンデバイス推論 / 制御周期） ｜ `4.30 ms`
  - 査読前のプレプリント。本文に閉ループ評価の結果の記載なし。目標ハードウェアと実際の制御周期で高速化が保たれるかに注目。
- `[M]` **[Video Friday: Life’s Better With a Little Robot Goose](https://spectrum.ieee.org/video-friday-goose-household-robots)** — ハードウェアとサプライチェーン（機体 / コスト）
  - 二次報道のため、引用前に一次情報の確認を推奨。本文に定量データの記載なし。価格、供給状況、量産出荷の主体に注目。

[本日のレーダー ›](radar/daily/2026-09-26.ja.md) · [週間まとめ ›](radar/weekly/2026-W39.ja.md) · [アーカイブ ›](radar/INDEX.md)

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
| ハードウェアとサプライチェーン | ヒューマノイド、アクチュエータ、触覚、BOM と生産ライン | コスト曲線と納入ペースの実際の律速 |

## 読み方

1. **主張より先に根拠タグを見る**。`[R]` の数値は著者の自己報告、`[M]` の出荷台数は媒体間で食い違うことが多く、`[O]` でも「プラットフォーム提供可能」と「顧客が導入済み」は別物です。
2. **形容詞より数値**。各項目は成功率・遅延・データ量を伴うことを目標にしています。数値がなければ、その項目はまだ物語の段階です。
3. **規制は日付で読む**。標準と規制の項目は発効日や段階日を明記し、プロジェクト計画にそのまま転記できます。

## 自動更新の仕組み

```
scripts/publish_daily.sh（メンテナーのマシン上の cron、01:40 UTC / 09:40 CST / 10:40 JST）
  └─ fetch    arXiv API（cs.RO の6クエリ、API が応答しない日はカテゴリ RSS）+ 公式・報道の Atom/RSS（AWS / NVIDIA / DeepMind / HF / TRI / IEEE / The Robot Report / 雷峰网 / MONOist）
  └─ distill  関連性ゲート → 8軸に分類 → 定量指標を抽出 → 説明可能なスコア → 軸・ソース・根拠区分ごとの上限で選抜
  └─ charts   手書き SVG：軸分布 / 日次推移 / 根拠構成
  └─ render   三言語の日報 + 3つの README へ注入 + アーカイブ索引と latest.json を更新
  └─ feeds    radar/feed.json（JSON Feed 1.1）+ 各言語の Atom + 今週のまとめ（radar/weekly/）
  └─ site     ランディングページ index.html · en/ · ja/（ダークテーマ、レーダー図、共有カード）
  └─ commit   変更があるときのみコミットして main に push
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
30 日以内の重複除外はそのまま機能します。

## ディレクトリ構成

```
data/        sources.json（出典と重み）· taxonomy.json（軸とシグナル）
             glossary.json（三言語 UI と用語）· baseline.json（キュレーション基準）
pairadar/    fetch / distill / charts / render / site / cli —— すべて標準ライブラリ
radar/       daily/YYYY-MM-DD.{zh,en,ja}.md · INDEX.md · latest.json · history.json
assets/      自動生成の SVG 図 · site.css · 共有カード og.*.png
docs/        METHODOLOGY.md（方法論・翻訳方針・既知の限界）
```

## 既知の限界（先に明示します）

- 要約は**抽出型**です。英語原文の抜粋は引用として示し、機械翻訳しません。その上の分析層は**言語ごとに執筆**し、逐語訳は行いません。多くの日はエージェントが下書きし、各行に下書きであることを明記し、記載する数値はその日の公開パージに既に存在するものに限ります。下書きがない言語は軸ごとのテンプレートが補い、注記は付きません。[下書きの査読と主張できないこと](docs/METHODOLOGY.md)。
- 図は**言語ごとに作成**し、ラベルもその言語で記します。SVG テキストなのでグリフは読者のブラウザが描画し、フォントの同梱はありません。長い名称には `data/taxonomy.json` に短缮形を用意し、それでも入らない場合のみ切り詰めます。現在切り詰めされたラベルがないことはテストで担保しています。
- 軸の判定は**キーワード一致**（部分文字列ではなく語単位）であり、分類モデルではありません。汎用ブログの項目は Physical AI のアンカー語（robot、humanoid、VLA、world model など）も含む必要があり、軸には少なくとも1件の一致が必要で、`python3 -m pairadar.lanes` は単一のキーワードで決まった項目（`thin`）や次点と近い項目（`ambiguous`）を示します。誤った軸は**見つけられます**が、防がれてはいません。

## 意図した選択（限界ではありません）

これらは「修正」しません。残りを検証可能にしているのは、この選択そのものです。

- ランキングは**説明可能な重み付き和**であり、学習された関連度ではありません。重みは `data/` にあり、フォークして調整できます。エージェントは新しい重みをプルリクエストとして提案できますが、読めないモデルに置き換えることはできません。
- 出典は公開された機械可読エンドポイントに限定し、本文のスクレイピングや有料壁の回避は行いません。これは約束ではなく**強制**です。下書きと査読のエージェントは `read`・`grep`・`glob` のみを宣言し、シェルもネットワークも持たないため、パイプラインが既に取得・公開したものの上でしか推論できません。その状態はテストで担保しています。

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
