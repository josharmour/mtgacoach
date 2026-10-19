using GreClient.Rules;
using Newtonsoft.Json.Linq;
using Wotc.Mtgo.Gre.External.Messaging;

namespace MtgaCoachBridge
{
    public partial class Plugin
    {
        // 2026-10-18 22:30-22:31 (Linux/Proton): the plugin set every attacker's
        // recipient to LegalDamageRecipients[0], so "both at the opponent" went
        // out as Gallia -> Jace, a mixed declaration the server rejected
        // (FailureReason_InvalidDamageRecipient); the empty finalize that
        // followed passed with no attackers. Attackers now get the recipient
        // Python asked for, and the pending request reports what is selected.

        private static JObject DamageRecipientJson(DamageRecipient recipient)
        {
            var json = new JObject { ["type"] = recipient.Type.ToString() };
            switch (recipient.IdCase)
            {
                case DamageRecipient.IdOneofCase.PlayerSystemSeatId:
                    json["playerSystemSeatId"] = (int)recipient.PlayerSystemSeatId;
                    break;
                case DamageRecipient.IdOneofCase.PlaneswalkerInstanceId:
                    json["planeswalkerInstanceId"] = (int)recipient.PlaneswalkerInstanceId;
                    break;
                case DamageRecipient.IdOneofCase.TeamId:
                    json["teamId"] = (int)recipient.TeamId;
                    break;
            }
            return json;
        }

        /// The legal recipient matching Python's {"playerSystemSeatId" | "planeswalkerInstanceId" | "teamId"};
        /// the first legal one when none was named (older coaches); null when the named one is not legal.
        private static DamageRecipient MatchDamageRecipient(Attacker attacker, JObject wanted)
        {
            if (wanted == null)
                return attacker.LegalDamageRecipients[0];
            foreach (var recipient in attacker.LegalDamageRecipients)
            {
                switch (recipient.IdCase)
                {
                    case DamageRecipient.IdOneofCase.PlayerSystemSeatId
                        when wanted.Value<int?>("playerSystemSeatId") == (int)recipient.PlayerSystemSeatId:
                    case DamageRecipient.IdOneofCase.PlaneswalkerInstanceId
                        when wanted.Value<int?>("planeswalkerInstanceId") == (int)recipient.PlaneswalkerInstanceId:
                    case DamageRecipient.IdOneofCase.TeamId
                        when wanted.Value<int?>("teamId") == (int)recipient.TeamId:
                        return recipient;
                }
            }
            return null;
        }

        private static JToken SelectedDamageRecipientJson(DeclareAttackerRequest request, uint attackerInstanceId)
        {
            foreach (var declared in request.Attackers)
            {
                if (declared.AttackerInstanceId == attackerInstanceId && declared.SelectedDamageRecipient != null)
                    return DamageRecipientJson(declared.SelectedDamageRecipient);
            }
            return JValue.CreateNull();
        }
    }
}
