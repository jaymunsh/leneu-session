#!/usr/bin/env python3
"""leneu-session 훅 (Stop / PreCompact / SessionStart / PreToolUse 공용).

자기검증: python3 session_hook.py --selftest
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path


DIR = ".leneu-session"
DEFAULTS = {"context_window": 1000000, "clear_recommend_pct": 30, "checkpoint_every_pct": 5, "keep_log_days": 0}
MAX_SESSIONS = 20
MAX_DEFER = 8  # 완료 알림이 끝내 안 오는 작업을 이 횟수의 Stop까지만 기다린다
PENDING = ".pending-checkpoint.md"  # 세션을 모를 때(예전 버전, 테스트) 쓰는 이름
PENDING_OF = re.compile(r"^\.pending-checkpoint-([\w-]{1,8})\.md$")
MAX_INJECT = 8000
TURNS_ROOM = 2000
FLUSH_WAIT = 2.0
MAX_HANDOFFS = 10
HEADER = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (turn|checkpoint|compact)\b.*$", re.M)
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
SYSTEM_TAG = re.compile(
    r"<(system-reminder|task-notification|command-[a-z-]+|local-command-[a-z-]+)>.*?</\1>", re.S
)
COMMAND = re.compile(r"\s*(?:<command-message>.*?</command-message>\s*)?<command-name>(.*?)</command-name>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
SECRET = re.compile(
    r"\bsk-[\w-]{16,}|\bghp_\w{16,}|\bgithub_pat_\w{10,}|\bAKIA[0-9A-Z]{16}\b"
    r"|\bBearer\s+[\w.~+/-]{10,}|\bxox[bp]-[\w-]{8,}"
)
# 보안 모드에서만 가린다. 순서가 중요하다: 긴 형식(이메일·카드)을 먼저 가려야 짧은 형식이 그 일부를 먹지 않는다
SECURE_RULES = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[이메일]"),
    (re.compile(r"(?<![\w.-])(?:https?://)?[\w-]+(?:\.[\w-]+)*\.(?:internal|corp|local|intra|lan)(?![\w.-])"
                r"(?:[:/][^\s)\]>\"']*)?"), "[내부주소]"),
    (re.compile(r"(?<!\d)\d{6}-[1-8]\d{6}(?!\d)"), "[주민번호]"),
    (re.compile(r"(?<!\d)\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}(?!\d)"), "[카드번호]"),
    (re.compile(r"(?<![\d+])(?:\+82[- ]?|0)1[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)"
                r"|(?<![\d+])0(?:2|[3-6]\d)[- .]\d{3,4}[- .]\d{4}(?!\d)"), "[전화번호]"),
    (re.compile(r"(?<![\d-])\d{2,6}-\d{2,6}-\d{2,7}(?![\d-])"), "[계좌번호]"),
    (re.compile(r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})"
                r"(?![\d.])"), "[내부IP]"),
    # 대소문자와 숫자가 섞인 긴 문자열. 커밋 해시·UUID(소문자 16진)와 경로는 건드리지 않는다
    (re.compile(r"(?<![\w/.-])(?=[\w-]*[a-z])(?=[\w-]*[A-Z])(?=[\w-]*\d)[\w-]{32,}(?![\w/.-])"), "[토큰]"),
]
CONFIDENTIAL = re.compile(r"대외비|기밀|사내\s*(?:한정|전용)|confidential|internal use only|do not distribute|\bNDA\b", re.I)
# 백그라운드 Bash는 기다리지 않는다. 개발 서버처럼 끝나지 않는 명령이 흔하다 (실제 기록 13세션 중 다수)
LAUNCHED = re.compile(r"\s*Async agent launched successfully")
NOTIFIED = re.compile(r"<task-notification>.*?<tool-use-id>(.*?)</tool-use-id>", re.S)


def find_root(cwd):
    """켜져 있는 프로젝트 루트. 가장 가까운 .git 또는 홈 위로는 올라가지 않는다."""
    start = Path(cwd or os.getcwd()).resolve()
    home = Path.home().resolve()
    for d in (start, *start.parents):
        if d == home:  # 홈에 폴더가 있어도 하위 전체를 기록하게 두지 않는다
            return None
        if (d / DIR).is_dir():
            return None if (d / DIR / "disabled").exists() else d
        if (d / ".git").exists():
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


def load_secure(root, any_state=False):
    """보안 모드 설정. 꺼져 있으면 None (any_state면 꺼져 있어도 지정어 목록을 준다)."""
    raw = _read_json_dict(root / DIR / "secure.json")
    terms = raw.get("terms") if isinstance(raw.get("terms"), list) else []
    secure = {"enabled": raw.get("enabled") is True, "terms": [t for t in terms if isinstance(t, str) and t.strip()]}
    return secure if secure["enabled"] or any_state else None


def load_state(root):
    return {
        sid: sess for sid, sess in _read_json_dict(root / DIR / ".state.json").items()
        if isinstance(sess, dict) and isinstance(sess.get("last_ckpt", 0), (int, float))
    }


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
    with p.open(encoding="utf-8", errors="replace") as f:  # splitlines는 문자열 속 U+2028에서도 끊는다
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
    return out


def _message(entry):
    m = entry.get("message")
    return m if isinstance(m, dict) else {}


def _last_main_text(entries):
    for e in reversed(entries):
        if e.get("type") == "assistant" and not e.get("isSidechain"):
            text = _text(_message(e).get("content")).strip()
            if text:
                return text
    return None


def load_settled(path, message, wait=None):
    """Stop 시점에 마지막 응답이 아직 transcript에 안 쓰였을 수 있어 잠깐 기다린다.
    늦게 읽으면 토큰 수가 한 턴 밀리고 결과가 비어서 기록된다."""
    entries = load_transcript(path)
    if not message:
        return entries
    deadline = time.monotonic() + (FLUSH_WAIT if wait is None else wait)
    while _last_main_text(entries) != message.strip() and time.monotonic() < deadline:
        time.sleep(0.05)
        entries = load_transcript(path)
    return entries


def context_tokens(entries):
    """마지막 메인 응답이 쓴 컨텍스트 크기. 출력 토큰은 포함하지 않는다."""
    for e in reversed(entries):
        if e.get("isSidechain") or e.get("type") != "assistant":
            continue
        usage = _message(e).get("usage")
        if not usage or not isinstance(usage, dict):
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
    return "\n".join(
        b["text"] for b in content
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    )


def _prompt(text):
    """슬래시 명령은 `/이름 인자`로 적는다. 펼쳐진 본문은 isMeta로 따로 온다."""
    command = COMMAND.match(text)
    if not command:
        return SYSTEM_TAG.sub("", text).strip(), False
    args = COMMAND_ARGS.search(text)
    return f"{command.group(1)} {args.group(1) if args else ''}".strip(), True


def _queued_prompt(entry):
    """모델이 일하는 도중 입력한 메시지는 user 항목이 아니라 queued_command 첨부로만 남는다."""
    a = entry.get("attachment")
    return _text(a.get("prompt")) if isinstance(a, dict) and a.get("type") == "queued_command" else None


def running_tasks(entries):
    """백그라운드로 띄웠는데 아직 완료 알림이 오지 않은 서브에이전트의 tool_use_id."""
    launched, done = set(), set()
    for e in entries:
        if e.get("isSidechain"):
            continue
        content = _message(e).get("content")
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and LAUNCHED.match(_text(b.get("content"))):
                launched.add(b.get("tool_use_id"))
        for text in (_queued_prompt(e), _text(content)):
            if text and "<task-notification>" in text:
                done.update(NOTIFIED.findall(text))
    return launched - done


def parse_turns(entries):
    """사람이 보낸 요청마다 턴 하나. 시스템이 끼워 넣은 user 항목은 턴이 아니다."""
    turns = []
    for e in entries:
        if e.get("isSidechain"):
            continue
        content = _message(e).get("content")
        queued = _queued_prompt(e)
        if e.get("type") == "user" or queued is not None:
            if e.get("isMeta") or e.get("isCompactSummary"):
                continue
            prompt, command = _prompt(_text(content) if queued is None else queued)
            if prompt and not prompt.startswith("[Request interrupted"):
                turns.append({"prompt": prompt, "reply": "", "tools": [], "files": [], "command": command})
        elif e.get("type") == "assistant" and turns:
            turn = turns[-1]
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and _text([b]).strip():
                    turn["reply"] = (turn["reply"] + "\n\n" + b["text"].strip()).strip()
                elif b.get("type") == "tool_use":
                    name = b.get("name")
                    turn["tools"].append(name if isinstance(name, str) else "?")
                    tool_input = b.get("input")
                    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
                    if name in EDIT_TOOLS and isinstance(path, str) and path not in turn["files"]:
                        turn["files"].append(path)
    # /clear 같은 로컬 명령은 응답이 없다. 마지막 항목은 응답이 아직 안 쓰였을 수 있어 남긴다
    return [t for t in turns[:-1] if not t["command"] or t["reply"] or t["tools"]] + turns[-1:]


def redact(text, secure):
    """가린 글과 가린 개수. 비밀값은 항상, 나머지는 보안 모드일 때만 가린다.
    ponytail: 형식 있는 값과 지정어만 잡는다. 평문으로 쓴 기밀은 지정어와 체크포인트 지시문에 맡긴다."""
    rules = [(SECRET, "[비밀값 가림]")] + (SECURE_RULES if secure else [])
    terms = secure["terms"] if secure else []
    if terms:
        index = {t.lower(): i for i, t in reversed(list(enumerate(terms, 1)))}
        alts = "|".join(re.escape(t) for t in sorted(index, key=len, reverse=True))
        rules.append((re.compile(alts, re.I), lambda m: f"[지정어-{index[m.group().lower()]}]"))
    hits = 0
    for pattern, label in rules:
        def replace(m):
            nonlocal hits
            if label == "[계좌번호]" and sum(c.isdigit() for c in m.group()) < 10:  # 날짜(2026-11-12) 등
                return m.group()
            hits += 1
            return label(m) if callable(label) else label
        text = pattern.sub(replace, text)
    return text, hits


def mask_secrets(text):
    return redact(text, None)[0]


def _clip(s, n, tail=False):
    s = " ".join(s.split())
    if len(s) <= n:
        return s
    return "…" + s[1 - n:] if tail else s[: n - 1] + "…"


def render_turn(turn, now, secure=None, session=None):
    """가린 뒤에 자른다. 잘린 비밀값 조각은 패턴에 걸리지 않는다."""
    prompt, a = redact(turn["prompt"], secure)
    reply, b = redact(turn["reply"], secure)
    files, c = redact(", ".join(turn["files"]), secure)
    tag = f" session={session[:8]}" if session else ""
    lines = ["", f"## {now:%Y-%m-%d %H:%M} turn{tag}", f"**요청:** {_clip(prompt, 400)}"]
    if reply:
        lines.append(f"**결과:** {_clip(reply, 600, tail=True)}")  # 결론은 끝에 있다
    if files:
        lines.append("**변경:** " + files)
    if turn["tools"]:
        lines.append("**도구:** " + ", ".join(dict.fromkeys(turn["tools"])))
    if secure and a + b + c:
        lines.append(f"**보안:** {a + b + c}건 가림")
    return "\n".join(lines) + "\n"


def append_log(root, text, now):
    path = root / DIR / "logs" / f"{now:%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", errors="backslashreplace") as f:  # 짝 없는 서로게이트가 올 수 있다
        f.write(text)
    return path


def decide_checkpoint(cfg, sess, tokens):
    window = cfg["context_window"]
    over = tokens >= window * cfg["clear_recommend_pct"] / 100
    if over and not sess.get("recommended"):  # 기준을 넘은 첫 턴에 바로 권한다. 간격을 기다리지 않는다
        return "checkpoint+clear"
    if tokens - sess["last_ckpt"] < window * cfg["checkpoint_every_pct"] / 100:
        return "none"
    return "checkpoint+clear" if over else "checkpoint"


# 저장된 파일은 훅이 가린 뒤라 원문으로 Edit하면 실패한다 (2026-11-12 실측: Edit 4번 실패 후 다시 씀)
SECURE_WRITER_NOTE = ("보안 모드: 기밀 문서 본문은 옮기지 말고 파일 경로·절 위치로 가리키며, 회사·고객·사람 이름은 역할로 씁니다. "
                      "저장할 때 훅이 개인정보와 지정어를 자동으로 가리므로, 저장한 뒤 다시 고치지 마세요.")


def pending_path(root, session=None):
    """세션마다 따로 둔다. 같은 폴더의 다른 터미널이 남의 체크포인트를 자기 이름으로 옮기지 않게."""
    return root / DIR / (f".pending-checkpoint-{session[:8]}.md" if session and session != "?" else PENDING)


def checkpoint_reason(root, recommend_clear, session=None):
    """화면에 그대로 보이므로 짧게 쓴다."""
    lines = [
        f"leneu-session 체크포인트 시점입니다. 다른 작업 없이 {pending_path(root, session)} 하나만 Write로 쓰고 "
        "'체크포인트 저장'이라고만 답하세요.",
        "### 헤더로 아래 순서대로, 해당 없는 항목은 빼고 6000자 이내, 비밀값 없이 씁니다: "
        "현재 작업의 요구와 사용자 정정(원문 그대로. 이전 체크포인트의 원문도 잇되 끝난 작업 것은 뺌) / "
        "확인된 사실(경로·명령·값·에러) / 서브에이전트 결과(결론·근거 파일·아직 반영 안 한 것) / "
        "변경 파일과 검증 여부 / 결정(고른 것, 버린 것, 이유) / "
        "막다른 길(실패한 시도와 이유) / 완료 기준(무엇이 되면 끝인가) / 다음 액션(1~3개, 파일 경로 포함)",
    ]
    if load_secure(root):
        lines.append(SECURE_WRITER_NOTE)
    if recommend_clear:
        lines.append("답 끝에 '컨텍스트가 커졌습니다. 작업이 일단락되면 /clear를 권합니다.'를 붙이세요.")
    return "\n".join(lines)


def _demote(body):
    """모델이 쓴 #, ## 헤더를 ###로 내린다. 코드 블록 안은 그대로 두되 로그 헤더 모양만 깬다."""
    out, fence = [], False
    for line in body.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif fence:
            line = " " + line if HEADER.match(line) else line
        else:
            line = re.sub(r"^#{1,2} ", "### ", line)
        out.append(line)
    return "\n".join(out)


