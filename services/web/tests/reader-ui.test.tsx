import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import React, { useState } from 'react';
import { JSDOM } from 'jsdom';
import type { ChapterResponse, TermRenderingView, WikiPageSummary } from '../src/types';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/', pretendToBeVisual: true });
for (const key of ['window', 'document', 'HTMLElement', 'Element', 'Node', 'ShadowRoot', 'MutationObserver', 'getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame']) {
  Object.defineProperty(globalThis, key, { configurable: true, value: (dom.window as any)[key] });
}
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const { render, fireEvent, waitFor, cleanup, act } = await import('@testing-library/react');
const { HoverCard } = await import('../src/components/HoverCard');
const { MentionPopover } = await import('../src/components/MentionPopover');
const { ReadingDesk, StoryCompanion } = await import('../src/components/ReadingDesk');
const { WikiView } = await import('../src/components/WikiView');
const { wikiPageForTerm } = await import('../src/wikiNavigation');
const { AnswerMarkdown } = await import('../src/components/AnswerMarkdown');
const { AskBox } = await import('../src/components/AskBox');
const { AskAnswer } = await import('../src/components/AskAnswer');
const originalFetch = globalThis.fetch;
afterEach(() => { cleanup(); globalThis.fetch = originalFetch; });
const rendering: TermRenderingView = { source_term: '梅', target_term: 'Mei', status: 'unlocked', term_role: 'chinese_person', candidates: [] };
const wiki: WikiPageSummary = { subject: 'mei-id', source_term: '梅', title: 'Mei', kind: 'character', facts: 2 };
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

for (const state of [
  { name: 'loading', loading: true, error: null, expected: /Opening your story wiki/ },
  { name: 'empty', loading: false, error: null, expected: /A world unfolding/ },
  { name: 'failed', loading: false, error: 'Unavailable', expected: /Could not load the wiki/ },
]) {
  test(`companion renders its ${state.name} state before any wiki subject exists`, () => {
    const view = render(<StoryCompanion novelId="book" at={1} pages={[]} loading={state.loading}
      error={state.error} revision={0} onRetry={() => {}} onOpenWiki={() => {}} />);
    assert.match(view.container.textContent!, state.expected);
  });
}

test('opening a chapter keeps its prose visible while the wiki loads and when no pages exist', async () => {
  let finishWiki!: (response: Response) => void;
  const chapter: ChapterResponse = { novel_id: 'book', chapter_index: 1, at: 8,
    text: 'Mei walked along the river.', spans: [{ char_start: 0, char_end: 3, rendering }],
    has_next: true, translation_warning: null, part: 1 };
  const requests: string[] = [];
  globalThis.fetch = async url => {
    requests.push(String(url));
    if (String(url).endsWith('/chapter/1')) return json(chapter);
    if (String(url).endsWith('/translation-health')) return json({ warn: false });
    if (String(url).endsWith('/wiki/pages?at=1')) return new Promise(resolve => { finishWiki = resolve; });
    throw new Error(`Unexpected request: ${url}`);
  };
  function Harness() {
    const [loaded, setLoaded] = useState<ChapterResponse | null>(null);
    return <ReadingDesk novelId="book" chapterIndex={1} chapter={loaded} clickableEntities
      onChapterLoaded={setLoaded} onNoChapter={() => {}} onNavigate={async () => {}}
      onFindMore={async () => {}} onChapters={() => {}} onAdd={() => {}} onOpenWiki={() => {}} />;
  }
  const view = render(<Harness />);
  await waitFor(() => assert.match(view.getByRole('article', { name: 'Chapter 1' }).textContent!, /Mei walked along the river/));
  assert.match(view.getByRole('complementary').textContent!, /Opening your story wiki/);
  await act(async () => finishWiki(json({ novel_id: 'book', at: 1, pages: [] })));
  assert.match(view.getByRole('article').textContent!, /Mei walked along the river/);
  assert.match(view.getByRole('complementary').textContent!, /A world unfolding/);
  assert.ok(requests.includes('/api/novels/book/wiki/pages?at=1'));
  fireEvent.click(view.getByRole('button', { name: 'Inspect Mei' }));
  assert.ok(await view.findByRole('dialog'));
  fireEvent.click(view.getByRole('button', { name: 'Close name card' }));
  await waitFor(() => assert.equal(view.queryByRole('dialog'), null));
});

