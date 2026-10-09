# -*- coding: utf-8 -*-
"""
月度验收表对账清理脚本（手动触发）
原始目标表（用例库）删了用例之后，当月验收表不会自动跟着删，这个脚本把验收表里
"源表已经没有了"的多余记录删掉，使两张表的用例对上。

判断规则：按「目标」文本逐条对账（源表同一个目标出现N次，验收表最多保留N条）。
同一个目标有多条时，优先保留已经填了验收结果/已打抽检标记的那几条，删没动过的。

默认只打印对账结果不删除（DRY RUN）。确认无误后再传 CONFIRM_DELETE=yes 真正删除。

需要 GitHub Actions Secrets：
- FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI（CLI 应用需是该多维表格的协作者）
"""

import requests
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID_CLI"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET_CLI"]

BASE_APP_TOKEN = "FnFab3FDKa0JU6sqa19cDVpHnM7"
SOURCE_TABLE_ID = "tbl46z8GYP5HN1MJ"      # 原始目标表（用例库）

FIELD_TARGET = "目标"
FIELD_MODULE = "二级模块"
FIELD_IPHONE_RESULT = "iPhone验收结果"
FIELD_MAC_RESULT = "Mac验收结果"
FIELD_TAG = "本次抽检版本号"

# 手动触发时的输入
TABLE_NAME = os.environ.get("TARGET_TABLE_NAME", "").strip()
TABLE_ID_OVERRIDE = os.environ.get("TARGET_TABLE_ID", "").strip()
CONFIRM_DELETE = os.environ.get("CONFIRM_DELETE", "").strip().lower() == "yes"
# 安全阀：一次最多删这么多条，超过就中止（防止源表读取异常导致误删一大片）
MAX_DELETE = int(os.environ.get("MAX_DELETE", "200"))


def get_tenant_token():
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 token 失败（检查 FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI）：{data}")
    return data["tenant_access_token"]


def get_all_records(token, table_id, app_token=BASE_APP_TOKEN):
    records = []
    page_token = None
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"拉取记录失败：{data}")
        records.extend(data["data"]["items"])
        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
    return records


def get_all_tables(token, app_token=BASE_APP_TOKEN):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables"
    tables, page_token = [], None
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


def find_table_by_name(token, table_name, app_token=BASE_APP_TOKEN):
    for t in get_all_tables(token, app_token):
        if t["name"] == table_name:
            return t["table_id"]
    return None


def batch_delete(token, table_id, record_ids, app_token=BASE_APP_TOKEN, chunk_size=450):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete"
    deleted = 0
    for i in range(0, len(record_ids), chunk_size):
        chunk = record_ids[i:i + chunk_size]
        resp = requests.post(url, headers=headers, json={"records": chunk}, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"批量删除失败（已删 {deleted} 条）：{data}")
        deleted += len(chunk)
        print(f"已删除 {deleted}/{len(record_ids)} 条")
    return deleted


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


def target_key(fields):
    """对账用的 key：目标文本（去掉首尾空白、全角空格）"""
    return get_text_value(fields, FIELD_TARGET).replace("　", " ").strip()


def has_data(fields):
    """这条记录有没有人动过：填了任一端验收结果，或被打过抽检标记"""
    if get_select_value(fields, FIELD_IPHONE_RESULT).strip() not in ("", "待验收"):
        return True
    if get_select_value(fields, FIELD_MAC_RESULT).strip() not in ("", "待验收"):
        return True
    tag = fields.get(FIELD_TAG)
    return bool(tag)


def main():
    token = get_tenant_token()
    now = datetime.now(BEIJING_TZ)

    table_name = TABLE_NAME or f"M{now.month}验收表"
    if TABLE_ID_OVERRIDE:
        table_id = TABLE_ID_OVERRIDE
        print(f"使用手动指定的 table_id: {table_id}")
    else:
        table_id = find_table_by_name(token, table_name)
        if not table_id:
            raise RuntimeError(f"没找到表 {table_name}")
        print(f"目标表：{table_name}（table_id: {table_id}）")

    source_records = get_all_records(token, SOURCE_TABLE_ID)
    target_records = get_all_records(token, table_id)
    print(f"原始目标表 {len(source_records)} 条，{table_name} {len(target_records)} 条，差 {len(target_records) - len(source_records)} 条")

    source_counts = Counter(target_key(r["fields"]) for r in source_records)

    by_key = defaultdict(list)
    for r in target_records:
        by_key[target_key(r["fields"])].append(r)

    to_delete, kept_count = [], 0
    for key, rows in by_key.items():
        allowed = source_counts.get(key, 0)
        # 有数据的排前面优先保留，其余按原顺序
        rows_sorted = sorted(rows, key=lambda r: not has_data(r["fields"]))
        kept_count += min(len(rows_sorted), allowed)
        to_delete.extend(rows_sorted[allowed:])

    missing = [k for k, c in source_counts.items() if c > len(by_key.get(k, []))]
    dirty = [r for r in to_delete if has_data(r["fields"])]

    print(f"\n【对账结果】保留 {kept_count} 条，待删除 {len(to_delete)} 条")
    print(f"其中源表已完全没有的目标：{sum(1 for r in to_delete if target_key(r['fields']) not in source_counts)} 条，"
          f"重复超出的：{len(to_delete) - sum(1 for r in to_delete if target_key(r['fields']) not in source_counts)} 条")
    if missing:
        print(f"⚠️ 源表有、验收表缺少的目标 {len(missing)} 个（本脚本不负责补，只提示）：")
        for k in missing[:10]:
            print(f"   - {k[:60]}")
    if dirty:
        print(f"⚠️ 待删除里有 {len(dirty)} 条已经填过验收结果/打过抽检标记，删掉会丢数据：")
        for r in dirty[:20]:
            print(f"   - {target_key(r['fields'])[:60]}")

    print("\n待删除明细（最多列 50 条）：")
    for r in to_delete[:50]:
        f = r["fields"]
        print(f"   - [{get_select_value(f, FIELD_MODULE)}] {target_key(f)[:70]}")

    if not to_delete:
        print("\n✅ 两张表已经对上，无需删除")
        return

    if len(to_delete) > MAX_DELETE:
        raise RuntimeError(
            f"待删除 {len(to_delete)} 条超过安全阀 MAX_DELETE={MAX_DELETE}，已中止。"
            f"确认结果合理的话，手动触发时把 max_delete 调大再跑。")

    if not CONFIRM_DELETE:
        print(f"\n🔍 当前是预演模式（DRY RUN），没有删除任何记录。"
              f"确认以上 {len(to_delete)} 条该删，再用 confirm=yes 重新触发一次。")
        return

    print(f"\n开始删除 {len(to_delete)} 条…")
    deleted = batch_delete(token, table_id, [r["record_id"] for r in to_delete])
    print(f"✅ 已删除 {deleted} 条，{table_name} 剩余 {len(target_records) - deleted} 条")


if __name__ == "__main__":
    main()
