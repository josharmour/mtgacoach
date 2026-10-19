using System;
using Newtonsoft.Json.Linq;

namespace MtgaCoachBridge
{
    public partial class Plugin
    {
        // Advertised in ping: the coach then runs the commands this plugin lacks
        // (draft, sealed, event, deck, survey) through arenamcp/mac_bridge_adapter.py.
        internal const string ReflectProtocol = "reflect-1";

        private ReflectBatch _reflect;

        // One ping reply for the socket-thread fast path and the main-thread case.
        internal static JObject PingResponse() => new JObject
        {
            ["ok"] = true,
            ["version"] = PluginInfo.Version,
            ["runtime"] = "bepinex",
            ["protocols"] = new JArray(ReflectProtocol)
        };

        private void HandleReflectBatch(PipeCommand cmd)
        {
            if (_reflect == null)
                _reflect = new ReflectBatch(FindSceneObject, FindPendingRequest, obj => obj is UnityEngine.Object);
            cmd.SetResponse(_reflect.Run(cmd.Json));
        }

        private static object FindSceneObject(Type type)
        {
            var found = UnityEngine.Object.FindAnyObjectByType(type);
            return found != null ? found : null;  // Unity's fake-null destroyed objects read as absent
        }

        // probe find_pending: GameManager -> WorkflowController -> current (else pending)
        // workflow -> BaseRequest (else Request).
        private ReflectBatch.PendingResult FindPendingRequest()
        {
            var pending = new ReflectBatch.PendingResult();
            var managerType = _reflect.ClassByFullName("GameManager");
            if (managerType == null)
            {
                pending.Error = "GameManager class not found";
                return pending;
            }
            var manager = FindSceneObject(managerType);
            if (manager == null)
            {
                pending.Error = "no GameManager in the scene (not in a match)";
                return pending;
            }
            var controller = Member(manager, "WorkflowController");
            if (controller == null)
            {
                pending.Error = "WorkflowController is null";
                return pending;
            }
            var workflow = Member(controller, "CurrentWorkflow") ?? Member(controller, "PendingWorkflow");
            if (workflow == null)
            {
                pending.Error = "no current or pending workflow";
                return pending;
            }
            pending.Request = Member(workflow, "BaseRequest") ?? Member(workflow, "Request");
            pending.Error = pending.Request != null ? "" : "workflow has no request";
            return pending;
        }

        // Reference-typed member: property getter first, then field.
        private static object Member(object obj, string name)
        {
            try
            {
                var getter = ReflectBatch.FindMethod(obj.GetType(), "get_" + name, 0);
                if (getter != null) return getter.Invoke(obj, new object[0]);
                return ReflectBatch.FindField(obj.GetType(), name)?.GetValue(obj);
            }
            catch (Exception)
            {
                return null;
            }
        }
    }
}
