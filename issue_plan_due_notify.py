# -*- coding: utf-8 -*-
"""问题池「计划修复时间」到期 / 逾期提醒 —— 发研发群，按人聚合

和飞书自己那条自动化（workflow_id=wkfvcwdNMvZZ40Mc）的分工：

  飞书自动化  一条记录私聊一次，到期当天 09:00，不经过这个仓库
  这个脚本    每天 09:30 往研发群发一条，按人聚合成"你有 N 条"，
              并且额外报已经逾期的（飞书那条只管当天，不管欠的）

两条都留着是刻意的：私聊保证个人看得见，群消息保证进度对所有人可见。
计划修复时间被批量填成同一天的时候（比如 2026-11-19 一天 119 条），
私聊会炸，群里这条反而是人均一行，更好看。

需要 GitHub Actions Secrets：
- FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI
- BEEHIVE_WEBHOOK_DEV（研发群）
"""

import requests
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from beehive_people import dev_mention, dev_beehive_id, dev_beehive_send

BEIJING_TZ = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID_CLI"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET_CLI"]

ISSUE_APP_TOKEN = "OR5ubORn3atfo3szLSTcbnVdnTf"
ISSUE_TABLE_ID = "tbl3vBsukTCg1yrn"
ISSUE_TABLE_URL = (f"https://pcnpuds47gj5.feishu.cn/base/{ISSUE_APP_TOKEN}"
                   f"?table={ISSUE_TABLE_ID}&view=vewmkD3ctI")

# 这几个状态算"还没修完"，到期了要催
UNFINISHED = {"待修复", "验证未通过待修复", "开发中"}

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() == "true"
# 周末默认不发，填 true 强制发
FORCE_SEND = os.environ.get("FORCE_SEND", "").strip().lower() == "true"
# 每人最多列几个主键
MAX_KEYS = int(os.environ.get("MAX_KEYS", "").strip() or 10)

RETRYABLE_CODES = {1254290, 1254291, 1254607, 1255001, 1255040, 99991400}


def api(method, url, token=None, params=None, body=None, timeout=20, tries=4):
    headers = {"Authorization": f"Bearer {token}"} if token else None
    delay = 2
    for attempt in range(1, tries + 1):
        reason = None
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=body, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504):
                reason = f"HTTP {resp.status_code}"
            else:
                data = resp.json()
                code = data.get("code")
                if code == 0:
                    return data
                if code in RETRYABLE_CODES:
                    reason = f"飞书 code={code} {data.get('msg')}"
                else:
                    return data
        except requests.RequestException as e:
            reason = f"请求异常 {e}"
        if attempt == tries:
            raise RuntimeError(f"请求失败（已重试{tries}次）：{url.split('/open-apis/')[-1]} → {reason}")
        print(f"⚠️ 第{attempt}次失败（{reason}），{delay}s 后重试")
        time.sleep(delay)
        delay *= 2


def get_tenant_token():
    data = api("POST", "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
               body={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET})
    if data.get("code") != 0:
        raise RuntimeError(f"获取 token 失败（检查 FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI）：{data}")
    return data["tenant_access_token"]


def get_all_records(token, app_token, table_id):
    records, page_token = [], None
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = api("GET", url, token=token, params=params)
        if data.get("code") != 0:
            raise RuntimeError(f"拉取记录失败：{data}")
        records.extend(data["data"]["items"])
        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
    return records


def text_of(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("name") or value.get("text") or "").strip()
    if isinstance(value, (list, tuple)):
        return text_of(value[0]) if value else ""
    return str(value)


def sel(rec, name):
    return text_of(rec.get("fields", {}).get(name))


def as_date(rec, name):
    """日期字段是毫秒时间戳。飞书纯日期换算到北京时间会停在前一天23:00，要补1小时"""
    value = rec.get("fields", {}).get(name)
    if not isinstance(value, (int, float)) or not value:
        return None
    return (datetime.fromtimestamp(value / 1000, tz=BEIJING_TZ) + timedelta(hours=1)).date()


def owners(rec):
    value = rec.get("fields", {}).get("技术负责人")
    if not isinstance(value, (list, tuple)):
        return []
    return [text_of(v) for v in value if text_of(v)]


def key_of(rec):
    return text_of(rec.get("fields", {}).get("主键")) or rec.get("record_id", "?")


