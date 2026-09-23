# Native Mac autoplay handoff — 2026-09-20

## Latest status — 22:59 local time

This supersedes the 22:35 status below. Desktop PID is now 54756 (query before
reuse). AP is ON and waiting at Arena's home screen. A new Sparky match is needed
for the next live card-play test; the executor must not queue it automatically.
The last match ended while being played manually; no autonomous win is claimed.
The user explicitly reported manually playing Plains and Seam Rip.

Further observations and changes:

- Cancelled the previous unpayable Vendor cast with a controlled click at 22:40:29.
- Retries at 22:40:48 and 22:41:14 returned prose rather than action JSON and
  paused without input. The AP button remained ON, although the engine was paused.
  This misleading UI status remains unfixed.
- A diagnostic against the configured scoped vision endpoint showed normal
  `finish_reason=stop`, not token truncation. JSON response mode is accepted.
  Added opt-in `json_mode=False` to `ProxyBackend.complete_with_image`; native
  autoplay passes `json_mode=True`, sending `response_format={"type":"json_object"}`.
  Other image callers retain their old request format. Invalid actions and low
  confidence still pause; prose is never converted into guessed clicks.
- 75 affected tests passed after the JSON-mode change (native executor/routing
  and proxy thinking controls), including enabled/disabled JSON-mode coverage.
  Focused Ruff checks pass. Broad proxy Ruff still has its pre-existing SIM114
  at line 277; it was left alone. Previous 196-test result is documented below.
- With JSON mode loaded, the next live run consistently returned JSON but missed
  a highlighted Plains: source screenshot center near x=650/1600=0.41, while
  model proposals used x=0.66. Report `bug_20260920_225241.json` and its autoplay
  image document an empty battlefield and the enlarged Plains. Three clicks
  were sent, but no land play was confirmed before a low-confidence pause.
- Saved-image probes found correct coordinates when explicitly explaining the
  conversion. Both a localization-only prompt and the full autoplay prompt
  returned approximately [0.41,0.79] after adding the formula and example.
  The system prompt now states x=pixel/image_width and y=pixel/image_height,
  explicitly warns against dividing pixels by 1000, and gives a 1600x935 example.
  This prompt-only adjustment passes Ruff and is loaded in the current runtime.
- After the latest restart, game state had advanced manually to turn 7. AP's
  attack proposal was discarded after the UI changed; foreground changes also
  stopped input. It then observed victory/loading/home. No confirmed input was
  sent by this runtime before home, so the coordinate clarification has NOT yet
  been validated by an autonomous live cast.

Next: a hands-off Sparky test from a known state, preserving exact before/after
images and GRE transitions. First verify the coordinate correction on a land,
then a spell and targeting/payment. If grounding still fails, evaluate the vision
model/grounding separately instead of treating more posted clicks as success.


## Latest Mac continuation — 22:35 local time

This section supersedes the immediate-next-step status below. Existing changes
were preserved; no commits or branches were created. Work is in
`/Volumes/repos/mtgacoach`, despite the Codex workspace starting in `arenaonair`.

**Double-click event delivery works. Autonomous card identification is still
unreliable.** AP is now OFF; Arena is left at Spellbook Vendor's Pay Costs prompt.
Do not count the user's manual Seam Rip cast/targeting as autoplay progress.

Live evidence, all on the existing Sparky match:

- 22:26:49: autoplay double-clicked a Plains, which left hand and appeared on the
  battlefield as instance 283. Reports `bug_20260920_222641.json` (before) and
  `bug_20260920_222703.json` (after) establish this transition.
- Subsequent automated Scavenger attempts did not cast it. One instead hovered
  Sheltered by Ghosts. The engine paused at 22:27:14 on prose without JSON.
- 22:29:07: the user manually double-clicked Seam Rip and completed targeting;
  Zephyr Gull was exiled. These actions happened with AP off.
