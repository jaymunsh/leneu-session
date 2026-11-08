# leneu-session

긴 작업이 세션을 넘어가도 끊기지 않게 하는 Claude Code 플러그인입니다. 일정 작업량마다 체크포인트를 자동으로 남기고, `/clear` 후 하던 일을 바로 잇습니다.

## 사용 흐름

```
/leneu-init            이 프로젝트에서 켠다 (프로젝트마다 한 번)
... 평소대로 작업 ...    턴은 자동 기록되고, 체크포인트가 자동으로 쌓인다
"/clear를 권합니다"      작업이 일단락되면 직접 /clear 를 친다
.                      아무거나 짧게 입력하면 이어서 진행한다
```

`/clear` 를 언제 치든 괜찮습니다. 권고가 뜨기 전이라도 작업 하나가 끝났을 때가 가장 좋은 시점입니다.

## 명령어

| 명령 | 하는 일 |
|---|---|
| `/leneu-init` | 켠다. 꺼 둔 프로젝트는 기록을 유지한 채 다시 켠다 |
| `/leneu-stop` | 끈다. 기록은 지우지 않는다 |
| `/leneu-handoff [초점]` | 인계 문서를 쓰고 이어갈 프롬프트를 준다. 다른 에이전트나 훅이 꺼진 환경에서도 쓸 수 있다 |
| `/leneu-brief [--test]` | 프로젝트 전체를 다른 프로젝트·에이전트에게 설명하는 문서를 쓴다 |

복귀, 로그 검색, 수동 기록은 말로 하면 됩니다. "지난번에 이거 어떻게 고쳤지", "이건 남겨줘".

## 설치

```bash
claude plugin marketplace add /Users/sunghyuk/Documents/Service/leneu-session
claude plugin install leneu-session@leneu-session
```

jay-session과 함께 켜 두면 훅이 겹치므로 비활성화합니다. 기존 `.claude/session/` 기록은 옮기지 않고 그대로 남습니다.

```bash
claude plugin disable jay-session@jay-claude
```

설치 없이 시험하려면 `claude --plugin-dir /Users/sunghyuk/Documents/Service/leneu-session`.

## 설정

`.leneu-session/config.json` (선택). 없으면 기본값을 씁니다.

| 키 | 기본값 | 뜻 |
|---|---|---|
| `context_window` | `1000000` | 컨텍스트 창 크기. **자동 감지가 안 됩니다** |
| `clear_recommend_pct` | `30` | 이 비율을 넘긴 체크포인트에서 `/clear` 를 권한다 |
| `checkpoint_every_pct` | `10` | 컨텍스트가 이 비율만큼 늘 때마다 체크포인트를 남긴다 |

1M 기본값에서는 약 100K마다 체크포인트가 쌓이고 권고는 300K를 넘긴 첫 체크포인트(시작 토큰이 47K면 약 347K)에서 뜹니다. 200K 창을 쓰는 프로젝트는 아래처럼 바꾸세요. 그대로 두면 권고 전에 auto-compact가 먼저 일어날 수 있습니다.

```json
{"context_window": 200000, "clear_recommend_pct": 45}
```

## 저장 위치

```
.leneu-session/
├── .gitignore         # 내용은 *. 폴더 전체가 git에서 제외된다
├── logs/YYYY-MM-DD.md # 턴 기록과 체크포인트. 삽입만 하며 영구 보존
├── handoffs/          # /leneu-handoff 문서. 최근 10개 유지
├── project-brief.md   # /leneu-brief 문서
├── config.json        # 선택
├── hook-errors.log    # 훅 오류. 다음 세션 시작 때 알려 준다
└── disabled           # /leneu-stop 표시
```

## 알려진 한계

- **`/clear` 는 수동입니다.** 훅으로 실행할 수단이 없습니다.
- **복귀 시 입력이 한 번 필요합니다.** `.` 같은 입력으로 시작합니다.
- **동시 세션을 구분하지 않습니다.** 같은 폴더를 터미널 여러 개로 동시에 열면 `/clear` 가 다른 터미널의 체크포인트를 읽을 수 있습니다. 복귀 시 저장소 상태를 확인하도록 안내해 완화합니다. 서브에이전트와 worktree는 영향이 없습니다.
- **비밀값 마스킹은 흔한 패턴만 가립니다.** 프롬프트에 붙여넣은 비밀번호처럼 형식 없는 값은 로그에 남습니다. 폴더는 기본적으로 git에서 제외됩니다.
- **비대화형 판별은 문서화되지 않은 값에 의존합니다.** `CLAUDE_CODE_ENTRYPOINT`(`claude -p` 는 `sdk-cli`)를 보며 값이 없으면 대화형으로 취급합니다.
- **체크포인트마다 짧은 턴이 하나 추가되어** 토큰이 조금 들고 화면에도 보입니다. 간격은 `checkpoint_every_pct` 로 조정합니다.

## 개발

```bash
python3 scripts/session_hook.py --selftest
```

설계: `docs/superpowers/specs/2026-11-09-leneu-session-design.md`
