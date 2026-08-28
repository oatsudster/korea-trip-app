# -*- coding: utf-8 -*-
"""One command for switching between the PC and the MacBook.

    python3 tools/sync.py              เริ่มงาน — ดึงของใหม่ลงมา
    python3 tools/sync.py "ข้อความ"     เลิกงาน — commit + push ขึ้นไป

Rebases rather than merges, so the history stays a straight line instead of
filling up with "Merge branch 'main'" noise every time the machines swap.
Stops and explains itself on conflict rather than guessing.
"""
import subprocess, sys, os

# The Windows console defaults to a Thai codepage that cannot encode emoji, and
# printing one there crashes the script outright. Force UTF-8 on the way out.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git(*args, check=True):
    r = subprocess.run(["git"] + list(args), cwd=REPO,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        print((r.stdout or "") + (r.stderr or ""))
        sys.exit(r.returncode)
    return (r.stdout or "").strip()


def count(rev):
    out = git("rev-list", "--count", rev, check=False)
    return int(out) if out.isdigit() else 0


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else None
    dirty = bool(git("status", "--porcelain"))

    # Commit first when finishing, so the rebase has something to replay and
    # local edits cannot be clobbered by what comes down from the remote.
    if msg:
        if dirty:
            git("add", "-A")
            git("commit", "-m", msg)
            print("committed:", msg)
        else:
            print("ไม่มีอะไรใหม่ให้ commit")
    elif dirty:
        print("⚠️  มีไฟล์ที่แก้ค้างอยู่ แต่ไม่ได้ใส่ข้อความ commit")
        print("   ถ้าจะบันทึกงาน:  python3 tools/sync.py \"ข้อความ\"")
        print("   จะดึงของใหม่เฉยๆ ต้อง commit หรือ stash ก่อน")
        sys.exit(1)

    git("fetch", "--quiet", "origin")
    behind = count("HEAD..origin/main")

    if behind:
        print("ดึงของใหม่ %d commit ..." % behind)
        r = subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=REPO,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print((r.stdout or "") + (r.stderr or ""))
            print("\n❌ rebase ชนกัน — แก้ไฟล์ที่ขัดแย้งแล้วรัน:")
            print("   git rebase --continue")
            print("   หรือยกเลิกด้วย:  git rebase --abort")
            sys.exit(1)
    else:
        print("ไม่มีของใหม่จากอีกเครื่อง")

    ahead = count("origin/main..HEAD")
    if ahead:
        print("push %d commit ขึ้นไป ..." % ahead)
        git("push", "origin", "main")
        print("✅ push แล้ว")
    elif msg:
        print("ไม่มีอะไรต้อง push")

    print("\nสถานะ:", git("log", "--oneline", "-1"))
    print("ไฟล์ค้าง:", len(git("status", "--porcelain").splitlines()))


if __name__ == "__main__":
    main()