def consume_pending(root, tokens, session_id, transcript_path, now, pending=None):
    """모델이 쓴 임시 파일을 로그로 옮긴다. 헤더와 시각은 훅이 붙인다.
    자기 세션의 파일을 먼저, 없으면 세션 없는 예전 이름의 파일을 옮긴다."""
    if pending is None:
        pending = next((p for p in (pending_path(root, session_id), root / DIR / PENDING) if p.exists()), None)
    if pending is None or not pending.exists():
        return False
    body = pending.read_text(errors="replace").strip()
    if body:
        body = redact(_demote(body), load_secure(root))[0]
        append_log(root, (
            f"\n## {now:%Y-%m-%d %H:%M} checkpoint tokens={tokens or '?'} session={session_id[:8]}\n"
            + (f"**원본:** {transcript_path}\n" if transcript_path else "")
            + f"{body}\n"
        ), now)
    pending.unlink()
    return bool(body)


def _new_turns(entries, cursor):
    """커서 뒤의 턴들. 커서를 모르면 이전 턴이 이미 기록됐거나 기록하면 안 되는 것일 수 있어
    (상태 파일 손상, 세션 중간에 켜기, fork) 마지막 턴만 남긴다. 로그는 지울 수 없기 때문이다."""
    if cursor:
        for i, e in enumerate(entries):
            if e.get("uuid") == cursor:
                return parse_turns(entries[i + 1:])
    return parse_turns(entries)[-1:]


def on_stop(data):
    root = find_root(data.get("cwd"))
    if root is None:
        return
    now = datetime.now()
    sid = data.get("session_id") or "?"
    transcript = data.get("transcript_path") or ""
    active = bool(data.get("stop_hook_active"))
    message = data.get("last_assistant_message")
    entries = load_settled(transcript, message)

    state = load_state(root)
    sess = state.pop(sid, {})
    state[sid] = sess  # 최근 사용한 세션을 뒤로 보내 오래된 것부터 지워지게 한다

    output = None
    try:
        # 체크포인트 지시는 첨부로 들어와 턴이 아니다. 그 사이 사용자가 입력한 메시지는 여기서 남겨야 한다
        _record_turns(root, entries, sess, message, now, sid)

        tokens = context_tokens(entries)
        consume_pending(root, tokens, sid, transcript, now)
        handoff = latest_handoff(root, _lineage(root, sid, state))
        handoff_id = str(handoff[1]) + str(handoff[1].stat().st_mtime) if handoff else None

        if tokens is not None:
            if "last_ckpt" not in sess or tokens < sess["last_ckpt"]:  # 첫 측정 또는 압축 직후
                sess["baseline"] = sess["last_ckpt"] = tokens
                sess["handoff_seen"] = handoff_id
                sess.pop("recommended", None)
            if handoff_id != sess.get("handoff_seen"):  # 방금 수동으로 남겼으니 겹쳐 쓰지 않는다
                sess["handoff_seen"] = handoff_id
                sess["last_ckpt"] = tokens
            elif not active and not is_noninteractive(_entrypoint()):
                action = decide_checkpoint(load_config(root), sess, tokens)
                # 서브에이전트가 도는 중이면 결과가 빠진 체크포인트가 되고, /clear하면 결과를 받을 세션이 사라진다
                if action != "none" and running_tasks(entries) and sess.get("deferred", 0) < MAX_DEFER:
                    sess["deferred"] = sess.get("deferred", 0) + 1
                    action = "none"
                elif action != "none":
                    sess.pop("deferred", None)
                if action != "none":
                    sess["last_ckpt"] = tokens
                    sess["recommended"] = sess.get("recommended") or action == "checkpoint+clear"
                    # decision: block은 transcript에 오류로 남아 모델이 체크포인트에 오류라고 적는다
                    output = {"hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": checkpoint_reason(root, action == "checkpoint+clear", sid),
                    }}
    finally:  # 커서를 못 남기면 다음 Stop마다 같은 턴이 다시 쌓이고 같은 오류가 반복된다
        _move_cursor(entries, sess)
        save_state(root, state)
    if output:
        print(json.dumps(output, ensure_ascii=False))


def _record_turns(root, entries, sess, message, now, sid=None):
    turns = _new_turns(entries, sess.get("cursor"))
    if turns and message and message.strip() not in turns[-1]["reply"]:
        turns[-1]["reply"] = (turns[-1]["reply"] + "\n\n" + message.strip()).strip()
    secure = load_secure(root)
    for turn in turns:
        append_log(root, render_turn(turn, now, secure, sid), now)


def _move_cursor(entries, sess):
    for e in reversed(entries):  # 끝에는 uuid 없는 메타 항목이 올 수 있다
        if e.get("uuid"):
            sess["cursor"] = e["uuid"]
            return


def on_session_end(data):
    """Esc로 끊긴 턴은 Stop이 없다. 바로 /clear 하면 여기서만 남길 수 있다."""
    root = find_root(data.get("cwd"))
    if root is None:
        return
    entries = load_transcript(data.get("transcript_path"))
    state = load_state(root)
    sid = data.get("session_id") or "?"
    sess = state.pop(sid, {})
    state[sid] = sess
    try:
        _record_turns(root, entries, sess, None, datetime.now(), sid)
        # 체크포인트를 쓰자마자 끝내면 옮길 Stop이 없다. 다음 세션이 옮기면 주인을 모른다
        consume_pending(root, context_tokens(entries), sid, data.get("transcript_path"), datetime.now())
        if data.get("reason") == "clear":  # 바로 뒤 SessionStart(clear)가 이 세션의 기록을 고르게 한다
            (root / DIR / ".cleared").write_text(sid)
    finally:
        _move_cursor(entries, sess)
        save_state(root, state)


def _sections(text):
    """로그를 (시각, 종류, 본문)으로 나눈다. 헤더 형식이 아닌 줄은 앞 절에 붙는다."""
    matches = list(HEADER.finditer(text))
    for m, nxt in zip(matches, [*matches[1:], None]):
        end = nxt.start() if nxt else len(text)
        try:
            when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
        except ValueError:  # 손으로 쓴 잘못된 헤더 하나가 복귀 전체를 막으면 안 된다
            continue
        yield when, m.group(2), text[m.start():end].strip()


def _log_files(root):
    return sorted((root / DIR / "logs").glob("*.md"))


def _of_session(body, sessions):
    """같은 폴더를 터미널 여러 개로 쓸 때 다른 터미널의 기록을 거른다. sessions는 이 터미널에서 이어진 세션들이다.
    표시가 없는 예전 기록은 통과시킨다."""
    if not sessions:
        return True
    tag = _tag(body)
    return not tag or tag in {s[:8] for s in sessions}


def latest_checkpoint(root, session=None):
    for path in reversed(_log_files(root)):
        found = [s for s in _sections(path.read_text(errors="replace"))
                 if s[1] == "checkpoint" and _of_session(s[2], session)]
        if found:
            return found[-1][0], found[-1][2], path
    return None


def latest_handoff(root, sessions=None):
    """수동 인계 문서 중 최신. 시각은 로그 헤더와 비교하도록 분 단위로 맞춘다."""
    ids = {s[:8] for s in sessions or ()}
    files = [p for p in (root / DIR / "handoffs").glob("handoff-*.md")
             if not ids or not HANDOFF_TAG.match(p.name) or HANDOFF_TAG.match(p.name).group(1) in ids]
    if not files:
        return None
    path = max(files, key=lambda p: p.stat().st_mtime)
    return datetime.fromtimestamp(path.stat().st_mtime).replace(second=0, microsecond=0), path


def _latest_source(root, session=None):
    """복귀에 쓸 최신 기록: (시각, 본문, 경로). 같은 분이면 사람이 요청한 handoff가 우선이다.
    세션을 알면 그 세션의 체크포인트를, 없으면 가장 최근 것을 쓴다."""
    checkpoint = (latest_checkpoint(root, session) if session else None) or latest_checkpoint(root)
    handoff = latest_handoff(root, session)
    if handoff and (not checkpoint or handoff[0] >= checkpoint[0]):
        return handoff[0], handoff[1].read_text(errors="replace"), handoff[1]
    return checkpoint


def turns_after(root, since, session=None):
    """같은 분에 쓰인 턴은 포함한다. 잃는 것보다 한 줄 겹치는 쪽이 낫다."""
    files = _log_files(root)
    if since is None:
        files = files[-2:]  # 체크포인트가 없으면 최근 것만 본다
    else:
        files = [p for p in files if p.stem >= f"{since:%Y-%m-%d}"]
    out = []
    for path in files:
        for dt, kind, body in _sections(path.read_text(errors="replace")):
            if kind == "turn" and (since is None or dt >= since) and _of_session(body, session):
                out.append(body)
    return "\n\n".join(out)


def _tag(body):
    """기록의 세션 표시. 주인을 모르는 기록(session=?)은 표시 없음으로 본다. 놓치는 것보다 낫다."""
    m = re.search(r"session=([^\s?]\S*)", body.split("\n", 1)[0])
    return m.group(1) if m else None


def _gist(body):
    """기록 한 줄 요약. 다음 액션의 첫 줄, 없으면 첫 내용 줄."""
    lines = [l.strip() for l in body.splitlines()[1:] if l.strip() and not l.startswith("**원본")]
    for i, line in enumerate(lines):
        if line.startswith("### 다음 액션") and i + 1 < len(lines):
            return _clip(lines[i + 1], 120)
    return _clip(next((l for l in lines if not l.startswith("#")), ""), 120)


def _chains(root):
    """세션 표시(8자) → 그 터미널 줄기의 첫 세션 표시. 상태에 없는 세션은 혼자 한 줄기다."""
    state = load_state(root)
    full = {k[:8]: k for k in state}

    def chain(tag):
        return (_lineage(root, full.get(tag), state) or [tag])[-1][:8]
    return chain, list(full)


def _record_tag(body, path):
    m = HANDOFF_TAG.match(path.name) if path else None
    return _tag(body) or (m.group(1) if m else None)


