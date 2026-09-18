# Qwen experiment — 2026-09-17

Qwen `qwen/qwen3.8-27b`, served by the book's existing Groq provider, returned a
promising reader-memory result. It is not approved for production. These are
single chapter development trials, with no evidence yet of performance across books.

| Prompt | Reasoning | Output ceiling | Input tokens | Output tokens | Result |
| --- | --- | ---: | ---: | ---: | --- |
| Reader-memory v4 | Medium | 4,096 | 4,362 | 2,276 | 9 compound notes; valid JSON |
| Compact v2 | Medium | 4,096 | Unknown | Unknown | Provider raised `TruncatedOutput` |

The completed reader-memory request took 5.41 seconds. Nine notes contain multiple
propositions each: this is not evidence that a downstream atomic record representation
would contain only nine records. The harness checks JSON parsing, not factual accuracy.

The v4 result has markedly better whole-chapter coverage than the earlier trials.
However, both prompt and model differ from the completed GPT-OSS trials. GPT-OSS's
v4 request remains quota-deferred. The second Qwen request held the v2 prompt,
reasoning setting, and output ceiling fixed against GPT-OSS's completed v2 run;
Qwen exhausted the output budget. It therefore provides no completed accuracy
comparison at that budget. Missing usage for the failed request is unknown, not zero.

## What the nine notes cover

These English descriptions summarize the returned Chinese notes for review; they
are not corrected model outputs or a proposed published translation.

1. Abandonment of Ares, transfer of dust and spear, destruction of his escape
   token, and his predicted death. The prediction is not represented as a witnessed death.
2. Ling Feng's three cultivation gains, limited present combat improvement, and
   conditional hope of defeating Taiyi.
3. Freya's recovered tablet half, Saliye's summoning plate, and the lotus gift to
   Longze Liyue, with the different beneficiaries preserved.
4. Fusion of the ancient vambrace with the golden armor, and concealment of the
   armor's aura.
5. The companions' gratitude and offers of service, plus Yan's number 117 and
   estate location.
6. Metatron's backing of Abaddon and Ling Feng's connection to the fate faction.
7. The crystal communication orb, the order faction's control of radio technology,
   and Freya's advice to remain at his estate for a year.
8. Hawkins's commission waiver, Ling Feng's surplus lotus, and cultivation-related
   reasons for trading it.
9. Abaddon's visit to Metatron, their unequal relationship, and the contrasting
   relative equality within the fate faction.

## Source-review issues still requiring improvement

- **Note 5:** Attributes willingness to serve to all three companions. The source
  explicitly gives aid pledges to An Ruosu and Chekhov; Lawrence joins the gratitude.
  “愿意效命” also overstates an offer of help. The note omits Ling Feng explicitly
  calling them friends, despite citing p098.
- **Note 8:** Changes Hawkins's waiver for this occasion (`这次`) into future sales
  generally (`今后`). Assigns Ling Feng's explanation about self-preservation to
  Hawkins as an admission. Changes “almost ineffective” lotus into “useless.”
- **Note 6:** Its cited set omits p112, the actual dialogue supporting Ling Feng's
  lack-of-choice framing. It also leaves out the fate god's name, Raziel.
- **Note 7:** Presents political control of communications as the cause of their
  lower stability; the source states the stability comparison and separately raises
  the faction concern. A causal explanation should not be added.
- **Names:** Converts many Traditional Chinese spellings to Simplified Chinese
  despite the instruction to copy source names. It preserves 凌峰 rather than
  inventing the earlier hybrid 凌飛, but exact source spelling is still not obeyed.
- **Coverage:** Omits the explicit assessment about Night Mother and the condition
  for Freya's future improvement from studying the tablet. Some notes group distinct
  developments broadly, especially the aid pledges and Yan's address.

This is the most promising reader-memory output reviewed so far, but errors in
scope and attribution remain. Next useful evidence is a completed same-prompt
comparison and an unseen-chapter check, followed by reviewing the full experimental
pipeline output with the owner. No production provider setting, worker code,
published records, or glossary was changed by these trials.

Raw local artifacts:

- `results/memory-v4-medium-80129cf4949848cc.json`
- `results/compact-v2-medium-0484531fc614f31c.json`

The result artifacts contain chapter text and remain ignored by Git.
