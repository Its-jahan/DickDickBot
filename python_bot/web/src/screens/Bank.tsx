import { useState } from 'react'
import { Card, CardContent, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { fa, num, pc } from '@/lib/format'
import { api } from '@/lib/api'
import { useToast } from '@/components/Toast'

function Row({ label, value, tone }: { label: string; value: string; tone?: 'up' | 'dn' }) {
  return (
    <div className="flex items-center justify-between py-1">
      <span className="text-sm text-muted-foreground">{label}</span>
      <b className={tone === 'up' ? 'text-success tnum' : tone === 'dn' ? 'text-destructive tnum' : 'tnum'}>{value}</b>
    </div>
  )
}

export function Bank({ d, chat, reload }: { d: any; chat: number; reload: () => Promise<void> }) {
  const [mode, setMode] = useState<'in' | 'out'>('in')
  const [amount, setAmount] = useState('')
  const [busy, setBusy] = useState(false)
  const toast = useToast()
  const net = d.rate - d.maintenance

  const go = async () => {
    const v = Number(amount)
    if (!v) return toast('مقدار رو بنویس', true)
    setBusy(true)
    try {
      const r = await api<any>('/api/bank/' + (mode === 'in' ? 'deposit' : 'withdraw'), { amount: v }, chat)
      setAmount('')
      toast(r.message)
      await reload()
    } catch (e: any) {
      toast(e.message, true)
    } finally {
      setBusy(false)
    }
  }

  const used = d.cap ? 100 - Math.round((d.remaining / d.cap) * 100) : 0

  return (
    <div className="space-y-3">
      <Card>
        <CardContent className="pt-4">
          <Row label="جیب (قابل دزدیدن)" value={num(d.wallet)} />
          <Row label="بانک (امن از دزدی)" value={num(d.balance)} />
          <Row label="سقف واریز امروز" value={`${fa(d.remaining)} از ${fa(d.cap)}`} />
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-secondary">
            <div className="h-full bg-primary transition-all" style={{ width: `${used}%` }} />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="pt-4">
          <Row label="سود روزانه" value={`${num(d.rate * 100, 2)}٪`} tone="up" />
          <Row label="کارمزد نگهداری" value={`−${num(d.maintenance * 100, 2)}٪`} tone="dn" />
          <div className="mt-1 flex items-center justify-between border-t pt-2">
            <span className="text-sm font-semibold">خالص برای تو</span>
            <b className={net >= 0 ? 'text-success tnum' : 'text-destructive tnum'}>{pc(net * 100)}</b>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-3 pt-4">
          <Tabs value={mode} onValueChange={(v) => setMode(v as 'in' | 'out')}>
            <TabsList>
              <TabsTrigger value="in">واریز</TabsTrigger>
              <TabsTrigger value="out">برداشت</TabsTrigger>
            </TabsList>
          </Tabs>
          <Input
            type="number"
            inputMode="numeric"
            placeholder="مقدار سانت"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
          />
          <Button size="lg" disabled={busy} onClick={go}>
            {mode === 'in' ? 'واریز به بانک' : 'برداشت از بانک'}
          </Button>
          <div className="text-center text-xs text-muted-foreground">
            {mode === 'in'
              ? `کارمزد واریز ${num(d.deposit_fee * 100)}٪`
              : `کارمزد برداشت ${num(d.withdraw_fee * 100)}٪`}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="pt-4">
          <CardTitle className="mb-2 text-base">🏛 بانک مرکزی</CardTitle>
          <Row label="ذخیره" value={num(d.reserve)} />
          <Row label="سپردهٔ مردم (بدهی بانک)" value={num(d.deposits)} />
          <Row label="وام‌های بیرون‌رفته" value={num(d.loans_out)} />
          <Row label="نقدِ قابل‌برداشت" value={num(d.cash)} />
          <Row label="نرخ وام" value={`${num(d.loan_rate * 100)}٪`} />
        </CardContent>
      </Card>
    </div>
  )
}
