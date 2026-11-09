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
from collections import defaultdict

from beehive_people import beehive_send, beehive_id, display_name, DEPARTED

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
# 报告"以人为本"：按提出人 / 问题负责人小组分别发，每个人只看自己要补什么。
# 近 N 天之外的当历史存量，不再打扰 —— 太老的问题多半已经不存在了。
FRESH_DAYS = int(os.environ.get("FRESH_DAYS", "").strip() or 60)
# 必填只对「待修复」强制。已修复/未重现/不需修复 这些状态下补字段没有意义。
REQUIRED_ONLY_STATUS = "待修复"

DEPRECATED_FIELDS = {"一级模块", "二级模块"}

# 这些值长得像填了，其实是默认值/占位符，一律当空
PLACEHOLDER_TEXTS = {"-", "无", "/", "[-](http://-)", "暂无"}

# 模块 → 应该是哪个应用端；带"后台"字样的模块统一归到 Web
MODULE_TO_PLATFORM = {"IM-Mac": "Mac", "IM-iPhone": "iPhone"}

# 首次发现版本号前缀 → 应用端
VERSION_PREFIX_TO_PLATFORM = {"Mac": "Mac", "IOS": "iPhone", "Web": "Web"}

# 这几个状态算"还没修完"
UNFINISHED_STATUS = {"待修复", "验证未通过待修复", "开发中"}

# 处理状态 → 负责人验证 允许的取值。来自 Webb 2026-11-09 定的流程：
# 负责人验证不是"验证修复管不管用"，而是"产品认不认可研发给的这个结论"，
# 所以每个处理状态都有对应的期望值，不存在"无需验证"。
#   已修复            → 已验证（验不过就该把状态改回 待修复）
#   待发版            → 待验证/验证未通过（对客强更后才算通过，同时改成 已修复）
#   待修复            → 待验证（还没验）/ 验证未通过（验了，还没修好）
#   待开发功能无需修复 → 验证未通过（功能没做，问题必然还在，产品持续跟进）
#   不需修复需求如此   → 已验证（认可）；不认可就把状态改回 待修复
#   未重现            → 已验证（认可）；不认可就把状态改回 待修复
#   验证未通过待修复   → 验证未通过（状态本身就是验证结果）
STATUS_TO_VERIFY = {
    "已修复": {"已验证"},
    "待发版": {"待验证", "验证未通过"},
    "待修复": {"待验证", "验证未通过"},
    "待开发功能无需修复": {"验证未通过"},
    "不需修复需求如此": {"已验证"},
    "未重现": {"已验证"},
    "验证未通过待修复": {"验证未通过"},
}
# 这两个状态本身就可疑，不管负责人验证填了什么都要列出来让人复核：
# 开发中 = 研发多半改完忘了改状态；临时hold = 挂起的不能忘了捞回来
ALWAYS_REVIEW_STATUS = {
    "开发中": "研发多半已经改完忘了改状态",
    "临时hold": "挂起中，需要定期捞回来复核",
}

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

def is_fresh(rec, today):
    """只看近 FRESH_DAYS 天提出的问题。更早的当历史存量，不再打扰"""
    d = as_date(rec, "问题提出时间")
    return d is not None and (today - d).days <= FRESH_DAYS


def owner_team(rec):
    """「问题负责人」是从模块表 lookup 出来的，值形如 "爱德 Lexia"，本身就是一个小组。
    按组聚合、组内几位一起 @，而不是拆成个人 —— 拆开会把同一条问题重复计好几遍"""
    return text_of(raw(rec, "问题负责人"))


def reporter_consistency(rec):
    """录入时就该填对的一致性问题，归提出人"""
    out = []
    module, platform = sel(rec, "模块"), sel(rec, "应用端")
    want = MODULE_TO_PLATFORM.get(module) or ("Web" if "后台" in module else None)
    if want and platform and platform != want:
        out.append(f"模块「{module}」的应用端应该是 {want}，填成了 {platform}")
    version = sel(rec, "首次发现版本号")
    want2 = next((p for prefix, p in VERSION_PREFIX_TO_PLATFORM.items()
                  if version.startswith(prefix)), None)
    if want2 and platform and platform != want2:
        out.append(f"版本号「{version}」的应用端应该是 {want2}，填成了 {platform}")
    return out


def owner_items(rec, status):
    """推进过程中该补的，归问题负责人小组"""
    out = []
    if sel(rec, "是否要改设计") == "是" and is_blank(rec, "设计稿链接") and is_blank(rec, "设计稿图片"):
        out.append("要改设计，但设计稿链接和图片都没填")
    if sel(rec, "进Sprint解决").startswith("SP"):
        for name in ("计划修复时间", "技术负责人"):
            if is_blank(rec, name):
                out.append(f"已进 Sprint，但 {name} 没填")
    if status == "已修复" and is_blank(rec, "上线日期"):
        out.append("已修复，但上线日期没填")
    allowed = STATUS_TO_VERIFY.get(status)
    if allowed and sel(rec, "负责人验证") not in allowed:
        want = "/".join(sorted(allowed))
        now_v = sel(rec, "负责人验证") or "空"
        out.append(f"处理状态={status} → 负责人验证应为「{want}」，现在是「{now_v}」")
    if status in ALWAYS_REVIEW_STATUS:
        out.append(f"处理状态={status}（{ALWAYS_REVIEW_STATUS[status]}）")
    if status in UNFINISHED_STATUS and any(n in DEPARTED for n in owner_names(rec)):
        out.append("技术负责人已离职，没人会收到到期提醒")
    return out


