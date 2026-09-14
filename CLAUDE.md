# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# 蜂巢验收自动化系统（`gwb-cloud/workflows`）

用户是产品经理，负责蜂巢（自研 SaaS IM 产品）的验收流程自动化。用户用 **GitHub Desktop** 提交代码、不熟悉命令行，解释操作步骤时要具体到"点哪个按钮"。修改脚本前先读完本文档，不要让用户重新解释背景、表结构或历史踩过的坑。

## 常用命令与调试方式

- **语法检查**（本仓库唯一的本地校验手段，没有测试、没有 lint、没有 requirements.txt）：
  ```bash
  python3 -m py_compile *.py
  ```
  CI 里没有语法检查这一步，语法错误只会在定时任务真正运行时才暴露（2026-09 曾因 `blind_test_tagging.py` 首行多了一个 `、` 导致打标任务连续一周失败），所以每次改完都要跑。
- **不要在本地直接运行业务脚本**：脚本在导入时就用 `os.environ[...]` 读 Secrets（缺了直接 KeyError），而且没有 dry-run 模式——一运行就会**真实发群消息 / 真实写飞书表**。需要验证逻辑时，用 `unittest.mock` 替换 `requests.get/post/put` 喂假数据来跑 `main()`。
- **线上测试方式是 GitHub 网页 → Actions → 选中对应 workflow → Run workflow**（`workflow_dispatch`）。可用的手动输入：
  - `daily_check.yml`：`force_send`（跳过日期判断，只验证"读表 + 发消息"链路）
  - `release_acceptance_summary.yml`：`target_date`（如 `2026-08-21`）、`debug`（打印每条记录的上线日期原始值和解析结果）
  - `blind_test_tagging.yml`：`target_date`（必须是节点更新总表里存在的发布日期）
- CI 环境：`ubuntu-latest` + Python 3.11，每个 yml 只执行 `pip install requests`。新增第三方依赖要改对应的每一个 yml。

## 整体架构

- 用 GitHub Actions 定时任务替代飞书多维表格原生自动化（飞书自动化有月度执行次数上限，且不支持复杂逻辑）。
- 纯 Python + `requests`，直接调飞书开放平台 API（读写多维表格）和蜂巢群机器人 webhook（发通知），无 SDK。
- **一个功能 = 一个 `.py` + 一个 `.github/workflows/*.yml`**，cron 定时 + `workflow_dispatch`。新增功能延续这个模式。
- **没有共享模块，公共代码是逐个脚本复制粘贴的**：`get_tenant_token`、`get_all_records`（分页）、`parse_date`、`get_select_value`/`get_text_value`/`get_person_name`、`send_to_beehive`。改其中任何一个的行为时，要 grep 全仓库同步修改。特别是：
  - `PERSON_BEEHIVE_ID` / 人员 ID 在 `check_and_notify.py`、`release_acceptance_summary.py`、`diagnose_at_users.py` 三处各有一份
  - 发布日期相关的 `get_releases` / `find_next_release_date` / `find_previous_release_date` / `belongs_to_release` 在 `release_acceptance_summary.py` 和 `blind_test_tagging.py` 各有一份，**两份必须保持一致**，否则两边算出的"本次发布"会不同
- 脚本顶部的 docstring 有些已经过时（例如 `weekly_redblack.py`/`check_and_notify.py` 提到的 `BEEHIVE_WEBHOOK_PRODUCT` 已不再使用），**以代码为准**。

### 发布日期的数据源（所有发布相关通知的基准）

- **节点更新总表的「版本发布日期」+「版本号」是发布日期的唯一数据源**，由用户手动维护，随时可能改期。脚本每次运行都重新读取，不缓存、不硬编码周几。
- **下一次发布** = 节点更新总表中 ≥ 今天−1 天的最近发布日期（1 天宽限兼容发布日刚过的过渡态）。
- **某次发布包含哪些需求** = 分工表里 `上线日期` 落在（上一次发布日期, 本次发布日期] 区间内、且 `端` ≠ 其他 的需求。这样发布改期后，分工表的上线日期不需要逐条跟着改。节点更新总表里没有更早发布记录时，退化为 `上线日期 == 本次发布日期`。
- `check_and_notify.py` 直接按节点更新总表每一行的发布日期检查 T+0/T+1，天然跟随改期。

