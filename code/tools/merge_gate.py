"""合并前检查（软件统一合并用，§7.3 协作规则 1–3）。不改你的工作区，可以随便跑。

  python tools/merge_gate.py captain/interaction           # 检查并试合并进 main，跑回归
  python tools/merge_gate.py demo/vision --base main --no-tests

做三件事：
  1. 归属：分支改了哪些文件、各归谁（tools/owners.py）。别人的分支动了 master_service.py → 拦；
     动了其他人的模块 → 提醒先打招呼。
  2. 试合并：在系统临时目录建一个临时 worktree，把分支合进 base，有冲突列出文件和负责人。
  3. 回归：在试合并结果上跑 tests/run_all.py，必须 ALL PASS。
全部通过才打印可以执行的合并命令；真正的合并由人来敲。退出码：0 可合并，1 不可合并，2 用法错误。
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import owners  # noqa: E402


def git(*args, cwd=None, check=True):
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return r


def review_paths(paths, branch_role):
    """返回 (errors, warnings, rows)。rows = [(path, 负责角色或 None)]。"""
    errors, warnings, rows = [], [], []
    for p in paths:
        role = owners.owner_of(p)
        rows.append((p, role))
        if p in owners.HUB and branch_role != "software":
            errors.append("%s 是交通枢纽，只由软件改；需要新接口请提需求" % p)
        elif role is not None and branch_role is not None and role != branch_role:
            warnings.append("%s 归%s，确认已经跟负责人打过招呼" % (p, owners.ROLES[role]))
    if branch_role is None:
        warnings.append("分支名看不出角色（应为 software/… captain/… demo/… hardware/…），跳过归属检查")
    return errors, warnings, rows


def trial_merge(root, base, branch, run_tests=True):
    """临时 worktree 里试合并 + 跑回归。返回 (ok, 说明行列表)。"""
    tmp = tempfile.mkdtemp(prefix="caremate-merge-")
    notes = []
    try:
        git("worktree", "add", "--detach", tmp, base, cwd=root)
        r = git("-c", "user.name=merge_gate", "-c", "user.email=merge_gate@local",
                "merge", "--no-commit", "--no-ff", branch, cwd=tmp, check=False)
        if r.returncode != 0:
            conflicted = git("diff", "--name-only", "--diff-filter=U", cwd=tmp, check=False).stdout.split()
            if not conflicted:
                return False, ["合并失败：" + (r.stderr or r.stdout).strip()]
            notes.append("合并冲突 %d 个文件：" % len(conflicted))
            for p in conflicted:
                notes.append("  %s（找%s）" % (p, owners.ROLES.get(owners.owner_of(p), "全队")))
            return False, notes
        notes.append("试合并无冲突")
        if not run_tests:
            return True, notes + ["（--no-tests：跳过回归）"]
        t = subprocess.run([sys.executable, os.path.join("tests", "run_all.py")], cwd=os.path.join(tmp, "code"),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        tail = [ln for ln in t.stdout.splitlines() if ln.strip()][-20:]
        notes += ["回归（试合并结果上）："] + ["  " + ln for ln in tail]
        return t.returncode == 0, notes
    finally:
        git("worktree", "remove", "--force", tmp, cwd=root, check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        git("worktree", "prune", cwd=root, check=False)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="合并前检查：归属 + 试合并 + 回归")
    ap.add_argument("branch")
    ap.add_argument("--base", default="main")
    ap.add_argument("--no-tests", action="store_true")
    args = ap.parse_args(argv)

    r = git("rev-parse", "--show-toplevel", check=False)
    if r.returncode != 0:
        print("不在 git 仓库里")
        return 2
    root = r.stdout.strip()
    for ref in (args.base, args.branch):
        if git("rev-parse", "--verify", "--quiet", ref + "^{commit}", cwd=root, check=False).returncode != 0:
            print("找不到分支或提交：%s（先 git fetch？）" % ref)
            return 2

    paths = git("diff", "--name-only", "%s...%s" % (args.base, args.branch), cwd=root).stdout.split()
    role = owners.role_of_branch(args.branch)
    print("分支 %s（%s）→ %s：改动 %d 个文件" % (args.branch, owners.ROLES.get(role, "角色未知"), args.base, len(paths)))
    if not paths:
        print("没有新改动，不用合并。")
        return 0
    errors, warnings, rows = review_paths(paths, role)
    for p, r in rows:
        print("  %-48s %s" % (p, owners.ROLES.get(r, "公共")))
    print()
    for w in warnings:
        print("⚠ " + w)
    for e in errors:
        print("✗ " + e)

    ok, notes = trial_merge(root, args.base, args.branch, run_tests=not args.no_tests)
    print()
    for n in notes:
        print(n)
    print()
    if errors or not ok:
        print("不能合并：先解决上面的 ✗。")
        return 1
    print("可以合并：git switch %s && git merge --no-ff %s" % (args.base, args.branch))
    return 0


if __name__ == "__main__":
    sys.exit(main())