def other_work(root, mine, now):
    """새로 시작한 세션은 어느 터미널을 이을지 모른다. 다른 줄기의 최근 24시간 체크포인트를 (시각, 표시, 요약)으로 준다."""
    chain, _ = _chains(root)
    latest = {}
    for path in _log_files(root)[-2:]:
        for dt, kind, body in _sections(path.read_text(errors="replace")):
            tag = _tag(body)
            if kind == "checkpoint" and tag and (now - dt).total_seconds() <= 24 * 3600:
                latest[chain(tag)] = (dt, tag, _gist(body))
    own = chain(mine) if mine else None
    return sorted(v for k, v in latest.items() if k != own)


def build_context(root, source, now, session=None):
    if source not in ("clear", "startup", "compact"):
        return None
    if session and not latest_checkpoint(root, session) and not latest_handoff(root, session):
        session = None  # 체크포인트를 남긴 적 없는 세션이면 가장 최근 기록과 그 뒤 모든 턴을 쓴다
    latest = _latest_source(root, session)
    if source == "compact" and not latest:
        return None  # 턴 기록은 압축 요약이 이미 담고 있다
    since, body, path = latest or (None, "", None)
    mine = _record_tag(body, path)
    if not session and mine:  # 이 터미널을 모를 때도 고른 기록과 같은 줄기의 턴만 붙인다
        chain, tags = _chains(root)
        session = {t for t in tags if chain(t) == chain(mine)} | {mine}
    turns = turns_after(root, since, session)
    if not body and not turns:
        return None
    path = path or _log_files(root)[-1]
    written = since or datetime.fromtimestamp(path.stat().st_mtime)

    if source == "compact":
        guidance = (
            "leneu-session: 컨텍스트 압축 직후입니다. 아래는 압축 전 마지막 기록과 그 뒤의 턴으로, "
            "압축 요약에서 빠진 결정과 실패한 시도를 보완합니다. 브리핑하거나 다시 묻지 말고 하던 작업을 계속하세요. "
            f"전체 기록: {path}"
        )
    else:
        guidance = (
            "leneu-session: 이전 작업 기록입니다. 사용자가 '.'처럼 짧게만 입력하면 이어가라는 뜻입니다. "
            "저장소 상태로 다음 액션이 아직 유효한지 확인한 뒤 이어가세요. 사용자의 요청이 이 작업과 무관하면 이 기록은 무시하세요. "
            f"전체 기록: {path}"
        )
    if source == "startup":
        guidance += f"\n작성 시각: {written:%Y-%m-%d %H:%M}."
        if (now - written).total_seconds() > 24 * 3600:
            guidance += " 하루 이상 지난 기록이니 이어갈지 먼저 사용자에게 물어보세요."
        others = other_work(root, mine, now)
        if others:
            guidance += "\n같은 폴더의 다른 터미널에서 하던 작업도 있습니다(최근 24시간):\n" + "".join(
                f"- {dt:%m-%d %H:%M} session={tag}: {gist}\n" for dt, tag, gist in others
            ) + ("사용자가 '.'처럼 짧게만 입력했다면 아래 기록과 위 작업 중 어느 작업을 이을지 먼저 물어보세요. "
                 "다른 작업의 기록은 로그에서 session=표시로 찾습니다.")

    # 다음 액션은 체크포인트 끝에 있어 앞뒤를 모두 남기고, 이후 턴 자리도 남긴다
    limit = MAX_INJECT - min(len(turns), TURNS_ROOM)
    if len(body) > limit:
        keep = (limit - 100) // 2
        body = f"{body[:keep]}\n\n[중략 {len(body) - 2 * keep}자. 전체: {path}]\n\n{body[-keep:]}"
    room = max(0, MAX_INJECT - len(body))
    if len(turns) > room:
        turns = turns[-room:] if room else ""
    parts = [guidance, body]
    if turns:
        parts.append("그 이후의 턴 기록:\n" + turns)
    return "\n\n".join(p for p in parts if p)


def is_allowed_write(root, file_path):
    """자동 승인은 이 경로들뿐이다. 심볼릭 링크로 밖을 가리키면 허용하지 않는다."""
    if not file_path:
        return False
    path = Path(file_path).resolve()
    if path == root / DIR / PENDING or (path.parent == root / DIR and PENDING_OF.match(path.name)):
        return True
    return path.parent == root / DIR / "handoffs" and fnmatch(path.name, "handoff-*.md")


def is_noninteractive(entrypoint):
    """claude -p 는 sdk-cli, 대화형은 cli (2026-11-09 실측, 공식 문서에는 없음). 모르면 대화형으로 본다."""
    return bool(entrypoint) and entrypoint.startswith("sdk")


def _entrypoint():
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT")


def _create_folder(target):
    folder = target / DIR
    (folder / "logs").mkdir(parents=True, exist_ok=True)
    if not (folder / ".gitignore").exists():
        (folder / ".gitignore").write_text("*\n")


def ensure_worktree(cwd):
    """로그 폴더는 git에서 제외되어 새 worktree에 없다. 메인이 켜져 있으면 여기도 켠다."""
    start = Path(cwd or os.getcwd()).resolve()
    target = next((d for d in (start, *start.parents) if (d / ".git").exists()), None)
    if target is None or not (target / ".git").is_file() or (target / DIR).exists():
        return None
    try:
        line = (target / ".git").read_text().strip()
        if not line.startswith("gitdir:"):
            return None
        gitdir = (target / line[len("gitdir:"):].strip()).resolve()
        main = (gitdir / (gitdir / "commondir").read_text().strip()).resolve().parent
    except OSError:  # 서브모듈에는 commondir이 없다
        return None
    if not (main / DIR).is_dir() or (main / DIR / "disabled").exists():
        return None
    _create_folder(target)
    for name in ("config.json", "secure.json"):
        if (main / DIR / name).exists():
            shutil.copy(main / DIR / name, target / DIR / name)
    return target


def new_errors(root):
    """새 오류를 한 번만 알리고 지난 기록으로 옮긴다."""
    log = root / DIR / "hook-errors.log"
    if not log.exists():
        return None
    lines = log.read_text(errors="replace").splitlines()
    with (root / DIR / "hook-errors.old.log").open("a") as f:
        f.write("".join(l + "\n" for l in lines))
    log.unlink()
    if not lines:
        return None
    return f"leneu-session 훅 오류 {len(lines)}건 (hook-errors.old.log). 마지막: {lines[-1][:200]}"


def prune_logs(root, cfg, now):
    """keep_log_days가 0이면(기본) 지우지 않는다."""
    days = cfg["keep_log_days"]
    if not days:
        return
    cutoff = f"{now - timedelta(days=days):%Y-%m-%d}"
    for path in _log_files(root):
        if path.stem < cutoff:
            path.unlink()


def _recover_pending(root):
    """비정상 종료로 남은 체크포인트. 쓰인 시각으로 넣고, 주인은 파일 이름에서 찾는다(모르면 ?)."""
    for pending in (root / DIR).glob(".pending-checkpoint*.md"):
        owner = PENDING_OF.match(pending.name)
        consume_pending(root, None, owner.group(1) if owner else "?", None,
                        datetime.fromtimestamp(pending.stat().st_mtime), pending)


def _lineage(root, sid, state=None, limit=10):
    """이 세션과, /clear로 거슬러 올라가는 이전 세션들. 짧게 쓰고 지운 세션도 앞 세션의 기록을 잇게 한다."""
    state, out = load_state(root) if state is None else state, []
    while sid and sid not in out and len(out) < limit:
        out.append(sid)
        sid = (state.get(sid) or {}).get("from")
    return out or None


def _closing_session(root, data):
    """이 터미널의 세션들. /clear면 방금 닫힌 세션부터, 압축이면 지금 세션부터. 새로 시작하면 모른다."""
    cleared = root / DIR / ".cleared"
    closed = cleared.read_text().strip() if cleared.exists() else None
    cleared.unlink(missing_ok=True)
    source, sid = data.get("source"), data.get("session_id")
    if source == "compact":
        return _lineage(root, sid)
    if source != "clear" or not closed:
        return None
    if sid:
        state = load_state(root)
        state.setdefault(sid, {})["from"] = closed
        save_state(root, state)
    return _lineage(root, closed)


def on_session_start(data):
    if is_noninteractive(_entrypoint()):
        return
    ensure_worktree(data.get("cwd"))
    root = find_root(data.get("cwd"))
    if root is None:
        return
    output = {}
    try:
        _recover_pending(root)
        prune_logs(root, load_config(root), datetime.now())
        context = build_context(root, data.get("source"), datetime.now(), _closing_session(root, data))
        if context:
            output["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": context}
    except Exception as e:  # /clear에서 기록을 못 넣었다면 지금 알려야 한다
        log_error(root, e)
    secure = load_secure(root)
    notes = [new_errors(root)]
    if secure:
        notes.append(f"leneu-session 보안 모드 켜짐 (지정어 {len(secure['terms'])}개). 끄기: /leneu-session:leneu-secure off")
    if any(notes):
        output["systemMessage"] = "\n".join(n for n in notes if n)
    if output:
        print(json.dumps(output, ensure_ascii=False))


def on_precompact(data):
    """압축은 컨텍스트만 지우고 transcript는 남는다. 원본 위치만 적어 둔다."""
    root = find_root(data.get("cwd"))
    if root is None:
        return
    now = datetime.now()
    append_log(root, (
        f"\n## {now:%Y-%m-%d %H:%M} compact\n"
        f"**메모:** 컨텍스트 압축({data.get('trigger', '?')}). 압축 전 원본: {data.get('transcript_path', '?')}\n"
    ), now)


def on_pre_tool_use(data):
    root = find_root(data.get("cwd"))
    if root is None or data.get("tool_name") != "Write":
        return
    tool_input = data.get("tool_input") or {}
    if not is_allowed_write(root, tool_input.get("file_path")):
        return
    out = {"hookEventName": "PreToolUse", "permissionDecision": "allow", "permissionDecisionReason": "leneu-session 기록"}
    secure = load_secure(root)
    if secure and isinstance(tool_input.get("content"), str):
        content, hits = redact(tool_input["content"], secure)
        out["updatedInput"] = dict(tool_input, content=content)
        marks = list(dict.fromkeys(m.group() for m in CONFIDENTIAL.finditer(content)))
        if marks:  # 무엇을 가려야 할지 패턴으로는 모른다. 사람이 보고 정한다
            out["permissionDecision"] = "ask"
            out["permissionDecisionReason"] = (
                f"leneu-session 보안 모드: 기밀 표시({', '.join(marks)})가 있는 기록입니다. "
                f"자동으로 {hits}건 가렸습니다. 이대로 저장할까요?"
            )
    print(json.dumps({"hookSpecificOutput": out}, ensure_ascii=False))


def _project_dir(cwd):
    """켜고 끌 위치: 이미 있는 폴더, 없으면 가장 가까운 .git, 없으면 cwd."""
    start = Path(cwd or os.getcwd()).resolve()
    home = Path.home().resolve()
    for d in (start, *start.parents):
        if d == home:  # 홈의 폴더를 집어 들면 하위 전체가 켜진다
            break
        if (d / DIR).is_dir() or (d / ".git").exists():
            return d
    return start


HOME_REFUSAL = "홈 폴더에서는 쓰지 않습니다. 하위의 모든 폴더에 적용되기 때문입니다. 프로젝트 폴더에서 실행하세요."


def _manual_folder(cwd):
    """수동 명령(handoff, brief)용 폴더. 없으면 만들되 자동 기록은 켜지 않는다. 홈이면 None."""
    target = _project_dir(cwd)
    if target == Path.home().resolve():
        return None
    folder = target / DIR
    if not folder.is_dir():
        _create_folder(target)
        (folder / "disabled").touch()  # 인계만 요청한 것이다. 자동 기록은 /leneu-init으로 켠다
    return folder


def cli_brief_path(cwd):
    folder = _manual_folder(cwd)
    return str(folder / "project-brief.md") if folder else HOME_REFUSAL


def cli_init(cwd):
    target = _project_dir(cwd)
    folder = target / DIR
    if target == Path.home().resolve():
        return HOME_REFUSAL
    existed = folder.is_dir()
    disabled = (folder / "disabled").exists()
    _create_folder(target)
    if disabled:
        (folder / "disabled").unlink()
        (folder / ".state.json").unlink(missing_ok=True)  # 꺼져 있던 동안의 턴을 기록하지 않는다
        return f"leneu-session을 다시 켰습니다: {folder}\n기존 기록은 그대로이며 다음 턴부터 다시 기록합니다."
    if existed:
        return f"이미 켜져 있습니다: {folder}\n로그 파일 {len(_log_files(target))}개."
    return f"leneu-session을 켰습니다: {folder}\n훅은 다음 턴부터 기록합니다."


