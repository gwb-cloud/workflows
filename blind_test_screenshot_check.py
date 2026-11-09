# -*- coding: utf-8 -*-
"""
盲测截图校验脚本
规则：某一端的验收结果填了「通过」或「不通过」，就必须在对应的截图列放图；
「待验收」「不涉及或无此需求」和空值不需要。

检查两类问题，按验收人汇总并 @ 到人：
1. 漏填截图：结果填了但截图列是空的
2. 疑似截图错误：
   - Mac 截图列里放了竖图（多半是手机截图），iPhone 截图列里放了横图
   - 同一张图被反复用在多行（重复粘贴）
   - 放的根本不是图片文件
   「内容完全不相关」这类需要看懂图才知道的，几何特征判断不了，见 CLAUDE.md 里的说明。

需要 GitHub Actions Secrets：
- FEISHU_APP_ID_CLI / FEISHU_APP_SECRET_CLI
- BEEHIVE_WEBHOOK（先用测试群验证，稳定后再换成正式群的 webhook）
"""

import requests
import os
import io
import json
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from beehive_people import PERSON_BEEHIVE_ID, NAME_ALIAS, beehive_id, mention, beehive_send

from PIL import Image

BEIJING_TZ = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID_CLI"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET_CLI"]
BEEHIVE_WEBHOOK = os.environ.get("BEEHIVE_WEBHOOK", "")

BLIND_TEST_APP_TOKEN = "FnFab3FDKa0JU6sqa19cDVpHnM7"

# 节点更新总表：用来算"本月第几次发版"，跟 blind_test_tagging.py 一致
NODE_APP_TOKEN = "IC7ObBQ2ya2H4FsqT3ocPreYned"
NODE_TABLE_ID = "tblSDaRAkltfVtx6"
FIELD_NODE_RELEASE_DATE = "版本发布日期"

# 校验范围：round = 只校验当前这一轮发版的列（默认，刚上规则时历史欠账太多）；
#          all  = 总验收列 + 所有轮次一起校验
SCOPE = os.environ.get("SCOPE", "round").strip().lower()

FIELD_OWNER = "验收人"


def env_int(name, default):
    """workflow_dispatch 没填的输入会传空字符串进来，不能直接 int()"""
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


# 一个月最多几次发版（超出的列由 Webb 手动建，脚本只认已经存在的列）
MAX_ROUNDS = env_int("MAX_ROUNDS", 3)

# 结果列 → 截图列。列名在飞书里偶尔带空格（"Mac 截图"），匹配时统一去掉空白
BASE_PAIRS = [
    ("Mac验收结果", "Mac截图", "Mac"),
    ("iPhone验收结果", "iPhone截图", "iPhone"),
]
ROUND_RESULT = {"Mac": "第{n}次发版-Mac结果", "iPhone": "第{n}次发版-iPhone结果"}
ROUND_SHOT = {"Mac": "第{n}次发版-Mac截图", "iPhone": "第{n}次发版-iPhone截图"}

# 填了这两个值就必须有截图；其余（空、待验收、不涉及或无此需求）不要求
RESULT_NEED_SHOT = {"通过", "不通过"}

# 同一张图出现在几行以上算重复粘贴
DUP_THRESHOLD = env_int("DUP_THRESHOLD", 3)
# 单次最多下载多少张图做尺寸检查，防止跑太久/太费调用
MAX_DOWNLOAD = env_int("MAX_DOWNLOAD", 300)

STATE_FILE = "screenshot_state.json"

# 加白：确认过没问题的图，以后不再报。存在 state 的 "ignored" 里，跟尺寸缓存一起
# 提交回仓库，所以加一次就永久生效。
# 手动触发时用 ignore / unignore 传图片编号（报告里每条疑似后面那个 [xxxxxxxx]），
# 逗号分隔；也可以传完整 file_token。
IGNORE_INPUT = os.environ.get("IGNORE", "").strip()
UNIGNORE_INPUT = os.environ.get("UNIGNORE", "").strip()


def short_id(file_token):
    """报告里给人看的图片编号，取 file_token 前 8 位"""
    return file_token[:8]


