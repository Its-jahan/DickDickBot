import { Home, Bell, LineChart, Landmark, Store, Backpack, Moon, Sun } from 'lucide-react'
import { cn } from '@/lib/utils'
import { currentTheme, setTheme } from '@/lib/theme'
import { tap } from '@/lib/tg'
import { useState } from 'react'
import { Button } from '@/components/ui/button'

export type TabKey = 'home' | 'feed' | 'crypto' | 'bank' | 'shop' | 'bag'

const TABS: [TabKey, typeof Home, string][] = [
  ['home', Home, 'خونه'],
  ['feed', Bell, 'رویدادها'],
  ['crypto', LineChart, 'بازار'],
  ['bank', Landmark, 'بانک'],
  ['shop', Store, 'فروشگاه'],
  ['bag', Backpack, 'کوله'],
]

export function BottomNav({ tab, onTab, unread }: {
  tab: TabKey; onTab: (t: TabKey) => void; unread?: number
}) {
  return (
    <nav className="fixed inset-x-0 bottom-0 z-40 border-t bg-background/85 backdrop-blur-xl">
      <div className="mx-auto flex max-w-2xl px-1 pb-[env(safe-area-inset-bottom)] pt-1.5">
        {TABS.map(([key, Icon, label]) => (
          <button
            key={key}
            onClick={() => { tap(); onTab(key) }}
            className={cn(
              'flex flex-1 flex-col items-center gap-1 rounded-md py-1.5 text-[10px] font-semibold transition-colors',
              tab === key ? 'text-primary' : 'text-muted-foreground'
            )}
          >
            <span className="relative">
              <Icon className="h-5 w-5" strokeWidth={tab === key ? 2.4 : 1.8} />
              {key === 'feed' && !!unread && (
                <span className="absolute -end-1.5 -top-1 min-w-[15px] rounded-full bg-destructive px-1
                                 text-[9px] font-bold leading-[15px] text-destructive-foreground">
                  {unread > 9 ? '۹+' : unread.toLocaleString('fa-IR')}
                </span>
              )}
            </span>
            {label}
          </button>
        ))}
      </div>
    </nav>
  )
}

export function ThemeToggle() {
  const [t, setT] = useState(currentTheme())
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label="تغییر تم"
      onClick={() => { const n = t === 'dark' ? 'light' : 'dark'; setTheme(n); setT(n); tap() }}
    >
      {t === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
    </Button>
  )
}
