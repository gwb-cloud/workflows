# -*- coding: utf-8 -*-
"""
发版抽检打标脚本
每天从"节点更新总表"找下一次发布（日期+版本号），取分工表里属于这次发布的需求，
用"二级模块"关键词匹配当月验收表里的用例，命中的行在"本次抽检版本号"字段打上版本号
（不新增记录、不重新分配验收人）。发布日期/需求归属规则跟 release_acceptance_summary.py 一致。

每次发布的抽检总量（已打标 + 关键词命中）不足200条时，才从未验证用例里随机补齐到200条，
所以每天重复运行不会让同一个版本的打标数量越来越多。

需要 GitHub Actions Secrets：
- FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI
（CLI 应用需要被添加为节点更新总表、分工表、盲测文档三个多维表格的协作者）
"""

import requests
import os
import random
from collections import defaultdict
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID_CLI"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET_CLI"]

# 节点更新总表：发布日期的唯一数据源
NODE_APP_TOKEN = "IC7ObBQ2ya2H4FsqT3ocPreYned"
NODE_TABLE_ID = "tblSDaRAkltfVtx6"
FIELD_NODE_VERSION = "版本号"
FIELD_NODE_RELEASE_DATE = "版本发布日期"

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
    """兼容文本(字符串)、多行文本([{"text":...}])、单选({"text":...})三种返回格式"""
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
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


def get_releases(token):
    """读节点更新总表，返回 {发布日期: [版本号, ...]}，没填发布日期的行跳过"""
    releases = defaultdict(list)
    for r in get_all_records(token, NODE_APP_TOKEN, NODE_TABLE_ID):
        fields = r.get("fields", {})
        release_date = parse_date(fields.get(FIELD_NODE_RELEASE_DATE))
        if not release_date:
            continue
        version = fields.get(FIELD_NODE_VERSION)
        version = str(version) if isinstance(version, (int, float)) else get_text_value(fields, FIELD_NODE_VERSION)
        if version:
            releases[release_date.date()].append(version)
    return releases


def find_next_release_date(releases, today):
    """节点更新总表里最近的即将到来的发布日期（今天或之后，允许1天宽限兼容发布日刚过的过渡态）。
    跟 release_acceptance_summary.py 里的同名函数保持一致，保证两边算出来的是同一次发布。"""
    candidates = [d for d in releases if d >= today - timedelta(days=1)]
    return min(candidates) if candidates else None


def find_previous_release_date(releases, target_date):
    earlier = [d for d in releases if d < target_date]
    return max(earlier) if earlier else None


def belongs_to_release(online_date, prev_release_date, target_date):
    """需求上线日期落在（上一次发布, 本次发布] 区间内就算本次发布的需求；
    节点更新总表里没有更早的发布记录时，退化成上线日期=本次发布日期"""
    if prev_release_date is None:
        return online_date == target_date
    return prev_release_date < online_date <= target_date


def get_existing_tags(fields, field_type):
    if field_type == 4:  # 多选
        return set(get_multi_select_values(fields, FIELD_TAG))
    return set(t.strip() for t in get_text_value(fields, FIELD_TAG).split(",") if t.strip())


