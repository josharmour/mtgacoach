"""Run the production Windows selection handler against a recording Arena stub.

Usage: python scripts/check_bridge_selection.py DOTNET NEWTONSOFT_JSON_DLL
No Arena process or live bridge is opened. Requires a .NET 10 SDK.
"""

import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape


def main():
    dotnet, json_dll = sys.argv[1:]
    root = Path(__file__).resolve().parents[1]
    source = (root / "bepinex-plugin/MtgaCoachBridge/Plugin.Actions.cs").read_text(encoding="utf-8-sig")
    start = source.index("        private void HandleSubmitSelection(")
    end = source.index("        private void HandleSubmitGroup(", start)
    # Compile the actual method, not a second implementation of its behavior.
    handler = source[start:end]
    scaffold = r'''
using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
class SelectNRequest {
    public List<uint> Selection;
    public bool Arbitrary;
    public void SubmitArbitrary() { Arbitrary = true; Selection = new List<uint>{0}; }
    public void SubmitSelection(List<uint> ids) { Selection = ids; }
}
class SearchRequest {
    public List<uint> Selection;
    public void SubmitSelection(List<uint> ids) { Selection = ids; }
}
class PipeCommand {
    public JObject Json, Response;
    public void SetResponse(JObject response) { Response = response; }
}
class Log { public void LogInfo(string text) {} }
class Harness {
    object Request, _lastKnownRequest;
    object _interactionLock = new object();
    Log _log = new Log();
    object FindPendingInteraction() => Request;
    HANDLER
    static void Require(bool value) { if (!value) throw new Exception("selection regression"); }
    static void Main() {
        foreach (var ids in new[]{new uint[]{}, new uint[]{351}, new uint[]{351,352}}) {
            var request = new SelectNRequest();
            var command = new PipeCommand {Json = new JObject { ["ids"] = new JArray(ids) }};
            new Harness {Request=request}.HandleSubmitSelection(command);
            Require(command.Response.Value<bool>("ok") && !request.Arbitrary);
            Require(request.Selection.Count == ids.Length);
            for (int i=0;i<ids.Length;i++) Require(request.Selection[i]==ids[i]);
        }
        var missing = new SelectNRequest();
        var bad = new PipeCommand {Json = new JObject()};
        new Harness {Request=missing}.HandleSubmitSelection(bad);
        Require(!bad.Response.Value<bool>("ok") && missing.Selection==null);
        var search = new SearchRequest();
        var empty = new PipeCommand {Json = new JObject { ["ids"] = new JArray() }};
        new Harness {Request=search}.HandleSubmitSelection(empty);
        Require(empty.Response.Value<bool>("ok") && search.Selection.Count==0);
        Console.WriteLine("PASS production handler: empty, single, multiple, missing and Search selections");
    }
}
'''.replace("HANDLER", handler)
    with tempfile.TemporaryDirectory(prefix="coach-selectn-") as temp:
        folder = Path(temp)
        (folder / "Program.cs").write_text(scaffold, encoding="utf-8")
        (folder / "Harness.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            '<TargetFramework>net10.0</TargetFramework><OutputType>Exe</OutputType>'
            '</PropertyGroup><ItemGroup><Reference Include="Newtonsoft.Json"><HintPath>'
            + escape(str(Path(json_dll).resolve()))
            + '</HintPath></Reference></ItemGroup></Project>'
        )
        subprocess.run([dotnet, "run", "--project", str(folder / "Harness.csproj")], check=True)


if __name__ == "__main__":
    main()
