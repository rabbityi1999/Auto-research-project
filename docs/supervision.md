# 有上限的自动续跑

普通 skill 只在模型得到执行机会时工作。`scripts/supervise.py` 通过独立本地进程调用 [Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)，在连接失败后有上限地重试。进程处理适配自本机 ppt-resilience 的模式，增加研究阶段与选文关口。

## 主动启用

```powershell
python scripts/supervise.py --run "runs/my-topic" --max-attempts 12
```

先确认 Codex CLI 已登录并具备所需工具。CLI 工具不一定和桌面聊天相同。`--run` 可以是已有 run 的绝对路径，`--codex` 可指定 codex.exe。监督会消耗正常 Codex 额度，普通 skill 不会自行启动。它只负责 run 内本地文献和 PPT，不启动实验、远程算力、定时任务、发布或推送。

- 每轮最多完成一个有独立价值的单元，例如一篇论文、一个小节或少量幻灯片，保存后返回，由监督器接续。
- 网络错误退避重试，间隔最多60秒。默认累计最多12次调用，重启不会清零。单次最多20分钟，超时停止检查。
- 登录、权限、额度、限流及不明错误停止。连续两次正常结束但没有内容进展，也停止。
- 每轮开始及结束都检查选文状态，`awaiting_selection` 必须停下。记住明确会话ID，不使用 `--last`。
- 保留 workspace-write 沙箱；权限不足时停止，不关闭沙箱或自动提权。
- 同一 run 仅一个写入者。监督运行期间，桌面聊天不要同时编辑该 run，也不要再开子代理或另一监督器。

## 查看与停止

```powershell
python scripts/workflow.py resume runs/my-topic
python scripts/supervise.py --run runs/my-topic --stop
```

run 内 `.research-supervisor/` 保存 supervisor.json、process.json、每次调用的 JSONL 日志和 STOP 标记。日志可能含研究资料，留在本地。STOP 存在时不启动后续调用，需用户明确移除后才能重启。Ctrl+C 也可停止。外部工具任务未必随 CLI 结束，恢复前仍需核对。

若监督进程意外退出，状态仍为 running，先查 process.json 与工具任务，确认旧写入者已结束，再使用 `--recover-interrupted`。不能仅凭聊天断线就重复启动操作。

缺少材料或工具时执行 `workflow.py block <run> --reason "具体缺失"`，解决后用 `workflow.py unblock <run>`，再启动监督。unblock 不替用户选文、不改变精读状态。达到累计次数上限后，需明确提高 `--max-attempts`。

## 升级旧任务

复制到其他项目的工具不会随 GitHub 更新自动升级。恢复时先核对实际脚本路径；新版 resume 返回当前工具版本、上次登记的版本，以及已保存但未登记的笔记。先核验这些笔记再补登记，不能自动把文件存在认定为精读完成。

升级时保留 runs/；先检查未提交修改，再更新工具和技能。不要覆盖研究材料或为匹配旧哈希撤销用户修改。

机器、进程和网络仍须可用。程序不能保证不断线或恢复未保存的推理；完成仍依赖真实全文核验和逐页视觉检查，不能用退出码代替。
