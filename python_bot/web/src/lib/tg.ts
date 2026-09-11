/** The Telegram WebApp object, when the page is running inside Telegram. */
export const TG: any = (window as any).Telegram?.WebApp ?? null

export function haptic(kind: 'success' | 'error' | 'warning' = 'success') {
  try { TG?.HapticFeedback?.notificationOccurred(kind) } catch { /* not in Telegram */ }
}

export function tap() {
  try { TG?.HapticFeedback?.impactOccurred('light') } catch { /* not in Telegram */ }
}
