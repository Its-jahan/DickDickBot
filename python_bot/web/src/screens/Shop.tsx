import { useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { fa, num } from '@/lib/format'
import { api } from '@/lib/api'
import { useToast } from '@/components/Toast'

export function Shop({ d, chat, reload }: { d: any; chat: number; reload: () => Promise<void> }) {
  const [busy, setBusy] = useState<string | null>(null)
  const toast = useToast()

  const buy = async (name: string) => {
    setBusy(name)
    try {
      const r = await api<any>('/api/shop/buy', { item: name }, chat)
      toast(r.message)
      await reload()
    } catch (e: any) {
      toast(e.message, true)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="space-y-3">
      <Card>
        <CardContent className="flex items-center justify-between pt-4">
          <span className="text-sm text-muted-foreground">جیب</span>
          <b className="tnum">{num(d.wallet)} سانت</b>
        </CardContent>
      </Card>
      {(d?.items ?? []).map((it: any) => {
        const out = it.day_left <= 0 || it.week_left <= 0
        return (
          <Card key={it.name}>
            <CardContent className="pt-4">
              <div className="flex items-start justify-between gap-3">
                <b>{it.name}</b>
                <b className="shrink-0 tnum text-primary">{fa(it.price)} سانت</b>
              </div>
              <div className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{it.desc}</div>
              <div className="mt-3 flex items-center justify-between gap-2">
                <Badge variant={out ? 'destructive' : 'default'}>
                  {out ? 'موجودی تموم شد' : `امروز ${fa(it.day_left)} · این هفته ${fa(it.week_left)}`}
                </Badge>
                <Button
                  size="sm"
                  disabled={out || d.wallet < it.price || busy === it.name}
                  onClick={() => buy(it.name)}
                >
                  خرید
                </Button>
              </div>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}