test('wiki navigation uses a unique saved source key, never the translated spelling', () => {
  const different = { ...wiki, subject: 'other-id', source_term: '美' };
  assert.equal(wikiPageForTerm([different, wiki], rendering)?.subject, 'mei-id');
  assert.equal(wikiPageForTerm([different], rendering), undefined);
  assert.equal(wikiPageForTerm([wiki, { ...wiki, subject: 'duplicate' }], rendering), undefined);
  assert.equal(wikiPageForTerm([wiki], undefined), undefined);
});

test('confirmation closes only after a successful save and keeps the authorized write chapter', async () => {
  let resolve!: (value: Response) => void;
  let body: any;
  globalThis.fetch = async (_url, init) => { body = JSON.parse(init!.body as string); return new Promise(r => { resolve = r; }); };
  let closed = 0;
  let updated: TermRenderingView | undefined;
  const view = render(<HoverCard novelId="book" mention="Mei" rendering={rendering} at={8} wikiAt={2}
    onClose={() => closed++} onRenderingChanged={next => { updated = next; }} />);
  fireEvent.click(view.getByRole('button', { name: 'Confirm “Mei”' }));
  assert.equal(closed, 0);
  assert.equal(body.at_chapter, 8);
  assert.equal(view.getByRole('button', { name: 'Saving…' }).hasAttribute('disabled'), true);
  await act(async () => resolve(json({})));
  assert.equal(closed, 1);
  assert.equal(updated?.status, 'locked');
  assert.match(view.container.textContent!, /through chapter 2/);
});

test('failed spelling saves stay open and show an actionable error', async () => {
  globalThis.fetch = async () => json({ error: 'Please retry the save' }, 503);
  let closed = 0;
  const view = render(<HoverCard novelId="book" mention="Mei" rendering={rendering} at={2} onClose={() => closed++} />);
  fireEvent.click(view.getByRole('button', { name: 'Confirm “Mei”' }));
  await waitFor(() => assert.match(view.getByRole('alert').textContent!, /Please retry/));
  assert.equal(closed, 0);
  assert.equal(view.getByRole('button', { name: 'Confirm “Mei”' }).hasAttribute('disabled'), false);
});

test('correcting a confirmed spelling saves and closes the card', async () => {
  let body: any;
  globalThis.fetch = async (_url, init) => { body = JSON.parse(init!.body as string); return json({}); };
  let closed = 0;
  const view = render(<HoverCard novelId="book" mention="Mei" rendering={{ ...rendering, status: 'locked' }} at={2} onClose={() => closed++} />);
  fireEvent.click(view.getByRole('button', { name: 'Change spelling' }));
  fireEvent.change(view.getByLabelText('Preferred spelling'), { target: { value: 'May' } });
  fireEvent.submit(view.getByLabelText('Preferred spelling').closest('form')!);
  await waitFor(() => assert.equal(closed, 1));
  assert.equal(body.target_term, 'May');
});

test('wiki shortcut dismisses the card before opening the canonical subject', () => {
  const events: string[] = [];
  const view = render(<HoverCard novelId="book" mention="Mei" rendering={rendering} at={2} wikiPage={wiki}
    onClose={() => events.push('close')} onOpenWiki={id => events.push(id)} />);
  fireEvent.click(view.getByRole('button', { name: /Open character wiki/ }));
  assert.deepEqual(events, ['close', 'mei-id']);
});

