# -*- coding: utf-8 -*-
"""蜂巢 @ 到人用的账号名单 + 发消息封装。

抽成单独文件是因为盲测截图校验和问题表巡检两个脚本都要 @ 人，名单放两份
迟早会不一致。以后新人入职只改这一个文件。
"""

import os
import requests

# 蜂巢账号（key 是蜂巢里的显示名）
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

# 飞书姓名 → 上面的 key（飞书名和蜂巢显示名不一致的才要写）
NAME_ALIAS = {
    "李雨纯": "雨纯",
    "张梦成": "梦成 Rhys",
    "Lexia": "乐夏",
    "陈杨": "秦汉",
}


def beehive_id(name):
    """飞书姓名 → 蜂巢 userId，查不到返回 None"""
    return PERSON_BEEHIVE_ID.get(NAME_ALIAS.get(name, name))


def display_name(name):
    """飞书姓名 → 蜂巢群里的显示名。正文里的"@XXX"必须用这个，不能用飞书姓名"""
    return NAME_ALIAS.get(name, name)


def mention(name):
    """能 @ 的才写成"@显示名"。

    两个坑都踩过，别改回去：
    1. 正文里的"@XXX"必须和 atUsersInfo[].groupNickname 完全一致，蜂巢才会把它渲染成
       蓝色链接（渲染时还会把它替换成这人在群里的真实昵称，比如 @郭文博 显示成 @Webb）。
       所以这里用 display_name()，不能用飞书姓名——否则有 NAME_ALIAS 的那几个人
       （乐夏/雨纯/梦成 Rhys）消息能收到，但文字是灰的，看着像没 @ 到。
    2. @ 不了的人不能也写 @，否则正文里的 @ 个数跟 atUserList 对不上，
       蜂巢会把后面的 @ 全渲染错位。退化文案里也不能再出现 @ 符号。
    """
    return f"@{display_name(name)}" if beehive_id(name) else f"{name}（缺蜂巢账号，未能提醒）"


def beehive_send(text, at_names, webhook=None):
    """at_names 要跟正文里"@姓名"出现的顺序、次数一一对应"""
    webhook = webhook if webhook is not None else os.environ.get("BEEHIVE_WEBHOOK", "")
    if not webhook:
        print("⚠️ 没配 BEEHIVE_WEBHOOK，只打印不发送")
        print(text)
        return
    at_ids, at_users_info = [], []
    for name in at_names:
        user_id = beehive_id(name)
        if user_id:
            at_ids.append(user_id)
            # groupNickname 必须和正文里 mention() 写的那个名字一致，见 mention() 注释
            at_users_info.append({"atUserID": user_id, "groupNickname": display_name(name)})
        else:
            print(f"⚠️ 没找到「{name}」的蜂巢账号，请补 PERSON_BEEHIVE_ID / NAME_ALIAS")
    if at_ids:
        payload = {"msg_type": "at_text",
                   "content": {"text": text, "atUserList": at_ids, "atUsersInfo": at_users_info}}
    else:
        payload = {"msg_type": "text", "content": {"text": text}}
    resp = requests.post(webhook, json=payload, timeout=15)
    print(f"发送结果：{resp.status_code} {resp.text}")

