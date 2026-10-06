# 小红书点点 → openCook

个人本机使用的 Python 命令行工具。把用户挑选的小红书**视频笔记链接**交给网页版点点，自动读取菜谱 JSON，生成可导入 openCook 的文件。第一版支持 macOS、Python ≥3.12 和 Google Chrome。

不遍历博主主页，不在评论区发消息，不调用其他模型 API。通过格式检查的菜谱自动导出；检查只验证结构和目标引用，**不代表已核实视频内容或估算用量**。

## 安装

先安装 Google Chrome 和 Python ≥3.12。macOS 自带的旧 Python 不适用。可通过 Homebrew 安装 Python：

```bash
brew install python@3.14
cd /Users/agiuser/Projects/openCook/tools/xhs_to_opencook
/opt/homebrew/bin/python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

工具直接使用已安装的 Chrome，无需运行 `playwright install`。其他位置的仓库或 Python 请调整路径。也可用 `python -m pip install .` 安装非 editable 版本。

## 登录与运行

```bash
xhs-to-opencook login
```

在弹出的专用 Chrome 中手动登录，然后回到终端按 Enter。工具确认登录后关闭窗口，保存登录状态。后续运行会重新打开同一配置目录。

创建 UTF-8 的 `videos.txt`，每行一个完整链接。支持 `www.xiaohongshu.com/explore/...`、`/discovery/item/...`、个人主页内单条笔记路径，以及 `xhslink.com` 短链接。尽量保留分享链接中的 `xsec_token`；过期令牌可能使原本存在的视频无法访问。空行忽略，UTF-8 BOM 可接受；不要混入分享文案。

```bash
xhs-to-opencook run --links videos.txt --output ./output
xhs-to-opencook run --links videos.txt --output ./output --refresh
```

运行期间保持专用 Chrome 打开。每条视频建立独立点点对话，串行处理。登录失效或验证码出现时，终端提示暂停：在 Chrome 中手动处理，回到终端按 Enter 后继续。Ctrl-C 可停止，重新运行同一命令可续跑。

默认复用成功缓存；失败和中断任务下次重试。`--refresh` 重新提问。每个视频最多两次尝试，JSON 无效时在原对话修正一次（整个视频最多一次修正），仍无效则失败，继续其他视频。页面适配失效时停止整批，保留已完成结果。

| 选项 | 默认值 | 用途 |
| --- | --- | --- |
| `--interval` | `10` 秒 | 两次任务/尝试之间的最小间隔 |
| `--timeout` | `180` 秒 | 单次点点回答等待时长 |
| `--state-dir` | `~/Library/Application Support/openCook XHS` | 专用 Chrome、缓存和原始回答目录；login/run 应使用相同目录 |
| `--non-interactive` | 关闭 | 登录/验证码需要处理时立即停止，适合自动任务 |
| `--refresh` | 关闭 | 忽略菜谱成功缓存，重新提问 |

间隔与超时须为大于 0 的有限数。状态目录与输出目录不能互相包含。同一状态目录同时只允许运行一个任务。

## 输出与导入

输出目录每完成一条任务都会更新，文件包含**本次输入列表的成功结果**，不会把历史所有缓存混入当前文件。

| 文件 | 内容 |
| --- | --- |
| `recipes.zip` | `recipes.json` 和 `images/<note-id>.jpg`，优先用此文件导入 |
| `recipes.json` | 独立导入文件，封面使用远程网址；网址可能过期 |
| `report.json` | 成功/缓存/失败/跳过、尝试次数、错误码、估算用量、封面失败、未处理数量 |
| `raw/<note-id>/<timestamp>-<attempt>/` | 来源、引用证据、原始/修正回答、转换结果或错误，异常时含页面截图及 HTML |

在 Android openCook 的文件导入入口选择 `recipes.zip`。菜谱集名称使用作者；食材用途保留在名称里，例如“白糖（杀水用）”和“白糖（料汁用）”。原视频链接、原始用量、估算说明及注意事项在备注中。

导入名称为 `菜名（作者·视频末8位ID）`，同视频多道菜加序号。极少数名称冲突时追加完整笔记 ID，并将实际名称写回缓存。工具不设置 `identifier`：openCook 按名称跳过已存在菜谱。重复导入同一文件应跳过；`--refresh` 后若点点改变菜名，可能成为新菜谱，不覆盖此前用户编辑。

仅来源明确时保留份数和标准 PT 时间。“一夜”“至熟”等保留为文字。点点应保留明确用量，估算缺失/模糊用量，按 `estimated` 标记写入备注与报告；格式校验无法判断模型标记是否符合视频事实。封面使用已登录浏览器获取，验证图片内容后转换为 JPEG。封面失败不影响文字导出，ZIP 不放无效图片。

## 状态和恢复

状态目录中 `chrome/` 存登录状态，`cache.sqlite3` 存逐条状态及短链接映射，`covers/` 存本地封面，`raw/` 存历次原始回答。输出也会复制相关笔记的原始记录。浏览器配置、缓存、依赖和默认输出均已加入 Git 忽略规则；自定义输出请放在 Git 仓库外或自行加入忽略规则。这些目录可能含登录信息、带令牌链接、个人页面及回答，应仅保存在本机。

常见错误：

| 错误码 | 处理 |
| --- | --- |
| `link_invalid` / `not_video` | 检查是否为可访问的视频笔记；更新完整分享链接 |
| `reference_mismatch` | 链接未形成唯一目标引用，或对话来源变更；不会提交错误引用/导出结果 |
| `answer_timeout` / `answer_incomplete` | 未看到页面明确完成信号，或显示中断；可提高超时后重试 |
| `json_invalid` | 回答缺字段、JSON 不完整或时间格式不合法；保留原文，重跑重新提问 |
| `image_failed` / `image_missing` | 文字仍可导入；检查报告，必要时刷新获取新封面 |
| `page_unsupported` | 页面版本变化，停止整批；需修正浏览器适配后再继续 |
| `login_required` | 重新 login 或取消 `--non-interactive` 手动处理 |
| `state_busy` | 同一配置已有进程，等待完成或停止已有任务 |

若关闭了 Chrome，工具会在后续尝试重新打开专用配置；任务失败仍保留已导出的菜谱。不要删除状态目录来解决单条失败；重新运行会自动重试失败任务。登录彻底失效时可先备份 `raw/`、`covers/` 和数据库，再移走 `chrome/` 并重新 login。

退出码：`0` 全部成功/缓存/跳过，`2` 部分任务失败，`1` 输入、运行环境或页面适配阻塞，`130` 用户中断。报告中的 `verification=format_and_reference_only` 说明验证范围。

## 开发与验证

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
ruff check src tests
python -m pip wheel . --no-deps --wheel-dir dist
```