### 三个项目

1. **项目1 - 验收重要节点通知**：`check_and_notify.py`，读节点更新总表，只看发布日期在最近 7 天内的版本，检查 T+0（P0 确认、ForBud 三端更新）/ T+1（盲测用例录入、帮助文档）事项。未完成的提醒**同时发到验收群和产品群**并 @ 负责人；超期 ≥ `ESCALATION_DAYS`(2) 天的再往产品群额外发一条黑榜预警。负责人来自 `ITEM_OWNERS`，其 key 必须与 `problems.append()` 里的文案逐字一致，改文案要同步改 key。
2. **项目2 - 上线验收流程 workflow 化**，两条线：
   - 发布验收总览：`release_acceptance_summary.py`
   - 月度盲测三件套，按顺序形成数据流水线（都在盲测 base 里，按表名 `M{月份}验收表` 定位）：
     `monthly_blind_test_setup.py`（每月1日建表 + 分配验收人）→ `blind_test_tagging.py`（每天给命中下一次发布的用例打版本号标记）→ `blind_test_progress_check.py`（每天晚上读结果统计进度）
3. **项目3 - 红黑榜**：`weekly_redblack.py`，每周一汇总本自然月红黑榜次数（纯文本，不 @）。

### 各脚本清单

| 脚本 | 触发时间（北京） | 作用 | 飞书应用 | 发消息用的 Secret |
|---|---|---|---|---|
| `check_and_notify.py` | 每天10点 | 见项目1 | 只读 | `BEEHIVE_WEBHOOK_YANSHOU` + `BEEHIVE_WEBHOOK_P1_NODE` |
| `release_acceptance_summary.py` | 每天10点、18点 | 按上文规则确定下一次发布及其需求，距发布 ≤5 个工作日才发；标题带版本号；按人/按端统计；验收表中窗口期内提出、二级模块模糊匹配、状态=待修复的遗留问题按 P0-P4 计数 | 只读 | `BEEHIVE_WEBHOOK_P2_WORKFLOW` |
| `weekly_redblack.py` | 每周一9点 | 本月红黑榜按人汇总 | 只读 | `BEEHIVE_WEBHOOK_P3_REDBLACK` |
| `monthly_blind_test_setup.py` | 每月1日9点 | 找/建 `M{n}人员` 列（上月顺序循环左移一位）→ 找/建 `M{n}验收表`（二级模块选项从用例库快照）→ 全量用例轮流分配验收人 | CLI 高权限 | 不发消息 |
| `blind_test_tagging.py` | 每天11点 | 按上文规则确定下一次发布及其需求，二级模块关键词匹配需求描述，在多选字段"本次抽检版本号"写入**版本号**（选项不存在会先自动添加）。每个版本的抽检总量 = 已打标 + 关键词命中，不足 200 条才从未完成用例里随机补齐到 200，重复运行不会继续增长 | CLI 高权限 | 不发消息 |
| `blind_test_progress_check.py` | 每天19点 | 时间进度基准线（本月第几天/总天数）vs 实际完成度：整体（Mac/iPhone 分端，带今日新增）；落后人员只列"姓名：进度 X%"（两端都填才算该人完成一条）；各版本抽检完成度 | CLI 高权限 | `BEEHIVE_WEBHOOK_P2_WORKFLOW`（上线流程验收群） |
| `test_webhook.py` | 手动 | 只测 webhook 连通性 | - | `BEEHIVE_WEBHOOK` |
| `diagnose_at_users.py` | 手动 | 逐人发一条 @ 测试消息，排查哪个人的蜂巢 ID 有问题 | - | `BEEHIVE_WEBHOOK_YANSHOU` |

## 飞书应用与权限

两个自建应用，权限分级使用，不要混用：

