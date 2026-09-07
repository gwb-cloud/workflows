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

    # ===== 时间进度基准线：今天是本月第几天 / 本月共几天 =====
    last_day = calendar.monthrange(now.year, now.month)[1]
    elapsed_days = now.day
    expected_pct = elapsed_days / last_day * 100

    # ===== 按人、按端统计完成情况 =====
    mac_done_count = 0
    iphone_done_count = 0
    owner_done = defaultdict(int)   # 两端都完成才算这个人的一条
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
        if mac_done and iphone_done:
            owner_done[owner] += 1

    issue_count = sum(1 for r in records if is_case_done(r["fields"]) and has_issue(r["fields"]))

    # 每日快照，用于"今日新增"这个辅助信息（整体 + 按人）
    today_str = now.strftime("%Y-%m-%d")
    state = load_state()
    prev = state.get(table_name)
    mac_delta = mac_done_count - prev["mac_done"] if prev else None
    iphone_delta = iphone_done_count - prev["iphone_done"] if prev else None
    prev_owner_done = prev.get("owner_done", {}) if prev else {}

    state[table_name] = {
        "date": today_str,
        "mac_done": mac_done_count,
        "iphone_done": iphone_done_count,
        "owner_done": dict(owner_done),
    }
    save_state(state)

    lines = [f"📋 {table_name} 进度检查（本月第 {elapsed_days}/{last_day} 天，预期进度 {expected_pct:.1f}%）"]

    lines.append("【整体进度】")
    for name, done in [("Mac", mac_done_count), ("iPhone", iphone_done_count)]:
        actual_pct = done / total * 100 if total else 0
        delta = mac_delta if name == "Mac" else iphone_delta
        delta_text = f"，今日新增{delta}" if delta is not None else ""
        flag = f" 🚨落后预期{expected_pct - actual_pct:.1f}pt" if actual_pct < expected_pct else " ✅达标"
        lines.append(f"- {name}：完成 {done}/{total}（{actual_pct:.1f}%{delta_text}）{flag}")

    lines.append(f"🐞 已验证中发现问题：{issue_count} 条")

    # ===== 未达标人员：完成度低于预期时间进度的人 =====
    behind_owners = []
    for owner, assigned in owner_total.items():
        done = owner_done.get(owner, 0)
        actual_pct = done / assigned * 100 if assigned else 0
        if actual_pct < expected_pct:
            behind_owners.append((owner, done, assigned, actual_pct))

    if behind_owners:
        behind_owners.sort(key=lambda x: x[3])  # 完成度最低的排前面
        lines.append(f"🚨 完成度低于预期进度（{expected_pct:.1f}%）的人员：")
        for owner, done, assigned, actual_pct in behind_owners:
            remaining = assigned - done
            prev_done = prev_owner_done.get(owner)
            delta_text = f"{done - prev_done}个" if prev_done is not None else "0个（首次统计）"
            lines.append(f"- {owner}：今日完成 {delta_text}，剩余 {remaining}/{assigned}，进度{actual_pct:.0f}%")
    else:
        lines.append("✅ 所有人完成度均达到预期进度")

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