# Brief: targeted feature end-to-end test

Read AGENT_BRIEF.md first: its tools, rules and final report files all apply.
Only the goal changes: keep your persona's device, language, text size, theme,
patience, and honest judgment, but follow the **Targeted Goal** provided in your
prompt instead of the default persona goal.

1. Stay in scope for the targeted flow: decline unrelated telemetry, crash-report,
   or account-sync prompts unless they are explicitly part of the targeted goal.
2. Execute the full end-to-end flow step by step, taking a screenshot after every
   action (`qa_observe` / `qa_act(..., observe_after=true)`).
3. Verify edge cases around the targeted feature: empty states, invalid input,
   navigation back and forth, and at least one setting or preference relevant to
   your persona (remember to leave and re-open the affected screen before reporting
   that a setting did nothing).
4. Record every bug, layout flaw, accessibility issue, and confusing moment using
   the standard `report_md` and `findings_json` format (`score` and `issues`) via
   `qa_record_findings`.
