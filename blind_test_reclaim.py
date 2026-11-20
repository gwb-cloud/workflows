# -*- coding: utf-8 -*-
"""有人离职，把他名下还没验的用例分回给其他人。

默认 DRY RUN，--confirm yes 才真写。
- **只回收「两端都没动」的行**。他已经验过的留着他的署名 —— 那是历史记录，
  清掉就丢了"当时是谁验的"，跟技术负责人离职的处理口径一致。
- **按「总数」补齐到人均**，不是按剩余量比例还回去。月度建表本来就是按总数平均
  分的（每人 ~183 条），离职回收就该把总数填回那个水位：谁被抽走得多谁补回来得多。
- 按二级模块轮转挑行，避免某个人一次性接手一整块陌生模块。

⚠️ 验收人字段要飞书 open_id，不是蜂巢 userId。脚本直接从记录里已有的
   验收人字段取 open_id，不用另外查。
"""
import json, os, subprocess, sys, argparse
from collections import defaultdict, Counter

SCRATCH = os.environ.get("SCRATCH", "").rstrip("/")
BASE, TABLE = "FnFab3FDKa0JU6sqa19cDVpHnM7", "tbl3IPfexzxT1sbM"
DONE = {"通过", "不通过", "不涉及或无此需求"}
MAXW = 300


def t(r, f):
    x = r.get(f)
    if isinstance(x, list):
        return "" if not x or isinstance(x[0], dict) else str(x[0]).strip()
    return str(x).strip() if x is not None else ""


def person(r):
    x = r.get("验收人") or []
    if isinstance(x, list) and x and isinstance(x[0], dict):
        return x[0].get("name", ""), x[0].get("id", "")
    return "", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leaver", required=True, help="离职的人（验收人字段里的姓名）")
    ap.add_argument("--data", required=True, help="全量 ndjson 路径")
    ap.add_argument("--confirm", default="no")
    a = ap.parse_args()

    rows = [json.loads(l) for l in open(a.data)]
    open_id = {}
    total = Counter()
    for r in rows:
        n, oid = person(r)
        if n:
            total[n] += 1
            if oid:
                open_id.setdefault(n, oid)
    if a.leaver not in total:
        sys.exit(f"❌ 表里没有「{a.leaver}」的记录")

    untouched = [r for r in rows if person(r)[0] == a.leaver
                 and t(r, "Mac验收结果") not in DONE
                 and t(r, "iPhone验收结果") not in DONE]
    kept = total[a.leaver] - len(untouched)
    others = [n for n in total if n != a.leaver]
    pool = len(untouched)

    # 按总数补齐到人均：目标水位 = (其他人现有总数 + 要分的量) / 人数
    base = {n: total[n] for n in others}
    target = (sum(base.values()) + pool) / len(others)
    need = {n: max(0, target - base[n]) for n in others}
    quota = {n: int(v) for n, v in need.items()}
    rest = pool - sum(quota.values())
    for n in sorted(others, key=lambda k: -(need[k] - quota[k]))[:rest]:
        quota[n] += 1
    assert sum(quota.values()) == pool, (sum(quota.values()), pool)

    # 按二级模块轮转分配，别让一个人一次接手一整块陌生模块
    by_mod = defaultdict(list)
    for r in untouched:
        by_mod[t(r, "二级模块")].append(r)
    remain = dict(quota)
    assign = {}
    for m in sorted(by_mod):
        for r in by_mod[m]:
            n = max(remain, key=lambda k: (remain[k], -base[k]))
            if remain[n] <= 0:
                break
            assign[r["record_id"]] = n
            remain[n] -= 1

    print(f"{a.leaver} 名下 {total[a.leaver]} 条："
          f"已验完 {kept} 条保持不动，未动 {pool} 条要分出去")
    print(f"目标水位：每人 {target:.1f} 条\n")
    got = Counter(assign.values())
    print(f"{'验收人':<10}{'现总数':>7}{'分到':>6}{'分后':>6}")
    print("-" * 32)
    for n in sorted(others, key=lambda k: -got[k]):
        print(f"{n:<10}{base[n]:>7}{got[n]:>6}{base[n]+got[n]:>6}")
    print("-" * 32)
    after = [base[n] + got[n] for n in others]
    print(f"分后区间 {min(after)}~{max(after)}，极差 {max(after)-min(after)}")
    miss = [n for n in others if n not in open_id]
    if miss:
        sys.exit(f"❌ 这些人拿不到 open_id，没法写：{miss}")

    if len(assign) > MAXW:
        sys.exit(f"❌ 超过安全上限 {MAXW}")
    if a.confirm != "yes":
        print("\n== DRY RUN ==（没写任何东西）。确认后加 --confirm yes")
        sys.exit(0)

    upd = {rid: {"验收人": [{"id": open_id[n]}]} for rid, n in assign.items()}
    ids = list(upd)
    out_dir = SCRATCH or os.path.dirname(os.path.abspath(a.data))
    for i in range(0, len(ids), 200):
        chunk = {k: upd[k] for k in ids[i:i + 200]}
        fn = f"reclaim_{i//200}.json"
        json.dump({"update_records": chunk}, open(os.path.join(out_dir, fn), "w"),
                  ensure_ascii=False)
        res = subprocess.run(["lark-cli", "base", "+record-batch-update",
                              "--base-token", BASE, "--table-id", TABLE, "--json", "@" + fn],
                             capture_output=True, text=True, cwd=out_dir)
        ok = '"ok": true' in res.stdout
        print(f"  批次 {i//200+1}: {len(chunk)} 行 → "
              f"{'✅' if ok else '❌ ' + (res.stdout or res.stderr)[:200]}")
        if not ok:
            sys.exit(1)
    print(f"写入完成 {len(ids)} 行")


if __name__ == "__main__":
    main()
