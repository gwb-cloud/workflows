# -*- coding: utf-8 -*-
"""
本次发布验收总览脚本
功能：读取"产品部项目管理"分工表，筛选上线日期等于目标发布日期的所有需求，
      统计验收完成情况（产品owner是否已介入验证），未验收的@对应产品owner提醒。

需要在 GitHub Actions 的 Secrets 里配置：
- FEISHU_APP_ID
- FEISHU_APP_SECRET
- BEEHIVE_WEBHOOK_P2_WORKFLOW

⚠️ 下面两个字段名在你发的截图里是被截断显示的，务必去表格里核对一次完整、准确的字段名，
   跟表格里实际的不一致会导致读不到数据（不会报错，只会静默漏检）：
   - FIELD_OWNER_VERIFY_TIME
   - FIELD_REVIEW_DATE（当前脚本逻辑没直接用到这个，先留着，后面要扩展评审环节检查时会用到）
"""

import requests
import os
from datetime import datetime

# ========== 配置区 ==========

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
BEEHIVE_WEBHOOK = os.environ["BEEHIVE_WEBHOOK_P2_WORKFLOW"]

# 分工表（产品部项目管理）的 app_token 和 table_id
TABLE_APP_TOKEN = "VhZebH05uaUlEyscWfWc2mMvnhc"
TABLE_ID = "tbl1Gwx4r7oOcV8g"

FIELD_PROJECT_DESC = "项目说明"
FIELD_PRODUCT_OWNER = "产品owner"
FIELD_ONLINE_DATE = "上线日期"
FIELD_PLATFORM = "端"                    # 单选字段：Mac / iPhone / Mac/iPhone / 后台 / 其他

# 按端拆分的验收结果字段，跟 ForBud 三份文档的划分对应
PLATFORM_RESULT_FIELDS = {
    "Mac": "Mac验收结果",
    "iPhone": "iPhone验收结果",
    "后台": "后台验收结果",
}

# "端"字段的选项值 → 实际要检查哪些验收结果字段。
# "Mac/iPhone"这种合并选项要拆成两个都检查；"其他"没有对应的结果字段，无法自动判断。
# "端"字段的选项值 → 实际要检查哪些验收结果字段。
# "Mac/iPhone"这种合并选项要拆成两个都检查。
# "其他"/"无需验收"不会走到这里，在筛选阶段已经被 SKIP_PLATFORM_VALUES 排除掉了。
PLATFORM_VALUE_MAP = {
    "Mac": ["Mac"],
    "iPhone": ["iPhone"],
    "Mac/iPhone": ["Mac", "iPhone"],
    "后台": ["后台"],
}

# "端"字段里，这个取值代表跟发版验收无关的任务（调研、线下方案等），
# 直接跳过，不计入统计、不触发提醒
SKIP_PLATFORM_VALUES = {"其他"}

RESULT_PASS = "通过"
RESULT_FAIL = "不通过"
RESULT_SKIP = "无需验收"

# 目标发布日期：默认取今天。手动触发测试时可以通过 workflow 输入指定日期，格式 2026-08-21
TARGET_DATE_STR = os.environ.get("TARGET_RELEASE_DATE", "")

# 调试模式：打印每条记录"上线日期"字段的原始值和解析结果，排查匹配不上的问题时打开
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


def get_records(token):
    records = []
    page_token = None
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{TABLE_APP_TOKEN}/tables/{TABLE_ID}/records"
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
    """发送消息。text 里若已包含"@姓名"行内占位，at_names 需要按出现顺序传入对应姓名列表。"""
    at_ids = []
    for name in (at_names or []):
        beehive_id = PERSON_BEEHIVE_ID.get(name)
        if beehive_id:
            at_ids.append(beehive_id)
        else:
            print(f"⚠️ 未找到 {name} 对应的蜂巢账号ID，这处@可能不会生效，请检查 PERSON_BEEHIVE_ID 配置")

    if at_ids:
        payload = {"msg_type": "at_text", "content": {"text": text, "atUserList": at_ids}}
    else:
        payload = {"msg_type": "text", "content": {"text": text}}

    resp = requests.post(BEEHIVE_WEBHOOK, json=payload, timeout=10)
    print(f"发送结果: {resp.status_code} {resp.text}")


from datetime import timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))


