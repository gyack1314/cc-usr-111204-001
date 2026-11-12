"""OPC UA 地址空间采集：按稳定身份构建可跨会话比对的节点与引用资料。

不按显示名称建树：节点只展开一次，但所有有效引用边全部保留，允许多父、
环与同名节点。续点按源节点和当前会话归属；会话失效后旧续点不复用，
未完成展开在新会话按稳定身份重做，重复引用幂等归并。
"""
import copy
from collections import deque

from .nodeid import (NodeIdError, SessionMap,
                     canonical_identifier, parse_wire, root_wire)

SESSION_INVALID_HINTS = ('badsessionid', 'sessionidinvalid', 'sessionclosed',
                         'badsession', 'sessiontimeout')
READ_ATTRIBUTES = ('Value', 'NodeClass', 'BrowseName', 'DisplayName',
                   'Description', 'WriteMask', 'UserWriteMask')


class CollectionError(RuntimeError):
    """采集过程中的可解释终止。"""


class BudgetExceeded(CollectionError):
    """节点或分页预算耗尽，未完成部分以缺口登记，不得冒充完整。"""


class CollectionCancelled(CollectionError):
    """外部请求取消，存续点释放后终止。"""


class StrictReplayAdapter:
    """把按序回放包装为严格核对参数的服务适配器（无 error 步的资料用）。

    真实接入可替换为同接口的适配器；传输与登录不在本模块范围。
    接口：browse / browse_next / read / close。
    """

    def __init__(self, replay, session_id):
        self._replay = replay
        self.session_id = session_id

    def browse(self, node_text):
        return self._replay.call('Browse', {
            'session': self.session_id, 'node': node_text})

    def browse_next(self, continuation_point, release):
        return self._replay.call('BrowseNext', {
            'session': self.session_id,
            'continuation_point': continuation_point,
            'release': bool(release)})

    def read(self, node_texts, attribute):
        return self._replay.call('Read', {
            'session': self.session_id,
            'nodes': list(node_texts), 'attribute': attribute})

    def close(self):
        return self._replay.call('Close', {'session': self.session_id})


class ScriptedAdapter:
    """按声明顺序解释合成服务资料的适配器。

    与 Replay 的区别：含 ``error`` 的步骤代表传输/服务层失败，抛错但不挪动
    后续响应，符合“这一调用没拿到响应、下一调用取下一条响应”的真实语义；
    每个会话独立取用属于自己的步骤，参数严格核对。
    """

    def __init__(self, steps, session_id):
        self.session_id = session_id
        self._steps = list(steps)
        self._position = 0
        self.calls = []

    def _take(self, operation, arguments):
        if self._position >= len(self._steps):
            raise RuntimeError(f'{self.session_id} 的脚本步骤已耗尽')
        step = self._steps[self._position]
        if step['operation'] != operation:
            raise ValueError(
                f'脚本操作顺序不符: 期望 {step["operation"]}，实际 {operation}')
        # 无 arguments 的 error 步代表传输层失败，无法核对请求，按操作匹配即可。
        if 'arguments' in step and step['arguments'] != arguments:
            raise ValueError(
                f'脚本请求参数不符: 期望 {step["arguments"]}，实际 {arguments}')
        self._position += 1
        self.calls.append({'operation': operation,
                           'arguments': dict(arguments)})
        if 'error' in step:
            raise RuntimeError(step['error'])
        return copy.deepcopy(step['response'])

    def remaining(self):
        return len(self._steps) - self._position

    def browse(self, node_text):
        return self._take('Browse',
                          {'session': self.session_id, 'node': node_text})

    def browse_next(self, continuation_point, release):
        return self._take('BrowseNext', {
            'session': self.session_id,
            'continuation_point': continuation_point,
            'release': bool(release)})

    def read(self, node_texts, attribute):
        return self._take('Read', {
            'session': self.session_id,
            'nodes': list(node_texts), 'attribute': attribute})

    def close(self):
        return self._take('Close', {'session': self.session_id})


