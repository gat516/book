"""Native NuExtract compiler probe; preserves LLMProvider calls and source provenance (§5.4)."""
import asyncio
import json

import httpx
from novel_llm import OllamaProvider
import two_pass_story_facts as harness


INSTRUCTIONS = """任务：根据原文章节核查并整理候选事实。只以source_passages为证据。
untrusted_analyst_notes只是检索线索，其中的分类、解释、引号、人物归属和段落编号均可能错误。
必须重新在原文定位证据，可以更正候选的错误编号；不要把候选中的引号当作原文。
每个c槽位最多一条原子事实。候选只有部分受支持时仅保留那一部分；没有可靠内容则整个槽位为null。
value和rationale用中文；subject和surface逐字保留原文名称；predicate用简短英文snake_case属性或关系。
value是该属性的值或关系宾语，不是整段情节；rationale只说明引用段落如何支持此事实。
evidence引用足以支持全部陈述的段落，不能因为人物在某段出现就引用它。不得生成原文之外的段落ID。
subject是唯一主体。linked_entities只列该事实实际涉及的其他具名参与者，不重复主体。
role描述另一个参与者在事实中的作用，如cause、target、group、opponent；没有关系就空数组。
人物说自己与他人合作，仅证明他说了这句话，不证明合作成立；意图、建议、承诺不能变成已完成事件。
叙述者向读者揭露秘密，不等于人物公开揭露，也不等于其他人物知道。保留否定和知情主体。
一次情绪或一次选择不证明性格发生持久变化；没有前后对比不要用development。
epistemic_status：explicit=原文直接陈述；strongly_implied=具体线索支持的推断；ambiguous=保留多种解释。
temporal_status：current=当前状态；began/ended=状态开始/结束；historical=此前事件；instantaneous=瞬时动作；ongoing=持续动作；unclear=不明。
importance：crucial=重大情节或状态后果；significant=有用的持久事实；characterization=描述性刻画。不要求分布均匀。
所有分类必须从template的选项中选；不是原文明确内容就不要补充。kind采用最贴近事实的类别。
"""


def extraction_template(schema):
    """Translate this harness's inline schema to NuExtract's documented template dialect."""
    if "anyOf" in schema:
        return extraction_template(next(s for s in schema["anyOf"] if s.get("type") != "null"))
    if "enum" in schema:
        return schema["enum"]
    kind = schema["type"]
    if kind == "object":
        return {key: extraction_template(value) for key, value in schema["properties"].items()}
    if kind == "array":
        return [extraction_template(schema["items"])]
    if kind == "string":
        return "string"
    raise ValueError(f"Unsupported native template type: {kind}")


def native_payload(payload):
    schema = payload.get("format")
    if not isinstance(schema, dict):
        raise ValueError("Native pass 2 requires an explicit schema")
    # NuExtract's template handles names as verbatim extraction. IDs remain offered enums.
    template = extraction_template(schema)
    for slot in template.values():
        slot["subject"] = "verbatim-string"
        slot["linked_entities"][0]["surface"] = "verbatim-string"
    documents = [m["content"].split("\nRequired output schema:\n")[0]
                 for m in payload["messages"] if m["role"] == "user"]
    # The generic harness's English guidance is replaced by a single language contract.
    document = "\n".join(documents).split("\n" + harness.FIELD_GUIDANCE)[0]
    return {**payload, "messages": [
        {"role": "template", "content": json.dumps(template, ensure_ascii=False)},
        {"role": "instructions", "content": INSTRUCTIONS},
        {"role": "user", "content": document}]}


class NativeClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []

    def build_request(self, method, url, **kwargs):
        if str(url) == "/api/chat" and "json" in kwargs:
            kwargs["json"] = native_payload(kwargs["json"])
            self.requests.append(kwargs["json"])
        return super().build_request(method, url, **kwargs)


async def run(args):
    original = harness.OllamaProvider
    instances = []

    class Provider(OllamaProvider):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.old_client = self._client
            self._client = NativeClient(base_url=kwargs["host"], timeout=self.old_client.timeout)
            instances.append(self)

    harness.OllamaProvider = Provider
    try:
        result = await harness.run(args)
        result["experiment"] = "nuextract_native_pass2_v1"
        result["native_requests"] = [request for provider in instances for request in provider._client.requests]
        # Chinese output is intentional here; generic harness counters are retained as observations.
        result["output_language"] = "source"
        result["quality"]["needs_review"] = True
        result["quality"]["language_note"] = "Chinese value/rationale is expected; CJK counts are not errors in native mode. Semantic review remains required."
        return result
    finally:
        harness.OllamaProvider = original
        for provider in instances:
            await provider._client.aclose()
            await provider.old_client.aclose()


def main():
    parser = harness.parser()
    parser.set_defaults(model="numind/nuextract3:Q6_K", host="http://127.0.0.1:11436",
                        first_token_timeout=60, idle_timeout=40, total_timeout=90)
    args = parser.parse_args()
    if not args.replay_analysis or args.minimal or args.source_only or args.decoding != "schema":
        parser.error("Use --replay-analysis with the classified schema mode")
    result = asyncio.run(run(args))
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"claims": len(result["claims"]), "rejected": len(result["rejected_claims"]),
                      "quality": result["quality"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
