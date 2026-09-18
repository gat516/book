# Joint notes and local identity trial — 2026-09-17

One live Groq `qwen/qwen3.8-27b` request, reasoning disabled, 3,000 output-token
ceiling, same chapter-1 source as the baseline. No downstream linking, rendering,
database publication, or production changes. The response completed successfully.

| Work | Input | Output | Total |
| --- | ---: | ---: | ---: |
| Previous notes-only extraction (medium reasoning) | 4,362 | 2,276 | 6,638 |
| Joint extraction/local identity (no reasoning) | 4,504 | 2,590 | 7,094 |
| Previous extraction plus five identity calls | 12,505 | 5,068 | 17,573 |

Joint extraction adds 456 tokens over notes-only extraction and uses 59.6% fewer
tokens than the old extraction/identity chain. This is a raw usage comparison, not
equivalent-quality savings: prompt, reasoning, and coverage differ. English rendering,
typed ontology classification, publication, and cross-chapter resolution are untested.

The response contains nine notes and twelve declared identities, all people. It
explicitly groups 凌峰/龍飛 and 霍金斯/老金, and keeps 亞巴頓 separate from 梅塔特隆.
It does not provide the broad item/place/group inventory of the earlier 42-entity run.
Some declared characters (沙利葉 and 龍澤璃月) are not linked to any returned note.

## Structural review

The protagonist's identity lists valid source witnesses but also malformed passage IDs
such as `p67` instead of `p067`. The conservative validator rejects that entity rather
than silently rewriting citations. Eleven entity definitions pass source-witness checks;
eight notes are flagged for referencing the rejected definition. Original IDs, names,
citations, and text remain in the saved raw response. Exact substring grounding alone
does not establish identity or factual support.

## Source review

- Note 2 invents knocking Ares unconscious; the source describes gripping his neck.
- Note 3 describes Ares being torn apart as completed. The source predicts that fate.
- Note 4 omits the limited current combat gain and conditional future improvement.
  Notes omit Saliye's summoning plate and Longze Liyue's lotus gift, despite declaring
  those characters. Armor concealment is also omitted.
- Note 5 assigns the explanation of the fate god's absence to Ling Feng; Freya says it.
- Note 7 overstates gratitude and offers of help as collective allegiance, and calls
  117 Yan's estate number although the source identifies it as her number.
- Note 8 conflates the order faction's unequal hierarchy with the fate faction,
  losing the source's explicit contrast. Its statement about Abaddon's dissatisfaction
  also reaches outside the cited closing passages.
- Note 9 retains the commission-waiver/trading idea, but omits the qualification that
  low-grade lotus is almost ineffective and the specific cultivation benefits.
- Three notes are spent on the opening confrontation while consequential later
  developments are omitted. This repeats the earlier compact extraction failure mode.

## Conclusion

The experiment demonstrates that emitting local identity in the extraction response
can be cheap; it does not validate this configuration's quality. Do not promote it.
The next controlled comparison should retain this prompt/model and change only reasoning
to medium, then review coverage, attribution, citations, identity, and total usage.
Chapter 1 remains a development case; later validation needs unseen chapters and
cross-chapter candidate resolution.

Artifact: `results/joint-extraction-chapter-1/linked-memory-v1-none-f406d7f06b97c220.json`.
Four focused offline validator tests pass. No manual corrections were substituted for
the model's output and no additional provider requests were made in this trial.

## Same-prompt medium reasoning attempt

The owner requested the controlled medium-reasoning test. One request retained the
same model, prompt, chapter and 3,000-token output ceiling, changing only reasoning.
Groq rejected it before a usable completion: safe quota fields reported a 1,000-token
allowance and 1,471 requested tokens, with a 345.6-second retry hint. Usage is unknown.
No factual or identity comparison is available. Waiting alone cannot be assumed to
fix a request exceeding an allowance; no automatic retry was made.

Artifact: `results/joint-extraction-chapter-1/linked-memory-v1-medium-e65a1eff0a8e9405.json`.