测试覆盖严格 JSON、估算备注、分阶段食材、多菜品、图片路径及降级、重复/短链接、离线缓存、刷新、两次尝试/一次修正、中断续跑和适配失败停止。浏览器测试使用真实 Chrome 的独立临时上下文和模拟页面，不使用个人登录配置、不请求真实点点；缺少 Chrome 时这部分会明确跳过。

站点适配集中于 `src/xhs_to_opencook/browser.py`：读取笔记状态；粘贴链接触发网页自身引用动作，并检查该动作返回的唯一笔记 ID；选择真实可见输入框（排除隐藏同名节点）；仅在新用户消息、新 AI 回答、`.ai-message-finished` 完成标记和对话 ID 都符合时接受答案。文字稳定只是完成信号后的附加检查。不会用正则补齐菜谱字段，也不会用未验证页面行为批量处理。

2026-10-06 在 macOS / Python 3.14 / Chrome 上实测：专用登录、自动提交、唯一目标引用、完整 JSON 和完成标记均可读取；村驴的腌萝卜、牛肉辣椒酱、干蒸排骨三条视频均已导出，三个本地封面有效。实测也出现点点 JSON 不合格，以及页面关闭的失败，均有原始记录。三条通过不能保证大批次长期稳定。44 项测试通过；Android 已实际导入三条，重复导入跳过，腌萝卜导入后重新导出的完整字段与原文件相符。详情见 [验收记录](VALIDATION.md)。
