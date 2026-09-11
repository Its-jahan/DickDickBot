import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { fa, num } from '@/lib/format'
import { TG } from '@/lib/tg'
import { ArrowLeftRight, Users, LogOut } from 'lucide-react'
import { useToast } from '@/components/Toast'

const GROUP_ONLY: [string, string][] = [
  ['d', 'رشد روزانه'], ['c', 'چالش'], ['dozdi', 'دزدی'],
  ['ejma', 'اجماع'], ['sarghat', 'سرقت از بانک'], ['farman', 'فرمان'],
]

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-md bg-secondary p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-lg font-bold tnum">{value}</div>
      {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  )
}

export function Home({ d, onPickGroup, onTransfer, onLogout }: {
  d: any
  onPickGroup: () => void
  onTransfer: () => void
  onLogout: () => void
}) {
  const toast = useToast()
  return (
    <div className="space-y-3">
      <Card>
        <CardContent className="pt-4">
          <div className="flex items-center justify-between">
            <span className="text-sm text-muted-foreground">سایز تو</span>
            <Badge variant="primary">
              {d.rank ? `رتبه ${fa(d.rank)} از ${fa(d.players)}` : 'بدون رتبه'}
            </Badge>
          </div>
          <div className="mt-1 text-4xl font-extrabold tracking-tight tnum">
            {num(d.size)}
            <span className="ms-2 text-base font-medium text-muted-foreground">سانت</span>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <Badge variant={d.perk && d.perk !== 'عادی' ? 'primary' : 'default'}>
              {d.perk && d.perk !== 'عادی' ? `✨ ${d.perk}` : 'بدون پرک'}
            </Badge>
            <Badge variant={d.grown_today ? 'success' : 'default'}>
              {d.grown_today ? '✅ امروز رشد کردی' : '⏳ امروز رشد نکردی'}
            </Badge>
          </div>
        </CardContent>
      </Card>

      <div className="grid grid-cols-2 gap-2.5">
        <Stat label="بانک" value={num(d.bank)}
              sub={`${num(d.bank_rate * 100, 2)}٪ − ${num(d.maintenance * 100, 2)}٪ روزانه`} />
        <Stat label="سبد کریپتو" value={num(d.portfolio)} sub="تا نفروشی سایز نیست" />
        <Stat label="پادشاه" value={d.is_king ? '👑 خودتی' : (d.king || '—')}
              sub={d.consort ? `💍 ${d.consort}` : 'بدون همسر'} />
        <Stat label="تورم" value={`${num(d.inflation, 2)}×`}
              sub={`خشم مردم ${fa(Math.round(d.unrest))}`} />
      </div>

      <Card>
        <CardContent className="space-y-3 pt-4">
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" size="sm" onClick={onPickGroup}>
              <Users className="h-3.5 w-3.5" /> تعویض گروه
            </Button>
            <Button size="sm" onClick={onTransfer}>
              <ArrowLeftRight className="h-3.5 w-3.5" /> انتقال بین‌گروهی
            </Button>
            {!TG?.initData && (
              <Button variant="ghost" size="sm" onClick={onLogout}>
                <LogOut className="h-3.5 w-3.5" /> خروج
              </Button>
            )}
          </div>
          <div className="text-xs text-muted-foreground">
            اینا فقط توی گروه کار می‌کنن، چون بقیه باید ببیننشون:
          </div>
          <div className="flex flex-wrap gap-2">
            {GROUP_ONLY.map(([c, l]) => (
              <button key={c} onClick={() => { if (TG) TG.close(); toast('توی گروه بزن: /' + c) }}>
                <Badge className="cursor-pointer">/{c} · {l}</Badge>
              </button>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
