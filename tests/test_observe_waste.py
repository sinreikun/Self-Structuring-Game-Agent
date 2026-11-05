import io,json,time
import pytest
from PIL import Image
from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.smoke import SyntheticGame
from selfstruct.research_actions import run_actions,action_budget_used

@pytest.fixture
def context(tmp_path):
 c=Controller(Config(root=str(tmp_path),keys=['A'],research_every=100000),SyntheticGame())
 c.step();c.scientist.client.vision=True
 p={'research_rows':c.store.rows(),'research_calls':1,'research_started':time.time()}
 j=c.store.job('research',p)
 yield c,j,p
 c.close()

@pytest.mark.parametrize('size',[[1600,900],[1024,768],'native'])
def test_oversize_success_and_resize_receipt(context,size):
 c,j,p=context
 if size=='native':
  c.visual.eyes.current=lambda:Image.new('RGB',(1600,900),'blue')
 req={'op':'observe','size':size,'mode':'RGB','output':'image'}
 if size!='native':req['event']=c.last_eid
 run_actions(c.scientist,j,p,[req])
 r=p['action_history'][-1]
 assert r['state']=='done',r
 result=r['result'];w,h=result['size']
 assert w*h<=262144 and result['rounded'] and result['requested_size']==size
 assert result['delivered_size']==[w,h]
 assert action_budget_used(p)==1 and len(p['visual_history'])==1
 _,attachment=c.visual.handle(p['visual_history'][0]['request'],native=True)
 import base64
 assert Image.open(io.BytesIO(base64.b64decode(attachment.split(',')[1]))).size==(w,h)


def test_duplicate_free_reference_and_changed_current(context):
 c,j,p=context
 frame=Image.new('RGB',(80,60),'blue');c.visual.eyes.current=lambda:frame
 req={'op':'observe','size':[64,32],'output':'image'}
 run_actions(c.scientist,j,p,[req,req])
 a,b=p['action_history'];assert a['state']=='done' and b['state']=='duplicate'
 assert b['result']['unchanged'] and b['result']['original_action_id']==a['action_id']
 assert b['result']['image_bytes']==0 and not b['result']['action_charged']
 assert len(p['visual_history'])==1 and action_budget_used(p)==1
 assert p.get('image_bytes',0)==0
 frame.putpixel((0,0),(255,0,0));p['research_calls']=2
 run_actions(c.scientist,j,p,[req])
 assert p['action_history'][-1]['state']=='done' and action_budget_used(p)==2
 assert len(p['visual_history'])==2


def test_normal_content_conditions_and_billing(context):
 c,j,p=context
 req={'op':'observe','event':c.last_eid,'size':[64,32],'mode':'RGB','output':'statistics'}
 expected,_=c.visual.handle(req,allowed={c.last_eid})
 run_actions(c.scientist,j,p,[req,{**req,'size':[32,32]}])
 result=p['action_history'][0]['result']
 for key in ('size','mode','mean','stddev','extrema','evidence','box','coordinates'):assert result[key]==expected[key]
 assert 'rounded' not in result and action_budget_used(p)==2
 assert all(r['state']=='done' for r in p['action_history'])
