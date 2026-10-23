# Auto Research Project

一个由你决定精读范围的科研工作流：**AI 检索论文 → 向你讲解 → 你选择文章 → AI 精读 → 文献调研 PPT**。

这是供 Codex 使用的仓库级技能与进度管理代码。研究判断、联网检索、阅读全文和排版由 AI 配合所在环境的工具完成；Python 脚本负责记录状态、检查交接条件。它不是独立运行的无人值守爬虫，也不需要在仓库内保存模型 API Key。

## 开始使用

1. 在 Codex 中打开这个仓库，使用一个新聊天，以便发现 `.agents/skills/` 中的技能。
2. 发送下面的请求，替换课题即可：

   ```text
   使用 $research-workflow，调研“大模型在时序异常检测中的应用”。
   重点关注近三年工作，也保留必要的经典论文。
   先向我介绍研究脉络和候选论文，等我选定后再精读。
   最后做一份用于 15 分钟组会汇报的中文文献调研 PPT。
   ```

3. AI 完成介绍后，你可以回复：

   ```text
   精读 P003、P007、P012，重点比较问题设定、方法原理和实验公平性。
   ```

4. AI 精读所选文章，完成横向比较和 PPT。你也可以随时要求补搜、换选、修改汇报重点，或说“继续上次调研”。

技能通过标准 `SKILL.md` 加载，参考 [OpenAI 官方技能说明](https://learn.chatgpt.com/docs/build-skills)。如果当前客户端没有显示技能，可直接要求 AI 读取 `.agents/skills/research-workflow/SKILL.md`。技能以本仓库为整体使用，不要只复制单个目录而遗漏共享脚本与协议。

## 五个技能

| 技能 | 职责 | 主要产出 |
| --- | --- | --- |
| `research-workflow` | 管理全过程、读取进度、安排下一步 | `state.json`、阶段交接 |
| `research-survey` | 拆解问题、联网搜索、去重和初筛 | 检索日志、候选论文库 |
| `research-briefing` | 介绍研究脉络及论文，帮助你做决定 | 中文导读、选文表 |
| `research-reading` | 只精读你已选定的文章，分析原文证据 | 每篇笔记、横向比较 |
| `research-slides` | 将研究脉络和精读发现组织成汇报 | 可编辑 PPTX、讲稿、参考文献 |

导读会解释“大家在解决什么、主要路线如何不同、每篇论文为何值得读”，而不仅是罗列摘要。精读会追踪方法、假设和实验结果。PPT 按研究问题和方法联系组织，避免逐篇摘要堆叠。

## 运行条件

- Codex 或可读取这些技能的 AI 环境，具备联网搜索和原文读取能力。
- Python 3.9+，进度脚本只使用标准库。
- 实际生成 PPT 时需要可用的演示文稿工具或库及渲染预览能力。优先使用当前环境的 Presentations 技能；没有时按 `research-slides` 的替代流程处理。仅有 Markdown 大纲不等于 PPT 已交付。
- 付费墙或扫描质量妨碍阅读全文时，文章保持待处理。AI 会说明缺什么，不能凭摘要将其标为已精读。

## 项目结构

```text
.agents/skills/       五个可复用技能
docs/protocol.md      数据字段、阶段与证据约定
scripts/workflow.py   本地进度管理 CLI
tests/               关键阶段约束测试
runs/<课题>/          每次实际调研的材料与输出（默认不上传）
```

每个调研目录包含 `brief.md`、`search-log.md`、`papers.json`、`briefing.md`、`notes/`、`synthesis.md`、`slides/` 和 `state.json`。初始化只创建结构；AI 在对应阶段填写真实内容。

## 命令行

通常让 AI 操作即可。以下命令在仓库根目录执行：

```powershell
python scripts/workflow.py init runs/my-topic --topic "我的研究课题"
python scripts/workflow.py status runs/my-topic
python scripts/workflow.py import runs/my-topic --papers candidates.json
python scripts/workflow.py brief runs/my-topic --file briefing.md
python scripts/workflow.py select runs/my-topic --ids P001 P003 --user-message "精读 P001 和 P003"
python scripts/workflow.py read runs/my-topic --id P001 --note notes/P001.md --source https://example.org/full-paper
python scripts/workflow.py check runs/my-topic
python scripts/workflow.py complete runs/my-topic --deck slides/review.pptx --qa slides/qa.md
python -m unittest discover -s tests -v
```

`--papers` 相对于当前工作目录；`--file`、`--note`、`--deck`、`--qa` 相对于该调研目录，且必须留在目录内。上面是语法示例，网址不是论文来源。导入数据格式见 [协议](docs/protocol.md)。

`select` 只能在导读之后执行，需记录用户的真实决定。脚本检查阶段和文件，不会认证消息是谁写的，也无法代替 AI 核对科学内容。修改候选集会使旧导读失效；重新选文会清除已登记的精读状态，需要重新核验现有笔记。笔记登记后发生改动，交付检查会提示重新登记。

`complete` 要求所选论文的笔记齐全、PPTX 包含幻灯片、存在质量检查记录。AI 仍必须逐页查看渲染结果并检查引用，这些判断无法仅凭文件存在性完成。

## GitHub 与材料管理

本仓库用于分享工作流代码。`runs/`、论文 PDF、生成的 PPT 和本地密钥默认忽略，不会因提交代码而上传研究材料。希望分享具体调研成果时，应单独明确要分享的文件。仓库尚未指定开源许可证；公开可见不等于已授予开源许可。

本版验证范围是技能结构与本地流程约束。真实课题的检索质量、全文读取和最终 PPT 效果需要在首次课题运行时验证。
