"""Immutable snapshots shared by Host/Scientist and the UI; no Agent capability."""
from dataclasses import dataclass, replace
import threading
import time

from .presentation import event_message


@dataclass(frozen=True)
class SemanticEvent:
    at: float
    kind: str
    text: str


@dataclass(frozen=True)
class TraceEvent:
    sequence: int
    at: float
    kind: str
    text: str


@dataclass(frozen=True)
class RealtimeActivity:
    phase: str = '停止中'
    purpose: str = '画面と入力の関係を観測'
    llm: str = '待機'
    experiment: str = '未確認の関係を調べます'
    action: str = 'まだ入力していません'
    duration: float = 0
    change: float = 0
    prediction: float = -1
    next: str = '次の観測を待っています'
    revision: str = ''
    candidate: str = ''
    comparison: str = '待機'
    connection: str = '未接続'
    recovery: str = ''
    last_observed: float = 0
    attention: tuple = ()
    events: tuple[SemanticEvent, ...] = ()
    trace: tuple[TraceEvent, ...] = ()
    adoption: str = ''
    adoption_at: float = 0


class Activity:
    def __init__(self):
        self._lock = threading.Lock()
        self._value = RealtimeActivity()
        self._sequence = 0

    def snapshot(self) -> RealtimeActivity:
        with self._lock: return self._value

    def update(self, **changes):
        with self._lock: self._value = replace(self._value, **changes)

    def record(self,kind: str,text: str):
        with self._lock:
            self._sequence+=1
            entry=TraceEvent(self._sequence,time.time(),kind,str(text)[:160])
            self._value=replace(self._value,trace=(*self._value.trace[-199:],entry))

    def emit(self, kind: str, **data):
        prefix={'llm_start':'LLM','llm_timeout':'LLM','llm_error':'LLM','llm_saved':'LLM','replay':'EVAL','comparison':'EVAL','rejected':'REJECT','adopted':'PROMOTE','recovery':'RECOVER'}.get(kind)
        if prefix:self.record(prefix,kind)
        text = event_message(kind, data)
        with self._lock:
            event = SemanticEvent(time.time(),kind,text)
            changes={'events': (*self._value.events[-7:],event)}
            if kind == 'adopted': changes.update(adoption=text,adoption_at=time.time())
            self._value=replace(self._value,**changes)
