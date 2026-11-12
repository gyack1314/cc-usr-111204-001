"""线上 NodeId 解析与跨会话稳定身份。

线上文本（ns=2;s=...）只在单个会话内有效；稳定身份由服务器标识、
NamespaceArray 中的命名空间 URI 与带类型的 identifier 组成。数字、字符串、
GUID、字节串即使文本相同也是不同身份，不做任何合并。
"""
import uuid

IDENTIFIER_TYPES = ('numeric', 'string', 'guid', 'bytes')
_PREFIXES = {'i': 'numeric', 's': 'string', 'g': 'guid', 'b': 'bytes'}
_TYPE_PREFIX = {v: k for k, v in _PREFIXES.items()}


class NodeIdError(ValueError):
    """NodeId 文本或命名空间解析失败。"""


class WireNodeId:
    """一次会话内的线上节点身份，含 ExpandedNodeId 的服务端信息。"""

    __slots__ = ('namespace_index', 'identifier_type', 'identifier',
                 'server_index', 'namespace_uri', 'raw')

    def __init__(self, namespace_index, identifier_type, identifier,
                 server_index=None, namespace_uri=None, raw=''):
        self.namespace_index = namespace_index
        self.identifier_type = identifier_type
        self.identifier = identifier
        self.server_index = server_index
        self.namespace_uri = namespace_uri
        self.raw = raw

    @property
    def is_expanded(self):
        return self.server_index is not None or self.namespace_uri is not None

    def __repr__(self):
        return f'WireNodeId({self.raw!r})'


def _coerce_identifier(identifier_type, value):
    if identifier_type == 'numeric':
        if type(value) is int:
            number = value
        elif isinstance(value, str):
            if not value.isdigit():
                raise NodeIdError('数字 identifier 必须为非负整数文本')
            number = int(value)
        else:
            raise NodeIdError('数字 identifier 必须为整数')
        if number < 0:
            raise NodeIdError('数字 identifier 不可为负')
        return number
    if identifier_type == 'string':
        if not isinstance(value, str):
            raise NodeIdError('字符串 identifier 必须为文本')
        return value
    if identifier_type == 'guid':
        try:
            return uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            raise NodeIdError('GUID identifier 不符合 UUID 规范')
    if identifier_type == 'bytes':
        if not isinstance(value, str) or len(value) % 2:
            raise NodeIdError('字节 identifier 必须为偶数长度 hex 文本')
        try:
            return bytes.fromhex(value)
        except ValueError:
            raise NodeIdError('字节 identifier 不是合法 hex')
    raise NodeIdError('identifier 类型未知')


def _split_header(text):
    """切出头部标记与 identifier 段；字符串 identifier 允许含分号。"""
    tokens = {}
    rest = text
    while rest:
        if rest.startswith(('ns=', 'svr=', 'su=')):
            key, _, rest = rest.partition('=')
            value, sep, rest = rest.partition(';')
            if not sep:
                raise NodeIdError(f'NodeId 缺少 identifier 段: {text!r}')
            tokens[key] = value
            continue
        break
    if not rest:
        raise NodeIdError(f'NodeId 缺少 identifier 段: {text!r}')
    marker, _, id_value = rest.partition('=')
    if marker not in _PREFIXES or not _:
        raise NodeIdError(f'NodeId identifier 标记未知: {text!r}')
    return tokens, _PREFIXES[marker], id_value


def parse_wire(text):
    """解析 ns=/svr=/su= 前缀与 i/s/g/b identifier，失败抛 NodeIdError。"""
    if not isinstance(text, str) or not text:
        raise NodeIdError('NodeId 文本为空')
    header, id_type, id_value = _split_header(text)
    if 'ns' not in header:
        raise NodeIdError(f'NodeId 缺少命名空间索引: {text!r}')
    try:
        ns_index = int(header['ns'])
    except ValueError:
        raise NodeIdError(f'命名空间索引不是整数: {text!r}')
    if ns_index < 0:
        raise NodeIdError(f'命名空间索引为负: {text!r}')
    server_index = None
    if 'svr' in header:
        try:
            server_index = int(header['svr'])
        except ValueError:
            raise NodeIdError(f'ServerIndex 不是整数: {text!r}')
        if server_index < 0:
            raise NodeIdError(f'ServerIndex 为负: {text!r}')
    namespace_uri = header.get('su')
    if namespace_uri is not None and not namespace_uri:
        raise NodeIdError(f'ExpandedNodeId 的命名空间 URI 为空: {text!r}')
    identifier = _coerce_identifier(id_type, id_value)
    return WireNodeId(ns_index, id_type, identifier, server_index,
                      namespace_uri, text)


def canonical_identifier(identifier_type, identifier):
    """输出可比较的规范文本：GUID 小写、字节串小写 hex，其余按原值。"""
    if identifier_type == 'numeric':
        return str(identifier)
    if identifier_type == 'string':
        return identifier
    if identifier_type == 'guid':
        return str(identifier).lower()
    if identifier_type == 'bytes':
        return identifier.hex()
    raise NodeIdError('identifier 类型未知')


