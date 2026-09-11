import { lazy, Suspense, useEffect, useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Sheet, SheetContent } from '@/components/ui/sheet'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { Point } from '@/components/PriceChart'
import { Skeleton } from '@/components/ui/skeleton'

// recharts is ~two thirds of the bundle and only the trade sheet ever needs it. Split
// out, the five screens that draw no chart never download it at all.
const PriceChart = lazy(() =>
  import('@/components/PriceChart').then((m) => ({ default: m.PriceChart })))
import { fa, num, pc, price as fmtPrice } from '@/lib/format'
import { api } from '@/lib/api'
import { useToast } from '@/components/Toast'
import { cn } from '@/lib/utils'

const RANGES: [number, string][] = [[6, '۶ ساعت'], [24, '۱ روز'], [72, '۳ روز'], [168, '۱ هفته']]

export function Crypto({ d, chat, reload }: { d: any; chat: number; reload: () => Promise<void> }) {
  const [open, setOpen] = useState<any | null>(null)
  return (
    <div className="space-y-3">
      <Card>
        <CardContent className="pt-4">
          <div className="flex items-center justify-between py-1">
            <span className="text-sm text-muted-foreground">جیب</span>
            <b className="tnum">{num(d.wallet)} سانت</b>
          </div>
          <div className="flex items-center justify-between py-1">
            <span className="text-sm text-muted-foreground">سقف خرید امروز</span>
            <b className="tnum">{fa(d.cap)}</b>
          </div>
          <div className="flex items-center justify-between py-1">
            <span className="text-sm text-muted-foreground">نقدینگی بانک مرکزی</span>
            <b className="tnum">{num(d.liquidity)}</b>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="divide-y pt-2">
          {d.coins.map((c: any) => (
            <button key={c.symbol} onClick={() => setOpen(c)} className="flex w-full items-center gap-3 py-3 text-right">
              <span className={cn('h-2 w-2 shrink-0 rounded-full',
                c.change > 0.01 ? 'bg-success' : c.change < -0.01 ? 'bg-destructive' : 'bg-muted-foreground')} />
              <span className="min-w-0 flex-1">
                <b className="block truncate">{c.name}</b>
                <span className="block truncate text-xs text-muted-foreground">
                  {c.units > 0
                    ? `تو: ${num(c.units, 4)} · ${num(c.value)} سانت`
                    : Math.abs(c.demand) > 0.005
                      ? (c.demand > 0 ? '🔥 تقاضا ' : '🧊 تقاضا ') + pc(c.demand * 100)
                      : c.symbol}
                </span>
              </span>
              <span className="shrink-0 text-left">
                <b className="block tnum">{fmtPrice(c.price)}</b>
                <span className={cn('block text-xs tnum', c.change >= 0 ? 'text-success' : 'text-destructive')}>
                  {pc(c.change)}
                </span>
              </span>
            </button>
          ))}
        </CardContent>
      </Card>

      <p className="px-2 pb-2 text-center text-xs leading-relaxed text-muted-foreground">
        کارمزد هر معامله {num(d.fee * 100)}٪. خرید قیمت رو بالا می‌بره و فروش پایین — ولی قیمتی
        که بهت می‌خوره میانگین مسیره، پس پامپ‌کردن سودی نداره.
      </p>

      <TradeSheet coin={open} fee={d.fee} chat={chat} onClose={() => setOpen(null)} reload={reload} />
    </div>
  )
}

function TradeSheet({ coin, fee, chat, onClose, reload }: {
  coin: any | null; fee: number; chat: number; onClose: () => void; reload: () => Promise<void>
}) {
  const [hours, setHours] = useState(24)
  const [points, setPoints] = useState<Point[]>([])
  const [loading, setLoading] = useState(false)
  const [amount, setAmount] = useState('')
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  useEffect(() => {
    if (!coin) return
    let alive = true
    setLoading(true)
    api<any>(`/api/crypto/history?symbol=${encodeURIComponent(coin.symbol)}&hours=${hours}`, undefined, chat)
      .then((r) => { if (alive) setPoints(r.points) })
      .catch(() => { if (alive) setPoints([]) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [coin, hours, chat])

  if (!coin) return null

  const run = async (path: string, body: Record<string, unknown>) => {
    setBusy(true)
    try {
      const r = await api<any>(path, body, chat)
      setAmount('')
      onClose()
      toast(r.message)
      await reload()
    } catch (e: any) {
      toast(e.message, true)
    } finally {
      setBusy(false)
    }
  }

  const v = Number(amount)

  return (
    <Sheet open onOpenChange={(o) => !o && onClose()}>
      <SheetContent title={coin.name}>
        <div className="flex items-end justify-between">
          <div>
            <div className="text-lg font-bold">{coin.name}</div>
            <div className="text-xs text-muted-foreground">{coin.symbol}</div>
          </div>
          <div className="text-left">
            <div className="text-2xl font-extrabold tnum">{fmtPrice(coin.price)}</div>
            <div className={cn('text-xs tnum', coin.change >= 0 ? 'text-success' : 'text-destructive')}>
              {pc(coin.change)} امروز
            </div>
          </div>
        </div>

        <div className="my-3">
          <Tabs value={String(hours)} onValueChange={(x) => setHours(Number(x))}>
            <TabsList>
              {RANGES.map(([h, label]) => (
                <TabsTrigger key={h} value={String(h)}>{label}</TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>

        <Suspense fallback={<Skeleton className="h-40 w-full" />}>
          <PriceChart data={points} loading={loading} />
        </Suspense>

        <div className="mt-3 flex flex-wrap gap-2">
          <Badge variant={coin.vs_base >= 0 ? 'success' : 'destructive'}>
            نسبت به پایه {pc(coin.vs_base)}
          </Badge>
          {coin.units > 0 && (
            <Badge variant="primary">
              موجودی {num(coin.units, 4)} · میانگین خرید {fmtPrice(coin.avg_cost)}
            </Badge>
          )}
        </div>

        <div className="mt-4 space-y-3">
          <Input
            type="number"
            inputMode="numeric"
            placeholder="مقدار سانت"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
          />
          <div className="flex gap-2">
            <Button className="flex-1" disabled={busy || !v} onClick={() => run('/api/crypto/buy', { symbol: coin.symbol, spend: v })}>
              خرید
            </Button>
            <Button variant="secondary" className="flex-1" disabled={busy || !v || !(coin.units > 0)}
                    onClick={() => run('/api/crypto/sell', { symbol: coin.symbol, amount: v })}>
              فروش
            </Button>
            <Button variant="outline" className="flex-1" disabled={busy || !(coin.units > 0)}
                    onClick={() => run('/api/crypto/sell', { symbol: coin.symbol, all: 1 })}>
              فروش همه
            </Button>
          </div>
          <div className="text-center text-xs text-muted-foreground">کارمزد {num(fee * 100)}٪ هر طرف</div>
        </div>
      </SheetContent>
    </Sheet>
  )
}
