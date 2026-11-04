"""Typed research wire format shared by constrained generation and Host validation."""
from copy import deepcopy
import math


def integer(low=0,high=2**53-1):return {'type':'integer','minimum':low,'maximum':high}
def string(limit=1000):return {'type':'string','maxLength':limit}
def array(items,low=0,high=64):return {'type':'array','items':items,'minItems':low,'maxItems':high}
def obj(properties,required=()):return {'type':'object','additionalProperties':False,'properties':properties,'required':list(required)}

BOX=array(integer(0,32768),4,4)
SIZE={'anyOf':[array(integer(1,262144),2,2),{'type':'string','enum':['native','fit']}]}
OPERATIONS=array(obj({'op':{'type':'string','enum':['grayscale','threshold','blur','edge','invert']},'value':integer(0,255),'radius':integer(0,8)},['op']),0,8)
REFERENCE={'anyOf':[obj({'event':integer(1),'phase':{'type':'string','enum':['before','after','diff']},'box':BOX},['event']),obj({'observation':integer(1),'phase':{'type':'string','enum':['before']},'box':BOX},['observation']),obj({'phase':{'type':'string','enum':['before']},'box':BOX})]}
QUERY={'event':integer(1),'observation':integer(1),'phase':{'type':'string','enum':['before','after','diff']},'box':BOX,'size':SIZE,
       'mode':{'type':'string','enum':['L','RGB']},'output':{'type':'string','enum':['pixels','pixel','statistics','histogram','components','image']},
       'offset':integer(),'limit':integer(1,2048),'point':array(integer(),2,2),'fresh':{'type':'boolean'},'resend':{'type':'boolean'},'compare':REFERENCE,'operations':OPERATIONS}
HYPOTHESIS={'anyOf':[integer(1),{**string(40),'minLength':1}]}
CONDITION_FIELDS={**{k:QUERY[k] for k in ('box','size','mode','operations')},'mean_min':{'type':'number','minimum':0,'maximum':255},'mean_max':{'type':'number','minimum':0,'maximum':255},'max_wait_steps':integer(1,128)}
CONDITION={'anyOf':[obj(CONDITION_FIELDS,['mean_min']),obj(CONDITION_FIELDS,['mean_max'])]}
ACTION_FIELDS={
 'research_report':({'title':{**string(120),'minLength':1},'target_goal':string(600),'goal_status':{'type':'string','enum':['unknown','hypothesis','observed']},'goal_evidence':array(integer(1),0,16),'findings':array(obj({'text':string(600),'status':{'type':'string','enum':['observation','hypothesis']},'evidence':array(integer(1),1,16)},['text','status','evidence']),0,8),'unknowns':array(string(300),0,8),'next_steps':array(string(300),0,8)},['title','target_goal','goal_status','goal_evidence','findings','unknowns','next_steps']),
 'memory_search':({'collection':{'type':'string','enum':['experience','knowledge','experiment','visual','research','note','report']},'query':string(160),'ref':string(128),'cursor':integer(),'watermark':integer(),'limit':integer(1,8)},['collection']),
 'memory_read':({'collection':{'type':'string','enum':['experience','knowledge','experiment','visual','research','note','report']},'ref':string(128),'revision':string(64),'offset':integer(),'limit':integer(1,1600)},['collection','ref','revision']),
 'memory_note':({'ref':string(128),'revision':string(64),'title':{**string(120),'minLength':1},'body':{**string(2000),'minLength':1},'evidence':array(integer(1),0,16)},['title','body']),
 'observe':(QUERY,[]),'compare':(QUERY,['compare']),'analyze':(QUERY,[]),
 'experiment':({'hypothesis':HYPOTHESIS,'action':integer(),'duration':integer(10,500),'wait':integer(10,2000),'repeat':integer(1,8),'condition':CONDITION},['action']),
 'evaluate':({'hypothesis':HYPOTHESIS,'change_probability':{'type':'number','minimum':0,'maximum':1}},['hypothesis']),
 'candidate':({'problem':string(),'evidence':array(integer(1),2,64),'behavior':string(),'evaluation':string()},['problem','evidence','behavior','evaluation']),
 'analyzer_create':({'definition':obj({'description':string(),'query':obj({k:v for k,v in QUERY.items() if k not in ('event','observation','phase','fresh','compare')})},['description','query'])},['definition']),
 'analyzer_run':({**QUERY,'id':string(128),'experimental':{'type':'boolean'}},['id']),
 'analyzer_evaluate':({'id':string(128),'train':array(integer(1),8,64),'test':array(integer(1),8,64)},['id','train','test']),
 'analyzer_list':({'cursor':integer()},[]),'analyzer_get':({'id':string(128)},['id']),'analyzer_retire':({'id':string(128)},['id']),
 'evidence_keep':({'observation':integer(1),'reason':string(240)},['observation','reason']),
 'library_request':({'name':string(500),'purpose':string(500),'reason':string(500)},['name','purpose','reason'])}