HANDOFF_TAG = re.compile(r"^handoff-\d{8}-\d{4}-(\w{8})\.md$")


def cli_handoff_path(cwd, now, session=None):
    """새 handoff 경로를 정해 준다. 시각을 모델이 짐작하지 않게 하고, 새 문서 자리를 비운다."""
    folder = _manual_folder(cwd)
    if folder is None:
        return HOME_REFUSAL
    handoffs = folder / "handoffs"
    handoffs.mkdir(exist_ok=True)
    old = sorted(handoffs.glob("handoff-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[MAX_HANDOFFS - 1:]
    for p in old:
        p.unlink()
    tag = f"-{session[:8]}" if session else ""  # 같은 폴더의 다른 터미널 것과 구분한다
    lines = [str(handoffs / f"handoff-{now:%Y%m%d-%H%M}{tag}.md")]
    if old:
        lines.append(f"오래된 handoff {len(old)}개를 지웠습니다.")
    if load_secure(folder.parent):
        lines.append(SECURE_WRITER_NOTE)
    return "\n".join(lines)


def cli_stop(cwd):
    folder = _project_dir(cwd) / DIR
    if not folder.is_dir():
        return "이 프로젝트에서는 켜져 있지 않습니다. 할 일이 없습니다."
    (folder / "disabled").touch()
    return (
        f"leneu-session을 껐습니다: {folder}\n기록은 그대로 남아 있습니다. "
        "다시 켜려면 /leneu-init, 완전히 지우려면 폴더를 직접 삭제하세요."
    )


SECURE_USAGE = ("사용법: /leneu-session:leneu-secure [on | off | add 단어, 단어 | remove 단어 | list]\n"
                "지정어는 쉼표로 나눕니다. 예: add 회사명, 고객사, 프로젝트 코드명")


def cli_secure(cwd, args):
    folder = _project_dir(cwd) / DIR
    if not folder.is_dir() or (folder / "disabled").exists():
        return "이 프로젝트에서 leneu-session이 켜져 있지 않습니다. 먼저 /leneu-init 으로 켜세요."
    root = folder.parent
    secure = load_secure(root, any_state=True)
    cmd, _, rest = (args or "").strip().partition(" ")
    words = [w.strip() for w in rest.split(",") if w.strip()]
    cmd = cmd.lower() or "on"
    if cmd == "on":
        secure["enabled"] = True
    elif cmd == "off":
        secure["enabled"] = False
    elif cmd == "add":
        secure["terms"] += [w for w in words if w.lower() not in {t.lower() for t in secure["terms"]}]
    elif cmd == "remove":
        drop = {w.lower() for w in words}
        secure["terms"] = [t for t in secure["terms"] if t.lower() not in drop]
    elif cmd not in ("list", "status"):
        return SECURE_USAGE
    (folder / "secure.json").write_text(json.dumps(secure, ensure_ascii=False, indent=1))

    terms = ", ".join(secure["terms"]) or "없음"
    if cmd == "on":
        return ("보안 모드를 켰습니다. 기록에서 이메일·전화번호·주민/카드/계좌번호·내부 IP와 주소·긴 토큰, "
                f"지정어를 가리고, 기밀 표시가 있는 체크포인트는 저장 전에 묻습니다.\n지정어: {terms}\n{SECURE_USAGE}")
    if cmd == "off":
        return f"보안 모드를 껐습니다. 다음 턴부터 평소대로 기록합니다. 지정어({terms})는 다시 켤 때를 위해 남겨 둡니다."
    return f"보안 모드: {'켜짐' if secure['enabled'] else '꺼짐'}\n지정어: {terms}"


HANDLERS = {
    "Stop": on_stop,
    "SessionStart": on_session_start,
    "PreToolUse": on_pre_tool_use,
    "PreCompact": on_precompact,
    "SessionEnd": on_session_end,
}


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
    assert cfg == {"context_window": 1000000, "clear_recommend_pct": 30, "checkpoint_every_pct": 5, "keep_log_days": 0}


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


CFG = {"context_window": 1000000, "clear_recommend_pct": 30, "checkpoint_every_pct": 10}


def _capture(fn, *args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args)
    return json.loads(buf.getvalue()) if buf.getvalue().strip() else None


def _stop(root, tokens, active=False, sid="s1", prompt="요청"):
    """토큰 수가 tokens인 transcript로 Stop 훅을 한 번 돌린다."""
    t = root / f"t-{sid}.jsonl"
    usage = {"input_tokens": tokens} if tokens else None
    n = sum(1 for _ in t.open()) if t.exists() else 0
    with t.open("a") as f:
        f.write(json.dumps(dict(_user(prompt), uuid=f"u{n}")) + "\n")
        f.write(json.dumps(dict(_assistant("답", usage=usage), uuid=f"a{n}")) + "\n")
    return _capture(on_stop, {"cwd": str(root), "session_id": sid, "transcript_path": str(t),
                              "stop_hook_active": active, "hook_event_name": "Stop"})


def _log_text(root):
    return "".join(p.read_text() for p in sorted((root / ".leneu-session" / "logs").glob("*.md")))


def test_decide_checkpoint_threshold():
    assert decide_checkpoint(CFG, {"last_ckpt": 47000}, 146999) == "none"
    assert decide_checkpoint(CFG, {"last_ckpt": 47000}, 147000) == "checkpoint"


def test_decide_checkpoint_recommends_clear_past_limit():
    assert decide_checkpoint(CFG, {"last_ckpt": 247000}, 347000) == "checkpoint+clear"
    assert decide_checkpoint(CFG, {"last_ckpt": 299000}, 299500) == "none"  # 넘어도 간격 전이면 조용히


def test_checkpoint_reason_contents():
    root = _project()
    r = checkpoint_reason(root, True)
    for h in ["현재 작업의 요구와 사용자 정정", "확인된 사실", "변경 파일과 검증 여부", "결정", "막다른 길", "완료 기준", "다음 액션"]:
        assert h in r, h
    assert r.index("완료 기준") < r.index("다음 액션")
    assert str(root / ".leneu-session" / ".pending-checkpoint.md") in r
    assert "/clear" in r and "비밀값" in r
    assert "/clear" not in checkpoint_reason(root, False)


def test_stop_first_measure_sets_baseline_without_block():
    root = _project()
    assert _stop(root, 47000) is None
    sess = load_state(root)["s1"]
    assert sess["baseline"] == 47000 and sess["last_ckpt"] == 47000


def test_stop_blocks_once_per_interval():
    root = _project()
    (root / ".leneu-session" / "config.json").write_text('{"checkpoint_every_pct": 10}')
    _stop(root, 47000)
    assert _stop(root, 146999) is None
    out = _stop(root, 147000)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "Stop" and "decision" not in out
    assert ".pending-checkpoint-s1.md" in ctx and "/clear" not in ctx
    assert _stop(root, 148000) is None  # 다음 턴에 또 재촉하지 않는다


def test_stop_recommends_clear_at_limit():
    root = _project()
    _stop(root, 250000)
    assert "/clear" in _stop(root, 350000)["hookSpecificOutput"]["additionalContext"]


def test_stop_hook_active_never_blocks():
    root = _project()
    _stop(root, 47000)
    assert _stop(root, 900000, active=True) is None
    assert load_state(root)["s1"]["last_ckpt"] == 47000


def test_stop_resets_after_compaction():
    root = _project()
    _stop(root, 300000)
    assert _stop(root, 50000) is None
    sess = load_state(root)["s1"]
    assert sess["baseline"] == 50000 and sess["last_ckpt"] == 50000


def test_stop_records_all_new_turns_once():
    root = _project()
    t = root / "t.jsonl"
    data = {"cwd": str(root), "session_id": "s", "transcript_path": str(t)}
    t.write_text(json.dumps(dict(_user("처음"), uuid="0")) + "\n")
    _capture(on_stop, data)  # 커서를 잡는다
    entries = [dict(_user("첫째"), uuid="1"), dict(_assistant("답1", usage=USAGE), uuid="2"),
               dict(_user("둘째"), uuid="3"), dict(_assistant("답2", usage=USAGE), uuid="4")]
    with t.open("a") as f:
        f.write("".join(json.dumps(e) + "\n" for e in entries))
    _capture(on_stop, data)
    _capture(on_stop, data)
    text = _log_text(root)
    assert text.count("첫째") == 1 and text.count("둘째") == 1


def test_stop_active_does_not_record_checkpoint_instruction():
    root = _project()
    _stop(root, 47000, prompt="기록됨")
    t = root / "t-s1.jsonl"
    hook = {"type": "attachment", "uuid": "h1", "attachment": {"type": "hook_additional_context", "content": ["체크포인트지시"]}}
    with t.open("a") as f:  # 실제로는 첨부로 들어온다 (2026-11-12 transcript 확인)
        f.write(json.dumps(hook) + "\n" + json.dumps(dict(_assistant("체크포인트 저장", usage=USAGE), uuid="a8")) + "\n")
    _capture(on_stop, {"cwd": str(root), "session_id": "s1", "transcript_path": str(t), "stop_hook_active": True})
    text = _log_text(root)
    assert "기록됨" in text and "체크포인트지시" not in text and "체크포인트 저장" not in text


def test_stop_without_usage_records_turn_without_block():
    root = _project()
    assert _stop(root, None, prompt="사용량없음") is None
    assert "사용량없음" in _log_text(root)


def test_stop_uninitialized_project_creates_nothing():
    d = Path(tempfile.mkdtemp()).resolve()
    (d / ".git").mkdir()
    assert _stop(d, 500000) is None
    assert not (d / ".leneu-session").exists()


def test_consume_pending_moves_to_log_once():
    root = _project()
    pending = root / ".leneu-session" / ".pending-checkpoint.md"
    pending.write_text("### 다음 액션\n1. 일\n")
    now = datetime(2026, 11, 9, 14, 15)
    assert consume_pending(root, 147000, "6f74a221-xxxx", "/tmp/t.jsonl", now) is True
    assert not pending.exists()
    assert consume_pending(root, 147000, "6f74a221-xxxx", "/tmp/t.jsonl", now) is False
    text = _log_text(root)
    assert "## 2026-11-09 14:15 checkpoint tokens=147000 session=6f74a221\n" in text
    assert "**원본:** /tmp/t.jsonl" in text and text.count("1. 일") == 1


def test_consume_pending_demotes_headers_and_masks():
    root = _project()
    pending = root / ".leneu-session" / ".pending-checkpoint.md"
    pending.write_text("## 제목\n# 큰제목\n### 유지\nsk-ant-abc123def456ghi\n")
    consume_pending(root, 1, "s", "t", datetime(2026, 11, 9))
    text = _log_text(root)
    body = text.split("**원본:** t\n")[1]
    assert "\n## " not in "\n" + body and "\n# " not in "\n" + body
    assert "### 제목" in body and "### 큰제목" in body and "### 유지" in body
    assert "sk-ant" not in body


def test_consume_pending_empty_is_discarded():
    root = _project()
    pending = root / ".leneu-session" / ".pending-checkpoint.md"
    pending.write_text("  \n")
    assert consume_pending(root, 1, "s", "t", datetime(2026, 11, 9)) is False
    assert not pending.exists() and _log_text(root) == ""


def test_stop_moves_leftover_pending_from_crash():
    root = _project()
    (root / ".leneu-session" / ".pending-checkpoint.md").write_text("### 다음 액션\n남은 것\n")
    _stop(root, 47000)
    assert _log_text(root).count("남은 것") == 1


def test_load_settled_waits_for_late_flush():
    p = Path(tempfile.mkdtemp()) / "t.jsonl"
    p.write_text(json.dumps(_user("요청")) + "\n")

    def flush():
        with p.open("a") as f:
            f.write(json.dumps(_assistant("늦은 답", usage=USAGE)) + "\n")

    threading.Timer(0.2, flush).start()
    assert context_tokens(load_settled(str(p), "늦은 답")) == 79000


def test_load_settled_gives_up_and_no_wait_without_message():
    p = Path(tempfile.mkdtemp()) / "t.jsonl"
    p.write_text(json.dumps(_user("요청")) + "\n")
    start = time.monotonic()
    assert len(load_settled(str(p), None)) == 1
    assert time.monotonic() - start < 0.2
    assert len(load_settled(str(p), "안 옴", wait=0.2)) == 1


def test_stop_late_flush_keeps_reply_without_duplicates():
    """Stop 시점에 응답이 아직 안 쓰였어도 결과가 남고, 다음 턴에 중복되지 않는다."""
    root = _project()
    t = root / "t.jsonl"
    meta = {"type": "last-prompt"}  # uuid 없는 항목이 끝에 와도 커서가 유지되어야 한다
    t.write_text(json.dumps(dict(_user("첫째"), uuid="1")) + "\n" + json.dumps(meta) + "\n")
    data = {"cwd": str(root), "session_id": "s", "transcript_path": str(t), "last_assistant_message": "첫 답"}
    old, globals()["FLUSH_WAIT"] = FLUSH_WAIT, 0.1
    try:
        _capture(on_stop, data)
        with t.open("a") as f:
            f.write(json.dumps(dict(_assistant("첫 답", usage=USAGE), uuid="2")) + "\n")
            f.write(json.dumps(dict(_user("둘째"), uuid="3")) + "\n")
            f.write(json.dumps(dict(_assistant("둘째 답", usage=USAGE), uuid="4")) + "\n")
        _capture(on_stop, dict(data, last_assistant_message="둘째 답"))
    finally:
        globals()["FLUSH_WAIT"] = old
    text = _log_text(root)
    assert text.count("**요청:** 첫째") == 1, text
    assert "**결과:** 첫 답" in text and "**요청:** 둘째" in text and "둘째 답" in text


def _with_entrypoint(value, fn):
    old = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
    os.environ["CLAUDE_CODE_ENTRYPOINT"] = value
    try:
        return fn()
    finally:
        if old is None:
            del os.environ["CLAUDE_CODE_ENTRYPOINT"]
        else:
            os.environ["CLAUDE_CODE_ENTRYPOINT"] = old


def test_is_noninteractive():
    assert is_noninteractive("sdk-cli") and is_noninteractive("sdk-ts")
    assert not is_noninteractive("cli") and not is_noninteractive(None)


def test_stop_noninteractive_records_turn_without_checkpoint():
    root = _project()
    _with_entrypoint("sdk-cli", lambda: _stop(root, 47000, prompt="헤드리스"))
    assert _with_entrypoint("sdk-cli", lambda: _stop(root, 500000)) is None
    assert "헤드리스" in _log_text(root)


def test_session_start_noninteractive_injects_nothing():
    root = _project()
    _write_log(root, "2026-11-09", CKPT)
    data = {"cwd": str(root), "source": "startup"}
    assert _with_entrypoint("sdk-cli", lambda: _capture(on_session_start, data)) is None


def test_on_precompact_records_without_affecting_lookup():
    root = _project()
    _write_log(root, "2026-11-09", CKPT)
    on_precompact({"cwd": str(root), "trigger": "auto", "transcript_path": "/tmp/t.jsonl"})
    text = _log_text(root)
    assert " compact" in text and "/tmp/t.jsonl" in text and "auto" in text
    assert "체크포인트 본문표식" in latest_checkpoint(root)[1]
    assert "compact" not in latest_checkpoint(root)[1]


def _worktree(main_enabled=True):
    main = Path(tempfile.mkdtemp()).resolve()
    (main / ".git" / "worktrees" / "w").mkdir(parents=True)
    (main / ".git" / "worktrees" / "w" / "commondir").write_text("../..\n")
    if main_enabled:
        (main / ".leneu-session" / "logs").mkdir(parents=True)
    wt = Path(tempfile.mkdtemp()).resolve()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/w\n")
    return main, wt


def test_ensure_worktree_enables_when_main_is_on():
    main, wt = _worktree()
    (main / ".leneu-session" / "config.json").write_text('{"context_window": 200000}')
    (wt / "sub").mkdir()
    assert ensure_worktree(str(wt / "sub")) == wt
    assert (wt / ".leneu-session" / ".gitignore").read_text() == "*\n"
    assert (wt / ".leneu-session" / "logs").is_dir()
    assert load_config(wt)["context_window"] == 200000
    assert find_root(str(wt)) == wt


def test_ensure_worktree_skips_when_main_off_or_disabled():
    _, wt = _worktree(main_enabled=False)
    assert ensure_worktree(str(wt)) is None and not (wt / ".leneu-session").exists()
    main, wt2 = _worktree()
    (main / ".leneu-session" / "disabled").touch()
    assert ensure_worktree(str(wt2)) is None and not (wt2 / ".leneu-session").exists()


def test_ensure_worktree_ignores_normal_repo_and_submodule():
    d = Path(tempfile.mkdtemp()).resolve()
    (d / ".git").mkdir()
    assert ensure_worktree(str(d)) is None
    sub = Path(tempfile.mkdtemp()).resolve()
    (sub / "modules" / "m").mkdir(parents=True)  # 서브모듈에는 commondir이 없다
    (d / "m").mkdir()
    (d / "m" / ".git").write_text(f"gitdir: {sub}/modules/m\n")
    assert ensure_worktree(str(d / "m")) is None


def test_ensure_worktree_keeps_existing_and_handles_broken_git_file():
    main, wt = _worktree()
    (wt / ".leneu-session").mkdir()
    (wt / ".leneu-session" / "disabled").touch()
    assert ensure_worktree(str(wt)) is None and find_root(str(wt)) is None
    d = Path(tempfile.mkdtemp()).resolve()
    (d / ".git").write_text("쓰레기\n")
    assert ensure_worktree(str(d)) is None


def test_session_start_autostarts_worktree():
    _, wt = _worktree()
    _capture(on_session_start, {"cwd": str(wt), "source": "startup"})
    assert (wt / ".leneu-session").is_dir()


def test_new_errors_reports_once():
    root = _project()
    assert new_errors(root) is None
    log_error(root, ValueError("하나"))
    log_error(root, ValueError("둘"))
    msg = new_errors(root)
    assert "2" in msg and "둘" in msg and "hook-errors" in msg
    assert new_errors(root) is None
    log_error(root, ValueError("셋"))
    assert "셋" in new_errors(root)
    assert "하나" in (root / ".leneu-session" / "hook-errors.old.log").read_text()


def test_session_start_shows_errors_to_user():
    root = _project()
    log_error(root, ValueError("터짐"))
    out = _capture(on_session_start, {"cwd": str(root), "source": "resume"})
    assert "터짐" in out["systemMessage"] and "hookSpecificOutput" not in out
    assert _capture(on_session_start, {"cwd": str(root), "source": "resume"}) is None


def _handoff(root, name, body, when):
    d = root / ".leneu-session" / "handoffs"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(body)
    os.utime(p, (when.timestamp(), when.timestamp()))
    return p


def test_latest_handoff_uses_newest_mtime():
    root = _project()
    assert latest_handoff(root) is None
    _handoff(root, "handoff-1.md", "옛것", datetime(2026, 11, 9, 10, 0, 30))
    new = _handoff(root, "handoff-2.md", "새것", datetime(2026, 11, 9, 11, 0, 30))
    (root / ".leneu-session" / "handoffs" / "other.md").write_text("무관")
    assert latest_handoff(root) == (datetime(2026, 11, 9, 11, 0), new)


def test_build_context_prefers_newer_handoff_with_later_turns():
    root = _project()
    _write_log(root, "2026-11-09", CKPT + TURN)
    _handoff(root, "handoff-1.md", "인계 본문표식", datetime(2026, 11, 9, 14, 30, 40))
    c = build_context(root, "clear", NOW)
    assert "인계 본문표식" in c and "체크포인트 본문표식" not in c
    assert "이후 턴표식" in c  # 같은 분의 턴을 잃지 않는다


def test_build_context_prefers_newer_checkpoint_over_old_handoff():
    root = _project()
    _write_log(root, "2026-11-09", CKPT)
    _handoff(root, "handoff-1.md", "인계 본문표식", datetime(2026, 11, 9, 9, 0))
    c = build_context(root, "clear", NOW)
    assert "체크포인트 본문표식" in c and "인계 본문표식" not in c


def test_build_context_compact_points_to_handoff_when_newest():
    root = _project()
    _write_log(root, "2026-11-09", CKPT)
    _handoff(root, "handoff-1.md", "인계 본문표식", datetime(2026, 11, 9, 14, 40))
    c = build_context(root, "compact", NOW)
    assert "handoff-1.md" in c and "인계 본문표식" in c


def test_stop_new_handoff_resets_interval_without_checkpoint():
    root = _project()
    (root / ".leneu-session" / "config.json").write_text('{"checkpoint_every_pct": 10}')
    _handoff(root, "handoff-0.md", "세션 전", datetime(2026, 11, 8))
    _stop(root, 47000)
    _handoff(root, "handoff-1.md", "수동", datetime(2026, 11, 9, 14, 40))
    assert _stop(root, 150000) is None
    assert load_state(root)["s1"]["last_ckpt"] == 150000
    assert _stop(root, 200000) is None
    assert _stop(root, 250000) is not None


def test_stop_old_handoff_does_not_suppress_checkpoint():
    root = _project()
    _handoff(root, "handoff-0.md", "세션 전", datetime(2026, 11, 8))
    _stop(root, 47000)
    assert _stop(root, 150000) is not None


def test_is_allowed_write_handoffs_only_direct_children():
    root = _project()
    d = root / ".leneu-session/handoffs"
    assert is_allowed_write(root, str(d / "handoff-20261109-1430.md"))
    assert not is_allowed_write(root, str(d / "other.md"))
    assert not is_allowed_write(root, str(d / "sub/handoff-1.md"))
    assert not is_allowed_write(root, str(d / "../handoff-1.md"))
    assert not is_allowed_write(root, str(d / "handoff-1.txt"))


def test_cli_handoff_path_prunes_to_make_room():
    root = _project()
    for i in range(12):
        _handoff(root, f"handoff-{i:02}.md", "x", datetime(2026, 11, 1 + i))
    out = cli_handoff_path(str(root), datetime(2026, 11, 20, 14, 30))
    assert str(root / ".leneu-session/handoffs/handoff-20261120-1430.md") in out
    assert "3" in out  # 지운 개수를 알린다
    left = {p.name for p in (root / ".leneu-session/handoffs").iterdir()}
    assert len(left) == 9 and "handoff-11.md" in left and "handoff-00.md" not in left


def test_cli_handoff_path_uninitialized_keeps_auto_off():
    d = Path(tempfile.mkdtemp()).resolve()
    (d / ".git").mkdir()
    out = cli_handoff_path(str(d), datetime(2026, 11, 20, 14, 30))
    assert "handoff-20261120-1430.md" in out
    assert (d / ".leneu-session" / ".gitignore").exists()
    assert (d / ".leneu-session" / "disabled").exists()  # 자동 기록은 동의 없이 켜지 않는다
    assert find_root(str(d)) is None


def _write_log(root, date, text):
    (root / ".leneu-session" / "logs" / f"{date}.md").write_text(text)


CKPT = "\n## 2026-11-09 14:15 checkpoint tokens=1 session=s\n**원본:** t\n### 다음 액션\n체크포인트 본문표식\n"
TURN = "\n## 2026-11-09 14:30 turn\n**요청:** 이후 턴표식\n"
NOW = datetime(2026, 11, 9, 15, 0)


def test_latest_checkpoint_stops_at_next_header():
    root = _project()
    _write_log(root, "2026-11-09", CKPT + TURN)
    dt, body, path = latest_checkpoint(root)
    assert dt == datetime(2026, 11, 9, 14, 15)
    assert "체크포인트 본문표식" in body and "이후 턴표식" not in body
    assert path.name == "2026-11-09.md"


def test_latest_checkpoint_picks_last_and_searches_older_files():
    root = _project()
    _write_log(root, "2026-11-08", CKPT.replace("2026-11-09", "2026-11-08").replace("본문표식", "어제"))
    _write_log(root, "2026-11-09", TURN)
    assert "어제" in latest_checkpoint(root)[1]
    _write_log(root, "2026-11-09", CKPT + CKPT.replace("본문표식", "나중"))
    assert "나중" in latest_checkpoint(root)[1]
    assert latest_checkpoint(_project()) is None


def test_turns_after_crosses_midnight_and_excludes_others():
    root = _project()
    _write_log(root, "2026-11-08", "\n## 2026-11-08 23:00 turn\n오래됨\n\n## 2026-11-08 23:50 turn\n어젯밤\n")
    compact = "\n## 2026-11-09 00:20 compact\n압축표식\n"
    _write_log(root, "2026-11-09", "\n## 2026-11-09 00:10 turn\n오늘\n" + compact)
    out = turns_after(root, datetime(2026, 11, 8, 23, 30))
    assert "어젯밤" in out and "오늘" in out
    assert "오래됨" not in out and "압축표식" not in out


def test_build_context_clear_injects_checkpoint_and_later_turns():
    root = _project()
    _write_log(root, "2026-11-09", CKPT + TURN)
    c = build_context(root, "clear", NOW)
    assert "체크포인트 본문표식" in c and "이후 턴표식" in c
    assert "확인" in c and "무시" in c and "." in c


def test_build_context_compact_restores_checkpoint_without_briefing():
    root = _project()
    _write_log(root, "2026-11-09", CKPT + TURN)
    c = build_context(root, "compact", NOW)
    assert "체크포인트 본문표식" in c and "이후 턴표식" in c and "2026-11-09.md" in c
    assert "계속" in c and "브리핑" in c and "." not in c.split("\n")[0][:20]


def test_build_context_resume_and_fork_inject_nothing():
    root = _project()
    _write_log(root, "2026-11-09", CKPT)
    assert build_context(root, "resume", NOW) is None
    assert build_context(root, "fork", NOW) is None


def test_build_context_startup_asks_only_when_old():
    root = _project()
    _write_log(root, "2026-11-09", CKPT)
    fresh = build_context(root, "startup", datetime(2026, 11, 9, 15, 15))
    old = build_context(root, "startup", datetime(2026, 11, 10, 15, 16))
    assert "체크포인트 본문표식" in fresh and "물어" not in fresh
    assert "체크포인트 본문표식" in old and "물어" in old


def test_build_context_empty_is_none():
    assert build_context(_project(), "clear", NOW) is None
    assert build_context(_project(), "compact", NOW) is None


def test_build_context_turns_only():
    root = _project()
    _write_log(root, "2026-11-09", TURN)
    assert "이후 턴표식" in build_context(root, "clear", NOW)
    assert build_context(root, "compact", NOW) is None


def test_build_context_clips_turns_before_checkpoint():
    root = _project()
    many = "".join(f"\n## 2026-11-09 14:30 turn\n{'가' * 500} 턴{i}\n" for i in range(40))
    _write_log(root, "2026-11-09", CKPT + many)
    c = build_context(root, "clear", NOW)
    assert len(c) <= 8000 + 1000
    assert "체크포인트 본문표식" in c and "턴39" in c and "턴0\n" not in c
    assert "2026-11-09.md" in c


def test_is_allowed_write_only_pending():
    root = _project()
    assert is_allowed_write(root, str(root / ".leneu-session/.pending-checkpoint.md"))
    assert not is_allowed_write(root, str(root / ".leneu-session/logs/2026-11-09.md"))
    assert not is_allowed_write(root, str(root / ".leneu-session/../evil.md"))
    assert not is_allowed_write(root, str(root / ".leneu-session/logs/../../evil.md"))
    assert not is_allowed_write(root, "/etc/passwd")
    assert not is_allowed_write(root, "")


def test_is_allowed_write_rejects_symlink_escape():
    root = _project()
    target = root / "outside.md"
    (root / ".leneu-session" / ".pending-checkpoint.md").symlink_to(target)
    assert not is_allowed_write(root, str(root / ".leneu-session/.pending-checkpoint.md"))


def test_on_pre_tool_use_allows_and_stays_silent_otherwise():
    root = _project()
    data = {"cwd": str(root), "tool_name": "Write",
            "tool_input": {"file_path": str(root / ".leneu-session/.pending-checkpoint.md")}}
    out = _capture(on_pre_tool_use, data)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert _capture(on_pre_tool_use, dict(data, tool_input={"file_path": "/etc/passwd"})) is None
    assert _capture(on_pre_tool_use, dict(data, tool_name="Edit")) is None


def test_on_session_start_outputs_additional_context():
    root = _project()
    _write_log(root, "2026-11-09", CKPT)
    out = _capture(on_session_start, {"cwd": str(root), "source": "clear"})
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "체크포인트 본문표식" in out["hookSpecificOutput"]["additionalContext"]
    assert _capture(on_session_start, {"cwd": str(root), "source": "resume"}) is None


def test_cli_init_creates_gitignore_and_reports_when_repeated():
    d = Path(tempfile.mkdtemp()).resolve()
    (d / ".git").mkdir()
    (d / "sub").mkdir()
    msg = cli_init(str(d / "sub"))
    assert (d / ".leneu-session" / ".gitignore").read_text() == "*\n"
    assert (d / ".leneu-session" / "logs").is_dir()
    assert not (d / "sub" / ".leneu-session").exists()
    assert "다음 턴" in msg
    assert "이미 켜져" in cli_init(str(d))


def test_cli_stop_keeps_logs_and_init_reenables():
    root = _project()
    _write_log(root, "2026-11-09", TURN)
    assert "/leneu-init" in cli_stop(str(root))
    assert (root / ".leneu-session" / "disabled").exists()
    assert "이후 턴표식" in _log_text(root)
    assert _stop(root, 47000, prompt="꺼짐표식") is None
    assert "꺼짐표식" not in _log_text(root)
    assert "다시" in cli_init(str(root))
    assert find_root(str(root)) == root
    assert "이후 턴표식" in _log_text(root)


def test_cli_init_refuses_home():
    home = Path(tempfile.mkdtemp()).resolve()
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        assert "프로젝트 폴더" in cli_init(str(home))
        assert not (home / ".leneu-session").exists()
    finally:
        os.environ["HOME"] = old


def test_cli_stop_without_folder_creates_nothing():
    d = Path(tempfile.mkdtemp()).resolve()
    (d / ".git").mkdir()
    cli_stop(str(d))
    assert not (d / ".leneu-session").exists()


def test_decide_checkpoint_recommends_as_soon_as_limit_is_crossed():
    assert decide_checkpoint(CFG, {"last_ckpt": 280000}, 300000) == "checkpoint+clear"
    assert decide_checkpoint(CFG, {"last_ckpt": 300000, "recommended": True}, 350000) == "none"
    assert decide_checkpoint(CFG, {"last_ckpt": 300000, "recommended": True}, 400000) == "checkpoint+clear"


def test_stop_recommends_on_first_turn_past_limit_then_at_intervals():
    root = _project()
    _stop(root, 250000)
    assert "/clear" in _stop(root, 305000)["hookSpecificOutput"]["additionalContext"]
    assert _stop(root, 350000) is None  # 다음 턴에 또 재촉하지 않는다
    assert "/clear" in _stop(root, 405000)["hookSpecificOutput"]["additionalContext"]


def test_stop_first_measure_past_limit_recommends():
    root = _project()  # 세션 중간에 켰거나 첫 턴이 큰 경우
    assert "/clear" in _stop(root, 400000)["hookSpecificOutput"]["additionalContext"]


def test_stop_recommends_again_after_compaction():
    root = _project()
    _stop(root, 305000)
    assert _stop(root, 50000) is None
    assert "/clear" in _stop(root, 301000)["hookSpecificOutput"]["additionalContext"]


def test_checkpoint_reason_is_short():
    r = checkpoint_reason(_project(), True)
    assert len(r.splitlines()) <= 3 and len(r) < 700, (len(r.splitlines()), len(r))


def test_session_end_logs_interrupted_turn_once():
    root = _project()
    _stop(root, 47000, prompt="앞요청")
    t = root / "t-s1.jsonl"
    _append(t, dict(_user("중단된요청"), uuid="x1"), dict(_assistant("하다만답"), uuid="x2"),
            dict(_user("[Request interrupted by user]"), uuid="x3"))
    on_session_end({"cwd": str(root), "session_id": "s1", "transcript_path": str(t), "reason": "clear"})
    on_session_end({"cwd": str(root), "session_id": "s1", "transcript_path": str(t), "reason": "clear"})
    text = _log_text(root)
    assert text.count("중단된요청") == 1 and "하다만답" in text and "interrupted" not in text
    _stop(root, 47000, prompt="뒤요청")
    assert _log_text(root).count("중단된요청") == 1


def test_parse_turns_skips_interrupt_marker():
    turns = parse_turns([_user("요청"), _assistant("일부"), _user("[Request interrupted by user for tool use]")])
    assert [t["prompt"] for t in turns] == ["요청"]


def _append(t, *entries, **kw):
    with t.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, **kw) + "\n")


