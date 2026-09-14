# 向量数据库候选记档（阶段 3 数据层候选引擎）

> 2026-08-30 调研记录｜来源 GitHub API 实证

## alibaba/zvec（候选，未定）

- 仓库：https://github.com/alibaba/zvec ｜ 官网 zvec.org ｜ Apache-2.0 ｜ C++ ｜ ★15.5K
- 定位：嵌入式（in-process）向量数据库，纯本地零配置，阿里内部 battle-tested
- 创建 2025-12-05，v0.7.0（2026-08-24），活跃迭代中（0.x，API 可能变动）
- SDK：Python 3.10–3.14（本机 3.14 正好支持）、Node.js、Go、Rust、Dart
- 平台：Linux / macOS / Windows x86_64（预编译 SDK）

### 对星枢路线 A 的价值点
1. **Hybrid Search**：向量 + FTS 全文 + 结构化过滤单查询融合——可替代 K1「sentence 向量 / hasher ILIKE」双轨，data_domain/level_cap 过滤可下沉检索层
2. Windows 原生 + Python 3.14 官方支持，零适配
3. N-gram tokenizer FTS（短语/代码/短文本），中文场景优于裸 ILIKE
4. WAL 持久化崩溃安全；多进程读单进程写

### 引入前提（阶段 3 时评估）
- [ ] 跑通 K1 中文同义词验收集（50 组 + 10 组跨语言，双断言：绝对 cos + 相对区分度）
- [ ] 存量 chroma_db 数据迁移路径（8.4M 目录 + sync_hub 集合）
- [ ] 与 24h 防拼接滑窗的集成方式
- [ ] 0.x API 稳定性观察（阶段 3 前若发 1.0 优先考虑）

### 结论
阶段 3（主干-分干数据层）候选引擎，阶段 1-2 不引入，避免中途换引擎打乱节奏。
