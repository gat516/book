import assert from 'node:assert/strict';
import { test } from 'node:test';
import React from 'react';
import TestRenderer, { act } from 'react-test-renderer';
import { FirstBookGuide } from '../src/components/FirstBookGuide';
import { SampleReader } from '../src/components/SampleReader';
import { LandingPage } from '../src/components/LandingPage';
import { setSession } from '../src/session';

const store = new Map<string,string>();
(globalThis as any).localStorage = { getItem: (k:string) => store.get(k) ?? null, setItem: (k:string,v:string) => store.set(k,v) };
(globalThis as any).window = { location: { search: '' }, addEventListener() {}, removeEventListener() {} };
function text(renderer: TestRenderer.ReactTestRenderer) { return JSON.stringify(renderer.toJSON()); }
function button(renderer: TestRenderer.ReactTestRenderer, label:string) {
  const content = (node: any): string => typeof node === 'string' ? node : Array.isArray(node) ? node.map(content).join('') : node?.props ? content(node.props.children) : '';
  return renderer.root.findAllByType('button').find(b => content(b.props.children).includes(label))!;
}

test('setup uses saved account state and routes each action', () => {
  setSession({ id:'owner',email:'reader@example.test',csrf_token:'test' });
  let settings = 0, created = 0, opened = 0;
  let view!: TestRenderer.ReactTestRenderer;
  act(() => { view = TestRenderer.create(<FirstBookGuide onCreate={() => created++} onSettings={() => settings++} />); });
  assert.match(text(view), /Bring the AI/);
  act(() => button(view,'Connect your provider').props.onClick());
  assert.equal(settings,1);
  act(() => view.update(<FirstBookGuide hasKey onCreate={() => created++} onSettings={() => settings++} />));
  assert.match(text(view), /Give your next story a home/);
  act(() => button(view,'Create your first book').props.onClick());
  assert.equal(created,1);
  act(() => view.update(<FirstBookGuide hasKey hasBook onCreate={() => created++} onOpen={() => opened++} />));
  assert.match(text(view), /One chapter opens a whole world/);
  act(() => button(view,'Open your book').props.onClick());
  assert.equal(opened,1);
  act(() => view.unmount());
});

test('sample knowledge and answers follow the selected chapter in both directions', () => {
  let view!: TestRenderer.ReactTestRenderer;
  act(() => { view = TestRenderer.create(<SampleReader />); });
  assert.doesNotMatch(text(view), /Was the tower’s lantern keeper/);
  act(() => button(view,'Next chapter').props.onClick());
  assert.match(text(view), /Was the tower’s lantern keeper/);
  act(() => button(view,'Ask the story').props.onClick());
  act(() => button(view,'What have we learned about Mei?').props.onClick());
  assert.match(text(view), /Mei once kept the tower’s light/);
  act(() => button(view,'Previous').props.onClick());
  assert.doesNotMatch(text(view), /Mei once kept the tower’s light/);
  act(() => button(view,'Character wiki').props.onClick());
  assert.doesNotMatch(text(view), /Was the tower’s lantern keeper/);
  act(() => view.unmount());
});

test('invitation is retained on Google login links and private beta is explained', () => {
  (globalThis as any).window.location.search = '?invite=sample%2Btoken';
  let view!: TestRenderer.ReactTestRenderer;
  act(() => { view = TestRenderer.create(<LandingPage />); });
  const links = view.root.findAllByType('a').filter(a => a.props.href.startsWith('/api/auth/login'));
  assert.ok(links.length > 0);
  assert.ok(links.every(a => a.props.href === '/api/auth/login?invite=sample%2Btoken'));
  assert.match(text(view), /New accounts need an invitation/);
  act(() => view.unmount());
  (globalThis as any).window.location.search = '';
});

test('an invitation survives the sample-reader round trip', async () => {
  const { publicHref } = await import('../src/publicNavigation');
  const sample = publicHref('/demo','?invite=one%2Btwo');
  assert.equal(sample,'/demo?invite=one%2Btwo');
  assert.equal(publicHref('/',sample.slice(sample.indexOf('?'))),'/?invite=one%2Btwo');
  assert.equal(publicHref('/demo',''),'/demo');
});
