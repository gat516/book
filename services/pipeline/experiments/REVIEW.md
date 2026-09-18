# Chapter 1 experiment review — 2026-09-17

Follow-up: [Qwen experiment and source review](QWEN-REVIEW.md). Qwen completed
reader-memory v4 with nine compound notes; a same-prompt compact-v2 comparison
exhausted the output ceiling. Neither result is approved for integration.

No candidate is approved for integration. Production remains paused. This review
concerns extraction only; it does not establish that identity resolution, English
rendering, or a complete experimental pipeline works.

All completed comparisons below used the book's Groq `openai/gpt-oss-120b` model
on the same chapter. Counts are output items, not independently verified facts.
Source hash: `97c2f994f1e68bd6d0c0c3907741316a7c67988ccc0197e3a47442487d5ea571`.

| Trial | Reasoning | Input tokens | Output tokens | Items | Review |
| --- | --- | ---: | ---: | ---: | --- |
| Earlier compact-memory trial | Low | 5,261 | 1,272 | 15 | 14 structurally accepted; citation errors and meaningful omissions |
| Compact v2 | Low | 5,222 | 1,349 | 27 | Invented name; predicted death presented as completed; too many opening actions |
| Compact v2 | Medium | 5,222 | 2,832 | 20 | Factual errors persist; all 20 omit a required XML attribute |
| Quote-first v3 | Medium | Unknown | Unknown | Unavailable | Provider raised output-limit error at the 4,096-token ceiling |
| Quote-first v3 | Low | 5,259 | 1,703 | 8 | Quotes match, but claims still distort meaning and omit later developments |
| Reader-memory v4 | Medium | — | — | — | Deferred before completion by provider quota; not evaluated |

The original production atomic discovery returned 77 claims at 5,140 input and
2,787 output tokens, then required another selection call. Smaller extraction
outputs can remove that extra work, but none of these experiments yet establishes
acceptable quality. More reasoning more than doubled v2 output tokens, without
resolving the substantive problems. One sample per configuration is diagnostic,
not a reliable model ranking.

## Specific failures checked against the chapter

**Compact v2, low:** Changes the source name 凌峰 into 凌飛, apparently mixing it
with the alias 龍飛. Describes Ares as a corpse and his destruction as completed,
where the chapter predicts his death after he loses his escape token. The opening
encounter still occupies 12 entries. Different gift recipients are conflated.

**Compact v2, medium:** Names improve, but c12 says Hawkins offers a low-price
exchange and help inside the trial. Passage p133 actually offers to sell Ling
Feng's treasures without taking any commission. c10 substitutes unrestricted use
for the specific benefit of concealing the golden armor's aura. c8 adds a
significant combat improvement that p080 does not establish. c7 drops the condition
that Freya must understand the completed tablet's mysteries. Important faction
and communication context is omitted. The 20 XML items exist, even though the
validator rejects all of them for missing `context`; fixing that format locally
would not fix their factual errors.

**Quote-first v3, low:** All quoted strings occur in their cited passages. This
does not mean their paraphrases are supported. Entry 3 turns Ares's intention to
ignore the wager after escaping into a claim that entering the portal would cancel
all wagers. Entry 7 combines Freya's tablet, Saliye's summoning plate, and Ling
Feng's lotus gift, leaving the actual gift recipient unclear and adding a reciprocal
gift framing. Five of eight entries cover the opening encounter. The response
misses the armor's concealment, aid pledges, Yan's identifier, the crystal orb and
communications faction, the commission waiver, cultivation trade intentions,
and the final comparison of faction hierarchies.

## Next experiment and acceptance

`prompts/memory-v4.txt` tests a different output unit: a compact reader note can
group tightly related facts about one consequential development. It asks for
whole-chapter coverage and approximately 12–15 notes without treating the count
as a quota. It uses a shorter Chinese prompt and a simple JSON shape. This changes
several aspects deliberately; success would identify a candidate, not isolate
which change caused it.

The provider deferred the first v4 attempt: 200,000-token allowance, 197,059 used,
5,416 requested, retry after 1,070 seconds. The request is saved; there is no automatic
retry loop. Failed/truncated requests may also consume tokens; missing usage is
unknown, not zero.

Next steps after cooldown:

1. Run the saved v4 command, review each note against its cited passages, and
   inspect omissions across the entire chapter.
2. Show the owner readable output and actual cost. A count near 15 is insufficient
   if facts are wrong or the last half of the chapter is missing.
3. If a candidate is promising, test it on an unseen chapter. Chapter 1 has guided
   prompt changes and cannot establish generalization.
4. Exercise the necessary downstream stages locally and inspect the combined
   results and total token cost before proposing production integration.
5. Integrate only after the owner is satisfied with the experimental behavior.

Exact requests, raw responses, token usage, and structural checks are in the ignored
`results/` directory. Those artifacts contain chapter text. Semantic findings here
are manual assistant review against source, not an automated accuracy score.
