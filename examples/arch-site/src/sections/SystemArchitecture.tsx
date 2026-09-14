import { Connector, Layer, LayerCard, SectionTitle } from './Shared'

export default function SystemArchitecture() {
  return (
    <section className="mt-16">
      <SectionTitle
        id="system"
        title="全系统架构 · 三层咬合视图"
        sub="应用层 / Agent 运行时 / 数据底座，标注各模块与新数据底座的咬合状态"
      />

      <div className="mt-5">
        <Layer name="应用层" sub="减法版 React 五页 + Express API（:7101）">
          <LayerCard title="Inbox 页" desc="上传 → 队列 → ingest 落库" tone="warning" status="管线在 · 缺清洗打标" />
          <LayerCard title="Vault 页" desc="目录树 + 编辑器 + git 历史" tone="danger" status="待改造 · 单仓拆分干" />
          <LayerCard title="Chat 页" desc="bot 检索回答 + 引用" tone="danger" status="待改造 · 换主干索引+身份" />
          <LayerCard title="Persona 页" desc="七段式 · roles×members 渲染" tone="positive" status="已咬合" />
          <LayerCard title="Audit 页" desc="git log + 哈希链" tone="warning" status="写审计在 · 缺读审计" />
          <LayerCard title="主干档案视图" desc="customers/ canonical 档案 + 归并队列" tone="danger" status="待建 · 五页之外新页面" />
        </Layer>

        <Connector label="HTTP / WebSocket · 当前无鉴权 → 需接 scoped key" />

        <Layer name="Agent 运行时" sub="一人一 bot · 网关是唯一数据通道">
          <LayerCard title="Persona 渲染管线" desc="角色×成员叠加 → 系统 prompt" tone="positive" status="已咬合" />
          <LayerCard title="网关工具层" desc="Read/Grep 经网关 · 段落剥离 · 读审计落链" tone="danger" status="待建 · 全系统核心缺口" />
          <LayerCard title="身份认证" desc="scoped key 三层 scope · 吊销 60s" tone="danger" status="待接入 · 资产在 key_scopes.py" />
          <LayerCard title="披露升级流" desc="自动过 / 主管审 / 双人审 · 到期回收" tone="danger" status="待建 · 雏形在 hub_mixins/disclosure_ops.py" />
          <LayerCard title="五层 harness" desc="文件工具 · Shell 三档 · 产物 · 通知" tone="warning" status="原版在跑 · 未接新壳" />
        </Layer>

        <Connector label="网关唯一读写 · 裸 git 仓库不分发" />

        <Layer name="数据底座" sub="主干-分干 · 已拍板 · 待开发">
          <LayerCard title="企业主干" desc="BOUNDARY 规则 · identity · customers/ 档案 · index/ · 审计链" tone="warning" status="已拍板 · 待开发" />
          <LayerCard title="项目分干 ×N" desc="inbox → 清洗 → 打标 → vault · 工作副本" tone="warning" status="已拍板 · 待开发" />
          <LayerCard title="流 ① 录入" desc="五段管线 · 打标不脱敏" tone="warning" status="半在 · ingest 缺 ②③ 段" />
          <LayerCard title="流 ③ 输出" desc="身份制分发 · min 叠加 · 段落剥离" tone="danger" status="待建 · 依赖网关" />
          <LayerCard title="流 ② 反哺" desc="去重 → 合并 → 打标继承 · 人工队列" tone="danger" status="待建 · 最后上线" />
        </Layer>
      </div>

      <div className="mt-3 rounded-xl border border-dashed border-zinc-300 p-3">
        <div className="text-[13px] font-medium text-zinc-500">
          原版资产库（冻结模块 + 可下沉资产，Python → 需移植 Node 或保留为边车服务）
        </div>
        <div className="mt-2 flex flex-wrap gap-2">
          {[
            ['sensitivity.py', '6 维链 + PII 预扫'],
            ['key_scopes.py', 'scoped key'],
            ['disclosure.py', 'min 叠加 + advance'],
            ['audit_chain.py', '哈希链'],
            ['entity_extraction.py', '抽取候选 + 审查队列'],
          ].map(([code, label]) => (
            <span key={code} className="rounded-md border border-zinc-200 bg-white px-3 py-1 text-xs text-zinc-500">
              <code className="font-mono text-zinc-900">{code}</code>　{label}
            </span>
          ))}
          <span className="rounded-md border border-zinc-200 bg-white px-3 py-1 text-xs text-zinc-500">
            冻结：看板/任务 · 联邦节点 · 插件 · 多主题
          </span>
          <span className="rounded-md border border-zinc-200 bg-white px-3 py-1 text-xs text-zinc-500">
            加法版 SaaS 根系 = 主干特殊目录域（未对齐）
          </span>
        </div>
      </div>

      <div className="mt-3 rounded-xl border border-rose-200 bg-rose-50 p-3 text-[13px] leading-5 text-rose-700">
        关键路径：网关工具层是全系统的咽喉——录入打标、输出剥离、读审计、反哺归并全部经过它；它不建成，数据底座的拍板设计无法生效。上线顺序：身份认证 → 录入打标 → 输出分发 → 反哺归并。
      </div>
    </section>
  )
}
