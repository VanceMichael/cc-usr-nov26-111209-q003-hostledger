# hostledger

物流主机交付文件的 RDW 帧检查、接收台账及 EBCDIC 预览工具。

## 现有能力

inspect 查看一份源文件的结构；capture 发起一次本机 HTTP 请求并保存响应状态、原始字节和摘要；receive 把已归档原文解析成逐票装载明细并写入台账；show / review / explain / items / export 分别列出接收事实、复核隔离原因、解释补发关系、按票查询和按业务批次导出。原文对象按 SHA-256 共用，独立请求分别记账。错误 HTTP 状态也会保存，原文归档不等于业务接收完成：只有状态 200 的原文可以进入接收作业。

### 接收条件

一次接收只有同时满足以下条件才会成为可查询的正式明细：

- 文件头 H 在首记录、文件尾 T 在末记录，头尾之间不再出现 H/T；
- 文件头版本存在于 `hostledger/fixtures/layouts.json`，且记录类型在该版本内有定义；
- 尾记录明细数与实得 N+C 条数一致，尾记录总额与逐票带符号金额精确合计一致；
- 每条明细的必需字段都在载荷范围内，COMP-3 每个半字节合法，重复组不越界。

任一条不满足时整次接收记为 `isolated`：运营可以用 `review` 看到每条记录、原始字节范围和具体原因，但 `items` 和 `export` 不会返回这批数据的任何一票。隔离的接收事实仍保留原文关系与批次号，便于对照。

### 补发与中断

- 文件名不是业务身份。同名文件再次接收会形成新的接收事实，不会被略过；相同内容重放也各自记账。
- 同一业务批次号下，新一次已接收的完整批次会记录 `supersedes` 指向上一版；旧原文和旧接收事实都不可修改。`explain` 列出补发前后各自包含的票号以及被隔离的记录序。
- 接收在单个 SQLite 事务里写入接收事实和全部逐票记录。写入中断时数据库回滚，最多留下一份未被任何接收事实引用的原文对象；不会出现半个批次。

## 运行

使用 Python 3.11 或更新版本，仅依赖标准库，无安装步骤。全部示例均为本地合成资料，不包含真实个人信息、商业凭据或线上地址。

```sh
python3 -m hostledger inspect hostledger/fixtures/sample.hex --full
python3 -m hostledger receive hostledger/fixtures/sample.hex --db ledger.sqlite3 --objects objects
python3 -m hostledger show --db ledger.sqlite3 --objects objects
python3 -m hostledger items --item ITEM001 --db ledger.sqlite3 --objects objects
python3 -m hostledger export B0001 --db ledger.sqlite3 --objects objects --output batch.json
python3 -m hostledger demo
python3 -m unittest discover -s tests -v
python3 -m compileall -q hostledger
```

台账默认库名为 `ledger.sqlite3`；`capture`、`show` 为兼容旧流程仍默认 `captures.sqlite3`，可用 `--db` 与 `--objects` 指定独立位置。需要手动发请求时运行 `python3 -m hostledger.source_server --port 8765`，再在另一终端运行 `python3 -m hostledger capture http://127.0.0.1:8765/file`。演示自己启动源服务，使用随机本机端口，接收 `/file` 与 `/redelivery` 两个快照并展示补发关系，退出时清理临时目录。

## 输出结构

`inspect --full` 和 `items` 中的每条记录都带 `record_offset`（RDW 起点，从文件第 0 字节算）、`record_length`、`payload_offset`、`payload_end`、`layout_version` 和 `fields`。`fields` 内每个字段给出绝对字节范围 `start`/`end`，重复组额外给出每组范围与原始十六进制。金额保留精确十进制字符串和原始 `amount_units`/`amount_sign`/`amount_scale`，不以浮点数承载。`count_provided`、`temperature_provided` 区分“未提供”（`false` 且值为 `null`）与“提供了零”（`true` 且值为 `0`）；填充空格记在 `group_padding` 或 `fields.*.hex` 中，不会和零值合并。

## 模块

`hostledger/parser.py` 负责单文档观察和整批接收判定，`archive.py` 保存不可变原文与请求事实，`ledger.py` 在其上记录接收事实与逐票明细，`transport.py` 限制实验请求到本机，`source_server.py` 提供来源示例。来源边界及字段含义见 `docs/source-contract.md`。

## 容器

Dockerfile 使用 Python 3.11 标准库镜像，不需要应用依赖下载。可用 `docker build -t hostledger:local .` 构建，再执行 `docker run --rm --network none hostledger:local`。本地 HTTP 与 PTY 演练不需要外部网络，测试及单次演示通常数秒完成。
