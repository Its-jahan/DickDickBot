import { useEffect, useState } from 'react'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { num } from '@/lib/format'
import { cn } from '@/lib/utils'

export type Player = { user_id: number; name: string; size: number; king: boolean; consort: boolean }

/** Everyone in the group except you. Shared by every action that needs a target. */
export function PlayerPicker({ chat, value, onPick, exclude }: {
  chat: number
  value: number | null
  onPick: (id: number, name: string) => void
  exclude?: (p: Player) => boolean
}) {
  const [players, setPlayers] = useState<Player[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [me, setMe] = useState<number | null>(null)

  useEffect(() => {
    let alive = true
    api<any>('/api/players', undefined, chat)
      .then((r) => { if (!alive) return; setPlayers(r.players); setMe(r.me) })
      .catch((e) => { if (alive) setErr(e.message) })
    return () => { alive = false }
  }, [chat])

  if (err) return <p className="py-4 text-center text-sm text-muted-foreground">{err}</p>
  if (!players) return <Skeleton className="h-32 w-full" />

  const list = players.filter((p) => p.user_id !== me && (!exclude || !exclude(p)))
  if (!list.length) {
    return <p className="py-4 text-center text-sm text-muted-foreground">کسی اینجا نیست</p>
  }

  return (
    <div className="max-h-56 divide-y overflow-y-auto rounded-md border">
      {list.map((p) => (
        <button
          key={p.user_id}
          onClick={() => onPick(p.user_id, p.name)}
          className={cn('flex w-full items-center justify-between p-3 text-right text-sm',
            value === p.user_id && 'bg-primary/10')}
        >
          <span className="min-w-0 truncate">
            {value === p.user_id ? '● ' : '○ '}{p.name} {p.king ? '👑' : ''}{p.consort ? '💍' : ''}
          </span>
          <span className="shrink-0 text-muted-foreground tnum">{num(p.size)}</span>
        </button>
      ))}
    </div>
  )
}
