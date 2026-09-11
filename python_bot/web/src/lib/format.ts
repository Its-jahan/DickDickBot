/** Persian digits everywhere, and one place that decides how many decimals. */
export const fa = (n: number | null | undefined) => (n ?? 0).toLocaleString('fa-IR')

export const num = (n: number | null | undefined, d = 0) =>
  (n ?? 0).toLocaleString('fa-IR', { minimumFractionDigits: d, maximumFractionDigits: d })

export const pc = (n: number | null | undefined) => ((n ?? 0) >= 0 ? '+' : '') + num(n, 2) + '٪'

/** A coin price needs more precision the cheaper it is, or a 2-size coin reads as flat. */
export const price = (n: number) => num(n, n >= 100 ? 2 : n >= 1 ? 3 : 5)
