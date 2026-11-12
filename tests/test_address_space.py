"""地址空间采集器的图展开、续点、会话与 Read 语义。"""
import unittest

from source_data.address_space import (BudgetExceeded, CollectionCancelled,
                                       Collector, collect_material,
                                       make_session_adapters)
from source_data.io import load

CONFIG = {
    'sessions': [
        {'id': 's1', 'server_uri': 'urn:h:lab',
         'namespaces': ['http://opcfoundation.org/UA/', 'urn:v:A', 'urn:v:B']},
        {'id': 's2', 'server_uri': 'urn:h:lab',
         'namespaces': ['http://opcfoundation.org/UA/', 'urn:v:B', 'urn:v:A']},
    ],
    'server_table': [{'server_index': 1, 'server_uri': 'urn:v:C'}],
    'roots': [{'namespace_index': 1, 'identifier_type': 'string',
               'identifier': 'root'}],
    'allowed_reference_types': ['Organizes', 'HasComponent'],
    'max_nodes': 100,
    'max_pages': 200,
    'read_batch': 2,
    'read_attributes': ['Value'],
}


class FakeAdapter:
    """可编程适配器，记录释放调用，可按会话注入失效与发布脚本。"""

    def __init__(self, session_id, scripts, releases, failures=()):
        self.session_id = session_id
        self._scripts = scripts          # wire text -> [page, page, ...]
        self._releases = releases
        self._fail_before = dict(failures or ())
        self.read_calls = []
        self.close_called = False

    def browse(self, node_text):
        return self._next(node_text)

    def browse_next(self, continuation_point, release):
        if release:
            self._releases.append(continuation_point)
            return {'status': 'Good', 'references': [],
                    'continuation': None}
        return self._next(continuation_point)

    def _next(self, key):
        if key in self._fail_before:
            raise RuntimeError(self._fail_before.pop(key))
        pages = self._scripts[key]
        page = pages.pop(0)
        if 'error' in page:
            raise RuntimeError(page['error'])
        return page

    def read(self, node_texts, attribute):
        self.read_calls.append(list(node_texts))
        results = []
        for text in node_texts:
            if text.endswith('s=bad'):
                results.append({'status': 'BadNotReadable', 'value': None})
            elif text.endswith('s=unc'):
                results.append({'status': 'UncertainLastUsableValue',
                                'value': 1, 'source_timestamp': 't0',
                                'server_timestamp': 't1'})
            else:
                results.append({'status': 'Good', 'value': 7,
                                'source_timestamp': 't0',
                                'server_timestamp': 't1'})
        return {'results': results}

    def close(self):
        self.close_called = True


def ref(target, rtype='HasComponent', forward=True, cls='Object', display='d'):
    return {'target': target, 'reference_type': rtype, 'forward': forward,
            'class': cls, 'display': display}


def page(refs, continuation=None, status='Good'):
    return {'status': status, 'references': refs, 'continuation': continuation}


