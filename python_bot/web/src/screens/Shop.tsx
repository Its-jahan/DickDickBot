import { useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { fa, num } from '@/lib/format'
import { api } from '@/lib/api'
import { useToast } from '@/components/Toast'
import { TG, haptic } from '@/lib/tg'

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

  const waitForDelivery = async (orderId: string) => {
    for (let attempt = 0; attempt < 15; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000))
      const order = await api<any>(`/api/stars/order/${orderId}`, undefined, chat)
      if (order.status === 'fulfilled') return true
      if (order.status === 'failed') return false
    }
    return false
  }

  const buyStars = async (sku: string) => {
    setBusy(`stars:${sku}`)
    try {
      const invoice = await api<any>('/api/stars/invoice', { sku }, chat)
      if (!TG?.openInvoice) {
        window.open(invoice.invoice_url, '_blank', 'noopener,noreferrer')
        toast('فاکتور تلگرام باز شد؛ بعد از پرداخت، خرید خودکار تحویل می‌شود.')
        return
      }
      const status = await new Promise<string>((resolve) => TG.openInvoice(invoice.invoice_url, resolve))
      if (status !== 'paid') {
        if (status !== 'cancelled') toast('پرداخت کامل نشد', true)
        return
      }
      const delivered = await waitForDelivery(invoice.order_id)
      if (delivered) {
        haptic('success'); toast('پرداخت تأیید شد و خرید تحویل گرفت ✅'); await reload()
      } else {
        toast('پرداخت ثبت شد؛ تحویل در حال پردازش است. کمی بعد صفحه را تازه کن.', true)
      }
    } catch (e: any) {
      haptic('error'); toast(e.message, true)
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
      <Card>
        <CardContent className="pt-4">
          <div className="mb-1 font-bold">بسته‌های سانتی با Telegram Stars</div>
          <p className="mb-3 text-xs leading-relaxed text-muted-foreground">
            پرداخت داخل تلگرام انجام می‌شود و بعد از تأیید رسمی، به همین گروه اضافه می‌شود.
          </p>
          <div className="grid grid-cols-3 gap-2">
            {(d?.star_packages ?? []).map((pack: any) => (
              <Button key={pack.sku} variant="outline" className="h-auto flex-col gap-1 py-3"
                      disabled={!!busy}
                      onClick={() => buyStars(pack.sku)}>
                <span>{fa(pack.quantity)} سانت</span>
                <span className="text-xs text-primary">{fa(pack.stars)} ⭐</span>
              </Button>
            ))}
          </div>
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
              <div className="mt-3 space-y-2.5">
                <Badge variant={out ? 'destructive' : 'default'}>
                  {out ? 'موجودی تموم شد' : `امروز ${fa(it.day_left)} · این هفته ${fa(it.week_left)}`}
                </Badge>
                <div className="grid grid-cols-2 gap-2">
                  <Button size="sm" variant="outline" className="w-full"
                          disabled={!!busy}
                          onClick={() => buyStars(it.sku)}>
                    {fa(it.stars)} ⭐
                  </Button>
                  <Button size="sm" className="w-full"
                          disabled={out || d.wallet < it.price || !!busy}
                          onClick={() => buy(it.name)}>
                    خرید با سانت
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}
