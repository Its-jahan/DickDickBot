/**
 * Drives the real UI in a real browser: click every tab, then hammer them with no wait.
 *
 * This exists because a rewrite shipped with tab switching broken and every Python
 * suite stayed green - they test the API, and this was a render bug. It is the browser
 * equivalent of "run the handlers, not just the SQL".
 *
 *   cd python_bot && SUPABASE_DB_URL=... python3 -c 'import webapp; webapp.app.run(port=8099)' &
 *   node web/test/tabs.mjs                 # needs a seeded player - see the suite
 *
 * Playwright is NOT a repo dependency and CI does not run this; the static guards in
 * test_webapp_build.py cover the same rule cheaply. Run this by hand after touching
 * anything in web/src that decides what gets rendered.
 */
import { chromium } from 'playwright'
import { readFileSync } from 'fs'

const initData = readFileSync('/tmp/initdata.txt', 'utf8')
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' })
const page = await browser.newPage()
// Stand in for telegram-web-app.js: same shape the app reads, nothing else.
await page.addInitScript((d) => {
  window.Telegram = { WebApp: { initData: d, colorScheme: 'dark', ready() {}, expand() {}, close() {},
                                HapticFeedback: { notificationOccurred() {}, impactOccurred() {} } } }
  window.__tgSettled = 1
}, initData)

const errors = []
page.on('pageerror', e => errors.push(e.message))
page.on('console', m => { if (m.type() === 'error') errors.push('[console] ' + m.text()) })

await page.goto('http://127.0.0.1:8099/', { waitUntil: 'domcontentloaded' })
await page.waitForSelector('nav button', { timeout: 20000 })

const TABS = ['خونه', 'جدول', 'بازار', 'بانک', 'فروشگاه', 'کوله']
const snap = async () => (await page.innerText('#root')).replace(/\s+/g, ' ').slice(0, 55)

console.log('--- one pass, waiting between ---')
for (const t of TABS) {
  await page.click(`nav button:has-text("${t}")`)
  await page.waitForTimeout(1200)
  console.log(`  ${t.padEnd(9)} ${JSON.stringify(await snap())}`)
}

console.log('--- hammering: switch with no wait at all (the reported case) ---')
for (let i = 0; i < 4; i++) {
  for (const t of TABS) await page.click(`nav button:has-text("${t}")`, { timeout: 5000 })
}
await page.waitForTimeout(2500)
console.log('  survived, now on:', JSON.stringify(await snap()))

console.log('--- back to every tab once more ---')
for (const t of TABS) {
  await page.click(`nav button:has-text("${t}")`)
  await page.waitForTimeout(900)
  const s = await snap()
  if (!s.trim()) throw new Error('blank screen on ' + t)
}
console.log('  all six still render')

console.log('--- page errors:', errors.length ? errors : '(none)')
await browser.close()
if (errors.length) process.exit(1)
