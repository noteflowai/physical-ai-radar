# 微信内容交付：先验证本地字节，再接入平台

Radar 负责稿件、来源与呈现。通用能力观察、权限事件及恢复规则由私有
Agent Control 负责；这里没有新增调度器、发布循环或线上微信适配器。

## 已实际验证的范围

2026-10-05，仓库外先复验了现有物理AI Lab 内容包，再运行候选适配器。
8 篇 Markdown 和完整 HTML 的哈希一致，正文逐字绑定到完整 HTML；
16 张封面、8 张说明图通过 PNG 分块 CRC、尺寸及像素解压检查。
机械臂原创理想算例的两组解都在 `1e-12 m` 以内回到目标。

适配器使用这份实际内容包导出两次，得到相同的 49 文件压缩包及哈希：
`d388cef7a8574ec3631f659bdf80a1998ccfe95714dce4f27d9aefbdc61ba899`。
其中包括 24 个稿件文件、24 张配图和一份规范化来源/文件清单。
账号配置、浏览器请求、会话元数据及抓取到的登录链接不进入导出。
原始交付包仍保留；新清单额外绑定正文和各张配图的实际哈希。

上述验证不等于复现来源文章中的实验。O/R/M 来源标注、历史日期、
理想运动学/仿真范围、项目自报和未知项应保留在稿件中。
没有验证微信编辑器预览、素材上传、保存草稿、发表、群发或小游戏关联。
也没有确定该账号最新接口权限，不能将第三方文档当作账号能力证明。

## 本地使用

保持现有内容包在 Git 之外。源目录需包含 `content-manifest.json`、
公开来源清单、`articles/` 与 `assets/`；不接受 ZIP 作为输入。

```bash
# 只检查实际文件；不写输出，不联网。
python3 -m pairadar.wechat --source /absolute/path/editorial-source

# 导出到全新的绝对目录；父目录必须已经存在。
python3 -m pairadar.wechat \
  --source /absolute/path/editorial-source \
  --out /absolute/path/new-attempt

python3 -m unittest discover -s tests -p 'test_wechat.py' -v
```

只使用 Python 标准库，不依赖本机内容包生成器的 Pillow、BeautifulSoup
或 Mistune。适配器保留现有稿件和图片，不重新生成美术、不改写结论。
本地标题/作者/摘要上限为保守编辑政策（32/16/120 字符），不是已核实的
最新微信平台限制。长度合格仍需实际编辑器和账号权限核验。

输入篡改、失效 PNG、主动 HTML、缺少 AI 披露、缺少来源、越界路径、
符号链接或伪造线上发布状态会拒绝导出。完成验证后才创建输出目录。
已有或中断的目录不覆盖、不自动重放，保留原始失败证据并使用新尝试。
成功回执只代表本地交付：`wechat_live_validated=false`、
`publication_approved=false`。发布前还需原有安全扫描、内容检查和目的地核验。
字节哈希用于核对一致性，不认证输入作者或建立独立审核。

## 平台边界

API 是后续接入的优先路径。已核查 wepub 固定源码
`6ec1a6dfea367232e9b6bdcee1b2003d4f28483d` 的草稿链路，并实际运行
4 项原始测试和 4 项补充接口测试，全部通过；这些测试禁止真实网络 fetch，
不等于本公众号已接入。通用接入评估与研究证据留在私有 Agent Control。
后续按稳定 token → 正文图/封面 → 草稿保存/回读 → 独立发表状态核对推进。
不以网页模拟作为接口默认替代，不混称发表和群发。

正文仍引用本地图片，必须在正式允许的平台接入后使用真实素材 ID/URL，
保存草稿后回读。草稿、发表、群发、关联分别记录；小游戏官方 CI 的预览/
上传也不能代替备案、审核、正式发布和真机验证。

本次既有浏览器返回明确的网站安全拒绝；没有重试或改用坐标脚本、CDP、
其他浏览器、API 规避。先通过受支持的设置/支持渠道核清准入来源。
平台明确恢复允许后，原有授权仍适用，无需为普通可逆操作重复索取授权。

已阅读的设计参考：

- [官方小游戏 CI](https://github.com/wechat-miniprogram/miniprogram-ci-dist/blob/master/README.md)
- [浏览器网站权限管理](https://learn.chatgpt.com/docs/chrome-extension)
- [Linux Computer Use 边界](https://learn.chatgpt.com/docs/linux/linux-app)
