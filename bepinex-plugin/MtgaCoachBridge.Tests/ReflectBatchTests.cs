using System;
using System.Collections.Generic;
using System.Linq;
using MtgaCoachBridge;
using Newtonsoft.Json.Linq;
using Xunit;

// Global-namespace class: the probe resolves a bare name only in the global namespace.
public class ReflectGlobalScreen
{
    public int Opened = 3;
}

namespace ReflectFakes
{
    public enum Phase
    {
        Beginning = 0,
        Main1 = 2,
    }

    public struct Point
    {
        public int X;
        public int Y;
    }

    public class Card
    {
        public uint GrpId;
        public Card(uint grpId) { GrpId = grpId; }
        public Card() { }
    }

    public class Request
    {
        public Phase Phase = Phase.Main1;
        public string Name { get; set; } = "ActionsAvailableRequest";
        public List<Card> Cards = new List<Card> { new Card(1), new Card(2), new Card(3) };
        public Card[] Hand = { new Card(7) };
        public Point Where = new Point { X = 4, Y = 5 };
        public Action OnSubmit = () => { };
        public Request Self;
        public Card Shared = new Card(9);
        public Card SharedAgain;
        public object _unknownFields = "skipped";
        public int Count { get; set; } = 1;
        public static Request Instance = new Request();
        public static string Static => "static-value";

        public List<object> Calls = new List<object>();

        public string Echo(uint value) { Calls.Add(value); return "uint"; }
        public string Echo(string value) { Calls.Add(value); return "string"; }
        public string Pick(Phase phase) { Calls.Add(phase); return phase.ToString(); }
        public int Sum(IEnumerable<uint> values) => (int)values.Sum(v => v);
        public int Total(List<Card> cards) => cards.Sum(c => (int)c.GrpId);
        public int Length(uint[] values) => values.Length;
        public int Lookup(Dictionary<string, int> map) => map["a"];
        public uint Grp(Card card) => card.GrpId;
        public void Fail() => throw new InvalidOperationException("boom");
        public string Pinned(int value) => "int";
        public string Pinned(long value) => "long";

        public Request()
        {
            SharedAgain = Shared;
        }
    }

    public class Derived : Request
    {
        public new string Echo(uint value) => "derived";
    }

    public class Bag
    {
        public readonly List<Card> Items = new List<Card>();
        public void Add(Card card) => Items.Add(card);
        public void Add(IEnumerable<Card> cards) => throw new InvalidOperationException("wrong overload");
    }

    public class Outer
    {
        public class Inner { }
    }
}

namespace MtgaCoachBridge.Tests
{
    using ReflectFakes;

    public class ReflectBatchTests
    {
        private readonly ReflectFakes.Request _request = new ReflectFakes.Request();
        private object _pending;
        private readonly Dictionary<Type, object> _scene = new Dictionary<Type, object>();
        private readonly ReflectBatch _batch;

        public ReflectBatchTests()
        {
            _pending = _request;
            _batch = new ReflectBatch(
                type => _scene.TryGetValue(type, out var found) ? found : null,
                () => new ReflectBatch.PendingResult { Request = _pending, Error = _pending == null ? "no GameManager in the scene (not in a match)" : "" },
                obj => obj is Outer);
        }

        private JObject Run(params JObject[] ops) => _batch.Run(new JObject { ["ops"] = new JArray(ops) });

        private static JObject Op(string op, object fields = null)
        {
            var node = fields == null ? new JObject() : JObject.FromObject(fields);
            node["op"] = op;
            return node;
        }

        private static JObject Ref(int index) => new JObject { ["ref"] = index };

        private static JToken Result(JObject response, int index)
        {
            Assert.True((bool)response["ok"], response.ToString());
            return response["results"][index];
        }

