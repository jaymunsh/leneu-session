#!/usr/bin/env python3
"""leneu-session 훅 (Stop / PreCompact / SessionStart / PreToolUse 공용).

자기검증: python3 session_hook.py --selftest
"""
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path


DIR = ".leneu-session"
DEFAULTS = {"context_window": 1000000, "clear_recommend_pct": 30, "checkpoint_every_pct": 10}
MAX_SESSIONS = 20
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
SYSTEM_TAG = re.compile(
    r"<(system-reminder|task-notification|command-[a-z-]+|local-command-[a-z-]+)>.*?</\1>", re.S
)
SECRET = re.compile(
    r"\bsk-[\w-]{16,}|\bghp_\w{16,}|\bgithub_pat_\w{10,}|\bAKIA[0-9A-Z]{16}\b"
    r"|\bBearer\s+[\w.~+/-]{10,}|\bxox[bp]-[\w-]{8,}"
)


def find_root(cwd):
    """켜져 있는 프로젝트 루트. 가장 가까운 .git 또는 홈 위로는 올라가지 않는다."""
    start = Path(cwd or os.getcwd()).resolve()
    home = Path.home().resolve()
    for d in (start, *start.parents):
        if (d / DIR).is_dir():
            return None if (d / DIR / "disabled").exists() else d
        if (d / ".git").exists() or d == home:
            return None
    return None


