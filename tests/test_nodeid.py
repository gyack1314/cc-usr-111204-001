"""nodeid 的解析、类型身份与每会话命名空间重解析。"""
import unittest
from source_data.nodeid import (NodeIdError, SessionMap, StableId,
                                canonical_identifier, parse_wire, root_wire)

SERVER = 'urn:hydrogen:lab'
NS_A = ['http://opcfoundation.org/UA/', 'urn:vendor:A', 'urn:vendor:B']
NS_B = ['http://opcfoundation.org/UA/', 'urn:vendor:B', 'urn:vendor:A']


class NodeIdTests(unittest.TestCase):
    def test_typed_identifiers_never_merge(self):
        keys = set()
        for text in ('ns=1;s=4711', 'ns=1;i=4711'):
            wire = parse_wire(text)
            keys.add(('s1', NS_A[wire.namespace_index],
                      wire.identifier_type, wire.identifier))
        # 文本相同（4711）但 string 与 numeric 是两个身份。
        self.assertEqual(len(keys), 2)
        self.assertEqual(parse_wire('ns=1;i=4711').identifier, 4711)
        self.assertEqual(parse_wire('ns=1;b=0A1BFF').identifier,
                         bytes.fromhex('0a1bff'))

    def test_guid_and_bytes_canonical(self):
        self.assertEqual(
            canonical_identifier('guid', parse_wire(
                'ns=1;g=550E8400-E29B-41D4-A716-446655440000').identifier),
            '550e8400-e29b-41d4-a716-446655440000')
        self.assertEqual(
            canonical_identifier('bytes', parse_wire('ns=1;b=0A1BFF').identifier),
            '0a1bff')

    def test_malformed(self):
        for text in ('', 'ns=1;x=1', 'ns=1;i=', 's=1', 'ns=1;i=-1',
                     'ns=1;b=zz', 'ns=1;g=not-a-uuid', ';i=1',
                     'ns=1;i=1.5'):
            with self.subTest(text=text):
                with self.assertRaises(NodeIdError):
                    parse_wire(text)

    def test_namespace_index_out_of_range(self):
        s1 = SessionMap('s1', SERVER, NS_A)
        with self.assertRaises(NodeIdError):
            s1.resolve(parse_wire('ns=99;i=1'))

    def test_namespace_reindex_on_each_session(self):
        s1 = SessionMap('s1', SERVER, NS_A)
        s2 = SessionMap('s2', SERVER, NS_B)
        # 同一线上文本 ns=1;s=plant 在两个会话指向不同命名空间 URI。
        id1 = s1.resolve(parse_wire('ns=1;s=plant'))
        id2 = s2.resolve(parse_wire('ns=1;s=plant'))
        self.assertEqual(id1.namespace_uri, 'urn:vendor:A')
        self.assertEqual(id2.namespace_uri, 'urn:vendor:B')
        self.assertNotEqual(id1, id2)
        # 同一稳定身份在两个会话还原出不同线上索引，绝不复用旧 ns。
        self.assertEqual(s1.wire_text(id1), 'ns=1;s=plant')
        self.assertEqual(s2.wire_text(id1), 'ns=2;s=plant')

    def test_expanded_node_id_external(self):
        table = [{'server_index': 1, 'server_uri': 'urn:vendor:C'}]
        s1 = SessionMap('s1', SERVER, NS_A, table)
        kind, payload = s1.classify(parse_wire(
            'svr=1;su=urn:vendor:C;ns=2;s=remote'))
        self.assertEqual(kind, 'external')
        self.assertEqual(payload['server_uri'], 'urn:vendor:C')
        # 未知服务端：server_uri 留空但仍只登记外部引用。
        kind, payload = s1.classify(parse_wire(
            'svr=9;su=urn:unknown;ns=1;s=ghost'))
        self.assertEqual(kind, 'external')
        self.assertIsNone(payload['server_uri'])
        # serverIndex=0 且 su 命中本会话命名空间时按本地处理。
        kind, payload = s1.classify(parse_wire(
            'svr=0;su=urn:vendor:A;ns=1;s=local'))
        self.assertEqual(kind, 'local')

    def test_duplicate_namespace_rejected(self):
        with self.assertRaises(NodeIdError):
            SessionMap('s1', SERVER, ['u', 'u'])

    def test_root_wire_typed(self):
        wire = root_wire({'namespace_index': 1,
                          'identifier_type': 'bytes', 'identifier': '0aff'})
        self.assertEqual(wire.identifier, bytes.fromhex('0aff'))
        self.assertEqual(wire.raw, 'ns=1;b=0aff')


if __name__ == '__main__':
    unittest.main()