        [Fact]
        public void PendingEncodesClassHandleFieldsAndMarkers()
        {
            var response = Run(new JObject { ["op"] = "pending", ["depth"] = 2, ["skip"] = new JArray("Calls") });
            var node = (JObject)Result(response, 0);
            Assert.Equal("ReflectFakes.Request", (string)node["$c"]);
            Assert.True((int)node["$h"] > 0);
            Assert.Equal("ActionsAvailableRequest", (string)node["Name"]);  // backing field cleaned
            Assert.Equal(1, (int)node["Count"]);
            Assert.Equal(new JObject { ["e"] = "Main1", ["v"] = 2 }, node["Phase"]);
            Assert.Null(node["OnSubmit"]);          // delegates skipped
            Assert.Null(node["_unknownFields"]);    // protobuf noise skipped
            Assert.Null(node["Calls"]);             // op "skip"
            Assert.Equal(JTokenType.Null, node["Self"].Type);
            var where = (JObject)node["Where"];
            Assert.True((bool)where["$struct"]);
            Assert.Equal("ReflectFakes.Point", (string)where["$c"]);
            Assert.Equal(4, (int)where["X"]);
        }

        [Fact]
        public void ListsAndArraysCarryCountAndItems()
        {
            var node = (JObject)Result(Run(new JObject { ["op"] = "pending", ["depth"] = 3, ["max_items"] = 2 }), 0);
            var cards = (JObject)node["Cards"];
            Assert.Equal("System.Collections.Generic.List<ReflectFakes.Card>", (string)cards["$c"]);
            Assert.Equal(3, (int)cards["$n"]);
            Assert.Equal(2, ((JArray)cards["$items"]).Count);
            Assert.Equal(1, (int)cards["$items"][0]["GrpId"]);
            var hand = (JObject)node["Hand"];
            Assert.Equal("ReflectFakes.Card[]", (string)hand["$c"]);
            Assert.Equal(1, (int)hand["$n"]);
            Assert.Equal(7, (int)hand["$items"][0]["GrpId"]);
        }

        [Fact]
        public void DepthLimitAndRepeatsCollapse()
        {
            var node = (JObject)Result(Run(new JObject { ["op"] = "pending", ["depth"] = 1 }), 0);
            Assert.True((bool)node["Shared"]["$more"]);
            Assert.True((bool)node["SharedAgain"]["$ref"]);
            Assert.Equal((int)node["Shared"]["$h"], (int)node["SharedAgain"]["$h"]);
        }

        [Fact]
        public void UnityObjectsStopTheWalk()
        {
            _scene[typeof(Outer)] = new Outer();
            var response = _batch.Run(new JObject { ["ops"] = new JArray(new JObject { ["op"] = "find", ["class"] = "ReflectFakes.Outer", ["depth"] = 3 }) });
            Assert.True((bool)Result(response, 0)["$unity"]);
        }

        [Fact]
        public void GetCallSetChainThroughRefs()
        {
            var response = Run(
                Op("pending", new { depth = 0 }),
                Op("get", new { target = Ref(0), member = "Cards", depth = 0 }),
                Op("call", new { target = Ref(1), method = "get_Item", args = new[] { new { @int = 1 } }, depth = 1 }),
                Op("set", new { target = Ref(0), member = "Count", value = new { @int = 5 } }),
                Op("get", new { target = Ref(0), member = "Count" }));
            Assert.Equal(2, (int)Result(response, 2)["GrpId"]);
            Assert.Equal(JTokenType.Null, Result(response, 3).Type);
            Assert.Equal(5, (int)Result(response, 4));
            Assert.Equal(5, _request.Count);
        }

        [Fact]
        public void HandlesResolveInLaterBatches()
        {
            int handle = (int)Result(Run(Op("pending", new { depth = 0 })), 0)["$h"];
            var response = Run(Op("get", new { target = new { h = handle }, member = "Name" }));
            Assert.Equal("ActionsAvailableRequest", (string)Result(response, 0));
        }

        [Fact]
        public void HandlesExpireAfterSixtyFourUnusedGenerations()
        {
            int handle = (int)Result(Run(Op("pending", new { depth = 0 })), 0)["$h"];
            _pending = null;
            for (int i = 0; i < 66; i++) Run(Op("static", new { @class = "ReflectFakes.Request", member = "Static" }));
            var response = Run(Op("get", new { target = new { h = handle }, member = "Name" }));
            Assert.False((bool)response["ok"]);
            Assert.Equal("unknown or expired handle", (string)response["error"]);
            Assert.Equal(0, (int)response["failed_op"]);
        }

