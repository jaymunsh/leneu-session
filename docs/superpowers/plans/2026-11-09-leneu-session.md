# leneu-session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 체크포인트 자동 기록과 `/clear` 후 자동 복귀를 제공하는 Claude Code 플러그인 `leneu-session`을 만든다.

**Architecture:** 훅 진입점 하나(`scripts/session_hook.py`)가 Stop, PreCompact, SessionStart, PreToolUse를 처리한다. 턴 기록은 스크립트가 쓰고, 체크포인트는 Stop 훅의 지시로 모델이 임시 파일에 쓴 뒤 훅이 로그로 옮긴다. 슬래시 명령은 마크다운 지시문이다.

**Tech Stack:** Python 3 표준 라이브러리, Claude Code 플러그인(hooks, commands, skills). 테스트는 `--selftest`의 assert.

**Spec:** `docs/superpowers/specs/2026-11-09-leneu-session-design.md`

## Global Constraints

- 외부 의존성 없음. 런타임은 `python3`.
- 저장 위치: `<프로젝트 루트>/.leneu-session/`. 이 폴더가 없으면 모든 훅이 무동작(worktree 자동 생성 예외).
- 기본 설정: `context_window=1000000`, `clear_recommend_pct=30`, `checkpoint_every_pct=10`.
- 로그 잘라내기: 요청 400자, 결과 600자. 주입 본문 8,000자, 체크포인트 우선 보존.
- 훅은 어떤 경우에도 작업을 막는 오류를 내지 않는다. 예외는 `.leneu-session/hook-errors.log`에 기록하고 exit 0.
- 모델은 로그 파일을 직접 쓰지 않는다.
- 로그 헤더 정규식: `^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (turn|checkpoint|compact)\b`.
- 커밋 메시지 끝에 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- `.state.json`이나 `config.json`이 깨졌을 때: 기본값으로 동작하고 다음 Stop에 복구. (Task 1)
- transcript에 깨진 줄이 있거나 usage가 없을 때: 턴 기록은 남기고 체크포인트 지시는 건너뜀. (Task 2, 3)
- 모델이 체크포인트 본문에 `## ` 헤더를 쓸 때: `### `로 내려 로그 구조를 지킴. (Task 3)
- 이전 세션 충돌로 임시 체크포인트가 남아 있을 때: 다음 Stop에서 한 번만 로그로 옮김. (Task 3)
- 자정을 넘긴 복귀: 전날 체크포인트와 오늘 턴 기록을 함께 찾음. (Task 4)

---

### Task 1: 뼈대와 공통 기반, 문서 확인

**Files:**
- Create: `.gitignore`, `.claude-plugin/plugin.json`, `hooks/hooks.json`, `scripts/session_hook.py`

**Interfaces:**
- Produces: `find_root(cwd: str) -> Path | None`, `load_config(root: Path) -> dict`, `load_state(root: Path) -> dict`, `save_state(root: Path, state: dict) -> None`, `log_error(root: Path | None, exc: BaseException) -> None`

- [ ] **Step 1: `git init`, `.gitignore`에 `.leneu-session/`, `__pycache__/`**

- [ ] **Step 2: 설계 문서의 "구현 전에 확인할 것"을 공식 문서에서 확인**

읽을 문서: `https://code.claude.com/docs/en/hooks` 전체. 확인 항목: Stop의 block 형식과 연속 제한, PreToolUse 허용 응답 형식과 권한 규칙과의 관계, SessionStart의 입력 없는 시작 가능 여부. 비대화형 구분은 `claude -p`와 대화형을 실제 실행해 훅 환경변수와 transcript의 `entrypoint` 값을 비교한다. 결과를 설계 문서 해당 절에 적는다. 설계가 바뀌어야 하면 멈추고 사용자에게 알린다.

- [ ] **Step 3: 실패하는 selftest 작성**

```python
# find_root: 하위 폴더에서 상위의 .leneu-session 발견
# find_root: 가장 가까운 .git(폴더 또는 파일) 위로는 탐색하지 않음 -> None
# find_root: git 밖이면 홈에서 멈춤
# find_root: .leneu-session/disabled가 있으면 None
assert load_config(root)["clear_recommend_pct"] == 30
# 깨진 config.json, 깨진 .state.json -> 예외 없이 기본값 / {}
# save_state 후 임시 파일이 남지 않고 load_state로 복원
```

- [ ] **Step 4: `python3 scripts/session_hook.py --selftest` 실패 확인**

- [ ] **Step 5: 구현.** `save_state`는 같은 폴더 임시 파일에 쓰고 `os.replace`. 세션 항목은 최근 20개만 유지. `main()`은 stdin JSON을 이벤트별 핸들러로 보내고 모든 예외를 `log_error`로 받는다. `hooks.json`은 Stop, PreCompact, SessionStart, PreToolUse(`Write`)를 timeout 10으로 등록. `plugin.json`의 name은 `leneu-session`.

