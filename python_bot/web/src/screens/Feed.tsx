import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { num } from '@/lib/format'

const ICON: Record<string, string> = {
  growth: '🌱', theft: '🥷', donation: '🎁', challenge: '⚔️', transfer: '🔁',
  night: '🌙', heist: '🚨', crypto: '📈', bank: '🏦', shop: '🏪', lottery: '🎟️',
  stars: '⭐',
}

const LABEL: Record<string, string> = {
  growth: 'رشد', theft: 'دزدی', donation: 'اهدا', challenge: 'چالش', transfer: 'انتقال',
  night: 'گزارش شبانه', heist: 'سرقت', crypto: 'بازار', bank: 'بانک', shop: 'فروشگاه',
  lottery: 'لاتاری', stars: 'خرید Stars',
}

const clock = (t: number) =>
  new Date(t * 1000).toLocaleTimeString('fa-IR', { hour: '2-digit', minute: '2-digit' })

/**
 * Today's log, and only today's - the day is the unit a player thinks in, and an
 * endless scroll of last week is not a notification screen. Nothing is deleted: the
 * server keeps every event forever, this view just asks for today.
 */
export function Feed({ d }: { d: any }) {
  if (!d.events.length) {
    return (
      <div className="py-16 text-center text-sm leading-loose text-muted-foreground">
        🔔<br /><br />
        امروز هنوز اتفاقی نیفتاده.<br />
        هر رشد، دزدی، چالش و انتقالی که بیفته اینجا میاد.
      </div>
    )
  }

  return (
    <div className="space-y-2.5">
      <div className="px-1 text-xs text-muted-foreground">
        همه‌چیزِ امروز ({d.day}) — فردا این صفحه از نو شروع می‌شه، ولی هیچی پاک نمی‌شه.
      </div>
      {d.events.map((e: any) => (
        <Card key={e.id}>
          <CardContent className="py-3">
            <div className="mb-1.5 flex items-center gap-2">
              <span className="text-base">{ICON[e.kind] ?? '•'}</span>
              <Badge variant={e.private ? 'default' : 'primary'}>
                {LABEL[e.kind] ?? e.kind}
              </Badge>
              {e.private && <Badge>فقط تو می‌بینی</Badge>}
              <span className="ms-auto text-[11px] text-muted-foreground tnum">{clock(e.t)}</span>
            </div>
            <div className="whitespace-pre-wrap break-words text-sm leading-relaxed"
                 dangerouslySetInnerHTML={{ __html: safe(e.text) }} />
            {e.amount ? (
              <div className="mt-1 text-xs text-muted-foreground tnum">{num(e.amount)} سانت</div>
            ) : null}
          </CardContent>
        </Card>
      ))}
    </div>
  )
}

/**
 * Event text is written by the game, never by a player - but it arrives already
 * containing the <b> tags the chat copy uses, so it cannot simply be escaped and it
 * must not be trusted wholesale either. Only the handful of tags the game emits survive;
 * everything else is neutralised.
 */
function safe(text: string): string {
  const escaped = String(text ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return escaped.replace(/&lt;(\/?)(b|i|u|s|code)&gt;/g, '<$1$2>')
}