test('popover opens by click, stays during editing, and dismisses with Escape or outside click', async () => {
  function Harness() {
    const [open, setOpen] = useState(false);
    return <><button>Outside</button><MentionPopover novelId="book" mention="Mei" rendering={rendering} at={2}
      hoverEnabled open={open} onOpenChange={setOpen} onRenderingChanged={() => {}} /></>;
  }
  const view = render(<Harness />);
  fireEvent.click(view.getByRole('button', { name: 'Inspect Mei' }));
  assert.ok(await view.findByRole('dialog'));
  fireEvent.click(view.getByRole('button', { name: 'Change spelling' }));
  const input = view.getByLabelText('Preferred spelling');
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: 'May' } });
  assert.ok(view.getByRole('dialog'));
  fireEvent.keyDown(input, { key: 'Escape' });
  await waitFor(() => assert.equal(view.queryByRole('dialog'), null));
  fireEvent.click(view.getByRole('button', { name: 'Inspect Mei' }));
  assert.ok(await view.findByRole('dialog'));
  fireEvent.pointerDown(view.getByRole('button', { name: 'Outside' }));
  await waitFor(() => assert.equal(view.queryByRole('dialog'), null));
});

test('wiki deep link opens the correct non-character shelf and bounds every request', async () => {
  const requests: string[] = [];
  const place = { ...wiki, subject: 'tower', title: 'Lantern Tower', kind: 'place' };
  globalThis.fetch = async (url) => {
    requests.push(String(url));
    return json(String(url).includes('/pages/tower')
      ? { novel_id: 'book', at: 2, subject: 'tower', title: 'Lantern Tower', kind: 'place', facts: [], names: {}, kinds: {} }
      : { novel_id: 'book', at: 2, pages: [wiki, place] });
  };
  const view = render(<WikiView novelId="book" at={2} initialSubject="tower" onClose={() => {}} />);
  await waitFor(() => assert.ok(view.getByRole('heading', { name: 'Lantern Tower' })));
  assert.equal(view.getByRole('tab', { name: 'Places' }).getAttribute('aria-selected'), 'true');
  assert.ok(requests.every(url => url.endsWith('?at=2')));
  assert.equal(requests.some(url => url.includes('/mei-id')), false);
});

test('companion discards a late page response when the reader moves back a chapter', async () => {
  let late!: (response: Response) => void;
  const requests: string[] = [];
  const page = (at: number, text: string) => ({ novel_id: 'book', at, subject: 'mei-id', title: 'Mei', kind: 'character', facts: [{ chapter: at, text, version: 'v', ordinal: 0 }], names: {}, kinds: {} });
  globalThis.fetch = async url => {
    requests.push(String(url));
    return String(url).endsWith('?at=3') ? new Promise(resolve => { late = resolve; }) : json(page(1, 'Mends ropes'));
  };
  const props = { novelId: 'book', pages: [wiki], loading: false, error: null, revision: 0, onRetry() {}, onOpenWiki() {} };
  const view = render(<StoryCompanion key="3" {...props} at={3} />);
  view.rerender(<StoryCompanion key="1" {...props} at={1} />);
  await waitFor(() => assert.match(view.container.textContent!, /Mends ropes/));
  await act(async () => late(json(page(3, 'Secret lantern keeper'))));
  assert.doesNotMatch(view.container.textContent!, /Secret lantern keeper/);
  assert.deepEqual(requests, ['/api/novels/book/wiki/pages/mei-id?at=3', '/api/novels/book/wiki/pages/mei-id?at=1']);
});

test('AI answers render paragraphs, lists, emphasis, tables, and chapter citations', () => {
  const answer = 'Here is what we know.\n\n- **Identity:** Ling Feng is his name. [chunk:2108 ch:5]\n- **Relationships:** He has a disciple. [chunk:2134 ch:4][chunk:2136 ch:4]\n\n## Details\n\n| Name | Role |\n| --- | --- |\n| Ling Feng | Teacher |';
  const view = render(<AnswerMarkdown answer={answer} at={39} sources={[
    { kind: 'chunk', id: 2108, chapter: 5 }, { kind: 'chunk', id: 2134, chapter: 4 }, { kind: 'chunk', id: 2136, chapter: 4 },
  ]} />);
  assert.equal(view.getAllByRole('listitem').length, 2);
  assert.equal(view.container.querySelector('strong')?.textContent, 'Identity:');
  assert.ok(view.getByRole('heading', { name: 'Details' }));
  assert.ok(view.getByRole('table'));
  assert.equal(view.getByTitle('Source: chapter 5').textContent, 'Ch. 5');
  assert.equal(view.getByTitle('Source: chapter 4').textContent, 'Ch. 4');
  assert.doesNotMatch(view.container.textContent!, /chunk:|\*\*Identity/);
});

