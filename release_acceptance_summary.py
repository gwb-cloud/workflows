# -*- coding: utf-8 -*-
"""
本次发布验收总览脚本（v3）
发布日期以"节点更新总表"里维护的「版本发布日期」为准（随时可能改期，脚本每次运行都重新读取）。
下一次发布 = 节点更新总表里最近的、今天或之后的发布日期；距发布≤5个工作日开始每天提醒。
本次发布包含的需求 = 分工表里 上线日期 落在（上一次发布日期, 本次发布日期] 区间内的需求，
这样发布改期后，分工表的上线日期不用跟着逐条改。

统计口径：
验收是否通过只看分工表的「状态」字段（同事不再维护 Mac/iPhone/后台验收结果 三个字段）：
PASS_STATUS_VALUES 算通过，SKIP_STATUS_VALUES 不纳入统计，其余状态（含空）都算待验收。

1. 按人维度：每个产品owner这次分到几个需求，通过/待验收 分别几个
2. 按端维度：按"端"字段拆分，每个端有多少条需求、其中多少条已通过
3. 验收表遗留问题：用"二级模块"关键词模糊匹配本次发布相关的需求，统计验收表里
   处理状态=待修复 的问题数量，按"问题定性"（P0-P4）分类

需要在 GitHub Actions 的 Secrets 里配置：
- FEISHU_APP_ID
- FEISHU_APP_SECRET
- BEEHIVE_WEBHOOK_P2_WORKFLOW

"""

import requests
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
BEEHIVE_WEBHOOK = os.environ["BEEHIVE_WEBHOOK_P2_WORKFLOW"]

# 节点更新总表：发布日期的唯一数据源
NODE_APP_TOKEN = "IC7ObBQ2ya2H4FsqT3ocPreYned"
NODE_TABLE_ID = "tblSDaRAkltfVtx6"
FIELD_NODE_VERSION = "版本号"
FIELD_NODE_RELEASE_DATE = "版本发布日期"

# 分工表（产品部项目管理）
RELEASE_APP_TOKEN = "VhZebH05uaUlEyscWfWc2mMvnhc"
RELEASE_TABLE_ID = "tbl1Gwx4r7oOcV8g"

FIELD_PROJECT_DESC = "项目说明"
FIELD_PRODUCT_OWNER = "产品owner"
FIELD_ONLINE_DATE = "上线日期"
FIELD_PLATFORM = "端"                    # 单选：Mac / iPhone / Mac/iPhone / 后台 / 其他

FIELD_STATUS = "状态"                   # 单选，完整选项见 PASS/SKIP 两个集合及注释

# 算"通过"的状态
PASS_STATUS_VALUES = {"验收通过（等发版）", "已上线Zelto（未对客）", "已上线（对客）"}
# 不纳入本次发布统计的状态（不算通过也不算待验收，不 @ 人）
SKIP_STATUS_VALUES = {"任务完成（无需开发）", "暂时hold", "长期任务"}
# 其余状态都算"待验收"：未开始 / 方案设计中 / 方案待评审 / 评审通过待澄清 / 已澄清待开发 / 开发中 / 验收中 / 空

PLATFORMS = ["Mac", "iPhone", "后台"]
PLATFORM_VALUE_MAP = {
    "Mac": ["Mac"],
    "iPhone": ["iPhone"],
    "Mac/iPhone": ["Mac", "iPhone"],
    "后台": ["后台"],
}
SKIP_PLATFORM_VALUES = {"其他"}

# 验收表（问题记录），用于统计"本次发布相关，还有多少待修复问题"
ISSUE_APP_TOKEN = "OR5ubORn3atfo3szLSTcbnVdnTf"
ISSUE_TABLE_ID = "tbl3vBsukTCg1yrn"
FIELD_ISSUE_TYPE = "问题定性"      # P0-P4
FIELD_ISSUE_STATUS = "处理状态"
FIELD_ISSUE_MODULE = "二级模块"    # 已核对，字段名正确
FIELD_ISSUE_RAISED_TIME = "问题提出时间"
ISSUE_STATUS_PENDING = "待修复"

