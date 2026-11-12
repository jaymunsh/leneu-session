---
name: session-manager
description: leneu-session의 복귀, 작업 로그 검색, 수동 기록. Use when the user starts a new session on continuing work or says 이어서 할게, 지난번 거 계속, 어디까지 했지, 작업로그, 개발일지, devlog, 회고, 결정 기록, "지난번에 이거 어떻게 했지", "왜 이렇게 했더라", "이건 남겨줘", or asks what was done in a previous session. Also use for 인계, 핸드오프, handoff, 세션 정리 (→ /leneu-handoff) and 프로젝트 브리프, 온보딩 문서, 다른 에이전트한테 설명, project brief (→ /leneu-brief).
---

# Session Manager

자동 기록과 `/clear` 후 복귀는 훅이 처리한다. 이 스킬은 훅이 다루지 않는 복귀, 검색, 수동 기록을 맡는다.

## 파일 위치

```
.leneu-session/
├── logs/YYYY-MM-DD.md   # 턴 기록, 체크포인트, 압축 시점. 삽입만 한다
├── handoffs/handoff-*.md # /leneu-handoff 문서. 최근 10개
├── project-brief.md      # /leneu-brief 문서
├── config.json           # 선택. context_window, clear_recommend_pct, checkpoint_every_pct, keep_log_days
├── secure.json           # /leneu-secure 상태. enabled가 true면 보안 모드
└── disabled              # 있으면 훅이 꺼진다
```

로그의 절 헤더는 `## YYYY-MM-DD HH:MM turn|checkpoint|compact` 형식이다. 체크포인트의 `**원본:**` 은 전체 대화 transcript 경로라서, 잘린 출력이나 대화 전문이 필요하면 그 파일을 grep 한다. 크기가 클 수 있으니 통째로 읽지 않는다.

## 복귀

`/clear` 직후와 새 세션 시작 때는 훅이 이미 최신 기록을 주입한다. 주입된 내용이 있으면 파일을 다시 찾지 않는다. 사용자가 `.` 처럼 짧게만 입력하면 이어가라는 뜻이다.

주입이 없는데 이어서 하자는 말이 나오면:

1. 최신 기록을 찾는다.
   ```bash
   ls -t .leneu-session/handoffs 2>/dev/null | head -1
   ls .leneu-session/logs 2>/dev/null | tail -2
   grep -n ' checkpoint ' "$(ls -d .leneu-session/logs/*.md 2>/dev/null | tail -1)" | tail -1
   ```
   아무것도 없으면 그렇게 말하고 `/leneu-init` 을 안내한다.
2. 가장 새 체크포인트나 handoff와 그 이후의 턴 기록을 읽는다.
3. **지금도 맞는지 검증한다.** git 저장소면 `git status` 와 `git log --oneline` 으로 그 뒤에 무슨 일이 있었는지 본다. 아니면 해당 파일을 직접 읽는다.
4. 3~5줄로 브리핑하고 다음 액션을 제안한다. 문서를 다시 읽어주지 않는다.

## 검색

"지난주에 그 에러 어떻게 고쳤지", "이 파일 왜 이렇게 됐지" 같은 질문은 로그를 검색한다.

```bash
grep -rn -i -B2 -A8 "검색어" .leneu-session/logs .leneu-session/handoffs 2>/dev/null | tail -60
```

파일이 크면 grep으로 좁힌 뒤 해당 구간만 읽는다. 결정만 모아 보려면 `grep -rh -A6 '^### 결정' .leneu-session/logs`. 회고 요청이면 날짜 파일을 골라 주제별로 묶어 요약한다. 시간순 나열은 원본을 읽는 것과 다르지 않다.

## 수동 기록

사용자가 "이건 남겨줘"라고 하면 그 프로젝트의 오늘 로그 끝에 Bash로 append 한다. 시각은 `date` 로 얻는다. 과거 항목은 고치거나 지우지 않는다. 헤더 형식을 지켜야 복귀 검색이 깨지지 않으며 본문에는 `## ` 를 쓰지 않는다. 이 기록은 훅을 거치지 않으므로, `secure.json` 이 켜져 있으면 이메일·전화번호·내부 주소와 `terms` 의 단어를 직접 `[이메일]`, `[지정어-N]`(목록 순서) 식으로 가려 쓴다.

```markdown

## 2026-11-09 14:02 turn
**요청:** {사용자가 원한 것}
**결과:** {한 것과 결과}
**메모:** {결정과 이유, 실패한 시도처럼 훅이 못 잡는 것}
```

## 끄고 켜기

`/leneu-stop` 은 기록을 남긴 채 훅만 끄고, `/leneu-init` 은 다시 켠다. `/clear` 는 대화를 버리는 동작이라 사람이 직접 친다. 대신 실행하겠다고 제안하지 않는다.
