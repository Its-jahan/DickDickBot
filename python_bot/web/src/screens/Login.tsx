import { useEffect, useRef, useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'

/**
 * The Login Widget, for players in an ordinary browser. Inside Telegram this screen is
 * never reached, because initData already identifies the player.
 */
export function Login() {
  const host = useRef<HTMLDivElement>(null)
  const [botName, setBotName] = useState<string | null>(null)
  const [broken, setBroken] = useState(false)

  useEffect(() => {
    fetch('/api/config').then((r) => r.json())
      .then((j) => setBotName(j.bot || 'dickchallengerbot'))
      .catch(() => setBotName('dickchallengerbot'))
  }, [])

  useEffect(() => {
    if (!botName || !host.current) return
    const sc = document.createElement('script')
    sc.async = true
    sc.src = 'https://telegram.org/js/telegram-widget.js?22'
    sc.setAttribute('data-telegram-login', botName)
    sc.setAttribute('data-size', 'large')
    sc.setAttribute('data-radius', '12')
    sc.setAttribute('data-request-access', 'write')
    sc.setAttribute('data-onauth', 'onTelegramAuth(user)')
    host.current.appendChild(sc)

    // The widget renders an <iframe> when it works. When the domain is not the one
    // registered with BotFather it instead prints a bare English "Bot domain invalid",
    // which tells a player nothing and tells the owner nothing about the fix either.
    const t = window.setTimeout(() => {
      if (!host.current?.querySelector('iframe')) setBroken(true)
    }, 3500)
    return () => window.clearTimeout(t)
  }, [botName])

  return (
    <div className="px-1 py-10 text-center">
      <div className="text-5xl">🍆</div>
      <h1 className="mt-4 text-2xl font-extrabold">دودول</h1>
      <p className="mx-auto mt-2 max-w-xs text-sm text-muted-foreground">
        با اکانت تلگرامت وارد شو تا سایز، بانک، بازار و کوله‌ت رو ببینی.
      </p>
      <div ref={host} className="mt-7 flex justify-center" />
      {broken && (
        <Card className="mt-5 text-right">
          <CardContent className="space-y-2 pt-4">
            <b>ورود با مرورگر هنوز فعال نیست</b>
            <p className="text-xs text-muted-foreground">
              تلگرام این دامنه رو برای این بات نمی‌شناسه، برای همین دکمهٔ ورود بالا نمیاد.
            </p>
            <p className="text-xs text-muted-foreground">
              <b>فعلاً از داخل تلگرام بازی کن:</b> توی گروه بزن <b>/app</b> — همه‌چیز کامل کار می‌کنه.
            </p>
            <p className="text-[11px] text-muted-foreground">
              (برای ادمین: توی <b>@BotFather</b> بزن <b>/setdomain</b>، بات رو انتخاب کن،
              و بفرست <b>{location.hostname}</b>)
            </p>
          </CardContent>
        </Card>
      )}
      <p className="mt-7 text-xs leading-relaxed text-muted-foreground">
        رمزی در کار نیست — تلگرام هویتت رو امضا می‌کنه و ما همون امضا رو چک می‌کنیم.
        <br />
        اگه توی تلگرامی، می‌تونی توی گروه <b>/app</b> بزنی.
      </p>
    </div>
  )
}
