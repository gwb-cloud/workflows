# -*- coding: utf-8 -*-
"""验收问题表（产品问题池）规则巡检

不只统计空值，而是按一份明确的规则清单去判，报告"违反了哪条规则、多少条、
是谁的"。规则分三类：

  A 必填     —— 任何时候都不该为空
  B 条件必填 —— 满足某个条件才要求填（比如进了 Sprint 才要求计划修复时间）
  C 一致性   —— 不是空，是几个字段互相矛盾（比如模块是 IM-Mac 但应用端写了 Web）

另外单独有一条"已过计划修复时间还停在待修复"，这条会 @ 到技术负责人。

两个坑，改规则前先看一眼：
1. 这张表原来 13 个字段配了默认值（问题类型=体验问题、设计稿链接=假链接 等），
   导致"有值"不等于"有人填过"。2026-11-07 已经把默认值清掉了，但 PLACEHOLDER_TEXTS
   这段逻辑要留着：历史行和手填都还会出现 "-" "无" "/" 这种占位值。
2. 「一级模块」「二级模块」准备废弃了，不参与校验。

需要 GitHub Actions Secrets：
- FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI
- BEEHIVE_WEBHOOK（先用测试群验证，稳定后再换正式群）
"""

import requests
import os
import time
from datetime import datetime, timezone, timedelta

from beehive_people import mention, beehive_send

BEIJING_TZ = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID_CLI"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET_CLI"]

ISSUE_APP_TOKEN = "OR5ubORn3atfo3szLSTcbnVdnTf"
ISSUE_TABLE_ID = "tbl3vBsukTCg1yrn"
ISSUE_TABLE_URL = (f"https://pcnpuds47gj5.feishu.cn/base/{ISSUE_APP_TOKEN}"
                   f"?table={ISSUE_TABLE_ID}&view=vewmkD3ctI")

# ========== 规则清单 ==========

# A 必填：这些列空着就是漏填
REQUIRED_FIELDS = [
    "问题描述", "应用端", "问题定性", "提出人", "问题提出时间",
    "模块", "问题类型", "问题发现阶段", "处理状态",
    "页面截图", "首次发现版本号",
]

# 废弃字段，不校验（即使有空值也不报）
DEPRECATED_FIELDS = {"一级模块", "二级模块"}

# 这些值长得像填了，其实是默认值/占位符，一律当空
PLACEHOLDER_TEXTS = {"-", "无", "/", "[-](http://-)", "暂无"}

# 模块 → 应该是哪个应用端；带"后台"字样的模块统一归到 Web
MODULE_TO_PLATFORM = {"IM-Mac": "Mac", "IM-iPhone": "iPhone"}

# 首次发现版本号前缀 → 应用端
VERSION_PREFIX_TO_PLATFORM = {"Mac": "Mac", "IOS": "iPhone", "Web": "Web"}

# 处理状态=已修复 时，负责人验证应该是这个值
# 注意：这个字段的枚举是 已验证/待验证/验证未通过，没有"验证通过"这一项
VERIFIED_OK = "已验证"

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() == "true"
# 每条规则在报告里最多列几个主键，免得消息刷屏
MAX_KEYS = int(os.environ.get("MAX_KEYS", "").strip() or 12)

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


# ========== 取值 ==========

def raw(rec, name):
    return rec.get("fields", {}).get(name)