- 22:31:04: with AP off, a controlled input probe hovered Optimistic Scavenger,
  inspected its expanded position, then posted the existing Quartz double-click
  sequence (40 ms down, 70 ms between clicks; click counts 1 then 2). It cast
  successfully and Arena auto-tapped the Plains. Report
  `bug_20260920_223105.json` shows Scavenger instance 291 on the battlefield,
  absent from hand, and Plains tapped. This was a diagnostic probe, not an
  autonomous model-selected action. The event timing was NOT changed.

Fix added during this continuation:

- `NativeMacInput.execute` now allows 300 ms for hover before click/double-click/
  drag, captures again, and rejects input when the target/window layout changes.
  Previously its final screenshot check preceded mouse movement, leaving hand
  expansion/reflow unchecked. Abort remains checked before mouse-down.
- The prompt asks for hover/identify/then-double-click on overlapping hands,
  treats previous inputs as attempts, and avoids unnecessary manual land taps.
- Four regression cases cover hover rejection for each mouse-press action and
  stopping during the extra capture. The complete handoff suite passed on Mac:
  **196 tests** (65 native/routing + 131 remaining). Focused Ruff check/format pass.
  Test tools were installed under `/tmp/mtgacoach-native-test-tools`, without
  altering the live app venv; use that directory plus `src` on `PYTHONPATH` with
  the local app Python to run pytest.

Retest after restarting the coach with this fix:

- 22:33:38: the new guard visibly rejected hover-changed layouts before clicking.
- 22:33:54: the model proposed "Play a Plains" at `(0.55, 0.93)` but actually
  double-clicked Spellbook Vendor. GRE then reported Pay Costs for Vendor and
  moved it from hand onto the stack (instance 293). Only one Plains was on the
  battlefield and it was already tapped; the model chose the wrong card.
- 22:34:00: payment proposal confidence was 0.70 and the engine correctly paused.
  The 0.80 threshold remains unchanged. Report `bug_20260920_223406.json` and its
  `_autoplay.png` preserve the payment screen and proposal. AP was explicitly
  stopped after capturing this evidence. Full automatic targeting/payment and
  full-match reliability remain unverified.

Next work: cancel the unpayable Vendor cast, then improve/measure visual card
identification before resuming repeated autonomous attempts. A hover freshness
check does not prove that the model named the correct card at its coordinates.
Keep the working event timing unless another controlled test shows a failure.

Accessibility approval was corrected by the user. `osascript` can now inspect
and control the coach UI. Latest desktop PID is 52857 (query before reuse).
Codex-launched Python has post-event access but no capture permission; continue
using app-generated reports for screenshots. Reports still contain credentials
in unrelated fields; inspect only selected state/autopilot/screenshot data.

## User request and immediate next step

Make full autoplay work with native Steam MTGA on Apple Silicon, without Wine,
CrossOver or BepInEx. User has an M5 Max, granted Accessibility and Screen
Recording to the desktop app, and is testing against Sparky. Most recently:

- User observed a Spellbook Vendor drag did not move far enough out of the hand.
- User pointed out cards can be played by double-clicking.
- The visual prompt now prefers `double_click` for lands/spells and reserves
  dragging for blockers/selection piles. **This latest change is tested locally
  but has NOT been loaded into the running Mac coach or verified live.**
- Restart the coach, enable AP if necessary, leave Arena in the foreground,
  and verify a double-click actually moves a card from hand to stack/battlefield.
  Then verify targeting/payment continuation. Do not claim full-match reliability.

The user offers to continue directly in Codex on the Mac. Keep only one agent
editing this shared checkout. Do not discard or overwrite the existing uncommitted
changes. No commits or branches have been created. There is also an unrelated
untracked `<arg_value>/` directory; leave it alone.

## Correct Mac environment

- Shared repo: `/Volumes/repos/mtgacoach`, also accessible under `~/repos/mtgacoach`.
  Linux `/mnt/repos/mtgacoach` edits are already visible here; no source deployment
  or file copying is necessary.
