# -*- coding: utf-8 -*-
"""
独立测试脚本：只测试 webhook 本身能不能发消息，跳过所有飞书表格读取和日期判断逻辑。
用来快速排查"到底是 webhook 地址/格式的问题，还是业务逻辑（日期判断）的问题"。
"""

import requests
import os

BEEHIVE_WEBHOOK = os.environ["BEEHIVE_WEBHOOK"]

resp = requests.post(
    BEEHIVE_WEBHOOK,
    json={
        "msg_type": "text",
        "content": {"text": "✅ Webhook 测试消息：如果群里能看到这条，说明地址和消息格式都没问题"}
    },
    timeout=10,
)

print(f"状态码: {resp.status_code}")
print(f"返回内容: {resp.text}")

if resp.status_code != 200:
    print("⚠️ 状态码不是200，webhook 地址或权限可能有问题")
