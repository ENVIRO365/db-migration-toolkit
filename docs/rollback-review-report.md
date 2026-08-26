# Rollback Mechanism Review Report

**Date:** 2026-08-26  
**Reviewer:** Agent (automated)  
**File Under Review:** `~/.agents/lib/rollback.sh` (182 lines)  
**Status:** 52 snapshots active, 55 history entries

---

## Executive Summary

The rollback system provides file-level snapshot/restore capability for agent actions. It handles the happy path (backup file → modify → restore) correctly for simple cases. However, **3 critical issues** make it unreliable in production: broken JSON serialization, permission-denied failures on restore, and silent data loss with symlinks. Additionally, **3 high-severity correctness bugs** affect concurrent and multi-undo scenarios.

**Verdict:** Not safe for unattended autonomous operation without fixes to issues #1, #2, and #4.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│  Agent Action (modify file)                     │
│    1. snapshot → save current state             │
│    2. execute  → perform modification           │
│    3. verify   → check result                   │
│    4. on fail  → restore from snapshot          │
└─────────────────────────────────────────────────┘

Storage: ~/.agents/.rollback/
├── history.jsonl          ← append-only log (all actions)
└── snapshots/
    └── <12-char-id>/
        ├── meta.json      ← action metadata
        ├── .type          ← "backup" | "backup_dir" | "create_new"
        └── <filename>     ← backed-up file content
```

---

## Issues Found

### Critical (Snapshot Becomes Unrestorable)

#### Issue #1: No JSON Escaping in meta.json or history.jsonl

**Location:** Lines 43–54 (meta.json heredoc), Line 57 (history.jsonl append)

**Problem:** All user-supplied strings (`$action`, `$desc`, `$file`) are interpolated directly into JSON via heredoc/echo without escaping. Any value containing `"`, `\`, newlines, or certain `$` sequences produces invalid JSON.

**Reproduction:**
```bash
rollback.sh snapshot /tmp/file "action" 'desc with "quotes"'
# meta.json contains: "description": "desc with "quotes""
# → JSONDecodeError on restore attempt
```

**Impact:** The snapshot is saved correctly (file copy works), but `restore` uses `python3 -c "json.load(...)"` to read `meta.json` for the target path. Invalid JSON = restore permanently fails. The snapshot is orphaned.

**Evidence:**
```
json.decoder.JSONDecodeError: Expecting ',' delimiter: line 5 column 30
```

**Fix:**
```bash
# Replace heredoc with python3-based safe JSON writer
python3 -c "
import json, sys, os
json.dump({
    'id': sys.argv[1], 'timestamp': sys.argv[2],
    'action': sys.argv[3], 'description': sys.argv[4],
    'target': sys.argv[5], 'type': sys.argv[6],
    'hostname': os.uname().nodename, 'user': os.environ['USER']
}, open(sys.argv[7], 'w'), indent=2)
" "$snap_id" "$(_ts)" "$action" "$desc" "$file" \
  "$(cat "${snap_dir}/.type")" "${snap_dir}/meta.json"
```

---

#### Issue #2: Restore Fails Silently on Read-Only Targets

**Location:** Line 92 (`cp "$backup_file" "$target"`)

**Problem:** `set -euo pipefail` causes the script to exit immediately when `cp` encounters a permission error. The restore log entry (line 118) is never written, and no error message reaches the caller beyond `cp`'s stderr.

**Reproduction:**
```bash
chmod 444 /tmp/important-config.txt
rollback.sh restore <snap_id>
# Output: cp: cannot create regular file '...': Permission denied
# Exit code: 1
# History log: NOT updated (caller thinks nothing happened)
```

**Impact:** The agent believes restore failed (gets exit code 1), but there's no structured error. Worse: if `pipefail` is inherited differently by a subshell, the error could be swallowed.

**Fix:**
```bash
# Before cp, ensure writability:
if [[ -f "$target" ]] && [[ ! -w "$target" ]]; then
    chmod u+w "$target" 2>/dev/null || {
        echo "ERROR: Cannot restore to read-only target: $target" >&2
        echo "{\"id\":\"${snap_id}\",\"ts\":\"$(_ts)\",\"action\":\"restore_failed\",\"target\":\"${target}\",\"error\":\"permission denied\"}" >> "$ROLLBACK_LOG"
        return 1
    }
fi
```

---

#### Issue #3: Symlinks Backed Up as Content, Not as Links