def make_session_adapters(config):
    """按声明顺序把 steps 就近归属到会话，返回 session_id -> 适配器工厂。

    无 arguments 的 error 步（如传输层失效）归属到上一条带会话的步骤；
    每个会话独立消费自己的步骤，失败步不挪动后续响应。
    """
    grouped = {sid: [] for sid, _, _ in
               Collector._validate_sessions(config['sessions'])}
    current = None
    for step in config['steps']:
        target = step.get('arguments', {}).get('session')
        if target is not None:
            if target not in grouped:
                raise ValueError(f'步骤引用了未登记会话 {target}')
            current = target
        elif current is None:
            raise ValueError(
                '首个步骤必须显式声明 arguments.session: ' + step['operation'])
        grouped[current].append(step)
    adapters = {sid: ScriptedAdapter(steps, sid)
                for sid, steps in grouped.items()}

    def factory(session_id):
        return adapters[session_id]

    factory.adapters = adapters
    return factory


def _is_session_invalid(message):
    text = str(message).lower().replace('_', '').replace(' ', '')
    return any(hint.replace('_', '') in text for hint in SESSION_INVALID_HINTS)


class Collector:
    def __init__(self, config, adapter_factory, cancel=None):
        self._factory = adapter_factory
        self._cancel = cancel
        self.server_uri = self._require_str(config, 'server_uri') \
            if 'server_uri' in config else None
        self.sessions_cfg = self._validate_sessions(config['sessions'])
        if self.server_uri is None:
            self.server_uri = self.sessions_cfg[0][1]
        if any(uri != self.server_uri for _, uri, _ in self.sessions_cfg):
            raise ValueError('配置中的会话属于不同服务器，拒绝跨服务器混采')
        self.server_table = tuple(config.get('server_table', ()))
        self.allowed_references = frozenset(config['allowed_reference_types'])
        if not self.allowed_references:
            raise ValueError('允许展开的正向引用类型为空')
        self.max_nodes = self._positive_int(config, 'max_nodes')
        self.max_pages = self._positive_int(config, 'max_pages')
        self.read_batch = self._positive_int(config, 'read_batch')
        self.read_attributes = tuple(config.get('read_attributes', ('Value',)))
        if any(a not in READ_ATTRIBUTES for a in self.read_attributes):
            raise ValueError('Read 属性超出约定范围')
        self.maps = {}
        for sid, uri, namespaces in self.sessions_cfg:
            self.maps[sid] = SessionMap(sid, uri, namespaces,
                                        self.server_table)
        self.active_session = self.sessions_cfg[0][0]
        self.adapter = self._factory(self.active_session)

        self.nodes = {}
        self.edges = {}
        self.externals = {}
        self.pages = []
        self.continuations = []
        self.reads = []
        self.gaps = []
        self.queue = deque()
        self.active_node = None
        self.page_count = 0
        self.failovers = 0
        self.closed = False

    @staticmethod
    def _require_str(config, key):
        value = config[key]
        if not isinstance(value, str) or not value:
            raise ValueError(f'{key} 必须为非空字符串')
        return value

    @staticmethod
    def _positive_int(config, key):
        value = config[key]
        if type(value) is not int or value <= 0:
            raise ValueError(f'{key} 必须为正整数')
        return value

    @staticmethod
    def _validate_sessions(sessions):
        rows = []
        seen = set()
        for session in sessions:
            sid = session['id']
            if not isinstance(sid, str) or not sid or sid in seen:
                raise ValueError('会话 id 非空且唯一')
            seen.add(sid)
            uri = session['server_uri']
            namespaces = session['namespaces']
            if not isinstance(uri, str) or not uri:
                raise ValueError('服务器身份缺失')
            if not namespaces or any(not isinstance(x, str) or not x
                                    for x in namespaces):
                raise ValueError('命名空间列表无效')
            if len(set(namespaces)) != len(namespaces):
                raise ValueError('命名空间 URI 重复')
            rows.append((sid, uri, tuple(namespaces)))
        if not rows:
            raise ValueError('至少需要一个会话')
        return rows

    @property
    def map(self):
        return self.maps[self.active_session]

    def _gap(self, kind, detail):
        record = {'gap_id': f'G{len(self.gaps) + 1:04d}',
                  'kind': kind, 'detail': dict(detail)}
        self.gaps.append(record)
        return record

    def close(self):
        if self.closed:
            return
        self._release_active(because='collection_close')
        try:
            self.adapter.close()
        finally:
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # ---------- 续点生命周期 ----------
    def _track_continuation(self, point, node_key, state):
        record = {
            'continuation_point': point,
            'source_node': node_key.canonical,
            'session': self.active_session,
            'state': state,
        }
        self.continuations.append(record)
        return record

    def _set_continuation_state(self, point, state):
        for record in reversed(self.continuations):
            if (record['continuation_point'] == point
                    and record['session'] == self.active_session):
                record['state'] = state
                return record
        raise CollectionError(f'续点不属于当前会话或已不存在: {point!r}')

    def _release_active(self, because):
        """异常、取消、预算终止时释放尚存续点；失效会话的续点只能废弃。"""
        if self.active_node is None:
            return None
        point = self.active_node.get('continuation_point')
        if not point:
            return None
        try:
            self.adapter.browse_next(point, True)
        except RuntimeError as exc:
            record = self._set_continuation_state(point, 'release_failed')
            record['error'] = str(exc)
            self._gap('continuation_release_failed', {
                'source_node': self.active_node['stable'].canonical,
                'session': self.active_session,
                'continuation_point': point,
                'because': because, 'error': str(exc)})
        else:
            self._set_continuation_state(point, 'released')
        return point

    # ---------- 会话失效与按稳定身份重做 ----------
    def _failover(self, error):
        if self.failovers >= len(self.sessions_cfg) - 1:
            raise BudgetExceeded('没有可切换的后续会话')
        point = self.active_node.get('continuation_point')
        if point:
            record = self._set_continuation_state(point, 'abandoned')
            record['error'] = str(error)
        previous = self.active_session
        next_session = self.sessions_cfg[self.failovers + 1][0]
        self.failovers += 1
        self.active_session = next_session
        self.adapter = self._factory(next_session)
        self._gap('session_failover', {
            'failed_session': previous,
            'resume_session': next_session,
            'error': str(error),
            'abandoned_continuation_point': point,
            'rule': '旧续点与旧 ns 索引均不复用，未完成节点按稳定身份重做'})
        # 所有未完成节点前插重做（优先于尚未轮到的新发现节点）；
        # 已完成节点出队时直接跳过，重复入队幂等。
        for record in sorted(self.nodes.values(),
                             key=lambda r: r['stable'].canonical,
                             reverse=True):
            if not record['browse_complete']:
                record['attempted'] = False  # 新会话允许按稳定身份重新展开
                self.queue.appendleft(record['stable'])
                self._gap('interrupted_browse', {
                    'source_node': record['stable'].canonical,
                    'failed_session': previous,
                    'resume_session': next_session,
                    'reason': str(error),
                    'rule': '按稳定身份在新会话重做，不携带旧 ns 索引与续点'})

    def _check_cancel(self):
        if self._cancel is not None and self._cancel():
            raise CollectionCancelled('外部请求取消采集')

    # ---------- 引用与节点登记 ----------
    def _register_edge(self, source, ref, target_stable):
        key = (source.key(), target_stable.key(),
               ref['reference_type'], bool(ref['forward']))
        if key in self.edges:
            self.edges[key]['occurrences'] += 1
            return False
        self.edges[key] = {
            'source': source.canonical,
            'target': target_stable.canonical,
            'reference_type': ref['reference_type'],
            'forward': bool(ref['forward']),
            'in_scope': (ref['reference_type'] in self.allowed_references
                         and bool(ref['forward'])),
            'first_seen_session': self.active_session,
            'source_wire': ref.get('_source_wire'),
            'target_wire': ref['target'],
            'occurrences': 1,
        }
        return True

    def _register_external(self, source, ref, external):
        key = (source.key(), external['server_index'],
               external['namespace_uri'], external['identifier_type'],
               external['identifier'], ref['reference_type'])
        if key in self.externals:
            self.externals[key]['occurrences'] += 1
            return
        self.externals[key] = {
            'source': source.canonical,
            'reference_type': ref['reference_type'],
            'forward': bool(ref['forward']),
            'target_server_index': external['server_index'],
            'target_server_uri': external['server_uri'],
            'target_namespace_uri': external['namespace_uri'],
            'target_identifier_type': external['identifier_type'],
            'target_identifier': external['identifier'],
            'raw_expanded_node_id': external['raw'],
            'first_seen_session': self.active_session,
            'occurrences': 1,
        }

    def _discover(self, stable, ref):
        existing = self.nodes.get(stable.key())
        if existing is not None:
            return existing
        if len(self.nodes) >= self.max_nodes:
            self._gap('node_budget', {
                'node': stable.canonical,
                'limit': self.max_nodes,
                'via_reference': ref['reference_type']})
            return None
        record = {
            'stable': stable,
            'node_class': ref['class'],
            'display_name': ref['display'],
            'browse_complete': False,
            'discovered': True,
            'discovery_session': self.active_session,
            'discovery_wire': ref['target'],
        }
        self.nodes[stable.key()] = record
        self.queue.append(stable)
        return record

    def _validate_ref(self, ref, source_wire):
        if not isinstance(ref, dict):
            raise ValueError('Browse 引用项必须为对象')
        for field in ('target', 'reference_type', 'class'):
            if not isinstance(ref.get(field), str) or not ref[field]:
                raise ValueError(f'Browse 引用字段 {field} 无效')
        if not isinstance(ref.get('forward'), bool):
            raise ValueError('Browse 引用 forward 必须为布尔值')
        if 'display' in ref and not isinstance(ref['display'], str):
            raise ValueError('display 必须为字符串')
        enriched = dict(ref)
        enriched['_source_wire'] = source_wire
        return enriched

    def _consume_references(self, source, refs, source_wire):
        for raw_ref in refs:
            ref = self._validate_ref(raw_ref, source_wire)
            wire = parse_wire(ref['target'])
            kind, payload = self.map.classify(wire)
            if kind == 'external':
                # 超出配置服务器范围的 ExpandedNodeId：只留外部引用，不连接。
                self._register_external(source, ref, payload)
                continue
            target = payload
            is_new_edge = self._register_edge(source, ref, target)
            if is_new_edge and (ref['reference_type'] in self.allowed_references
                                and ref['forward']):
                self._discover(target, ref)

    # ---------- 单节点分页展开 ----------
    def _browse_node(self, record):
        source = record['stable']
        node_text = self.map.wire_text(source)
        self.active_node = record
        record['continuation_point'] = None
        try:
            response = self.adapter.browse(node_text)
            if not self._record_page(source, node_text, 'Browse', response):
                return
            self._consume_references(source, response.get('references', []),
                                     node_text)
            point = response.get('continuation')
            if point:
                if not isinstance(point, str):
                    raise ValueError('continuation point 必须为字符串或 null')
                record['continuation_point'] = point
                self._track_continuation(point, source, 'active')
            while point:
                self._check_cancel()
                response = self.adapter.browse_next(point, False)
                if not self._record_page(source, node_text, 'BrowseNext',
                                         response, continuation_point=point):
                    # 非 Good 页面：续点状态不再可信，释放后以缺口保留未完整。
                    self._release_active(because='bad_browse_status')
                    record['continuation_point'] = None
                    return
                # 空页带非空续点仍须继续，不能以零条冒报完整。
                self._consume_references(source, response.get('references', []),
                                         node_text)
                next_point = response.get('continuation')
                self._set_continuation_state(point, 'consumed')
                point = next_point
                record['continuation_point'] = point
                if point:
                    if not isinstance(point, str):
                        raise ValueError('continuation point 必须为字符串或 null')
                    self._track_continuation(point, source, 'active')
                # point 为 null：服务端已在末页自动释放续点，不额外请求。
            # 所有分页完成才可标记展开完整。
            record['browse_complete'] = True
        except (CollectionCancelled, BudgetExceeded):
            # 采集控制异常优先：释放存续点后继续向上传播，不能被服务错误兜底吞掉。
            self._release_active(because='termination')
            record['continuation_point'] = None
            raise
        except RuntimeError as exc:
            if _is_session_invalid(exc):
                self._failover(exc)
            else:
                self._release_active(because='browse_error')
                record['continuation_point'] = None
                self._gap('browse_error', {
                    'source_node': source.canonical,
                    'session': self.active_session,
                    'error': str(exc)})
        finally:
            self.active_node = None

    def _record_page(self, source, node_text, operation, response,
                     continuation_point=None):
        """登记一页原始响应；非 Good 状态登记缺口并返回 False。"""
        if not isinstance(response, dict):
            raise ValueError('Browse 响应必须为对象')
        status = response.get('status')
        if not isinstance(status, str) or not status:
            raise ValueError('Browse 响应 status 无效')
        if not isinstance(response.get('references', []), list):
            raise ValueError('Browse references 必须为列表')
        self.page_count += 1
        if self.page_count > self.max_pages:
            raise BudgetExceeded(
                f'分页预算耗尽: max_pages={self.max_pages}')
        self.pages.append({
            'page_id': f'P{self.page_count:04d}',
            'source_node': source.canonical,
            'session': self.active_session,
            'operation': operation,
            'node_wire': node_text,
            'request_continuation_point': continuation_point,
            'status_code': status,
            'reference_count': len(response.get('references', [])),
            'response_continuation_point': response.get('continuation'),
        })
        if not status.startswith('Good'):
            self._gap('browse_status_not_good', {
                'source_node': source.canonical,
                'session': self.active_session,
                'operation': operation,
                'node_wire': node_text,
                'status_code': status,
                'continuation_point': continuation_point})
            return False
        return True

    # ---------- 遍历入口 ----------
    def walk(self, roots):
        for root in roots:
            wire = root_wire(root)
            stable = self.map.resolve(wire)
            if stable.key() not in self.nodes:
                if len(self.nodes) >= self.max_nodes:
                    self._gap('node_budget', {
                        'node': stable.canonical, 'limit': self.max_nodes,
                        'via_reference': 'root'})
                    continue
                self.nodes[stable.key()] = {
                    'stable': stable,
                    'node_class': None,
                    'display_name': None,
                    'browse_complete': False,
                    'discovered': False,
                    'discovery_session': self.active_session,
                    'discovery_wire': wire.raw,
                }
                self.queue.append(stable)
        while self.queue:
            self._check_cancel()
            stable = self.queue.popleft()
            record = self.nodes[stable.key()]
            if record['browse_complete'] or record.get('attempted'):
                # 未完成但已尝试过的节点（Bad 状态/错误）不无限重试，
                # 缺口已登记；只有会话失效重做会清掉 attempted。
                continue
            record['attempted'] = True
            self._browse_node(record)

    # ---------- 属性 Read：按请求索引还原 ----------
    def read_values(self):
        if 'Value' not in self.read_attributes:
            return
        variables = sorted(
            (r for r in self.nodes.values()
             if r['node_class'] == 'Variable'),
            key=lambda r: r['stable'].canonical)
        for start in range(0, len(variables), self.read_batch):
            self._check_cancel()
            batch = variables[start:start + self.read_batch]
            batch_id = f'RB{len(self.reads) + 1:04d}'
            try:
                node_texts, unresolved = [], []
                for record in batch:
                    try:
                        node_texts.append(self.map.wire_text(record['stable']))
                    except NodeIdError as exc:
                        unresolved.append((record, str(exc)))
                for record, message in unresolved:
                    self._gap('namespace_unavailable', {
                        'node': record['stable'].canonical,
                        'session': self.active_session, 'error': message})
                if not node_texts:
                    continue
                response = self.adapter.read(node_texts, 'Value')
            except RuntimeError as exc:
                if _is_session_invalid(exc):
                    raise CollectionError(
                        'Read 阶段会话失效，本资料不支持自动重连重试') from exc
                for text in node_texts:
                    self._gap('read_error', {
                        'session': self.active_session,
                        'node_wire': text, 'error': str(exc)})
                continue
            results = response.get('results')
            if not isinstance(results, list):
                raise ValueError('Read 响应 results 必须为列表')
            if len(results) != len(node_texts):
                self._gap('read_result_mismatch', {
                    'batch_id': batch_id,
                    'requested': len(node_texts),
                    'returned': len(results)})
            batch_record = {
                'batch_id': batch_id,
                'session': self.active_session,
                'attribute': 'Value',
                'requests': [],
            }
            for index, (text, result) in enumerate(zip(node_texts, results)):
                batch_record['requests'].append(
                    self._render_read_result(index, text, result))
            self.reads.append(batch_record)

    @staticmethod
    def _status_category(status):
        if not isinstance(status, str) or not status:
            raise ValueError('Read StatusCode 无效')
        if status.startswith('Good'):
            return 'Good'
        if status.startswith('Uncertain'):
            return 'Uncertain'
        if status.startswith('Bad'):
            return 'Bad'
        raise ValueError(f'无法识别的 StatusCode 主类别: {status}')

    def _render_read_result(self, index, text, result):
        if not isinstance(result, dict):
            raise ValueError('Read 结果项必须为对象')
        status = result['status']
        category = self._status_category(status)
        record = {
            'request_index': index,
            'node_wire': text,
            'status_code': status,
            'status_category': category,
        }
        if 'value' in result:
            # 原值保留；Bad 的 null 只是线上原值，不写成正常取值。
            record['value_present'] = True
            record['raw_value'] = result['value']
        else:
            record['value_present'] = False
        for stamp in ('source_timestamp', 'server_timestamp'):
            if stamp in result:
                record[stamp] = result[stamp]
        return record


