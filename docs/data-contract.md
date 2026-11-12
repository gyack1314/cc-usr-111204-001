# OPC UA 适配器与地址空间资料约定

资料为服务层已解码响应，`Replay.call(operation, arguments)` 返回不解释的下一
条响应，真实接入可替换为相同服务适配器。服务操作映射为 Browse、BrowseNext、
Read、Close；BrowseNext 参数中 `release=true` 用于释放，续点只在产生它的会话
中合法。示例步骤用于展示响应形状，扩展场景可提供不同 steps，不以一次固定操作
序列限制遍历策略。

## 线上身份与稳定身份

节点文本（如 `ns=2;s=plant`）是线上的索引身份，仅在单个会话内有效。
`source_data.nodeid` 负责：

- 解析 `ns=`/`svr=`/`su=` 前缀与 `i`(numeric)/`s`(string)/`g`(guid)/`b`(bytes)
  四类 identifier；数字保留整数、bytes 用 hex、guid 按 UUID 规范值、string 按
  原文，不做 Unicode 合并。**四类 identifier 即使文本相同也是不同身份**。
- 稳定身份 = 服务器 URI + NamespaceArray 中的命名空间 URI + 带类型 identifier，
  规范文本形如 `stable:server=urn:hydrogen:lab;ns=urn:vendor:A;s=plant`。
- 每个会话用该会话自己的 NamespaceArray 重新解析索引（`SessionMap`），绝不携带
  旧会话的 ns 索引；同一稳定身份在新会话按新索引还原为线上文本。
- ExpandedNodeId：`svr>0` 或 `su` 不在本会话命名空间内时判为外部，只登记外部
  引用，不发起连接；未知服务端的 `server_uri` 留空。`server_table` 按 ServerIndex
  解释已知外部服务。

## 服务适配器接口

`source_data.address_space` 面向以下最小接口（传输与登录不在范围）：

- `browse(node_text) -> {status, references, continuation}`
- `browse_next(continuation_point, release) -> 同 Browse 响应`
- `read(node_texts, attribute) -> {results: [...]}`，results 与请求顺序一一对应
- `close()`

离线资料用 `ScriptedAdapter` 解释：含 `error` 的步骤代表传输/服务层失败，抛错但
不挪动后续响应；步骤按声明顺序就近归属到 `arguments.session` 所指会话。
`make_session_adapters(config)` 返回会话到适配器的工厂。`StrictReplayAdapter`
适用于无 error 步、需要严格按序回放的场景。

## 遍历与续点口径

- 只沿 `allowed_reference_types` 中的**正向**引用展开；节点只展开一次，但所有
  有效引用边都保留（含非允许类型、反向、多父、环、同名），重复引用幂等归并并
  累计 `occurrences`。
- Browse/BrowseNext 的每一页按源节点和当前会话登记原始 StatusCode、引用条数与
  响应续点；空页带非空 continuation 仍继续换页。节点所有分页完成才置
  `browse_complete=true`，非 Good 状态登记 `browse_status_not_good` 缺口且保持
  未完整，绝不以“读到零条”冒报完整。
- 续点状态机：`active -> consumed/released/abandoned/release_failed`。异常、
  取消、资源预算终止时对尚存续点发 `release=true`；会话失效（BadSessionIdInvalid
  等）时旧续点不可复用，标记 `abandoned`，未完成节点在新会话按稳定身份重做。
- 资源预算 `max_nodes`/`max_pages` 与外部 `cancel` 触发可解释终止，缺口逐节点
  登记，未完成节点不会被标记完整。

## Read 口径

- Read 的 results 严格按请求索引还原，逐属性保存 StatusCode 主类别
  （Good/Uncertain/Bad）、SourceTimestamp、ServerTimestamp 与原值；Bad 不转成
  null 正常值（null 只是线上原值，以 `value_present`/`raw_value` 区分），
  Uncertain 单列。
- 只有 Variable 且请求了 Value 属性时才读 Value；Object 节点仍保留在节点资料中。
- Read 分批执行，批次之间不是原子快照。`consistency` 明确区分“遍历完整”
  （`traversal_complete`）与“同刻一致”（`point_in_time_snapshot=false`）；服务器
  未提供整库版本保证时，资料不声称瞬时一致。

## 资料产物

`python -I source_data/cli.py collect <file> -o out/<name>.material.json` 产出
确定性 JSON：sessions（含每会话 namespace 索引表）、nodes（稳定身份）、edges、
external_references、browse_pages、continuation_points、read_batches、gaps 与
summary。重复运行字节级一致，可跨会话/跨厂商升级比对。现有 `domain.summarize`
仅清点会话和响应，不规范化 NodeId、不展开引用图，保持不变。
