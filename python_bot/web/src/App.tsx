import { useCallback, useEffect, useState } from 'react'
import { api, authHeaders, readLogin, ApiError } from '@/lib/api'
import { TG, waitForTelegram } from '@/lib/tg'
import { num } from '@/lib/format'
import { BottomNav, ThemeToggle, type TabKey } from '@/components/Shell'
import { useToast } from '@/components/Toast'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { Home } from '@/screens/Home'
import { Top } from '@/screens/Top'
import { Crypto } from '@/screens/Crypto'
import { Bank } from '@/screens/Bank'
import { Shop } from '@/screens/Shop'
import { Bag } from '@/screens/Bag'
import { Transfer } from '@/screens/Transfer'
import { Login } from '@/screens/Login'

const ENDPOINT: Record<TabKey, string> = {
  home: '/api/home', top: '/api/top', crypto: '/api/crypto',
  bank: '/api/bank', shop: '/api/shop', bag: '/api/inventory',
}
const USABLE = ['دستکش', 'کیسه', 'بلیت طلایی']

type Phase = 'boot' | 'login' | 'groups' | 'empty' | 'play' | 'error'

export default function App() {
  const [phase, setPhase] = useState<Phase>('boot')
  const [error, setError] = useState('')
  const [name, setName] = useState('')
  const [groups, setGroups] = useState<any[]>([])
  const [chat, setChat] = useState<number | null>(null)
  const [tab, setTab] = useState<TabKey>('home')
  const [data, setData] = useState<any | null>(null)
  const [loading, setLoading] = useState(false)
  const [xfer, setXfer] = useState(false)
  const toast = useToast()

  const logout = useCallback(() => {
    try { localStorage.removeItem('login'); localStorage.removeItem('chat') } catch { /* private window */ }
    setChat(null); setData(null); setPhase('login')
  }, [])

  const boot = useCallback(async () => {
    // telegram-web-app.js is async, so at this point it may not have landed yet. Wait a
    // bounded moment for it rather than deciding early and showing a Telegram user the
    // browser login screen.
    await waitForTelegram()
    if (!TG?.initData && !readLogin()) return setPhase('login')
    try {
      const r = await fetch('/api/groups', { headers: authHeaders() })
      const j = await r.json()
      if (!j.ok) {
        // A stored login the server no longer accepts (expired, or the token rotated)
        // must drop the player back to the widget rather than stranding them on an
        // error they cannot act on.
        if (r.status === 403 && readLogin()) return logout()
        throw new ApiError(j.error || 'خطا', r.status)
      }
      setName(j.name)
      setGroups(j.groups)
      if (!j.groups.length) return setPhase('empty')
      let saved: number | null = null
      try { saved = Number(localStorage.getItem('chat')) || null } catch { /* private window */ }
      const pick = j.groups.length === 1 ? j.groups[0].chat_id
        : (saved && j.groups.some((g: any) => g.chat_id === saved) ? saved : null)
      if (pick) { setChat(pick); setPhase('play') } else setPhase('groups')
    } catch (e: any) {
      setError(e.message || 'خطا در اتصال')
      setPhase('error')
    }
  }, [logout])

  useEffect(() => {
    waitForTelegram().then(() => {
      try {
        TG?.ready()
        TG?.expand()
        // No saved choice means follow the client. The pre-paint script in index.html
        // could not read this, because Telegram had not loaded yet.
        let saved: string | null = null
        try { saved = localStorage.getItem('theme') } catch { /* private window */ }
        if (!saved && TG?.colorScheme) {
          document.documentElement.classList.toggle('dark', TG.colorScheme === 'dark')
        }
      } catch { /* not in Telegram */ }
    })
    ;(window as any).onTelegramAuth = (user: any) => {
      try { localStorage.setItem('login', JSON.stringify(user)) } catch { /* private window */ }
      setPhase('boot'); boot()
    }
    boot()
  }, [boot])

  const reload = useCallback(async () => {
    if (!chat) return
    setLoading(true)
    try {
      const d = await api<any>(ENDPOINT[tab], undefined, chat)
      if (tab === 'bag') d.items.forEach((i: any) => { i.usable = USABLE.includes(i.name) })
      setData(d)
    } catch (e: any) {
      setData(null)
      toast(e.message, true)
    } finally {
      setLoading(false)
    }
  }, [chat, tab, toast])

  useEffect(() => { if (phase === 'play') reload() }, [phase, tab, chat, reload])

  const choose = (id: number) => {
    try { localStorage.setItem('chat', String(id)) } catch { /* private window */ }
    setChat(id); setData(null); setTab('home'); setPhase('play')
  }

  const header = (
    <div className="mb-3 flex items-center justify-between">
      <div className="text-sm font-semibold text-muted-foreground">
        {groups.find((g) => g.chat_id === chat)?.title ?? 'دودول'}
      </div>
      <ThemeToggle />
    </div>
  )

  if (phase === 'boot') {
    return <Wrap><Skeleton className="h-32 w-full" /><div className="h-3" /><Skeleton className="h-24 w-full" /></Wrap>
  }
  if (phase === 'login') return <Wrap><Login /></Wrap>
  if (phase === 'error') {
    return <Wrap><div className="py-16 text-center text-sm text-muted-foreground">{error}</div></Wrap>
  }
  if (phase === 'empty') {
    return (
      <Wrap>
        <div className="py-16 text-center text-sm leading-loose text-muted-foreground">
          سلام {name} 👋<br /><br />
          هنوز توی هیچ گروهی بازی نکردی.<br />
          اول توی گروه یه بار <b>/d</b> بزن، بعد برگرد.
        </div>
      </Wrap>
    )
  }
  if (phase === 'groups') {
    return (
      <Wrap>
        <div className="mb-3 flex items-center justify-between">
          <h1 className="text-lg font-bold">سلام {name} 👋</h1>
          <div className="flex items-center gap-1">
            <ThemeToggle />
            {!TG?.initData && <Button variant="ghost" size="sm" onClick={logout}>خروج</Button>}
          </div>
        </div>
        <p className="mb-3 text-sm text-muted-foreground">کدوم گروه؟ هر گروه لیگ جداگانهٔ خودشه.</p>
        <div className="space-y-2.5">
          {groups.map((g) => (
            <Card key={g.chat_id} className="cursor-pointer transition-colors hover:bg-accent"
                  onClick={() => choose(g.chat_id)}>
              <CardContent className="flex items-center justify-between py-4">
                <b className="truncate">{g.title}</b>
                <b className="shrink-0 tnum">{num(g.size)} سانت</b>
              </CardContent>
            </Card>
          ))}
        </div>
      </Wrap>
    )
  }

  return (
    <>
      <Wrap>
        {header}
        {loading && !data ? (
          <><Skeleton className="h-32 w-full" /><div className="h-3" /><Skeleton className="h-28 w-full" /></>
        ) : !data ? (
          <div className="py-16 text-center text-sm text-muted-foreground">چیزی برای نمایش نیست</div>
        ) : tab === 'home' ? (
          <Home d={data} onPickGroup={() => setPhase('groups')} onTransfer={() => setXfer(true)} onLogout={logout} />
        ) : tab === 'top' ? <Top d={data} />
          : tab === 'crypto' ? <Crypto d={data} chat={chat!} reload={reload} />
          : tab === 'bank' ? <Bank d={data} chat={chat!} reload={reload} />
          : tab === 'shop' ? <Shop d={data} chat={chat!} reload={reload} />
          : <Bag d={data} chat={chat!} reload={reload} />}
      </Wrap>
      <Transfer chat={chat!} open={xfer} onClose={() => setXfer(false)} reload={reload} />
      <BottomNav tab={tab} onTab={setTab} />
    </>
  )
}

function Wrap({ children }: { children: React.ReactNode }) {
  return <div className="mx-auto max-w-2xl px-4 py-4">{children}</div>
}
