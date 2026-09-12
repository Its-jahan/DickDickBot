import { useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Sheet, SheetContent } from '@/components/ui/sheet'
import { PlayerPicker } from '@/components/PlayerPicker'
import { fa } from '@/lib/format'
import { api } from '@/lib/api'
import { useToast } from '@/components/Toast'

/**
 * `kind` / `usable` / `needs_target` come from the server, which reads bot.py's own item
 * buckets. This screen used to keep its own hardcoded list of usable names, so an item
 * added to a bucket in bot.py stayed dead here forever - the drift bug class this repo
 * keeps getting bitten by.
 */
export function Bag({ d, chat, reload }: { d: any; chat: number; reload: () => Promise<void> }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [aim, setAim] = useState<any>(null)       // the direct item awaiting a target
  const [target, setTarget] = useState<number | null>(null)
  const toast = useToast()

  const items: any[] = d?.items ?? []

  const use = async (name: string, targetId?: number | null) => {
    setBusy(name)
    try {
      const r = await api<any>('/api/inventory/use',
        targetId ? { item: name, target: targetId } : { item: name }, chat)
      toast(r.message)
      setAim(null)
      setTarget(null)
      await reload()
    } catch (e: any) {
      toast(e.message, true)
    } finally {
      setBusy(null)
    }
  }

  if (!items.length) {
    return (
      <div className="py-16 text-center text-sm text-muted-foreground">
        کوله‌ت خالیه — از فروشگاه بخر یا با /d شانس بیار
      </div>
    )
  }

  return (
    <>
      <div className="space-y-3">
        {items.map((it: any) => (
          <Card key={it.name}>
            <CardContent className="pt-4">
              <div className="flex items-center justify-between gap-3">
                <b className="min-w-0 truncate">{it.name} × {fa(it.count)}</b>
                <div className="flex shrink-0 items-center gap-2">
                  {it.needs_target && <Badge>🎯 روی یکی</Badge>}
                  {it.usable && (
                    <Button variant="secondary" size="sm" disabled={busy === it.name}
                            onClick={() => (it.needs_target ? (setAim(it), setTarget(null)) : use(it.name))}>
                      {it.needs_target ? 'استفاده' : 'فعال‌کردن'}
                    </Button>
                  )}
                </div>
              </div>
              <div className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{it.desc}</div>
              {!it.usable && (
                <div className="mt-2 text-xs text-muted-foreground">
                  این آیتم خودکار کار می‌کنه — لازم نیست فعالش کنی.
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>

      {aim && (
        <Sheet open onOpenChange={(o) => !o && setAim(null)}>
          <SheetContent title={aim.name}>
            <div className="mb-1 text-lg font-bold">🎒 {aim.name}</div>
            <p className="mb-3 text-xs leading-relaxed text-muted-foreground">{aim.desc}</p>
            <div className="space-y-3">
              <PlayerPicker chat={chat} value={target} onPick={setTarget} />
              <Button size="lg" disabled={busy === aim.name || !target}
                      onClick={() => use(aim.name, target)}>
                استفاده کن
              </Button>
              <p className="text-center text-[11px] leading-relaxed text-muted-foreground">
                📣 این روی سایز یکی دیگه اثر می‌ذاره، پس ربات توی گروه اعلامش می‌کنه.
              </p>
            </div>
          </SheetContent>
        </Sheet>
      )}
    </>
  )
}