def test_stop_survives_malformed_entries_without_repeating():
    malformed = [
        {"type": "user", "uuid": "b", "message": None},
        {"type": "assistant", "uuid": "b", "message": "문자열"},
        {"type": "assistant", "uuid": "b", "message": {"usage": 5, "content": []}},
        {"type": "assistant", "uuid": "b", "message": {"content": [{"type": "text", "text": None}]}},
        {"type": "assistant", "uuid": "b", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": "x"}]}},
        {"type": "assistant", "uuid": "b", "message": {"content": [{"type": "tool_use", "name": None, "input": {"file_path": 3}}]}},
    ]
    for bad in malformed:
        root = _project()
        _stop(root, 47000, prompt="앞요청")
        _append(root / "t-s1.jsonl", bad)
        _stop(root, 47000, prompt="뒤요청")
        _stop(root, 47000, prompt="끝요청")
        text = _log_text(root)
        assert all(text.count(p) == 1 for p in ("앞요청", "뒤요청", "끝요청")), (bad, text)


def test_stop_survives_lone_surrogate():
    root = _project()
    _stop(root, 47000, prompt="이모지\ud83d끝")
    _stop(root, 47000, prompt="다음")
    text = _log_text(root)
    assert text.count("이모지") == 1 and "다음" in text and "cursor" in load_state(root)["s1"]


def test_stop_saves_cursor_when_logging_fails():
    root = _project()
    _stop(root, 47000, prompt="먼저")
    old, globals()["append_log"] = append_log, lambda *a: (_ for _ in ()).throw(OSError("디스크"))
    try:
        _stop(root, 47000, prompt="실패턴")
        assert False, "오류는 main까지 올라가 기록되어야 한다"
    except OSError:
        pass
    finally:
        globals()["append_log"] = old
    _stop(root, 47000, prompt="회복")
    text = _log_text(root)
    assert text.count("먼저") == 1 and text.count("회복") == 1 and "실패턴" not in text


def test_stop_recovers_from_wrong_shape_state():
    for broken in ('{"s1": 5}', '{"s1": {"last_ckpt": "x"}}', '{"s1": []}'):
        root = _project()
        (root / ".leneu-session" / ".state.json").write_text(broken)
        assert _stop(root, 47000) is None
        assert load_state(root)["s1"]["last_ckpt"] == 47000


def test_load_transcript_keeps_unicode_line_separators():
    p = Path(tempfile.mkdtemp()) / "t.jsonl"
    _append(p, _user("앞 뒤"), _user("가\u0085나"), ensure_ascii=False)
    assert [e["message"]["content"] for e in load_transcript(str(p))] == ["앞 뒤", "가\u0085나"]


def test_stop_first_sight_logs_only_last_turn():
    root = _project()
    t = root / "t.jsonl"
    _append(t, dict(_user("예전요청"), uuid="1"), dict(_assistant("답", usage=USAGE), uuid="2"),
            dict(_user("지금요청"), uuid="3"), dict(_assistant("답", usage=USAGE), uuid="4"))
    _capture(on_stop, {"cwd": str(root), "session_id": "s", "transcript_path": str(t)})
    text = _log_text(root)
    assert "지금요청" in text and "예전요청" not in text


def test_stop_broken_state_does_not_relog_earlier_turns():
    root = _project()
    _stop(root, 47000, prompt="기록된요청")
    (root / ".leneu-session" / ".state.json").write_text("{broken")
    _stop(root, 47000, prompt="새요청")
    text = _log_text(root)
    assert text.count("기록된요청") == 1 and text.count("새요청") == 1


def test_reenable_does_not_log_turns_from_while_off():
    root = _project()
    _stop(root, 47000, prompt="켜진때")
    cli_stop(str(root))
    _stop(root, 47000, prompt="비공개요청")
    cli_init(str(root))
    _stop(root, 47000, prompt="다시켠뒤")
    text = _log_text(root)
    assert "비공개요청" not in text and "다시켠뒤" in text and text.count("켜진때") == 1


def test_parse_turns_records_slash_commands():
    cmd = "<command-message>deploy</command-message>\n<command-name>/deploy</command-name>\n<command-args>production now</command-args>"
    turns = parse_turns([_user(cmd), _user("펼친 본문", isMeta=True), _assistant("배포함")])
    assert [(t["prompt"], t["reply"]) for t in turns] == [("/deploy production now", "배포함")]


def test_parse_turns_drops_local_commands_without_reply():
    turns = parse_turns([
        _user("<command-name>/clear</command-name>\n<command-message>clear</command-message>\n<command-args></command-args>"),
        _user("<local-command-stdout></local-command-stdout>"),
        _user("."), _assistant("이어감"),
    ])
    assert [t["prompt"] for t in turns] == ["."]


def test_parse_turns_keeps_quoted_command_tag_in_real_prompt():
    turns = parse_turns([_user("이 태그 <command-name>/x</command-name> 왜 생겨?")])
    assert len(turns) == 1 and "왜 생겨" in turns[0]["prompt"]


def test_session_start_recovers_unmoved_pending():
    root = _project()
    _write_log(root, "2026-11-09", CKPT)
    pending = root / ".leneu-session" / ".pending-checkpoint.md"
    pending.write_text("### 다음 액션\n미이동표식\n")
    when = datetime(2026, 11, 9, 14, 40, 30)
    os.utime(pending, (when.timestamp(), when.timestamp()))
    out = _capture(on_session_start, {"cwd": str(root), "source": "clear"})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "미이동표식" in ctx and "체크포인트 본문표식" not in ctx
    assert not pending.exists()
    text = _log_text(root)
    assert "## 2026-11-09 14:40 checkpoint" in text and "None" not in text


def test_bad_log_header_is_skipped():
    root = _project()
    _write_log(root, "2026-11-09", CKPT + "\n## 2026-13-45 10:00 turn\n깨진헤더\n")
    assert "체크포인트 본문표식" in build_context(root, "clear", NOW)


def test_session_start_reports_failure_immediately():
    root = _project()
    log_error(root, ValueError("예전오류"))
    old, globals()["build_context"] = build_context, lambda *a: 1 / 0
    try:
        out = _capture(on_session_start, {"cwd": str(root), "source": "clear"})
    finally:
        globals()["build_context"] = old
    assert "2" in out["systemMessage"] and "ZeroDivisionError" in out["systemMessage"]


def test_build_context_long_checkpoint_keeps_next_action():
    root = _project()
    body = "앞머리표식 " + "가" * 9000 + "\n### 다음 액션\n꼬리표식\n"
    _write_log(root, "2026-11-09", CKPT.replace("### 다음 액션\n체크포인트 본문표식\n", body) + TURN)
    c = build_context(root, "clear", NOW)
    assert "앞머리표식" in c and "꼬리표식" in c and "중략" in c and "이후 턴표식" in c
    assert len(c) <= 8000 + 1000
    assert "6000자" in checkpoint_reason(root, False)


def test_render_turn_keeps_end_of_reply():
    out = render_turn({"prompt": "p", "reply": "서두 " * 400 + "최종결과", "tools": [], "files": []}, NOW)
    res = next(l for l in out.splitlines() if l.startswith("**결과:**"))
    assert res.endswith("최종결과") and len(res) <= 610


# ---- 백그라운드 작업 대기

def _launch(tid, text="Async agent launched successfully.\nagentId: x"):
    """text가 리스트면 블록 목록으로, 문자열이면 그대로 결과 내용으로 쓴다 (실제 기록에 둘 다 있다)."""
    return {"type": "user", "uuid": "L" + tid,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}}


