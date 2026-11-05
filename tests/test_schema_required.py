import copy,json
import pytest
from selfstruct.research_schema import action_schema,validate,SchemaViolation,validate_experiment
from selfstruct.research import ResearchRepairNeeded
from test_state_on_road import road

BASE={'op':'experiment','hypothesis':'h','action':0,'claim':{'type':'transition','probability':.5,'from_state':{'text':'a','evidence':[1],'phase':'before'},'to_state':2}}

@pytest.mark.parametrize('path',[('hypothesis',),('claim',),('claim','type'),('claim','probability'),('claim','from_state'),('claim','to_state'),('claim','from_state','text'),('claim','from_state','evidence'),('claim','from_state','phase')])
def test_required_paths_are_grammar_required(path):
    r=copy.deepcopy(BASE);parent=r
    for k in path[:-1]:parent=parent[k]
    del parent[path[-1]]
    with pytest.raises(SchemaViolation):validate_experiment(r,1)
    validate_experiment(BASE,1)

def test_values_remain_host_responsibility(road):
    from selfstruct.research_actions import run_actions,execute
    s,p,e=road
    r={'op':'experiment','hypothesis':'invented','action':0,'claim':{'type':'change_rate','probability':.5,'situation':{'text':'s','evidence':[e],'phase':'before'}}}
    validate_experiment(r,1);run_actions(s,'job',p,[r])
    result=p['action_history'][-1]['result']
    assert result['state']=='claim_required' and 'invented' in result['fields'][0]['reason']
    for state in (900,901):
        r={**r,'hypothesis':'h','claim':{'type':'transition','probability':.5,'from_state':state,'to_state':state}}
        validate_experiment(r,1);result=execute(s,'job',p,r,str(state))[0]
        assert len(result['fields'])==2 and all(str(state) in f['reason'] for f in result['fields'])
    r={**r,'claim':{'type':'change_rate','probability':2,'situation':{'text':'s','evidence':[e],'phase':'before'}}}
    
    with pytest.raises(SchemaViolation):validate_experiment(r,1)
    assert execute(s,'job',p,r,'prob')[0]['fields'][0]['field']=='claim.probability'
    r={**r,'claim':{'type':'change_rate','probability':.5,'situation':{'text':'s','evidence':[e],'phase':'before'}}}
    assert execute(s,'job',p,r,'alias')[0]['registration']['hypothesis']==p['hypotheses']['h']

def test_change_rate_situation_is_gate_required(road):
    from selfstruct.research_actions import execute
    s,p,e=road
    r={'op':'experiment','hypothesis':'h','action':0,'claim':{'type':'change_rate','probability':.5}}
    validate_experiment(r,1)
    result=execute(s,'job',p,r,'nosit')[0]
    assert any(f['field']=='claim.situation' for f in result['fields'])

def test_schema_violation_persisted_separately_before_findings(tmp_path):
    from selfstruct.config import Config
    from selfstruct.engine import Controller
    from selfstruct.smoke import SyntheticGame
    c=Controller(Config(root=str(tmp_path),keys=['A']),SyntheticGame());c.step()
    response=json.dumps({'findings':[{'id':'h','text':'unexecuted','evidence':[c.last_eid]}],'actions':[{'op':'experiment','action':0}]})
    class Client:
        def complete(self,*a,**kw):
            variants=kw['schema']['properties']['actions']['items']['anyOf']
            assert not any(v['properties']['op']['enum']==['experiment'] for v in variants)
            return response
    c.scientist.client=Client();jid=c.store.job('research',{})
    try:
        with pytest.raises(ResearchRepairNeeded,match='schema_violation'):c.scientist.study(c.store.rows(),jid)
        p=json.loads(c.store.get_job(jid)['payload'])
        assert len(p['schema_violations'])==1 and p['schema_violations'][0]['response']==response
        assert not p.get('action_history') and not p.get('hypotheses')
        assert 'schema_violation' in p['repair_error']
    finally:c.close()
