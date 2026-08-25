import { useState } from "react";
import { ask } from "../api";
import type { AskResponse } from "../types";

interface Props {
  novelId: string;
  at: number;
}

export function AskBox({ novelId, at }: Props) {
  const [question, setQuestion] = useState("");
  const [response, setResponse] = useState<AskResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!question.trim()) return;
    setPending(true);
    setError(null);
    try {
      // Same `at` as the reader pane — asking is gated identically to reading, not to
      // some separately-tracked client value.
      setResponse(await ask(novelId, question, at));
    } catch (err) {
      setError(String(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="ask-box">
      <form onSubmit={submit}>
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask about what you've read so far…"
        />
        <button type="submit" disabled={pending}>
          {pending ? "Asking…" : "Ask"}
        </button>
      </form>
      {error && <p className="ask-box-error">{error}</p>}
      {response && (
        <div className="ask-box-answer">
          <p>{response.answer}</p>
          {/* Rendered plainly and visibly, not tucked away — the whole point of this
              field is to make the spoiler gate visible (PLAN.md §5.4). */}
          {response.retrieved_sources.length > 0 && (
            <p className="ask-box-sources">
              Drew from:{" "}
              {response.retrieved_sources
                .map((source) => `chapter ${source.chapter} (${source.kind})`)
                .join(", ")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