def _stable_payload(stable):
    return {
        'server_uri': stable.server_uri,
        'namespace_uri': stable.namespace_uri,
        'identifier_type': stable.identifier_type,
        'identifier': canonical_identifier(stable.identifier_type,
                                           stable.identifier),
        'stable_id': stable.canonical,
    }


def build_material(collector, config, source_digest=None):
    nodes = []
    for record in sorted(collector.nodes.values(),
                         key=lambda r: r['stable'].canonical):
        nodes.append({
            **_stable_payload(record['stable']),
            'node_class': record['node_class'],
            'display_name': record['display_name'],
            'browse_complete': record['browse_complete'],
            'discovered_via_browse': record['discovered'],
            'discovery_session': record['discovery_session'],
            'discovery_wire': record['discovery_wire'],
        })
    edges = sorted(collector.edges.values(),
                   key=lambda e: (e['source'], e['target'],
                                  e['reference_type'], e['forward']))
    externals = sorted(collector.externals.values(),
                       key=lambda e: (e['source'], e['raw_expanded_node_id']))
    return {
        'material_version': 1,
        'source_sha256': source_digest,
        'server': {
            'server_uri': collector.server_uri,
            'server_table': list(collector.server_table),
        },
        'sessions': [
            {'id': sid, 'server_uri': uri, 'namespace_array': list(ns),
             'namespace_index_map': {u: i for i, u in enumerate(ns)}}
            for sid, uri, ns in collector.sessions_cfg
        ],
        'config': {
            'allowed_reference_types': sorted(collector.allowed_references),
            'max_nodes': collector.max_nodes,
            'max_pages': collector.max_pages,
            'read_batch': collector.read_batch,
            'read_attributes': list(collector.read_attributes),
        },
        'consistency': {
            'traversal_complete': all(
                r['browse_complete'] for r in collector.nodes.values()),
            'point_in_time_snapshot': False,
            'snapshot_guarantee': (
                '服务器未提供整库版本保证；Read 分批执行，批次之间不是原子快照，'
                '资料只保证遍历完整性，不声称同一时刻一致'),
        },
        'nodes': nodes,
        'edges': edges,
        'external_references': externals,
        'browse_pages': collector.pages,
        'continuation_points': collector.continuations,
        'read_batches': collector.reads,
        'gaps': collector.gaps,
        'summary': {
            'nodes_total': len(collector.nodes),
            'nodes_browse_complete': sum(
                1 for r in collector.nodes.values() if r['browse_complete']),
            'edges_total': len(collector.edges),
            'external_references_total': len(collector.externals),
            'browse_pages_total': len(collector.pages),
            'read_batches_total': len(collector.reads),
            'read_results_good': sum(
                1 for b in collector.reads for r in b['requests']
                if r['status_category'] == 'Good'),
            'read_results_uncertain': sum(
                1 for b in collector.reads for r in b['requests']
                if r['status_category'] == 'Uncertain'),
            'read_results_bad': sum(
                1 for b in collector.reads for r in b['requests']
                if r['status_category'] == 'Bad'),
            'gaps_total': len(collector.gaps),
            'session_failovers': collector.failovers,
        },
    }


def collect_material(config, adapter_factory, roots, cancel=None,
                     source_digest=None):
    """完整采集流程；调用方负责确保 adapter_factory 只产生同服务器会话。"""
    with Collector(config, adapter_factory, cancel) as collector:
        collector.walk(roots)
        collector.read_values()
        return build_material(collector, config, source_digest)
