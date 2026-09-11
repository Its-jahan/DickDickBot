import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { haptic } from '@/lib/tg'

type Toast = { text: string; bad?: boolean } | null
const Ctx = createContext<(text: string, bad?: boolean) => void>(() => {})

export const useToast = () => useContext(Ctx)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [t, setT] = useState<Toast>(null)

  const show = useCallback((text: string, bad?: boolean) => {
    setT({ text, bad })
    haptic(bad ? 'error' : 'success')
    window.setTimeout(() => setT(null), 3400)
  }, [])

  return (
    <Ctx.Provider value={show}>
      {children}
      <div
        className={cn(
          'pointer-events-none fixed inset-x-4 bottom-24 z-[60] mx-auto max-w-md rounded-lg border bg-card px-4 py-3 text-sm shadow-xl transition-all',
          t ? 'translate-y-0 opacity-100' : 'translate-y-3 opacity-0',
          t?.bad && 'border-destructive/50'
        )}
        role="status"
      >
        {t?.text}
      </div>
    </Ctx.Provider>
  )
}
