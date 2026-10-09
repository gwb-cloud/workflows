# -*- coding: utf-8 -*-
"""
同步「是否主流程」+ 给主流程用例打抽检版本号（手动触发）

1. 按「目标」文本一一对应，把原始目标表的「是否主流程」同步到当月验收表
   （验收表没有这个字段就先照源表的字段类型建一个）
2. 「是否主流程」为真的行，「本次抽检版本号」改成只有 TAG_VALUES 这几个值
   （按要求清空原有历史版本号；非主流程的行一律不动）

默认只打印预演结果（DRY RUN），confirm=yes 才真正写表。

需要 GitHub Actions Secrets：
- FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI（CLI 应用需是该多维表格的协作者）
"""

import requests
import os
from collections import Counter
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID_CLI"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET_CLI"]

BASE_APP_TOKEN = "FnFab3FDKa0JU6sqa19cDVpHnM7"
SOURCE_TABLE_ID = "tbl46z8GYP5HN1MJ"      # 原始目标表（用例库）

FIELD_TARGET = "目标"
FIELD_MAINFLOW = "是否主流程"
FIELD_TAG = "本次抽检版本号"

TABLE_NAME = os.environ.get("TARGET_TABLE_NAME", "").strip()
TAG_VALUES = [t.strip() for t in os.environ.get("TAG_VALUES", "MACSP16-1015,MACSP17-1029").split(",") if t.strip()]
# 「是否主流程」是单选/文本时，这些值算"是"；复选框类型直接按勾选判断
TRUE_VALUES = {v.strip() for v in os.environ.get("MAINFLOW_TRUE_VALUES", "是,Y,yes,true,主流程,是主流程").split(",") if v.strip()}
CONFIRM = os.environ.get("CONFIRM_WRITE", "").strip().lower() == "yes"
MAX_UPDATE = int(os.environ.get("MAX_UPDATE", "2500"))


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
    records, page_token = [], None
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


def get_fields(token, table_id, app_token=BASE_APP_TOKEN):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    resp = requests.get(url, headers=headers, params={"page_size": 100}, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"拉取字段失败：{data}")
    return data["data"]["items"]


def find_field(fields, name):
    for f in fields:
        if f["field_name"] == name:
            return f
    return None


def create_field(token, table_id, field_name, field_type, property_=None, app_token=BASE_APP_TOKEN):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    body = {"field_name": field_name, "type": field_type}
    if property_:
        body["property"] = property_
    resp = requests.post(url, headers=headers, json=body, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"创建字段「{field_name}」失败：{data}")
    print(f"已在目标表创建字段「{field_name}」（type={field_type}）")
    return data["data"]["field"]


def ensure_select_options(token, table_id, field, option_names, app_token=BASE_APP_TOKEN):
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


def batch_update(token, table_id, updates, app_token=BASE_APP_TOKEN, chunk_size=450):
    """updates: [{"record_id": .., "fields": {..}}]，批量接口，避免逐条 PUT 触发限流"""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
    done = 0
    for i in range(0, len(updates), chunk_size):
        chunk = updates[i:i + chunk_size]
        resp = requests.post(url, headers=headers, json={"records": chunk}, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"批量更新失败（已更新 {done} 条）：{data}")
        done += len(chunk)
        print(f"已更新 {done}/{len(updates)} 条")
    return done


