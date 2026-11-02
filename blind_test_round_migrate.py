# -*- coding: utf-8 -*-
"""
月度验收表"分轮结果列"准备/迁移脚本（一次性任务，按月手动触发）

抽检结果改成按"本月第N次发版"分列统计之后，当月验收表需要有「第N次发版-Mac结果」
「第N次发版-iPhone结果」两列。这个脚本做三件事：

1. 体检：把当月验收表的列结构、记录条数、「本次抽检版本号」分布、基础结果列填写情况打出来
2. 建列：按节点更新总表里当月的发布次数，补齐「第N次发版-Mac/iPhone结果」
3. 迁移：把已经在基础列（Mac验收结果/iPhone验收结果）里填好的结果，复制到指定某一轮的列里，
   并把该轮抽中但还没验的行置为"待验收"

默认 DRY RUN，只读不写；确认计划没问题后，手动触发时把 confirm 填 yes 才会真正写入。

需要 GitHub Actions Secrets：FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI
"""

import requests
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID_CLI"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET_CLI"]

# 节点更新总表：发布日期的唯一数据源
NODE_APP_TOKEN = "IC7ObBQ2ya2H4FsqT3ocPreYned"
NODE_TABLE_ID = "tblSDaRAkltfVtx6"
FIELD_NODE_VERSION = "版本号"
FIELD_NODE_RELEASE_DATE = "版本发布日期"

BLIND_TEST_APP_TOKEN = "FnFab3FDKa0JU6sqa19cDVpHnM7"
FIELD_TAG = "本次抽检版本号"
FIELD_MAC_RESULT = "Mac验收结果"
FIELD_IPHONE_RESULT = "iPhone验收结果"

# 列名模板要和 blind_test_tagging.py / blind_test_progress_check.py 保持一致
ROUND_MAC_FIELD = "第{n}次发版-Mac结果"
ROUND_IPHONE_FIELD = "第{n}次发版-iPhone结果"
ROUND_RESULT_OPTIONS = [{"name": "待验收"}, {"name": "通过"}, {"name": "不通过"}]
PENDING_VALUES = {"", "待验收"}

# 要处理哪个月的表，格式 2026-11，留空取当前北京时间所在月
TARGET_MONTH = os.environ.get("TARGET_MONTH", "").strip()
# 要补哪几轮的结果列，逗号分隔，比如 1,2；留空则按节点更新总表里当月的发布次数全补
CREATE_ROUNDS = os.environ.get("CREATE_ROUNDS", "").strip()
# 把基础列里已有的结果迁进第几轮；留空 = 不迁移，只体检+建列
MIGRATE_ROUND = os.environ.get("MIGRATE_ROUND", "").strip()
# 迁移范围：tag = 只迁「本次抽检版本号」带这一轮版本号的行（默认）；results = 所有基础列有结果的行
MIGRATE_SCOPE = os.environ.get("MIGRATE_SCOPE", "tag").strip().lower()
CONFIRM = os.environ.get("CONFIRM", "").strip().lower() == "yes"
MAX_WRITE = int(os.environ.get("MAX_WRITE", "600"))

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


def get_all_tables(token, app_token):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables"
    tables, page_token = [], None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        data = api("GET", url, token=token, params=params)
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


def get_fields(token, app_token, table_id):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    data = api("GET", url, token=token, params={"page_size": 100})
    if data.get("code") != 0:
        raise RuntimeError(f"拉取字段失败：{data}")
    return data["data"]["items"]


def create_field(token, app_token, table_id, field_name, field_type, property_=None):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    body = {"field_name": field_name, "type": field_type}
    if property_:
        body["property"] = property_
    data = api("POST", url, token=token, body=body)
    if data.get("code") != 0:
        raise RuntimeError(f"创建字段「{field_name}」失败：{data}")
    print(f"已创建结果列「{field_name}」")


