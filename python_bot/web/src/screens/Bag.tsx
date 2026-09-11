import { useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { fa } from '@/lib/format'
import { api } from '@/lib/api'
import { useToast } from '@/components/Toast'

export function Bag({ d, chat, reload }: { d: any; chat: number; reload: () => Promise<void> }) {
  const [busy, setBusy] = useState<string | null>(null)
  const toast = useToast()

  if (!d.items.length) {
    return (
      <div className="py-16 text-center text-sm text-muted-foreground">
        کوله‌ت خالیه — از فروشگاه بخر یا با /d شانس بیار
      </div>
    )
  }

  const use = async (name: string) => {
    setBusy(name)
    try {
      const r = await api<any>('/api/inventory/use', { item: name }, chat)
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
      {d.items.map((it: any) => (
        <Card key={it.name}>
          <CardContent className="pt-4">
            <div className="flex items-center justify-between gap-3">
              <b>{it.name} × {fa(it.count)}</b>
              {it.usable && (
                <Button variant="secondary" size="sm" disabled={busy === it.name} onClick={() => use(it.name)}>
                  فعال‌کردن
                </Button>
              )}
            </div>
            <div className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{it.desc}</div>
            {!it.usable && (
              <div className="mt-2 text-xs text-muted-foreground">این آیتم رو باید توی گروه استفاده کنی</div>
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  )
}
