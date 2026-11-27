# tl1833 调研：跨会话 tail 的 outline 模式效果评估

- 任务：tl1833（P2·只读调研）
- 日期：2026-11-27（任务书 2026-11-26，文件名沿用任务书指定）
- 线别：reasonix fork 开发线
- 结论性质：调研（非诊断），含方案建议供拍板
- 归口：workspace docs（DP-0022）；`feat-tl1833` 分支仅留调研件副本（原型脚本+本报告），**零仓库代码改动**
- non-goals 自证：未改 read_session_tail 现状行为、未改 zcode、未动协议字段、未做 UI 设计。worktree `wt-tl1833` 相对 tip `918164cb62cc` 的 diff 仅新增本报告与调研脚本（`git diff --stat` 自证）。

---

## 0. 结论先行

1. **zcode 的调用级意图是「模型生成、UI 提取」，协议里没有意图字段。**意图文本来自工具入参（Bash 的 `description` 参数由模型在调用时填写）与工作流步骤 label 等；UI 渲染时从 `input` 里提取。**协议 ToolCallRow 无 title/intent 字段**（任务书 rows.ts:207 核实属实）。
2. **reasonix 会话 transcript 的 78.2% 字节是工具输出**；现状 `read_session_tail`（按字节读尾）10KiB 预算的聚合构成：**60.3% 是被硬切的半截 JSON**（单行超预算时连整行都拿不到）、10.5% 完整工具输出行、仅 29.2% 是 assistant 行。32/45 会话的 tail 里**一个 assistant 轮次都没有**。
3. **解析侧 outline 原型（只抽 assistant 文本+工具名序列）实测大幅可行**：同 10KiB 预算，assistant 轮次覆盖从合计 26 个 → 992 个，**放大 38.2 倍**（任务书预估 10 倍，实测更高）；**意图可得率 98.5%**（2490 次工具调用中，98.5% 所在 assistant 行带非空文本）；44/45 会话 outline 覆盖到会话收尾结论。
4. **方案建议：解析侧 outline（read_session_tail 加可选 outline 模式或伴生只读工具），协议不加字段。**「给每行 transcript 加意图字段」性价比低——意图信息天然存在于 assistant 行自身文本中（可得率 98.5%），不需要模型或协议配合。

---

## 1. 范围与数据源

| 项 | 值 |
|---|---|
| reasonix 主仓 tip | `918164cb62cc`（main-v2-stable，2026-11-27 现查） |
| worktree | `github-repo/worktrees/wt-tl1833`（branch `feat-tl1833`，起点=tip） |
| 会话样本 | `~/.reasonix/sessions/` 主 transcript `*.jsonl` 共 **45 个**（2 个 code-* 主会话 + 43 个 subagent-*），合计 10.2MB，2026-05~06 真实会话 |
| 抽样口径 | 与 `read_session_tail` 实际读取口径一致：`IsMainTranscript`（`internal/store/session.go:89-99` 排除 events/turn/conflict 等后缀 + `.inbox.jsonl`） |
| 预算 | 10240 字节（`read_session_tail` 默认 `max_bytes`） |
| token 估算 | chars/4 粗估（中英混合），标注为估算值 |

---

## 2. zcode 意图字段机制核实（调研问①）

**结论：意图=模型在工具入参里生成，随调用持久化；协议无调用级意图字段；UI 侧只做提取渲染。**

### 2.1 协议层无意图字段

- `packages/shared/src/zcode-protocol-v4/rows.ts:206-235`：`toolCallRowSchema` 字段为 `toolCallId / toolName / status / inputText / input / output / display / error / progress / ...`，**无 title、无 intent**（任务书「rows.ts:207 已查」即此 schema 字段区起点，核实属实）。

### 2.2 意图文本的生成点=工具 schema 参数（模型填写）

- `apps/zcode-cli/packages/contracts/src/tools/bash.ts:60`：`description: z.string().optional().describe(BASH_DESCRIPTION_FIELD_PROMPT)`——Bash 工具入参自带可选 `description`；字段说明（`bash.ts:14-27`）明确写给模型：「Clear, concise description of what this command does in active voice ... **the user reads this description, often without seeing the command**」，并给出正例（`ls → List files in current directory`）。
- 持久化实锤（本机 `~/.zcode/cli/db/db.sqlite` part 表，只读抽样）：Bash 调用 input 形如
  `{"command": "cd ... && head -100 scripts/check-fork-integrity.mjs", "description": "查看 fork integrity 脚本头部了解锚点机制"}`。

### 2.3 UI 侧提取链（file:line）

