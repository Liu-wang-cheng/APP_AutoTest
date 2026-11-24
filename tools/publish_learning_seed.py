# -*- coding: utf-8 -*-
"""Publish verified learning data as a reviewed GitHub seed.

Flow:
  1. collect verified / AI-audited entries from the local learning library;
  2. validate the mapped AP event still exists in the current event config;
  3. ask the configured AI to review the mappings; reject suspicious rows;
  4. merge approved rows into config/fault_events/seed_mappings.json;
  5. with --upload, commit to a `learning-seed` branch and open a PR.

Never ships or prints a token. Reads GITHUB_TOKEN or ~/.github_token.
"""
import argparse
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QSettings

from core.apos import ai_prd, faults, prd_memory, seed_review


def _token():
    return (os.environ.get("GITHUB_TOKEN", "").strip()
            or os.environ.get("GH_TOKEN", "").strip()
            or _read(os.path.expanduser("~/.github_token")))


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _call_ai(base, key, model):
    return lambda p: ai_prd.call_relay(
        base, key, model, [{"role": "user", "content": p}],
        max_tokens=8192)


def _merge(entries):
    out = {}
    for e in entries:
        key = str(e.get("key") or "")
        if not key or not e.get("event"):
            continue
        old = out.get(key)
        if old is None or (e.get("verified_at") or "") >= (old.get("verified_at") or ""):
            out[key] = e
    return [out[k] for k in sorted(out)]


def _valid(entry, expect):
    """只校验事件在 AP 配置里存在: 学习库已不存语音号(语义映射跨项目复用)."""
    ev = str(entry.get("event") or "")
    return bool((expect or {}).get(ev))


def _github_upload(data, repo, branch):
    import requests
    token = _token()
    if not token:
        raise SystemExit("缺少 GITHUB_TOKEN（或 ~/.github_token），未上传")
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    base = f"https://api.github.com/repos/{repo}"
    meta = requests.get(base, headers=headers, timeout=20)
    meta.raise_for_status()
    default = meta.json().get("default_branch") or "master"
    ref = requests.get(f"{base}/git/ref/heads/{default}",
                       headers=headers, timeout=20)
    ref.raise_for_status()
    sha = ref.json()["object"]["sha"]
    got = requests.get(f"{base}/git/ref/heads/{branch}",
                       headers=headers, timeout=20)
    if got.status_code == 404:
        r = requests.post(f"{base}/git/refs", headers=headers,
                          json={"ref": f"refs/heads/{branch}",
                                "sha": sha}, timeout=20)
        r.raise_for_status()
    path = "config/fault_events/seed_mappings.json"
    remote = requests.get(f"{base}/contents/{path}", headers=headers,
                          params={"ref": branch}, timeout=20)
    remote_entries = []
    file_sha = None
    if remote.status_code == 200:
        info = remote.json()
        file_sha = info.get("sha")
        try:
            remote_entries = json.loads(
                base64.b64decode(info["content"]).decode("utf-8")
            ).get("entries", [])
        except Exception:
            remote_entries = []
    merged = _merge(remote_entries + data["entries"])
    payload = {"version": 1,
               "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "entries": merged}
    body = {"message": "chore(learning): merge reviewed seed mappings",
            "content": base64.b64encode(
                json.dumps(payload, ensure_ascii=False,
                           indent=1).encode("utf-8")).decode("ascii"),
            "branch": branch}
    if file_sha:
        body["sha"] = file_sha
    r = requests.put(f"{base}/contents/{path}", headers=headers,
                     json=body, timeout=30)
    r.raise_for_status()
    pulls = requests.get(f"{base}/pulls", headers=headers,
                         params={"head": f"{repo.split('/')[0]}:{branch}",
                                 "state": "open"}, timeout=20)
    pulls.raise_for_status()
    if not pulls.json():
        pr = requests.post(f"{base}/pulls", headers=headers,
                           json={"title": "chore(learning): reviewed seed",
                                 "head": branch, "base": default,
                                 "body": "AI-reviewed learning seed update."},
                           timeout=30)
        pr.raise_for_status()
        print("PR:", pr.json().get("html_url"))
    print(f"uploaded {len(merged)} entries to {branch}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upload", action="store_true",
                    help="真正提交到 GitHub（默认只本地导出）")
    ap.add_argument("--repo", default="Liu-wang-cheng/APP_AutoTest")
    ap.add_argument("--branch", default="learning-seed")
    ap.add_argument("--sources", default="verified,ai_review")
    ap.add_argument("--config", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    st = QSettings("vacuum_test", "case_studio")
    cfg_path = args.config or str(st.value(
        "apos/fault_event_config", "") or "")
    expect = faults.load_event_config(cfg_path) if cfg_path else {}
    sources = tuple(x.strip() for x in args.sources.split(",") if x.strip())
    mem = prd_memory.PrdMemory()
    entries = []
    for key, val in mem.items.items():
        if not isinstance(val, dict) or val.get("source") not in sources:
            continue
        item = {"key": key}
        for field in prd_memory.PrdMemory._SEED_FIELDS:
            if val.get(field) not in (None, ""):
                item[field] = val[field]
        if _valid(item, expect):
            entries.append(item)
    entries = _merge(entries)
    print(f"候选 {len(entries)} 条（{', '.join(sources)}）")

    call = _call_ai(str(st.value("ai/prd_base_url", "") or ""),
                    str(st.value("ai/prd_api_key", "") or ""),
                    str(st.value("ai/prd_model", "") or ""))
    approved, rejected, errors = seed_review.review_entries(
        entries, expect, call)
    for r in rejected:
        print("拒绝:", r.get("key"), "|", r.get("review_reason") or "")
    for e in errors:
        print("AI 错误:", e[:200])
    print(f"AI 审核通过 {len(approved)} 条，拒绝 {len(rejected)} 条")

    from core.driver import BASE_DIR
    out = args.out or os.path.join(BASE_DIR, "config", "fault_events",
                                   "seed_mappings.json")
    existing = []
    try:
        with open(out, encoding="utf-8") as f:
            existing = json.load(f).get("entries", [])
    except Exception:
        pass
    merged = _merge(existing + approved)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"version": 1,
                   "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "entries": merged}, f, ensure_ascii=False, indent=1)
    print("本地种子:", out, "共", len(merged), "条")
    if args.upload:
        _github_upload({"entries": approved}, args.repo, args.branch)
    else:
        print("未上传（加 --upload 才会提交 GitHub 分支/PR）")


if __name__ == "__main__":
    main()
