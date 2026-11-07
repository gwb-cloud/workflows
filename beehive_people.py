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


def mention(name):
    """能 @ 的才写成"@姓名"。@ 不了的人如果也写 @，正文里的 @ 个数就会跟
    atUserList 对不上，蜂巢会把后面的 @ 都渲染错位，所以这里退化成纯姓名"""
    # 退化文案里不能再出现 @ 符号，否则又会被算成一处 @
    return f"@{name}" if beehive_id(name) else f"{name}（缺蜂巢账号，未能提醒）"


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
            at_users_info.append({"atUserID": user_id, "groupNickname": NAME_ALIAS.get(name, name)})
        else:
            print(f"⚠️ 没找到「{name}」的蜂巢账号，请补 PERSON_BEEHIVE_ID / NAME_ALIAS")
    if at_ids:
        payload = {"msg_type": "at_text",
                   "content": {"text": text, "atUserList": at_ids, "atUsersInfo": at_users_info}}
    else:
        payload = {"msg_type": "text", "content": {"text": text}}
    resp = requests.post(webhook, json=payload, timeout=15)
    print(f"发送结果：{resp.status_code} {resp.text}")
