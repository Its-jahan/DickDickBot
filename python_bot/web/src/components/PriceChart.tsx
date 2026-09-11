import { Area, AreaChart, ResponsiveContainer, Tooltip, YAxis } from 'recharts'
import { price as fmtPrice, num } from '@/lib/format'
import { Skeleton } from '@/components/ui/skeleton'

export type Point = { t: number; p: number }

/**
 * Deliberately axis-light: this is a sparkline with a scale, not a trading terminal.
 * The y domain is the series' own range rather than zero-based, because a coin moving
 * 3% against a 500-size base is invisible on a zero-based axis - and 3% is the size of
 * move this market actually makes.
 */
export function PriceChart({ data, loading }: { data: Point[]; loading?: boolean }) {
  if (loading) return <Skeleton className="h-40 w-full" />
  if (data.length < 2) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
        هنوز تاریخچهٔ کافی برای نمودار نیست
      </div>
    )
  }

  const first = data[0].p
  const last = data[data.length - 1].p
  const up = last >= first
  const color = up ? 'hsl(var(--success))' : 'hsl(var(--destructive))'
  const lo = Math.min(...data.map((d) => d.p))
  const hi = Math.max(...data.map((d) => d.p))
  const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.02 || 1

  return (
    <div className="h-40 w-full" dir="ltr">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 6, right: 4, bottom: 0, left: 4 }}>
          <defs>
            <linearGradient id="pcFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.35} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <YAxis domain={[lo - pad, hi + pad]} hide />
          <Tooltip
            cursor={{ stroke: 'hsl(var(--muted-foreground))', strokeWidth: 1 }}
            contentStyle={{
              background: 'hsl(var(--card))',
              border: '1px solid hsl(var(--border))',
              borderRadius: 'var(--radius)',
              fontSize: 12,
              direction: 'rtl',
            }}
            labelFormatter={(_, p) =>
              p?.[0] ? new Date((p[0].payload as Point).t * 1000).toLocaleTimeString('fa-IR',
                { hour: '2-digit', minute: '2-digit' }) : ''
            }
            formatter={(v: number) => [fmtPrice(v) + ' سانت', 'قیمت']}
          />
          <Area
            type="monotone"
            dataKey="p"
            stroke={color}
            strokeWidth={2}
            fill="url(#pcFill)"
            isAnimationActive={false}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
      <div className="mt-1 flex justify-between px-1 text-[11px] text-muted-foreground tnum" dir="rtl">
        <span>کمترین {fmtPrice(lo)}</span>
        <span className={up ? 'text-success' : 'text-destructive'}>
          {(up ? '+' : '') + num(((last - first) / (first || 1)) * 100, 2)}٪ در این بازه
        </span>
        <span>بیشترین {fmtPrice(hi)}</span>
      </div>
    </div>
  )
}
