import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';

const root = new URL('./', import.meta.url);
const read = name => readFileSync(new URL(name, root));

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
