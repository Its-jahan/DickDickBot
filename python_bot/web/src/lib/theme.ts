export type Theme = 'light' | 'dark'

export function currentTheme(): Theme {
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
}

/** Writes the class the tokens key off, and remembers the choice. index.html applies
 *  the same decision before first paint so the app never flashes the wrong theme. */
export function setTheme(t: Theme) {
  document.documentElement.classList.toggle('dark', t === 'dark')
  try { localStorage.setItem('theme', t) } catch { /* private window */ }
}
