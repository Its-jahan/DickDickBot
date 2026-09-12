import { useEffect, useState } from 'react'
import { Sheet, SheetContent } from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { PlayerPicker } from '@/components/PlayerPicker'
import { api } from '@/lib/api'
import { num } from '@/lib/format'
import { useToast } from '@/components/Toast'

/**
 * The three group mechanics that need somebody ELSE to tap something.
 *
 * They used to be chat-only for that reason. They are here now because the missing
 * piece was never the browser - it was that the group had to see the message and get
 * the button. The endpoints post both, so a challenge opened here is accepted from the
 * chat and a vote started here is voted on from either side. One book, not one per
 * surface.
 */
export type GroupKind = 'challenge' | 'ejma' | 'decree'

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="py-6 text-center text-sm text-muted-foreground">{children}</div>
}

export function GroupSheet({ kind, chat, onClose, reload }: {
  kind: GroupKind | null
  chat: number
  onClose: () => void
  reload: () => Promise<void>
}) {
  const [d, setD] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  // Tagged with the kind it belongs to, for the same reason the tab payloads are: the
  // sheet re-renders the instant `kind` changes, long before the new fetch lands, and
  // handing the decree screen a challenge payload is exactly how the tab crash happened.
  const path = kind === 'challenge' ? '/api/challenges'
    : kind === 'ejma' ? '/api/ejma' : '/api/decree'

  const load = async () => {
    if (!kind) return
    try {
      const r = await api<any>(path, undefined, chat)
      setD({ kind, payload: r })
    } catch (e: any) {
      toast(e.message, true)
      setD({ kind, payload: null })
    }
  }

  useEffect(() => { setD(null); load() /* eslint-disable-next-line */ }, [kind, chat])

  if (!kind) return null
  const p = d && d.kind === kind ? d.payload : null

  const run = async (url: string, body: any) => {
    setBusy(true)
    try {
      const r = await api<any>(url, body, chat)
      toast(r.message?.split('\n')[0] ?? 'انجام شد')
      await load()
      await reload()
      return true
    } catch (e: any) {
      toast(e.message, true)
      return false
    } finally {
      setBusy(false)
    }
  }

  return (
    <Sheet open onOpenChange={(o) => !o && onClose()}>
      <SheetContent title="گروه">
        {!p ? <><Skeleton className="h-8 w-40" /><div className="h-3" /><Skeleton className="h-24 w-full" /></>
          : kind === 'challenge' ? <Challenge p={p} busy={busy} run={run} />
          : kind === 'ejma' ? <Ejma p={p} chat={chat} busy={busy} run={run} />
          : <Decree p={p} busy={busy} run={run} />}
      </SheetContent>
    </Sheet>
  )
}

function Challenge({ p, busy, run }: { p: any; busy: boolean; run: any }) {
  const [bet, setBet] = useState('10')
  const open = p?.open ?? []
  const mine = open.filter((c: any) => c.challenger_id === p.me_id)
  const theirs = open.filter((c: any) => c.challenger_id !== p.me_id)
  return (
    <div className="space-y-3">
      <div className="text-lg font-bold">⚔️ چالش</div>
      <div className="text-xs text-muted-foreground">
        سایز تو: <b className="tnum">{num(p.size)}</b> سانت. شرط از هر دو طرف همون لحظهٔ
        قبول‌شدن کم می‌شه.
      </div>
      <div className="flex gap-2">
        <Input type="number" inputMode="numeric" placeholder="شرط" value={bet}
               onChange={(e) => setBet(e.target.value)} />
        <Button disabled={busy} onClick={() => run('/api/challenge/create', { bet: Number(bet) })}>
          بنداز
        </Button>
      </div>
      <p className="text-[11px] leading-relaxed text-muted-foreground">
        📣 چالشت با دکمهٔ «قبول» می‌ره تو گروه، پس هرکی تو تلگرام هم هست می‌تونه قبولش کنه.
      </p>

      <div className="border-t pt-3 text-sm font-semibold">چالش‌های باز</div>
      {!theirs.length && !mine.length && <Empty>الان چالش بازی نیست.</Empty>}
      {theirs.map((c: any) => (
        <div key={c.nonce} className="flex items-center justify-between rounded-md bg-secondary p-3">
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold">{c.challenger}</div>
            <div className="text-[11px] text-muted-foreground tnum">{num(c.bet)} سانت</div>
          </div>
          <Button size="sm" disabled={busy || p.size < c.bet}
                  onClick={() => run('/api/challenge/accept', { nonce: c.nonce })}>
            {p.size < c.bet ? 'سایز کمه' : 'قبول'}
          </Button>
        </div>
      ))}
      {mine.map((c: any) => (
        <div key={c.nonce} className="flex items-center justify-between rounded-md border p-3">
          <div className="text-sm">چالش خودت — <b className="tnum">{num(c.bet)}</b> سانت</div>
          <Badge>منتظر حریف</Badge>
        </div>
      ))}
    </div>
  )
}