- **只读应用**（`FEISHU_APP_ID` / `FEISHU_APP_SECRET`）：`bitable:app:readonly`，用于只读查询脚本
- **高权限 CLI 应用**（`FEISHU_APP_ID_CLI` / `FEISHU_APP_SECRET_CLI`）：`bitable:app` 完整读写，仅用于盲测三件套。`blind_test_tagging.py` 需要读节点更新总表和分工表，所以 CLI 应用也要是这两个文档的协作者
- 应用要访问某个多维表格，必须先在该文档右上角"..."→ 添加文档应用，把应用加为协作者；只有 API 权限不够（缺协作者权限时接口返回非 0 code，脚本抛 RuntimeError，Actions 标红）

## 蜂巢 Webhook 消息格式

支持文本、@消息（`at_text`）、文件、图片四种类型。

```json
{
  "msg_type": "at_text",
  "content": {
    "text": "@张三 消息正文",
    "atUserList": ["蜂巢userid"],
    "atUsersInfo": [{"atUserID": "蜂巢userid", "groupNickname": "张三"}]
  }
}
```

- `atUserList` 里是**蜂巢自己的账号 ID**，不是飞书 open_id（两者都以 `ou` 开头，容易混淆）
- 必须同时带 `atUsersInfo`（含 `groupNickname`），否则不会渲染成蓝色 @（历史踩过的坑）
- `text` 里 `@姓名` 出现的顺序和次数要与传入的 `at_names` 列表一一对应（见 `check_and_notify.py` 的 `send_to_beehive` 注释）
- `PERSON_BEEHIVE_ID` 结构：`{"飞书姓名": {"id": "蜂巢userid", "nickname": "蜂巢显示名"}}`。key 用于匹配飞书数据，`nickname` 是蜂巢真实显示名（例："陈杨"在蜂巢显示为"秦汉"）。人员缺失时 @ **静默失效**，只在日志里打印 ⚠️
- ⚠️ 飞书"人员"字段读出来的是飞书实名（例如验收表的验收人是"郭文博""李雨纯"），而 `PERSON_BEEHIVE_ID` 现有 key 有些是花名/简称（"Webb""雨纯"）。给基于人员字段的消息加 @ 时，先确认 key 与实际读出的姓名一致

## 已知的关键坑（不要重新踩）

1. **日期字段偏 1 小时**：飞书"纯日期"字段的毫秒时间戳按 UTC+8 换算后时钟固定停在 23:00。所有 `parse_date()` 都在换算后再 `+ timedelta(hours=1)`，这是实测规律，不要"修正"成标准时区换算。
2. **GitHub Actions 跑在 UTC**：所有"今天"必须用 `datetime.now(BEIJING_TZ)`，`BEIJING_TZ = timezone(timedelta(hours=8))`。cron 表达式也是 UTC（北京时间 −8 小时）。
3. **YAML 缩进**：`env:` 必须与 `run:` 同级，缩进错会报一连串 "Unrecognized named-value"。
4. **"端"字段取值要核对**：`release_acceptance_summary.py` 的 `PLATFORM_VALUE_MAP` 用的合并选项是 `"Mac/iPhone"`。如果表里实际选项文字不同（例如 `Mac,iPhone`），这类需求会被判为"无法判断"→ 计入待验收、且不进按端统计，不报错。"其他"代表调研/线下方案，统计时一律过滤。
5. **`monthly_blind_test_setup.py` 不是幂等的**：当月表已存在时会复用，但第三步**仍会把全部用例再插入一遍**——同一个月重复手动运行会让验收表记录翻倍。
6. **表名/列名不含年份**：`M{月份}验收表`、`M{月份}人员` 只按月份命名，跨年后会命中去年同月的旧表/旧列。

## 状态持久化

`blind_test_progress_check.py` 读写仓库根目录的 `blind_test_state.json`，按表名存上一次运行的 `mac_done` / `iphone_done`，用于计算整体进度的"今日新增"（与上一次运行比较，同一天手动重跑会显示 0）。对应 yml 最后一步由 `github-actions[bot]` 自动 commit + push。

- **这个文件由脚本维护，不要手动编辑或删除**（删除后下次不显示今日新增）
- **机器人几乎每天都往 main 推提交**，所以用 GitHub Desktop 改代码前必须先点 **Fetch origin → Pull origin**，否则 Push 会被拒绝或产生冲突

