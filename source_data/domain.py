"""清点已解码服务资料，不遍历节点或跟踪续点。"""
from .io import unique,integer

def summarize(v,root):
    unique(v['sessions'],'id');sizes=[]
    for s in v['sessions']:
        if not isinstance(s['server_uri'],str) or not s['server_uri']:raise ValueError('服务器身份缺失')
        if not s['namespaces'] or any(not isinstance(x,str) for x in s['namespaces']):raise ValueError('命名空间列表无效')
        if len(set(s['namespaces']))!=len(s['namespaces']):raise ValueError('命名空间URI重复')
        sizes.append(len(s['namespaces']))
    for r in v['roots']:
        integer(r['namespace_index']);
        if r['identifier_type'] not in ('numeric','string','guid','bytes'):raise ValueError('节点类型未知')
    return {'sessions':len(sizes),'namespace_counts':sizes,'roots':len(v['roots']),'source_steps':len(v['steps'])}
