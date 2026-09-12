import { Card, CardContent, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { fa, num } from '@/lib/format'
import { TG } from '@/lib/tg'
import { ArrowLeftRight, Users, LogOut, VenetianMask, Gift, Sprout, Swords,
         Scale, Crown, Banknote } from 'lucide-react'
import type { ActionKind } from '@/screens/Actions'
import type { GroupKind } from '@/screens/Group'

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-md bg-secondary p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-lg font-bold tnum">{value}</div>
      {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  )
}

export function Home({ d, onPickGroup, onTransfer, onLogout, onAction, onGroup, onGrow, onHeist }: {
  d: any
  onPickGroup: () => void
  onTransfer: () => void
  onLogout: () => void
  onAction: (k: ActionKind) => void
  onGroup: (k: GroupKind) => void
  onGrow: () => void
  onHeist: () => void
}) {
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
        <CardContent className="pt-4">
          <CardTitle className="mb-2 text-base">🏆 جدول گروه</CardTitle>
          <div className="divide-y">
            {(d?.board ?? []).map((r: any, i: number) => (
              <div key={r.user_id}
                   className={cn('flex items-center gap-3 py-2',
                     r.user_id === d.me_id && '-mx-2 rounded-md bg-primary/10 px-2')}>
                <span className="w-6 text-center text-sm text-muted-foreground tnum">
                  {i < 3 ? ['🥇', '🥈', '🥉'][i] : fa(i + 1)}
                </span>
                <span className="min-w-0 flex-1 truncate">
                  {r.name} {r.king ? '👑' : ''}{r.consort ? '💍' : ''}
                  {r.streak > 1 && <span className="ms-1 text-xs text-muted-foreground">🔥{fa(r.streak)}</span>}
                </span>
                <b className="tnum">{num(r.size)}</b>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-3 pt-4">
          <div className="text-xs text-muted-foreground">
            همه‌چی از همین‌جا — ربات نتیجه رو تو گروه اعلام می‌کنه:
          </div>
          <Button size="lg" disabled={d.grown_today} onClick={onGrow}>
            <Sprout className="h-4 w-4" />
            {d.grown_today ? 'امروز رشد کردی' : 'رشد روزانه'}
          </Button>
          <div className="grid grid-cols-2 gap-2.5">
            <Button variant="secondary" onClick={() => onAction('steal')}>
              <VenetianMask className="h-4 w-4" /> دزدی
            </Button>
            <Button variant="secondary" onClick={() => onAction('donate')}>
              <Gift className="h-4 w-4" /> اهدای سایز
            </Button>
            <Button variant="secondary" onClick={() => onGroup('challenge')}>
              <Swords className="h-4 w-4" /> چالش
            </Button>
            <Button variant="secondary" onClick={() => onGroup('ejma')}>
              <Scale className="h-4 w-4" /> اجماع
            </Button>
            <Button variant="secondary" onClick={() => onGroup('decree')}>
              <Crown className="h-4 w-4" /> فرمان
            </Button>
            <Button variant="secondary" onClick={onHeist}>
              <Banknote className="h-4 w-4" /> سرقت از بانک
            </Button>
          </div>
          <div className="flex flex-wrap gap-2 border-t pt-3">
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
        </CardContent>
      </Card>
    </div>
  )
}
