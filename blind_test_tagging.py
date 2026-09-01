# -*- coding: utf-8 -*-
"""
发版抽检打标脚本
每天检查分工表里"上线日期=今天"的需求，用"二级模块"关键词匹配当月验收表里的用例，
命中的行在"本次抽检版本号"字段打上这次发布日期的标记（不新增记录、不重新分配验收人）。

需要 GitHub Actions Secrets：
- FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI
"""

import requests
import os
import random
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID_CLI"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET_CLI"]

# 分工表（产品部项目管理）
RELEASE_APP_TOKEN = "VhZebH05uaUlEyscWfWc2mMvnhc"
RELEASE_TABLE_ID = "tbl1Gwx4r7oOcV8g"
FIELD_PROJECT_DESC = "项目说明"
FIELD_ONLINE_DATE = "上线日期"
FIELD_PLATFORM = "端"
SKIP_PLATFORM_VALUES = {"其他"}

# 盲测用例库所在文档
BLIND_TEST_APP_TOKEN = "FnFab3FDKa0JU6sqa19cDVpHnM7"
FIELD_MODULE = "二级模块"
FIELD_TAG = "本次抽检版本号"

TARGET_DATE_STR = os.environ.get("TARGET_RELEASE_DATE", "")

# 关键词匹配数量不够时，从未验证用例里随机补齐到这个数
MIN_SAMPLE_SIZE = 200


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


def update_record(token, app_token, table_id, record_id, fields):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    resp = requests.put(url, headers=headers, json={"fields": fields}, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"更新记录失败：{data}")


def parse_date(ms_timestamp):
    """飞书日期字段换算成北京时间后固定停在23:00，需要再加1小时才能拿到正确日期"""
    if not ms_timestamp:
        return None
    return datetime.fromtimestamp(ms_timestamp / 1000, tz=BEIJING_TZ) + timedelta(hours=1)


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


def get_select_value(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
    return ""


def get_field_type(token, app_token, table_id, field_name):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    resp = requests.get(url, headers=headers, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"拉取字段失败：{data}")
    for f in data["data"]["items"]:
        if f["field_name"] == field_name:
            return f["type"], f.get("field_id")
    return None, None


def ensure_multi_select_option(token, app_token, table_id, field_id, option_name):
    """多选字段写入前，确认这个选项存在，不存在就先加上"""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}"
    resp = requests.get(url, headers=headers, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"读取字段详情失败：{data}")
    options = data["data"]["field"].get("property", {}).get("options", [])
    if any(o["name"] == option_name for o in options):
        return
    options.append({"name": option_name})
    resp = requests.put(url, headers=headers, json={
        "field_name": data["data"]["field"]["field_name"],
        "type": 4,
        "property": {"options": options},
    }, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"新增多选选项失败：{data}")


def get_multi_select_values(fields, field_name):
    value = fields.get(field_name)
    if not isinstance(value, list):
        return []
    result = []
    for v in value:
        result.append(v.get("text", "") if isinstance(v, dict) else str(v))
    return result


def is_case_done(fields):
    iphone = get_select_value(fields, "iPhone验收结果")
    mac = get_select_value(fields, "Mac验收结果")
    return iphone in {"通过", "不通过"} and mac in {"通过", "不通过"}


def main():
    token = get_tenant_token()
    now = datetime.now(BEIJING_TZ)

    if TARGET_DATE_STR:
        target_date = datetime.strptime(TARGET_DATE_STR, "%Y-%m-%d").date()
    else:
        target_date = now.date()

    target_table_name = f"M{now.month}验收表"
    blind_table_id = find_table_by_name(token, BLIND_TEST_APP_TOKEN, target_table_name)
    if not blind_table_id:
        raise RuntimeError(f"没找到 {target_table_name}，请确认本月验收表已经建好")

    # 拿这次要上线的需求
    release_records = get_all_records(token, RELEASE_APP_TOKEN, RELEASE_TABLE_ID)
    features = []
    for r in release_records:
        fields = r["fields"]
        online_date = parse_date(fields.get(FIELD_ONLINE_DATE))
        if not online_date or online_date.date() != target_date:
            continue
        platform_value = get_select_value(fields, FIELD_PLATFORM)
        if platform_value in SKIP_PLATFORM_VALUES:
            continue
        desc = fields.get(FIELD_PROJECT_DESC, "")
        if desc:
            features.append(desc)

    if not features:
        print(f"{target_date} 没有匹配到需要上线的需求，跳过打标")
        return

    print(f"本次发布（{target_date}）涉及 {len(features)} 个需求")

    # 拿当月验收表所有用例，按"二级模块"关键词匹配需求描述
    blind_records = get_all_records(token, BLIND_TEST_APP_TOKEN, blind_table_id)
    tag_value = target_date.strftime("%Y-%m-%d")

    matched_ids = []
    for r in blind_records:
        fields = r["fields"]
        module = get_text_value(fields, FIELD_MODULE)
        if module and any(module in desc for desc in features):
            matched_ids.append(r["record_id"])

    print(f"关键词匹配到 {len(matched_ids)} 条")

    if len(matched_ids) < MIN_SAMPLE_SIZE:
        matched_set = set(matched_ids)
        # 从未验证过、且还没被匹配到的用例里随机补齐
        pool = [r["record_id"] for r in blind_records
                if r["record_id"] not in matched_set and not is_case_done(r["fields"])]
        need_more = MIN_SAMPLE_SIZE - len(matched_ids)
        supplement = random.sample(pool, min(need_more, len(pool)))
        matched_ids.extend(supplement)
        print(f"数量不足{MIN_SAMPLE_SIZE}条，从未验证用例里随机补充 {len(supplement)} 条")
        if len(pool) < need_more:
            print(f"⚠️ 未验证用例池只剩 {len(pool)} 条，本次实际总数不足{MIN_SAMPLE_SIZE}条")

    # 检查"本次抽检版本号"字段的实际类型，文本/多选两种写法不同
    field_type, field_id = get_field_type(token, BLIND_TEST_APP_TOKEN, blind_table_id, FIELD_TAG)
    if field_type is None:
        raise RuntimeError(f"没找到字段 {FIELD_TAG}")

    records_by_id = {r["record_id"]: r["fields"] for r in blind_records}
    tagged_count = 0

    for record_id in matched_ids:
        fields = records_by_id[record_id]
        if field_type == 4:  # 多选
            existing = set(get_multi_select_values(fields, FIELD_TAG))
            if tag_value in existing:
                continue
            ensure_multi_select_option(token, BLIND_TEST_APP_TOKEN, blind_table_id, field_id, tag_value)
            existing.add(tag_value)
            update_record(token, BLIND_TEST_APP_TOKEN, blind_table_id, record_id,
                          {FIELD_TAG: sorted(existing)})
        else:  # 文本（历史表兼容）
            existing_tag = get_text_value(fields, FIELD_TAG)
            tags = set(t.strip() for t in existing_tag.split(",") if t.strip())
            if tag_value in tags:
                continue
            tags.add(tag_value)
            update_record(token, BLIND_TEST_APP_TOKEN, blind_table_id, record_id,
                          {FIELD_TAG: ",".join(sorted(tags))})
        tagged_count += 1

    print(f"本次共打标 {tagged_count} 条用例（标记：{tag_value}）")


if __name__ == "__main__":
    main()