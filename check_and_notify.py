# -*- coding: utf-8 -*-
"""
每日验收巡检脚本（多群路由版）
功能：读取飞书多维表格"节点更新总表"，检查各节点是否按规定时间窗口完成，
      未完成的通过蜂巢 webhook 发消息提醒到对应的群。

需要在 GitHub Actions 的 Secrets 里配置：
- FEISHU_APP_ID
- FEISHU_APP_SECRET
- BEEHIVE_WEBHOOK_YANSHOU   （验收群 webhook）
- BEEHIVE_WEBHOOK_PRODUCT   （产品群 webhook，如暂不需要可以先不配，脚本会自动跳过）

需要在下方"配置区"填入你自己的表格信息。
"""

import requests
import os
from datetime import datetime, timedelta

# ========== 配置区：改成你自己的实际值 ==========

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]

# 多群路由表：key 是群的业务名称，value 是对应的 webhook 地址
# 以后新增群，只需要在这里加一行，加一个对应的 GitHub Secret，不用改下面的业务逻辑
WEBHOOK_MAP = {
    "验收群": os.environ.get("BEEHIVE_WEBHOOK_YANSHOU"),
    "产品群": os.environ.get("BEEHIVE_WEBHOOK_P1_NODE"),
}

# 多维表格的 app_token 和 table_id，从表格 URL 里取：
# https://xxx.feishu.cn/base/{app_token}?table={table_id}
TABLE_APP_TOKEN = "IC7ObBQ2ya2H4FsqT3ocPreYned" 
TABLE_ID = "tblSDaRAkltfVtx6"

# 字段名，必须和表格里的字段名完全一致
FIELD_VERSION = "版本号"
FIELD_RELEASE_DATE = "版本发布日期"
FIELD_P0_CHECKED = "验收无P0问题"
FIELD_FORBUD_MAC = "ForBud-Mac已更新"
FIELD_FORBUD_IPHONE = "ForBud-iPhone已更新"
FIELD_FORBUD_BACKEND = "ForBud-后台已更新"
FIELD_BLIND_TEST = "盲测用例已录入"
FIELD_HELP_DOC = "帮助文档已更新"
FIELD_I18N_CHECK = "多语言走查"

# "验收无P0问题"如果是单选字段，这里填完成时对应的选项文字
P0_DONE_VALUE = "是"

# 巡检提醒默认发到哪个群（以后红黑榜等其他消息可以指定发"产品群"）
DEFAULT_TARGET = "验收群"

FORCE_SEND = os.environ.get("FORCE_SEND", "false").lower() == "true"

# ========== 以下不用改 ==========


def get_tenant_token():
    """获取飞书 tenant_access_token"""
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
    """拉取总表所有记录，自动翻页"""
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


def send_to_beehive(text, target=DEFAULT_TARGET):
    """发送文本消息到指定的蜂巢群"""
    webhook_url = WEBHOOK_MAP.get(target)
    if not webhook_url:
        print(f"⚠️ 未配置「{target}」对应的 webhook 地址，跳过发送。消息内容：{text}")
        return
    resp = requests.post(
        webhook_url,
        json={"msg_type": "text", "content": {"text": text}},
        timeout=10,
    )
    print(f"发送到「{target}」结果: {resp.status_code} {resp.text}")


def parse_date(ms_timestamp):
    """飞书日期字段返回的是毫秒级时间戳"""
    if not ms_timestamp:
        return None
    return datetime.fromtimestamp(ms_timestamp / 1000)


def is_checked(fields, field_name):
    """判断复选框字段是否已勾选（兼容 True / None / False）"""
    return fields.get(field_name) is True


def is_p0_confirmed(fields):
    """判断'验收无P0问题'字段是否已确认，兼容单选/复选框两种取值形式"""
    value = fields.get(FIELD_P0_CHECKED)
    if isinstance(value, bool):
        return value is True
    if isinstance(value, str):
        return value == P0_DONE_VALUE
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return first.get("text") == P0_DONE_VALUE
    return False


def main():
    token = get_tenant_token()
    records = get_records(token)
    now = datetime.now()

    if FORCE_SEND:
        # 调试模式：跳过日期判断，验证"读表+发消息"全链路是否打通
        if records:
            fields = records[0].get("fields", {})
            version = fields.get(FIELD_VERSION, "未知版本")
            send_to_beehive(f"🔧 强制测试模式：成功读取到版本 {version} 的记录，webhook 发送正常")
        else:
            send_to_beehive("🔧 强制测试模式：表格已连通，但没有读到任何记录")
        print("FORCE_SEND 模式已执行，跳过正常巡检逻辑")
        return

    for record in records:
        fields = record.get("fields", {})
        version = fields.get(FIELD_VERSION, "未知版本")
        release_date = parse_date(fields.get(FIELD_RELEASE_DATE))

        if not release_date:
            continue

        if now - release_date > timedelta(days=7):
            continue

        days_since_release = (now - release_date).days
        problems = []

        if days_since_release >= 0:
            if not is_p0_confirmed(fields):
                problems.append("验收无P0问题 未确认")
            if not is_checked(fields, FIELD_FORBUD_MAC):
                problems.append("ForBud-Mac 更新记录 未更新")
            if not is_checked(fields, FIELD_FORBUD_IPHONE):
                problems.append("ForBud-iPhone 更新记录 未更新")
            if not is_checked(fields, FIELD_FORBUD_BACKEND):
                problems.append("ForBud-后台 更新记录 未更新")

        if days_since_release >= 1:
            if not is_checked(fields, FIELD_BLIND_TEST):
                problems.append("盲测用例 未在24H内录入")
            if not is_checked(fields, FIELD_HELP_DOC):
                problems.append("帮助文档 未在24H内更新")

        if problems:
            text = f"⚠️ 版本 {version} 验收巡检提醒：\n" + "\n".join(f"- {p}" for p in problems)
            send_to_beehive(text, target="验收群")
            print(text)
        else:
            print(f"版本 {version} 巡检通过，无异常")


if __name__ == "__main__":
    main()