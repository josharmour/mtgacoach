# MtgaCoach GRE Bridge — BepInEx Plugin

BepInEx plugin that lets the mtgacoach autopilot submit GRE actions directly
to MTGA instead of simulating mouse clicks.

## How it works

Coach’s [Python bridge](../src/arenamcp/gre_bridge.py) listens on loopback TCP. The [BepInEx plugin](MtgaCoachBridge/Plugin.Pipe.cs) connects to Coach and exchanges newline-delimited JSON. The plugin dispatches game actions on the Unity main thread. See [Plugin.Actions.cs](MtgaCoachBridge/Plugin.Actions.cs) for the commands supported by this build.

Windows and Linux/Proton use this plugin. Managed Apple Silicon Mac packages include the native IL2CPP bridge and stage it while Arena is closed. Native Mac source users build that [bridge separately](../spikes/mac-il2cpp/build.sh); BepInEx does not load into native Arena. [Customer platform setup](../INSTALL.md#platform-setup). Manual coaching can read Arena’s log on all three platforms.

## Install and build

Use **Tools → Setup & Repair** in Coach to check BepInEx and the packaged plugin. Close Arena before replacing the Windows plugin.

For a development build, use the installed Arena directory containing `MTGA_Data/Managed` and `BepInEx/core` as `MtgaDir`. From the plugin project directory:

```powershell
dotnet build -c Release -p:MtgaDir="D:\Games\MTGA"
```

The DLL is written to `bin/Release/net472/MtgaCoachBridge.dll`. A customer release uses the provenance procedure below.

## Release provenance

Release packaging requires a fresh Windows build against the installed Arena
managed assemblies. From the repo root run:

```powershell
.\installer\build-bridge.ps1 -MtgaDir "C:\Program Files\Wizards of the Coast\MTGA"
```

This rebuilds without incremental output, records compiler/reference/source
hashes, and stages only our DLL and manifest. It does not deploy into Arena. GitHub's packaging runners do not contain those proprietary
references. They verify `src/arenamcp/resources/MtgaCoachBridge.build.json`
against every bridge source/project input and the staged DLL before building the
wheel or installer. A missing manifest or changed source/DLL fails packaging.

The manifest records schema version 1, SHA-256 maps for source inputs and named
reference assemblies, the DLL SHA-256, compiler version, and UTC build time.
Keep the successful build log with release evidence. Publish only the bridge DLL
and hashes; do not add Arena's reference DLLs or machine credentials to the repo.
Run `python scripts/verify_bridge_release.py` before cutting a release. This check
establishes correspondence to the recorded build; live Windows gameplay remains
a separate qualification.
