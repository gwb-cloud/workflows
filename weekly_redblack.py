# -*- coding: utf-8 -*-
"""
每周一红黑榜月度累计统计脚本
功能：读取"团队行为记录表"，统计本自然月内每个人的红榜/黑榜次数，
      汇总成一条消息，通过蜂巢 webhook 发送到群里。

需要在 GitHub Actions 的 Secrets 里配置：
- FEISHU_APP_ID
- FEISHU_APP_SECRET
- BEEHIVE_WEBHOOK_PRODUCT

需要在下方"配置区"填入你自己的表格信息。
"""

import requests
import os
from datetime import datetime
from collections import defaultdict

# ========== 配置区：改成你自己的实际值 ==========

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
BEEHIVE_WEBHOOK = os.environ["BEEHIVE_WEBHOOK_P3_REDBLACK"]

# 团队行为记录表（红黑榜）的 app_token 和 table_id
TABLE_APP_TOKEN = "G2gsbrQTVaN6XXsNiEkcklvinAh"
TABLE_ID = "tblQyXjW8i85vfxT"

# 字段名，必须和表格里的字段名完全一致
FIELD_TYPE = "类型"          # 单选：红榜 / 黑榜
FIELD_ASPECT = "涉及方面"     # 单选
FIELD_DESC = "行为描述"       # 文本
FIELD_OWNER = "责任人"        # 人员字段
FIELD_OCCUR_DATE = "发生时间"  # 日期字段，统计口径按这个字段筛选"本月"

# "类型"字段里，代表红榜/黑榜的选项文字，需要和表格里的选项完全一致
TYPE_RED = "红榜"
TYPE_BLACK = "黑榜"

# ========== 以下不用改 ==========


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


def get_records(token):
    """拉取行为记录表所有记录，自动翻页"""
    records = []
    page_token = None
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{TABLE_APP_TOKEN}/tables/{TABLE_ID}/records"

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


def send_to_beehive(text):
    resp = requests.post(
        BEEHIVE_WEBHOOK,
        json={"msg_type": "text", "content": {"text": text}},
        timeout=10,
    )
    print(f"发送结果: {resp.status_code} {resp.text}")


from datetime import timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))


def parse_date(ms_timestamp):
    """飞书日期字段返回的是毫秒级时间戳。经实测验证：这类"纯日期"字段换算成北京时间后，
    时钟部分固定停在 23:00（比表格里显示的日期的0点少1小时），底层时区基准跟北京时间差1小时。
    这里换算成北京时间后再加1小时，才能拿到表格里实际显示的那个日期。"""
    if not ms_timestamp:
        return None
    return datetime.fromtimestamp(ms_timestamp / 1000, tz=BEIJING_TZ) + timedelta(hours=1)


def get_select_value(fields, field_name):
    """兼容单选字段的不同返回格式（字符串 / {"text":...} / [{"text":...}]）"""
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return first.get("text", "")
        return str(first)
    return ""


def get_person_name(fields, field_name):
    """人员字段通常返回 [{"name": "张三", "id": "ou_xxx"}]"""
    value = fields.get(field_name)
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return first.get("name", "未知")
    if isinstance(value, dict):
        return value.get("name", "未知")
    return "未知"


def main():
    token = get_tenant_token()
    records = get_records(token)
    now = datetime.now(BEIJING_TZ)
    current_month = now.month
    current_year = now.year

    red_count = defaultdict(int)
    black_count = defaultdict(int)

    for record in records:
        fields = record.get("fields", {})
        occur_date = parse_date(fields.get(FIELD_OCCUR_DATE))

        if not occur_date:
            continue
        if occur_date.year != current_year or occur_date.month != current_month:
            continue

        record_type = get_select_value(fields, FIELD_TYPE)
        owner = get_person_name(fields, FIELD_OWNER)

        if record_type == TYPE_RED:
            red_count[owner] += 1
        elif record_type == TYPE_BLACK:
            black_count[owner] += 1

    # 按次数从高到低排序
    red_sorted = sorted(red_count.items(), key=lambda x: x[1], reverse=True)
    black_sorted = sorted(black_count.items(), key=lambda x: x[1], reverse=True)

    red_text = "、".join(f"{name} {count}次" for name, count in red_sorted) or "暂无"
    black_text = "、".join(f"{name} {count}次" for name, count in black_sorted) or "暂无"

    text = (
        f"📊 本月红黑榜累计（截至{now.month}月{now.day}日）\n"
        f"🟢 红榜：{red_text}\n"
        f"🔴 黑榜：{black_text}"
    )

    send_to_beehive(text)
    print(text)


if __name__ == "__main__":
    main()