def get_text_value(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
    if isinstance(value, list) and value:
        return "".join(v.get("text", "") if isinstance(v, dict) else str(v) for v in value)
    return ""


def target_key(fields):
    return get_text_value(fields, FIELD_TARGET).replace("　", " ").strip()


def mainflow_raw(fields):
    """原样取「是否主流程」的值，用于写回目标表"""
    value = fields.get(FIELD_MAINFLOW)
    if isinstance(value, dict):
        return value.get("text", "")
    if isinstance(value, list) and value:
        return "".join(v.get("text", "") if isinstance(v, dict) else str(v) for v in value)
    return value


def blank(value):
    """空值的几种等价形态：没填、空串、复选框未勾选"""
    return value is None or value is False or (isinstance(value, str) and not value.strip())


def same_value(a, b):
    return (blank(a) and blank(b)) or a == b


def is_mainflow(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip() in TRUE_VALUES


def multi_values(fields, field_name):
    value = fields.get(field_name)
    if not isinstance(value, list):
        return []
    return [v.get("text", "") if isinstance(v, dict) else str(v) for v in value]


def main():
    token = get_tenant_token()
    now = datetime.now(BEIJING_TZ)
    table_name = TABLE_NAME or f"M{now.month}验收表"

    table_id = find_table_by_name(token, table_name)
    if not table_id:
        raise RuntimeError(f"没找到表 {table_name}")
    print(f"目标表：{table_name}（table_id: {table_id}）｜ 要打的标记：{'、'.join(TAG_VALUES)}")

    source_fields = get_fields(token, SOURCE_TABLE_ID)
    src_mainflow = find_field(source_fields, FIELD_MAINFLOW)
    if not src_mainflow:
        raise RuntimeError(f"原始目标表里没有「{FIELD_MAINFLOW}」字段，请先确认字段名")
    print(f"源表「{FIELD_MAINFLOW}」字段类型 type={src_mainflow['type']}")

    source_records = get_all_records(token, SOURCE_TABLE_ID)
    target_records = get_all_records(token, table_id)
    print(f"原始目标表 {len(source_records)} 条，{table_name} {len(target_records)} 条")

    # 源表：目标 → 是否主流程
    src_value, conflicts = {}, set()
    for r in source_records:
        key = target_key(r["fields"])
        value = mainflow_raw(r["fields"])
        if key in src_value and src_value[key] != value:
            conflicts.add(key)
        src_value.setdefault(key, value)
    if conflicts:
        print(f"⚠️ 源表有 {len(conflicts)} 个目标重复且「{FIELD_MAINFLOW}」取值不一致，按第一条为准")

    target_fields = get_fields(token, table_id)
    tgt_mainflow = find_field(target_fields, FIELD_MAINFLOW)
    tag_field = find_field(target_fields, FIELD_TAG)
    if not tag_field:
        raise RuntimeError(f"{table_name} 里没有「{FIELD_TAG}」字段")

    # 逐条算出要改什么
    updates, stat = [], Counter()
    missing_in_source = []
    for r in target_records:
        key = target_key(r["fields"])
        fields_to_write = {}
        if key not in src_value:
            missing_in_source.append(key)
            stat["源表没有此目标"] += 1
            continue
        want = src_value[key]
        if not same_value(mainflow_raw(r["fields"]), want):
            fields_to_write[FIELD_MAINFLOW] = want
            stat["同步是否主流程"] += 1
        if is_mainflow(want):
            stat["主流程用例"] += 1
            current = multi_values(r["fields"], FIELD_TAG)
            if current != TAG_VALUES:
                fields_to_write[FIELD_TAG] = list(TAG_VALUES) if tag_field["type"] == 4 else ",".join(TAG_VALUES)
                stat["改抽检版本号"] += 1
        if fields_to_write:
            updates.append({"record_id": r["record_id"], "fields": fields_to_write})

    print(f"\n【预演结果】需要更新 {len(updates)} 条记录")
    for k, v in stat.items():
        print(f"   - {k}：{v} 条")
    if missing_in_source:
        print(f"⚠️ {table_name} 里有 {len(missing_in_source)} 条目标在源表找不到（不处理）：")
        for k in missing_in_source[:5]:
            print(f"     · {k[:60]}")

    sample = [u for u in updates if FIELD_TAG in u["fields"]][:5]
    for u in sample:
        print(f"   例：{u['fields']}")

    if not updates:
        print("\n✅ 已经是目标状态，无需更新")
        return
    if len(updates) > MAX_UPDATE:
        raise RuntimeError(f"待更新 {len(updates)} 条超过安全阀 MAX_UPDATE={MAX_UPDATE}，已中止")

    if not CONFIRM:
        print(f"\n🔍 当前是预演模式（DRY RUN），没有改动表。确认无误后用 confirm=yes 重新触发一次。")
        return

    # 真正写表前，先补字段和选项
    if not tgt_mainflow:
        tgt_mainflow = create_field(token, table_id, FIELD_MAINFLOW, src_mainflow["type"],
                                    src_mainflow.get("property"))
    elif tgt_mainflow["type"] == 3:
        ensure_select_options(token, table_id, tgt_mainflow,
                              [str(v) for v in src_value.values() if isinstance(v, str)])
    if tag_field["type"] == 4:
        ensure_select_options(token, table_id, tag_field, TAG_VALUES)

    print(f"\n开始更新 {len(updates)} 条…")
    done = batch_update(token, table_id, updates)
    print(f"\n✅ 完成：更新 {done} 条；主流程用例 {stat['主流程用例']} 条已标记 {'、'.join(TAG_VALUES)}")


if __name__ == "__main__":
    main()
