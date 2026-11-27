# -*- coding: utf-8 -*-
"""tl1833 outline 模式效果评估原型（只读，不改任何会话文件）。

对比两种 tail 策略（预算 B=10240 字节 = read_session_tail 默认 max_bytes）：
  A. 全量字节 tail（现状）：os.ReadFile 后取尾部 B 字节，行边界对齐
     （对齐 internal/agent/session_collab_tools.go:915-924 的实现）。
  B. outline tail（原型）：从尾部向前逐完整行解析 ChatML jsonl，
     每行产出一条极简摘要（assistant 文本 / 工具名+意图 / user 行），预算用尽即停。

指标：
  1. 轮次覆盖：两种模式各自覆盖的 assistant 行数与 user 行数 → 放大倍数。
  2. 意图可得率：全会话每个 tool_call，其所在 assistant 行有非空
     content 或 reasoning_content 即视为「意图可得」（outline 的意图来源
     = 模型调用工具前后自己写的话）。
  3. token 估算：chars/4 粗估（中英混合，报告标注为估算）。
  4. 保真度：outline 是否覆盖「最后一条带文本的 assistant 行」（会话
     收尾结论），以及 outline 在同预算下向前多看多久的历史。

输出：JSON 结果文件（UTF-8），避免 Windows 控制台 GBK 乱码。
"""

import json
import os
import sys

SESSION_DIR = r"C:\Users\yinji\.reasonix\sessions"
RESULT_PATH = r"C:\Users\yinji\AppData\Roaming\reasonix\global-workspace\tmp\tl1833_probe_result.json"
BUDGET = 10240          # read_session_tail 默认 max_bytes
TRUNC = 60              # outline 单条摘要最大字符数


def is_main_transcript(name: str) -> bool:
    """对齐 internal/store/session.go:89-99 + .inbox 排除 + .bak 排除。"""
    low = name.lower()
    if not low.endswith(".jsonl"):
        return False
    bad = (".events.jsonl", ".turnevents.jsonl", ".conflict.jsonl",
           ".repair.jsonl", ".rewriteattribution.jsonl",
           ".conflictpreserve.jsonl", ".guardian.jsonl", ".inbox.jsonl")
    return not low.endswith(bad)


def tail_bytes_mode(data: bytes, budget: int):
    """模拟 Go 版 read_session_tail：尾部 budget 字节，回退到行边界。"""
    if len(data) <= budget:
        return data
    cut = budget
    for i in range(budget - 1, 0, -1):
        if data[i:i+1] == b"\n":
            cut = i + 1
            break
    return data[len(data)-cut:]


def first_line(text, limit=TRUNC):
    if not isinstance(text, str):
        return ""
    t = text.strip().splitlines()
    s = t[0].strip() if t else ""
    return s[:limit]


def parse_lines(data: bytes):
    """把 transcript 字节按行拆成 (offset_bytes, parsed_json_or_None, raw_line)。"""
    out = []
    off = 0
    for raw in data.splitlines(keepends=True):
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            obj = None
        out.append((off, obj, raw))
        off += len(raw)
    return out


def classify(obj):
    """返回 (role_class, tool_names, intent_text)。intent=模型自己写的话。"""
    if not isinstance(obj, dict):
        return "broken", [], ""
    role = obj.get("role")
    if role == "user":
        return "user", [], first_line(obj.get("content"))
    if role == "assistant":
        calls = obj.get("tool_calls") or []
        names = []
        for c in calls:
            fn = (c.get("function") or {})
            names.append(fn.get("name") or "?")
        intent = first_line(obj.get("content")) or first_line(obj.get("reasoning_content"))
        return "assistant", names, intent
    if role == "tool":
        return "tool", [], ""
    return "other", [], ""


def outline_line(role_class, names, intent):
    """产出一条 outline 摘要行（str 或 None）。"""
    if role_class == "user":
        if not intent:
            return None
        return "U: " + intent
    if role_class == "assistant":
        if names:
            head = "A[" + ",".join(n for n in names[:3]) + "]"
            if len(names) > 3:
                head += "+%d" % (len(names) - 3)
            if intent:
                return head + ": " + intent
            return head + ": (无文本)"
        if intent:
            return "A: " + intent
        return None
    return None


