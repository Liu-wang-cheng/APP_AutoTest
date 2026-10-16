# -*- coding: utf-8 -*-
"""清理 GitHub 上过老的 Release(只删 Release 与它的资产, **不动 tag、不动源码历史**)。

    python tools/prune_releases.py                # 演练: 只打印会删哪些
    python tools/prune_releases.py --apply        # 真删
    python tools/prune_releases.py --keep 3 --apply

为什么敢删(2026-10-16 盘点):
  · 每个分发包 ~225MB, 34 个版本就是 7.65GB —— 公共仓库的存储大头全在这儿;
  · **更新器只读仓库根 version.json 指向的那一个包**, 老资产不参与任何自动流程;
  · 真要用老版本, 随时能从 tag 重建: `git checkout v1.7 && build.bat`。
  所以"留最近几个"是安全的 —— 唯一代价是老版本的直链失效。

安全阀:
  · **默认演练**(不加 --apply 只打印), CI 里的手动触发也默认演练;
  · 永远保留 version.json 里那个版本(哪怕它排不进最近 N 个);
  · 永远保留最近 N 个(按创建时间, 默认 5)。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def releases_to_delete(releases, keep=5, keep_tag=""):
    """要删的 Release 列表(纯函数, 便于单测)。

    releases 按**创建时间倒序**传入; 保留:
      · 前 keep 个(最近发布的);
      · tag 等于 keep_tag 的那个(当前版本, 由 version.json 决定) —— 双保险。
    """
    keep = max(0, int(keep))
    out = []
    for i, r in enumerate(releases):
        if i < keep:
            continue
        if keep_tag and r.get("tag_name") == keep_tag:
            continue
        out.append(r)
    return out


def _get(url, token, limit=None):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "curl/8"}
    if token:
        h["Authorization"] = "token " + token
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def list_releases(repo, token):
    """全部 Release(按创建时间倒序); 分页取全(仓库大了不能只看第一页)"""
    out, url = [], f"https://api.github.com/repos/{repo}/releases?per_page=100"
    while url:
        h = {"Accept": "application/vnd.github+json", "User-Agent": "curl/8"}
        if token:
            h["Authorization"] = "token " + token
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req) as r:
            out.extend(json.load(r))
            link = r.headers.get("Link") or ""
        nxt = [p for p in link.split(",") if 'rel="next"' in p]
        url = nxt[0].split("<")[1].split(">")[0] if nxt else None
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


def current_version_tag(repo, token):
    """仓库根 version.json 里的版本 → "v{version}"(取不到就返回空串, 不影响主流程)"""
    try:
        import base64
        d = _get(f"https://api.github.com/repos/{repo}/contents/version.json", token)
        v = json.loads(base64.b64decode(d["content"]).decode("utf-8")).get("version") or ""
        return f"v{v}" if v else ""
    except Exception as e:
        print(f"  [WARN] 读 version.json 失败(忽略): {type(e).__name__}: {e}")
        return ""


def main(argv=None):
    from core.console import force_utf8_stdout
    force_utf8_stdout()          # 中文输出在 cp1252 控制台上会崩(CI 上实测)

    ap = argparse.ArgumentParser(description="清理过老的 GitHub Release(不动 tag)")
    ap.add_argument("--repo", default="", help="owner/repo(默认从 git remote 解析)")
    ap.add_argument("--keep", type=int, default=5, help="保留最近几个(默认 5)")
    ap.add_argument("--apply", action="store_true", help="真删; 不加则只演练")
    args = ap.parse_args(argv)

    # 同目录复用: token 解析在 release.py, owner/repo 解析在 verify_release.py
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import release as _rel
    from verify_release import repo_from_git_remote
    repo = args.repo or repo_from_git_remote()
    token = _rel.get_token()
    if not repo or not token:
        print(f"[ERROR] 解析不出仓库({repo!r})或拿不到 token")
        return 1

    rels = list_releases(repo, token)
    keep_tag = current_version_tag(repo, token)
    todo = releases_to_delete(rels, args.keep, keep_tag)
    freed = sum(a.get("size", 0) for r in todo for a in r.get("assets") or [])
    print(f"仓库 {repo}: 共 {len(rels)} 个 Release, 保留最近 {args.keep} 个"
          + (f" + 当前版本 {keep_tag}" if keep_tag else ""))
    for r in rels:
        mark = "保留" if r not in todo else "删除"
        print("  [%s] %-9s %s" % (mark, r.get("tag_name"), r.get("created_at", "")[:10]))
    print(f"将删除 {len(todo)} 个, 释放约 {freed / 1e9:.2f} GB")
    if not args.apply:
        print("（演练模式, 什么都没删; 加 --apply 才真删）")
        return 0
    bad = 0
    for r in todo:
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/releases/{r['id']}",
                method="DELETE",
                headers={"Authorization": "token " + token,
                         "Accept": "application/vnd.github+json", "User-Agent": "curl/8"})
            urllib.request.urlopen(req)
            print(f"  已删 {r.get('tag_name')}")
        except urllib.error.HTTPError as e:
            bad += 1
            print(f"  [FAIL] {r.get('tag_name')} HTTP {e.code}")
    print(f"完成: 删除 {len(todo) - bad} 个, 失败 {bad} 个")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