def is_blank(rec, name):
    value = rec.get("fields", {}).get(name)
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return not value
    return False


def group_by_owner(records):
    """人 → 主键列表；没填技术负责人的归到 None"""
    out = defaultdict(list)
    for r in records:
        names = owners(r)
        if names:
            for n in names:
                out[n].append(key_of(r))
        else:
            out[None].append(key_of(r))
    return out


def render(title, records, lines, at_names):
    """把一组记录按人聚合成若干行，同时把要 @ 的人按出现顺序塞进 at_names"""
    if not records:
        return
    grouped = group_by_owner(records)
    unassigned = grouped.pop(None, [])
    lines.append(f"\n{title}（{len(records)} 条）：")
    for name, keys in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        shown = "/".join(keys[:MAX_KEYS]) + (f" 另有{len(keys) - MAX_KEYS}条" if len(keys) > MAX_KEYS else "")
        lines.append(f"- {dev_mention(name)}：{len(keys)} 条｜主键 {shown}")
        if dev_beehive_id(name):
            at_names.append(name)
    if unassigned:
        shown = "/".join(unassigned[:MAX_KEYS]) + (f" 另有{len(unassigned) - MAX_KEYS}条" if len(unassigned) > MAX_KEYS else "")
        lines.append(f"- ⚠️ 没填技术负责人，没人会收到提醒：{len(unassigned)} 条｜主键 {shown}")


def main():
    now = datetime.now(BEIJING_TZ)
    today = now.date()
    if now.weekday() >= 5 and not FORCE_SEND:
        print(f"今天是周{now.weekday() + 1}，不发研发群（要发填 force_send=true）")
        return

    token = get_tenant_token()
    records = get_all_records(token, ISSUE_APP_TOKEN, ISSUE_TABLE_ID)
    print(f"共 {len(records)} 条记录")

    due_today, overdue, unplanned = [], [], []
    for r in records:
        if sel(r, "处理状态") not in UNFINISHED:
            continue
        # 已进 Sprint 却没配技术负责人 / 没排计划修复时间 —— 这类谁都催不到：
        # 飞书的到期提醒按技术负责人发，没人就不发；没有计划修复时间更是连到期都算不出来。
        # 问题池周报也会报一份给问题负责人，两边都报是故意的。
        if sel(r, "进Sprint解决").startswith("SP") and (
                is_blank(r, "技术负责人") or is_blank(r, "计划修复时间")):
            unplanned.append(r)
        plan = as_date(r, "计划修复时间")
        if not plan:
            continue
        if plan == today:
            due_today.append(r)
        elif plan < today:
            overdue.append(r)

    print(f"今天到期 {len(due_today)} 条，已逾期 {len(overdue)} 条，"
          f"进Sprint但没配人/没排期 {len(unplanned)} 条")
    if not due_today and not overdue and not unplanned:
        print("没有要催的，不发送")
        return

    lines = [f"🛠 修复进度提醒｜{now:%m-%d}"]
    at_names = []
    render("📌 今天到期", due_today, lines, at_names)
    render("🔴 已经逾期", overdue, lines, at_names)
    if unplanned:
        lines.append(f"\n⚠️ 已进 Sprint 但没配技术负责人或没排计划修复时间：{len(unplanned)} 条")
        miss_owner = [r for r in unplanned if is_blank(r, "技术负责人")]
        miss_plan = [r for r in unplanned if is_blank(r, "计划修复时间")]
        for label, group in (("没配技术负责人", miss_owner), ("没排计划修复时间", miss_plan)):
            if group:
                keys = [key_of(r) for r in group[:MAX_KEYS]]
                more = f" 另有{len(group) - MAX_KEYS}条" if len(group) > MAX_KEYS else ""
                lines.append(f"- {label}：{len(group)} 条｜主键 {'/'.join(keys)}{more}")
    lines.append("\n修完记得把「处理状态」改成已修复，并填上「上线日期」。")
    lines.append(f"📋 问题池：{ISSUE_TABLE_URL}")

    text = "\n".join(lines)
    print(text)
    if DRY_RUN:
        print(f"\n== DRY RUN ==（不发送）\n待 @ 的人：{at_names}")
        return
    dev_beehive_send(text, at_names)


if __name__ == "__main__":
    main()