def batch_update(token, app_token, table_id, updates, chunk_size=450):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
    done = 0
    for i in range(0, len(updates), chunk_size):
        chunk = updates[i:i + chunk_size]
        data = api("POST", url, token=token, body={"records": chunk})
        if data.get("code") != 0:
            raise RuntimeError(f"批量更新失败（已更新 {done} 条）：{data}")
        done += len(chunk)
        print(f"已更新 {done}/{len(updates)} 条")
    return done


def parse_date(ms_timestamp):
    """飞书日期字段换算成北京时间后固定停在23:00，需要再加1小时才能拿到正确日期"""
    if not ms_timestamp:
        return None
    return datetime.fromtimestamp(ms_timestamp / 1000, tz=BEIJING_TZ) + timedelta(hours=1)


def get_text_value(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
    if isinstance(value, list) and value:
        return "".join(v.get("text", "") if isinstance(v, dict) else str(v) for v in value)
    return ""


def get_select_value(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
    return ""


def get_multi_select_values(fields, field_name):
    value = fields.get(field_name)
    if not isinstance(value, list):
        return []
    return [v.get("text", "") if isinstance(v, dict) else str(v) for v in value]


def get_tags(fields):
    value = fields.get(FIELD_TAG)
    if isinstance(value, list):
        return [t for t in get_multi_select_values(fields, FIELD_TAG) if t]
    return [t.strip() for t in get_text_value(fields, FIELD_TAG).split(",") if t.strip()]


def get_month_releases(token, year, month):
    """节点更新总表里该月的发布，按日期排序返回 [(轮次, 版本号, 日期), ...]"""
    by_date = defaultdict(list)
    for r in get_all_records(token, NODE_APP_TOKEN, NODE_TABLE_ID):
        fields = r.get("fields", {})
        release_date = parse_date(fields.get(FIELD_NODE_RELEASE_DATE))
        if not release_date or release_date.year != year or release_date.month != month:
            continue
        version = fields.get(FIELD_NODE_VERSION)
        version = str(version) if isinstance(version, (int, float)) else get_text_value(fields, FIELD_NODE_VERSION)
        if version:
            by_date[release_date.date()].append(version)
    return [(i + 1, "/".join(by_date[d]), d) for i, d in enumerate(sorted(by_date))]


def main():
    now = datetime.now(BEIJING_TZ)
    if TARGET_MONTH:
        year, month = (int(x) for x in TARGET_MONTH.split("-")[:2])
    else:
        year, month = now.year, now.month

    token = get_tenant_token()

    table_name = f"M{month}验收表"
    table_id = find_table_by_name(token, BLIND_TEST_APP_TOKEN, table_name)
    if not table_id:
        raise RuntimeError(f"没找到 {table_name}")
    print(f"目标表：{table_name}（{table_id}），处理月份 {year}-{month:02d}")

    rounds = get_month_releases(token, year, month)
    if rounds:
        for n, version, date in rounds:
            print(f"  第{n}次发版：{version}（{date}）")
    else:
        print(f"  ⚠️ 节点更新总表里 {year}-{month:02d} 没有填发布日期的版本")

    existing_fields = {f["field_name"] for f in get_fields(token, BLIND_TEST_APP_TOKEN, table_id)}
    records = get_all_records(token, BLIND_TEST_APP_TOKEN, table_id)
    print(f"\n现有列：{'、'.join(sorted(existing_fields))}")
    print(f"记录条数：{len(records)}")

    # ---------- 体检 ----------
    tag_counter = Counter()
    mac_filled = iphone_filled = 0
    for r in records:
        fields = r["fields"]
        tags = get_tags(fields)
        if tags:
            tag_counter.update(tags)
        else:
            tag_counter["（未打标）"] += 1
        if get_select_value(fields, FIELD_MAC_RESULT).strip() not in PENDING_VALUES:
            mac_filled += 1
        if get_select_value(fields, FIELD_IPHONE_RESULT).strip() not in PENDING_VALUES:
            iphone_filled += 1
    print(f"「{FIELD_TAG}」分布：")
    for tag, count in tag_counter.most_common():
        print(f"  {tag}：{count} 条")
    print(f"基础结果列已填：{FIELD_MAC_RESULT} {mac_filled} 条，{FIELD_IPHONE_RESULT} {iphone_filled} 条")
    for n, _version, _date in rounds:
        for name in (ROUND_MAC_FIELD.format(n=n), ROUND_IPHONE_FIELD.format(n=n)):
            if name in existing_fields:
                done = sum(1 for r in records
                           if get_select_value(r["fields"], name).strip() not in PENDING_VALUES)
                scope = sum(1 for r in records if get_select_value(r["fields"], name).strip())
                print(f"「{name}」：本轮范围 {scope} 条，已填结果 {done} 条")

    # ---------- 建列 ----------
    if CREATE_ROUNDS:
        want_rounds = [int(x) for x in CREATE_ROUNDS.split(",") if x.strip()]
    else:
        want_rounds = [n for n, _v, _d in rounds]
    to_create = [name for n in want_rounds
                 for name in (ROUND_MAC_FIELD.format(n=n), ROUND_IPHONE_FIELD.format(n=n))
                 if name not in existing_fields]
    print(f"\n需要新建的结果列：{'、'.join(to_create) if to_create else '无'}")

    # ---------- 迁移计划 ----------
    updates = []
    if MIGRATE_ROUND:
        round_no = int(MIGRATE_ROUND)
        mac_field = ROUND_MAC_FIELD.format(n=round_no)
        iphone_field = ROUND_IPHONE_FIELD.format(n=round_no)
        version = next((v for n, v, _d in rounds if n == round_no), "")

        if MIGRATE_SCOPE == "results":
            in_scope = [r for r in records
                        if get_select_value(r["fields"], FIELD_MAC_RESULT).strip() not in PENDING_VALUES
                        or get_select_value(r["fields"], FIELD_IPHONE_RESULT).strip() not in PENDING_VALUES]
            print(f"\n迁移范围 results：基础列有结果的 {len(in_scope)} 条 → 第{round_no}次发版")
        else:
            if not version:
                raise RuntimeError(f"节点更新总表里查不到 {year}-{month:02d} 第{round_no}次发版的版本号，"
                                   f"没法按版本号圈定范围；确认范围后可以改用 MIGRATE_SCOPE=results")
            in_scope = [r for r in records if version in get_tags(r["fields"])]
            print(f"\n迁移范围 tag：「{FIELD_TAG}」= {version} 的 {len(in_scope)} 条 → 第{round_no}次发版")
            if not in_scope:
                print(f"  ⚠️ 没有行带 {version} 这个标记，这一轮还没打标，无需迁移")

        for r in in_scope:
            fields = r["fields"]
            write = {}
            for round_field, base_field in ((mac_field, FIELD_MAC_RESULT), (iphone_field, FIELD_IPHONE_RESULT)):
                if get_select_value(fields, round_field).strip():
                    continue   # 本轮这一列已经有值，不覆盖
                base = get_select_value(fields, base_field).strip()
                write[round_field] = base if base not in PENDING_VALUES else "待验收"
            if write:
                updates.append({"record_id": r["record_id"], "fields": write})

        moved = sum(1 for u in updates if any(v != "待验收" for v in u["fields"].values()))
        print(f"计划写入 {len(updates)} 条（其中 {moved} 条带已有结果，其余置为待验收）")
        for u in updates[:5]:
            print(f"  样例 {u}")
        if len(updates) > MAX_WRITE:
            raise RuntimeError(f"计划写入 {len(updates)} 条，超过 MAX_WRITE={MAX_WRITE}，"
                               f"先确认范围是否正确，必要时调大 MAX_WRITE")

    if not CONFIRM:
        print("\n== DRY RUN ==（没有任何写入）确认以上计划无误后，手动触发时把 confirm 填 yes")
        return

    for name in to_create:
        create_field(token, BLIND_TEST_APP_TOKEN, table_id, name, 3, {"options": ROUND_RESULT_OPTIONS})
    if updates:
        batch_update(token, BLIND_TEST_APP_TOKEN, table_id, updates)
    print(f"\n完成：新建 {len(to_create)} 列，写入 {len(updates)} 条")


if __name__ == "__main__":
    main()
