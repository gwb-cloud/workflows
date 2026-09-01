# -*- coding: utf-8 -*-
"""
月度盲测建表 + 分人脚本
每月1日自动执行：
1. 从人员名单表读取上个月的人员顺序，循环左移一位得到本月新顺序，写回人员名单表新增一列
2. 新建本月验收表
3. 从原始目标表读取全部用例，按新顺序循环分配验收人，批量写入本月验收表

⚠️ 这个脚本需要"建表+写记录"的完整权限，要用新申请的高权限 CLI 应用，
   并且要确认这个应用已经被加入到目标多维表格文档的协作者列表，否则会报权限错误。

⚠️ 首次运行强烈建议先用 workflow_dispatch 手动触发 + 观察日志，
   Feishu 建表/建字段接口的字段类型编号如果和实际不符会直接报错，属于正常调试过程，
   不用担心第一次没跑通。
"""

import requests
import os
import re
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID_CLI"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET_CLI"]

BASE_APP_TOKEN = "FnFab3FDKa0JU6sqa19cDVpHnM7"
ROSTER_TABLE_ID = "tblbuVpl9xK3E0Rc"      # 人员名单
SOURCE_TABLE_ID = "tbl46z8GYP5HN1MJ"      # 原始目标表（盲测用例库）

FIELD_SOURCE_TARGET = "目标"
FIELD_SOURCE_MODULE = "二级模块"

# 新建月度验收表的字段结构。type 编号是飞书官方定义：1=文本 3=单选 5=日期 11=人员
# 如果建表报错提示字段类型不对，多半是这几个编号需要按报错信息调整
# 新建月度验收表的字段结构。type 编号是飞书官方定义：1=文本 3=单选 4=多选 5=日期 11=人员
# 如果建表报错提示字段类型不对，多半是这几个编号需要按报错信息调整
# "二级模块"的具体选项列表是运行时从原始目标表动态读取的，见 build_new_table_fields()
def build_new_table_fields(module_options):
    return [
        {"field_name": "目标", "type": 1},
        {"field_name": "二级模块", "type": 3, "property": {"options": module_options}},
        {"field_name": "验收人", "type": 11},
        {"field_name": "iPhone验收结果", "type": 3, "property": {"options": [
            {"name": "待验收"}, {"name": "通过"}, {"name": "不通过"}
        ]}},
        {"field_name": "Mac验收结果", "type": 3, "property": {"options": [
            {"name": "待验收"}, {"name": "通过"}, {"name": "不通过"}
        ]}},
        {"field_name": "iPhone验收时间", "type": 5},
        {"field_name": "Mac验收时间", "type": 5},
        {"field_name": "本次抽检版本号", "type": 4},  # 多选，支持一条用例同时被多个发布日期标记
    ]


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


def get_all_records(token, table_id, app_token=BASE_APP_TOKEN):
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


def get_table_fields(token, table_id, app_token=BASE_APP_TOKEN):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    resp = requests.get(url, headers=headers, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"拉取字段失败：{data}")
    return data["data"]["items"]


