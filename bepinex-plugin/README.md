# MtgaCoach GRE Bridge — BepInEx Plugin

BepInEx plugin that lets the mtgacoach autopilot submit GRE actions directly
to MTGA instead of simulating mouse clicks.

## How It Works

1. Plugin hooks into MTGA's Unity runtime via BepInEx
2. Opens a named pipe (`\\.\pipe\mtgacoach_gre`) for IPC
3. Python autopilot connects to the pipe and sends JSON commands
4. Plugin finds the pending `ActionsAvailableRequest` in the game
5. Calls `SubmitAction()` directly on the game's GRE interface

## Install BepInEx

1. Download [BepInEx 5.x](https://github.com/BepInEx/BepInEx/releases) (x64, Mono)
2. Extract into your MTGA install directory:
   ```
   C:\Program Files\Wizards of the Coast\MTGA\
   ```
   You should have:
   ```
   MTGA/
   ├── BepInEx/
   │   ├── core/
   │   ├── plugins/     ← our DLL goes here
   │   └── ...
   ├── doorstop_config.ini
   ├── winhttp.dll
   └── MTGA.exe
   ```
3. Run MTGA once to let BepInEx initialize, then close it.

## Build the Plugin

```powershell
cd bepinex-plugin\MtgaCoachBridge
dotnet build -c Release
```

If MTGA is not in the default location, pass the managed directory:
```powershell
dotnet build -c Release -p:MtgaManagedDir="D:\Games\MTGA\MTGA_Data\Managed"
```

## Install the Plugin

Copy the built DLL to BepInEx plugins:
```powershell
copy bin\Release\net472\MtgaCoachBridge.dll "C:\Program Files\Wizards of the Coast\MTGA\BepInEx\plugins\"
```

## Protocol

The pipe uses newline-delimited JSON. Each request is one line, each response
is one line.

### ping
```json
→ {"action": "ping"}
← {"ok": true, "version": "0.1.0"}
```

### get_pending_actions
```json
→ {"action": "get_pending_actions"}
← {"ok": true, "has_pending": true, "request_type": "ActionsAvailable",
    "actions": [{"actionType": "Cast", "grpId": 12345, "instanceId": 678}, ...],
    "can_pass": true}
```

### submit_action
```json
→ {"action": "submit_action", "action_index": 0, "auto_pass": false}
← {"ok": true, "submitted_type": "Cast", "submitted_grp_id": 12345, "submitted_instance_id": 678}
```

### submit_pass
```json
→ {"action": "submit_pass"}
← {"ok": true, "submitted_type": "Pass"}
```