## 涉及的飞书表清单

⚠️ 多个不同的 app_token，不是同一个 base，改脚本前先确认对应哪个：

| 表 | app_token | table_id | 关键字段 |
|---|---|---|---|
| 节点更新总表（发布日期数据源） | `IC7ObBQ2ya2H4FsqT3ocPreYned` | `tblSDaRAkltfVtx6` | 版本号/版本发布日期/验收无P0问题（兼容单选"是"或复选框）/ForBud-Mac\|iPhone\|后台已更新/盲测用例已录入/帮助文档已更新/多语言走查（已定义但未参与检查） |
| 分工表（产品部项目管理） | `VhZebH05uaUlEyscWfWc2mMvnhc` | `tbl1Gwx4r7oOcV8g` | 项目说明/产品owner/上线日期/端（单选）/Mac验收结果/iPhone验收结果/后台验收结果（通过/不通过/无需验收） |
| 团队行为记录表（红黑榜） | `G2gsbrQTVaN6XXsNiEkcklvinAh` | `tblQyXjW8i85vfxT` | 类型（红榜/黑榜）/责任人/发生时间 |
| 盲测原始目标表（用例库） | `FnFab3FDKa0JU6sqa19cDVpHnM7` | `tbl46z8GYP5HN1MJ` | 目标/二级模块（单选） |
| 人员名单（轮换表） | `FnFab3FDKa0JU6sqa19cDVpHnM7` | `tblbuVpl9xK3E0Rc` | `M{n}人员` 人员字段列，每月循环左移一位 |
| 验收表（问题记录） | `OR5ubORn3atfo3szLSTcbnVdnTf` | `tbl3vBsukTCg1yrn` | 问题定性（P0-P4）/处理状态/问题提出时间/二级模块 |
| 月度验收表 `M{n}验收表` | 同盲测原始目标表 | 按表名动态查找/创建 | 目标/二级模块/验收人/iPhone验收结果/Mac验收结果（待验收/通过/不通过）/iPhone\|Mac验收时间/本次抽检版本号（多选，值为版本号如 `v1.0.407`；2026-09 之前自动打的是日期；历史表可能是逗号分隔文本，代码两种都兼容） |

飞书建字段的 type 编号：1=文本 3=单选 4=多选 5=日期 11=人员。

## 关键业务规则

- **P0-P4 定义**：P0=功能不可用/安全问题/不解决不能发版；P1=功能可用但逻辑或体验有明显偏差；P2=P1 场景里影响面小或投入产出比不高的；P3=不影响使用或有明确技术限制；P4=暂不修复（无法复现/历史数据）
- **唯一硬卡点是 P0**：必须跟需求组长确认，该判 P0 没反馈出来算产品判断失误，直接上黑榜。其余检查项（ForBud、盲测录入、帮助文档等）超时只记录、不阻断，可能进红黑榜
- **发布节奏不固定**（平均约两周一次，会延期）：一律以节点更新总表为准（见"发布日期的数据源"），改期只改表，不改代码
- **模糊匹配**（二级模块文字是否出现在需求"项目说明"里）是已知的精度权衡。结果明显不合理时，优先怀疑字段名或数据本身，其次才考虑算法

## 命名与配置约定

- GitHub Secrets 按项目/用途命名；新消息该发到哪个群按业务归属选对应 Secret（盲测进度属于上线流程验收，用 `BEEHIVE_WEBHOOK_P2_WORKFLOW`）
- 发 @ 消息前确认 `PERSON_BEEHIVE_ID` 里对应人员的蜂巢 ID 和显示名已配全（且所有副本都已同步）

## 部署后的确认习惯

- 部署后用 `workflow_dispatch` 手动触发，看 Actions 日志，不要等定时任务
- GitHub Desktop 提交后，去 **github.com 网页**上用 `Cmd+F` 搜索确认代码真的更新了（历史上出现过 commit 了但实际没生效）
- 日期/时间相关的 bug，第一反应检查时区处理（见"已知的关键坑" 1、2）