# 目标发布日期：默认从节点更新总表里自动找最近的即将到来的发布日期。手动触发测试时可以通过 workflow 输入指定日期，格式 2026-08-21
TARGET_DATE_STR = os.environ.get("TARGET_RELEASE_DATE", "")

# 提前多少个工作日开始提醒（含发布当天本身）
ALERT_WINDOW_WORKDAYS = 5

DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# 产品owner姓名（必须跟飞书里显示的名字完全一致，用来匹配数据）→ 蜂巢信息
# 如果这个人在蜂巢里的显示名跟飞书不一样（花名/曾用名不同），nickname 填蜂巢那边真实显示的名字，
# 不要瞎猜，@不生效大概率就是这里对不上
PERSON_BEEHIVE_ID = {
    "Webb": {"id": "ouv4qovzpupe9m", "nickname": "Webb"},
    "爱德": {"id": "ouv4qovznoxoxq", "nickname": "爱德"},
    "雨纯": {"id": "ouv4qovzoc6cad", "nickname": "雨纯"},
    "苏宸": {"id": "ouvkmtuntg5xpk", "nickname": "苏宸"},
    "可大力": {"id": "ouv4qovzlarm8d", "nickname": "可大力"},
    "陈杨": {"id": "ouviua2rcuub5e", "nickname": "秦汉"},  # 飞书叫陈杨，蜂巢显示名是秦汉
    "李静": {"id": "ouvcofw0bgs9hf", "nickname": "李静"},
    "Rhys": {"id": "ouv4qovznsatvc", "nickname": "Rhys"},
    "Crisley": {"id": "ouv4qow0vt3ksn", "nickname": "Crisley"},
    "Yvonne": {"id": "ouv4qovzmwpkuy", "nickname": "Yvonne"},
    "唐炜": {"id": "ouv4qovznbncqw", "nickname": "唐炜"},
    "严光": {"id": "ouvgwgfyt1elue", "nickname": "严光"},
}

# ========== 以下不用改 ==========


def get_tenant_token():
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 token 失败：{data}")
    return data["tenant_access_token"]


def get_all_records(token, app_token, table_id):
    records = []
    page_token = None
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"拉取记录失败：{data}")
        records.extend(data["data"]["items"])
        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
    return records


def send_to_beehive(text, at_names=None):
    at_ids = []
    at_users_info = []
    for name in (at_names or []):
        info = PERSON_BEEHIVE_ID.get(name)
        if info and info.get("id"):
            at_ids.append(info["id"])
            at_users_info.append({"atUserID": info["id"], "groupNickname": info.get("nickname", name)})
        else:
            print(f"⚠️ 未找到 {name} 对应的蜂巢账号信息，这处@可能不会生效，请检查 PERSON_BEEHIVE_ID 配置")

    if at_ids:
        payload = {
            "msg_type": "at_text",
            "content": {"text": text, "atUserList": at_ids, "atUsersInfo": at_users_info},
        }
    else:
        payload = {"msg_type": "text", "content": {"text": text}}

    resp = requests.post(BEEHIVE_WEBHOOK, json=payload, timeout=10)
    print(f"发送结果: {resp.status_code} {resp.text}")


def parse_date(ms_timestamp):
    """飞书日期字段换算成北京时间后固定停在23:00，需要再加1小时才能拿到正确日期"""
    if not ms_timestamp:
        return None
    return datetime.fromtimestamp(ms_timestamp / 1000, tz=BEIJING_TZ) + timedelta(hours=1)


def get_releases(token):
    """读节点更新总表，返回 {发布日期: [版本号, ...]}，没填发布日期的行跳过"""
    releases = defaultdict(list)
    for r in get_all_records(token, NODE_APP_TOKEN, NODE_TABLE_ID):
        fields = r.get("fields", {})
        release_date = parse_date(fields.get(FIELD_NODE_RELEASE_DATE))
        if not release_date:
            continue
        version = fields.get(FIELD_NODE_VERSION)
        version = str(version) if isinstance(version, (int, float)) else get_text_value(fields, FIELD_NODE_VERSION)
        releases[release_date.date()].append(version or "未填版本号")
    return releases


def find_next_release_date(releases, today):
    """节点更新总表里最近的即将到来的发布日期（今天或之后，允许1天宽限兼容发布日刚过的过渡态）。
    跟 blind_test_tagging.py 里的同名函数保持一致，保证两边算出来的是同一次发布。"""
    candidates = [d for d in releases if d >= today - timedelta(days=1)]
    return min(candidates) if candidates else None