test('answer rendering rejects HTML and remote images and does not invent source provenance', () => {
  const answer = '<script>alert("bad")</script>\n\n<img src="https://example.test/leak" onerror="alert(1)">\n\n![tracking](https://example.test/pixel)\n\n[unsafe](javascript:alert%281%29) [external](https://example.test)\n\nKnown [chunk:1 ch:2]. Fabricated [chunk:999 ch:2]. Future [chunk:3 ch:8]. Literal `[chunk:1 ch:2]`.';
  const view = render(<AnswerMarkdown answer={answer} at={2} sources={[
    { kind: 'chunk', id: 1, chapter: 2 }, { kind: 'chunk', id: 3, chapter: 8 },
  ]} />);
  assert.equal(view.container.querySelector('script, img, iframe, a'), null);
  assert.equal(view.getAllByText('Source unavailable').length, 2);
  assert.equal(view.getByTitle('Source: chapter 2').textContent, 'Ch. 2');
  assert.equal(view.container.querySelector('code')?.textContent, '[chunk:1 ch:2]');
});

test('formatted answers expand without another request and close with Escape or outside click', async () => {
  const view = render(<AskAnswer question="Who is Ling Feng?" response={{ at: 5, answer: '**Ling Feng** is a teacher.',
    retrieved_sources: [{ kind: 'chunk', id: 1, chapter: 4 }, { kind: 'chunk', id: 2, chapter: 4 }, { kind: 'chunk', id: 3, chapter: 5 }], served_by: null }} />);
  assert.equal(view.getByRole('region', { name: 'AI answer' }).tabIndex, 0);
  assert.ok(view.getByText('Sources: chapters 4, 5'));
  const expand = view.getByRole('button', { name: 'Expand answer' });
  fireEvent.click(expand);
  const dialog = await view.findByRole('dialog', { name: 'Ask the story' });
  assert.equal(dialog.querySelector('strong')?.textContent, 'Ling Feng');
  assert.match(dialog.textContent!, /Who is Ling Feng/);
  fireEvent.keyDown(view.getByRole('button', { name: 'Close expanded answer' }), { key: 'Escape' });
  await waitFor(() => assert.equal(view.queryByRole('dialog'), null));
  await waitFor(() => assert.equal(document.activeElement, expand));
  fireEvent.click(expand);
  assert.ok(await view.findByRole('dialog'));
  fireEvent.pointerDown(document.querySelector('.ask-answer-overlay')!);
  await waitFor(() => assert.equal(view.queryByRole('dialog'), null));
});

test('Ask AI keeps the submitted question with its answer and clears it for a new request', async () => {
  const requests: any[] = [];
  let finish!: (response: Response) => void;
  globalThis.fetch = async (_url, init) => { requests.push(JSON.parse(init!.body as string)); return new Promise(resolve => { finish = resolve; }); };
  const view = render(<AskBox novelId="book" at={5} />);
  const input = view.getByRole('textbox', { name: 'Ask about the story' });
  assert.equal(view.getByRole('button', { name: 'Ask' }).hasAttribute('disabled'), true);
  fireEvent.change(input, { target: { value: 'Who is Ling Feng?' } });
  fireEvent.submit(input.closest('form')!);
  assert.ok(view.getByRole('status'));
  fireEvent.submit(input.closest('form')!);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].at, 5);
  fireEvent.change(input, { target: { value: 'What does he own?' } });
  await act(async () => finish(json({ at: 5, answer: '**Ling Feng** is a teacher.', retrieved_sources: [], served_by: null })));
  assert.match(view.getByRole('region', { name: 'AI answer' }).textContent!, /Who is Ling Feng/);
  assert.doesNotMatch(view.getByRole('region', { name: 'AI answer' }).textContent!, /What does he own/);
  fireEvent.submit(input.closest('form')!);
  assert.equal(view.queryByRole('region', { name: 'AI answer' }), null);
  await act(async () => finish(json({ error: 'Try again shortly' }, 503)));
  assert.match(view.getByRole('alert').textContent!, /Try again shortly/);
});
