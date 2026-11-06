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

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() == "true"
FORCE_SEND = os.environ.get("FORCE_SEND", "").strip().lower() == "true"

# 蜂巢账号（key 是蜂巢里的显示名），用于 @ 到人
PERSON_BEEHIVE_ID = {
    "郭文博": "ouv4qovzpupe9m",
    "爱德": "ouv4qovznoxoxq",
    "雨纯": "ouv4qovzoc6cad",
    "段苏宸": "ouvkmtuntg5xpk",
    "可大力": "ouv4qovzlarm8d",
    "秦汉": "ouviua2rcuub5e",
    "李静": "ouvcofw0bgs9hf",
    "梦成 Rhys": "ouv4qovznsatvc",
    "Crisley": "ouv4qow0vt3ksn",
    "Yvonne": "ouv4qovzmwpkuy",
    "唐炜": "ouv4qovznbncqw",
    "严光": "ouvgwgfyt1elue",
    "乐夏": "ouvwzhogat6shz",
}
# 飞书验收人姓名 → 上面的 key（飞书名和蜂巢显示名不一致的才要写）
NAME_ALIAS = {
    "李雨纯": "雨纯",
    "张梦成": "梦成 Rhys",
    "Lexia": "乐夏",
    "陈杨": "秦汉",
}

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


def beehive_id(name):
    """飞书姓名 → 蜂巢 userId，查不到返回 None"""
    return PERSON_BEEHIVE_ID.get(NAME_ALIAS.get(name, name))


def mention(name):
    """能 @ 的才写成"@姓名"。@ 不了的人如果也写 @，正文里的 @ 个数就会跟
    atUserList 对不上，蜂巢会把后面的 @ 都渲染错位，所以这里退化成纯姓名"""
    # 退化文案里不能再出现 @ 符号，否则又会被算成一处 @
    return f"@{name}" if beehive_id(name) else f"{name}（缺蜂巢账号，未能提醒）"


def beehive_send(text, at_names):
    """at_names 要跟正文里"@姓名"出现的顺序、次数一一对应"""
    if not BEEHIVE_WEBHOOK:
        print("⚠️ 没配 BEEHIVE_WEBHOOK，只打印不发送")
        print(text)
        return
    at_ids, at_users_info = [], []
    for name in at_names:
        user_id = beehive_id(name)
        if user_id:
            at_ids.append(user_id)
            at_users_info.append({"atUserID": user_id, "groupNickname": NAME_ALIAS.get(name, name)})
        else:
            print(f"⚠️ 没找到「{name}」的蜂巢账号，请补 PERSON_BEEHIVE_ID / NAME_ALIAS")
    if at_ids:
        payload = {"msg_type": "at_text",
                   "content": {"text": text, "atUserList": at_ids, "atUsersInfo": at_users_info}}
    else:
        payload = {"msg_type": "text", "content": {"text": text}}
    resp = requests.post(BEEHIVE_WEBHOOK, json=payload, timeout=15)
    print(f"发送结果：{resp.status_code} {resp.text}")


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
    wrong = defaultdict(list)     # 人 → [问题描述]
    downloaded = 0
    seen = set()
    for ft, fname, mime, owner, group, platform in to_check:
        if mime and not mime.startswith("image/"):
            wrong[owner].append(f"{group}-{platform} 放的不是图片（{fname or mime}）")
            continue
        if ft in seen:
            continue
        seen.add(ft)
        if ft not in sizes:
            if downloaded >= MAX_DOWNLOAD:
                continue
            size = download_size(token, ft)
            downloaded += 1
            if size:
                sizes[ft] = list(size)
        size = sizes.get(ft)
        if not size:
            continue
        w, h = size
        if platform == "Mac" and h > w:
            wrong[owner].append(f"{group}-Mac 截图是竖图（{w}×{h}，疑似手机截图）")
        elif platform == "iPhone" and w > h:
            wrong[owner].append(f"{group}-iPhone 截图是横图（{w}×{h}，疑似电脑截图）")

    for ft, refs in shot_refs.items():
        rows = len({(o, g, p) for o, g, p in refs})
        if len(refs) >= DUP_THRESHOLD and rows >= DUP_THRESHOLD:
            owner = refs[0][0]
            wrong[owner].append(f"同一张图被用在 {len(refs)} 处")

    state["sizes"] = sizes
    state["last_run"] = now.strftime("%Y-%m-%d %H:%M")
    save_state(state)

    # ---------- 拼报告 ----------
    miss_total = sum(sum(v.values()) for v in missing.values())
    wrong_total = sum(len(v) for v in wrong.values())
    lines = [f"📸 盲测截图校验｜{table_name}｜{now:%m-%d}｜{scope_desc}",
             f"已填截图 {filled_total} 处，本次实际下载检查 {downloaded} 张"]
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
    text = "\n".join(lines)

    if DRY_RUN:
        print("== DRY RUN ==（不发送）\n" + text)
        print(f"\n待 @ 的人：{at_names}")
        return
    beehive_send(text, at_names)


if __name__ == "__main__":
    main()