# ---------------------------------------------------------------------------
# 研发同学（问题池的「技术负责人」字段）。和上面验收团队那份分开放，因为两拨人
# 不在同一个群里：验收报告发验收群，研发的到期提醒发研发群。
#
# key 用飞书里的姓名，value 用蜂巢显示名；蜂巢的 userId 查 DEV_BEEHIVE_ID。
# 正文里写 @ 要用蜂巢显示名，原因见 mention() 的注释。
# ---------------------------------------------------------------------------
DEV_BEEHIVE_ID = {
    "士衡": "ouv4mpnbncczdy",   "Bruce": "ouvfh4io3kb1vy",  "harlake": "ouvhuw8poaor0t",
    "Kong": "ouvcxm64x9wofo",  "Liko": "ouv4qlum38oqvk",   "Yatoro": "ouvppako1eetr0",
    "安柔": "ouv4qlvxa3mbkp",   "柏恒": "ouvvx6noopaq9b",     "冰太": "ouv4qlvdk3wvvs",
    "陈理仕": "ouv4qlus1pb5id",  "陈明辉": "ouv4qlvi4dqw3q",    "高耳机": "ouv4qlw5dbnz4y",
    "耿德宁": "ouv4qltgfewpjl",  "公台": "ouv4qlvxoaugyp",     "广志": "ouvgkyrsf68ufv",
    "浩澜": "ouv4qluqoynfcv",   "杰乐": "ouv4qltpybd7yq",     "进华": "ouv4qluiakumao",
    "李恒琪": "ouvcfsos3iut8s",  "李玉海": "ouvao0m143l2bp",    "李振民": "ouv4qltobpg3qq",
    "李正平": "ouv4qluvjmwkdw",  "蔺国防": "ouv8khicchsmqy",    "刘健": "ouv4qlueucfikc",
    "秦兴华": "ouv4qltuxgea3m",  "盛杰": "ouv4qltyxx9hdk",     "守白": "ouvjhgphs7fnzd",
    "孙文": "ouvgomnvjyywac",   "熙": "ouv4qlustaym2w",      "阳帅": "ouvmfoi5uxl9ya",
    "云恒": "ouv4qltxmwkvwk",   "张雷雷": "ouv8i53f3p0jb2",    "赵旭": "ouv4qlvms7w5uf",
    "自力": "ouv4qltxndfdhj",
}

# 飞书姓名 → 蜂巢显示名。只写两边对不上的
DEV_NAME_ALIAS = {
    "王自力": "自力",
    "德宁": "耿德宁",
    "陈浩澜": "浩澜",
    "陈进华": "进华",
    "胡杰乐": "杰乐",
    "kong": "Kong",
    "王涛": "云恒",      # ← 按飞书表里的头像推断的，还没跟本人确认
}

# 还对不上的飞书姓名（合计 214 条记录），缺蜂巢账号，提醒里只会写名字、@ 不到人：
#   于子俊 53、甘浩宇 29、董鸿华 17、张婷华 11、王鹏程 8、高洪娟 8、
#   学实（郭鸣轩）6、迷人的幽幽 6、张可松 4、tomo 2、兰图 2、朱新宏 1、欧荣辉 1、李明 1
# 补的时候：名字一致就加进 DEV_BEEHIVE_ID，不一致就在 DEV_NAME_ALIAS 里加一行。


def dev_display_name(name):
    """飞书姓名 → 研发在蜂巢群里的显示名"""
    # 爱德 / Crisley / 可大力 这几个验收组的人偶尔也会被填成技术负责人，走上面那份名单
    if NAME_ALIAS.get(name, name) in PERSON_BEEHIVE_ID:
        return NAME_ALIAS.get(name, name)
    return DEV_NAME_ALIAS.get(name, name)


def dev_beehive_id(name):
    """飞书姓名 → 研发的蜂巢 userId，查不到返回 None"""
    display = dev_display_name(name)
    return DEV_BEEHIVE_ID.get(display) or PERSON_BEEHIVE_ID.get(display)


def dev_mention(name):
    """同 mention()：@ 不到的人不写 @，否则正文的 @ 个数会跟 atUserList 对不上"""
    return f"@{dev_display_name(name)}" if dev_beehive_id(name) else f"{name}（缺蜂巢账号）"


def dev_beehive_send(text, at_names, webhook=None):
    """发给研发群。at_names 要跟正文里"@姓名"出现的顺序、次数一一对应"""
    webhook = webhook if webhook is not None else os.environ.get("BEEHIVE_WEBHOOK_DEV", "")
    if not webhook:
        print("⚠️ 没配 BEEHIVE_WEBHOOK_DEV，只打印不发送")
        print(text)
        return
    at_ids, at_users_info = [], []
    for name in at_names:
        user_id = dev_beehive_id(name)
        if user_id:
            at_ids.append(user_id)
            at_users_info.append({"atUserID": user_id, "groupNickname": dev_display_name(name)})
        else:
            print(f"⚠️ 没找到「{name}」的蜂巢账号，请补 DEV_BEEHIVE_ID / DEV_NAME_ALIAS")
    if at_ids:
        payload = {"msg_type": "at_text",
                   "content": {"text": text, "atUserList": at_ids, "atUsersInfo": at_users_info}}
    else:
        payload = {"msg_type": "text", "content": {"text": text}}
    resp = requests.post(webhook, json=payload, timeout=15)
    print(f"发送结果：{resp.status_code} {resp.text}")
