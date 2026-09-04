# -*- coding: utf-8 -*-
"""
本次发布验收总览脚本（v2）
固定周五发布：每天自动算出"本周五"作为目标发布日期，从周一到周五每天都检查同一批需求，
提前5个工作日开始提醒，而不是只在发布当天检查一次。

统计口径：
1. 按人维度：每个产品owner这次分到几个需求，通过/待验收/不通过 分别几个
2. 按端维度：Mac验收了多少条、iPhone验收了多少条，其中各自多少条不通过（发现问题数）
3. 验收表遗留问题：用"二级模块"关键词模糊匹配本次发布相关的需求，统计验收表里
   处理状态=待修复 的问题数量，按"问题定性"（P0-P4）分类

需要在 GitHub Actions 的 Secrets 里配置：
- FEISHU_APP_ID
- FEISHU_APP_SECRET
- BEEHIVE_WEBHOOK_P2_WORKFLOW

⚠️ 验收表的"二级模块"字段名是按你们其他表的命名习惯猜的，务必去表格里核对一次，
   不一致的话模糊匹配会全部落空（不会报错，只是匹配不到）。
"""

import requests
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
BEEHIVE_WEBHOOK = os.environ["BEEHIVE_WEBHOOK_P2_WORKFLOW"]

# 分工表（产品部项目管理）
RELEASE_APP_TOKEN = "VhZebH05uaUlEyscWfWc2mMvnhc"
RELEASE_TABLE_ID = "tbl1Gwx4r7oOcV8g"

FIELD_PROJECT_DESC = "项目说明"
FIELD_PRODUCT_OWNER = "产品owner"
FIELD_ONLINE_DATE = "上线日期"
FIELD_PLATFORM = "端"                    # 单选：Mac / iPhone / Mac/iPhone / 后台 / 其他

PLATFORM_RESULT_FIELDS = {
    "Mac": "Mac验收结果",
    "iPhone": "iPhone验收结果",
    "后台": "后台验收结果",
}
PLATFORM_VALUE_MAP = {
    "Mac": ["Mac"],
    "iPhone": ["iPhone"],
    "Mac/iPhone": ["Mac", "iPhone"],
    "后台": ["后台"],
}
SKIP_PLATFORM_VALUES = {"其他"}

RESULT_PASS = "通过"
RESULT_FAIL = "不通过"
RESULT_SKIP = "无需验收"

# 验收表（问题记录），用于统计"本次发布相关，还有多少待修复问题"
ISSUE_APP_TOKEN = "OR5ubORn3atfo3szLSTcbnVdnTf"
ISSUE_TABLE_ID = "tbl3vBsukTCg1yrn"
FIELD_ISSUE_TYPE = "问题定性"      # P0-P4
FIELD_ISSUE_STATUS = "处理状态"
FIELD_ISSUE_MODULE = "二级模块"    # ⚠️ 字段名待核对
ISSUE_STATUS_PENDING = "待修复"

# 目标发布日期：默认从分工表里自动找最近的即将到来的上线日期。手动触发测试时可以通过 workflow 输入指定日期，格式 2026-08-21
TARGET_DATE_STR = os.environ.get("TARGET_RELEASE_DATE", "")

# 提前多少个工作日开始提醒（含发布当天本身）
ALERT_WINDOW_WORKDAYS = 5

DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# 产品owner姓名 → 蜂巢账号ID，需要你实际维护补全
PERSON_BEEHIVE_ID = {
    "Webb": "ouv4qovzpupe9m",
    "爱德": "ouv4qovznoxoxq",
    "雨纯": "ouv4qovzoc6cad",
    "苏宸": "ouvkmtuntg5xpk",
    "大力": "ouv4qovzlarm8d",
    "秦汉": "ouviua2rcuub5e",
    "李静": "ouvcofw0bgs9hf",
    "Rhys": "ouv4qovznsatvc",
    "Crisley": "ouv4qow0vt3ksn", 
    "Yvonne": "ouv4qovzmwpkuy",
    "唐炜": "ouv4qovznbncqw",
    "严光": "ouvgwgfyt1elue",
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
        beehive_id = PERSON_BEEHIVE_ID.get(name)
        if beehive_id:
            at_ids.append(beehive_id)
            at_users_info.append({"atUserID": beehive_id, "groupNickname": name})
        else:
            print(f"⚠️ 未找到 {name} 对应的蜂巢账号ID，这处@可能不会生效，请检查 PERSON_BEEHIVE_ID 配置")

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


def find_next_release_date(records, today):
    """从分工表里找最近的即将到来的上线日期（今天或之后，允许1天宽限兼容日期刚过还没更新的过渡态），
    作为下一次要监控的发布目标。取所有候选里最近的一个，不假设固定在周几。"""
    candidate_dates = set()
    for r in records:
        fields = r.get("fields", {})
        platform_value = get_select_value(fields, FIELD_PLATFORM)
        if platform_value in SKIP_PLATFORM_VALUES:
            continue
        online_date = parse_date(fields.get(FIELD_ONLINE_DATE))
        if not online_date:
            continue
        d = online_date.date()
        if d >= today - timedelta(days=1):
            candidate_dates.add(d)
    if not candidate_dates:
        return None
    return min(candidate_dates)


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
    """返回 (总体状态, 涉及的端列表)
    总体状态: '通过' / '不通过' / '未完成' / '无需验收' / '无法判断'"""
    platform_value = get_select_value(fields, FIELD_PLATFORM)
    if not platform_value:
        return "未知", []

    actual_platforms = PLATFORM_VALUE_MAP.get(platform_value)
    if not actual_platforms:
        return "无法判断", [platform_value]

    statuses = {}
    for p in actual_platforms:
        field_name = PLATFORM_RESULT_FIELDS.get(p)
        if field_name:
            statuses[p] = get_select_value(fields, field_name)

    relevant = {p: s for p, s in statuses.items() if s != RESULT_SKIP}
    if not relevant:
        return "无需验收", list(statuses.keys())

    failed = [p for p, s in relevant.items() if s == RESULT_FAIL]
    if failed:
        return "不通过", failed

    pending = [p for p, s in relevant.items() if s != RESULT_PASS]
    if pending:
        return "未完成", pending

    return "通过", []


def compute_platform_stats(matched_fields_list):
    """按端统计：这个端总共需要验多少条、已经验了多少条、其中不通过多少条"""
    stats = {p: {"total": 0, "done": 0, "failed": 0} for p in PLATFORM_RESULT_FIELDS}
    for fields in matched_fields_list:
        platform_value = get_select_value(fields, FIELD_PLATFORM)
        actual_platforms = PLATFORM_VALUE_MAP.get(platform_value, [])
        for p in actual_platforms:
            field_name = PLATFORM_RESULT_FIELDS.get(p)
            if not field_name:
                continue
            result = get_select_value(fields, field_name)
            stats[p]["total"] += 1
            if result in (RESULT_PASS, RESULT_FAIL):
                stats[p]["done"] += 1
            if result == RESULT_FAIL:
                stats[p]["failed"] += 1
    return stats


def get_related_issues(token, feature_descriptions):
    """从验收表里找处理状态=待修复、且二级模块能模糊匹配到本次发布需求的问题"""
    records = get_all_records(token, ISSUE_APP_TOKEN, ISSUE_TABLE_ID)
    matched = []
    for r in records:
        fields = r["fields"]
        status = get_select_value(fields, FIELD_ISSUE_STATUS)
        if status != ISSUE_STATUS_PENDING:
            continue
        module = get_text_value(fields, FIELD_ISSUE_MODULE)
        if module and any(module in desc for desc in feature_descriptions):
            matched.append(fields)
    return matched


def main():
    token = get_tenant_token()
    now = datetime.now(BEIJING_TZ)
    today = now.date()

    records = get_all_records(token, RELEASE_APP_TOKEN, RELEASE_TABLE_ID)
    print(f"分工表共读取到 {len(records)} 条记录")

    if TARGET_DATE_STR:
        target_date = datetime.strptime(TARGET_DATE_STR, "%Y-%m-%d").date()
        print(f"手动指定目标日期: {target_date}")
    else:
        target_date = find_next_release_date(records, today)
        if target_date is None:
            print("分工表里没有找到任何即将到来的上线日期，跳过")
            return
        wd = workdays_between(today, target_date)
        print(f"下一次发布：{target_date}（距今 {wd} 个工作日）")
        if wd is None or wd > ALERT_WINDOW_WORKDAYS:
            print(f"还没进入提前{ALERT_WINDOW_WORKDAYS}个工作日的提醒窗口，跳过")
            return

    matched = []
    for record in records:
        fields = record.get("fields", {})
        raw_online_date = fields.get(FIELD_ONLINE_DATE)
        online_date = parse_date(raw_online_date)

        if DEBUG:
            desc = fields.get(FIELD_PROJECT_DESC, "未命名需求")
            parsed_str = online_date.strftime("%Y-%m-%d %H:%M:%S %Z") if online_date else "解析失败/为空"
            print(f"[DEBUG] {desc} | 上线日期原始值: {raw_online_date} | 解析结果: {parsed_str}")

        if not online_date or online_date.date() != target_date:
            continue
        platform_value = get_select_value(fields, FIELD_PLATFORM)
        if platform_value in SKIP_PLATFORM_VALUES:
            continue
        matched.append(fields)

    if not matched:
        print(f"{target_date} 没有匹配到上线日期为目标日期的需求，跳过")
        return

    # ===== 按人统计 =====
    owner_stats = defaultdict(lambda: {"通过": 0, "待验收": 0, "不通过": 0})
    feature_descriptions = []

    for fields in matched:
        status, _ = evaluate_acceptance(fields)
        if status == "无需验收":
            continue
        owner = get_person_name(fields, FIELD_PRODUCT_OWNER)
        desc = fields.get(FIELD_PROJECT_DESC, "未命名需求")
        feature_descriptions.append(desc)

        if status == "通过":
            owner_stats[owner]["通过"] += 1
        elif status == "不通过":
            owner_stats[owner]["不通过"] += 1
        else:  # 未完成 / 无法判断，统一归入待验收
            owner_stats[owner]["待验收"] += 1

    if not owner_stats:
        print(f"{target_date} 匹配到的需求全部标记为无需验收，跳过发送")
        return

    # ===== 按端统计 =====
    platform_stats = compute_platform_stats(
        [f for f in matched if evaluate_acceptance(f)[0] != "无需验收"]
    )

    # ===== 验收表遗留问题统计 =====
    related_issues = get_related_issues(token, feature_descriptions)
    issue_type_count = defaultdict(int)
    for fields in related_issues:
        issue_type = get_text_value(fields, FIELD_ISSUE_TYPE) or "未分类"
        issue_type_count[issue_type] += 1

    # ===== 拼装消息 =====
    total_features = sum(sum(s.values()) for s in owner_stats.values())
    lines = [f"📋 本次发布（{target_date}）验收总览：共 {total_features} 个需求"]

    lines.append("【按人统计】")
    at_names_in_order = []
    for owner, s in sorted(owner_stats.items()):
        owner_total = sum(s.values())
        detail = f"通过{s['通过']}个"
        if s["待验收"] > 0:
            detail += f"，待验收{s['待验收']}个"
        if s["不通过"] > 0:
            detail += f"，不通过{s['不通过']}个"
        line = f"- {owner}：共{owner_total}个需求，{detail}"
        if s["待验收"] > 0 or s["不通过"] > 0:
            line += f" @{owner}"
            at_names_in_order.append(owner)
        lines.append(line)

    lines.append("【验收进度（按端）】")
    for p in ["Mac", "iPhone", "后台"]:
        s = platform_stats.get(p, {"total": 0, "done": 0, "failed": 0})
        if s["total"] == 0:
            continue
        fail_text = f"，其中不通过 {s['failed']} 条" if s["failed"] > 0 else ""
        lines.append(f"- {p}：已验收 {s['done']}/{s['total']} 条{fail_text}")

    lines.append(f"【验收表遗留问题】（与本次发布相关，处理状态=待修复，共 {len(related_issues)} 个）")
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