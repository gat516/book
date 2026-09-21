import { lazy, Suspense, useState } from "react";
import { ask } from "../api";
import type { AskResponse } from "../types";

// Load Markdown support only when an answer arrives, keeping chapter startup small.
const AskAnswer = lazy(() => import("./AskAnswer").then(module => ({ default: module.AskAnswer })));

interface Props {
  novelId: string;
  at: number;
}

export function AskBox({ novelId, at }: Props) {
  const [question, setQuestion] = useState("");
  const [response, setResponse] = useState<AskResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [answeredQuestion, setAnsweredQuestion] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const submittedQuestion = question.trim();
    if (!submittedQuestion || pending) return;
    setPending(true);
    setError(null);
    setResponse(null);
    try {
      // Same `at` as the reader pane — asking is gated identically to reading, not to
      // some separately-tracked client value.
      setResponse(await ask(novelId, submittedQuestion, at));
      setAnsweredQuestion(submittedQuestion);
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
          aria-label="Ask about the story"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask about what you've read so far…"
        />
        <button type="submit" disabled={pending || !question.trim()}>
          {pending ? "Asking…" : "Ask"}
        </button>
      </form>
      {pending && <p className="ask-box-pending" role="status">Looking through your chapters…</p>}
      {error && <p className="ask-box-error" role="alert">{error}</p>}
      {response && <Suspense fallback={<p className="ask-box-pending" role="status">Formatting answer…</p>}><AskAnswer response={response} question={answeredQuestion} /></Suspense>}
    </div>
  );
}
