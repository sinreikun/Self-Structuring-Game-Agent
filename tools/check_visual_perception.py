"""Directed native-image capability probe, never autonomous-learning validation."""
import argparse
import base64
import hashlib
import json
from pathlib import Path

from selfstruct.config import write_json
from selfstruct.research import LocalLLM
from selfstruct.resources import ManagedLLM

QUESTION='画面に実際に表示されている指示文と、図形同士の位置関係を短く説明してください。見えないこと・操作結果は補完せずunknown。これは視覚入力単独の検証で、操作や道具選択の検証ではありません。'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model-root',type=Path,required=True)
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--image',type=Path,required=True)
    p.add_argument('--report',type=Path,required=True)
    args=p.parse_args()
    state=json.loads(args.state.read_text())
    manager=ManagedLLM(args.model_root,24*1024**3)
    try:
        manager.start(state)
        client=LocalLLM('http://127.0.0.1:8766/v1','local');client.health()
        if not client.vision:raise ValueError('Runtime does not advertise image input')
        raw=args.image.read_bytes()
        response=client.complete(QUESTION,max_tokens=256,
            images=['data:image/png;base64,'+base64.b64encode(raw).decode()],on_progress=lambda text:None)
        result={'status':'completed_requires_visual_review','capabilities':client.capabilities,
                'model_sha256':state['model_sha256'],'projector_sha256':state.get('projector_sha256'),
                'image_sha256':hashlib.sha256(raw).hexdigest(),'prompt':QUESTION,'response':response,
                'completion':client.last_completion,'directed':True,'autonomous_choice_verified':False,
                'game_goal_achieved':False}
        write_json(args.report,result);print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:manager.stop()


if __name__=='__main__':main()
