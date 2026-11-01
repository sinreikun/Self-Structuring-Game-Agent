"""Typed research wire format shared by constrained generation and Host validation."""
from copy import deepcopy
import math


def integer(low=0,high=2**53-1):return {'type':'integer','minimum':low,'maximum':high}
def string(limit=1000):return {'type':'string','maxLength':limit}
def array(items,low=0,high=64):return {'type':'array','items':items,'minItems':low,'maxItems':high}
def obj(properties,required=()):return {'type':'object','additionalProperties':False,'properties':properties,'required':list(required)}

BOX=array(integer(0,32768),4,4)
SIZE={'anyOf':[array(integer(1,262144),2,2),{'type':'string','enum':['native']}]}
OPERATIONS=array(obj({'op':{'type':'string','enum':['grayscale','threshold','blur','edge','invert']},'value':integer(0,255),'radius':integer(0,8)},['op']),0,8)
REFERENCE={'anyOf':[obj({'event':integer(1),'phase':{'type':'string','enum':['before','after','diff']},'box':BOX},['event']),obj({'observation':integer(1),'phase':{'type':'string','enum':['before']},'box':BOX},['observation']),obj({'phase':{'type':'string','enum':['before']},'box':BOX})]}
QUERY={'event':integer(1),'observation':integer(1),'phase':{'type':'string','enum':['before','after','diff']},'box':BOX,'size':SIZE,
       'mode':{'type':'string','enum':['L','RGB']},'output':{'type':'string','enum':['pixels','pixel','statistics','histogram','components','image']},
       'offset':integer(),'limit':integer(1,2048),'point':array(integer(),2,2),'fresh':{'type':'boolean'},'compare':REFERENCE,'operations':OPERATIONS}
HYPOTHESIS={'anyOf':[integer(1),{**string(40),'minLength':1}]}
CONDITION_FIELDS={**{k:QUERY[k] for k in ('box','size','mode','operations')},'mean_min':{'type':'number','minimum':0,'maximum':255},'mean_max':{'type':'number','minimum':0,'maximum':255},'max_wait_steps':integer(1,128)}
CONDITION={'anyOf':[obj(CONDITION_FIELDS,['mean_min']),obj(CONDITION_FIELDS,['mean_max'])]}
ACTION_FIELDS={
 'observe':(QUERY,[]),'compare':(QUERY,['compare']),'analyze':(QUERY,[]),
 'experiment':({'hypothesis':HYPOTHESIS,'action':integer(),'duration':integer(10,500),'wait':integer(10,2000),'repeat':integer(1,8),'condition':CONDITION},['action','hypothesis']),
 'evaluate':({'hypothesis':HYPOTHESIS,'change_probability':{'type':'number','minimum':0,'maximum':1}},['hypothesis','change_probability']),
 'candidate':({'problem':string(),'evidence':array(integer(1),2,64),'behavior':string(),'evaluation':string()},['problem','evidence','behavior','evaluation']),
 'analyzer_create':({'definition':obj({'description':string(),'query':obj({k:v for k,v in QUERY.items() if k not in ('event','observation','phase','fresh','compare')})},['description','query'])},['definition']),
 'analyzer_run':({**QUERY,'id':string(128),'experimental':{'type':'boolean'}},['id']),
 'analyzer_evaluate':({'id':string(128),'train':array(integer(1),8,64),'test':array(integer(1),8,64)},['id','train','test']),
 'analyzer_list':({'cursor':integer()},[]),'analyzer_get':({'id':string(128)},['id']),'analyzer_retire':({'id':string(128)},['id']),
 'evidence_keep':({'observation':integer(1),'reason':string(240)},['observation','reason']),
 'library_request':({'name':string(500),'purpose':string(500),'reason':string(500)},['name','purpose','reason'])}


def action_schema(key_count=None,legacy=False):
    variants=[]
    for op,(properties,required) in ACTION_FIELDS.items():
        properties=deepcopy(properties)
        if op=='experiment' and key_count is not None:properties['action']['maximum']=key_count-1
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


def response_schema(base,keys,allowed):
    schema=deepcopy(base)
    schema['properties']['actions']['items']=action_schema(len(keys))
    schema['properties']['visual_requests']['items']=action_schema(len(keys),legacy=True)
    schema['properties']['findings']['items']['required']=['id','text','evidence']
    finding=schema['properties']['findings']['items']['properties']
    finding['id']['minLength']=1
    finding['action']['maximum']=len(keys)-1
    if allowed:finding['evidence']['items']={'type':'integer','enum':sorted(allowed)}
    else:schema['properties']['findings']['maxItems']=0
    return schema


def repair_kind(payload):
    if payload.get('repair_kind') in ('protocol','implementation'):return payload['repair_kind']
    if payload.get('source') or payload.get('implementation_plan') or payload.get('candidate_sha256'):return 'implementation'
    return 'protocol'
