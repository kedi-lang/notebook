; Language injections for Kedi.
;
; Every region of embedded Python source becomes a "language
; injection" so editor consumers can apply Python syntax highlighting
; (and LSP servers can route hover / go-to-def into Python tools)
; without any extra glue.
;
; Injection sites:
;   - `python_code`  — the body of a triple-backtick fenced block
;                       (top-level python_block, variable_init_block_stmt
;                       RHS, return_block_stmt value).
;   - `python_inline_body` — between single backticks for
;                       inline_python_expr (and the body of a
;                       backtick-wrapped `type_python` type
;                       annotation, which is also Python code).

((python_code) @injection.content
 (#set! injection.language "python")
 (#set! injection.include-children))

((python_inline_body) @injection.content
 (#set! injection.language "python")
 (#set! injection.include-children))
