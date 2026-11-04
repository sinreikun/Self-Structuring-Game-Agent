import copy
import pytest
from selfstruct.research_schema import reference_domain,response_schema,validate_experiment,SchemaViolation
from selfstruct.research import PROPOSAL_SCHEMA
from selfstruct.research_actions import record_findings,execute
from test_state_on_road import road

def schema(s,p):
    return response_schema(PROPOSAL_SCHEMA,s.keys,{1},['new'],p.get('hypotheses',{}),reference_domain(s.store,p))['properties']['actions']['items']

def test_snapshot_creation_execution_and_saved_states(road):
    s,p,e=road
    old=schema(s,{'hypotheses':{}})
    r={'op':'experiment','hypothesis':'next','action':0,'claim':{'type':'transition','probability':.5,'from_state':{'text':'a','evidence':[e],'phase':'before'},'to_state':{'text':'b','evidence':[e],'phase':'after'}}}
    record_findings(s,'job',p,{'findings':[{'id':'next','text':'new prediction','evidence':[e]}]})
    with pytest.raises(SchemaViolation):validate_experiment(r,1,old)
    current=schema(s,p);validate_experiment(r,1,current)
    assert reference_domain(s.store,p)['states']==[]
    bad=copy.deepcopy(r);bad['claim']['from_state']=e
    with pytest.raises(SchemaViolation):validate_experiment(bad,1,current)
    result=execute(s,'job',p,r,'inline')[0]
    ids=result['registration']['claim']
    assert len(reference_domain(s.store,p)['states'])==2
    r['claim']=ids
    with pytest.raises(SchemaViolation):validate_experiment(r,1,current)
    validate_experiment(r,1,schema(s,p))
    assert execute(s,'job',p,r,'ids')[0]['experiment']

def test_domain_excludes_history_unknown_and_out_of_range(road):
    s,p,e=road
    past=s.store.knowledge('hypothesis','past','inferred','hypothesis',[e])
    p['hypotheses']['missing']=999999
    d=reference_domain(s.store,p)
    assert past not in d['hypotheses'] and 'missing' not in d['hypotheses']
    r={'op':'experiment','hypothesis':'h','action':0,'claim':{'type':'change_rate','probability':.5}}
    for value in ['invented',past,999999]:
        with pytest.raises(SchemaViolation):validate_experiment({**r,'hypothesis':value},1,schema(s,p))
    for value in [-.1,1.1]:
        with pytest.raises(SchemaViolation):validate_experiment({**r,'claim':{'type':'change_rate','probability':value}},1,schema(s,p))