- [ ] **Step 6: selftest 통과 확인 후 커밋** (`feat: 플러그인 뼈대와 공통 기반`)

### Task 2: transcript 파싱과 턴 기록

**Files:**
- Modify: `scripts/session_hook.py`

**Interfaces:**
- Consumes: `find_root`
- Produces: `load_transcript(path: str) -> list[dict]`, `context_tokens(entries: list[dict]) -> int | None`, `parse_turns(entries: list[dict]) -> list[dict]`(각 항목 키 `prompt, reply, tools, files`), `mask_secrets(text: str) -> str`, `render_turn(turn: dict, now: datetime) -> str`, `append_log(root: Path, text: str, now: datetime) -> Path`

- [ ] **Step 1: 실패하는 selftest 작성**

```python
usage = {"input_tokens": 4, "cache_creation_input_tokens": 761, "cache_read_input_tokens": 78235}
assert context_tokens([{"type": "assistant", "message": {"usage": usage}}]) == 79000
# sidechain 항목의 usage는 무시, usage가 없으면 None
assert len(parse_turns(two_user_turns)) == 2          # 커서 이후 전부 기록
assert parse_turns(only_task_notification) == []      # 시스템 메시지 제외
assert parse_turns(only_tool_result) == []
assert "sk-ant-abc123def456" not in mask_secrets("key sk-ant-abc123def456")
assert "일반 문장" in mask_secrets("일반 문장")
out = render_turn(turn, datetime(2026, 11, 9, 14, 2))
assert out.lstrip().startswith("## 2026-11-09 14:02 turn")
assert "\n## " not in out.lstrip()[3:]                # 여러 줄 입력도 헤더를 만들지 않음
assert append_log(root, "x", datetime(2026, 11, 9)).name == "2026-11-09.md"
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현.** jay-session의 `load_transcript`, `_text`, `SYSTEM_TAG`를 가져오고 태그에 `task-notification`을 추가한다. Stop 훅 피드백 항목의 실제 형태는 Task 3 실행에서 확인해 제외 목록에 넣는다. 마스킹 대상: `sk-…`, `ghp_…`, `github_pat_…`, `AKIA…`, `Bearer …`, `xox[bp]-…`.

- [ ] **Step 4: 통과 확인 후 커밋** (`feat: 턴 기록`)

### Task 3: Stop 체크포인트

**Files:**
- Modify: `scripts/session_hook.py`

**Interfaces:**
- Consumes: Task 1, Task 2 전체
- Produces: `consume_pending(root: Path, tokens: int | None, session_id: str, transcript_path: str, now: datetime) -> bool`, `decide_checkpoint(cfg: dict, sess: dict, tokens: int) -> str`(`"none" | "checkpoint" | "checkpoint+clear"`), `checkpoint_reason(root: Path, recommend_clear: bool) -> str`, `on_stop(data: dict) -> None`

- [ ] **Step 1: 실패하는 selftest 작성**

```python
cfg = {"context_window": 1000000, "clear_recommend_pct": 30, "checkpoint_every_pct": 10}
assert decide_checkpoint(cfg, {"last_ckpt": 47000}, 146999) == "none"
assert decide_checkpoint(cfg, {"last_ckpt": 47000}, 147000) == "checkpoint"
assert decide_checkpoint(cfg, {"last_ckpt": 247000}, 347000) == "checkpoint+clear"
# 첫 Stop: baseline, last_ckpt가 현재 토큰으로 설정되고 block 없음
# 토큰이 last_ckpt보다 줄면 baseline, last_ckpt 재설정(압축)
# stop_hook_active=True: 출력 없음, 턴 미기록, 임시 파일은 로그로 이동
# 임시 파일: 헤더에 tokens/session, **원본:** 경로 포함, 이동 후 삭제, 두 번 호출해도 1회만 기록
# 본문의 "## 제목"은 "### 제목"으로 변환
# usage 없음: 턴 기록은 남고 block 없음
# .leneu-session 없음: 아무 파일도 생기지 않음
r = checkpoint_reason(root, True)
for h in ["현재 작업의 요구와 사용자 정정", "확인된 사실", "변경 파일과 검증 여부", "결정", "막다른 길", "다음 액션"]:
    assert h in r
assert ".pending-checkpoint.md" in r and "/clear" in r
assert "/clear" not in checkpoint_reason(root, False)
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현.** 순서는 설계 문서 Stop 절 1~8을 따른다. `reason`에는 임시 파일 절대경로, 항목, "이 파일 작성만 하고 다른 작업을 시작하지 말 것", "비밀값을 적지 말 것"을 넣는다. 새 handoff 파일이 생긴 경우의 `last_ckpt` 갱신은 Task 7에서 추가한다.

