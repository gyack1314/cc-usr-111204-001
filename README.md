# 制氢设备地址空间的命名空间稳定采集

此Python项目管理独立的能源领域合成源资料，`data/`为原始样本及访问配置，`source_data.io`负责严格读取和字节身份，`source_data.domain`提供现有库存摘要，`source_data.replay`按声明顺序返回不解释的源响应。字段和支持的来源口径见[资料约定](docs/data-contract.md)。所有机构、设备、对象与数值均为虚构。

地址空间采集由两个新模块承担：`source_data.nodeid`把线上NodeId（ns索引身份）解析为“服务器URI+命名空间URI+带类型identifier”的稳定身份，数字/字符串/GUID/字节串不因文本相同而合并，每个会话重新解析命名空间索引；`source_data.address_space`通过Browse/BrowseNext/Read/Close适配器展开引用图，节点只展开一次但保留全部引用边（多父、环、同名），续点按源节点和会话归属，会话失效后按稳定身份在新会话重做并幂等归并，外部ExpandedNodeId只留引用不连接。

Python 3.11及以上即可使用，无第三方Python依赖。项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q source_data tests
python -m source_data.cli inspect data/normal.json
python -I source_data/cli.py collect data/hydrogen-upgrade.json -o out/hydrogen-upgrade.material.json
```

`inspect`展示已有的原始库存，不修改资料。`collect`输出可跨会话比对的节点、引用、分页、续点、Read与缺口资料（确定性JSON，`out/`已被gitignore）；读取不可信数据时用`-I`隔离运行并以参数传入路径。`invalid-`前缀文件为不合法输入，其他变体保留不同业务边界；源协议与二进制格式使用资料约定中的有限范围，未解释部分仍以原字节保存。

固定基础镜像见Dockerfile，可执行`docker build -t energy-source .`，再用`docker run --rm energy-source`显示摘要；`docker run --rm energy-source python -m unittest discover -s tests -v`执行已有检查。资料和回放不依赖真实设备、账户或实时网络服务。