**Location:** Line 33 (`cp "$file" "${snap_dir}/..."`)

**Problem:** `cp` without `-P` follows symlinks and copies the target file's content. On restore, it writes content back *through* the symlink to whatever the link currently points at. If the symlink was re-pointed or broken, restore either corrupts the wrong file or fails.

**Reproduction:**
```bash
ln -s /etc/app.conf /tmp/link-to-conf
rollback.sh snapshot /tmp/link-to-conf "backup" "before change"
# Backs up /etc/app.conf CONTENT, not the symlink itself

# Later: symlink is re-pointed
ln -sf /etc/other.conf /tmp/link-to-conf
rollback.sh restore <id>
# Overwrites /etc/other.conf with old /etc/app.conf content!
```

**Impact:** Silent data corruption of an unrelated file.

**Fix:**
```bash
if [[ -L "$file" ]]; then
    # Store the symlink target path, not the file content
    readlink "$file" > "${snap_dir}/$(basename "$file").symlink"
    echo "symlink" > "${snap_dir}/.type"
elif [[ -f "$file" ]]; then
    cp -p "$file" "${snap_dir}/$(basename "$file")"
    echo "backup" > "${snap_dir}/.type"
fi
```

---

### High (Correctness Bugs)

#### Issue #4: ID Collision Under Parallel Execution

**Location:** Line 18 — `_id() { date +%s%N | sha256sum | head -c 12; }`

**Problem:** `sha256sum` is deterministic. Two agents calling `_id()` in the same nanosecond produce the identical ID. The second `mkdir -p "$snap_dir"` succeeds (directory already exists), and the second `cp` overwrites the first snapshot's backed-up file.

**Likelihood:** Moderate in multi-agent orchestration (the `dispatching-parallel-agents` skill dispatches simultaneous subagents).

**Fix:**
```bash
_id() { echo "$(date +%s%N)$$${RANDOM}$(head -c 8 /dev/urandom | xxd -p)" | sha256sum | head -c 12; }
```

---

#### Issue #5: `undo N` Can Double-Restore Same File Incorrectly

**Location:** Lines 146–165

**Problem:** `undo 3` restores the last 3 snapshots in reverse order via `tac`. If two of those target the same file (e.g., two edits to `config.py`), it restores the *older* state last — leaving the file in the wrong state.

**Example:**
```
History:
  snap_A → config.py (state: original)
  snap_B → config.py (state: after-edit-1)
  snap_C → other.py  (state: original)

`undo 3` restores C, B, A in that order.
- Restores C → other.py gets original ✓
- Restores B → config.py gets after-edit-1 state
- Restores A → config.py gets ORIGINAL state ← wrong if intent was undo-last-edit
```

**Fix:** De-duplicate by target path, keeping only the oldest snapshot per unique target:
```bash
# In rollback_undo, after collecting snap_ids:
declare -A seen_targets
for sid in $snap_ids; do
    target=$(python3 -c "..." "${ROLLBACK_DIR}/snapshots/${sid}/meta.json")
    if [[ -z "${seen_targets[$target]:-}" ]]; then
        seen_targets[$target]=$sid
        rollback_restore "$sid"
    fi
done
```

---

#### Issue #6: `grep -v` Filter Is a Substring Match

**Location:** Line 152 — `grep -v '"action":"restore"'`

**Problem:** Actions named `"restore-backup"`, `"pre-restore-check"`, or `"restore_failed"` are all filtered out by the substring match. The `undo` function would skip them.

**Fix:** Use a more precise pattern:
```bash
grep -v '"action":"restore",' "$ROLLBACK_LOG"
# The trailing comma ensures exact value match in the JSON structure
```
Or better, use `jq` / `python3` for proper JSON filtering.

---

### Medium (Portability / Robustness)

#### Issue #7: `find -printf` Is GNU-Only

**Location:** Line 63

**Problem:** macOS/BSD `find` does not support `-printf`. The pruning logic fails entirely on non-Linux systems.

**Fix:**
```bash
# POSIX-compatible alternative:
find "${ROLLBACK_DIR}/snapshots" -maxdepth 1 -mindepth 1 -type d \
  | xargs -I{} stat --format='%Y {}' {} 2>/dev/null \
  | sort -n | head -n $((count - MAX_SNAPSHOTS)) | awk '{print $2}' \
  | xargs rm -rf
```

---

#### Issue #8: `tac` Is Linux-Only

**Location:** Line 160