        [Fact]
        public void OverloadsFollowArgumentKinds()
        {
            var response = Run(
                Op("pending", new { depth = 0 }),
                Op("call", new { target = Ref(0), method = "Echo", args = new[] { new { @uint = 5 } } }),
                Op("call", new { target = Ref(0), method = "Echo", args = new[] { new { str = "x" } } }),
                Op("call", new { target = Ref(0), method = "Pick", args = new[] { new { @enum = "Beginning" } } }),
                Op("call", new { target = Ref(0), method = "Pinned", args = new[] { new { @int = 1 } }, sig = new[] { "System.Int64" } }));
            Assert.Equal("uint", (string)Result(response, 1));
            Assert.Equal("string", (string)Result(response, 2));
            Assert.Equal("Beginning", (string)Result(response, 3));
            Assert.Equal("long", (string)Result(response, 4));
            Assert.Equal((uint)5, _request.Calls[0]);
        }

        [Fact]
        public void MostDerivedOverloadWins()
        {
            _pending = new Derived();
            var response = Run(Op("pending", new { depth = 0 }), Op("call", new { target = Ref(0), method = "Echo", args = new[] { new { @uint = 1 } } }));
            Assert.Equal("derived", (string)Result(response, 1));
        }

        [Fact]
        public void ListArgumentsBuildTheParameterCollection()
        {
            var cards = new JArray(
                new JObject { ["op"] = "new", ["class"] = "ReflectFakes.Card", ["args"] = new JArray(new JObject { ["uint"] = 4 }), ["depth"] = 0 },
                new JObject { ["op"] = "new", ["class"] = "ReflectFakes.Card", ["args"] = new JArray(new JObject { ["uint"] = 6 }), ["depth"] = 0 });
            var response = Run(
                (JObject)cards[0], (JObject)cards[1],
                Op("pending", new { depth = 0 }),
                Op("call", new { target = Ref(2), method = "Total", args = new[] { new { list = new[] { Ref(0), Ref(1) } } } }),
                Op("call", new { target = Ref(2), method = "Sum", args = new[] { new { list = new[] { new { @uint = 2 }, new { @uint = 3 } } } } }),
                Op("call", new { target = Ref(2), method = "Length", args = new[] { new { list = new[] { new { @uint = 2 } } } } }),
                Op("call", new { target = Ref(2), method = "Lookup", args = new[] { new { dict = new object[] { new object[] { new { str = "a" }, new { @int = 8 } } } } } }));
            Assert.Equal(10, (int)Result(response, 3));
            Assert.Equal(5, (int)Result(response, 4));
            Assert.Equal(1, (int)Result(response, 5));
            Assert.Equal(8, (int)Result(response, 6));
        }

        [Fact]
        public void AddPicksTheElementOverloadForObjects()
        {
            var response = Run(
                Op("new", new { @class = "ReflectFakes.Bag", depth = 0 }),
                Op("new", new { @class = "ReflectFakes.Card", args = new[] { new { @uint = 2 } }, depth = 0 }),
                Op("call", new { target = Ref(0), method = "Add", args = new[] { Ref(1) } }),
                Op("get", new { target = Ref(0), member = "Items", depth = 2 }));
            Assert.Equal(1, (int)Result(response, 3)["$n"]);
        }

        [Fact]
        public void ExpectChecksClassAndMembers()
        {
            Assert.True((bool)Run(Op("pending", new { depth = 0 }), Op("expect", new { target = Ref(0), @class = "Request" }))["ok"]);
            Assert.True((bool)Run(Op("pending", new { depth = 0 }), Op("expect", new { target = Ref(0), member = "Phase", equals = "Main1" }))["ok"]);
            Assert.True((bool)Run(Op("pending", new { depth = 0 }), Op("expect", new { target = Ref(0), member = "Phase", equals = 2 }))["ok"]);
            var wrongClass = Run(Op("pending", new { depth = 0 }), Op("expect", new { target = Ref(0), @class = "Card" }));
            Assert.Equal("expected Card, found Request", (string)wrongClass["error"]);
            var wrongValue = Run(Op("pending", new { depth = 0 }), Op("expect", new { target = Ref(0), member = "Count", equals = 4 }));
            Assert.StartsWith("identity mismatch: Count is 1", (string)wrongValue["error"]);
        }

