import { TG } from './tg'

/**
 * HTTP header values are Latin-1 ONLY.
 *
 * The Login Widget's payload carries the player's Telegram display name verbatim, and a
 * Persian name (or an emoji) is not Latin-1 - so putting it in a header made fetch()
 * refuse to run at all: "String contains non ISO-8859-1 code point". The symptom is the
 * whole app dying rather than a failed login, and it never shows up inside Telegram
 * because initData arrives percent-encoded.
 *
 * Values that already fit are passed through byte-for-byte, which matters because the
 * widget's HMAC is taken over exactly those bytes.
 */
export function headerSafe(v: string): string {
  if (/^[\x00-\xFF]*$/.test(v)) return v
  let s = ''
  for (const b of new TextEncoder().encode(v)) s += String.fromCharCode(b)
  return 'b64:' + btoa(s)
}

export function readLogin(): string | null {
  try { return localStorage.getItem('login') } catch { return null }
}

export function authHeaders(): Record<string, string> {
  // Inside Telegram, initData is re-issued on every launch and is always the freshest
  // thing we have. In a browser it is empty and the stored Login Widget payload is what
  // identifies the player. Sending both is harmless - the server tries initData first.
  const h: Record<string, string> = { 'X-Telegram-Init-Data': headerSafe(TG?.initData || '') }
  const login = readLogin()
  if (login) h['X-Telegram-Login'] = headerSafe(login)
  return h
}

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

export async function api<T = any>(path: string, body?: Record<string, unknown>, chatId?: number | null): Promise<T> {
  const opt: RequestInit = { headers: authHeaders() }
  if (body) {
    opt.method = 'POST'
    ;(opt.headers as Record<string, string>)['Content-Type'] = 'application/json'
    opt.body = JSON.stringify({ ...body, chat_id: chatId })
  }
  const url = path + (body ? '' : (path.includes('?') ? '&' : '?') + 'chat_id=' + chatId)
  const r = await fetch(url, opt)
  const j = await r.json().catch(() => ({ ok: false, error: 'پاسخ نامعتبر از سرور' }))
  if (!j.ok) throw new ApiError(j.error || 'خطا', r.status)
  return j as T
}
