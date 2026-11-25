// Browser checks for the Nova avatar card. Needs a running core server:
//   uvicorn nova_core.main:app --port 8765
//   node tests/web/avatar_check.cjs            (NOVA_URL to override, SHOTS=dir for screenshots)
const { chromium } = require('playwright');
const assert = require('assert');

const URL = process.env.NOVA_URL || 'http://127.0.0.1:8765/';
const SHOTS = process.env.SHOTS || '';

async function openDashboard(browser, routes = async () => {}) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.addInitScript(() => localStorage.setItem('nova_token', 'test-token'));
    await routes(page);
    await page.goto(URL);
    return { page, errors };
}

const avatarAttr = (page) => page.$eval('#nova-avatar', (el) => el.dataset.avatar || '');
const avatarState = (page) => page.$eval('#nova-avatar', (el) => el.dataset.state);

async function checkRenders(browser) {
    const { page, errors } = await openDashboard(browser);
    await page.waitForFunction(() => document.querySelector('#nova-avatar')?.dataset.avatar === 'ready', null, { timeout: 20000 });
    assert.ok(await page.$('#nova-avatar canvas'), 'canvas mounted');
    await page.waitForTimeout(800);
    if (SHOTS) await page.locator('.avatar-card').screenshot({ path: `${SHOTS}/avatar_idle.png` });
    for (const st of ['thinking', 'error']) {
        await page.evaluate((s) => window.NovaAvatar.setState(s), st);
        await page.waitForTimeout(700);
        if (SHOTS) await page.locator('.avatar-card').screenshot({ path: `${SHOTS}/avatar_${st}.png` });
    }
    await page.evaluate(() => window.NovaAvatar.setState('talking', { text: 'Halo! Aku Nova.' }));
    await page.waitForTimeout(300);
    if (SHOTS) await page.locator('.avatar-card').screenshot({ path: `${SHOTS}/avatar_talking.png` });
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/dashboard.png` });
    assert.deepStrictEqual(errors, [], 'no page errors');
    await page.close();
    console.log('ok: avatar renders');
}

async function checkFallback(browser) {
    const { page, errors } = await openDashboard(browser, (p) => p.route('**/nova_avatar.glb', (r) => r.fulfill({ status: 404, body: '' })));
    await page.waitForFunction(() => document.querySelector('#nova-avatar')?.dataset.avatar === 'fallback', null, { timeout: 20000 });
    assert.ok(await page.isVisible('.avatar-fallback'), 'fallback visible');
    assert.ok(await page.isVisible('#console-input'), 'console still usable');
    assert.deepStrictEqual(errors, [], 'no page errors');
    await page.close();
    console.log('ok: fallback on missing model');
}

async function checkTalkTiming(browser) {
    const { page } = await openDashboard(browser);
    await page.waitForFunction(() => window.NovaAvatar);
    const d = await page.evaluate(() => [
        window.NovaAvatar.talkDuration(''),
        window.NovaAvatar.talkDuration('x'.repeat(40)),
        window.NovaAvatar.talkDuration('x'.repeat(5000)),
    ]);
    assert.deepStrictEqual(d, [1200, 2200, 6000]);
    await page.close();
    console.log('ok: talk duration clamped');
}

async function checkConsoleFlow(browser) {
    let mode = 'ok';
    const { page, errors } = await openDashboard(browser, (p) => p.route('**/command', async (r) => {
        await new Promise((res) => setTimeout(res, 1000));
        if (mode === 'ok') {
            await r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ response_text: 'Siap!', routed_path: 'test' }) });
        } else {
            await r.fulfill({ status: 500, body: 'boom' });
        }
    }));
    await page.waitForFunction(() => window.NovaAvatar);

    await page.type('#console-input', 'nyalakan lampu');
    assert.strictEqual(await avatarState(page), 'listening', 'typing -> listening');

    await page.click('#console-form button[type=submit]');
    await page.waitForTimeout(200);
    assert.strictEqual(await avatarState(page), 'thinking', 'pending -> thinking');
    await page.waitForFunction(() => document.querySelector('#nova-avatar').dataset.state === 'talking', null, { timeout: 3000 });
    await page.waitForFunction(() => document.querySelector('#nova-avatar').dataset.state === 'idle', null, { timeout: 4000 });

    mode = 'fail';
    await page.type('#console-input', 'gagal');
    await page.click('#console-form button[type=submit]');
    await page.waitForFunction(() => document.querySelector('#nova-avatar').dataset.state === 'error', null, { timeout: 3000 });

    // Rapid double submit: the last result wins, nothing stays stuck in "thinking"
    mode = 'ok';
    await page.type('#console-input', 'satu');
    await page.click('#console-form button[type=submit]');
    await page.type('#console-input', 'dua');
    await page.click('#console-form button[type=submit]');
    await page.waitForFunction(() => document.querySelector('#nova-avatar').dataset.state === 'talking', null, { timeout: 4000 });
    await page.waitForFunction(() => document.querySelector('#nova-avatar').dataset.state === 'idle', null, { timeout: 5000 });

    assert.deepStrictEqual(errors, [], 'no page errors');
    await page.close();
    console.log('ok: console drives avatar states');
}

(async () => {
    const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
    const only = process.argv[2];
    const checks = { checkRenders, checkFallback, checkTalkTiming, checkConsoleFlow };
    try {
        for (const [name, fn] of Object.entries(checks)) {
            if (!only || only === name) await fn(browser);
        }
    } finally {
        await browser.close();
    }
})().catch((e) => { console.error(e); process.exit(1); });
