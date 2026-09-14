import type { ReactNode } from 'react'

export type Tone = 'positive' | 'warning' | 'danger'

const toneCls: Record<Tone, string> = {
  positive: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  warning: 'bg-amber-50 text-amber-700 border-amber-200',
  danger: 'bg-rose-50 text-rose-700 border-rose-200',
}

export function StatusBadge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`inline-block rounded-md border px-2 py-0.5 text-xs leading-5 ${toneCls[tone]}`}>
      {children}
    </span>
  )
}

export function LayerCard({
  title,
  desc,
  tone,
  status,
}: {
  title: string
  desc: string
  tone: Tone
  status: string
}) {
  return (
    <div className="min-w-[150px] flex-1 basis-[150px] rounded-lg border border-zinc-200 bg-white px-3 py-2">
      <div className="text-sm font-medium leading-5 text-zinc-900">{title}</div>
      <div className="mt-0.5 text-xs leading-[18px] text-zinc-500">{desc}</div>
      <div className="mt-2">
        <StatusBadge tone={tone}>{status}</StatusBadge>
      </div>
    </div>
  )
}

export function Layer({
  name,
  sub,
  children,
}: {
  name: string
  sub: string
  children: ReactNode
}) {
  return (
    <div className="rounded-xl border border-zinc-200 bg-zinc-50/60 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <span className="text-[15px] font-medium text-zinc-900">{name}</span>
        <span className="text-xs text-zinc-400">{sub}</span>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">{children}</div>
    </div>
  )
}

export function Connector({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 px-3 py-1">
      <span className="ml-5 h-5 w-px bg-zinc-400" />
      <span className="text-xs text-zinc-400">{label}</span>
    </div>
  )
}

export function Step({
  num,
  title,
  desc,
  warn,
}: {
  num: number
  title: string
  desc: string
  warn?: boolean
}) {
  return (
    <div
      className={`min-w-[130px] flex-1 basis-[130px] rounded-xl border bg-white p-3 ${
        warn ? 'border-amber-300' : 'border-zinc-200'
      }`}
    >
      <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-zinc-900 text-xs text-white">
        {num}
      </span>
      <div className="mt-2 text-sm font-medium leading-5 text-zinc-900">{title}</div>
      <div className="mt-1 text-xs leading-[18px] text-zinc-500">{desc}</div>
    </div>
  )
}

export function SectionTitle({ id, title, sub }: { id: string; title: string; sub: string }) {
  return (
    <div id={id} className="scroll-mt-20">
      <h2 className="text-xl font-medium text-zinc-900">{title}</h2>
      <p className="mt-1 text-sm text-zinc-500">{sub}</p>
    </div>
  )
}
