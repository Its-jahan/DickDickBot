import { Card, CardContent } from '@/components/ui/card'
import { fa, num } from '@/lib/format'
import { cn } from '@/lib/utils'

const MEDAL = ['🥇', '🥈', '🥉']

export function Top({ d }: { d: any }) {
  // Defence in depth. App gates rendering on the payload belonging to this tab, so an
  // empty list here should be a real empty list - but a screen must never be the thing
  // that takes the whole app down, which is exactly what .rows.length on undefined did.
  const rows: any[] = d?.rows ?? []
  if (!rows.length) {
    return <div className="py-16 text-center text-sm text-muted-foreground">هنوز کسی بازی نکرده</div>
  }
  return (
    <Card>
      <CardContent className="divide-y pt-2">
        {rows.map((r: any, i: number) => (
          <div
            key={r.user_id}
            className={cn('flex items-center gap-3 py-2.5', r.user_id === d.me && '-mx-2 rounded-md bg-primary/10 px-2')}
          >
            <span className="w-7 text-center text-sm text-muted-foreground tnum">
              {i < 3 ? MEDAL[i] : fa(i + 1)}
            </span>
            <span className="min-w-0 flex-1 truncate">
              {r.name} {r.king ? '👑' : ''}{r.consort ? '💍' : ''}
              {r.streak > 1 && <span className="ms-1 text-xs text-muted-foreground">🔥{fa(r.streak)}</span>}
            </span>
            <b className="tnum">{num(r.size)}</b>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}
