"""下载源抽象层:把原本硬编码在 resource_client.py / services/rss_poller.py 里、
按 source 字符串散落分派的三个下载源(dmhy / animegarden / nyaa),收敛成统一的
SourceAdapter 注册表。

- base.py     统一结果模型 ResourceItem、SourceAdapter 接口、共享纯函数、配置读取、唯一权威过滤谓词 matches_criteria
- dmhy.py / animegarden.py / nyaa.py  三个源各自的 adapter(搜索抓取解析 + RSS Feed 构造/解析)
- registry.py 注册表:get_source / list_sources / enabled_sources

新增一个下载源 = 新增一个 adapter 模块并在 registry 里注册,不再需要改多处字符串字面量。
源的 base URL / 启用开关可在设置页配置(见 config_store.DEFAULTS['download_sources']),
应对站点换域名/换镜像。
"""