        [Fact]
        public void ExpectPendingDetectsAStaleRequest()
        {
            int handle = (int)Result(Run(Op("pending", new { depth = 0 })), 0)["$h"];
            Assert.True((bool)Run(Op("expect_pending", new { target = new { h = handle } }))["ok"]);
            _pending = new Derived();
            var stale = Run(Op("expect_pending", new { target = new { h = handle } }));
            Assert.Equal("stale: the pending request changed (now Derived)", (string)stale["error"]);
        }

        [Fact]
        public void NoPendingRequestIsReportedInline()
        {
            _pending = null;
            var node = Result(Run(Op("pending")), 0);
            Assert.Equal("no GameManager in the scene (not in a match)", (string)node["$none"]);
        }

        [Fact]
        public void FindAndStaticResolveClassesByFullName()
        {
            _scene[typeof(ReflectGlobalScreen)] = new ReflectGlobalScreen();
            var response = Run(
                Op("find", new { @class = "ReflectGlobalScreen", depth = 1 }),
                Op("static", new { @class = "ReflectFakes.Request", member = "Instance", depth = 0 }),
                Op("static", new { @class = "ReflectFakes.Request", member = "Static" }));
            Assert.Equal(3, (int)Result(response, 0)["Opened"]);
            Assert.Equal("ReflectFakes.Request", (string)Result(response, 1)["$c"]);
            Assert.Equal("static-value", (string)Result(response, 2));
        }

        [Fact]
        public void OptionalOpsYieldNullAndErrorsStopTheBatch()
        {
            var response = Run(
                Op("find", new { @class = "NoSuchScreen", optional = true }),
                Op("pending", new { depth = 0 }),
                Op("get", new { target = Ref(0), member = "X", optional = true }),
                Op("call", new { target = Ref(1), method = "Fail" }),
                Op("pending"));
            Assert.False((bool)response["ok"]);
            Assert.Equal(3, (int)response["failed_op"]);
            Assert.Equal("System.InvalidOperationException: boom", (string)response["error"]);
            Assert.Equal(3, ((JArray)response["results"]).Count);
            Assert.Equal(JTokenType.Null, response["results"][0].Type);
            Assert.Equal(JTokenType.Null, response["results"][2].Type);
        }

        [Fact]
        public void ErrorsNameTheMissingPiece()
        {
            Assert.Equal("class not found: Nope", (string)Run(Op("find", new { @class = "Nope" }))["error"]);
            Assert.Equal("no member Nope on ReflectFakes.Request",
                         (string)Run(Op("pending", new { depth = 0 }), Op("get", new { target = Ref(0), member = "Nope" }))["error"]);
            Assert.Equal("no method Nope/0 on ReflectFakes.Request",
                         (string)Run(Op("pending", new { depth = 0 }), Op("call", new { target = Ref(0), method = "Nope" }))["error"]);
            Assert.Equal("no overload of Grp accepts these arguments",
                         (string)Run(Op("pending", new { depth = 0 }), Op("call", new { target = Ref(0), method = "Grp", args = new[] { new { @uint = 1 } } }))["error"]);
            Assert.Equal("ref does not name an object result",
                         (string)Run(Op("pending", new { depth = 0 }), Op("get", new { target = Ref(0), member = "Count" }), Op("get", new { target = Ref(1), member = "X" }))["error"]);
            Assert.Equal("unknown op nope", (string)Run(Op("nope"))["error"]);
        }

        [Fact]
        public void TypeNamesUseIl2CppFormat()
        {
            Assert.Equal("ReflectFakes.Outer.Inner", ReflectBatch.TypeName(typeof(Outer.Inner)));
            Assert.Equal("System.Collections.Generic.Dictionary<System.String,System.Int32>", ReflectBatch.TypeName(typeof(Dictionary<string, int>)));
            Assert.Equal("System.UInt32[]", ReflectBatch.TypeName(typeof(uint[])));
            Assert.Equal("ReflectGlobalScreen", ReflectBatch.TypeName(typeof(ReflectGlobalScreen)));
        }
    }
}
