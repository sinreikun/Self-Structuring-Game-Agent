(module
  ;; The seed has no names or rules referring to any particular game.
  (memory (export "memory") 1 256)
  ;; Persistent internal state lives in memory so Host can checkpoint it.
  (data (i32.const 2048) "\49\3a\02\00")
  (func (export "choose") (param $feature i32) (param $keys i32) (result i32)
    (local $next i32)
    (local.set $next (i32.add (i32.mul (i32.load (i32.const 2048)) (i32.const 1664525)) (i32.const 1013904223)))
    (i32.store (i32.const 2048) (local.get $next))
    (i32.rem_u (local.get $next) (local.get $keys)))
  (func (export "duration") (param $feature i32) (result i32) (i32.const 60))
  (func (export "wait") (param $feature i32) (result i32) (i32.const 80))
  ;; -1 is explicitly unknown, otherwise 0..1000 is probability of change.
  (func (export "predict") (param $feature i32) (param $action i32) (result i32) (i32.const -1))
  (func (export "learn") (param $before i32) (param $action i32) (param $after i32) (param $changed i32))
)