- App bundle: `/Applications/MTGA Coach.app`.
- Working app interpreter:
  `/Users/joshu/Library/Application Support/mtgacoach/venv/bin/python`
  (Python 3.12.13, PyObjC installed, editable source points at the shared repo).
- Do not use the SMB-hosted `.venv/` or `.venv_mac_311/` for live app testing;
  they are broken/slow in this environment. The local app venv lacks pytest.
- Native Arena executable:
  `/Users/joshu/Library/Application Support/Steam/steamapps/common/MTGA/MTGA.app/Contents/MacOS/MTGA`.
- macOS 26.6.2, arm64. Arena is a universal executable.
- Runtime log: `~/.arenamcp/standalone.log`.
- Desktop log: `~/Library/Application Support/mtgacoach/desktop.log`.
- Settings: `~/.arenamcp/settings.json`. **Contains secrets: do not print it.**
  The selected vision alias is `glm-5.3-flash`, using direct LiteLLM at
  `http://10.0.0.10:8444/v1` with a scoped key already configured. Do not replace it
  with an administrator key. The ordinary voice coach endpoint separately reports
  HTTP 403; native autoplay uses the working direct endpoint.

Tools → Restart Coach shuts down cleanly, but its relaunch may fail. If the app
does not return, run `open -a "MTGA Coach"`. Avoid starting duplicate instances.
Startup through the SMB checkout has taken 15–120 seconds. Historical desktop
exceptions are not necessarily current failures. Do not restart Arena itself.

Permission attribution matters: the desktop app passed both checks, while an
SSH-launched Python reports no Screen Recording access. Do not infer that the
app lacks permission, or ask to grant SSH remote capture access. Use the app's
bug reports to inspect its actual captured model image.

## Implemented architecture

- `src/arenamcp/native_mac_autopilot.py`: experimental independent visual executor,
  polling even without log events, log state plus image completion, one input then
  observe, bounded retries, stop/abort, stale-screen/state checks, low-confidence
  pause, no automatic queueing/purchases/concedes.
- `src/arenamcp/native_mac_input.py`: window-only capture, normalized coordinates
  mapped into Quartz logical screen points, Retina support, focus and input-owner
  checks, mouse/key/scroll/double-click/drag execution and guaranteed releases.
- `standalone.py` selects this engine for native macOS instead of BepInEx and
  polls it each coaching iteration. Existing bridge platforms are preserved.
- Mac Tools menu has Autoplay Permissions, Autoplay Vision Model and Stop Autoplay
  (F11). AP toggles/resumes the engine.
- `NATIVE_MAC_AUTOPLAY.md` explains setup, limitations and windowed-mode use.

Windowed mode works; no specific resolution/fullscreen requirement. Arena must
remain foreground. Other apps may be open but must not receive the intended click.
Actively using another app pauses input. Resizing/moving/hovering over cards during
the model request can invalidate that screenshot. User had been manually playing
during early tests, then agreed to stop; do not attribute those turns to autoplay.

## Confirmed fixes and live observations

1. AppKit `NSWorkspace.frontmostApplication()` stayed stale in the headless coach
   subprocess without a Cocoa main run loop. Reproduced live: cached foreground
   remained Ghostty after switching apps, whereas an independent query updated.
   Replaced it with synchronous ApplicationServices `GetFrontProcess` /
   `GetProcessPID` via ctypes; tested on this ARM Mac.
2. A rectangle-based occlusion guard incorrectly blocked the entire screen because
   Dock and Untapped have transparent, click-through windows. Replaced it with
   `AXUIElementCopyElementAtPosition` plus `AXUIElementGetPid` to verify the actual
   input recipient. It still fails closed on unknown/error/non-Arena targets.
   Added `pyobjc-framework-ApplicationServices` as a Darwin dependency.
3. GLM accepts actual Arena images. Observed response times roughly 4–8 seconds
   (one earlier cold synthetic request exceeded 15 seconds). Main blocking issues
   were focus/occlusion and action accuracy, not absence of vision support.
