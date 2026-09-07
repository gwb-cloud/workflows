# -*- coding: utf-8 -*-
"""
盲测完成情况校验脚本
读取当月验收表，检查：
1. 整体覆盖进度（谁的还没做完，快到月底时会加重语气提醒）
2. 每个被打标的发布日期，对应用例的抽检完成度

需要 GitHub Actions Secrets：
- FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI
- BEEHIVE_WEBHOOK_P3_REDBLACK（复用红黑榜那个群，也可以换成别的）
"""

import requests
import os
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import calendar

BEIJING_TZ = timezone(timedelta(hours=8))

STATE_FILE = "blind_test_state.json"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

FEISHU_APP_ID = os.environ["FEISHU_APP_ID_CLI"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET_CLI"]
BEEHIVE_WEBHOOK = os.environ["BEEHIVE_WEBHOOK_P3_REDBLACK"]

BLIND_TEST_APP_TOKEN = "FnFab3FDKa0JU6sqa19cDVpHnM7"

FIELD_OWNER = "验收人"
FIELD_IPHONE_RESULT = "iPhone验收结果"
FIELD_MAC_RESULT = "Mac验收结果"
FIELD_TAG = "本次抽检版本号"

RESULT_DONE_VALUES = {"通过", "不通过"}

# 快到月底（还剩这么多天以内）时，覆盖不足会额外加重提醒语气
MONTH_END_ALERT_DAYS = 5


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


def get_all_tables(token, app_token):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables"
    tables = []
    page_token = None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"拉取表列表失败：{data}")
        tables.extend(data["data"]["items"])
        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
    return tables


def find_table_by_name(token, app_token, table_name):
    for t in get_all_tables(token, app_token):
        if t["name"] == table_name:
            return t["table_id"]
    return None


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


def get_person_name(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return first.get("name", "未知")
    return "未知"


def get_select_value(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
    return ""


def get_text_value(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        parts = []
        for v in value:
            parts.append(v.get("text", "") if isinstance(v, dict) else str(v))
        return "".join(parts)
    return ""


def is_case_done(fields):
    """iPhone和Mac两端结果都填了（通过/不通过），才算这条用例完成"""
    iphone = get_select_value(fields, FIELD_IPHONE_RESULT)
    mac = get_select_value(fields, FIELD_MAC_RESULT)
    return iphone in RESULT_DONE_VALUES and mac in RESULT_DONE_VALUES


def has_issue(fields):
    """任意一端结果是"不通过"，就算这条用例发现了问题"""
    iphone = get_select_value(fields, FIELD_IPHONE_RESULT)
    mac = get_select_value(fields, FIELD_MAC_RESULT)
    return iphone == "不通过" or mac == "不通过"


def send_to_beehive(text):
    resp = requests.post(
        BEEHIVE_WEBHOOK,
        json={"msg_type": "text", "content": {"text": text}},
        timeout=10,
    )
    print(f"发送结果: {resp.status_code} {resp.text}")


def main():
    token = get_tenant_token()
    now = datetime.now(BEIJING_TZ)
    table_name = f"M{now.month}验收表"

    table_id = find_table_by_name(token, BLIND_TEST_APP_TOKEN, table_name)
    if not table_id:
        raise RuntimeError(f"没找到 {table_name}")

    records = get_all_records(token, BLIND_TEST_APP_TOKEN, table_id)
    total = len(records)

    # ===== 检查一：整体覆盖进度（Mac/iPhone分开统计 + 每日增量）=====
    mac_done_count = 0
    iphone_done_count = 0
    owner_pending = defaultdict(int)
    owner_total = defaultdict(int)

    for r in records:
        fields = r["fields"]
        owner = get_person_name(fields, FIELD_OWNER)
        owner_total[owner] += 1
        mac_done = get_select_value(fields, FIELD_MAC_RESULT) in RESULT_DONE_VALUES
        iphone_done = get_select_value(fields, FIELD_IPHONE_RESULT) in RESULT_DONE_VALUES
        if mac_done:
            mac_done_count += 1
        if iphone_done:
            iphone_done_count += 1
        if not (mac_done and iphone_done):
            owner_pending[owner] += 1

    issue_count = sum(1 for r in records if is_case_done(r["fields"]) and has_issue(r["fields"]))

    # 读取上次快照，算今天两端各新增完成了多少
    today_str = now.strftime("%Y-%m-%d")
    state = load_state()
    prev = state.get(table_name)

    mac_delta = mac_done_count - prev["mac_done"] if prev else None
    iphone_delta = iphone_done_count - prev["iphone_done"] if prev else None

    state[table_name] = {"date": today_str, "mac_done": mac_done_count, "iphone_done": iphone_done_count}
    save_state(state)

    last_day = calendar.monthrange(now.year, now.month)[1]
    days_left = last_day - now.day
    is_urgent = days_left <= MONTH_END_ALERT_DAYS

    def platform_line(name, done, delta):
        remaining = total - done
        delta_text = f"，今日新增 {delta}" if delta is not None else "（首次统计，暂无增量）"
        return f"- {name}：已完成 {done}/{total}{delta_text}，剩余 {remaining}"

    lines = [
        f"📋 {table_name} 覆盖进度（本月还剩 {days_left} 天）：",
        platform_line("Mac", mac_done_count, mac_delta),
        platform_line("iPhone", iphone_done_count, iphone_delta),
        f"🐞 已验证中发现问题：{issue_count} 条",
    ]

    pending_owners = sorted(owner_pending.items(), key=lambda x: x[1], reverse=True)
    if pending_owners:
        prefix = "🚨 月底临近，以下人员未完成较多：" if is_urgent else "🟡 未完成情况："
        lines.append(prefix)
        for owner, pending in pending_owners:
            if pending > 0:
                lines.append(f"- {owner}：还剩 {pending}/{owner_total[owner]} 条")

    # ===== 检查二：各发布日期抽检完成度（兼容文本/多选两种字段类型）=====
    tag_records = defaultdict(list)
    for r in records:
        fields = r["fields"]
        raw = fields.get(FIELD_TAG)
        if isinstance(raw, list):  # 多选字段
            tags = [v.get("text", "") if isinstance(v, dict) else str(v) for v in raw]
        else:  # 文本字段（历史表兼容）
            tag_str = get_text_value(fields, FIELD_TAG)
            tags = [t.strip() for t in tag_str.split(",") if t.strip()]
        for tag in tags:
            if tag:
                tag_records[tag].append(fields)

    if tag_records:
        lines.append("📦 各发布日期抽检完成度（完成数/总数，括号内为发现问题数）：")
        for tag, fields_list in sorted(tag_records.items()):
            done = sum(1 for f in fields_list if is_case_done(f))
            tag_issues = sum(1 for f in fields_list if is_case_done(f) and has_issue(f))
            lines.append(f"- {tag}：{done}/{len(fields_list)}（发现问题 {tag_issues} 条）")

    text = "\n".join(lines)
    send_to_beehive(text)
    print(text)


if __name__ == "__main__":
    main()