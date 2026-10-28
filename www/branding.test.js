import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';

const root = new URL('./', import.meta.url);
const read = name => readFileSync(new URL(name, root));
const brandHashes = {
  'admiral-logo-ink.svg': 'f043907a475f00badae071ede1349e48ba08324aa383fbccf1ba646b9d6880f4',
  'Archivo.woff2': 'c44a3147770a4e67df0e68fc99249e503dab408165d6a48c4180d8e4c272642c',
  'IBMPlexMono-Regular.woff2': 'b6e314e84f62a17399dbc54fb50345292417dd4392f133687e3e9488d3eea9f2',
  'IBMPlexMono-Medium.woff2': '337a34abdf3dfa11bd0213f6345f0d3a6111fcbe251f6cc44627da40f63c68a4',
  'paper-tile.webp': '13f05ad5b2aebd7adc34e5a177d88ceffc25a65f902e86ab5e75b6abac5ccf99',
};

test('Admiral × Copper uses authentic local artwork and the complete Admiral paper/blue system', () => {
  const html = read('index.html').toString();
  const header = html.match(/<header\b[\s\S]*?<\/header>/)[0];
  assert.match(header, /<img\b[^>]*class="admiral-logo"[^>]*src="\/assets\/admiral-logo-ink\.svg"[^>]*alt="Admiral"/);
  assert.doesNotMatch(header, /<svg\b|admiral-wordmark/);
  assert.ok(header.indexOf('admiral-logo') < header.indexOf('brand-cross'));
  assert.ok(header.indexOf('brand-cross') < header.indexOf('copper-logo'));
  for (const [name, hash] of Object.entries(brandHashes)) {
    assert.equal(createHash('sha256').update(read(`assets/${name}`)).digest('hex'), hash, name);
  }
  const css = read('style.css').toString();
  for (const token of ['--paper: #f2efe7', '--ink: #121420', '--blue: #2152ee', '--rule: #c9c4b8']) assert.ok(css.includes(token), token);
  assert.match(css, /font-family:\s*'Archivo'/);
  assert.match(css, /font-family:\s*'IBM Plex Mono'/);
  for (const name of ['Archivo.woff2', 'IBMPlexMono-Regular.woff2', 'IBMPlexMono-Medium.woff2', 'paper-tile.webp']) assert.ok(css.includes(`/assets/${name}`));
  assert.doesNotMatch(css, /--copper|color-scheme:\s*dark|border-radius:\s*[1-9]/);
  assert.match(css, /\.mode-button\.active\s*\{[^}]*background:\s*var\(--blue\)/);
  const app = read('app.js').toString();
  assert.ok(app.includes('IBM Plex Mono'));
  assert.ok(app.includes('#2152ee'));
  assert.doesNotMatch(app, /#e7a16d|#bd8054|#182021/);
});

test('header uses the supplied Copper logo rather than an invented mark', () => {
  const html = read('index.html').toString();
  assert.match(html, /<img\b[^>]*class="copper-logo"[^>]*src="\.?\/?assets\/copper-logo\.webp"[^>]*alt="Copper"/);
  assert.equal(/class="copper-(?:mark|wordmark|dot)"/.test(html), false);
  const css = read(existsSync(new URL('styles.css', root)) ? 'styles.css' : 'style.css').toString();
  assert.match(css, /\.copper-logo\s*\{[^}]*object-fit\s*:\s*contain/);
  assert.equal(/\.copper-logo\s*\{[^}]*filter\s*:/.test(css), false, 'Keep the supplied logo colours');
});

test('the exact supplied logo is packaged locally for offline use', () => {
  const bytes = read('assets/copper-logo.webp');
  assert.equal(bytes.toString('ascii', 0, 4), 'RIFF');
  assert.equal(bytes.toString('ascii', 8, 12), 'WEBP');
  assert.equal(createHash('sha256').update(bytes).digest('hex'), '277380eeaffcfa4ffb5429b15281eff9caa2c799ebe2d8548437f57980d3bffc');
  if (existsSync(new URL('static-routes.json', root))) {
    const manifest = JSON.parse(read('static-routes.json'));
    assert.ok(manifest.static_routes.includes('/assets/copper-logo.webp'));
  }
});
