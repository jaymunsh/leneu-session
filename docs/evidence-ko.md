# leneu-session이 이렇게 동작하는 이유

English: [evidence.md](evidence.md)

이 문서는 설계의 근거를 정리합니다. 외부 주장은 원문에서 인용했고, 2026-11-12에 출처를 직접 열어 확인했습니다. 온라인에서 돌지만 출처에서 찾지 못한 주장은 사실처럼 되풀이되지 않도록 마지막에 따로 적었습니다.

## 1. 컨텍스트가 길어지면 모델이 흐려진다

**Anthropic, [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)** (2025-09-29)

> LLMs have an "attention budget" that they draw on when parsing large volumes of context.

> Every new token introduced depletes this budget by some amount.

글은 이것을 *context rot*이라고 부르고, 해법 두 가지를 듭니다. 둘 다 이 플러그인이 씁니다.
- 압축
- "structured note-taking": 에이전트가 "컨텍스트 창 밖의 메모리에 남는 메모를 정기적으로 쓰는" 방식

**Chroma, [Context Rot](https://www.trychroma.com/research/context-rot)** (기술 보고서, 2025-07-14). GPT-4.1, Claude 4, Gemini 2.5, Qwen3 등 모델 18개를 평가했습니다.

> model performance consistently degrades with increasing input length.

**leneu-session이 가져온 것:** 지금 쓰는 컨텍스트는 작게 두고, 중요한 상태는 대화가 아니라 파일에 둡니다. 체크포인트가 그 메모입니다.

## 2. 짧은 컨텍스트와 인계는 싸고, 잃는 것이 적다

**Belcak 외, [You don't need long context](https://arxiv.org/abs/2611.05306)** (arXiv:2611.05306, 2026-11)

> frontier language models sacrifice little to no task accuracy when maintaining short contexts.

> the disparity in cost may rise to as much as 7x.

7배는 오픈 웨이트 모델을 256K 컨텍스트 창으로 서빙할 때와 16K로 서빙할 때의 생성 토큰당 비용 차이입니다. 모델에 따라 3.6배에서 7배입니다. 논문은 이렇게도 적습니다.

> Their numerical boundaries are rough rules of thumb.

이 문장은 "짧은 컨텍스트"를 16K–50K로 정의한 부분에 대한 것입니다.

논문의 압축 방식은 세 가지를 남깁니다(부록 A의 요약이며 원문 인용은 아님).
- 과제를 거의 원문 그대로
- 현재 상태와 다음 단계
- 계획과 완료 조건

체크포인트 항목은 이 셋에 맞췄습니다. 요구는 원문 그대로, 다음 액션, 완료 기준입니다.

**논문에서 가져오지 *않은* 것:**

- **토큰 기준값:** Claude Code는 첫 메시지 전에 이미 약 47K 토큰을 씁니다(시스템 프롬프트, 도구, CLAUDE.md). 그래서 16K–48K 한도를 그대로 쓸 수 없습니다.
- **비용 배수:** 오픈 웨이트 모델의 GPU 서빙 비용 추정이라 Claude API 요금과는 다릅니다. Claude는 프롬프트 캐시 덕분에 긴 컨텍스트를 다시 읽는 비용이 이미 낮습니다.

## 3. Claude Code 공식 안내

**[Claude Code 문서: context window](https://code.claude.com/docs/en/context-window)**

> run `/clear` when switching to unrelated work. Old conversation crowds out the files you need next and costs tokens on every message.

> Claude Code compacts automatically as you approach the limit

문서는 언제 정리하는 게 가장 좋은지는 말하지 않습니다. 그 답은 아래 4절의 실측으로 정했습니다.

## 4. 실제 세션에서 잰 값

압축과 체크포인트 수치는 한 사용자의 30일치 Claude Code 기록에서, 마지막 두 줄은 디스크에 있는 기록 83개 전체에서 나왔습니다. 토큰 수와 항목 종류만 봤고 내용은 읽지 않았습니다. 플러그인을 만든 사람과 같은 사용자의 기록이라, 표본 하나입니다.

| 측정 | 결과 |
|---|---|
| 자동 압축 | 28건 |
| 300K 토큰(1M 창의 30%) 전에 일어난 압축 | 28건 중 21건 |
| 압축 시점의 최소값과 중간값 | 201,443과 약 247K 토큰 |
| 체크포인트 없이 압축된 경우: 간격 10% | 28건 중 3건 |
| 체크포인트 없이 압축된 경우: 간격 5%(기본값) | 28건 중 0건 |
| 체크포인트로 늘어난 토큰: 간격 10% / 5% / 3% | 1.1% / 1.8% / 2.6% |
| 모델이 일하는 도중 입력해서 이전 버전이 기록하지 못한 메시지 | 43건 |
| 세션이 끝날 때 아직 도는 것으로 남은 백그라운드 서브에이전트 | 기록 83개 중 0건 |

**체크포인트 간격이 5%인 이유:** 이 기록에서는 컨텍스트가 다 차기 한참 전에 압축이 일어나는 경우가 많았습니다. 간격을 5%로 두면 모든 압축 직전에 되살릴 체크포인트가 있었고, 늘어난 토큰은 2%가 안 됐습니다.

**그래도 `/clear` 권유를 30%에 두는 이유:** 권유가 압축보다 먼저 나온 경우는 21%뿐이었습니다. 18%로 낮추면 전부 먼저 나옵니다. 그래도 기본값을 30%로 둔 건, `/clear`를 너무 일찍 권하면 아직 쓸모 있는 맥락까지 끊기기 때문입니다. 권유가 늦는 경우는 체크포인트와 압축 후 복원이 보완합니다. 일찍 정리하는 쪽이 좋으면 `clear_recommend_pct`를 낮추세요.

## 확인하지 못한 주장

이 주제의 요약 글에 자주 나오지만 인용된 출처에서 찾지 못한 주장입니다. 이 플러그인의 근거로 쓰지 않습니다.

- **"컨텍스트 40% 안팎에서 인계하라":** 논문에도 Anthropic 문서에도 없습니다.
- **"압축하는 시점에 모델은 가장 멍청한 상태다(least intelligent point)":** Claude Code 문서와 Anthropic 엔지니어링 글 어디에서도 이 표현을 찾지 못했습니다.
- **"비용 2.2배~233배 절감":** 논문 뒷부분에 나올 수 있지만 원문 문장으로 확인하지 못했습니다. 논문도 비용 수치를 추정치라고 밝힙니다.
- **"16K–36K(약 30~40%)":** 단위 오류입니다. 16K–36K는 1M 창의 약 2~4%입니다.
- **"Brainy Papers" 실험(`/compact` 뒤 세부 사항 6가지가 모두 사라짐):** 출처를 찾지 못했습니다.