**Problem:** `tac` is not available on macOS. The `undo` function breaks.

**Fix:** `tail -r` (macOS) or portable: `sed '1!G;h;$!d'` or `python3 -c "import sys; print(''.join(reversed(sys.stdin.readlines())))"`.

---

#### Issue #9: No Size Guard on Snapshots

**Location:** Line 33 (`cp "$file"`)

**Problem:** No check on file size before copying. A 10GB log file or database dump is blindly duplicated. With `MAX_SNAPSHOTS=100`, this could fill 1TB of disk.

**Fix:**
```bash
local file_size
file_size=$(stat -c %s "$file" 2>/dev/null || echo 0)
if (( file_size > 104857600 )); then  # 100MB
    echo "WARNING: File is $(( file_size / 1048576 ))MB — skipping full backup, saving metadata only" >&2
    echo "large_file_skipped" > "${snap_dir}/.type"
    return
fi
```

---

#### Issue #10: Fragile Argument Handling in Dispatch

**Location:** Lines 171–172 — `rollback_list "${@:-20}"` / `rollback_undo "${@:-1}"`

**Problem:** After `shift`, `$@` is empty if no additional args were passed. `${@:-20}` works because empty `$@` triggers the default. However, if extra unexpected args are passed (e.g., `rollback.sh list 5 extra`), all are forwarded. This is fragile and unclear.

**Fix:**
```bash
list)   shift; rollback_list "${1:-20}" ;;
undo)   shift; rollback_undo "${1:-1}" ;;
```

---

### Low (Minor / Cosmetic)

| # | Issue | Impact |
|---|-------|--------|
| 11 | Emoji in output (📸, ⏪, 🔄) | Garbled in non-UTF-8 terminals or CI |
| 12 | No file locking on `history.jsonl` | Theoretical interleave with concurrent appends |
| 13 | Prune runs after snapshot creation (small race window) | Extremely unlikely to matter |

---

## Test Results Summary

| Test Case | Result | Notes |
|-----------|--------|-------|
| Basic file backup + restore | PASS | Happy path works correctly |
| File with spaces in path | PASS | Bash quoting handles it |
| Special chars in description | **FAIL** | Breaks JSON → unrestorable |
| Quotes in file path | **FAIL** | Breaks JSON → unrestorable |
| Symlink backup | PASS* | *Backs up content, not link |
| Symlink restore | PASS* | *Writes through current link target |
| Directory backup + restore | PASS | Full recursive copy works |
| Read-only file snapshot | PASS | `cp` can read the file |
| Read-only file restore | **FAIL** | Permission denied, no recovery |
| Permission preservation | PASS | `cp` preserves mode bits |
| `list` command | PASS | Displays correctly |
| `undo` command | PASS* | *See issue #5 for edge case |
| Pruning at MAX_SNAPSHOTS | Not tested | Would need 100+ snapshots |

---

## Risk Matrix

```
              LIKELIHOOD
              Low    Med    High
         ┌────────┬────────┬────────┐
  High   │   #3   │   #5   │   #1   │
IMPACT   ├────────┼────────┼────────┤
  Med    │ #7,#8  │   #4   │   #6   │
         ├────────┼────────┼────────┤
  Low    │#11,#12 │  #9,#10│        │
         └────────┴────────┴────────┘
```

---

## Recommendations

### Immediate (before next autonomous agent session)

1. **Fix JSON escaping** (Issue #1) — use `python3` for all JSON generation
2. **Add PID + random to ID generator** (Issue #4) — prevent parallel collisions
3. **Handle permission errors in restore** (Issue #2) — `chmod u+w` or structured error

### Short-term (within 1 week)

4. Fix symlink handling with `cp -P` or explicit symlink type (Issue #3)
5. Add size guard (Issue #9) — skip or warn for files > 100MB
6. Fix `undo` de-duplication logic (Issue #5)

### Long-term (nice to have)

7. Replace bash JSON with `jq` throughout
8. Add cross-platform support (`tac` → portable reverse, `find -printf` → `stat`)
9. Add `flock` for atomic history.jsonl writes
10. Add `--dry-run` flag to `undo` for preview

---

## Appendix: Tested On

- **OS:** Ubuntu Linux (kernel 6.x)
- **Bash:** 5.2
- **Python:** 3.12
- **Coreutils:** GNU 9.x (includes `tac`, `find -printf`)
- **Snapshot count at time of review:** 52 active, 55 history entries
