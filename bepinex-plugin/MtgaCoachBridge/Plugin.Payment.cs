using System;
using GreClient.Rules;
using Newtonsoft.Json.Linq;
using Wotc.Mtgo.Gre.External.Messaging;
using GreAction = Wotc.Mtgo.Gre.External.Messaging.Action;

namespace MtgaCoachBridge
{
    public partial class Plugin
    {
        // A PayCostsRequest without Auto Pay (issue #414; an X spell taking every
        // source, 2026-10-18) is paid one mana ability at a time, as Arena's own
        // bot does (PayCostsGoldfishHandler). The chosen color is submitted the
        // way the client's ManaSelectionFlow does: a copy of the action holding
        // only that ManaPaymentOption.
        // Payload: {"instance_id", "ability_grp_id" (0 = any), "color": "ManaColor_Red"}.
        private void HandleSubmitPaymentAction(PipeCommand cmd)
        {
            var request = FindPendingInteraction() as PayCostsRequest;
            var payment = request?.PaymentActions;
            if (payment?.Actions == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "No PayCostsRequest with payment actions pending" });
                return;
            }
            uint instanceId = cmd.Json.Value<uint?>("instance_id") ?? 0;
            uint abilityGrpId = cmd.Json.Value<uint?>("ability_grp_id") ?? 0;
            GreAction source = null;
            foreach (var candidate in payment.Actions)
            {
                if (candidate.InstanceId == instanceId && (abilityGrpId == 0 || candidate.AbilityGrpId == abilityGrpId))
                {
                    source = candidate;
                    break;
                }
            }
            if (source == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"No payment action for instance {instanceId}" });
                return;
            }

            var action = new GreAction(source);
            string colorName = cmd.Json.Value<string>("color");
            if (!string.IsNullOrEmpty(colorName))
            {
                if (!Enum.TryParse(colorName, out ManaColor color))
                {
                    cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Unknown mana color {colorName}" });
                    return;
                }
                ManaPaymentOption chosen = null;
                foreach (var option in source.ManaPaymentOptions)
                {
                    foreach (var mana in option.Mana)
                    {
                        if (mana.Color == color)
                        {
                            chosen = option;
                            break;
                        }
                    }
                    if (chosen != null)
                        break;
                }
                if (chosen == null)
                {
                    cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Instance {instanceId} cannot make {colorName}" });
                    return;
                }
                action.ManaPaymentOptions.Clear();
                action.ManaPaymentOptions.Add(chosen);
            }

            _log.LogInfo($"Submitting payment action: instance {instanceId} ability {source.AbilityGrpId} color {colorName ?? "-"}");
            payment.SubmitAction(action);
            cmd.SetResponse(new JObject
            {
                ["ok"] = true,
                ["submitted_type"] = "PaymentAction",
                ["instance_id"] = (int)instanceId,
            });
        }
    }
}
