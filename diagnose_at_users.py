# -*- coding: utf-8 -*-
"""
逐人测试@效果的诊断脚本
给每个人单独发一条只@他一个人的测试消息，打印蜂巢接口的原始返回，
用来排查具体是哪几个人的userid有问题。

需要 GitHub Actions Secrets：
- BEEHIVE_WEBHOOK_YANSHOU （随便用一个已知能用的webhook测试即可）
"""

import requests
import os
import time

BEEHIVE_WEBHOOK = os.environ["BEEHIVE_WEBHOOK_YANSHOU"]

PEOPLE = {
    "Webb": "ouv4qovzpupe9m",
    "爱德": "ouv4qovznoxoxq",
    "雨纯": "ouv4qovzoc6cad",
    "苏宸": "ouvkmtuntg5xpk",
    "可大力": "ouv4qovzlarm8d",
    "秦汉(陈杨)": "ouviua2rcuub5e",
    "李静": "ouvcofw0bgs9hf",
    "Rhys": "ouv4qovznsatvc",
    "Crisley": "ouv4qow0vt3ksn",
    "Yvonne": "ouv4qovzmwpkuy",
    "唐炜": "ouv4qovznbncqw",
    "严光": "ouvgwgfyt1elue",
}


def main():
    for name, user_id in PEOPLE.items():
        payload = {
            "msg_type": "at_text",
            "content": {
                "text": f"@{name} 诊断测试",
                "atUserList": [user_id],
                "atUsersInfo": [{"atUserID": user_id, "groupNickname": name}],
            },
        }
        resp = requests.post(BEEHIVE_WEBHOOK, json=payload, timeout=10)
        print(f"【{name}】 id={user_id}")
        print(f"  状态码: {resp.status_code}")
        print(f"  返回内容: {resp.text}")
        print("---")
        time.sleep(1)  # 避免连续请求触发限流


if __name__ == "__main__":
    main()