4. Action parsing now allows prose and mana symbols such as `{W}` before one JSON
   command while rejecting multiple commands/action arrays. Invalid responses and
   proposed actions/confidence are logged. Confidence below 0.8 pauses; do not
   silently lower this threshold to make the test appear successful.
5. Three initial drags were posted but did not play the intended cards. Never treat
   `inputs_sent` as successful game actions. User specifically saw insufficient
   drag distance; double-click preference is the next live test.
6. **Actual native clicks are verified:** around 22:12:46–22:13:00 local time, the
   engine advanced into combat, selected attackers and confirmed. A screenshot
   showed Choose attackers after the first click, subsequent logs and game state
   advanced through combat; opponent life fell from 3 to 2. It later paused on a
   0.70-confidence end-turn proposal. This was NOT an autonomous complete match.

## Debug reports and exact model image

User's original report:
`~/.arenamcp/bug_reports/bug_20260920_220531.json`
showed `inputs_sent: 0`. Its regular MTGA screenshot included the coach overlay,
so it was not sufficient to infer the image actually sent to the model.

New reports now attach the executor's exact last model image as
`screenshots.autoplay`. `autopilot.engine` includes last proposal, last notice,
window bounds and image size. The image is cached in memory and written only
when a bug report is saved, not on every frame.

Useful confirmed report:
`~/.arenamcp/bug_reports/bug_20260920_221251.json`
and `bug_20260920_221251_autoplay.png` in the same directory.
It shows an unobscured Arena-only image, 1600×935, mapped to window bounds
`[19, 43, 1280, 748]` logical points. Bottom-right button clicks mapped correctly.
The model had earlier proposed inaccurate/repeated card drags; inspect image and
proposal together rather than assuming display scaling is broken.

Do not print full reports indiscriminately: their existing settings dump can
contain credentials. Read selected game/autopilot/log/screenshot fields only.

The report button can be invoked through macOS Accessibility without guessed
mouse coordinates. Determine the current desktop PID first (it changes on restart):

```sh
ps -axo pid,ppid,command | grep 'arenamcp.desktop$'
osascript -e 'tell application "System Events" to tell (first application process whose unix id is DESKTOP_PID) to click button "🐞 Report" of group 1 of window 1'
osascript -e 'tell application "MTGA" to activate'
```

Replace `DESKTOP_PID` with the actual number; do not run the placeholder literally.
Access to System Events/Automation may depend on the terminal's own permissions.

## Tests

Linux development venv `.venv/bin/python` is usable on the Linux host only.
The final focused suite passed **192 tests**, including double-click event counts,
abort between clicks, and observing targeting after a double-click card play.

```sh
python -m pytest -q tests/test_native_mac_autopilot.py \
  tests/test_standalone_native_autopilot.py tests/test_proxy_thinking_control.py \
  tests/test_autopilot_bridge_lock.py tests/test_platform_darwin.py \
  tests/test_standalone_conversation.py tests/test_repair_darwin.py \
  tests/test_desktop_runtime_darwin.py tests/test_pipe_conversation.py
```

Ruff checks/format checks pass for the new native modules/tests and the touched
diagnostics module. Existing unrelated broad lint issues and Linux PortAudio test
collection failures were left alone. Files in this repo may use CRLF; use
`git -c core.whitespace=cr-at-eol diff --check` and avoid whole-file newline churn.

## If remote access is needed again

The Linux host cannot route directly to the VPN Mac address `192.168.2.20`.
The user established a reverse tunnel from Mac to Blackwell:

```sh
ssh -N -o ExitOnForwardFailure=yes -R 127.0.0.1:22220:localhost:22 joshu@10.0.0.10
```

Linux-side connection:

```sh
ssh -F /dev/null -p 22220 -o BatchMode=yes -o ConnectTimeout=5 \
  -o HostKeyAlias=10.0.0.50 joshu@127.0.0.1
```

Direct Mac work needs no tunnel. No remote restarts or further game inputs were
initiated after the user offered to move this session to the Mac.
