# -*- coding: utf-8 -*-
"""新同事入职，把当月未验收的用例按比例抽一部分分给他。

默认 DRY RUN，--confirm yes 才真写。
- 只抽「两端都没动」的行：已经验过一端的不动，不然别人的活白做了
- 按二级模块轮转挑行，让新人每个功能都碰到，而不是集中在少数模块
  （2026-11-12 卫权入职时，95 条正好覆盖全部 71 个模块）
- 名额用最大余数法按各人剩余量比例分配，剩得多的多出
- 不在现有人之间互相挪：只从每人手里抽走等比例的一部分给新人。
  拉平到人均会让干得最多的人反而加活（郭文博已验 110 条、剩 48，拉平要再加 47）

⚠️ 表里一旦已经有这个人的记录就会拒绝执行，防止重复分配。
⚠️ 验收人字段要的是飞书 open_id，不是蜂巢 userId，两套 ID 不通用。
   open_id 用 lark-cli contact +search-user --query <姓名> --as user 查。
"""
import json, subprocess, sys, argparse
from collections import defaultdict, Counter
OLD = "/private/tmp/claude-501/-Users-webbguo-Documents-GitHub/10360672-65da-411b-b742-cf0457393ee0/scratchpad/"
S   = "/private/tmp/claude-501/-Users-webbguo-Documents-GitHub/309c77cd-db62-4887-95b8-127ad5c766e7/scratchpad/"
BASE, TABLE = "FnFab3FDKa0JU6sqa19cDVpHnM7", "tbl3IPfexzxT1sbM"
NEW_NAME, NEW_OPEN_ID = "卫权", "ou_a4dfe759f201cd77937e118fbc2e15a3"
DONE = {"通过", "不通过", "不涉及或无此需求"}
MAXW = 200          # 安全上限

ap = argparse.ArgumentParser(); ap.add_argument("--confirm", default="no"); a = ap.parse_args()
rows = [json.loads(l) for l in open(OLD + "audit/m11_now.ndjson")]

def t(r, f):
    x = r.get(f)
    if isinstance(x, list):
        return "" if not x or isinstance(x[0], dict) else str(x[0]).strip()
    return str(x).strip() if x is not None else ""
def who(r):
    x = r.get("验收人") or []
    return x[0]["name"] if isinstance(x, list) and x and isinstance(x[0], dict) else ""

untouched = [r for r in rows
             if t(r, "Mac验收结果") not in DONE and t(r, "iPhone验收结果") not in DONE]
if any(who(r) == NEW_NAME for r in rows):
    sys.exit(f"❌ 表里已经有「{NEW_NAME}」的记录了，看起来已经分过，拒绝重复执行")

left = Counter(who(r) for r in untouched)
TOT = sum(left.values()); N = len(left)
target = TOT // (N + 1)

# 最大余数法：按剩余量比例分配名额，凑足 target
exact = {n: left[n] * target / TOT for n in left}
quota = {n: int(v) for n, v in exact.items()}
for n in sorted(left, key=lambda k: -(exact[k] - quota[k]))[:target - sum(quota.values())]:
    quota[n] += 1
assert sum(quota.values()) == target

# 按二级模块轮转挑行：每轮每个模块取一条，让新人覆盖到尽量多的功能
by_mod = defaultdict(lambda: defaultdict(list))      # 模块 → 人 → [记录]
for r in untouched:
    by_mod[t(r, "二级模块")][who(r)].append(r)
picked, remain = [], dict(quota)
mods = sorted(by_mod)
progress = True
while len(picked) < target and progress:
    progress = False
    for m in mods:
        if len(picked) >= target: break
        for person in sorted(by_mod[m], key=lambda p: -remain.get(p, 0)):
            if remain.get(person, 0) > 0 and by_mod[m][person]:
                picked.append(by_mod[m][person].pop()); remain[person] -= 1
                progress = True
                break

print(f"未验收（两端都没动）{TOT} 条 ｜ {N} 人 → 加 {NEW_NAME} 共 {N+1} 人，每人 {target} 条")
print(f"\n{'原验收人':<10}{'现剩余':>6}{'抽走':>6}{'抽后':>6}")
print("-" * 30)
got = Counter(who(r) for r in picked)
for n in sorted(left, key=lambda k: -left[k]):
    print(f"{n:<10}{left[n]:>6}{got[n]:>6}{left[n]-got[n]:>6}")
print("-" * 30)
print(f"{NEW_NAME:<10}{0:>6}{len(picked):>6}{len(picked):>6}")
mod_cnt = Counter(t(r, "二级模块") for r in picked)
print(f"\n{NEW_NAME} 拿到的 {len(picked)} 条覆盖 {len(mod_cnt)} 个二级模块"
      f"（全表共 {len(by_mod)} 个），每模块 {min(mod_cnt.values())}~{max(mod_cnt.values())} 条")
missing = sorted(set(by_mod) - set(mod_cnt))
if missing:
    print(f"没覆盖到的模块（这些模块的未验收用例已被抽完或本就没有）：{len(missing)} 个 {missing[:8]}")

for n, q in quota.items():
    if got[n] != q:
        print(f"⚠️ {n} 实际抽到 {got[n]} 条，少于名额 {q}（该人剩余用例分布不够轮转）")

if len(picked) > MAXW: sys.exit(f"❌ 超过安全上限 {MAXW}")
if a.confirm != "yes":
    print("\n== DRY RUN ==（没写任何东西）。确认后加 --confirm yes")
    print("前 5 条示例：")
    for r in picked[:5]:
        print(f"   用例 {t(r,'目标')} ｜ {t(r,'二级模块')} ｜ {who(r)} → {NEW_NAME} ｜ {t(r,'目标描述')[:28]}")
    sys.exit(0)

upd = {r["record_id"]: {"验收人": [{"id": NEW_OPEN_ID}]} for r in picked}
ids = list(upd)
for i in range(0, len(ids), 200):
    chunk = {k: upd[k] for k in ids[i:i+200]}
    fn = f"assign_{i//200}.json"
    json.dump({"update_records": chunk}, open(S + fn, "w"), ensure_ascii=False)
    out = subprocess.run(["lark-cli", "base", "+record-batch-update", "--base-token", BASE,
                          "--table-id", TABLE, "--json", "@" + fn],
                         capture_output=True, text=True, cwd=S)
    ok = '"ok": true' in out.stdout
    print(f"  批次 {i//200+1}: {len(chunk)} 行 → {'✅' if ok else '❌ ' + (out.stdout or out.stderr)[:200]}")
    if not ok: sys.exit(1)
print(f"写入完成 {len(ids)} 行")