def _notify(tid, status="completed"):
    prompt = f"<task-notification>\n<task-id>x</task-id>\n<tool-use-id>{tid}</tool-use-id>\n<status>{status}</status>\n</task-notification>"
    return {"type": "attachment", "uuid": "N" + tid, "attachment": {"type": "queued_command", "prompt": prompt}}


def test_running_tasks_matches_launch_and_notification():
    entries = [_launch("t1"), _launch("t2"), _notify("t1"), _launch("t3", "일반 결과"),
               _launch("b1", "Command running in background with ID: b1."),  # 개발 서버 등은 기다리지 않는다
               _launch("t4", [{"type": "text", "text": "Async agent launched successfully."}])]
    assert running_tasks(entries) == {"t2", "t4"}
    assert running_tasks(entries + [_notify("t2", "failed"), _notify("t4", "killed")]) == set()


def _stop_with(root, tokens, extra=(), sid="s1"):
    t = root / f"t-{sid}.jsonl"
    with t.open("a") as f:
        for e in extra:
            f.write(json.dumps(e) + "\n")
    return _stop(root, tokens, sid=sid)


def test_stop_defers_checkpoint_until_background_tasks_finish():
    root = _project()
    _stop(root, 250000)
    assert _stop_with(root, 350000, [_launch("t1")]) is None  # 서브에이전트가 도는 중
    assert "recommended" not in load_state(root)["s1"]
    ctx = _stop_with(root, 351000, [_notify("t1")])["hookSpecificOutput"]["additionalContext"]
    assert "/clear" in ctx