def render_people(title, data, unit, tail):
    """一个人/一组一段。返回 (正文, 要@的人)；没内容返回 (None, [])"""
    if not data:
        return None, []
    keys = sorted(data, key=lambda g: -sum(len(v) for v in data[g].values()))
    body, ats, grand = [], [], 0
    for g in keys:
        names = [n for n in g.split() if n]          # 小组是空格分隔的几个人
        label = "".join(f"@{display_name(n)}" for n in names if beehive_id(n))
        ats += [n for n in names if beehive_id(n)]
        if not label:
            continue
        n_total = sum(len(v) for v in data[g].values())
        grand += n_total
        body.append(f"\n{label}（{n_total} 处）")
        for item, ks in sorted(data[g].items(), key=lambda kv: -len(kv[1])):
            more = f" 另有{len(ks) - MAX_KEYS}条" if len(ks) > MAX_KEYS else ""
            body.append(f"  · {item}：{len(ks)} 处｜{unit} {'/'.join(ks[:MAX_KEYS])}{more}")
    if not body:
        return None, []
    return f"{title}｜近 {FRESH_DAYS} 天共 {grand} 处\n" + "\n".join(body) + tail, ats


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

    # 3.3 处理状态 ↔ 负责人验证。按状态分开报，不然一条里混了七八种情况没法改
    by_status = defaultdict(list)
    for r in records:
        status = sel(r, "处理状态")
        allowed = STATUS_TO_VERIFY.get(status)
        if allowed and sel(r, "负责人验证") not in allowed:
            by_status[status].append(r)
    for status in sorted(by_status, key=lambda k: -len(by_status[k])):
        want = "/".join(sorted(STATUS_TO_VERIFY[status]))
        out.append((f"处理状态={status}，但负责人验证不是「{want}」", by_status[status]))

    # 开发中 / 临时hold：状态本身就要人工复核
    for status, why in ALWAYS_REVIEW_STATUS.items():
        hits = [r for r in records if sel(r, "处理状态") == status]
        if hits:
            out.append((f"处理状态={status}（{why}）", hits))

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

    # 技术负责人已离职，但问题还没修完 —— 没人会收到到期提醒，等于无人认领。
    # 离职名单只能人工维护（见 beehive_people.DEPARTED 的注释，通讯录查不出来）。
    # 已经关闭的记录不报：那是历史署名，清掉反而丢了"当时是谁修的"。
    hits = [r for r in records if sel(r, "处理状态") in UNFINISHED_STATUS
            and any(n in DEPARTED for n in owner_names(r))]
    if hits:
        out.append(("技术负责人已离职，但问题还没修完（没人会收到提醒）", hits))

    return out


# ---------- 盲测 ↔ 问题池 交叉校验 ----------
# 关联键：问题池「来源用例编号」填当月验收表「目标」列那个 ID（= 用例库主键）。
# 这是两张表之间唯一的关联，2026-11-09 建立，之前完全没有交叉校验。
BLIND_TEST_APP_TOKEN = "FnFab3FDKa0JU6sqa19cDVpHnM7"
CASE_LIB_TABLE_ID = "tbl46z8GYP5HN1MJ"      # 盲测用例库
FIELD_CASE_REF = "来源用例编号"
FIELD_CASE_ID = "主键"                       # 用例库里的唯一 ID
FAIL_RESULTS = {"不通过"}                    # 跟 blind_test_screenshot_check 的口径一致


def find_blind_test_table(token, now):
    """当月验收表叫 M{月}验收表，找不到返回 None（月初还没建表时会这样）"""
    want = f"M{now.month}验收表"
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
           f"{BLIND_TEST_APP_TOKEN}/tables")
    data = api("GET", url, token=token, params={"page_size": 100})
    if data.get("code") != 0:
        raise RuntimeError(f"拉取数据表列表失败：{data}")
    for t in data["data"]["items"]:
        if t.get("name") == want:
            return t["table_id"]
    return None


def parse_case_refs(rec):
    """「来源用例编号」允许一条问题关联多个用例，逗号分隔"""
    raw_text = sel(rec, FIELD_CASE_REF)
    if not raw_text:
        return []
    return [x.strip() for x in raw_text.replace("，", ",").split(",") if x.strip()]