# Procedure contents are supplied by the model, never completed by Host.
STATE_CHOICE={'anyOf':[integer(1),obj({'state':{'type':'boolean','enum':[True]},'text':{**string(1000),'minLength':1},'evidence':array(integer(1),1,16),'phase':{'type':'string','enum':['before','after']}},['state','text','evidence','phase'])]}
INLINE_STATE={'anyOf':[integer(1),obj({'text':{**string(1000),'minLength':1},'evidence':array(integer(1),1,16),'phase':{'type':'string','enum':['before','after']}},['text','evidence','phase'])]}
CLAIM_FIELDS={'type':{'type':'string','enum':['transition','change_rate']},'probability':{'type':'number','minimum':0,'maximum':1},'from_state':INLINE_STATE,'to_state':INLINE_STATE}
# Disjoint branches compile to grammar constraints, including conditional required fields.
EXPERIMENT_CLAIM={'description':'Model preregistration saved before input. States resolve to existing IDs or inline model definitions; receipt returns IDs. Repeat the same claim for reuse.', 'anyOf':[
    obj({**CLAIM_FIELDS,'type':{'type':'string','enum':['transition']}},['type','probability','from_state','to_state']),
    obj({k:v for k,v in CLAIM_FIELDS.items() if k not in ('from_state','to_state')} | {'type':{'type':'string','enum':['change_rate']}},['type','probability'])]}
ACTION_FIELDS['experiment'][0]['claim']=EXPERIMENT_CLAIM
ACTION_FIELDS['experiment'][0]['hypothesis']={**HYPOTHESIS,'description':'Existing hypothesis knowledge ID or model alias created in this Job, available at call start; create missing hypotheses in findings, then reference next call. Host never infers references.'}

class SchemaViolation(ValueError):
    """Generated action violates the grammar contract, distinct from value rejection."""

def validate_experiment(request,key_count,schema=None):
    if isinstance(request,dict) and request.get('op')=='experiment':
        try:validate(schema if schema is not None else action_schema(key_count),request)
        except ValueError as error:raise SchemaViolation('schema_violation: '+str(error)) from error

ACTION_FIELDS['registration']=({'route':string(80),'decision':{'type':'string','enum':['complete','withdraw']},'claim_type':{'type':'string','enum':['change_rate','transition']},'probability':{'type':'number','minimum':0,'maximum':1},'from_state':STATE_CHOICE,'to_state':STATE_CHOICE},['route','decision'])



def action_schema(key_count=None,legacy=False,experiment_required=True):
    variants=[]
    for op,(properties,required) in ACTION_FIELDS.items():
        properties=deepcopy(properties)
        if op=='experiment' and key_count is not None:properties['action']['maximum']=key_count-1
        if op=='experiment' and experiment_required:required=[*required,'hypothesis','claim']
        properties={'op':{'type':'string','enum':[op]},**properties}
        required=(['op'] if not legacy or op!='observe' else [])+required
        if op in ('observe','compare','analyze','analyzer_run'):
            event={k:v for k,v in properties.items() if k!='observation'};event['fresh']={'type':'boolean','enum':[False]}
            observation={k:v for k,v in properties.items() if k!='event'};observation['phase']={'type':'string','enum':['before']};observation['fresh']={'type':'boolean','enum':[False]}
            current={k:v for k,v in properties.items() if k not in ('event','observation')};current['phase']={'type':'string','enum':['before']};current['fresh']={'type':'boolean','enum':[False]}
            fresh={k:v for k,v in current.items()};fresh['fresh']={'type':'boolean','enum':[True]}
            variants.extend([obj(event,[*required,'event']),obj(observation,[*required,'observation']),obj(current,required),obj(fresh,[*required,'fresh'])])
        else:variants.append(obj(properties,required))
    return {'anyOf':variants}


def validate(schema,value,path='action'):
    """Validate the small schema subset above without adding a runtime dependency."""
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try:validate(option,value,path);return
            except ValueError:pass
        raise ValueError(path+': expected one of the typed action variants; use separate JSON fields, not a command string in op')
    kind=schema.get('type')
    valid={'object':isinstance(value,dict),'array':isinstance(value,list),'string':isinstance(value,str),
           'integer':type(value)==int,'number':type(value) in (int,float) and math.isfinite(value),'boolean':type(value)==bool}
    if kind and not valid[kind]:raise ValueError(path+': expected '+kind)
    if 'enum' in schema and value not in schema['enum']:raise ValueError(path+': expected '+str(schema['enum']))
    if kind=='object':
        if any(k not in value for k in schema.get('required',[])):raise ValueError(path+': missing required field')
        fields=schema.get('properties',{})
        if schema.get('additionalProperties') is False and set(value)-set(fields):raise ValueError(path+': unknown fields '+str(sorted(set(value)-set(fields))))
        for k,v in value.items():
            if k in fields:validate(fields[k],v,path+'.'+k)
    elif kind=='array':
        if not schema.get('minItems',0)<=len(value)<=schema.get('maxItems',2**31):raise ValueError(path+': array size out of range')
        for item in value:validate(schema['items'],item,path+'[]')
    elif kind=='string':
        if not schema.get('minLength',0)<=len(value)<=schema.get('maxLength',2**31):raise ValueError(path+': string too long')
    elif kind in ('number','integer'):
        if not schema.get('minimum',-math.inf)<=value<=schema.get('maximum',math.inf):raise ValueError(path+': number out of range')