def get_person_name(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return first.get("name", "")
    return ""


def get_text_value(fields, field_name):
    """多行文本字段，兼容纯字符串 / [{"text":...}] 两种返回格式"""
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        parts = []
        for v in value:
            if isinstance(v, dict):
                parts.append(v.get("text", ""))
            else:
                parts.append(str(v))
        return "".join(parts)
    return ""


def get_select_value(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
    return ""


def compute_new_order(prev_order):
    """循环左移一位：第一位挪到最后"""
    if not prev_order:
        return []
    return prev_order[1:] + [prev_order[0]]


def get_all_tables(token, app_token=BASE_APP_TOKEN):
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


def find_table_by_name(token, table_name, app_token=BASE_APP_TOKEN):
    for t in get_all_tables(token, app_token):
        if t["name"] == table_name:
            return t["table_id"]
    return None


def create_field(token, table_id, field_name, field_type, app_token=BASE_APP_TOKEN, property_=None):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    body = {"field_name": field_name, "type": field_type}
    if property_:
        body["property"] = property_
    resp = requests.post(url, headers=headers, json=body, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"创建字段失败 {field_name}：{data}")
    return data["data"]["field"]


def update_record(token, table_id, record_id, fields, app_token=BASE_APP_TOKEN):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    resp = requests.put(url, headers=headers, json={"fields": fields}, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"更新记录失败：{data}")


def create_table(token, table_name, fields, app_token=BASE_APP_TOKEN):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables"
    body = {"table": {"name": table_name, "fields": fields}}
    resp = requests.post(url, headers=headers, json=body, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"建表失败：{data}")
    return data["data"]["table_id"]


def batch_create_records(token, table_id, records, app_token=BASE_APP_TOKEN, chunk_size=500):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
    for i in range(0, len(records), chunk_size):
        chunk = records[i:i + chunk_size]
        body = {"records": [{"fields": r} for r in chunk]}
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"批量写入失败（第{i}条起）：{data}")
        print(f"已写入 {min(i + chunk_size, len(records))}/{len(records)} 条")


def main():
    token = get_tenant_token()

    now = datetime.now(BEIJING_TZ)
    current_month = now.month
    target_field_name = f"M{current_month}人员"
    target_table_name = f"M{current_month}验收表"

    # ===== 第一步：确定本月人员顺序 =====
    roster_fields = get_table_fields(token, ROSTER_TABLE_ID)
    field_names = [f["field_name"] for f in roster_fields]
    roster_records = get_all_records(token, ROSTER_TABLE_ID)

    if target_field_name in field_names:
        # 本月的列已经存在（提前手工/批量建好过），直接复用，不重新计算、不覆盖
        print(f"{target_field_name} 已存在，直接复用现有顺序，不重新计算")
        new_order = []
        name_to_field_value = {}
        for r in roster_records:
            fields = r["fields"]
            name = get_person_name(fields, target_field_name)
            if name:
                new_order.append(name)
                name_to_field_value[name] = fields.get(target_field_name)
    else:
        # 本月的列不存在，从最近一个存在的月份列循环左移一位，生成新列
        existing_months = []
        for name in field_names:
            m = re.match(r"M(\d+)人员", name)
            if m:
                existing_months.append(int(m.group(1)))
        if not existing_months:
            raise RuntimeError("没有找到任何 M#人员 历史列，无法计算新顺序，请检查人员名单表结构")
        prev_field_name = f"M{max(existing_months)}人员"

        prev_order = []
        name_to_field_value = {}
        for r in roster_records:
            fields = r["fields"]
            name = get_person_name(fields, prev_field_name)
            if name:
                prev_order.append(name)
                name_to_field_value[name] = fields.get(prev_field_name)

        new_order = compute_new_order(prev_order)
        print(f"参考顺序（{prev_field_name}）：{prev_order}")
        print(f"生成新顺序（{target_field_name}）：{new_order}")
        print("⚠️ 请人工核对以上新顺序是否需要处理入职/离职调整")

        create_field(token, ROSTER_TABLE_ID, target_field_name, field_type=11)
        for idx, name in enumerate(new_order):
            if idx >= len(roster_records):
                print(f"⚠️ 人员名单表行数不够，第{idx + 1}位（{name}）没有对应行可写，跳过")
                continue
            record_id = roster_records[idx]["record_id"]
            update_record(token, ROSTER_TABLE_ID, record_id, {target_field_name: name_to_field_value[name]})
        print(f"人员名单表已新增 {target_field_name} 列")

    if not new_order:
        raise RuntimeError(f"{target_field_name} 没有读到任何人员，无法继续分配")

    # ===== 第二步：找本月验收表，没有就建 =====
    new_table_id = find_table_by_name(token, target_table_name)
    if new_table_id:
        print(f"找到已存在的表：{target_table_name}（table_id: {new_table_id}），将直接写入")
    else:
        # 读取原始目标表"二级模块"字段的选项，作为新表同字段的快照（不是实时引用，源表以后加选项不会自动同步）
        source_fields = get_table_fields(token, SOURCE_TABLE_ID)
        module_options = []
        for f in source_fields:
            if f["field_name"] == FIELD_SOURCE_MODULE:
                module_options = f.get("property", {}).get("options", [])
                break
        module_options = [{"name": o["name"]} for o in module_options]
        print(f"读取到二级模块选项快照 {len(module_options)} 个")

        new_table_id = create_table(token, target_table_name, build_new_table_fields(module_options))
        print(f"已创建新表：{target_table_name}（table_id: {new_table_id}）")

    # ===== 第三步：读取用例库，按顺序循环分配，批量写入 =====
    source_records = get_all_records(token, SOURCE_TABLE_ID)
    print(f"用例库共 {len(source_records)} 条")

    records_to_insert = []
    for i, r in enumerate(source_records):
        fields = r["fields"]
        target = get_text_value(fields, FIELD_SOURCE_TARGET)
        module = get_select_value(fields, FIELD_SOURCE_MODULE)
        owner_name = new_order[i % len(new_order)]
        records_to_insert.append({
            "目标": target,
            "二级模块": module,
            "验收人": name_to_field_value[owner_name],
        })

    batch_create_records(token, new_table_id, records_to_insert)
    print(f"{target_table_name} 已写入 {len(records_to_insert)} 条用例")
    print(f"✅ 全部完成：{target_table_name}（table_id: {new_table_id}）")


if __name__ == "__main__":
    main()