def resolve_tokens(raw, known_tokens):
    """把用户填的编号/完整 token 解析成真正的 file_token。
    编号是前缀，所以可能撞车或者找不到，都要说清楚，不能默默吞掉"""
    out, unknown, ambiguous = [], [], []
    for piece in (raw or "").replace("，", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        hits = [t for t in known_tokens if t == piece or t.startswith(piece)]
        if len(hits) == 1:
            out.append(hits[0])
        elif not hits:
            unknown.append(piece)
        else:
            ambiguous.append(piece)
    return out, unknown, ambiguous

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() == "true"
FORCE_SEND = os.environ.get("FORCE_SEND", "").strip().lower() == "true"

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


def get_all_tables(token, app_token):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables"
    tables, page_token = [], None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        data = api("GET", url, token=token, params=params)
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


def get_field_names(token, app_token, table_id):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    data = api("GET", url, token=token, params={"page_size": 100})
    if data.get("code") != 0:
        raise RuntimeError(f"拉取字段失败：{data}")
    return [f["field_name"] for f in data["data"]["items"]]


def norm(name):
    """列名匹配统一去掉空白：飞书里手建的列名经常多一个空格（"Mac 截图"）"""
    return "".join(name.split())


def resolve(field_names, wanted):
    """按去空白后的名字找真实列名，找不到返回 None"""
    target = norm(wanted)
    for name in field_names:
        if norm(name) == target:
            return name
    return None


def get_select_value(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
    if isinstance(value, list) and value:
        first = value[0]
        return first.get("text", "") if isinstance(first, dict) else str(first)
    return ""


def get_person_name(fields, field_name):
    value = fields.get(field_name)
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return first.get("name", "未知")
    return "未知"


def get_attachments(fields, field_name):
    value = fields.get(field_name)
    return [a for a in value if isinstance(a, dict)] if isinstance(value, list) else []


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def download_size(token, file_token):
    """下载附件读出宽高。只认识得出尺寸的图片，其它返回 None"""
    url = f"https://open.feishu.cn/open-apis/drive/v1/medias/{file_token}/download"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if resp.status_code != 200 or resp.content[:1] == b"{":
            return None
        with Image.open(io.BytesIO(resp.content)) as img:
            return img.size
    except Exception as e:
        print(f"⚠️ 读取附件 {file_token} 失败：{e}")
        return None


# 手机整屏截图的尺寸特征。要防的是"把手机整屏图贴到 Mac 列"，
# 而手机整屏截图的尺寸一定精确等于某个设备分辨率；人在 Mac 上裁出来的
# 一小块是任意尺寸，所以只认分辨率、不认"是不是竖图"，误报趋近于零。
PHONE_RESOLUTIONS = {
    (1320, 2868),   # iPhone 16 Pro Max
    (1206, 2622),   # iPhone 16 Pro
    (1179, 2556),   # iPhone 15/16 Pro、15/16
    (1290, 2796),   # iPhone 14/15/16 Plus、Pro Max
    (1170, 2532),   # iPhone 12/13/14
    (1284, 2778),   # iPhone 12/13 Pro Max
    (1125, 2436),   # iPhone X/XS/11 Pro
    (1242, 2688),   # iPhone XS Max/11 Pro Max
    (828, 1792),    # iPhone XR/11
    (750, 1334),    # iPhone SE2/SE3/7/8
    (640, 1136),    # iPhone SE1
    (1080, 2340),   # 安卓常见
    (1080, 2400),
    (1440, 3200),
}
# 有人会把截图等比缩小再传，尺寸就对不上了，所以比例也认
PHONE_RATIOS = sorted({round(h / w, 4) for w, h in PHONE_RESOLUTIONS})
RATIO_TOLERANCE = 0.01   # 1%


def _is_phone_shot(w, h):
    """这张图是不是一张"没动过的手机整屏截图"（含横屏和等比缩放）"""
    if (w, h) in PHONE_RESOLUTIONS or (h, w) in PHONE_RESOLUTIONS:
        return True
    # 等比缩放：比例对上且仍是整屏量级（裁出来的小块比例几乎不可能正好落在设备比例上）
    long_side, short_side = max(w, h), min(w, h)
    if short_side < 500:
        return False
    ratio = round(long_side / short_side, 4)
    return any(abs(ratio - r) <= r * RATIO_TOLERANCE for r in PHONE_RATIOS)


def parse_date(ms_timestamp):
    """飞书日期字段换算成北京时间后固定停在23:00，需要再加1小时才能拿到正确日期"""
    if not ms_timestamp:
        return None
    return datetime.fromtimestamp(ms_timestamp / 1000, tz=BEIJING_TZ) + timedelta(hours=1)


def current_round(token, now):
    """当前是本月第几次发版：当月发布日期排序后，取今天所属的那一轮
    （今天还没到第一次发布就算第1轮，过了最后一次就算最后一轮）"""
    dates = sorted({d for d in (parse_date(r.get("fields", {}).get(FIELD_NODE_RELEASE_DATE))
                                for r in get_all_records(token, NODE_APP_TOKEN, NODE_TABLE_ID))
                    if d and d.year == now.year and d.month == now.month})
    if not dates:
        return None
    today = now.date()
    for i, d in enumerate(dates):
        if today <= d.date():
            return i + 1
    return len(dates)


def build_pairs(field_names, only_round=None):
    """返回 [(结果列, 截图列, 端, 分组名)]，只保留表里真实存在的列。
    only_round 不为空时，只校验那一轮的列，总验收列和其它轮次都不看"""
    pairs = []
    if only_round is None:
        for result_name, shot_name, platform in BASE_PAIRS:
            r = resolve(field_names, result_name)
            s = resolve(field_names, shot_name)
            if r and s:
                pairs.append((r, s, platform, "总验收"))
            elif r and not s:
                print(f"⚠️ 有结果列「{r}」但找不到对应的截图列「{shot_name}」，这一组跳过")
    rounds = [only_round] if only_round else range(1, MAX_ROUNDS + 1)
    for n in rounds:
        for platform in ("Mac", "iPhone"):
            r = resolve(field_names, ROUND_RESULT[platform].format(n=n))
            s = resolve(field_names, ROUND_SHOT[platform].format(n=n))
            if r and s:
                pairs.append((r, s, platform, f"第{n}次发版"))
            elif r and not s:
                print(f"⚠️ 有结果列「{r}」但找不到对应的截图列，这一组跳过")
    return pairs


def main():
    now = datetime.now(BEIJING_TZ)
    if now.weekday() not in (0, 2, 4) and not FORCE_SEND:
        print(f"今天（北京时间 {now:%Y-%m-%d}）不是周一/三/五，跳过。手动触发传 force_send=true 可强制执行")
        return

    token = get_tenant_token()
    table_name = f"M{now.month}验收表"
    table_id = find_table_by_name(token, BLIND_TEST_APP_TOKEN, table_name)
    if not table_id:
        raise RuntimeError(f"没找到 {table_name}")

    only_round = None
    if SCOPE == "round":
        only_round = current_round(token, now)
        if only_round is None:
            print("节点更新总表里本月没有填发布日期，算不出当前是第几次发版，跳过")
            return

    field_names = get_field_names(token, BLIND_TEST_APP_TOKEN, table_id)
    pairs = build_pairs(field_names, only_round)
    if not pairs:
        print(f"没有任何「结果列 + 截图列」配对（SCOPE={SCOPE}），跳过")
        return
    scope_desc = f"只看第{only_round}次发版" if only_round else "总验收 + 全部轮次"
    print(f"{table_name}：校验范围 {scope_desc}，参与的列 {len(pairs)} 组 → "
          + "、".join(f"{g}/{p}" for _r, _s, p, g in pairs))

    records = get_all_records(token, BLIND_TEST_APP_TOKEN, table_id)
    print(f"记录条数：{len(records)}")

    missing = defaultdict(lambda: defaultdict(int))      # 人 → 分组/端 → 漏填数
    shot_refs = defaultdict(list)                        # file_token → [(人, 组, 端)]
    to_check = []                                        # 待下载检查的图
    filled_total = 0

    for r in records:
        fields = r["fields"]
        owner = get_person_name(fields, FIELD_OWNER)
        for result_name, shot_name, platform, group in pairs:
            if get_select_value(fields, result_name).strip() not in RESULT_NEED_SHOT:
                continue
            shots = get_attachments(fields, shot_name)
            if not shots:
                missing[owner][f"{group}-{platform}"] += 1
                continue
            filled_total += 1
            for a in shots:
                ft = a.get("file_token")
                if not ft:
                    continue
                shot_refs[ft].append((owner, group, platform))
                to_check.append((ft, a.get("name", ""), a.get("type", ""), owner, group, platform))

    # ---------- 图片本身的检查 ----------
    state = load_state()
    sizes = state.get("sizes", {})
    ignored = state.get("ignored", {})

    # 本次跑之前先处理加白/取消加白的输入
    all_tokens = sorted({t for t, *_ in to_check})
    if UNIGNORE_INPUT:
        # 去重：同一个 token 可能既在 ignored 里又在本次的图里，不去重会被当成"编号撞车"
        hit, unknown, ambi = resolve_tokens(UNIGNORE_INPUT, sorted(set(ignored) | set(all_tokens)))
        for t in hit:
            ignored.pop(t, None)
            print(f"↩️ 取消加白 {short_id(t)}")
        for t in unknown:
            print(f"⚠️ 取消加白失败：找不到编号 {t}")
        for t in ambi:
            print(f"⚠️ 取消加白失败：编号 {t} 对应多张图，请填完整 file_token")
    if IGNORE_INPUT:
        hit, unknown, ambi = resolve_tokens(IGNORE_INPUT, all_tokens)
        for t in hit:
            ignored[t] = now.strftime("%Y-%m-%d")
            print(f"✅ 已加白 {short_id(t)}")
        for t in unknown:
            print(f"⚠️ 加白失败：这次的图里找不到编号 {t}（只能加白本次报告里出现过的图）")
        for t in ambi:
            print(f"⚠️ 加白失败：编号 {t} 对应多张图，请填完整 file_token")

    wrong = defaultdict(list)     # 人 → [问题描述]
    downloaded = 0
    from_cache = 0
    ignored_hits = 0
    seen = set()
    for ft, fname, mime, owner, group, platform in to_check:
        if ft in ignored:
            ignored_hits += 1
            continue
        if mime and not mime.startswith("image/"):
            wrong[owner].append(f"{group}-{platform} 放的不是图片（{fname or mime}）")
            continue
        if ft in seen:
            continue
        seen.add(ft)
        if ft not in sizes:
            if downloaded >= MAX_DOWNLOAD:
                continue
            fetched = download_size(token, ft)
            downloaded += 1
            if fetched:
                sizes[ft] = list(fetched)
        else:
            from_cache += 1
        size = sizes.get(ft)
        if not size:
            continue
        w, h = size
        phone = _is_phone_shot(w, h)
        if platform == "Mac" and phone:
            wrong[owner].append(f"{group}-Mac 截图是手机整屏截图（{w}×{h}）[{short_id(ft)}]")
        elif platform == "iPhone" and not phone and w > h * 1.2 and w >= 1200:
            wrong[owner].append(f"{group}-iPhone 截图是大横图（{w}×{h}，疑似电脑截图）[{short_id(ft)}]")

    for ft, refs in shot_refs.items():
        if ft in ignored:
            continue
        rows = len({(o, g, p) for o, g, p in refs})
        if len(refs) >= DUP_THRESHOLD and rows >= DUP_THRESHOLD:
            owner = refs[0][0]
            wrong[owner].append(f"同一张图被用在 {len(refs)} 处 [{short_id(ft)}]")

    state["sizes"] = sizes
    state["ignored"] = ignored
    state["last_run"] = now.strftime("%Y-%m-%d %H:%M")
    save_state(state)

    # ---------- 拼报告 ----------
    miss_total = sum(sum(v.values()) for v in missing.values())
    wrong_total = sum(len(v) for v in wrong.values())
    lines = [f"📸 盲测截图校验｜{table_name}｜{now:%m-%d}｜{scope_desc}",
             f"已填截图 {filled_total} 处，本次下载 {downloaded} 张 + 读缓存 {from_cache} 张"
             + (f"，跳过已加白 {ignored_hits} 处" if ignored_hits else "")]
    at_names = []

    if miss_total:
        lines.append(f"\n❌ 漏填截图 {miss_total} 处，涉及 {len(missing)} 人：")
        for owner, detail in sorted(missing.items(), key=lambda kv: -sum(kv[1].values())):
            parts = "、".join(f"{k} {v}" for k, v in sorted(detail.items(), key=lambda kv: -kv[1]))
            lines.append(f"- {mention(owner)}：{sum(detail.values())} 处（{parts}）")
            if beehive_id(owner):
                at_names.append(owner)
    else:
        lines.append("\n✅ 没有漏填")

    if wrong_total:
        lines.append(f"\n⚠️ 疑似截图放错 {wrong_total} 处：")
        for owner, items in sorted(wrong.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- {mention(owner)}：")
            for it in items[:5]:
                lines.append(f"    · {it}")
            if len(items) > 5:
                lines.append(f"    · …另有 {len(items) - 5} 处")
            if beehive_id(owner):
                at_names.append(owner)
    else:
        lines.append("\n✅ 没有发现放错的截图")

    lines.append("\n规则：结果填了「通过」或「不通过」就要配截图；「待验收」「不涉及或无此需求」不用。")
    if wrong_total:
        lines.append("截图确认没问题的，拿后面那个 [编号] 去 Actions 手动触发本流程、填进 ignore，以后不再报。")
    text = "\n".join(lines)

    if DRY_RUN:
        print("== DRY RUN ==（不发送）\n" + text)
        print(f"\n待 @ 的人：{at_names}")
        return
    beehive_send(text, at_names)


if __name__ == "__main__":
    main()
