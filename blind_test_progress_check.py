# -*- coding: utf-8 -*-
"""
盲测完成情况校验脚本
读取当月验收表，检查：
1. 整体覆盖进度（谁的还没做完，快到月底时会加重语气提醒）
2. 每个被打标的发布日期，对应用例的抽检完成度

需要 GitHub Actions Secrets：
- FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI
- BEEHIVE_WEBHOOK_MANGCE   （验收群的【盲测】机器人，跟截图校验同一个）
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

# 周六周日不发进度提醒（.yml 的 cron 已经限定周一到周五，这里再兜一层，
# 防止 GitHub Actions 延迟跨天把周五的任务拖到周六才跑）。手动触发时传 FORCE_SEND=true 可强制发送
FORCE_SEND = os.environ.get("FORCE_SEND", "").strip().lower() == "true"

FEISHU_APP_ID = os.environ["FEISHU_APP_ID_CLI"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET_CLI"]
BEEHIVE_WEBHOOK = os.environ["BEEHIVE_WEBHOOK_MANGCE"]

BLIND_TEST_APP_TOKEN = "FnFab3FDKa0JU6sqa19cDVpHnM7"

# 节点更新总表：用来把发布日期换算成"本月第几次发版"
NODE_APP_TOKEN = "IC7ObBQ2ya2H4FsqT3ocPreYned"
NODE_TABLE_ID = "tblSDaRAkltfVtx6"
FIELD_NODE_VERSION = "版本号"
FIELD_NODE_RELEASE_DATE = "版本发布日期"

# 每次发版的抽检结果列（跟 blind_test_tagging.py 保持一致，改名要两边一起改）
ROUND_MAC_FIELD = "第{n}次发版-Mac结果"
ROUND_IPHONE_FIELD = "第{n}次发版-iPhone结果"

# 抽检轮次的结果回填到基础结果列（基础列为空时才写），避免验收人同一条填两遍
BACKFILL = os.environ.get("BACKFILL", "true").strip().lower() != "false"

FIELD_OWNER = "验收人"
FIELD_IPHONE_RESULT = "iPhone验收结果"
FIELD_MAC_RESULT = "Mac验收结果"
FIELD_TAG = "本次抽检版本号"

# 「有值即有效」：两端进度/结论常常不一致，值不可能全是通过/不通过，
# 所以只要结果字段非空就算这一端已验收，只有空值和"待验收"算没做
RESULT_PENDING_VALUES = {"", "待验收"}


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


def parse_date(ms_timestamp):
    """飞书日期字段换算成北京时间后固定停在23:00，需要再加1小时才能拿到正确日期"""
    if not ms_timestamp:
        return None
    return datetime.fromtimestamp(ms_timestamp / 1000, tz=BEIJING_TZ) + timedelta(hours=1)


def get_month_releases(token, year, month):
    """当月的发布：返回 [(轮次, 版本号文案, 发布日期), ...]，按日期排序"""
    by_date = defaultdict(list)
    for r in get_all_records(token, NODE_APP_TOKEN, NODE_TABLE_ID):
        fields = r["fields"]
        d = parse_date(fields.get(FIELD_NODE_RELEASE_DATE))
        if not d or d.year != year or d.month != month:
            continue
        version = fields.get(FIELD_NODE_VERSION)
        version = str(version) if isinstance(version, (int, float)) else get_text_value(fields, FIELD_NODE_VERSION)
        if version:
            by_date[d.date()].append(version)
    return [(i + 1, "/".join(by_date[d]), d) for i, d in enumerate(sorted(by_date))]


def get_table_field_names(token, app_token, table_id):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    resp = requests.get(url, headers=headers, params={"page_size": 100}, timeout=15)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"拉取字段失败：{data}")
    return {f["field_name"] for f in data["data"]["items"]}


def batch_update(token, app_token, table_id, updates, chunk_size=450):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
    headers = {"Authorization": f"Bearer {token}"}
    done = 0
    for i in range(0, len(updates), chunk_size):
        chunk = updates[i:i + chunk_size]
        resp = requests.post(url, headers=headers, json={"records": chunk}, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"回填失败（已写 {done} 条）：{data}")
        done += len(chunk)
    return done


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


def is_result_done(fields, field_name):
    """这一端是否已验收：结果字段有值（且不是"待验收"）就算"""
    return get_select_value(fields, field_name).strip() not in RESULT_PENDING_VALUES


def is_case_done(fields):
    """iPhone和Mac两端都填了结果，才算这条用例整体完成"""
    return is_result_done(fields, FIELD_IPHONE_RESULT) and is_result_done(fields, FIELD_MAC_RESULT)


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
    now = datetime.now(BEIJING_TZ)
    if now.weekday() >= 5 and not FORCE_SEND:
        print(f"今天是{'周六' if now.weekday() == 5 else '周日'}（北京时间 {now:%Y-%m-%d %H:%M}），跳过进度提醒。"
              f"需要强制发送时，手动触发并把 force_send 设为 true")
        return

    token = get_tenant_token()
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
    # 个人进度按两端分别算：一条用例算2个待办（Mac一个、iPhone一个），
    # 两端进度不一致时不会被慢的那端拖死
    owner_done = defaultdict(int)
    owner_total = defaultdict(int)

    for r in records:
        fields = r["fields"]
        owner = get_person_name(fields, FIELD_OWNER)
        owner_total[owner] += 2
        mac_done = is_result_done(fields, FIELD_MAC_RESULT)
        iphone_done = is_result_done(fields, FIELD_IPHONE_RESULT)
        if mac_done:
            mac_done_count += 1
            owner_done[owner] += 1
        if iphone_done:
            iphone_done_count += 1
            owner_done[owner] += 1

    issue_count = sum(1 for r in records if is_case_done(r["fields"]) and has_issue(r["fields"]))

    # 每日快照，用于整体进度的"今日新增"
    today_str = now.strftime("%Y-%m-%d")
    state = load_state()
    prev = state.get(table_name)
    mac_delta = mac_done_count - prev["mac_done"] if prev else None
    iphone_delta = iphone_done_count - prev["iphone_done"] if prev else None

    state[table_name] = {
        "date": today_str,
        "mac_done": mac_done_count,
        "iphone_done": iphone_done_count,
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
        for owner, _, _, actual_pct in behind_owners:
            lines.append(f"- {owner}：进度 {actual_pct:.0f}%")
    else:
        lines.append("✅ 所有人完成度均达到预期进度")

    # ===== 检查二：各次发版的抽检完成度 =====
    # 一个月多次发版，结果各自记在"第N次发版-Mac/iPhone结果"列里，互不覆盖
    field_names = get_table_field_names(token, BLIND_TEST_APP_TOKEN, table_id)
    rounds = get_month_releases(token, now.year, now.month)
    backfill_updates = []

    round_lines = []
    for round_no, version_text, release_date in rounds:
        mac_field = ROUND_MAC_FIELD.format(n=round_no)
        iphone_field = ROUND_IPHONE_FIELD.format(n=round_no)
        if mac_field not in field_names and iphone_field not in field_names:
            continue   # 这一轮还没开始打标

        scope = done = issues = 0
        for r in records:
            fields = r["fields"]
            mac = get_select_value(fields, mac_field).strip()
            iphone = get_select_value(fields, iphone_field).strip()
            if not mac and not iphone:
                continue   # 不在本轮抽检范围
            scope += 1
            mac_done = mac not in RESULT_PENDING_VALUES
            iphone_done = iphone not in RESULT_PENDING_VALUES
            if mac_done and iphone_done:
                done += 1
                if mac == "不通过" or iphone == "不通过":
                    issues += 1
            # 本轮验过的结果回填到基础列（基础列为空时才写），省得同一条填两遍
            if BACKFILL:
                write = {}
                if mac_done and not is_result_done(fields, FIELD_MAC_RESULT):
                    write[FIELD_MAC_RESULT] = mac
                if iphone_done and not is_result_done(fields, FIELD_IPHONE_RESULT):
                    write[FIELD_IPHONE_RESULT] = iphone
                if write:
                    backfill_updates.append({"record_id": r["record_id"], "fields": write})

        pct = done / scope * 100 if scope else 0
        round_lines.append(f"- 第{round_no}次发版（{version_text}，{release_date:%m-%d}）："
                           f"{done}/{scope}（{pct:.0f}%，发现问题 {issues} 条）")

    if round_lines:
        lines.append("📦 本月各次发版抽检完成度：")
        lines.extend(round_lines)

    if backfill_updates:
        written = batch_update(token, BLIND_TEST_APP_TOKEN, table_id, backfill_updates)
        print(f"已把 {written} 条抽检结果回填到基础验收结果列")

    text = "\n".join(lines)
    send_to_beehive(text)
    print(text)


if __name__ == "__main__":
    main()