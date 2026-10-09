# -*- coding: utf-8 -*-
"""
月度验收表双向对账脚本（手动触发）
原始目标表（用例库）增删用例之后，当月验收表不会自动跟着变，这个脚本把两张表对齐：
1. 删除：验收表里源表已经没有的记录（不管有没有填过验收结果/打过抽检标记，一律删）
2. 补充：源表有、验收表缺的用例，新增到验收表，验收人按当前分配条数最少的人补齐

判断规则：按「目标」文本逐条对账（源表同一个目标出现N次，验收表就保留N条）。
同一个目标在验收表有多条时，优先保留已填验收结果/已打抽检标记的，删没动过的。

默认只打印对账结果，不改表（DRY RUN）。确认无误后再传 CONFIRM_DELETE=yes 真正执行。

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
MAX_CREATE = int(os.environ.get("MAX_CREATE", "200"))


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


def batch_create(token, table_id, rows, app_token=BASE_APP_TOKEN, chunk_size=450):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
    created = 0
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i + chunk_size]
        resp = requests.post(url, headers=headers, json={"records": [{"fields": r} for r in chunk]}, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"批量新增失败（已新增 {created} 条）：{data}")
        created += len(chunk)
        print(f"已新增 {created}/{len(rows)} 条")
    return created


def get_field(token, table_id, field_name, app_token=BASE_APP_TOKEN):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    resp = requests.get(url, headers=headers, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"拉取字段失败：{data}")
    for f in data["data"]["items"]:
        if f["field_name"] == field_name:
            return f
    return None


def ensure_select_options(token, table_id, field, option_names, app_token=BASE_APP_TOKEN):
    """单选字段写入前，确认选项都存在，缺的先补上（源表后加的二级模块选项，新表里可能没有）"""
    existing = [o["name"] for o in field.get("property", {}).get("options", [])]
    missing = list(dict.fromkeys(n for n in option_names if n and n not in existing))
    if not missing:
        return
    options = [{"name": n} for n in existing + missing]
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field['field_id']}"
    resp = requests.put(url, headers=headers, json={
        "field_name": field["field_name"], "type": field["type"], "property": {"options": options},
    }, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"给「{field['field_name']}」新增选项失败：{data}")
    print(f"已给「{field['field_name']}」补充选项：{'、'.join(missing)}")


def get_person_value(fields, field_name):
    """人员字段的原始值，用于原样写回新记录"""
    value = fields.get(field_name)
    return value if isinstance(value, list) and value else None


def get_person_name(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0].get("name", "")
    return ""


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


FIELD_OWNER = "验收人"


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
    print(f"原始目标表 {len(source_records)} 条，{table_name} {len(target_records)} 条，"
          f"差 {len(target_records) - len(source_records)} 条")

    source_by_key = defaultdict(list)
    for r in source_records:
        source_by_key[target_key(r["fields"])].append(r)
    target_by_key = defaultdict(list)
    for r in target_records:
        target_by_key[target_key(r["fields"])].append(r)

    # 逐个目标配对：验收表多的删掉，少的补上
    to_delete, to_create, kept = [], [], []
    for key in set(source_by_key) | set(target_by_key):
        src_rows = source_by_key.get(key, [])
        tgt_rows = target_by_key.get(key, [])
        # 同名多条时，有数据的排前面优先保留（删除数量不变，只影响留哪条）
        tgt_rows = sorted(tgt_rows, key=lambda r: not has_data(r["fields"]))
        pair = min(len(src_rows), len(tgt_rows))
        kept.extend(tgt_rows[:pair])
        to_delete.extend(tgt_rows[pair:])
        to_create.extend(src_rows[pair:])

    sort_key = lambda r: (get_select_value(r["fields"], FIELD_MODULE), target_key(r["fields"]))
    to_delete.sort(key=sort_key)
    to_create.sort(key=sort_key)

    dirty = [r for r in to_delete if has_data(r["fields"])]
    print(f"\n【对账结果】保留 {len(kept)} 条 ｜ 待删除 {len(to_delete)} 条 ｜ 待新增 {len(to_create)} 条")
    if dirty:
        print(f"（待删除里有 {len(dirty)} 条填过验收结果/打过抽检标记，按要求一并删除）")

    if to_delete:
        print("\n待删除（最多列 50 条）：")
        for r in to_delete[:50]:
            f = r["fields"]
            print(f"   - [{get_select_value(f, FIELD_MODULE)}] {target_key(f)[:70]}"
                  f"{' ⚠️有数据' if has_data(f) else ''}")
    if to_create:
        print("\n待新增（最多列 50 条）：")
        for r in to_create[:50]:
            f = r["fields"]
            print(f"   - [{get_select_value(f, FIELD_MODULE)}] {target_key(f)[:70]}")

    if not to_delete and not to_create:
        print("\n✅ 两张表已经对上，无需处理")
        return

    if len(to_delete) > MAX_DELETE:
        raise RuntimeError(f"待删除 {len(to_delete)} 条超过安全阀 MAX_DELETE={MAX_DELETE}，已中止。"
                           f"确认结果合理的话，手动触发时把 max_delete 调大再跑。")
    if len(to_create) > MAX_CREATE:
        raise RuntimeError(f"待新增 {len(to_create)} 条超过安全阀 MAX_CREATE={MAX_CREATE}，已中止。"
                           f"确认结果合理的话，手动触发时把 max_create 调大再跑。")

    # ===== 新增记录的验收人：从当前表里已有的人中，挑分配条数最少的补齐 =====
    new_rows = []
    if to_create:
        owner_values, owner_counts = {}, Counter()
        for r in kept:
            name = get_person_name(r["fields"], FIELD_OWNER)
            if not name:
                continue
            owner_counts[name] += 1
            owner_values.setdefault(name, get_person_value(r["fields"], FIELD_OWNER))
        if not owner_counts:
            raise RuntimeError(f"{table_name} 里读不到任何验收人，无法给新增用例分配，请先确认表结构")

        if CONFIRM_DELETE:   # 预演模式下不碰表，补选项也是写操作
            module_field = get_field(token, table_id, FIELD_MODULE)
            if module_field and module_field.get("type") == 3:
                ensure_select_options(token, table_id, module_field,
                                      [get_select_value(r["fields"], FIELD_MODULE) for r in to_create])

        assigned = Counter()
        for r in to_create:
            # 谁手里的条数最少就给谁，条数相同按姓名排序保证结果稳定
            name = min(owner_counts, key=lambda n: (owner_counts[n], n))
            owner_counts[name] += 1
            assigned[name] += 1
            fields = {FIELD_TARGET: get_text_value(r["fields"], FIELD_TARGET)}
            module = get_select_value(r["fields"], FIELD_MODULE)
            if module:
                fields[FIELD_MODULE] = module
            if owner_values.get(name):
                fields[FIELD_OWNER] = owner_values[name]
            new_rows.append(fields)
        print("\n新增用例的验收人分配：" + "、".join(f"{n} {c}条" for n, c in sorted(assigned.items())))

    if not CONFIRM_DELETE:
        print(f"\n🔍 当前是预演模式（DRY RUN），没有改动表。"
              f"确认以上 删除{len(to_delete)}条 / 新增{len(to_create)}条 无误后，再用 confirm=yes 重新触发一次。")
        return

    deleted = created = 0
    if to_delete:
        print(f"\n开始删除 {len(to_delete)} 条…")
        deleted = batch_delete(token, table_id, [r["record_id"] for r in to_delete])
    if new_rows:
        print(f"开始新增 {len(new_rows)} 条…")
        created = batch_create(token, table_id, new_rows)
    print(f"\n✅ 完成：删除 {deleted} 条，新增 {created} 条，"
          f"{table_name} 现在共 {len(target_records) - deleted + created} 条（源表 {len(source_records)} 条）")


if __name__ == "__main__":
    main()
