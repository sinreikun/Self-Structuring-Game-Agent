"""Agent-provided descriptions are display-only, never permissions or game labels."""
from .presentation import prose


def validate_description(request, revision):
    nodes=request.get('nodes',[]);edges=request.get('edges',[])
    if not isinstance(nodes,list) or len(nodes)>32 or not isinstance(edges,list) or len(edges)>64:raise ValueError('Invalid capability graph')
    result=[];ids=set()
    for node in nodes:
        if not isinstance(node,dict) or not isinstance(node.get('id'),str) or len(node['id'])>64 or node['id'] in ids:raise ValueError('Invalid capability identity')
        ids.add(node['id'])
        item={'id':node['id'],'revision':revision}
        for key in ('description','role','input','output','effectiveness'):
            value=node.get(key,'未報告')
            if not isinstance(value,str) or len(value)>240:raise ValueError('Invalid description')
            item[key]=prose(value)
        item['uses']=max(0,min(int(node.get('uses',0)),2**31-1))
        item['active']=bool(node.get('active',False))
        generation=node.get('created_generation')
        if generation is not None:
            if type(generation)!=int or not 0<=generation<=1000000:raise ValueError('Invalid creation generation')
            item['created_generation']=generation
        status=node.get('status','使用中' if item['active'] else '待機中')
        if status not in {'使用中','待機中','試験中','廃止候補'}:raise ValueError('Invalid capability status')
        item['status']=status
        result.append(item)
    links=[]
    for edge in edges:
        if not isinstance(edge,list) or len(edge)!=2 or any(e not in ids for e in edge):raise ValueError('Invalid capability edge')
        links.append(edge)
    return {'nodes':result,'edges':links,'source':'agent_self_report','revision':revision}
