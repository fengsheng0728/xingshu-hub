# 星枢机密词库 — 维护责任人约定（K3，附录 F 2026-08-06）

## 文件结构

```
config/secret-words/
├── base.txt                    通用 30 词（所有部署必载）
├── industry-manufacturing.txt  制造业起始包
├── industry-finance.txt        金融行业起始包
├── industry-ecommerce.txt      电商/零售起始包
├── industry-healthcare.txt     医疗行业起始包
├── industry-education.txt      教育/机构起始包
└── README.md                   本约定
```

## 启用方式

config.yaml 指定词库目录：

```yaml
sensitivity:
  secret_words_dir: ./config/secret-words   # 缺省=内置默认 22 词
  secret_words_industries: [base, finance]  # 启用包列表（base 必含）
```

启用后 `SYNC_HUB_SECRET_WORDS` env 可单文件覆盖（测试/临时用）。

## 维护责任人约定

1. **词库变更 = 安全变更**：增删词必须走审批（manager/orchestrator），
   变更后调用 `POST /api/v1/chunks/reclassify`（E.7 重判定存量数据）。
2. **增词原则**：只加"公司特有/业务敏感"词；通用词进 base；
   行业词进对应 industry-*.txt。禁止放法律明文禁止传播的内容。
3. **删词原则**：先确认没有存量 chunk 依赖该词降级（查审计 `memory_locked_pii`/`chunks_ingested`），
   再删并重判定。
4. **词库导出/查看审批门**：`GET /api/v1/sensitivity/words` 需要 manager/orchestrator
   角色（K3 修正：词库本身就是敏感信息，不能任意查看）。
5. **季度复核**：每季度由安全负责人核对词库与最新业务线（新项目代号/新产品名补入）。

## 文件格式

- 每行一词，`#` 开头为注释（可作分组标题）
- UTF-8 编码，LF 行尾
- 词之间用换行分隔，不trim内部空格（词本身含空格合法）