function Ejma({ p, chat, busy, run }: { p: any; chat: number; busy: boolean; run: any }) {
  const [target, setTarget] = useState<number | null>(null)
  const open = p?.open ?? []
  return (
    <div className="space-y-3">
      <div className="text-lg font-bold">⚖️ اجماع</div>
      <div className="text-xs text-muted-foreground">
        امروز <b>{p.active_today}</b> نفر فعالن (حداقل {p.min_players} نفر لازمه).
        {!p.eligible && ' تو الان نمی‌تونی اجماع راه بندازی یا رای بدی.'}
      </div>

      {p.eligible && (
        <>
          <PlayerPicker chat={chat} value={target} onPick={setTarget} />
          <Button disabled={busy || !target}
                  onClick={() => run('/api/ejma/start', { target })}>
            اجماع راه بنداز
          </Button>
        </>
      )}

      <div className="border-t pt-3 text-sm font-semibold">رای‌گیری‌های باز</div>
      {!open.length && <Empty>الان رای‌گیری بازی نیست.</Empty>}
      {open.map((v: any) => (
        <div key={v.vote_id} className="space-y-2 rounded-md bg-secondary p-3">
          <div className="flex items-center justify-between">
            <div className="min-w-0 truncate text-sm font-semibold">علیه {v.target}</div>
            <Badge variant="primary" className="tnum">−{num(v.amount)}</Badge>
          </div>
          <div className="text-[11px] text-muted-foreground tnum">
            ✅ {v.yes} · ❌ {v.no} — برای تصویب {v.required} رای لازمه
          </div>
          {p.eligible && v.target_id !== p.me_id && (
            <div className="grid grid-cols-2 gap-2">
              <Button size="sm" disabled={busy}
                      onClick={() => run('/api/ejma/vote', { vote_id: v.vote_id, choice: 'yes' })}>
                ✅ موافق
              </Button>
              <Button size="sm" variant="secondary" disabled={busy}
                      onClick={() => run('/api/ejma/vote', { vote_id: v.vote_id, choice: 'no' })}>
                ❌ مخالف
              </Button>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function Decree({ p, busy, run }: { p: any; busy: boolean; run: any }) {
  const choices = p?.choices ?? []
  return (
    <div className="space-y-3">
      <div className="text-lg font-bold">👑 فرمان</div>
      <div className="flex flex-wrap gap-2 text-xs">
        <Badge>تورم {p.inflation?.toFixed?.(2) ?? '—'}</Badge>
        <Badge variant={p.unrest >= 60 ? 'primary' : 'default'}>خشم {Math.round(p.unrest ?? 0)}/100</Badge>
      </div>
      {!p.is_king && <Empty>فرمان مال پادشاهه{p.king ? ` — الان ${p.king}` : ''}.</Empty>}
      {p.is_king && p.signed_today && <Empty>امروز فرمانت رو امضا کردی. فردا دوباره. 👑</Empty>}
      {p.is_king && !p.signed_today && (
        <>
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            😈 فاسدها جیبت رو پر می‌کنن و خشم مردم رو بالا می‌برن. 😇 درست‌کارها برات
            خرج دارن و آرومشون می‌کنن. یکی رو امضا کن.
          </p>
          {choices.map((c: any) => (
            <button key={c.code} disabled={busy}
                    className="w-full rounded-md border p-3 text-start transition-colors hover:bg-accent disabled:opacity-50"
                    onClick={() => run('/api/decree/sign', { code: c.code })}>
              <div className="text-sm font-semibold">
                {c.kind === 'bad' ? '😈' : '😇'} {c.title}
              </div>
              <div className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">{c.desc}</div>
            </button>
          ))}
        </>
      )}
    </div>
  )
}
