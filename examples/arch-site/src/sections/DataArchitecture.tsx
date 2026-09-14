import { SectionTitle, Step } from './Shared'

const C = {
  primary: '#18181b',
  secondary: '#52525b',
  tertiary: '#a1a1aa',
  border: '#d4d4d8',
  surface: '#ffffff',
}

function TrunkBranchSvg() {
  return (
    <svg viewBox="0 0 780 330" className="mt-4 block w-full" role="img" aria-label="主干分干结构图">
      <defs>
        <marker id="arw" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill={C.tertiary} />
        </marker>
      </defs>

      <rect x="10" y="110" width="150" height="110" rx="12" fill="none" stroke={C.border} />
      <text x="85" y="136" textAnchor="middle" fontSize="14" fontWeight="500" fill={C.primary}>请求方</text>
      <text x="85" y="158" textAnchor="middle" fontSize="12" fill={C.secondary}>scoped key 身份</text>
      <text x="85" y="178" textAnchor="middle" fontSize="12" fill={C.secondary}>endpoints · data_domain</text>
      <text x="85" y="196" textAnchor="middle" fontSize="12" fill={C.secondary}>level_cap</text>

      <rect x="240" y="20" width="280" height="290" rx="12" fill="none" stroke={C.primary} strokeWidth="1.5" />
      <text x="380" y="48" textAnchor="middle" fontSize="15" fontWeight="500" fill={C.primary}>企业主干（独立 git 仓库）</text>
      <text x="380" y="70" textAnchor="middle" fontSize="12" fill={C.tertiary}>准入全系统最严 · 不分发裸仓库</text>
      <g fontSize="12" fill={C.secondary}>
        <rect x="258" y="86" width="244" height="30" rx="6" fill="none" stroke={C.border} />
        <text x="270" y="105">BOUNDARY.md　边界规则单一事实源</text>
        <rect x="258" y="124" width="244" height="30" rx="6" fill="none" stroke={C.border} />
        <text x="270" y="143">identity/　身份注册 + key 签发记录</text>
        <rect x="258" y="162" width="244" height="30" rx="6" fill="none" stroke={C.border} />
        <text x="270" y="181">customers/　共享实体 canonical 档案</text>
        <rect x="258" y="200" width="244" height="30" rx="6" fill="none" stroke={C.border} />
        <text x="270" y="219">index/　元数据级索引（全员可见）</text>
        <rect x="258" y="238" width="244" height="30" rx="6" fill="none" stroke={C.border} />
        <text x="270" y="257">audit/　读写审计哈希链</text>
      </g>
      <text x="380" y="292" textAnchor="middle" fontSize="12" fill={C.tertiary}>projects/　分干指针登记</text>

      <rect x="600" y="20" width="170" height="86" rx="12" fill="none" stroke={C.border} />
      <text x="685" y="44" textAnchor="middle" fontSize="13" fontWeight="500" fill={C.primary}>项目分干 A</text>
      <text x="685" y="64" textAnchor="middle" fontSize="11" fill={C.secondary}>inbox/ · vault/（打标 md）</text>
      <text x="685" y="82" textAnchor="middle" fontSize="11" fill={C.secondary}>_originals/ · audit/</text>

      <rect x="600" y="122" width="170" height="86" rx="12" fill="none" stroke={C.border} />
      <text x="685" y="146" textAnchor="middle" fontSize="13" fontWeight="500" fill={C.primary}>项目分干 B</text>
      <text x="685" y="166" textAnchor="middle" fontSize="11" fill={C.secondary}>同构 · 独立 git 仓库</text>
      <text x="685" y="184" textAnchor="middle" fontSize="11" fill={C.secondary}>分干之间永不互读</text>

      <rect x="600" y="224" width="170" height="86" rx="12" fill="none" stroke={C.border} strokeDasharray="4 3" />
      <text x="685" y="252" textAnchor="middle" fontSize="13" fill={C.tertiary}>项目分干 …</text>
      <text x="685" y="272" textAnchor="middle" fontSize="11" fill={C.tertiary}>每项目一个，工作副本</text>

      <line x1="160" y1="140" x2="236" y2="120" stroke={C.tertiary} markerEnd="url(#arw)" />
      <text x="198" y="112" textAnchor="middle" fontSize="11" fill={C.secondary}>① 认证+定位</text>
      <line x1="236" y1="210" x2="160" y2="190" stroke={C.tertiary} markerEnd="url(#arw)" />
      <text x="198" y="226" textAnchor="middle" fontSize="11" fill={C.secondary}>④ 按身份分发</text>

      <line x1="600" y1="80" x2="524" y2="100" stroke={C.primary} strokeWidth="1.5" markerEnd="url(#arw)" />
      <text x="562" y="72" textAnchor="middle" fontSize="11" fontWeight="500" fill={C.primary}>反哺</text>
      <line x1="600" y1="170" x2="524" y2="165" stroke={C.primary} strokeWidth="1.5" markerEnd="url(#arw)" />
      <line x1="524" y1="240" x2="600" y2="252" stroke={C.tertiary} strokeDasharray="4 3" markerEnd="url(#arw)" />
      <text x="562" y="230" textAnchor="middle" fontSize="11" fill={C.tertiary}>指针/规则下行</text>
    </svg>
  )
}