- [ ] **Step 4: 통과 확인 후 커밋** (`feat: Stop 체크포인트`)

### Task 4: 복귀 주입과 자동 승인, /leneu-init

**Files:**
- Modify: `scripts/session_hook.py`
- Create: `commands/leneu-init.md`, `commands/leneu-stop.md`

**Interfaces:**
- Consumes: Task 1~3
- Produces: `latest_checkpoint(root: Path) -> tuple[datetime, str, Path] | None`, `turns_after(root: Path, since: datetime | None) -> str`, `build_context(root: Path, source: str, now: datetime) -> str | None`, `is_allowed_write(root: Path, file_path: str) -> bool`, `on_session_start(data: dict) -> None`, `on_pre_tool_use(data: dict) -> None`

- [ ] **Step 1: 실패하는 selftest 작성**

```python
# latest_checkpoint: 오늘 파일에 없으면 이전 날짜 파일에서 찾고, 본문은 다음 "## " 전까지
# turns_after: 전날 체크포인트 이후 오늘 turn까지 포함, compact 항목 제외
c = build_context(root, "clear", now)
assert "체크포인트 본문표식" in c and "이후 턴표식" in c and "확인" in c
assert "본문표식" not in build_context(root, "compact", now)
assert ".md" in build_context(root, "compact", now)
assert build_context(root, "resume", now) is None
assert build_context(root, "fork", now) is None
# startup: 25시간 지난 체크포인트면 이어갈지 확인 문구 포함, 1시간이면 미포함
# 8000자 초과: 체크포인트 전체는 보존, 턴 기록을 자르고 경로 포함
# 체크포인트도 턴도 없으면 None
# 체크포인트 없이 턴만 있으면 턴으로 컨텍스트 생성
assert is_allowed_write(root, str(root / ".leneu-session/.pending-checkpoint.md"))
assert not is_allowed_write(root, str(root / ".leneu-session/logs/2026-11-09.md"))
assert not is_allowed_write(root, str(root / ".leneu-session/../evil.md"))
assert not is_allowed_write(root, "/etc/passwd")
# --stop: disabled 생성, logs 파일 유지. 이후 on_stop은 아무것도 기록하지 않음
# --init: disabled 제거, 기존 로그 유지. 다시 기록 시작
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현.** `.`만 입력해도 이어가도록 안내 문구에 명시한다. 경로 비교는 `Path.resolve()` 뒤에 한다. 허용 응답 형식은 Task 1 Step 2 결과를 따른다. `commands/leneu-init.md`는 설계 문서 명령어 절대로 쓰고, 폴더 생성은 `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_hook.py" --init`으로 처리해 `.gitignore`(`*`), `logs/`를 만든다. 이미 있으면 상태만 보고하고, `disabled`가 있으면 지운다. `commands/leneu-stop.md`는 `--stop`을 호출해 `disabled`를 만들고 "기록은 그대로이며 `/leneu-init`으로 다시 켠다"고 알린다. 폴더가 없으면 아무것도 만들지 않는다.

- [ ] **Step 4: 통과 확인 후 커밋** (`feat: 복귀 주입과 자동 승인, init`)

### Task 5: 체감 검증 (사용자 게이트)

**Files:** 없음. 결과에 따라 설계 문서 수정.

- [ ] **Step 1: 임시 프로젝트 준비.** `mktemp -d`로 만든 폴더에서 init 후 `config.json`에 `{"context_window": 200000, "checkpoint_every_pct": 2, "clear_recommend_pct": 30}`. 사용자에게 `claude --plugin-dir /Users/sunghyuk/Documents/Service/leneu-session` 실행을 안내한다. 임시 프로젝트에는 `.claude/session/`이 없어 jay-session의 턴 기록은 돌지 않으므로, 이 시험을 위해 비활성화할 필요는 없다.

- [ ] **Step 2: 사용자가 직접 확인.** 턴 기록, 체크포인트가 끼어드는 느낌과 분량, 권한 프롬프트 없음, `/clear` 후 `.` 입력으로 이어가기.

- [ ] **Step 3: 로그의 Stop 훅 피드백 오염 여부 확인.** 있으면 Task 2의 제외 목록에 추가하고 selftest를 더한다.

- [ ] **Step 4: 사용자 판단 받기.** 거슬리면 시점이나 방식을 조정하고 설계 문서에 반영한다. **승인 전에는 Task 6으로 넘어가지 않는다.**

### Task 6: 나머지 훅 동작

**Files:**
- Modify: `scripts/session_hook.py`

