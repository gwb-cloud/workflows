# -*- coding: utf-8 -*-
"""
Webhook 探测：给每个已配置的 BEEHIVE_WEBHOOK_* 各发一条写着自己 Secret 名字的消息，
看哪个群收到哪条，就能把 Secret 和真实群对应起来。

这是一次性排查工具，跑完就可以不用了。消息正文写清楚"无需处理"，
免得群里的人以为出了什么事。
"""
import os
import json
import time
import requests

# 顺序决定发送顺序，群里收到的时间戳可以辅助对照
SECRETS = [
    "BEEHIVE_WEBHOOK_FABAN",     # 验收群【发版】
    "BEEHIVE_WEBHOOK_MANGCE",    # 验收群【盲测】
    "BEEHIVE_WEBHOOK_WENTI",     # 验收群【问题】
    "BEEHIVE_WEBHOOK_DEV",       # 高优问题修复专项（研发群）
]

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() == "true"
ONLY = {s.strip() for s in os.environ.get("ONLY", "").replace("，", ",").split(",") if s.strip()}


def main():
    targets = [s for s in SECRETS if not ONLY or s in ONLY]
    if ONLY - set(SECRETS):
        print(f"⚠️ only 里有不认识的名字，已忽略：{'、'.join(sorted(ONLY - set(SECRETS)))}")

    # 同一个群可能配了两个 Secret（名字不同、URL 相同），这种情况要能看出来
    url_seen = {}
    sent = skipped = failed = 0
    for idx, name in enumerate(targets, 1):
        url = os.environ.get(name, "").strip()
        if not url:
            print(f"⏭️  {name}：没配置，跳过")
            skipped += 1
            continue
        if url in url_seen:
            print(f"🔁 {name}：URL 和 {url_seen[url]} 完全相同，是同一个群，只发一次")
            continue
        url_seen[url] = name

        text = (f"🔎 自动化通知配置探测（{idx}/{len(targets)}）——无需处理，看完即可忽略\n"
                f"本群对应的配置名是：{name}\n"
                f"这条消息只是为了把配置名和群对上号，不代表有任何异常。")
        if DRY_RUN:
            print(f"== DRY RUN ==（不发送）{name}\n{text}\n")
            sent += 1
            continue
        try:
            resp = requests.post(url, json={"msg_type": "text", "content": {"text": text}}, timeout=15)
            ok = resp.status_code == 200
            print(f"{'✅' if ok else '❌'} {name}：HTTP {resp.status_code} {resp.text[:120]}")
            sent += ok
            failed += (not ok)
        except Exception as e:
            print(f"❌ {name}：发送异常 {e}")
            failed += 1
        time.sleep(1)      # 别把群机器人打到限流

    print(f"\n发送成功 {sent} ｜ 未配置 {skipped} ｜ 失败 {failed}")
    print("去各个群里看收到了哪条，把「群名 → 配置名」告诉我就行。")


if __name__ == "__main__":
    main()