def find_previous_release_date(releases, target_date):
    earlier = [d for d in releases if d < target_date]
    return max(earlier) if earlier else None


def belongs_to_release(online_date, prev_release_date, target_date):
    """需求上线日期落在（上一次发布, 本次发布] 区间内就算本次发布的需求；
    节点更新总表里没有更早的发布记录时，退化成上线日期=本次发布日期"""
    if prev_release_date is None:
        return online_date == target_date
    return prev_release_date < online_date <= target_date


def subtract_workdays(end_date, n):
    """从 end_date 往前数 n 个工作日（含 end_date 本身），返回起始日期"""
    d = end_date
    counted = 0
    while counted < n:
        if d.weekday() < 5:
            counted += 1
        if counted == n:
            break
        d -= timedelta(days=1)
    return d


def workdays_between(start_date, end_date):
    """计算 start_date 到 end_date（含首尾）之间有几个工作日（周一到周五）"""
    if end_date < start_date:
        return None
    days = 0
    d = start_date
    while d <= end_date:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def get_person_name(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return first.get("name", "未知")
    if isinstance(value, dict):
        return value.get("name", "未知")
    return "未知"


def get_select_value(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
    return ""


def get_text_value(fields, field_name):
    """兼容文本、多行文本、单选三种返回格式"""
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
    if isinstance(value, list) and value:
        parts = []
        for v in value:
            parts.append(v.get("text", "") if isinstance(v, dict) else str(v))
        return "".join(parts)
    return ""


def evaluate_acceptance(fields):
    """根据「状态」字段返回 '通过' / '待验收' / '不统计'"""
    status = get_select_value(fields, FIELD_STATUS)
    if status in PASS_STATUS_VALUES:
        return "通过"
    if status in SKIP_STATUS_VALUES:
        return "不统计"
    return "待验收"


def compute_platform_stats(matched_fields_list):
    """按端统计：这个端总共有多少条需求、其中多少条已通过（Mac/iPhone 合并选项两端各算一条）"""
    stats = {p: {"total": 0, "done": 0} for p in PLATFORMS}
    for fields in matched_fields_list:
        platform_value = get_select_value(fields, FIELD_PLATFORM)
        for p in PLATFORM_VALUE_MAP.get(platform_value, []):
            stats[p]["total"] += 1
            if evaluate_acceptance(fields) == "通过":
                stats[p]["done"] += 1
    return stats


def get_related_issues(token, feature_descriptions, window_start, window_end):
    """从验收表里找处理状态=待修复、二级模块能模糊匹配到本次发布需求、
    且问题提出时间落在 [window_start, window_end] 窗口内的问题"""
    records = get_all_records(token, ISSUE_APP_TOKEN, ISSUE_TABLE_ID)
    matched = []
    for r in records:
        fields = r["fields"]
        status = get_select_value(fields, FIELD_ISSUE_STATUS)
        if status != ISSUE_STATUS_PENDING:
            continue
        raised_date = parse_date(fields.get(FIELD_ISSUE_RAISED_TIME))
        if not raised_date or not (window_start <= raised_date.date() <= window_end):
            continue
        module = get_text_value(fields, FIELD_ISSUE_MODULE)
        if module and any(module in desc for desc in feature_descriptions):
            matched.append(fields)
    return matched


def main():
    token = get_tenant_token()
    now = datetime.now(BEIJING_TZ)
    today = now.date()

    releases = get_releases(token)
    print(f"节点更新总表共读取到 {len(releases)} 个发布日期")

    records = get_all_records(token, RELEASE_APP_TOKEN, RELEASE_TABLE_ID)
    print(f"分工表共读取到 {len(records)} 条记录")

    if TARGET_DATE_STR:
        target_date = datetime.strptime(TARGET_DATE_STR, "%Y-%m-%d").date()
        print(f"手动指定目标日期: {target_date}")
    else:
        target_date = find_next_release_date(releases, today)
        if target_date is None:
            print("节点更新总表里没有找到任何即将到来的发布日期，跳过")
            return
        wd = workdays_between(today, target_date)
        print(f"下一次发布：{target_date}（距今 {wd} 个工作日）")
        if wd is None or wd > ALERT_WINDOW_WORKDAYS:
            print(f"还没进入提前{ALERT_WINDOW_WORKDAYS}个工作日的提醒窗口，跳过")
            return

    version_text = "/".join(releases.get(target_date, [])) or "节点更新总表中无此日期"
    prev_release_date = find_previous_release_date(releases, target_date)
    print(f"本次发布：{version_text}（{target_date}），上一次发布：{prev_release_date or '无'}")

    matched = []
    for record in records:
        fields = record.get("fields", {})
        raw_online_date = fields.get(FIELD_ONLINE_DATE)
        online_date = parse_date(raw_online_date)

        if DEBUG:
            desc = fields.get(FIELD_PROJECT_DESC, "未命名需求")
            parsed_str = online_date.strftime("%Y-%m-%d %H:%M:%S %Z") if online_date else "解析失败/为空"
            status = get_select_value(fields, FIELD_STATUS) or "空"
            print(f"[DEBUG] {desc} | 上线日期原始值: {raw_online_date} | 解析结果: {parsed_str} | 状态: {status}")

        if not online_date or not belongs_to_release(online_date.date(), prev_release_date, target_date):
            continue
        platform_value = get_select_value(fields, FIELD_PLATFORM)
        if platform_value in SKIP_PLATFORM_VALUES:
            continue
        matched.append(fields)

    if not matched:
        print(f"{target_date} 没有匹配到属于本次发布的需求，跳过")
        return

    # ===== 按人统计 =====
    owner_stats = defaultdict(lambda: {"通过": 0, "待验收": 0})
    feature_descriptions = []
    counted = []

    for fields in matched:
        status = evaluate_acceptance(fields)
        if status == "不统计":
            continue
        counted.append(fields)
        owner = get_person_name(fields, FIELD_PRODUCT_OWNER)
        feature_descriptions.append(fields.get(FIELD_PROJECT_DESC, "未命名需求"))
        owner_stats[owner][status] += 1

    if not owner_stats:
        print(f"{target_date} 匹配到的需求状态全部是 {'/'.join(sorted(SKIP_STATUS_VALUES))}，跳过发送")
        return

    # ===== 按端统计 =====
    platform_stats = compute_platform_stats(counted)

    # ===== 验收表遗留问题统计 =====
    window_start = subtract_workdays(target_date, ALERT_WINDOW_WORKDAYS)
    related_issues = get_related_issues(token, feature_descriptions, window_start, target_date)
    issue_type_count = defaultdict(int)
    for fields in related_issues:
        issue_type = get_text_value(fields, FIELD_ISSUE_TYPE) or "未分类"
        issue_type_count[issue_type] += 1

    # ===== 拼装消息 =====
    total_features = sum(sum(s.values()) for s in owner_stats.values())
    lines = [f"📋 本次发布（{version_text}，{target_date}）验收总览：共 {total_features} 个需求"]

    lines.append("【按人统计】")
    at_names_in_order = []
    for owner, s in sorted(owner_stats.items()):
        owner_total = sum(s.values())
        detail = f"通过{s['通过']}个"
        if s["待验收"] > 0:
            detail += f"，待验收{s['待验收']}个"
        line = f"- {owner}：共{owner_total}个需求，{detail}"
        if s["待验收"] > 0:
            line += f" @{owner}"
            at_names_in_order.append(owner)
        lines.append(line)

    lines.append("【验收进度（按端）】")
    for p in PLATFORMS:
        s = platform_stats[p]
        if s["total"] == 0:
            continue
        lines.append(f"- {p}：已验收通过 {s['done']}/{s['total']} 条")

    lines.append(f"【验收表遗留问题】（{window_start}~{target_date}期间提出，与本次发布相关，处理状态=待修复，共 {len(related_issues)} 个）")
    if issue_type_count:
        for issue_type, count in sorted(issue_type_count.items()):
            lines.append(f"- {issue_type}：{count}个")
    else:
        lines.append("- 无")

    text = "\n".join(lines)
    send_to_beehive(text, at_names=at_names_in_order)
    print(text)


if __name__ == "__main__":
    main()
