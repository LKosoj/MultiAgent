"""Browser check for refresh, run completion and delayed review answers."""
import functools
import http.server
import os
from pathlib import Path
import subprocess
import tempfile
import threading

from playwright.sync_api import expect, sync_playwright

client = Path(__file__).resolve().parents[2]
entry = Path(__file__).with_suffix('.tsx')
with tempfile.TemporaryDirectory(prefix='storybook-film-review-') as directory:
    root = Path(directory)
    subprocess.run([str(client / 'node_modules/esbuild/bin/esbuild'), str(entry), '--bundle', '--platform=browser', '--outfile=' + str(root / 'bundle.js')], check=True, env={**os.environ, 'NODE_PATH': str(client / 'node_modules')})
    (root / 'index.html').write_text('<div id="root"></div><script src="/bundle.js"></script>')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path='/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome', args=['--no-sandbox'])
            page = browser.new_page(); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            def wait_calls(count): page.wait_for_function('(n) => window.reviewCalls() >= n', arg=count)
            def resolve(index, note, passed):
                page.evaluate('([i,n,p]) => window.resolveReview(i, {review:{status:"pending",note:n,input_digest:n,stale:false},technical_passed:p,final_available:p})', [index, note, passed])
            wait_calls(1); resolve(0, 'first', False)
            expect(page.get_by_role('button', name='Принять художественно')).to_be_disabled()
            page.get_by_role('button', name='F5').click(); wait_calls(2)
            page.get_by_role('button', name='F5').click(); wait_calls(3)
            resolve(2, 'fresh', True); resolve(1, 'old', False)
            expect(page.get_by_label('Замечания')).to_have_value('fresh')
            expect(page.get_by_role('button', name='Принять художественно')).to_be_enabled()
            page.get_by_role('button', name='Start').click(); wait_calls(4)
            page.get_by_role('button', name='Done').click(); wait_calls(5)
            resolve(4, 'finished', True); resolve(3, 'running', False)
            expect(page.get_by_label('Замечания')).to_have_value('finished')
            page.get_by_role('button', name='F5').click(); wait_calls(6)
            page.get_by_role('button', name='Other').click(); wait_calls(7)
            resolve(6, 'other series', True); resolve(5, 'old series', False)
            expect(page.get_by_label('Замечания')).to_have_value('other series')
            # A refresh may read the old state while an approval is still being saved.
            page.get_by_role('button', name='Принять художественно').click(); wait_calls(8)
            page.get_by_role('button', name='F5').click(); wait_calls(9)
            resolve(8, 'pre-write', True)
            page.evaluate('() => window.resolveReview(7, {review:{status:"approved",note:"saved",input_digest:"saved",stale:false},technical_passed:true,final_available:true})')
            wait_calls(10)
            page.evaluate('() => window.resolveReview(9, {review:{status:"approved",note:"saved",input_digest:"saved",stale:false},technical_passed:true,final_available:true})')
            expect(page.get_by_text('Художественное решение: принято.')).to_be_visible()
            # A write from the old series must not update or lock the new one.
            page.get_by_role('button', name='На доработку').click(); wait_calls(11)
            page.get_by_role('button', name='Other').click(); wait_calls(12)
            resolve(11, 'new series', True)
            page.evaluate('() => window.resolveReview(10, {review:{status:"redo",note:"old write",input_digest:"old",stale:false},technical_passed:true,final_available:true})')
            expect(page.get_by_label('Замечания')).to_have_value('new series')
            expect(page.get_by_role('button', name='На доработку')).to_be_enabled()
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
print('Film review browser checks passed')
