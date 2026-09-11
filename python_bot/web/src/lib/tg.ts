/**
 * Telegram's WebApp object - which may not exist YET.
 *
 * telegram-web-app.js is loaded async so it can never block the first paint (a
 * synchronous head script on an unreachable host is a blank page for as long as the
 * network takes to time out). That means at module-evaluation time it is usually
 * absent, so this is a live binding that fills in once the script lands, and boot waits
 * a bounded moment for it rather than assuming either way.
 */
export let TG: any = (window as any).Telegram?.WebApp ?? null

/** How long boot will wait for Telegram before deciding this is an ordinary browser.
 *  Long enough for a slow phone, short enough that a blocked host is not a hang. */
export const TG_WAIT_MS = 3000

export function waitForTelegram(timeout = TG_WAIT_MS): Promise<any | null> {
  const found = () => (window as any).Telegram?.WebApp ?? null
  TG = found()
  if (TG) return Promise.resolve(TG)
  // Outside Telegram the script tag still exists, so "settled" (loaded or failed)
  // without a WebApp object is a definitive no - no point waiting the full timeout.
  return new Promise((resolve) => {
    const t0 = Date.now()
    const poll = window.setInterval(() => {
      TG = found()
      const settled = (window as any).__tgSettled
      if (TG || settled || Date.now() - t0 > timeout) {
        window.clearInterval(poll)
        resolve(TG)
      }
    }, 50)
  })
}

export function haptic(kind: 'success' | 'error' | 'warning' = 'success') {
  try { TG?.HapticFeedback?.notificationOccurred(kind) } catch { /* not in Telegram */ }
}

export function tap() {
  try { TG?.HapticFeedback?.impactOccurred('light') } catch { /* not in Telegram */ }
}
