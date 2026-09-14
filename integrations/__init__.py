"""星枢 SyncHub — 集成层 Integration Hub（§七 门框）

本期只建"门框"，不建"房间"：
- Connector 适配器契约（base.py）+ 目录扫描注册表（registry.py）
- 新增连接器 = 在 connectors/ 目录丢一个实现 Connector 接口的文件，暴露 get_connector()
- 集成数据一律走附录 E 汇入管道（hub.ingest_chunks），trust_level="external"（taint）
- 具体 ERP/CRM 适配器有客户合同时再写，禁在本期落地
"""
