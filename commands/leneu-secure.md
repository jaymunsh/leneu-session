---
description: 보안 모드를 켜고 끈다. 기록에서 개인정보·내부 주소·지정어를 가린다
argument-hint: "[on | off | add 단어, 단어 | remove 단어 | list]"
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_hook.py" --secure "$ARGUMENTS"`

위 결과를 그대로 사용자에게 전달한다. 추가 작업이나 질문은 하지 않는다.