def main():
    token = get_tenant_token()
    now = datetime.now(BEIJING_TZ)

    # 发布日期和版本号都以节点更新总表为准，不能直接用运行当天，
    # 否则连续巡检多天会打出多个不同的标签，同一次发布被拆成好几份
    releases = get_releases(token)

    if TARGET_DATE_STR:
        target_date = datetime.strptime(TARGET_DATE_STR, "%Y-%m-%d").date()
        print(f"手动指定目标日期: {target_date}")
        if target_date not in releases:
            raise RuntimeError(f"节点更新总表里没有发布日期为 {target_date} 且填了版本号的记录")
    else:
        target_date = find_next_release_date(releases, now.date())
        if target_date is None:
            print("节点更新总表里没有找到任何即将到来的发布日期，跳过")
            return

    tag_value = "/".join(releases[target_date])
    prev_release_date = find_previous_release_date(releases, target_date)
    print(f"目标发布：{tag_value}（{target_date}），上一次发布：{prev_release_date or '无'}")

    target_table_name = f"M{now.month}验收表"
    blind_table_id = find_table_by_name(token, BLIND_TEST_APP_TOKEN, target_table_name)
    if not blind_table_id:
        raise RuntimeError(f"没找到 {target_table_name}，请确认本月验收表已经建好")

    # 拿这次发布包含的需求
    features = []
    for r in get_all_records(token, RELEASE_APP_TOKEN, RELEASE_TABLE_ID):
        fields = r["fields"]
        online_date = parse_date(fields.get(FIELD_ONLINE_DATE))
        if not online_date or not belongs_to_release(online_date.date(), prev_release_date, target_date):
            continue
        platform_value = get_select_value(fields, FIELD_PLATFORM)
        if platform_value in SKIP_PLATFORM_VALUES:
            continue
        desc = fields.get(FIELD_PROJECT_DESC, "")
        if desc:
            features.append(desc)

    if not features:
        print(f"{tag_value}（{target_date}）没有匹配到需要上线的需求，跳过打标")
        return

    print(f"本次发布涉及 {len(features)} 个需求")

    # 检查"本次抽检版本号"字段的实际类型，文本/多选两种写法不同
    field_type, field_id = get_field_type(token, BLIND_TEST_APP_TOKEN, blind_table_id, FIELD_TAG)
    if field_type is None:
        raise RuntimeError(f"没找到字段 {FIELD_TAG}")

    # 拿当月验收表所有用例，按"二级模块"关键词匹配需求描述
    blind_records = get_all_records(token, BLIND_TEST_APP_TOKEN, blind_table_id)
    records_by_id = {r["record_id"]: r["fields"] for r in blind_records}

    already_tagged = {r["record_id"] for r in blind_records
                      if tag_value in get_existing_tags(r["fields"], field_type)}
    matched = set()
    for r in blind_records:
        module = get_text_value(r["fields"], FIELD_MODULE)
        if module and any(module in desc for desc in features):
            matched.add(r["record_id"])

    print(f"之前已打标 {len(already_tagged)} 条，本次关键词匹配到 {len(matched)} 条")

    selected = already_tagged | matched
    if len(selected) < MIN_SAMPLE_SIZE:
        # 从未验证过、且还没选中的用例里随机补齐，已打标的也算进总数，重复运行不会越补越多
        pool = [r["record_id"] for r in blind_records
                if r["record_id"] not in selected and not is_case_done(r["fields"])]
        need_more = MIN_SAMPLE_SIZE - len(selected)
        supplement = random.sample(pool, min(need_more, len(pool)))
        selected.update(supplement)
        print(f"数量不足{MIN_SAMPLE_SIZE}条，从未验证用例里随机补充 {len(supplement)} 条")
        if len(pool) < need_more:
            print(f"⚠️ 未验证用例池只剩 {len(pool)} 条，本次实际总数不足{MIN_SAMPLE_SIZE}条")

    to_tag = selected - already_tagged
    if to_tag and field_type == 4:
        ensure_multi_select_option(token, BLIND_TEST_APP_TOKEN, blind_table_id, field_id, tag_value)

    for record_id in to_tag:
        tags = get_existing_tags(records_by_id[record_id], field_type)
        tags.add(tag_value)
        if field_type == 4:  # 多选
            value = sorted(tags)
        else:  # 文本（历史表兼容）
            value = ",".join(sorted(tags))
        update_record(token, BLIND_TEST_APP_TOKEN, blind_table_id, record_id, {FIELD_TAG: value})

    print(f"本次新打标 {len(to_tag)} 条，{tag_value} 累计抽检 {len(selected)} 条")


if __name__ == "__main__":
    main()