def test_stop_stops_waiting_for_tasks_that_never_report():
    root = _project()
    _stop(root, 47000)
    _stop_with(root, 200000, [_launch("t1")])
    outs = [_stop(root, 200000 + i) for i in range(MAX_DEFER)]
    assert outs[-1] is not None and all(o is None for o in outs[:-1])


def test_checkpoint_reason_asks_for_subagent_results():
    assert "서브에이전트 결과" in checkpoint_reason(_project(), False)


# ---- 턴 도중 입력

def _queued(text):
    return {"type": "attachment", "uuid": "q" + text[:6], "attachment": {"type": "queued_command", "prompt": text}}


def test_parse_turns_records_prompts_typed_mid_turn():
    turns = parse_turns([_user("첫 요청"), _assistant("작업 중"), _queued("이것도 같이 해줘"),
                         _notify("t1"), _assistant("둘 다 함")])
    assert [t["prompt"] for t in turns] == ["첫 요청", "이것도 같이 해줘"]
    assert turns[1]["reply"] == "둘 다 함"


def test_stop_records_prompt_typed_during_checkpoint():
    root = _project()
    _stop(root, 47000, prompt="앞요청")
    t = root / "t-s1.jsonl"
    with t.open("a") as f:
        f.write(json.dumps(dict(_queued("체크포인트중입력"), uuid="q1")) + "\n")
        f.write(json.dumps(dict(_assistant("체크포인트 저장", usage=USAGE), uuid="a9")) + "\n")
    _capture(on_stop, {"cwd": str(root), "session_id": "s1", "transcript_path": str(t), "stop_hook_active": True})
    _stop(root, 47000, prompt="뒷요청")
    text = _log_text(root)
    assert text.count("체크포인트중입력") == 1 and text.count("앞요청") == 1


# ---- 로그 정리

def test_session_start_deletes_logs_older_than_keep_days():
    root = _project()
    for d in ["2026-08-01", "2026-10-20", "2026-11-09"]:
        _write_log(root, d, TURN)
    _capture(on_session_start, {"cwd": str(root), "source": "startup"})
    assert len(_log_files(root)) == 3  # 기본은 지우지 않는다
    (root / ".leneu-session" / "config.json").write_text('{"keep_log_days": 30}')
    assert load_config(root)["keep_log_days"] == 30
    prune_logs(root, load_config(root), NOW)
    assert [p.stem for p in _log_files(root)] == ["2026-10-20", "2026-11-09"]


# ---- 작은 결함

def _fake_home(fn):
    home = _project().parent / ("home" + os.urandom(3).hex())
    home.mkdir()
    old = os.environ["HOME"]
    os.environ["HOME"] = str(home)
    try:
        return fn(home)
    finally:
        os.environ["HOME"] = old


def test_handoff_and_init_never_enable_home():
    def run(home):
        out = cli_handoff_path(str(home), datetime(2026, 11, 20))
        assert not (home / ".leneu-session").exists() and "홈" in out
        (home / ".leneu-session").mkdir()  # 예전 버전이 만들어 둔 폴더
        assert find_root(str(home)) is None  # 꺼짐 표시가 없어도 홈은 기록하지 않는다
        (home / ".leneu-session" / "disabled").touch()
        (home / "proj").mkdir()
        assert "홈" in cli_init(str(home)) and "홈" in cli_brief_path(str(home))
        cli_init(str(home / "proj"))  # 홈이 아니라 그 폴더에서 켜진다
        assert (home / ".leneu-session" / "disabled").exists() and find_root(str(home / "proj")) == home / "proj"
    _fake_home(run)


def test_brief_path_points_to_project_root_from_subfolder():
    d = Path(tempfile.mkdtemp()).resolve()
    (d / ".git").mkdir()
    (d / "src" / "deep").mkdir(parents=True)
    assert cli_brief_path(str(d / "src" / "deep")) == str(d / ".leneu-session" / "project-brief.md")
    assert (d / ".leneu-session" / "disabled").exists() and not (d / "src" / ".leneu-session").exists()


def test_consume_pending_keeps_code_blocks():
    root = _project()
    pending = root / ".leneu-session" / ".pending-checkpoint.md"
    pending.write_text("## 제목\n```bash\n# 설치\n## 2026-11-09 10:00 turn\n```\n# 밖\n")
    consume_pending(root, 1, "s", "t", datetime(2026, 11, 9))
    body = _log_text(root).split("**원본:** t\n")[1]
    assert "### 제목" in body and "\n# 설치\n" in body and "### 밖" in body
    assert len(list(_sections(_log_text(root)))) == 1  # 코드 블록 속 가짜 헤더는 절을 만들지 않는다


# ---- 보안 모드

def _secure(root, terms=()):
    (root / ".leneu-session" / "secure.json").write_text(json.dumps({"enabled": True, "terms": list(terms)}))
    return load_secure(root)


def test_redact_hides_personal_and_internal_values():
    s = {"enabled": True, "terms": ["ACME", "프로젝트 블루"]}
    cases = {
        "kim.lee@acme-corp.co.kr": "[이메일]", "010-1234-5678": "[전화번호]", "+82 10-1234-5678": "[전화번호]",
        "900101-1234567": "[주민번호]", "1234-5678-9012-3456": "[카드번호]", "110-123-456789": "[계좌번호]",
        "192.168.0.12": "[내부IP]", "10.2.3.4": "[내부IP]", "https://wiki.corp/x/y": "[내부주소]",
        "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY": "[토큰]", "sk-ant-abc123def456ghi": "[비밀값 가림]",
    }
    for raw, label in cases.items():
        out, n = redact(f"값은 {raw} 입니다", s)
        assert raw not in out and label in out and n == 1, (raw, out)
    out, n = redact("acme와 프로젝트 블루, 다시 ACME", s)
    assert out == "[지정어-1]와 [지정어-2], 다시 [지정어-1]" and n == 3


def test_redact_keeps_ordinary_text():
    s = {"enabled": True, "terms": []}
    text = ("2026-11-12 14:30 v0.2.0 0c1ab1c 커밋, 6f74a221-d734-482d-ae53-0900f6f71336, "
            "/Users/me/proj/scripts/session_hook.py:42, 127.0.0.1:8080, 103/103, https://github.com/a/b, "
            ".claude/settings.local.json, CLAUDE_CODE_ENTRYPOINT")
    assert redact(text, s) == (text, 0)
    assert redact("kim@x.com", None) == ("kim@x.com", 0)  # 보안 모드가 아니면 비밀값만 가린다


def test_secure_mode_masks_turn_log_and_checkpoint():
    root = _project()
    _secure(root, ["ACME"])
    _stop(root, 47000, prompt="ACME 담당자 kim@acme.io 에게 보낼 문서")
    (root / ".leneu-session" / ".pending-checkpoint.md").write_text("### 사실\nACME 계약 010-1111-2222\n")
    _stop(root, 47000)
    text = _log_text(root)
    assert "ACME" not in text and "kim@acme.io" not in text and "010-1111-2222" not in text
    assert "[지정어-1]" in text and "**보안:**" in text


