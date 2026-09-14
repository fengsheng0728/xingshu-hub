import DataArchitecture from '../sections/DataArchitecture'
import SystemArchitecture from '../sections/SystemArchitecture'
import { StatusBadge } from '../sections/Shared'

export default function Home() {
  return (
    <div className="min-h-screen bg-zinc-50 font-sans text-zinc-900">
      <header className="sticky top-0 z-10 border-b border-zinc-200 bg-white/90 backdrop-blur">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div>
            <h1 className="text-lg font-medium leading-6">星枢架构</h1>
            <p className="text-xs text-zinc-400">数据底座 × 全系统三层视图 · v0.2 拍板版（2026-08-30）</p>
          </div>
          <nav className="flex items-center gap-2 text-sm">
            <a href="#system" className="rounded-md border border-zinc-200 px-3 py-1 text-zinc-600 hover:bg-zinc-100">
              全系统架构
            </a>
            <a href="#data" className="rounded-md border border-zinc-200 px-3 py-1 text-zinc-600 hover:bg-zinc-100">
              数据底座架构
            </a>
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-4 pb-16">
        <div className="mt-6 flex flex-wrap items-center gap-3">
          <span className="text-sm text-zinc-500">咬合状态：</span>
          <StatusBadge tone="positive">已咬合</StatusBadge>
          <StatusBadge tone="warning">半咬合 · 缺环节</StatusBadge>
          <StatusBadge tone="danger">待改造 / 待建</StatusBadge>
        </div>

        <SystemArchitecture />
        <DataArchitecture />

        <footer className="mt-16 border-t border-zinc-200 pt-4 text-xs text-zinc-400">
          依据：anc-subtraction SPEC/PLAN · anc-边界问题-交流表 · 原版 sensitivity / key_scopes / disclosure / audit_chain 资产 · 2026-08-30 设计对齐结论
        </footer>
      </main>
    </div>
  )
}