def text_of(value):
    """把飞书各种字段长相压成一个字符串，取不到就空串"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return str(value.get("name") or value.get("text") or "").strip()
    if isinstance(value, (list, tuple)):
        return text_of(value[0]) if value else ""
    return str(value)


def sel(rec, name):
    """单选/多选取第一个值。飞书不同接口有时给 "通过" 有时给 ["通过"]，都兼容"""
    return text_of(raw(rec, name))


def is_blank(rec, name):
    """空值判定。占位符（设计稿链接的默认值那种）也算空"""
    value = raw(rec, name)
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip() in PLACEHOLDER_TEXTS
    if isinstance(value, (list, tuple)):
        if not value:
            return True
        return all(text_of(v) == "" or text_of(v) in PLACEHOLDER_TEXTS for v in value)
    if isinstance(value, dict):
        t = text_of(value)
        return not t or t in PLACEHOLDER_TEXTS
    return False


def as_date(rec, name):
    """日期字段是毫秒时间戳。飞书纯日期换算到北京时间会停在前一天23:00，要补1小时"""
    value = raw(rec, name)
    if not isinstance(value, (int, float)) or not value:
        return None
    return (datetime.fromtimestamp(value / 1000, tz=BEIJING_TZ) + timedelta(hours=1)).date()


def key_of(rec):
    return text_of(raw(rec, "主键")) or rec.get("record_id", "?")


def owner_names(rec, name="技术负责人"):
    value = raw(rec, name)
    if not isinstance(value, (list, tuple)):
        return []
    return [text_of(v) for v in value if text_of(v)]


# ========== 规则判定 ==========

def check_required(records):
    out = []
    for name in REQUIRED_FIELDS:
        if name in DEPRECATED_FIELDS:
            continue
        hits = [r for r in records if is_blank(r, name)]
        if hits:
            out.append((f"{name} 为空", hits))
    return out


def check_conditional(records):
    out = []

    # 3.1 要改设计 → 设计稿链接 或 设计稿图片 至少有一个
    hits = [r for r in records if sel(r, "是否要改设计") == "是"
            and is_blank(r, "设计稿链接") and is_blank(r, "设计稿图片")]
    if hits:
        out.append(("是否要改设计=是，但设计稿链接和设计稿图片都没填", hits))

    # 3.2 进了 Sprint → 计划修复时间 + 技术负责人 必填
    in_sprint = [r for r in records if sel(r, "进Sprint解决").startswith("SP")]
    for name in ("计划修复时间", "技术负责人"):
        hits = [r for r in in_sprint if is_blank(r, name)]
        if hits:
            out.append((f"已进 Sprint，但 {name} 为空", hits))

    # 3.4 已修复 → 必须有上线日期
    hits = [r for r in records if sel(r, "处理状态") == "已修复" and is_blank(r, "上线日期")]
    if hits:
        out.append(("处理状态=已修复，但上线日期为空", hits))

    # 待修复却连计划修复时间都没有 —— 不算硬违例，但值得盯
    hits = [r for r in records if sel(r, "处理状态") == "待修复" and is_blank(r, "计划修复时间")]
    if hits:
        out.append(("处理状态=待修复，但没有计划修复时间", hits))

    return out


def check_consistency(records):
    out = []

    # 3.3 已修复 → 负责人验证应为「已验证」
    hits = [r for r in records if sel(r, "处理状态") == "已修复"
            and sel(r, "负责人验证") != VERIFIED_OK]
    if hits:
        out.append((f"处理状态=已修复，但负责人验证不是「{VERIFIED_OK}」", hits))

    # 3.7 模块 ↔ 应用端
    hits = []
    for r in records:
        module, platform = sel(r, "模块"), sel(r, "应用端")
        want = MODULE_TO_PLATFORM.get(module) or ("Web" if "后台" in module else None)
        if want and platform and platform != want:
            hits.append(r)
    if hits:
        out.append(("模块和应用端对不上（IM-Mac→Mac / IM-iPhone→iPhone / 后台类→Web）", hits))

    # 首次发现版本号 ↔ 应用端
    hits = []
    for r in records:
        version, platform = sel(r, "首次发现版本号"), sel(r, "应用端")
        want = next((p for prefix, p in VERSION_PREFIX_TO_PLATFORM.items()
                     if version.startswith(prefix)), None)
        if want and platform and platform != want:
            hits.append(r)
    if hits:
        out.append(("首次发现版本号和应用端对不上", hits))

    # 首次发现版本号的问题，提出时间不该早于……（留给以后接发布节点表，先不判）
    return out


def check_overdue(records, today):
    """3.8 过了计划修复时间还停在待修复 —— 要 @ 技术负责人"""
    hits = []
    for r in records:
        if sel(r, "处理状态") != "待修复":
            continue
        plan = as_date(r, "计划修复时间")
        if plan and plan < today:
            hits.append(r)
    return hits


# ========== 报告 ==========

def fmt(title, hits, total):
    keys = [key_of(r) for r in hits[:MAX_KEYS]]
    more = f" 等{len(hits)}条" if len(hits) > MAX_KEYS else ""
    return f"- {title}：{len(hits)} 条（{len(hits) / total:.0%}）｜主键 {'/'.join(keys)}{more}"


def main():
    now = datetime.now(BEIJING_TZ)
    today = now.date()
    token = get_tenant_token()
    records = get_all_records(token, ISSUE_APP_TOKEN, ISSUE_TABLE_ID)
    total = len(records)
    if not total:
        print("表里没有记录，跳过")
        return

    required = check_required(records)
    conditional = check_conditional(records)
    consistency = check_consistency(records)
    overdue = check_overdue(records, today)

    lines = [f"🧹 问题池规则巡检｜{now:%m-%d}｜共 {total} 条"]
    at_names = []

    for label, icon, groups in (("必填缺失", "🔴", required),
                                ("条件必填", "🟠", conditional),
                                ("字段互相矛盾", "🟡", consistency)):
        lines.append(f"\n{icon} {label}：")
        if groups:
            for title, hits in sorted(groups, key=lambda g: -len(g[1])):
                lines.append(fmt(title, hits, total))
        else:
            lines.append("- 无")

    lines.append("\n⏰ 过了计划修复时间还停在待修复：")
    if overdue:
        by_owner = {}
        for r in overdue:
            names = owner_names(r) or ["未指派技术负责人"]
            for n in names:
                by_owner.setdefault(n, []).append(key_of(r))
        for name, keys in sorted(by_owner.items(), key=lambda kv: -len(kv[1])):
            shown = "/".join(keys[:MAX_KEYS]) + (f" 等{len(keys)}条" if len(keys) > MAX_KEYS else "")
            if name == "未指派技术负责人":
                lines.append(f"- {name}：{len(keys)} 条｜主键 {shown}")
            else:
                lines.append(f"- {mention(name)}：{len(keys)} 条｜主键 {shown}")
                at_names.append(name)
    else:
        lines.append("- 无")

    lines.append(f"\n📋 问题池：{ISSUE_TABLE_URL}")

    text = "\n".join(lines)
    print(text)
    if DRY_RUN:
        print("\n== DRY RUN ==（不发送）")
        return
    beehive_send(text, at_names)


if __name__ == "__main__":
    main()