function RedLine({ children }: { children: string }) {
  return (
    <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm leading-[22px] text-rose-700">
      {children}
    </div>
  )
}

export default function DataArchitecture() {
  return (
    <section className="mt-16">
      <SectionTitle
        id="data"
        title="数据底座架构 · 主干-分干与三条数据流"
        sub="主从声明：共享实体真相只在主干；录入打标不脱敏；反哺归并三件套；身份制分发"
      />

      <div className="mt-5 rounded-xl border border-zinc-200 bg-white p-4">
        <div className="text-[15px] font-medium text-zinc-900">仓库结构</div>
        <TrunkBranchSvg />
      </div>

      <div className="mt-8">
        <div className="text-[15px] font-medium text-zinc-900">数据流 ① · 录入（分干内，打标不脱敏）</div>
        <div className="mt-3 flex flex-wrap gap-2">
          <Step num={1} title="上传" desc="落 inbox/pending + .meta（谁/何时/来源信任级）" />
          <Step num={2} title="清洗" desc="格式归一；PII 全文预扫在切割前跑（E.1）" />
          <Step num={3} title="打标" desc="6 维链 fail-closed；文件级 + 段落级，内容不改写" />
          <Step num={4} title="落库" desc="vault/ 落盘 + git commit；原件进 _originals/ 受控区" />
          <Step num={5} title="索引回写" desc="主干只登记元数据级条目，内容不上行" />
        </div>
        <p className="mt-2 text-xs text-zinc-400">
          失败 → inbox/failed/ + 错误留痕，人工介入；结构化 PII 走 5 类正则，机密词库作第二道网。
        </p>
      </div>

      <div className="mt-8">
        <div className="text-[15px] font-medium text-zinc-900">数据流 ② · 反哺（分干 → 主干，归并三件套）</div>
        <div className="mt-3 flex flex-wrap gap-2">
          <Step num={1} title="触发" desc="项目完成 + 日常更新同步反哺，同录入管线（trust=internal）" />
          <Step num={2} title="去重" desc="精确键 → 规则模糊 → LLM 判似；≥0.9 自动合，0.6–0.9 进人工" />
          <Step num={3} title="合并" desc="小节级写入 canonical 档案，来源分干 + commit 指针可回溯" />
          <Step num={4} title="打标继承" desc="只降不升：任一来源头 NONE，主干档案对应段永远 NONE" />
          <Step num={5} title="人工队列" desc="低置信合并、同字段 3 次覆盖冲突 → 人工裁决后才落库" warn />
        </div>
      </div>

      <div className="mt-8">
        <div className="text-[15px] font-medium text-zinc-900">数据流 ③ · 输出（身份制分发 + 渐进升级）</div>
        <div className="mt-3 flex flex-wrap gap-2">
          <Step num={1} title="认证" desc="scoped key → Principal（endpoints / data_domain / level_cap）" />
          <Step num={2} title="定位" desc="查主干 index/（元数据级）；跨项目查询只走主干" />
          <Step num={3} title="定级" desc="实际级别 = min(level_cap, 文件级, 段落级)" />
          <Step num={4} title="分发" desc="网关剥离越权段落后下发；先低级别，默认 SUMMARY" />
          <Step num={5} title="升级" desc="同域+在办任务自动过 → 主管人审 → NONE 双人审；到期回收" />
        </div>
        <p className="mt-2 text-xs text-zinc-400">
          全程读审计入链：谁、哪把 key、看了什么、给到哪级、何时。披露级别沿用原版四级 NONE / METADATA / SUMMARY / FULL。
        </p>
      </div>

      <div className="mt-8">
        <div className="text-[15px] font-medium text-zinc-900">两条 LLM 红线（放开零 token 后仍保留）</div>
        <div className="mt-3 space-y-2">
          <RedLine>红线 1 · 打标敏感段永不送 LLM——归并管线先剥离敏感段再外呼；必须处理敏感内容时用本地模型或纯规则。</RedLine>
          <RedLine>红线 2 · LLM 只产候选不直接落库——合并建议经置信度分流与人工队列，防幻觉污染真相源。</RedLine>
        </div>
      </div>

      <div className="mt-8">
        <div className="text-[15px] font-medium text-zinc-900">原版资产映射（下沉复用，非重造）</div>
        <div className="mt-3 flex flex-wrap gap-2">
          {[
            ['sensitivity.py', '6 维判定链 · 切割前 PII 预扫'],
            ['chunk_level()', '段落级打标 · 父级封顶只降不升'],
            ['key_scopes.py', '三层 scope 身份 · 吊销 60s 生效'],
            ['disclosure.py', 'min 叠加判定 · advance 升级流'],
            ['audit_chain.py', '哈希链 · 读写双审计'],
            ['taint', 'UNTRUSTED 来源强制 NONE'],
          ].map(([code, label]) => (
            <span key={code} className="rounded-md border border-zinc-200 bg-white px-3 py-1 text-xs text-zinc-500">
              <code className="font-mono text-zinc-900">{code}</code>　{label}
            </span>
          ))}
        </div>
      </div>
    </section>
  )
}
