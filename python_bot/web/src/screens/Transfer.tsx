import { useEffect, useState } from 'react'
import { Sheet, SheetContent } from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { fa, num } from '@/lib/format'
import { api } from '@/lib/api'
import { useToast } from '@/components/Toast'
import { cn } from '@/lib/utils'

/**
 * A sheet rather than a seventh nav tab: six is already a lot at phone width, and this
 * is a thing you do occasionally, not a screen you live on. Every refusal the chat
 * enforces is re-checked server-side - this screen only renders what it is told.
 */
export function Transfer({ chat, open, onClose, reload }: {
  chat: number; open: boolean; onClose: () => void; reload: () => Promise<void>
}) {
  const [d, setD] = useState<any | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [to, setTo] = useState<number | null>(null)
  const [amount, setAmount] = useState('')
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  useEffect(() => {
    if (!open) return
    let alive = true
    setD(null); setErr(null); setAmount('')
    api<any>('/api/transfer', undefined, chat)
      .then((r) => { if (!alive) return; setD(r); setTo(r.groups?.[0]?.chat_id ?? null) })
      .catch((e) => { if (alive) setErr(e.message) })
    return () => { alive = false }
  }, [open, chat])

  const go = async () => {
    const v = Number(amount)
    if (!v) return toast('مقدار رو بنویس', true)
    if (!to) return toast('گروه مقصد رو انتخاب کن', true)
    setBusy(true)
    try {
      const r = await api<any>('/api/transfer', { amount: v, to_chat: to }, chat)
      onClose()
      toast(r.message)
      await reload()
    } catch (e: any) {
      toast(e.message, true)
    } finally {
      setBusy(false)
    }
  }

  const body = () => {
    if (err) return <p className="py-6 text-center text-sm text-muted-foreground">{err}</p>
    if (!d) return <Skeleton className="h-40 w-full" />
    if (!d.enabled) return <p className="py-6 text-center text-sm text-muted-foreground">🔒 این قابلیت الان بسته‌ست.</p>
    if (!d.source_ok) {
      return (
        <div className="space-y-3 py-4">
          <p className="text-sm text-muted-foreground">{d.source_reason || 'این گروه اجازهٔ خروج سایز نداره.'}</p>
          <p className="text-xs text-muted-foreground">
            سایز فقط از یه گروهِ واقعی و فعال می‌تونه خارج بشه — این جلوی ساختن گروه الکی و
            پروارکردن سایز توش رو می‌گیره.
          </p>
        </div>
      )
    }
    if (!d.groups.length) {
      return (
        <p className="py-6 text-center text-sm text-muted-foreground">
          تو هیچ گروه دیگه‌ای بازی نمی‌کنی! اول تو یه گروه دیگه <b>/d</b> بزن.
        </p>
      )
    }
    if (d.wait_seconds > 0) {
      const h = Math.floor(d.wait_seconds / 3600)
      const m = Math.floor((d.wait_seconds % 3600) / 60)
      return (
        <p className="py-6 text-center text-sm text-muted-foreground">
          تازه انتقال زدی — تا <b>{fa(h)}</b> ساعت و <b>{fa(m)}</b> دقیقهٔ دیگه صبر کن.
        </p>
      )
    }

    const v = Number(amount)
    const fee = v >= d.min_amount ? Math.floor(v * d.fee_ratio) : 0
    return (
      <div className="space-y-3">
        <p className="text-xs text-muted-foreground">
          کارمزد <b className="text-destructive">{fa(Math.round(d.fee_ratio * 100))}٪</b> ·
          حداقل {fa(d.min_amount)} سانت · هر {fa(d.cooldown_hours)} ساعت یک بار
        </p>
        <div className="text-xs text-muted-foreground">به کدوم گروه؟</div>
        <div className="divide-y rounded-md border">
          {d.groups.map((g: any) => (
            <button key={g.chat_id} onClick={() => setTo(g.chat_id)}
                    className={cn('flex w-full items-center justify-between p-3 text-right text-sm',
                      to === g.chat_id && 'bg-primary/10')}>
              <span className="truncate">{to === g.chat_id ? '● ' : '○ '}{g.title}</span>
              <span className="shrink-0 text-muted-foreground tnum">{num(g.size)}</span>
            </button>
          ))}
        </div>
        <Input type="number" inputMode="numeric" placeholder="مقدار سانت"
               value={amount} onChange={(e) => setAmount(e.target.value)} />
        <div className="text-center text-xs text-muted-foreground">
          {v >= d.min_amount
            ? <>کارمزد <b className="text-destructive">{fa(fee)}</b> · به مقصد می‌رسه <b className="text-success">{fa(v - fee)}</b></>
            : `حداقل ${fa(d.min_amount)} سانت`}
        </div>
        <Button size="lg" disabled={busy} onClick={go}>فرستادن</Button>
        <p className="text-center text-[11px] text-muted-foreground">
          📣 ربات این انتقال رو تو هر دو گروه اعلام می‌کنه
        </p>
      </div>
    )
  }

  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent title="انتقال بین‌گروهی">
        <div className="mb-3 flex items-center justify-between">
          <b className="text-lg">🔁 انتقال بین‌گروهی</b>
          {d && <span className="text-xs text-muted-foreground tnum">جیبت {num(d.wallet)}</span>}
        </div>
        {body()}
      </SheetContent>
    </Sheet>
  )
}
