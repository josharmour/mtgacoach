# Native-Mac IL2CPP GRE spike: results

Scope: the first reviewable milestone in `autopilot4mac.md` — observe the live
request in the native Mac client and submit play/draw, Keep and a land by
identity, with each outcome confirmed. This covers only the first half of G1/G2;
creatures, payment chains, backgrounded Arena, repeated launches and scene
transitions remain untested.

## Configuration (2026-09-23)

- MTGA 2026.63.0 (Steam), Unity 6000.3.14f1, IL2CPP metadata v39, running
  natively as arm64 on Apple Silicon, macOS 26.6.2.
- `probe.cpp` injected with `DYLD_INSERT_LIBRARIES` (the app is Developer ID
  signed with codesign `flags=0x0`: no hardened runtime, no entitlements).
- No BepInEx, Il2CppInterop, .NET hosting or Rosetta. All game access goes
  through `GameAssembly.dylib`'s exported `il2cpp_*` functions.
- Main thread: `PAPA.Update`'s `MethodInfo->methodPointer` (and
  `virtualMethodPointer`) swapped for a trampoline. The layout check found
  `name` at slot 3 before writing; no code pages were patched.

## Launch gotcha

Launching through `/usr/bin/nohup` (or any SIP-protected binary, such as
`/usr/bin/env`) silently strips `DYLD_*` from the child environment. The first
live launch therefore ran with no probe. Confirmed with a local test binary:
through `nohup` the probe did not load, launched directly it did.
`launch_mtga_probe.sh` now execs MTGA directly.

## Results (pid 61136)

| Check | Evidence |
| --- | --- |
| Probe loads and hooks | Probe log 22:51:45–48: API resolved, `hooked PAPA.Update ... name_slot=3 virtual_swapped=1`, listening on 44223 |
| Main thread runs jobs | `ping` ticks 743 → 962 over ~2 s |
| Background static read | `static - PAPA _instance` → non-null `PAPA` |
| Unity API on main thread | `pending` at home: `FindAnyObjectByType(GameManager)` returned null without a thread exception |
| Live request discovery | In a Bot Match: `GameManager.WorkflowController.CurrentWorkflow.BaseRequest` yielded `ChooseStartingPlayerRequest`, `MulliganRequest`, then `ActionsAvailableRequest` with `[Cast#159, Cast#161, Cast#162, Play#160, Play#163, Play#165, Pass#0]` |
| Play/draw | 22:55:08 `ChooseStartingPlayer(1)`; Player.log has `ChooseStartingPlayerResp`; server `turnInfo` then shows `turnNumber 1, activePlayer 1` (the probe's seat) |
| Keep | 22:55:08 `MulliganRequest.KeepHand()`; Player.log `MulliganResp` `MulliganOption_AcceptHand` |
| Land by identity | 22:55:09 `SubmitAction(Actions[3], false)` after checking instance 160; Player.log `PerformActionResp ActionType_Play grpId 75556 instanceId 160` |
| Server accepted the land | Next GRE state: `ObjectIdChanged 160 → 279`, `ZoneTransfer` 279 zone 31 → 28, category `PlayLand`, `UserActionTaken` by seat 1 |

The first run of `g2_smoke.py` printed "NOT SEEN" for the play/draw response
because that response carries none of the fields it extracts, and an empty dict
is falsy. The response was present; the check is fixed.

## Not yet shown

- Casting a spell through its successor requests (casting-time options,
  targets, payment/autotap).
- Operation with Arena backgrounded; repeated cold launches; scene transitions.
- The coach's `gre_bridge.py` protocol (the probe speaks its own test protocol
  on port 44223).