**Interfaces:**
- Consumes: `find_root`, `append_log`, `build_context`
- Produces: `on_precompact(data: dict) -> None`, `is_noninteractive(entrypoint: str | None) -> bool`, `ensure_worktree(cwd: str) -> Path | None`, `new_errors(root: Path, sess: dict) -> str | None`

- [ ] **Step 1: 실패하는 selftest 작성**

```python
# on_precompact: compact 항목과 transcript 경로 기록. latest_checkpoint 결과에 영향 없음
# is_noninteractive: Task 1 Step 2에서 확인한 값으로 True/False, None이면 False
# 비대화형이면 Stop은 턴만 기록하고 block 없음, SessionStart는 출력 없음
# ensure_worktree: .git 파일 + commondir → 메인이 켜져 있으면 worktree에 폴더 생성
# ensure_worktree: 메인이 꺼져 있으면 생성 안 함, 일반 저장소면 None, 이미 있으면 그대로
# 새 오류가 있으면 SessionStart 출력에 한 줄 알림, 알린 오류는 다시 알리지 않음
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현.** 메인 체크아웃 경로는 `.git` 파일의 `gitdir:` → 그 폴더의 `commondir` 값 → 해석된 경로의 부모. subprocess를 쓰지 않는다.

- [ ] **Step 4: 통과 확인 후 커밋** (`feat: 압축 기록, 비대화형, worktree, 오류 알림`)

### Task 7: /leneu-handoff

**Files:**
- Create: `commands/leneu-handoff.md`
- Modify: `scripts/session_hook.py`

**Interfaces:**
- Consumes: `build_context`, `turns_after`, `is_allowed_write`, `on_stop`
- Produces: `latest_handoff(root: Path) -> tuple[datetime, Path] | None`

- [ ] **Step 1: 실패하는 selftest 작성**

```python
# handoff가 체크포인트보다 새로우면 handoff 본문과 그 이후 턴을 주입, 오래되면 체크포인트 주입
# 새 handoff 파일을 감지한 Stop은 last_ckpt를 현재 토큰으로 갱신하고 block 없음
assert is_allowed_write(root, str(root / ".leneu-session/handoffs/handoff-20261109-1430.md"))
assert not is_allowed_write(root, str(root / ".leneu-session/handoffs/other.md"))
assert not is_allowed_write(root, str(root / ".leneu-session/handoffs/sub/handoff-1.md"))
```

- [ ] **Step 2: 실패 확인**

- [ ] **Step 3: 구현.** 명령 파일은 설계 문서 명령어 절대로 쓰고 `/jay-new`의 원칙(먼저 쓰고 나중에 묻기, `date` 사용, git 아닐 때 분기, 복붙 프롬프트만 출력, `/clear` 대신 실행 제안 금지)을 잇는다. 10개 초과분 삭제 명령은 `ls -t` + `grep '^handoff-'` 방식.

- [ ] **Step 4: 통과 확인 후 커밋** (`feat: 수동 handoff`)

### Task 8: /leneu-brief와 스킬

**Files:**
- Create: `commands/leneu-brief.md`, `skills/session-manager/SKILL.md`

- [ ] **Step 1: `/jay-brief`를 기반으로 `commands/leneu-brief.md` 작성.** 설계 문서 `/leneu-brief` 절의 5개 개선을 반영하고, 재료 grep을 `.leneu-session/logs/`의 `### 결정`, `### 막다른 길`로 바꾼다. `--test`일 때만 백지 테스트.

- [ ] **Step 2: `skills/session-manager/SKILL.md` 작성.** 복귀, 로그 검색, 수동 기록 절차. jay-session의 description 트리거 어휘를 잇되 경로와 명령어를 바꾼다.

- [ ] **Step 3: 잔재 확인.** `grep -rnE 'jay-|\.claude/session' commands skills` 결과가 없어야 한다.

- [ ] **Step 4: 임시 git 저장소에서 `/leneu-brief` 실행.** SHA, 근거 표시가 있고, 재실행 시 설계 결정 절이 보존되는지 확인.

- [ ] **Step 5: 커밋** (`feat: brief와 세션 스킬`)

### Task 9: 마무리와 전체 검증

**Files:**
- Create: `README.md`

- [ ] **Step 1: README 작성.** 설치, jay-session 비활성화, 사용 흐름(`/leneu-init` → 권고 → `/clear` → `.`), 설정 3개와 200K 창 조정, 알려진 한계(동시 세션, 마스킹 불완전, `/clear` 수동).

- [ ] **Step 2: selftest 전체 통과 확인**

- [ ] **Step 3: 전체 흐름 실행.** 임시 프로젝트에서 init → 체크포인트 → `/leneu-handoff` → `/clear` → 복귀, `claude -p` 무동작, worktree 자동 생성을 실제로 확인한다.

- [ ] **Step 4: 커밋** (`docs: README`)