def _read_json_dict(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(root):
    raw = _read_json_dict(root / DIR / "config.json")
    return {
        k: raw[k] if isinstance(raw.get(k), (int, float)) and not isinstance(raw.get(k), bool) and raw[k] > 0 else v
        for k, v in DEFAULTS.items()
    }


def load_state(root):
    return _read_json_dict(root / DIR / ".state.json")


def save_state(root, state):
    """세션은 최근 것만 남기고, 쓰다가 죽어도 깨지지 않게 교체한다."""
    state = dict(list(state.items())[-MAX_SESSIONS:])
    fd, tmp = tempfile.mkstemp(dir=root / DIR, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, root / DIR / ".state.json")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def log_error(root, exc):
    if root is None:
        return
    try:
        with (root / DIR / "hook-errors.log").open("a") as f:
            f.write(f"{type(exc).__name__}: {exc}\n")
    except OSError:
        pass


def load_transcript(path):
    p = Path(path or "")
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def context_tokens(entries):
    """마지막 메인 응답이 쓴 컨텍스트 크기. 출력 토큰은 포함하지 않는다."""
    for e in reversed(entries):
        if e.get("isSidechain") or e.get("type") != "assistant":
            continue
        usage = e.get("message", {}).get("usage")
        if not usage:
            continue
        try:
            total = sum(usage.get(k, 0) for k in USAGE_KEYS)
        except TypeError:
            return None
        if total > 0:  # 합성 오류 메시지는 usage가 0이다
            return total
    return None


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def parse_turns(entries):
    """사람이 보낸 요청마다 턴 하나. 시스템이 끼워 넣은 user 항목은 턴이 아니다."""
    turns = []
    for e in entries:
        if e.get("isSidechain"):
            continue
        content = e.get("message", {}).get("content")
        if e.get("type") == "user":
            if e.get("isMeta") or e.get("isCompactSummary"):
                continue
            prompt = SYSTEM_TAG.sub("", _text(content)).strip()
            if prompt:
                turns.append({"prompt": prompt, "reply": "", "tools": [], "files": []})
        elif e.get("type") == "assistant" and turns:
            turn = turns[-1]
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    turn["reply"] = (turn["reply"] + "\n\n" + b["text"].strip()).strip()
                elif b.get("type") == "tool_use":
                    turn["tools"].append(b.get("name", ""))
                    path = (b.get("input") or {}).get("file_path")
                    if b.get("name") in EDIT_TOOLS and path and path not in turn["files"]:
                        turn["files"].append(path)
    return turns


def mask_secrets(text):
    # ponytail: 흔한 패턴만 가린다. 형식 없는 비밀값은 못 막으며 README에 한계를 적는다
    return SECRET.sub("[비밀값 가림]", text)


def _clip(s, n):
    s = " ".join(mask_secrets(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def render_turn(turn, now):
    lines = ["", f"## {now:%Y-%m-%d %H:%M} turn", f"**요청:** {_clip(turn['prompt'], 400)}"]
    if turn["reply"]:
        lines.append(f"**결과:** {_clip(turn['reply'], 600)}")
    if turn["files"]:
        lines.append("**변경:** " + ", ".join(turn["files"]))
    if turn["tools"]:
        lines.append("**도구:** " + ", ".join(dict.fromkeys(turn["tools"])))
    return "\n".join(lines) + "\n"


def append_log(root, text, now):
    path = root / DIR / "logs" / f"{now:%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(text)
    return path


HANDLERS = {}


def main():
    """훅은 어떤 경우에도 작업을 막지 않는다. 오류는 기록만 하고 exit 0."""
    root = None
    try:
        data = json.load(sys.stdin)
        root = find_root(data.get("cwd"))
        handler = HANDLERS.get(data.get("hook_event_name"))
        if handler:
            handler(data)
    except Exception as e:
        log_error(root, e)


# ---------------------------------------------------------------- selftest


def _project():
    d = Path(tempfile.mkdtemp()).resolve()
    (d / ".leneu-session" / "logs").mkdir(parents=True)
    return d


def test_find_root_climbs_from_subfolder():
    root = _project()
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    assert find_root(str(sub)) == root


def test_find_root_stops_at_nearest_git():
    root = _project()
    inner = root / "nested"
    (inner / ".git").mkdir(parents=True)
    assert find_root(str(inner)) is None


def test_find_root_treats_git_file_as_boundary():
    root = _project()
    inner = root / "wt"
    inner.mkdir()
    (inner / ".git").write_text("gitdir: /x\n")
    assert find_root(str(inner)) is None


def test_find_root_stops_at_home():
    root = _project()
    home = root / "home"
    home.mkdir()
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        assert find_root(str(home)) is None
    finally:
        os.environ["HOME"] = old


def test_find_root_none_when_disabled():
    root = _project()
    (root / ".leneu-session" / "disabled").touch()
    assert find_root(str(root)) is None


def test_load_config_defaults():
    cfg = load_config(_project())
    assert cfg == {"context_window": 1000000, "clear_recommend_pct": 30, "checkpoint_every_pct": 10}


def test_load_config_override_keeps_other_defaults():
    root = _project()
    (root / ".leneu-session" / "config.json").write_text('{"context_window": 200000}')
    cfg = load_config(root)
    assert cfg["context_window"] == 200000 and cfg["clear_recommend_pct"] == 30


def test_load_config_broken_uses_defaults():
    root = _project()
    (root / ".leneu-session" / "config.json").write_text("{broken")
    assert load_config(root)["clear_recommend_pct"] == 30
    (root / ".leneu-session" / "config.json").write_text('{"context_window": "many"}')
    assert load_config(root)["context_window"] == 1000000
    (root / ".leneu-session" / "config.json").write_text("[1]")
    assert load_config(root)["context_window"] == 1000000


def test_state_roundtrip_leaves_no_temp_files():
    root = _project()
    save_state(root, {"s": {"last_ckpt": 5}})
    assert load_state(root)["s"]["last_ckpt"] == 5
    names = {p.name for p in (root / ".leneu-session").iterdir()}
    assert names == {"logs", ".state.json"}, names


def test_state_broken_returns_empty():
    root = _project()
    (root / ".leneu-session" / ".state.json").write_text("{broken")
    assert load_state(root) == {}
    (root / ".leneu-session" / ".state.json").write_text("[]")
    assert load_state(root) == {}


def test_state_keeps_recent_sessions_only():
    root = _project()
    save_state(root, {f"s{i}": {"seen": i} for i in range(30)})
    state = load_state(root)
    assert len(state) == 20 and "s29" in state and "s0" not in state


def test_log_error_writes_to_file():
    root = _project()
    log_error(root, ValueError("boom"))
    assert "boom" in (root / ".leneu-session" / "hook-errors.log").read_text()
    log_error(None, ValueError("no root"))  # 루트를 모르면 조용히 넘어간다


USAGE = {"input_tokens": 4, "cache_creation_input_tokens": 761, "cache_read_input_tokens": 78235, "output_tokens": 999}


def _user(text, **kw):
    return {"type": "user", "uuid": text[:8], "message": {"content": text}, **kw}


def _assistant(text="", tools=(), usage=None, **kw):
    content = [{"type": "tool_use", "name": n, "input": i} for n, i in tools]
    if text:
        content.append({"type": "text", "text": text})
    msg = {"content": content}
    if usage:
        msg["usage"] = usage
    return {"type": "assistant", "uuid": "a" + text[:7], "message": msg, **kw}


def test_context_tokens_excludes_output():
    assert context_tokens([_assistant("x", usage=USAGE)]) == 79000


def test_context_tokens_uses_last_main_entry():
    side = dict(USAGE, input_tokens=1000000)
    entries = [_assistant("a", usage=USAGE), _assistant("b", usage=side, isSidechain=True)]
    assert context_tokens(entries) == 79000


def test_context_tokens_none_without_usage():
    assert context_tokens([_assistant("x")]) is None
    zero = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    assert context_tokens([_assistant("x", usage=zero)]) is None
    assert context_tokens([_assistant("x", usage={"input_tokens": "?"})]) is None


def test_parse_turns_records_every_turn():
    turns = parse_turns([
        _user("첫 요청"), _assistant("첫 답"),
        _user("둘째 요청"),
        _assistant("둘째 답", tools=[("Edit", {"file_path": "a.py"}), ("Bash", {"command": "ls"})]),
    ])
    assert [t["prompt"] for t in turns] == ["첫 요청", "둘째 요청"]
    assert turns[0]["reply"] == "첫 답" and turns[1]["files"] == ["a.py"]
    assert turns[1]["tools"] == ["Edit", "Bash"]


def test_parse_turns_ignores_system_entries():
    tool_result = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}
    assert parse_turns([_user("<task-notification>끝남</task-notification>"), _assistant("답")]) == []
    assert parse_turns([tool_result]) == []
    assert parse_turns([_user("<system-reminder>잡음</system-reminder>")]) == []
    assert parse_turns([_user("스킬 본문", isMeta=True)]) == []
    assert parse_turns([_user("압축 요약", isCompactSummary=True)]) == []


def test_parse_turns_skips_sidechain_and_strips_tags():
    turns = parse_turns([
        _user("<system-reminder>잡음</system-reminder>진짜 요청"),
        _assistant("메인"),
        _assistant("서브", isSidechain=True),
        _user("서브 요청", isSidechain=True),
    ])
    assert len(turns) == 1 and turns[0]["prompt"] == "진짜 요청" and turns[0]["reply"] == "메인"


def test_mask_secrets():
    for secret in ["sk-ant-abc123def456ghi", "ghp_abcdefghijklmnop1234", "github_pat_abc123def456",
                   "AKIAIOSFODNN7EXAMPLE", "Bearer abcdef123456789", "xoxb-1234567890-abc"]:
        out = mask_secrets(f"키는 {secret} 입니다")
        assert secret not in out and "키는" in out and "입니다" in out, out
    assert mask_secrets("일반 문장 task-sk 와 skill") == "일반 문장 task-sk 와 skill"


def test_render_turn_format_and_clipping():
    turn = {"prompt": "줄1\n## 줄2 " + "가" * 500, "reply": "나" * 700 + " sk-ant-abc123def456ghi",
            "tools": ["Edit", "Edit", "Bash"], "files": ["a.py"]}
    out = render_turn(turn, datetime(2026, 11, 9, 14, 2))
    assert out.lstrip().startswith("## 2026-11-09 14:02 turn\n")
    assert "\n## " not in out.lstrip()[3:], out  # 여러 줄 입력도 헤더를 만들지 않음
    assert "**변경:** a.py" in out and "**도구:** Edit, Bash" in out
    req = next(l for l in out.splitlines() if l.startswith("**요청:**"))
    res = next(l for l in out.splitlines() if l.startswith("**결과:**"))
    assert len(req) <= 410 and len(res) <= 610


def test_render_turn_masks_before_clipping():
    out = render_turn({"prompt": "sk-ant-abc123def456ghi", "reply": "", "tools": [], "files": []},
                      datetime(2026, 11, 9))
    assert "sk-ant" not in out


def test_append_log_by_date_appends_only():
    root = _project()
    path = append_log(root, "하나\n", datetime(2026, 11, 9, 23, 59))
    assert path == root / ".leneu-session" / "logs" / "2026-11-09.md"
    append_log(root, "둘\n", datetime(2026, 11, 9, 23, 59))
    assert path.read_text() == "하나\n둘\n"
    assert append_log(root, "셋\n", datetime(2026, 11, 10)).name == "2026-11-10.md"


def test_load_transcript_skips_broken_lines():
    p = Path(tempfile.mkdtemp()) / "t.jsonl"
    p.write_text('{"type":"user"}\n{broken\n\n[1]\n')
    assert load_transcript(str(p)) == [{"type": "user"}]
    assert load_transcript("") == [] and load_transcript("/nonexistent") == []


def selftest():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