| 环节 | 位置 | 行为 |
|---|---|---|
| Agent/Task 类卡片主文本 | `packages/ui/src/ToolCallBlocks/renderers/agentHelpers.ts:228-272` `getAgentPrimaryText` | 优先级：title → **`readStringField(toolCall.input, ["description"])`（:242）** → subagent_type/agent_type → output 摘要 → fallback |
| Bash 卡收起态 | `packages/ui/src/ToolCallBlocks/renderers/execute.tsx:364-376` | 有命令文本时 `primaryText=null`，主行显示命令本身（`getExecuteSecondaryText` 取 `command/cmd/script`，:20）；`title` 仅作悬停提示 |
| 工作流 workspace 卡「已执行 X」 | `packages/ui/src/app-shell/WorkflowWorkspaceCard.tsx:174-186` | 动词=i18n `chat.toolCall.execute.ran`=「已执行」（`packages/ui/src/i18n/locales/zh-CN.ts:5836`）+ 对象=`card.command ?? card.primary`；卡模型由 `packages/ui/src/app-shell/workflowWorkspaceTranscript.ts:103-152` `buildWorkspaceCards` 构造（terminal 分支 primary=格式化命令，兜底=工作流步骤 label `stepLabelBySiteId`——步骤 label 亦是模型在创建工作流时生成） |
| 通用摘要（bot 通道） | `packages/shared/src/tool-call-summary.ts:210-216` `getCompactToolCallSummary` | `primaryText = (title && normalize(title)) \|\| "tool"`，`secondaryText = getInputSummary(input)`（从 input 取 `command/path/file_path/prompt`，:164） |

### 2.4 对 reasonix 的含义

zcode 的「意图行」**不是协议新增能力**，而是「模型按工具 schema 的 description 参数自觉填写 + UI 从 input 提取」。reasonix transcript（ChatML 风格 jsonl）的 `tool_calls[].function.arguments` 是原始 JSON 字符串，**Bash 调用同样可以带 description 参数**（若模型侧提示词要求填写）；但 outline 原型实测表明：即使不依赖工具参数，assistant 行自身文本（content/reasoning_content）已提供 98.5% 的意图（见 §3）。

---

## 3. outline 模式效果评估（调研问②）

### 3.1 现状 read_session_tail 的成本结构

实现：`internal/agent/session_collab_tools.go:889-932`——`os.ReadFile` 全量读入 → 取尾部 `max_bytes`（默认 10240、上限 65536）→ 窗口内回退到 `\n` 行边界（**窗口内无 `\n` 则硬切半行**，注释 "when we can"）→ 原样字节返回，无解析无过滤。

45 会话聚合，10KiB tail 的字节构成：

| tail 内容 | 字节占比 | 说明 |
|---|---|---|
| 半截 JSON（硬切行） | **60.3%** | 14/45 会话 tail 为单行硬切（该行 >10KiB），读方拿到无法解析的字节 |
| role=tool 完整行 | 10.5% | 工具完整输出 |
| role=assistant 完整行 | 29.2% | 含 reasoning_content 长文 |
| role=user | 0.1% | |

辅助事实：

- **工具输出占全会话字节 78.2%**（45 会话 file-level 占比区间 32.6%~95.2%）。
- **32/45 会话（71%）的 tail 里 assistant 轮次数=0**——现状默认预算下，读方对七成会话「只看到工具过程，看不到任何一句模型的话」。

### 3.2 outline 原型设计

从尾部向前逐完整行解析 ChatML jsonl，每行产一条极简摘要行，预算用尽即停：

- user 行 → `U: <content 首行≤60 字符>`
- assistant 行带文本 → `A: <首行≤60>`；带 tool_calls → `A[工具名,..]: <content 或 reasoning_content 首行≤60>`；全空 → `(无文本)`
- role=tool 行 → 不产出（调用信息已并入 assistant 行），仅统计字节

原型脚本：`tmp/tl1833_outline_probe.py`（只读；随分支留痕 `docs/reasonix/report/tl1833-outline-原型.py`）。

### 3.3 数字表

| 指标 | 全量字节 tail（现状） | outline（原型） | 倍率/结论 |
|---|---|---|---|
| 同预算覆盖 assistant 轮次（45 会话合计） | **26** | **992** | **38.2×**（任务书预估 ~10×，实测更高） |
| 同预算覆盖 user 轮次 | 少量（0.1% 字节） | 全部最近 user 行 | outline 显式保留用户指令行 |
| 意图可得率（调用级） | —（tail 多为字节噪声） | **98.5%**（2490 次调用） | 意图=所在 assistant 行非空 content/reasoning |
| token 成本（估算，chars/4） | 10KiB≈2.6k tokens，其中 ≥60% 无法解析 | 同 2.6k tokens 覆盖 38×轮次；折算**每 assistant 轮次 ≈26 tokens**（tail 现状≈每轮 100+ tokens 且多不可读） | 同预算信息密度提升一个数量级 |
| 会话收尾结论覆盖 | tail 通常含末行 | **44/45 会话** outline 覆盖最后一条带文本 assistant 行 | 宏观保真：收尾结论不丢 |
| 最大样本（code-my-project，1.37MB/404 调用） | tail 5 轮 | **126 轮**，意图可得率 95.5% | 25.2× |
| 次大样本（图像数据管理平台，0.92MB/276 调用） | tail 1 轮 | **134 轮**，意图可得率 94.6% | 134× |

### 3.4 宏观信息保真度（定性）

outline 样本（code-my-project，时间正序，节选）：

