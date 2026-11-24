# -*- coding: utf-8 -*-
"""批量测试前检查机器人 /tmp/music 是否包含 PRD 所需语音。

语音号/文件名只在本项目内使用, 复用语音(Q138 复用 Q066)会同时检查
自身 Q 号和最终复用语音。SSH 优先尝试有密码账号, 最后尝试无密码。
"""
import re

#: 按用户给定的顺序: 先试有密码, 最后试无密码
SSH_PASSWORDS = ("inmotion123", "autopack1688", "")
SSH_USER = "root"
MUSIC_DIR = "/tmp/music"


def voice_no(value):
    m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else None


def voice_filename(value):
    """Q001 在设备上是 Q001.mp3, 其它按 PRD 原位数(003.mp3 / 0003.mp3)。"""
    raw = re.sub(r"\D", "", str(value or ""))
    if not raw:
        return ""
    n = int(raw)
    if len(raw) >= 4:          # 四位编号的项目(ZY2L)文件名也是四位
        return f"{raw}.mp3"
    if n == 1:
        return "Q001.mp3"
    if n < 1000:
        return f"{n:03d}.mp3"
    return f"{n}.mp3"


def required_files(rows, expect=None):
    """PRD 行 → {文件名: [{qid, condition, field}]}。

    ★ 语音包检查只看 PRD 要求的语音文件(用户 2026-11-17): 验证机器人
      是否满足 PRD, 而不是按 AP 平台配置的语音号反推 —— 两者不一致本身
      就是测试要发现的问题。expect 参数保留仅为兼容调用方。
    """
    out = {}
    from core.apos import ai_prd
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        qid = str(row.get("qid") or "")
        full_text = " ".join(filter(None, (
            str(row.get("voice_text") or ""),
            str(row.get("note") or ""))))
        # PRD 明确写「暂时不提供语音」的, 不要求语音文件
        if any(k in full_text for k in (
                "暂时不提供语音", "暂不提供语音", "不提供语音", "无语音")):
            continue
        # PRD Comment 明确「Not apply this voice」→ 不是必需语音(用户 2026-11-17)
        if row.get("skip_voice") or ai_prd.voice_not_needed(
                row.get("comment"), row.get("voice_text"), row.get("note")):
            continue
        # 语音包检查页的 PRD 内容也用中文(非中文场景已翻译, 用户 2026-11-17)
        condition = str(row.get("prd_content_zh")
                        or row.get("voice_text") or row.get("note") or "")
        for field, label in (("voice", "PRD语音"),
                             ("reuse_voice", "复用语音")):
            fname = voice_filename(row.get(field))
            if not fname:
                continue
            out.setdefault(fname, []).append({
                "qid": qid, "condition": condition,
                "field": label, "voice": str(row.get(field) or ""),
            })
    return out


def check_files(rows, files, expect=None):
    """files: 设备上已有的文件名集合。返回缺失列表(按 PRD 语音号)。"""
    have = {str(x) for x in (files or [])}
    missing = []
    for fname, sources in sorted(required_files(rows, expect).items()):
        if fname in have:
            continue
        missing.append({
            "file": fname,
            "qids": [x["qid"] for x in sources],
            "sources": sources,
        })
    return missing


def list_remote_music(host, user=SSH_USER, passwords=SSH_PASSWORDS,
                      timeout=8):
    """SSH 列出 /tmp/music; 返回 {ok, checked, files, error, password?}。"""
    try:
        import paramiko
    except Exception as e:
        return {"ok": False, "checked": False, "files": [],
                "error": f"缺少 paramiko: {e}"}
    last = ""
    for password in passwords:
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(host, username=user, password=password,
                           timeout=timeout, banner_timeout=timeout,
                           auth_timeout=timeout, look_for_keys=False,
                           allow_agent=False)
            try:
                _in, out, err = client.exec_command(
                    f"ls -1 {MUSIC_DIR}", timeout=timeout)
                data = out.read().decode("utf-8", "replace")
                error = err.read().decode("utf-8", "replace").strip()
            finally:
                client.close()
            if error:
                return {"ok": False, "checked": False, "files": [],
                        "error": error[:300]}
            files = [x.strip() for x in data.splitlines()
                     if x.strip().lower().endswith(".mp3")]
            return {"ok": True, "checked": True, "files": files,
                    "error": "", "password_used": bool(password)}
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
    return {"ok": False, "checked": False, "files": [],
            "error": last or "SSH 连接失败"}


def check_remote(host, rows, user=SSH_USER, passwords=SSH_PASSWORDS,
                 timeout=8, expect=None):
    """完整检查: 连接 → 列文件 → 计算缺失(expect=AP 事件配置)."""
    info = list_remote_music(host, user, passwords, timeout)
    if not info.get("ok"):
        return {**info, "missing": [], "required": []}
    missing = check_files(rows, info.get("files") or [], expect)
    return {**info, "missing": missing,
            "required": sorted(required_files(rows, expect))}