def response_schema(base,keys,allowed,new_ids=None,aliases=None,domain=None):
    schema=deepcopy(base)
    schema['properties']['actions']['items']=action_schema(len(keys))
    schema['properties']['visual_requests']['items']=action_schema(len(keys),legacy=True)
    schema['properties']['findings']['items']['required']=['id','text','evidence']
    finding=schema['properties']['findings']['items']['properties']
    finding['id']['minLength']=1
    finding['id']['description']='Create missing references here first. Experiments can use only the call-start catalog; new findings become referencable next call.'
    if new_ids is not None:
        finding['id']['enum']=new_ids
        references={'anyOf':[{'type':'string','enum':list(dict.fromkeys([*(aliases or {}),*new_ids]))}]}
        if aliases:references['anyOf'].append({'type':'integer','enum':sorted(set(aliases.values()))})
        for name in ('actions','visual_requests'):
            for variant in schema['properties'][name]['items']['anyOf']:
                if 'hypothesis' in variant['properties'] and variant['properties']['op']['enum']!=['experiment']:variant['properties']['hypothesis']=deepcopy(references)
    if domain is not None:
        for name in ('actions','visual_requests'):
            variants=[]
            for variant in schema['properties'][name]['items']['anyOf']:
                fields=variant['properties']
                if fields['op']['enum']==['experiment']:
                    if not domain['hypotheses']:continue
                    fields['hypothesis']={'anyOf':[{'type':kind,'enum':[v for v in domain['hypotheses'] if type(v)==typ]} for kind,typ in [('string',str),('integer',int)] if any(type(v)==typ for v in domain['hypotheses'])], 'description':'Snapshot at call start. Missing references must be created in findings first and used next call.'}
                    for branch in fields['claim']['anyOf']:
                        for field in ('from_state','to_state'):
                            if field in branch['properties']:
                                choices=[deepcopy(INLINE_STATE['anyOf'][1])]
                                if domain['states']:choices.insert(0,{'type':'integer','enum':domain['states']})
                                branch['properties'][field]={'anyOf':choices}
                variants.append(variant)
            schema['properties'][name]['items']['anyOf']=variants
    # A valid database row is not necessarily a reference presented to this Job.
    # Historical references remain available through explicit memory retrieval.
    events=sorted(allowed)
    for name in ('actions','visual_requests'):
        bound=[]
        for variant in schema['properties'][name]['items']['anyOf']:
            fields=variant['properties']
            if 'event' in fields:
                if not events:continue
                fields['event']={**fields['event'],'enum':events}
            if 'compare' in fields:
                refs=[]
                for ref in fields['compare']['anyOf']:
                    props=ref['properties']
                    if 'event' in props:
                        if not events:continue
                        props['event']={**props['event'],'enum':events}
                    refs.append(ref)
                fields['compare']['anyOf']=refs
            bound.append(variant)
        schema['properties'][name]['items']['anyOf']=bound
    finding['action']['maximum']=len(keys)-1
    if 'transition' in finding:finding['transition']['properties']['action']['maximum']=len(keys)-1
    if allowed:finding['evidence']['items']={'type':'integer','enum':sorted(allowed)}
    else:schema['properties']['findings']['maxItems']=0
    return schema


def repair_kind(payload):
    if payload.get('repair_kind') in ('protocol','implementation'):return payload['repair_kind']
    if payload.get('source') or payload.get('implementation_plan') or payload.get('candidate_sha256'):return 'implementation'
    return 'protocol'


def reference_domain(store,payload):
    """Only hypotheses bound by model findings in this Job, plus model saved states with frames."""
    with store.lock:
        valid={r[0] for r in store.db.execute("SELECT id FROM knowledge WHERE kind='hypothesis' AND provenance='inferred'")}
        aliases={k:v for k,v in payload.get('hypotheses',{}).items() if v in valid}
        states=[r[0] for r in store.db.execute("SELECT DISTINCT k.id FROM knowledge k JOIN state_frames s ON s.state=k.id WHERE k.kind='state' AND k.provenance='self_generated_model' ORDER BY k.id")]
    return {'hypotheses':[*sorted(aliases),*sorted(set(aliases.values()))],'states':states}
