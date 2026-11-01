"""The blind evaluator must not confuse transport, random wins, or semantics."""
from tools.check_autonomous_research import BlindEnvironment, assess
import threading


def test_goal_without_self_selected_research_cannot_pass():
    result=assess({},True,[],[])
    assert result['status']=='failed' and result['first_unmet']=='observation_requested'
    assert not result['semantic_understanding_verified']


def test_statistics_are_not_spatial_screen_understanding():
    p={'action_history':[{'state':'done','request':{'op':'observe'}}],
       'research_steps':[{'delivered_visual_evidence':[{'payload_fields':['mean'],'image_attached':False}]}]}
    result=assess(p,False,[],[])
    assert result['gates']['observation_requested']
    assert not result['gates']['spatial_evidence_delivered']
    assert result['first_unmet']=='spatial_evidence_delivered'


def test_even_all_behavioral_gates_require_independent_semantic_review():
    p={'action_history':[{'state':'done','request':{'op':'observe'}},
                         {'state':'done','step':2,'action_id':'2:0','request':{'op':'experiment'},'result':{'completed':1}}],
       'research_steps':[{'step':3,'consumed_actions':['2:0'],
                          'delivered_visual_evidence':[{'payload_fields':['pixels']}]}]}
    r=assess(p,True,[(3,{'findings':[{'text':'This explanation may be wrong.'}]})],[{'title':'unverified'}])
    assert all(r['gates'].values())
    assert r['status']=='behavioral_candidate_requires_semantic_review'
    assert not r['semantic_understanding_verified']
    assert assess(p,True,[(3,{'findings':[{}]})],[{}],error='timeout')['status']=='failed'


def test_hidden_task_has_visible_instruction_and_real_input_dependent_outcome():
    game=BlindEnvironment(271828);game.start()
    before=game.capture().tobytes()
    key='A' if (game.target-game.position)*game.direction>0 else 'B'
    sent=[]
    for _ in range(abs(game.target-game.position)):
        game.act(key,.01,threading.Event(),sent.append)
    assert game.won and len(sent)==4 and game.capture().tobytes()!=before
    assert all(t['key']==key for t in game.transitions)
