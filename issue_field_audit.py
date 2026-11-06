# -*- coding: utf-8 -*-
"""
验收问题表字段完整性巡检
把「数据表」每一列的空值情况统计出来发到群里，避免问题录进来之后模块、负责人、
优先级这些关键字段一直空着没人补。

只统计人工需要填的列：自动编号、创建时间、修改时间、修改人、查找引用、公式这些
是系统自己算的，空着也不是谁的锅，默认跳过。

需要 GitHub Actions Secrets：
- FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI
- BEEHIVE_WEBHOOK（先用测试群验证，稳定后再换正式群）
"""

import requests
import os
import time
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID_CLI"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET_CLI"]
BEEHIVE_WEBHOOK = os.environ.get("BEEHIVE_WEBHOOK", "")

# 验收问题表（产品问题池）
ISSUE_APP_TOKEN = "OR5ubORn3atfo3szLSTcbnVdnTf"
ISSUE_TABLE_ID = "tbl3vBsukTCg1yrn"

# 系统自动算的列，空不空都不用管
AUTO_UI_TYPES = {"AutoNumber", "CreatedTime", "ModifiedTime", "CreatedUser",
                 "ModifiedUser", "Formula", "Lookup", "Button"}

# 这些列空着要重点提醒，排在报告最前面
DEFAULT_KEY_FIELDS = "模块,问题类型,问题定性,处理状态,问题负责人,问题描述"
# workflow_dispatch 没填的输入会传空字符串进来，不能直接用 get 的默认值
KEY_FIELDS = [f.strip() for f in
              (os.environ.get("KEY_FIELDS", "").strip() or DEFAULT_KEY_FIELDS).split(",") if f.strip()]

# 空值比例低于这个数的列不单独列出来，免得报告太长
MIN_RATIO = float(os.environ.get("MIN_RATIO", "0") or 0)

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() == "true"

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


def get_fields(token, app_token, table_id):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    data = api("GET", url, token=token, params={"page_size": 200})
    if data.get("code") != 0:
        raise RuntimeError(f"拉取字段失败：{data}")
    return data["data"]["items"]


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


def is_blank(value):
    """飞书的空值有好几种长相：缺键、None、""、[]、{}、[""]，都算空"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return all(is_blank(v) for v in value) if value else True
    if isinstance(value, dict):
        if "text" in value:
            return is_blank(value["text"])
        return not value
    return False


def send_to_beehive(text):
    if not BEEHIVE_WEBHOOK:
        print("⚠️ 没配 BEEHIVE_WEBHOOK，只打印不发送")
        return
    resp = requests.post(BEEHIVE_WEBHOOK, json={"msg_type": "text", "content": {"text": text}}, timeout=15)
    print(f"发送结果：{resp.status_code} {resp.text}")


def main():
    now = datetime.now(BEIJING_TZ)
    token = get_tenant_token()

    fields = get_fields(token, ISSUE_APP_TOKEN, ISSUE_TABLE_ID)
    records = get_all_records(token, ISSUE_APP_TOKEN, ISSUE_TABLE_ID)
    total = len(records)
    if not total:
        print("表里没有记录，跳过")
        return

    manual = [f for f in fields if f.get("ui_type") not in AUTO_UI_TYPES]
    skipped = [f["field_name"] for f in fields if f.get("ui_type") in AUTO_UI_TYPES]
    print(f"共 {total} 条记录，{len(fields)} 列，其中人工填写的 {len(manual)} 列；"
          f"跳过自动列：{'、'.join(skipped) or '无'}")

    stats = []
    for f in manual:
        name = f["field_name"]
        empty = sum(1 for r in records if is_blank(r.get("fields", {}).get(name)))
        stats.append((name, empty, empty / total))

    key_stats = [s for s in stats if s[0] in KEY_FIELDS and s[1] > 0]
    key_stats.sort(key=lambda s: -s[1])
    other_stats = [s for s in stats if s[0] not in KEY_FIELDS and s[1] > 0 and s[2] >= MIN_RATIO]
    other_stats.sort(key=lambda s: -s[1])
    clean = [s[0] for s in stats if s[1] == 0]

    lines = [f"🧹 验收问题表字段巡检｜{now:%m-%d}｜共 {total} 条"]

    if key_stats:
        lines.append("\n🔴 关键字段缺失：")
        for name, empty, ratio in key_stats:
            lines.append(f"- {name}：空 {empty} 条（{ratio:.0%}）")
    else:
        lines.append("\n✅ 关键字段都填齐了")

    if other_stats:
        lines.append("\n⚪️ 其它字段缺失（供参考）：")
        for name, empty, ratio in other_stats[:15]:
            lines.append(f"- {name}：空 {empty} 条（{ratio:.0%}）")
        if len(other_stats) > 15:
            lines.append(f"- …另有 {len(other_stats) - 15} 列有缺失")

    if clean:
        lines.append(f"\n填写完整的列：{'、'.join(clean)}")

    text = "\n".join(lines)
    print(text)
    if DRY_RUN:
        print("\n== DRY RUN ==（不发送）")
        return
    send_to_beehive(text)


if __name__ == "__main__":
    main()