def parse_date(ms_timestamp):
    """飞书日期字段返回的是毫秒级时间戳，统一按北京时间解析。
    不能用系统本地时区解读——GitHub Actions 跑在 UTC，直接用 fromtimestamp()
    会把"北京时间当天0点"解析成前一天，导致日期匹配全部错位。"""
    if not ms_timestamp:
        return None
    return datetime.fromtimestamp(ms_timestamp / 1000, tz=BEIJING_TZ)


def get_person_name(fields, field_name):
    """人员字段通常返回 [{"name": "张三", "id": "ou_xxx"}]"""
    value = fields.get(field_name)
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return first.get("name", "未知")
    if isinstance(value, dict):
        return value.get("name", "未知")
    return "未知"


def get_multi_select_values(fields, field_name):
    """多选字段，兼容字符串列表 / [{"text":...}] 两种返回格式"""
    value = fields.get(field_name)
    if not isinstance(value, list):
        return []
    result = []
    for v in value:
        if isinstance(v, dict):
            result.append(v.get("text", ""))
        else:
            result.append(str(v))
    return result


def get_select_value(fields, field_name):
    """单选字段，兼容字符串 / {"text":...} 两种返回格式"""
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
    return ""


def evaluate_acceptance(fields):
    """按"端"字段的单选值，映射到实际要检查的验收结果字段，返回 (总体状态, 涉及的端列表)
    总体状态: '通过' / '不通过' / '未完成' / '无需验收' / '无法判断'

    某个端的验收结果字段若填了"无需验收"，该端会被排除，不参与判断；
    如果涉及的端全部都是"无需验收"，整条需求返回'无需验收'。
    '无法判断'只在"端"出现配置里没覆盖到的新选项值时触发，是兜底提醒。"""
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

    # 先排除标记为"无需验收"的端，不参与后续判断
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


def main():
    token = get_tenant_token()
    records = get_records(token)

    if TARGET_DATE_STR:
        target_date = datetime.strptime(TARGET_DATE_STR, "%Y-%m-%d").date()
    else:
        target_date = datetime.now(BEIJING_TZ).date()

    print(f"目标日期: {target_date}")
    print(f"共读取到 {len(records)} 条记录")

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
            continue  # 调研/线下方案等跟发版验收无关，直接跳过
        matched.append(fields)

    if not matched:
        print(f"{target_date} 没有匹配到上线日期为今天的需求，跳过")
        return

    total = 0
    passed, failed, pending, unclassified = [], [], [], []

    for fields in matched:
        desc = fields.get(FIELD_PROJECT_DESC, "未命名需求")
        owner = get_person_name(fields, FIELD_PRODUCT_OWNER)
        status, related_platforms = evaluate_acceptance(fields)

        if status == "无需验收":
            continue  # 涉及的端全部标记无需验收，等同于这条需求不用验收，不计入统计

        total += 1
        if status == "通过":
            passed.append(desc)
        elif status == "不通过":
            failed.append((desc, owner, related_platforms))
        elif status == "无法判断":
            unclassified.append((desc, owner, related_platforms))
        else:
            pending.append((desc, owner, related_platforms))

    if total == 0:
        print(f"{target_date} 匹配到的需求全部标记为无需验收，跳过发送")
        return

    lines = [
        f"📋 本次发布（{target_date}）验收总览：",
        f"共 {total} 个需求，通过 {len(passed)} 个，不通过 {len(failed)} 个，"
        f"未完成验收 {len(pending)} 个，无法自动判断 {len(unclassified)} 个",
    ]

    at_names_in_order = []

    if failed:
        lines.append("🔴 验收不通过：")
        for desc, owner, platforms in failed:
            lines.append(f"- {desc}（{'/'.join(platforms)}不通过） @{owner}")
            at_names_in_order.append(owner)

    if pending:
        lines.append("🟡 未完成验收：")
        for desc, owner, platforms in pending:
            lines.append(f"- {desc}（{'/'.join(platforms)}待验收） @{owner}")
            at_names_in_order.append(owner)

    if unclassified:
        lines.append("⚪ 端字段无法自动判断，需人工确认归属：")
        for desc, owner, platforms in unclassified:
            lines.append(f"- {desc}（端=\"{'/'.join(platforms)}\"） @{owner}")
            at_names_in_order.append(owner)

    text = "\n".join(lines)
    send_to_beehive(text, at_names=at_names_in_order)
    print(text)


if __name__ == "__main__":
    main()