class GraphTests(unittest.TestCase):
    def _make(self, scripts_s1, releases=None, failures=None):
        releases = releases if releases is not None else []
        adapters = {}
        adapters['s1'] = FakeAdapter('s1', scripts_s1, releases, failures)
        adapters['s2'] = FakeAdapter('s2', {}, releases)
        c = Collector(CONFIG, lambda sid: adapters[sid])
        return c, adapters, releases

    def test_expand_once_but_keep_all_edges_multiparent_and_cycle(self):
        scripts = {
            'ns=1;s=root': [page([ref('ns=1;s=child'),
                                  ref('ns=1;s=child', rtype='Organizes')])],
            'ns=1;s=child': [page([ref('ns=1;s=root'),     # 环
                                   ref('ns=1;s=child')])],  # 自环
        }
        c, _, _ = self._make(scripts)
        c.walk(CONFIG['roots'])
        # 两个节点各只展开一次（browse 脚本被取空即证明无重复展开）。
        self.assertTrue(all(not v for v in scripts.values()))
        self.assertEqual(len(c.nodes), 2)
        # 多父/环/自环边全部保留。
        kinds = {(e['source'], e['target']) for e in c.edges.values()}
        self.assertIn(
            ('stable:server=urn:h:lab;ns=urn:v:A;s=root',
             'stable:server=urn:h:lab;ns=urn:v:A;s=child'), kinds)
        self.assertIn(
            ('stable:server=urn:h:lab;ns=urn:v:A;s=child',
             'stable:server=urn:h:lab;ns=urn:v:A;s=root'), kinds)

    def test_empty_page_with_continuation_keeps_paging(self):
        scripts = {
            'ns=1;s=root': [
                page([], continuation='cp/1'),
                page([], continuation='cp/2'),
                page([ref('ns=1;s=leaf')], continuation=None)],
            'cp/1': [page([], continuation='cp/2')],
            'cp/2': [page([ref('ns=1;s=leaf')], continuation=None)],
            'ns=1;s=leaf': [page([])],
        }
        c, _, _ = self._make(scripts)
        c.walk(CONFIG['roots'])
        root = next(r for r in c.nodes.values()
                    if r['stable'].identifier == 'root')
        self.assertTrue(root['browse_complete'])
        self.assertEqual(len(c.pages), 4)  # 空页不算完整，继续换页。

    def test_non_allowed_and_inverse_edges_kept_but_not_expanded(self):
        scripts = {
            'ns=1;s=root': [page([
                ref('ns=1;s=keep', rtype='HasProperty'),
                ref('ns=1;s=back', forward=False)])],
        }
        c, _, _ = self._make(scripts)
        c.walk(CONFIG['roots'])
        self.assertEqual(len(c.nodes), 1)  # 两个目标都不展开。
        self.assertEqual(len(c.edges), 2)  # 但边全部保留。
        self.assertTrue(all(not e['in_scope'] for e in c.edges.values()))

    def test_external_expanded_node_id_is_reference_only(self):
        scripts = {
            'ns=1;s=root': [page([ref('svr=1;su=urn:v:C;ns=2;s=remote')])],
        }
        c, _, _ = self._make(scripts)
        c.walk(CONFIG['roots'])
        self.assertEqual(len(c.nodes), 1)
        self.assertEqual(len(c.externals), 1)
        self.assertEqual(
            c.externals[next(iter(c.externals))]['target_server_uri'],
            'urn:v:C')

    def test_duplicate_reference_is_idempotent_merge(self):
        scripts = {
            'ns=1;s=root': [page([ref('ns=1;s=leaf')])],
            'ns=1;s=leaf': [page([ref('ns=1;s=root')])],
        }
        c, _, _ = self._make(scripts)
        c.walk(CONFIG['roots'])
        leaf_edges = [e for e in c.edges.values()
                      if e['target'].endswith('s=leaf')]
        self.assertEqual(len(leaf_edges), 1)

    def test_continuation_released_on_cancel(self):
        scripts = {
            'ns=1;s=root': [page([ref('ns=1;s=a')], continuation='cp/root')],
        }
        releases = []
        c, _, _ = self._make(scripts, releases)
        calls = {'n': 0}

        def cancel():
            calls['n'] += 1
            return calls['n'] > 1  # 首次 Browse 放行，首次换页前取消。

        c._cancel = cancel
        with self.assertRaises(CollectionCancelled):
            c.walk(CONFIG['roots'])
        c.close()
        self.assertIn('cp/root', releases)
        self.assertTrue(any(cp['state'] == 'released'
                            for cp in c.continuations))
        root = next(r for r in c.nodes.values()
                    if r['stable'].identifier == 'root')
        self.assertFalse(root['browse_complete'])  # 绝不冒报完整。

    def test_budget_termination_releases_continuation(self):
        small = dict(CONFIG, max_nodes=1, max_pages=1)
        scripts = {
            'ns=1;s=root': [page([ref('ns=1;s=a'),
                                  ref('ns=1;s=b')], continuation='cp/x')],
            'cp/x': [page([], continuation='cp/y')],
            'cp/y': [page([], continuation=None)],
        }
        releases = []
        adapters = {'s1': FakeAdapter('s1', scripts, releases),
                    's2': FakeAdapter('s2', {}, releases)}
        c = Collector(small, lambda sid: adapters[sid])
        with self.assertRaises(BudgetExceeded):
            c.walk(CONFIG['roots'])  # 分页预算耗尽：释放存续点后可解释终止。
        c.close()
        # 预算终止时存续点必须被释放，不能静默丢弃，也不冒报完整。
        self.assertIn('cp/x', releases)
        self.assertTrue(any(g['kind'] == 'node_budget' for g in c.gaps))
        root = next(r for r in c.nodes.values()
                    if r['stable'].identifier == 'root')
        self.assertFalse(root['browse_complete'])

    def test_session_failover_redoes_by_stable_identity_without_old_ns(self):
        # s1 的 root 第一页拿到 child 并留下续点，换页时会话失效。
        s1_scripts = {
            'ns=1;s=root': [page([ref('ns=1;s=child')], continuation='cp/s1')],
            'cp/s1': [{'error': 'BadSessionIdInvalid'}],
        }
        releases = []
        adapters = {
            's1': FakeAdapter('s1', s1_scripts, releases),
            's2': FakeAdapter('s2', {
                # 命名空间重排后 root 变成 ns=2，重做不带旧 ns/旧续点。
                'ns=2;s=root': [page([ref('ns=2;s=child')])],
                'ns=2;s=child': [page([])],
            }, releases),
        }
        c = Collector(CONFIG, lambda sid: adapters[sid])
        c.walk(CONFIG['roots'])
        self.assertEqual(c.active_session, 's2')
        self.assertEqual(c.failovers, 1)
        # 失效续点标记 abandoned，而不是被拿去复用或释放。
        cp = next(cp for cp in c.continuations if cp['state'] == 'abandoned')
        self.assertEqual(cp['session'], 's1')
        self.assertTrue(all(n['browse_complete'] for n in c.nodes.values()))
        # s2 浏览 root 用的是重解析后的 ns=2。
        browse_wires = [p['node_wire'] for p in c.pages if p['session'] == 's2']
        self.assertIn('ns=2;s=root', browse_wires)
        self.assertNotIn('ns=1;s=root', browse_wires)
        c.close()
        self.assertTrue(adapters['s2'].close_called)

    def test_bad_browse_status_records_gap_and_keeps_incomplete(self):
        scripts = {
            'ns=1;s=root': [page([ref('ns=1;s=dead')])],
            'ns=1;s=dead': [page([], status='BadNoMatch')],
        }
        c, _, _ = self._make(scripts)
        c.walk(CONFIG['roots'])
        dead = next(r for r in c.nodes.values()
                    if r['stable'].identifier == 'dead')
        self.assertFalse(dead['browse_complete'])
        self.assertTrue(any(g['kind'] == 'browse_status_not_good'
                            for g in c.gaps))

    def test_read_restores_by_index_keeps_bad_and_uncertain(self):
        scripts = {
            'ns=1;s=root': [page([
                ref('ns=1;s=good', cls='Variable'),
                ref('ns=1;s=unc', cls='Variable'),
                ref('ns=1;s=bad', cls='Variable'),
                ref('ns=1;s=obj', cls='Object')])],
            'ns=1;s=good': [page([])], 'ns=1;s=unc': [page([])],
            'ns=1;s=bad': [page([])], 'ns=1;s=obj': [page([])],
        }
        c, adapters, _ = self._make(scripts)
        c.walk(CONFIG['roots'])
        c.read_values()
        all_requests = [r for b in c.reads for r in b['requests']]
        categories = {r['node_wire'].split(';')[-1]: r['status_category']
                      for r in all_requests}
        self.assertEqual(categories['s=good'], 'Good')
        self.assertEqual(categories['s=unc'], 'Uncertain')
        self.assertEqual(categories['s=bad'], 'Bad')
        # Bad 的 null 作为原值保留，不伪装成正常 value。
        bad = next(r for r in all_requests
                   if r['node_wire'].endswith('s=bad'))
        self.assertTrue(bad['value_present'])
        self.assertIsNone(bad['raw_value'])
        # 只有 Variable 读 Value，Object 不读。
        self.assertEqual(sum(len(b['requests']) for b in c.reads), 3)
        # read_batch=2 产生分批，批次不是原子快照。
        self.assertEqual(len(adapters['s1'].read_calls), 2)

    def test_material_is_deterministic_across_runs(self):
        value, _ = load('data/hydrogen-upgrade.json')

        def run():
            factory = make_session_adapters(value)
            return collect_material(value, factory, value['roots'])

        import json
        first = json.dumps(run(), ensure_ascii=False, sort_keys=True)
        second = json.dumps(run(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_fixture_end_to_end_summary(self):
        value, digest = load('data/hydrogen-upgrade.json')
        factory = make_session_adapters(value)
        material = collect_material(value, factory, value['roots'],
                                    source_digest=digest)
        summary = material['summary']
        self.assertEqual(summary['session_failovers'], 1)
        self.assertEqual(summary['read_results_good'], 1)
        self.assertEqual(summary['read_results_uncertain'], 1)
        self.assertEqual(summary['read_results_bad'], 1)
        self.assertEqual(summary['external_references_total'], 2)
        self.assertFalse(material['consistency']['point_in_time_snapshot'])
        # 两个同线上文本 tag-B 是不同稳定身份，未被合并。
        tag_nodes = [n for n in material['nodes']
                     if n['identifier'] == 'tag-B']
        self.assertEqual(len(tag_nodes), 2)
        self.assertEqual(len({n['namespace_uri'] for n in tag_nodes}), 2)
        # 所有脚本步骤都被消费。
        self.assertTrue(all(a.remaining() == 0
                            for a in factory.adapters.values()))


if __name__ == '__main__':
    unittest.main()
