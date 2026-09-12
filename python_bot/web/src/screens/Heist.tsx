import { useEffect, useRef, useState } from 'react'
import { Sheet, SheetContent } from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { PlayerPicker } from '@/components/PlayerPicker'
import { api } from '@/lib/api'
import { num } from '@/lib/format'
import { useToast } from '@/components/Toast'

/**
 * The bank job, in a browser.
 *
 * The chat gets this as a chain of scheduled message edits. There is no scheduler here,
 * so the screen POLLS: every transition is derived on the server from two stored
 * timestamps (alarm_at, vault_at), which is why both surfaces show the same run at the
 * same moment and why a deploy mid-heist loses nothing.
 *
 * The anti-cheat survives the port. /api/heist never sends the sequence - during the
 * reveal it sends the ONE symbol that is on screen right now, so no single response,
 * screenshot or devtools frame ever contains two symbols of the answer.
 */
const POLL_MS = 700

export function HeistSheet({ chat, open, onClose, reload }: {
  chat: number
  open: boolean
  onClose: () => void
  reload: () => Promise<void>
}) {
  const [d, setD] = useState<any>(null)
  const [partner, setPartner] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [wire, setWire] = useState<number | null>(null)
  const toast = useToast()
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    if (!open) return
    let t: any
    const tick = async () => {
      try {
        const r = await api<any>('/api/heist', undefined, chat)
        if (alive.current) setD(r)
      } catch { /* a dropped poll must never kill the run */ }
      if (alive.current) t = setTimeout(tick, POLL_MS)
    }
    tick()
    return () => { alive.current = false; clearTimeout(t) }
  }, [open, chat])

  if (!open) return null
  const run = d?.run ?? null

  const act = async (url: string, body: any) => {
    setBusy(true)
    try {
      const r = await api<any>(url, body, chat)
      if (r.message) toast(r.message)
      if (r.wire !== undefined) setWire(r.wire)
      if (r.result === 'lost' || r.result === 'done') { setWire(null); await reload() }
      return r
    } catch (e: any) {
      toast(e.message, true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Sheet open onOpenChange={(o) => !o && onClose()}>
      <SheetContent title="سرقت از بانک">
        <div className="mb-3 text-lg font-bold">🥷 سرقت از بانک</div>
        {!d ? <Skeleton className="h-28 w-full" />
          : d.jailed ? (
            <div className="py-6 text-center text-sm text-muted-foreground">
              ⛓ تو زندان بانکی. {d.bail > 0 && <>با <b>{num(d.bail)}</b> سانت وثیقه می‌تونی زودتر بیای بیرون — تو گروه <code>/vasighe</code> بزن.</>}
            </div>
          ) : !run ? (
            <div className="space-y-3">
              <p className="text-xs leading-relaxed text-muted-foreground">
                سه مرحله‌ست و <b>دو نفر</b> لازمه: شریکت دزدگیر رو می‌بُره، تو گاوصندوق
                رو باز می‌کنی، و آخرش هر دو باید فرار کنید. اگه گیر بیفتین هر دو می‌رید
                زندان. تو صندوق حدود <b>{num(d.vault)}</b> سانته.
              </p>
              <PlayerPicker chat={chat} value={partner} onPick={setPartner} />
              <Button size="lg" disabled={busy || !partner}
                      onClick={() => act('/api/heist/start', { partner })}>
                پیشنهاد بده
              </Button>
              <p className="text-center text-[11px] text-muted-foreground">
                📣 پیشنهاد با دکمه می‌ره تو گروه، پس شریکت از تلگرام هم می‌تونه قبول کنه.
              </p>
            </div>
          ) : <Run run={run} wire={wire} busy={busy} act={act} />}
      </SheetContent>
    </Sheet>
  )
}

function Clock({ s }: { s: number | null }) {
  if (s === null || s === undefined) return null
  return <Badge variant={s < 3 ? 'primary' : 'default'} className="tnum">⏳ {s.toFixed(1)}s</Badge>
}

function Run({ run, wire, busy, act }: { run: any; wire: number | null; busy: boolean; act: any }) {
  const id = run.attempt_id
  const wires: string[] = run.wires ?? []
  const symbols: string[] = run.symbols ?? []

  if (run.status === 'offered') {
    return (
      <div className="space-y-3 text-center">
        <div className="text-sm">
          {run.is_partner
            ? <><b>{run.thief}</b> می‌خواد بانک رو بزنه و تو رو شریک کرده.</>
            : <>منتظر <b>{run.partner}</b>…</>}
        </div>
        <div className="text-xs text-muted-foreground">
          غنیمت حدود {num(run.would_be)} سانت — اگه گیر بیفتین هر دو می‌رید زندان.
        </div>
        {run.is_partner && (
          <Button size="lg" disabled={busy} onClick={() => act('/api/heist/accept', { attempt_id: id })}>
            🤝 هستم
          </Button>
        )}
      </div>
    )
  }

  // stage 1 — the alarm. The accomplice's job.
  if (run.stage === 1) {
    return (
      <div className="space-y-3 text-center">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">🔌 مرحلهٔ ۱ — دزدگیر</div>
          <Clock s={run.seconds_left} />
        </div>
        {!run.is_partner ? (
          <div className="py-6 text-sm text-muted-foreground">
            نوبت <b>{run.partner}</b>ه — داره دزدگیر رو می‌بُره.
          </div>
        ) : wire !== null && !run.armed ? (
          <div className="space-y-2 py-4">
            <div className="text-4xl">{wires[wire]}</div>
            <div className="text-xs text-muted-foreground">
              این سیمو یادت باشه. دیگه نشونت نمی‌دم. صبر کن تا علامت بدم…
            </div>
          </div>
        ) : run.armed ? (
          <div className="space-y-3">
            <div className="text-sm font-bold">✂️ الان! سیمو بِبُر — کدوم بود؟</div>
            <div className="grid grid-cols-3 gap-2">
              {wires.map((w, i) => (
                <button key={i} disabled={busy}
                        className="rounded-md border py-3 text-2xl transition-colors hover:bg-accent disabled:opacity-50"
                        onClick={() => act('/api/heist/cut', { attempt_id: id, wire: i })}>
                  {w}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="py-6 text-sm text-muted-foreground">🤫 صبر کن…</div>
        )}
      </div>
    )
  }

  // stage 2 — the vault. The thief's job, one symbol at a time.
  if (run.stage === 2) {
    return (
      <div className="space-y-3 text-center">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">🔓 مرحلهٔ ۲ — گاوصندوق</div>
          <Clock s={run.seconds_left} />
        </div>
        {!run.is_thief ? (
          <div className="py-6 text-sm text-muted-foreground">
            نوبت <b>{run.thief}</b>ه — داره گاوصندوق رو باز می‌کنه.
          </div>
        ) : run.phase === 'reveal' ? (
          <div className="py-8">
            <div className="text-6xl">{symbols[run.show]}</div>
            <div className="mt-3 text-xs text-muted-foreground tnum">
              {run.step + 1} از {run.length}
            </div>
          </div>
        ) : run.phase === 'blank' ? (
          <div className="py-12 text-sm text-muted-foreground">…</div>
        ) : run.phase === 'recall' ? (
          <div className="space-y-3">
            <div className="text-sm">به همون ترتیب بزن — <b className="tnum">{run.progress}</b> از {run.length}</div>
            <div className="grid grid-cols-3 gap-2">
              {symbols.map((sy, i) => (
                <button key={i} disabled={busy}
                        className="rounded-md border py-3 text-2xl transition-colors hover:bg-accent disabled:opacity-50"
                        onClick={() => act('/api/heist/tap', { attempt_id: id, symbol: i })}>
                  {sy}
                </button>
              ))}
            </div>
          </div>
        ) : <div className="py-8 text-sm text-muted-foreground">👀 حاضر شو…</div>}
      </div>
    )
  }

  // stage 3 — the getaway. Both, or neither.
  return (
    <div className="space-y-3 text-center">
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold">🏃 مرحلهٔ ۳ — فرار</div>
        <Clock s={run.seconds_left} />
      </div>
      <div className="text-xs text-muted-foreground">هر دوتون باید بزنین بیرون!</div>
      <div className="space-y-1 text-sm">
        <div>{run.escape_thief ? '✅' : '⬜️'} {run.thief}</div>
        <div>{run.escape_partner ? '✅' : '⬜️'} {run.partner}</div>
      </div>
      <Button size="lg" disabled={busy || (run.is_thief ? run.escape_thief : run.escape_partner)}
              onClick={() => act('/api/heist/escape', { attempt_id: id })}>
        🏃‍♂️ فرار!
      </Button>
    </div>
  )
}
