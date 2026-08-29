; Syntax highlighting for Kedi (tree-sitter-kedi)
;
; Editor consumers: VS Code, Neovim (nvim-treesitter), Helix, Zed,
; tree-sitter highlight CLI. Captures use the standard tree-sitter
; capture names so existing themes pick them up without extra mapping.

; ----------------------------------------------------------------
; Comments
; ----------------------------------------------------------------

(line_comment) @comment.line
(block_comment) @comment.block
(procedure_def
  body: (block
    . (block_comment) @variable))

; ----------------------------------------------------------------
; Operators / punctuation
; ----------------------------------------------------------------

(template_block_stmt
  ">>" @keyword)
(raw_invoke_stmt
  "<<" @keyword)

["@" "~" "<" ">"] @punctuation.special
["=" ":" "(" ")" "[" "]" "," "|" "->" "*"] @punctuation.delimiter
":=" @operator
["```"] @punctuation.special
"`" @punctuation.special

; ----------------------------------------------------------------
; Procedure & validation block keywords
; ----------------------------------------------------------------

(validation_keyword) @keyword
"import" @operator
"export" @operator
"package" @keyword
"auto" @keyword
"optimize" @keyword
"agent" @keyword
"adapter" @keyword
"model" @keyword
"effort" @keyword
"approval" @keyword
"codemode" @keyword
"system" @keyword
"mcp" @keyword
"settings" @keyword
"artifacts" @keyword
"output" @keyword
"subagent" @keyword
"max_agents" @keyword
"workflow" @keyword
"profile" @keyword
"use" @keyword
"case" @keyword
"data" @keyword
"test_data" @keyword
"metric" @keyword
"if" @keyword
"else" @keyword
"loop" @keyword
"map" @keyword

; ----------------------------------------------------------------
; Procedure and type-definition names
; ----------------------------------------------------------------

(procedure_def name: (identifier) @function)
(type_def name: (identifier) @type.definition)
(package_directive name: (identifier) @namespace)
(package_field name: (identifier) @property)
(package_field value: (package_plain_value) @string)
(package_python_dependencies "python_dependencies" @property)
(package_dependency value: (package_dependency_value) @string)
(module_import module: (module_path) @namespace)
(module_import_name name: (identifier) @variable)
(module_export_name name: (identifier) @variable)
(binding_target name: (identifier) @variable)
(assignment_stmt name: (identifier) @variable)
(assignment_block_stmt name: (identifier) @variable)
(loop_stmt binder: (identifier) @variable)
(param name: (identifier) @variable.parameter)
(type_field name: (identifier) @property)

; ----------------------------------------------------------------
; Template segments
; ----------------------------------------------------------------

; Segment captures apply anywhere (template_line, template_block, returns, …).
(input_segment name: (identifier) @variable)
(system_angle_segment) @variable
(call_segment name: (identifier) @function.call)
(output_segment
  name: (identifier) @variable.builtin)
(text_segment) @string
(condition_text_segment) @string

; ----------------------------------------------------------------
; Type expressions
; ----------------------------------------------------------------

(type_ref name: (identifier) @type)
(type_apply name: (identifier) @type)
(type_string) @string

; ----------------------------------------------------------------
; Validation suite names (after `@test:` / `@eval:`)
; ----------------------------------------------------------------

(validation_block procedure: (identifier) @function)
(test_case name: (identifier) @label)
(eval_data name: (identifier) @label)
(eval_test_data name: (identifier) @label)
(eval_metric name: (identifier) @label)
(eval_metric dataset: (identifier) @variable)

(optimize_directive name: (identifier) @label)
(profile_directive name: (identifier) @label)
(subagent_directive name: (identifier) @label)
(max_agents_directive value: (positive_integer) @number)
(workflow_directive
  "workflow" @keyword
  value: (identifier) @constant)
(history_directive
  "history" @keyword
  value: (history_plain_value) @string)
(history_directive "history" @keyword)

(skills_directive
  ">" @operator)
(skills_directive "skills" @keyword)
(skills_plain_value) @constant.builtin
(skills_field
  name: (identifier) @property)
(skills_field value: (settings_plain_value) @string)
(skills_field value: (inline_python_expr) @string.special)
(codemode_directive
  ">" @operator)
(codemode_directive "codemode" @keyword)
(codemode_plain_value) @constant.builtin
(codemode_field
  name: (identifier) @property)
(codemode_field value: (settings_plain_value) @string)
(codemode_field value: (inline_python_expr) @string.special)
(history_field name: (identifier) @property)
(history_field value: (settings_plain_value) @string)
(history_field value: (inline_python_expr) @string.special)
(agent_directive
  value: (adapter_plain_value) @label)
(agent_field name: (identifier) @label)
(agent_field
  value: (agent_command_plain_value) @string)
(agent_field
  value: (inline_python_expr) @string.special)
(adapter_directive
  value: (adapter_plain_value) @label)
(use_directive name: (identifier) @label)
(use_tool_name name: (identifier) @function.call)
(use_tool_backtick name: (identifier) @function.call)
(mcp_field name: (identifier) @property)
(settings_field name: (identifier) @property)
(artifacts_field name: (identifier) @property)

(model_directive
  value: (model_plain_value) @string)
(model_directive
  value: (inline_python_expr) @string.special)

(agent_directive
  value: (inline_python_expr) @string.special)

(adapter_directive
  value: (inline_python_expr) @string.special)

(effort_directive
  value: (effort_plain_value) @string)
(effort_directive
  value: (inline_python_expr) @string.special)

(approval_directive
  value: (approval_plain_value) @string)
(approval_directive
  value: (inline_python_expr) @string.special)

(mcp_field
  value: (mcp_plain_value) @string)
(mcp_field
  value: (inline_python_expr) @string.special)

(settings_field
  value: (settings_plain_value) @string)
(settings_field
  value: (inline_python_expr) @string.special)

(artifacts_field
  value: (artifacts_plain_value) @string)
(artifacts_field
  value: (inline_python_expr) @string.special)

; ----------------------------------------------------------------
; Python source embedded in Kedi (these regions get a Python
; injection — see queries/injections.scm — but we also give them a
; fallback "code" style so they remain readable when no injection
; parser is available).
; ----------------------------------------------------------------

(python_code) @string.special
(python_inline_body) @string.special