def probe_file(path):
    with open(path, "rb") as f:
        data = f.read()
    lines = parse_lines(data)
    n_lines = len(lines)

    # ── 全会话统计（意图可得率口径） ──────────────────────────
    total_calls = 0
    calls_with_intent = 0
    asst_rows = 0
    user_rows = 0
    tool_rows = 0
    tool_bytes = 0      # role=tool 行的原始字节（tail 噪声主体）
    last_asst_text_idx = -1
    for i, (off, obj, raw) in enumerate(lines):
        rc, names, intent = classify(obj)
        if rc == "assistant":
            asst_rows += 1
            total_calls += len(names)
            if names and intent:
                calls_with_intent += len(names)
            if (obj.get("content") or "").strip():
                last_asst_text_idx = i
        elif rc == "user":
            user_rows += 1
        elif rc == "tool":
            tool_rows += 1
            tool_bytes += len(raw)

    intent_rate = (calls_with_intent / total_calls) if total_calls else None

    # ── A. 全量字节 tail（现状） ─────────────────────────────
    tail = tail_bytes_mode(data, BUDGET)
    tail_start_off = len(data) - len(tail)
    tail_asst = 0
    tail_user = 0
    tail_tool_bytes = 0
    tail_lines = 0
    for off, obj, raw in lines:
        if off < tail_start_off:
            continue
        tail_lines += 1
        rc, names, intent = classify(obj)
        if rc == "assistant":
            tail_asst += 1
        elif rc == "user":
            tail_user += 1
        elif rc == "tool":
            tail_tool_bytes += len(raw)

    # ── B. outline tail（原型）：从尾向前收摘要行 ────────────
    outline_items = []   # (byte_len, text, idx)
    for i in range(n_lines - 1, -1, -1):
        off, obj, raw = lines[i]
        rc, names, intent = classify(obj)
        text = outline_line(rc, names, intent)
        if text is None:
            continue
        b = len(text.encode("utf-8"))
        outline_items.append((b, text, i))
    # 预算内从最新往旧收
    used = 0
    picked = []
    for b, text, i in outline_items:
        if used + b > BUDGET:
            continue  # 单条超预算的旧摘要跳过，继续尝试更旧的（近似贪心）
        used += b
        picked.append((b, text, i))
        if used >= BUDGET:
            break
    picked_min_idx = min((i for _, _, i in picked), default=n_lines)
    outline_asst = sum(1 for _, _, i in picked
                       if classify(lines[i][1])[0] == "assistant")
    outline_user = sum(1 for _, _, i in picked
                       if classify(lines[i][1])[0] == "user")
    outline_covers_last_asst_text = picked_min_idx <= last_asst_text_idx if last_asst_text_idx >= 0 else None

    # outline 展示样本（最新 12 条，按时间正序）
    sample = [t for _, t, _ in sorted(picked, key=lambda x: -x[2])][:12]

    return {
        "file": os.path.basename(path),
        "size_kb": round(len(data) / 1024, 1),
        "lines": n_lines,
        "asst_rows": asst_rows,
        "user_rows": user_rows,
        "tool_rows": tool_rows,
        "tool_bytes_kb": round(tool_bytes / 1024, 1),
        "tool_bytes_share": round(tool_bytes / len(data), 3) if len(data) else None,
        "total_tool_calls": total_calls,
        "intent_rate": round(intent_rate, 3) if intent_rate is not None else None,
        "tail": {
            "assistant_turns": tail_asst,
            "user_turns": tail_user,
            "lines": tail_lines,
            "tool_bytes_kb": round(tail_tool_bytes / 1024, 1),
            "tool_bytes_share": round(tail_tool_bytes / len(tail), 3) if tail else None,
        },
        "outline": {
            "assistant_turns": outline_asst,
            "user_turns": outline_user,
            "lines": len(picked),
            "bytes_used": used,
            "covers_last_asst_text": outline_covers_last_asst_text,
            "turn_ratio_vs_tail": round(outline_asst / tail_asst, 2) if tail_asst else None,
            "sample": sample,
        },
    }


def main():
    files = sorted(
        os.path.join(SESSION_DIR, n) for n in os.listdir(SESSION_DIR)
        if is_main_transcript(n)
    )
    results = [probe_file(p) for p in files]

    tot_calls = sum(r["total_tool_calls"] for r in results)
    tot_intent = sum(
        r["intent_rate"] * r["total_tool_calls"]
        for r in results if r["intent_rate"] is not None
    )
    sum_tail_asst = sum(r["tail"]["assistant_turns"] for r in results)
    sum_outline_asst = sum(r["outline"]["assistant_turns"] for r in results)
    sum_tool_bytes = sum(r["tool_bytes_kb"] for r in results)
    sum_size = sum(r["size_kb"] for r in results)
    covers = [r["outline"]["covers_last_asst_text"] for r in results
              if r["outline"]["covers_last_asst_text"] is not None]

    summary = {
        "files": len(results),
        "budget_bytes": BUDGET,
        "total_size_kb": round(sum_size, 1),
        "tool_bytes_kb_total": round(sum_tool_bytes, 1),
        "tool_bytes_share_total": round(sum_tool_bytes / sum_size, 3) if sum_size else None,
        "total_tool_calls": tot_calls,
        "intent_rate_global": round(tot_intent / tot_calls, 3) if tot_calls else None,
        "tail_assistant_turns_total": sum_tail_asst,
        "outline_assistant_turns_total": sum_outline_asst,
        "turn_ratio_global": round(sum_outline_asst / sum_tail_asst, 2) if sum_tail_asst else None,
        "outline_covers_last_asst_text": {
            "yes": covers.count(True),
            "no": covers.count(False),
        },
    }
    out = {"summary": summary, "per_file": results}
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("OK files=%s total_calls=%s intent_rate=%s turn_ratio=%s" % (
        summary["files"], tot_calls,
        summary["intent_rate_global"], summary["turn_ratio_global"]))


if __name__ == "__main__":
    main()