def test_secure_mode_write_masks_input_and_asks_on_confidential_marker():
    root = _project()
    _secure(root, ["ACME"])
    path = str(root / ".leneu-session" / ".pending-checkpoint.md")
    data = {"cwd": str(root), "tool_name": "Write", "tool_input": {"file_path": path, "content": "ACME 일정"}}
    out = _capture(on_pre_tool_use, data)["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"] == {"file_path": path, "content": "[지정어-1] 일정"}
    data["tool_input"]["content"] = "대외비 자료 요약"
    out = _capture(on_pre_tool_use, data)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask" and "대외비" in out["permissionDecisionReason"]


def test_normal_mode_write_is_allowed_unchanged():
    root = _project()
    path = str(root / ".leneu-session" / ".pending-checkpoint.md")
    out = _capture(on_pre_tool_use, {"cwd": str(root), "tool_name": "Write",
                                     "tool_input": {"file_path": path, "content": "대외비 kim@x.io"}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow" and "updatedInput" not in out["hookSpecificOutput"]


def test_secure_checkpoint_reason_says_point_dont_copy():
    root = _project()
    assert "보안 모드" not in checkpoint_reason(root, True)
    _secure(root)
    r = checkpoint_reason(root, True)
    assert "보안 모드" in r and len(r) < 800


def test_secure_mode_tells_writer_masking_is_automatic():
    root = _project()
    assert "다시 고치지" not in cli_handoff_path(str(root), NOW)
    _secure(root, ["ACME"])
    assert "다시 고치지" in cli_handoff_path(str(root), NOW) and "다시 고치지" in checkpoint_reason(root, False)


def test_cli_secure_toggles_and_manages_terms():
    root = _project()
    assert "켰습니다" in cli_secure(str(root), "")
    assert load_secure(root) == {"enabled": True, "terms": []}
    cli_secure(str(root), "add ACME, 프로젝트 블루")
    cli_secure(str(root), "add 홍길동")
    assert load_secure(root)["terms"] == ["ACME", "프로젝트 블루", "홍길동"]
    cli_secure(str(root), "remove 프로젝트 블루")
    assert "ACME" in cli_secure(str(root), "list") and load_secure(root)["terms"] == ["ACME", "홍길동"]
    assert "껐습니다" in cli_secure(str(root), "off") and load_secure(root) is None
    assert load_secure(root, any_state=True)["terms"] == ["ACME", "홍길동"]  # 꺼도 목록은 남는다
    d = Path(tempfile.mkdtemp()).resolve()
    assert "/leneu-init" in cli_secure(str(d), "")


def test_session_start_reminds_secure_mode():
    root = _project()
    _secure(root, ["A", "B"])
    out = _capture(on_session_start, {"cwd": str(root), "source": "startup"})
    assert "보안 모드" in out["systemMessage"] and "2" in out["systemMessage"]


def test_ensure_worktree_copies_secure_settings():
    main, wt = _worktree()
    (main / ".leneu-session" / "secure.json").write_text('{"enabled": true, "terms": ["X"]}')
    ensure_worktree(str(wt))
    assert load_secure(wt) == {"enabled": True, "terms": ["X"]}


# ---- 같은 폴더의 동시 세션

def _two_terminals(root):
    _write_log(root, "2026-11-09", (
        "\n## 2026-11-09 14:10 checkpoint tokens=1 session=aaaa1111\n### 다음 액션\nA작업표식\n"
        "\n## 2026-11-09 14:20 checkpoint tokens=1 session=bbbb2222\n### 다음 액션\nB작업표식\n"
        "\n## 2026-11-09 14:30 turn session=aaaa1111\n**요청:** A턴표식\n"
        "\n## 2026-11-09 14:31 turn session=bbbb2222\n**요청:** B턴표식\n"
        "\n## 2026-11-09 14:32 turn\n**요청:** 옛형식턴표식\n"
    ))


def test_clear_restores_the_closing_sessions_own_record():
    root = _project()
    _two_terminals(root)
    _capture(on_session_end, {"cwd": str(root), "session_id": "aaaa1111-xxxx", "reason": "clear"})
    out = _capture(on_session_start, {"cwd": str(root), "source": "clear", "session_id": "new"})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "A작업표식" in ctx and "A턴표식" in ctx and "옛형식턴표식" in ctx
    assert "B작업표식" not in ctx and "B턴표식" not in ctx
    assert not (root / ".leneu-session" / ".cleared").exists()  # 한 번만 쓴다


def test_clear_falls_back_to_latest_when_session_left_no_checkpoint():
    root = _project()
    _two_terminals(root)
    _capture(on_session_end, {"cwd": str(root), "session_id": "cccc3333", "reason": "clear"})
    ctx = _capture(on_session_start, {"cwd": str(root), "source": "clear"})["hookSpecificOutput"]["additionalContext"]
    assert "B작업표식" in ctx


def test_clear_follows_the_chain_of_sessions_in_one_terminal():
    """A → /clear → B(체크포인트 없이 짧게 씀) → /clear. 다른 터미널(B가 아닌 bbbb2222)의 더 새 기록이 아니라 A의 것을 잇는다."""
    root = _project()
    _two_terminals(root)
    _capture(on_session_end, {"cwd": str(root), "session_id": "aaaa1111-x", "reason": "clear"})
    _capture(on_session_start, {"cwd": str(root), "source": "clear", "session_id": "dddd4444-x"})
    with (root / ".leneu-session" / "logs" / "2026-11-09.md").open("a") as f:
        f.write("\n## 2026-11-09 14:40 turn session=dddd4444\n**요청:** D턴표식\n")
    _capture(on_session_end, {"cwd": str(root), "session_id": "dddd4444-x", "reason": "clear"})
    out = _capture(on_session_start, {"cwd": str(root), "source": "clear", "session_id": "eeee5555-x"})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "A작업표식" in ctx and "A턴표식" in ctx and "D턴표식" in ctx
    assert "B작업표식" not in ctx and "B턴표식" not in ctx


def test_restore_ignores_other_terminals_handoff():
    root = _project()
    _two_terminals(root)
    mine = Path(cli_handoff_path(str(root), datetime(2026, 11, 9, 14, 50), "aaaa1111-x").splitlines()[0])
    other = Path(cli_handoff_path(str(root), datetime(2026, 11, 9, 14, 55), "bbbb2222-x").splitlines()[0])
    assert mine.name == "handoff-20261109-1450-aaaa1111.md" and is_allowed_write(root, str(mine))
    _handoff(root, mine.name, "내handoff표식", datetime(2026, 11, 9, 14, 50))
    _handoff(root, other.name, "남의handoff표식", datetime(2026, 11, 9, 14, 55))
    _capture(on_session_end, {"cwd": str(root), "session_id": "aaaa1111-x", "reason": "clear"})
    ctx = _capture(on_session_start, {"cwd": str(root), "source": "clear"})["hookSpecificOutput"]["additionalContext"]
    assert "내handoff표식" in ctx and "남의handoff표식" not in ctx
    assert cli_handoff_path(str(root), NOW, None).splitlines()[0].endswith("-1500.md")  # 세션을 모르면 예전 이름


def test_startup_lists_other_terminals_recent_work():
    root = _project()
    _two_terminals(root)
    ctx = build_context(root, "startup", datetime(2026, 11, 9, 15, 0))
    assert "B작업표식" in ctx  # 가장 새 기록은 그대로 넣고
    assert "다른 터미널" in ctx and "session=aaaa1111" in ctx and "A작업표식" in ctx  # 다른 줄기는 한 줄로 알린다
    assert "어느 작업" in ctx
    assert "B턴표식" in ctx and "A턴표식" not in ctx  # 이어 붙이는 턴은 고른 기록의 줄기 것만
    old = build_context(root, "startup", datetime(2026, 11, 11, 15, 0))
    assert "다른 터미널" not in old  # 하루 넘은 것은 알리지 않는다


def test_startup_one_terminal_has_no_other_work_note():
    root = _project()
    _write_log(root, "2026-11-09", CKPT.replace("session=s", "session=aaaa1111")
               + "\n## 2026-11-09 14:40 checkpoint tokens=1 session=dddd4444\n### 다음 액션\n이어서\n")
    state = load_state(root)
    state["dddd4444-x"] = {"from": "aaaa1111-x"}  # /clear로 이어진 같은 터미널
    save_state(root, state)
    assert "다른 터미널" not in build_context(root, "startup", datetime(2026, 11, 9, 15, 0))


def test_checkpoint_written_right_before_clear_is_restored():
    """체크포인트를 쓰자마자 /clear 하면 다음 Stop이 없다. 세션이 끝날 때 그 세션 이름으로 옮긴다."""
    root = _project()
    _two_terminals(root)
    (root / ".leneu-session" / ".pending-checkpoint.md").write_text("### 다음 액션\n막쓴표식\n")
    _capture(on_session_end, {"cwd": str(root), "session_id": "aaaa1111-x", "reason": "clear"})
    assert "checkpoint tokens=? session=aaaa1111" in _log_text(root)
    ctx = _capture(on_session_start, {"cwd": str(root), "source": "clear"})["hookSpecificOutput"]["additionalContext"]
    assert "막쓴표식" in ctx and "B작업표식" not in ctx


def test_checkpoint_of_unknown_session_is_not_filtered_out():
    """비정상 종료로 주인을 모르는 체크포인트(session=?)는 어느 터미널에서도 놓치지 않는다."""
    root = _project()
    _two_terminals(root)
    with (root / ".leneu-session" / "logs" / "2026-11-09.md").open("a") as f:
        f.write("\n## 2026-11-09 14:50 checkpoint tokens=? session=?\n### 다음 액션\n주인모름표식\n")
    _capture(on_session_end, {"cwd": str(root), "session_id": "aaaa1111-x", "reason": "clear"})
    ctx = _capture(on_session_start, {"cwd": str(root), "source": "clear"})["hookSpecificOutput"]["additionalContext"]
    assert "주인모름표식" in ctx


def test_two_terminals_never_take_each_others_checkpoint():
    root = _project()
    r = checkpoint_reason(root, False, "aaaa1111-x")
    mine = root / ".leneu-session" / ".pending-checkpoint-aaaa1111.md"
    assert str(mine) in r and is_allowed_write(root, str(mine))
    assert not is_allowed_write(root, str(root / ".leneu-session" / ".pending-checkpoint-x/y.md"))
    mine.write_text("### 다음 액션\nA가쓴표식\n")
    _stop(root, 47000, sid="bbbb2222-x")  # 다른 터미널의 턴이 먼저 끝나도
    assert mine.exists() and "A가쓴표식" not in _log_text(root)
    _stop(root, 47000, sid="aaaa1111-x")
    assert "session=aaaa1111\n" in _log_text(root) and "A가쓴표식" in _log_text(root)


def test_crash_leftover_keeps_its_owner_from_file_name():
    root = _project()
    (root / ".leneu-session" / ".pending-checkpoint-cccc3333.md").write_text("### 다음 액션\n남은표식\n")
    _capture(on_session_start, {"cwd": str(root), "source": "startup"})
    assert "session=cccc3333" in _log_text(root) and "남은표식" in _log_text(root)


def test_turn_header_carries_session():
    out = render_turn({"prompt": "p", "reply": "", "tools": [], "files": []}, NOW, session="6f74a221-d734")
    assert "## 2026-11-09 15:00 turn session=6f74a221\n" in out
    root = _project()
    _stop(root, 47000, sid="6f74a221-zz", prompt="태그확인")
    assert "turn session=6f74a221" in _log_text(root)


def selftest():
    os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"  # 어디서 돌려도 대화형 기준으로 검사한다
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
    if "--selftest" in sys.argv:
        selftest()
    elif "--init" in sys.argv:
        print(cli_init(os.getcwd()))
    elif "--stop" in sys.argv:
        print(cli_stop(os.getcwd()))
    elif "--handoff-path" in sys.argv:
        print(cli_handoff_path(os.getcwd(), datetime.now(), os.environ.get("CLAUDE_CODE_SESSION_ID")))
    elif "--brief-path" in sys.argv:
        print(cli_brief_path(os.getcwd()))
    elif "--secure" in sys.argv:
        print(cli_secure(os.getcwd(), " ".join(sys.argv[sys.argv.index("--secure") + 1:])))
    else:
        main()
