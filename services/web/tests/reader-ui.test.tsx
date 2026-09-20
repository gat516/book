import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import React, { useState } from 'react';
import { JSDOM } from 'jsdom';
import type { TermRenderingView, WikiPageSummary } from '../src/types';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/', pretendToBeVisual: true });
for (const key of ['window', 'document', 'HTMLElement', 'Element', 'Node', 'ShadowRoot', 'MutationObserver', 'getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame']) {
  Object.defineProperty(globalThis, key, { configurable: true, value: (dom.window as any)[key] });
}
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const { render, fireEvent, waitFor, cleanup, act } = await import('@testing-library/react');
const { HoverCard } = await import('../src/components/HoverCard');
const { MentionPopover } = await import('../src/components/MentionPopover');
const { StoryCompanion } = await import('../src/components/ReadingDesk');
const { WikiView } = await import('../src/components/WikiView');
const { wikiPageForTerm } = await import('../src/wikiNavigation');
const originalFetch = globalThis.fetch;
afterEach(() => { cleanup(); globalThis.fetch = originalFetch; });
const rendering: TermRenderingView = { source_term: '梅', target_term: 'Mei', status: 'unlocked', term_role: 'chinese_person', candidates: [] };
const wiki: WikiPageSummary = { subject: 'mei-id', source_term: '梅', title: 'Mei', kind: 'character', facts: 2 };
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

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