def check_blind_test_link(records, month_rows, case_ids):
    """两个方向：①填的编号用例库里有没有 ②盲测挂了的用例问题池有没有录"""
    out = []

    bad = [r for r in records
           if any(ref not in case_ids for ref in parse_case_refs(r))]
    if bad:
        out.append((f"「{FIELD_CASE_REF}」填的编号在用例库里不存在", bad))

    if month_rows is None:
        return out
    linked = {ref for r in records for ref in parse_case_refs(r)}
    missing = []
    for row in month_rows:
        case_id = sel(row, "目标")
        if not case_id or case_id in linked:
            continue
        failed = [p for p in ("Mac", "iPhone")
                  if sel(row, f"{p}验收结果") in FAIL_RESULTS]
        if failed:
            # 伪装成一条记录：主键放用例 ID，验收人原样带过来，
            # 这样报告里能按验收人 @ 到具体的人
            missing.append({"fields": {"主键": case_id,
                                       "验收人": row.get("fields", {}).get("验收人")}})
    if missing:
        out.append(("盲测标了「不通过」，但问题池里没有对应记录（疑似漏录）", missing))
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
    # 写"另有N条"而不是"等N条"：后者会被读成总数，和前面的条数对不上
    more = f" 另有{len(hits) - MAX_KEYS}条" if len(hits) > MAX_KEYS else ""
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

    # 盲测交叉校验：拿不到盲测表就跳过这一节，不要让整个巡检挂掉
    cross = []
    try:
        case_ids = {sel(r, FIELD_CASE_ID)
                    for r in get_all_records(token, BLIND_TEST_APP_TOKEN, CASE_LIB_TABLE_ID)}
        case_ids.discard("")
        month_table = find_blind_test_table(token, now)
        month_rows = (get_all_records(token, BLIND_TEST_APP_TOKEN, month_table)
                      if month_table else None)
        cross = check_blind_test_link(records, month_rows, case_ids)
        print(f"盲测交叉校验：用例库 {len(case_ids)} 条 ID，"
              f"当月表 {len(month_rows) if month_rows else 0} 条")
    except Exception as e:
        print(f"⚠️ 盲测交叉校验跳过（{e}）")

    # ---------- 按人/按组归集，分三条消息发 ----------
    reporter = defaultdict(lambda: defaultdict(list))   # 提出人 → 问题 → [主键]
    groups = defaultdict(lambda: defaultdict(list))     # 负责人小组 → 问题 → [主键]

    for r in records:
        if not is_fresh(r, today):
            continue
        k, status = key_of(r), sel(r, "处理状态")

        rep = (owner_names(r, "提出人") or [None])[0]
        # @ 不到的人（离职、高管）报了也没用，直接跳过
        if rep and beehive_id(rep):
            if status == REQUIRED_ONLY_STATUS:
                for name in REQUIRED_FIELDS:
                    if name == "提出人":
                        continue
                    if is_blank(r, name):
                        reporter[rep][f"{name} 没填"].append(k)
            for title in reporter_consistency(r):
                reporter[rep][title].append(k)

        team = owner_team(r)
        if team:
            for title in owner_items(r, status):
                groups[team][title].append(k)

    msg_reporter, at_reporter = render_people(
        f"🧹 问题池巡检 · 录入质量（提出人）", reporter, "主键",
        f"\n\n说明：只看「{REQUIRED_ONLY_STATUS}」且近 {FRESH_DAYS} 天提出的问题。"
        f"\n📋 问题池：{ISSUE_TABLE_URL}")
    msg_owner, at_owner = render_people(
        f"🧹 问题池巡检 · 推进质量（问题负责人）", groups, "主键",
        "\n\n说明：按模块负责小组 @，组内几位一起收到，认领后处理即可。"
        f"\n📋 问题池：{ISSUE_TABLE_URL}")
    blind_data = {}
    for title, hits in cross:
        for r in hits:
            who = (owner_names(r, "验收人") or owner_names(r, "提出人") or [""])[0]
            if who and beehive_id(who):
                blind_data.setdefault(who, defaultdict(list))[title].append(key_of(r))
    msg_blind, at_blind = render_people(
        "🔗 盲测漏录（验收人）", blind_data, "用例",
        "\n\n录进问题池时，把用例编号填进「来源用例编号」即可消除。")

    # 逾期清单跟着研发群那条走，这里不再重复发（见 issue_plan_due_notify.py）
    print(f"逾期未修 {len(overdue)} 条（由研发群那条提醒负责，这里不发）")

    sent = 0
    for tag, text, ats in (("录入质量", msg_reporter, at_reporter),
                           ("推进质量", msg_owner, at_owner),
                           ("盲测漏录", msg_blind, at_blind)):
        if text is None:
            print(f"✅ {tag}：没有要报的，不发")
            continue
        sent += 1
        if DRY_RUN:
            print(f"\n== DRY RUN ==（不发送）第 {sent} 条 / {tag}\n{text}\n待 @ 的人：{ats}")
        else:
            beehive_send(text, ats)
    if sent == 0 and not DRY_RUN:
        beehive_send(f"🧹 问题池巡检｜{now:%m-%d}｜近 {FRESH_DAYS} 天没有发现问题 ✅", [])


if __name__ == "__main__":
    main()
