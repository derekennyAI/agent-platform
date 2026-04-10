---
name: shell
description: "Execute shell scripts from the skill's user-scripts/ directory. Works in sandbox. Use when the user asks to run a script, automate a task, process files, or when you need to run a multi-step shell workflow. Triggered by: 'run this script', 'execute', 'automate this', 'write a script that', 'bash script', 'shell script'. Scripts must live in skills/shell/user-scripts/ — no arbitrary inline commands."
---

# Shell Script Skill

Run shell scripts from `skills/shell/user-scripts/` via a constrained runner. Works in both sandbox and real workspace. No arbitrary inline commands — file-based only.

## How to use

### 1. Write a script

Save it to `skills/shell/user-scripts/<name>.sh` (relative to workspace root):

```bash
#!/usr/bin/env bash
echo "Hello from Derek"
```

### 2. Make it executable

```bash
chmod +x skills/shell/user-scripts/<name>.sh
```

### 3. Run it

```bash
bash skills/shell/scripts/run.sh <name>.sh [args...]
```

## Security constraints

- **Path confinement**: Only scripts inside `skills/shell/user-scripts/` can run. Symlinks are resolved — no escaping.
- **No inline commands**: The runner takes a filename, not a command string. No `-c`, no eval.
- **Executable bit required**: Scripts must be `chmod +x` before they'll run.
- **60-second timeout**: Runaway scripts are killed automatically.
- **Audit log**: Every execution is logged to `skills/shell/audit.log` with timestamp, script name, and exit code.
- **Path-relative**: Uses paths relative to the skill directory.

## Passing arguments

Arguments after the script name are forwarded:

```bash
bash skills/shell/scripts/run.sh backup.sh /tmp/output
```

Inside `backup.sh`, `$1` would be `/tmp/output`.

## Available runtimes

Scripts can use any shebang:
- `#!/usr/bin/env bash`
- `#!/usr/bin/env python3`
- `#!/usr/bin/env node`

The runner just needs the file to be executable — the shebang determines the interpreter.

## Workflow

1. Write script to `skills/shell/user-scripts/`
2. `chmod +x` it
3. Run via `bash skills/shell/scripts/run.sh <name>.sh`
4. Check output / fix errors
5. Commit the script to git if it's worth keeping
