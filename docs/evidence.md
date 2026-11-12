# Why leneu-session works this way

한국어: [evidence-ko.md](evidence-ko.md)

This page lists what the design rests on. Each external claim is quoted from the source and was checked on 2026-11-12. Claims that circulate online but could not be found in a source are listed separately, so they don't get repeated as fact.

## 1. Long context degrades the model

**Anthropic, [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)** (2025-09-29)

> LLMs have an "attention budget" that they draw on when parsing large volumes of context.

> Every new token introduced depletes this budget by some amount.

The article calls this *context rot* and describes two remedies this plugin uses: compaction, and "structured note-taking", where "the agent regularly writes notes persisted to memory outside of the context window".

**Chroma, [Context Rot](https://www.trychroma.com/research/context-rot)** (technical report, 2025-07-14). The report evaluated 18 models, including GPT-4.1, Claude 4, Gemini 2.5 and Qwen3.

> model performance consistently degrades with increasing input length.

**What leneu-session takes from this:** keep the live context small, and keep the important state in files instead of in the conversation. The checkpoints are those notes.

## 2. Short context with handoffs costs less and loses little

**Belcak et al., [You don't need long context](https://arxiv.org/abs/2611.05306)** (arXiv:2611.05306, 2026-11)

> frontier language models sacrifice little to no task accuracy when maintaining short contexts.

> the disparity in cost may rise to as much as 7x.

The 7x compares the cost per generated token when serving open-weight models at a 256K context window instead of 16K. The paper gives 3.6x to 7x depending on the model. It also notes:

> Their numerical boundaries are rough rules of thumb.

That sentence is about its 16K–50K definition of "short context". The paper's compaction method keeps a near-verbatim copy of the task, the current state with next steps, and the plan with its completion requirements (Appendix A, as summarized; not quoted). The checkpoint sections mirror these three parts: requirements quoted verbatim, next actions, and done criteria.

**What leneu-session does *not* take from the paper:**

- **Its token thresholds.** Claude Code uses about 47K tokens before the first message (system prompt, tools, CLAUDE.md), so a 16K–48K limit can't be applied as written.
- **Its cost multipliers.** Those come from GPU serving estimates for open-weight models, not from Claude's API pricing. On Claude, prompt caching already makes rereading a long context cheap.

## 3. Claude Code's own guidance

**[Claude Code docs: context window](https://code.claude.com/docs/en/context-window)**

> run `/clear` when switching to unrelated work. Old conversation crowds out the files you need next and costs tokens on every message.

> Claude Code compacts automatically as you approach the limit

The docs don't say when compaction is best. Section 4 below covers that with measurements.

## 4. Measured on real sessions

The compaction and checkpoint numbers come from one user's Claude Code transcripts over 30 days, and the last two rows from all 83 transcripts on disk. Only token counts and entry types were read, not content. The scripts and the user are the same as in the plugin's development, so this is a sample of one.

| Measurement | Result |
|---|---|
| Automatic compactions | 28 |
| Compactions that fired before 300K tokens (30% of a 1M window) | 21 of 28 |
| Smallest and median size when compaction fired | 201,443 and about 247K tokens |
| Compactions with no checkpoint before them: checkpoints every 10% | 3 of 28 |
| Compactions with no checkpoint before them: checkpoints every 5% (default) | 0 of 28 |
| Extra tokens spent on checkpoints, every 10% / 5% / 3% | 1.1% / 1.8% / 2.6% |
| Prompts typed while Claude was working, which earlier versions failed to log | 43 |
| Background subagents still marked running at the end of a session | 0 of 83 transcripts |

**Why checkpoints every 5%.** In this sample, compaction often fired well before the context filled. With checkpoints every 5%, every compaction had a recent checkpoint to restore. The cost was under 2% more tokens.

**Why `/clear` is still recommended at 30%.** The recommendation came before compaction in only 21% of cases. Lowering it to 18% would have caught all of them. The default stays at 30% because recommending `/clear` too early cuts off useful context. The checkpoints and the compaction restore cover the remaining cases. Lower `clear_recommend_pct` if you'd rather clear early.

## Claims we could not verify

These claims appear in summaries of this topic, but we couldn't find them in the cited sources. They are not used here.

- **"Hand off at about 40% of the context."** This is not in the paper or in Anthropic's docs.
- **"The model is at its least intelligent point when compacting."** We found this wording in neither the Claude Code docs nor the Anthropic engineering article.
- **"2.2x to 233x cost savings."** This may come from the paper's later sections. We couldn't confirm it verbatim, and the paper calls its cost figures estimates.
- **"16K–36K (about 30–40%)."** This is a unit error. 16K–36K is about 2–4% of a 1M window.
- **A "Brainy Papers" test where six code details were all lost after `/compact`.** We couldn't find the source.
