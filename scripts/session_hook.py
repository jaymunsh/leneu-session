#!/usr/bin/env python3
"""leneu-session 훅 (Stop / PreCompact / SessionStart / PreToolUse 공용).

자기검증: python3 session_hook.py --selftest
"""
import json
import os
import sys
import tempfile
from pathlib import Path


DIR = ".leneu-session"
DEFAULTS = {"context_window": 1000000, "clear_recommend_pct": 30, "checkpoint_every_pct": 10}
MAX_SESSIONS = 20


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
