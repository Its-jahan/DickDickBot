const REPLACEMENTS: [string, string][] = [
  ['زن جنده', 'ملکهٔ شانس'], ['حروم‌دست', 'دست‌طلایی'],
  ['کیرشکسته', 'آسیب‌دیده'],
  ['حرومزاده', 'یخ‌زده'], ['کون‌سوخته', 'بداقبال'],
  ['کون‌گشاد', 'خونسرد'], ['کص‌شانس', 'خوش‌شانس'],
  ['کص‌کش', 'حسابگر'], ['کیرکلفت', 'قدرتمند'],
  ['سوراخ‌جیب', 'جیب‌باز'], ['جاکش', 'میانجی'],
  ['لاشی', 'جان‌سخت'], ['جقی', 'ریسک‌باز'],
  ['شاه کص', 'ته‌جدولی'], ['دودول', 'قدرت'],
  ['شومبول', 'قدرت'], ['کیر', 'قدرت'], ['کص', 'ضعف'],
  ['کون', 'توان'], ['جنده', 'بدنام'], ['جق', 'ریسک'],
]

export type ToneMode = 'adult' | 'polite'

export function politeText(value: string): string {
  return REPLACEMENTS.reduce((text, [from, to]) => text.split(from).join(to), value)
}

/** Render server-owned copy while keeping player/group names byte-for-byte intact. */
export function tonePayload<T>(value: T, mode: ToneMode, key = ''): T {
  if (mode !== 'polite') return value
  if (typeof value === 'string') {
    const userOwned = new Set(['name', 'title', 'actor', 'target', 'username'])
    return (userOwned.has(key) ? value : politeText(value)) as T
  }
  if (Array.isArray(value)) return value.map((v) => tonePayload(v, mode, key)) as T
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .map(([k, v]) => [k, tonePayload(v, mode, k)])
    ) as T
  }
  return value
}