class StableId:
    """服务器 URI + 命名空间 URI + 带类型 identifier 的稳定身份。"""

    __slots__ = ('server_uri', 'namespace_uri', 'identifier_type', 'identifier')

    def __init__(self, server_uri, namespace_uri, identifier_type, identifier):
        if identifier_type not in IDENTIFIER_TYPES:
            raise NodeIdError('identifier 类型未知')
        self.server_uri = server_uri
        self.namespace_uri = namespace_uri
        self.identifier_type = identifier_type
        self.identifier = identifier

    def key(self):
        if self.identifier_type == 'guid':
            ident = self.identifier
        else:
            ident = self.identifier
        return (self.server_uri, self.namespace_uri,
                self.identifier_type, ident)

    def __eq__(self, other):
        return isinstance(other, StableId) and self.key() == other.key()

    def __hash__(self):
        return hash(self.key())

    @property
    def canonical(self):
        prefix = _TYPE_PREFIX[self.identifier_type]
        value = canonical_identifier(self.identifier_type, self.identifier)
        return (f'stable:server={self.server_uri};ns={self.namespace_uri};'
                f'{prefix}={value}')

    def __repr__(self):
        return f'StableId({self.canonical!r})'


class SessionMap:
    """单个会话的 NamespaceArray 视图；每个会话必须重新构建。"""

    def __init__(self, session_id, server_uri, namespaces, server_table=()):
        self.session_id = session_id
        self.server_uri = server_uri
        if not namespaces or any(not isinstance(x, str) or not x
                                 for x in namespaces):
            raise NodeIdError('命名空间列表无效')
        if len(set(namespaces)) != len(namespaces):
            raise NodeIdError('命名空间 URI 重复')
        self.namespaces = tuple(namespaces)
        self._uri_index = {uri: i for i, uri in enumerate(self.namespaces)}
        self.server_table = {int(row['server_index']): row['server_uri']
                             for row in (server_table or ())}

    def uri_of(self, namespace_index):
        if namespace_index >= len(self.namespaces):
            raise NodeIdError(
                f'会话 {self.session_id} 中 ns={namespace_index} 超出 '
                f'NamespaceArray 范围 0..{len(self.namespaces) - 1}')
        return self.namespaces[namespace_index]

    def classify(self, wire):
        """返回 ('local', StableId) 或 ('external', 外部描述)。"""
        if wire.is_expanded and (wire.server_index or 0) > 0:
            uri = wire.namespace_uri
            return 'external', {
                'server_index': wire.server_index,
                'server_uri': self.server_table.get(wire.server_index),
                'namespace_uri': uri,
                'identifier_type': wire.identifier_type,
                'identifier': canonical_identifier(wire.identifier_type,
                                                    wire.identifier),
                'raw': wire.raw,
            }
        if wire.is_expanded and wire.namespace_uri is not None:
            if wire.namespace_uri not in self._uri_index:
                return 'external', {
                    'server_index': wire.server_index or 0,
                    'server_uri': self.server_uri,
                    'namespace_uri': wire.namespace_uri,
                    'identifier_type': wire.identifier_type,
                    'identifier': canonical_identifier(wire.identifier_type,
                                                        wire.identifier),
                    'raw': wire.raw,
                }
            namespace_uri = wire.namespace_uri
        else:
            namespace_uri = self.uri_of(wire.namespace_index)
        return 'local', StableId(self.server_uri, namespace_uri,
                                 wire.identifier_type, wire.identifier)

    def resolve(self, wire):
        kind, payload = self.classify(wire)
        if kind != 'local':
            raise NodeIdError(f'外部 ExpandedNodeId 不能解析为本地身份: {wire.raw!r}')
        return payload

    def wire_text(self, stable):
        """按当前会话的索引把稳定身份还原为线上文本，绝不复用旧 ns。"""
        if stable.server_uri != self.server_uri:
            raise NodeIdError('稳定身份属于其他服务器')
        if stable.namespace_uri not in self._uri_index:
            raise NodeIdError(
                f'命名空间 {stable.namespace_uri} 在会话 {self.session_id} 不可用')
        ns_index = self._uri_index[stable.namespace_uri]
        prefix = _TYPE_PREFIX[stable.identifier_type]
        value = canonical_identifier(stable.identifier_type, stable.identifier)
        return f'ns={ns_index};{prefix}={value}'


def root_wire(root):
    """把配置中的根节点描述转换为线上身份（identifier 仍按类型严格解析）。"""
    ns_index = root['namespace_index']
    if type(ns_index) is not int or ns_index < 0:
        raise NodeIdError('根节点命名空间索引无效')
    id_type = root['identifier_type']
    identifier = _coerce_identifier(id_type, root['identifier'])
    text_type = _TYPE_PREFIX[id_type]
    raw = f'ns={ns_index};{text_type}={canonical_identifier(id_type, identifier)}'
    return WireNodeId(ns_index, id_type, identifier, raw=raw)
