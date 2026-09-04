# Per-machine interaction policy

GPT Controller supports different UI policies on different enrolled PCs.

## Protected machine

Use this for a Desktop that the user actively operates:

```json
"interaction_policy": {
  "non_interference": true,
  "allow_physical_input": false,
  "allow_foreground_activation": false,
  "allow_visible_gui_launch": false
},
"browser": {
  "headless_only": true
}
```

This mode keeps the original non-interference contract: no physical mouse/keyboard takeover, no forced foreground activation, and no arbitrary visible GUI launch.

## Dedicated human-operated worker machine

Use this only on a machine where ChatGPT is allowed to behave like the person sitting at the computer:

```json
"interaction_policy": {
  "non_interference": false,
  "allow_physical_input": true,
  "allow_foreground_activation": true,
  "allow_visible_gui_launch": true
},
"browser": {
  "headless_only": false
}
```

The local config is authoritative. Actions cannot elevate permissions.

Agent 1.1 adds a broad `windows.ui` surface for this dedicated mode. In addition to accessibility-pattern operations, an exact-targeted dedicated machine can:

- enumerate visible windows and inspect the active window;
- dump a bounded UI Automation tree for semantic screen inspection;
- launch, focus, maximize, minimize, restore, close, move, and resize applications;
- click controls or arbitrary screen coordinates;
- move, double-click, right-click, scroll, and drag the physical mouse;
- send hotkeys, key sequences, and Unicode text;
- read and write text clipboard content;
- wait/sleep between visual steps;
- capture a selected window or the desktop to `C:/GPT-Controller/scratch/ui-captures`.

Intrusive `windows.ui` operations and `desktop=true` require `target.mode="agent"`. They cannot use `any` or ordered fallback, which prevents a laptop takeover sequence from falling through to a protected Desktop.

The repository includes `examples/agent.config.dedicated-laptop.example.json` as a dedicated-worker example.