```
U: 先重新配置沙箱根目录，我要如何做
A: GitHub 访问超时了，搜不到具体 issue。不过从我的系统提示里有几条线索：
A[web_search]: GitHub is timing out. Let me try the issues page with a sear
A[web_fetch]: The search didn't find the GitHub repo. Let me try directly
A[directory_tree]: Found `.reasonix/` directory (which contains skills) and `RE
A[search_files]: 翻一下有没有 Reasonix 的配置文件：
A: 找到了。沙箱根是在启动 Reasonix 时由当前工作目录决定的...
U: exit
A: 好的，退出后请执行：
```

足以支撑跨会话协调判断（「这条线在干嘛/卡在哪/做到哪」）；损失的只有工具输出正文与参数细节，需要时再按 budget 全量读该会话即可（两种模式互补，非替代关系）。

弱意图样本（98.5% 之外的 1.5%）：形如 `A[search_files]: (无文本)`——assistant 连续静默调用工具，outline 只能给出工具名序列；工具名本身已传达相当意图（read_file/web_search/directory_tree 语义自明）。

---

## 4. 方案对比与建议（调研问③）

意图可得率 98.5%，未触发「大量工具无意图」的替代方案前提；协议加字段路线**无需启动**。对比留档供拍板：

| 方案 | 做法 | 优点 | 缺点/风险 | 判定 |
|---|---|---|---|---|
| **A. 解析侧 outline**（推荐） | `read_session_tail` 加可选 `mode=outline`（默认 `full` 保持现状行为，non-goal 兼容），或伴生只读工具 `read_session_outline`；Go 侧解析主 transcript 只吐摘要行 | 零协议改动；零模型侧配合；兼容全部历史会话；实测 38×轮次/98.5% 意图 | 解析器与 transcript 行格式耦合（格式演进需同步改）；reasoning 是思考非正式结论，个别意图行质量弱（1.5% 无文本） | **推荐**：成本最低、实测收益最大、可退（mode 开关） |
| B. 写侧摘要 sidecar | 会话层 append assistant 行时同步写摘要 jsonl，读侧 O(1) | 读侧零解析 | 动写路径（store 内层）；历史会话无 sidecar 需回填；摘要与正文双真相源，违背 SSOT 有腐化风险 | 不推荐：复杂度高、收益被 A 覆盖 |
| C. 协议/transcript 加意图字段 | 每行加 `intent` 或 ToolCallRow 加字段 | 语义最标准 | 动协议（non-goal）；依赖模型配合填写、字段污染所有行；历史会话不兼容；实测意图信息 98.5% 已天然存在，字段冗余 | 不推荐：无对应问题缺口 |

风险与前提（方案 A）：

1. 行格式耦合——建议解析函数集中在 sessioncollab/store 一处并配格式断言测试（格式变更即红），不走散落正则。
2. outline 摘要行的截断（60 字符）可能截断中文语义——实测 60 字符对首行意图够用，可作为参数再调。
3. subagent 转录里 reasoning 常为英文、content 为中文——摘要行混合双语，对跨会话读者（模型）无碍。

---

## 5. 通俗版

现在的 `read_session_tail` 像是「把别的同事聊天记录的最后 10KB 原样撕下来给你」——十次里有六次撕在半句话中间，剩下的多半是程序输出的粘贴大段，你很难看出这个人到底在干嘛。实测里七成会话撕下来的碎片里连一句人话（模型的正式发言）都没有。

调研想学 zcode 界面的做法：zcode 每执行一条命令，界面上只显示一句「已执行 净树对照 task742/task758」这种简短意图。查下来这句话不是界面猜的，也不是协议里专门存的字段——是模型执行命令时自己顺手写的一句话（工具参数里的 description），界面把它提取出来显示而已。

于是我们做了个原型：读别的会话时不再撕原始字节，而是把记录里「AI 说了什么、调了什么工具」抽成一行行小摘要。用同样 10KB 的预算，原来只能看到 26 轮碎片，现在能看 992 轮完整轮廓（38 倍）；98.5% 的工具调用都能配上一句「当时为什么这么做」；45 个会话里 44 个连最后的收尾结论都保住了。所以建议就按这个思路给 `read_session_tail` 加个可选的 outline 模式（默认行为一点不动），协议不用改、模型不用配合，老会话文件直接兼容。

---

## 6. 证据与复现

- 机制核实：§2 表中 file:line 均出自 `github-repo/zcode`（sync v3.17.2）与 reasonix 主仓 tip `918164cb62cc`。
- 原型脚本：workspace `tmp/tl1833_outline_probe.py`；原始结果 `tmp/tl1833_probe_result.json`（45 会话逐文件数字+样本行）；分支留痕副本 `docs/reasonix/report/tl1833-outline-原型.py`。
- 复现：`python -I tmp/tl1833_outline_probe.py`（只读 sessions 目录，输出 JSON）。
- 零代码改动自证：`cd github-repo/worktrees/wt-tl1833 && git diff --stat 918164cb62cc HEAD` → 仅 `docs/reasonix/report/*` 新增。
