using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using Newtonsoft.Json.Linq;

namespace MtgaCoachBridge
{
    /// <summary>
    /// The "reflect-1" protocol of the native-Mac library (spikes/mac-il2cpp/probe.cpp,
    /// run_batch and its Encoder), on Mono reflection. arenamcp/mac_bridge_adapter.py
    /// builds the draft, sealed, event, deck and survey commands on these ops, so the
    /// Windows/Proton client gets the same hands as the Mac. Ops: pending, find, static,
    /// get, call, set, new, expect, expect_pending. Results, handles, markers ($c, $h,
    /// $ref, $more, $unity, $struct, $n, $items, $none) and errors follow the probe.
    /// Main thread only. Unity access is injected so tests run without the game.
    /// </summary>
    internal sealed class ReflectBatch
    {
        public sealed class PendingResult
        {
            public object Request;
            public string Error = "";
        }

        private const BindingFlags AllDeclared = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public |
                                                 BindingFlags.NonPublic | BindingFlags.DeclaredOnly;
        private const ulong HandleGenerations = 64;

        private readonly Func<Type, object> _findSceneObject;
        private readonly Func<PendingResult> _findPending;
        private readonly Func<object, bool> _isUnityObject;

        private readonly Dictionary<int, HandleEntry> _handles = new Dictionary<int, HandleEntry>();
        private readonly Dictionary<object, int> _handleIds = new Dictionary<object, int>(ReferenceComparer.Instance);
        private int _nextHandle = 1;
        private ulong _generation;
        private Dictionary<string, Type> _classes;

        private sealed class HandleEntry
        {
            public object Object;
            public ulong Generation;
        }

        public ReflectBatch(Func<Type, object> findSceneObject, Func<PendingResult> findPending,
                            Func<object, bool> isUnityObject)
        {
            _findSceneObject = findSceneObject;
            _findPending = findPending;
            _isUnityObject = isUnityObject;
        }

        public ulong Generation => _generation;

        public JObject Run(JObject request)
        {
            var started = Stopwatch.StartNew();
            BeginGeneration();
            if (!(request["ops"] is JArray ops))
                return new JObject { ["ok"] = false, ["error"] = "reflect_batch needs an ops array" };
            var objects = new List<object>();
            var results = new JArray();
            for (int index = 0; index < ops.Count; index++)
            {
                var op = ops[index] as JObject ?? new JObject();
                string kind = Text(op["op"]);
                var skip = new HashSet<string>();
                if (op["skip"] is JArray names)
                    foreach (var name in names) skip.Add(Text(name));
                var encoder = new Encoder(this, (int)Int(op["max_nodes"], 4000), (int)Int(op["max_items"], 100), skip);
                int depth = (int)Int(op["depth"], 2);
                string error = "";
                JToken result = JValue.CreateNull();
                object obj = null;
                try
                {
                    switch (kind)
                    {
                        case "pending":
                        {
                            var pending = _findPending();
                            obj = pending.Request;
                            result = obj != null ? encoder.Object(obj, depth) : new JObject { ["$none"] = pending.Error };
                            break;
                        }
                        case "find":
                        case "static":
                        {
                            string className = Text(op["class"]);
                            var type = ClassByFullName(className);
                            if (type == null)
                            {
                                error = "class not found: " + className;
                            }
                            else if (kind == "find")
                            {
                                obj = _findSceneObject(type);
                                result = encoder.Object(obj, depth);
                            }
                            else
                            {
                                string member = Text(op["member"]);
                                var getter = FindMethod(type, "get_" + member, 0);
                                var field = FindField(type, member);
                                if (getter != null && getter.IsStatic)
                                {
                                    obj = Invoke(getter, null, new object[0], ref error);
                                    result = encoder.Object(obj, depth);
                                }
                                else if (field != null && field.IsStatic)
                                {
                                    obj = field.GetValue(null);
                                    result = encoder.Object(obj, depth);
                                }
                                else
                                {
                                    error = "no static member " + member;
                                }
                            }
                            break;
                        }
                        case "get":
                        case "call":
                        case "set":
                        case "expect":
                        case "expect_pending":
                        {
                            var target = ResolveTarget(op["target"], objects, ref error);
                            if (target == null) break;
                            if (kind == "expect") Expect(op, target, ref error);
                            else if (kind == "expect_pending") ExpectPending(target, ref error);
                            else if (kind == "get") obj = Get(op, target, encoder, depth, ref result, ref error);
                            else if (kind == "call") obj = Call(op, target, objects, encoder, depth, ref result, ref error);
                            else Set(op, target, objects, ref error);
                            break;
                        }
                        case "new":
                            obj = New(op, objects, encoder, depth, ref result, ref error);
                            break;
                        default:
                            error = "unknown op " + kind;
                            break;
                    }
                }
                catch (Exception exc)
                {
                    error = Describe(exc);
                }
                if (obj != null && obj.GetType().IsValueType) obj = null;  // value results are not refs
                if (error.Length > 0 && Flag(op["optional"]))
                {
                    error = "";  // speculative read: absent member or null ref yields null
                    result = JValue.CreateNull();
                    obj = null;
                }
                if (error.Length > 0)
                    return new JObject { ["ok"] = false, ["error"] = error, ["failed_op"] = index, ["results"] = results };
                results.Add(result);
                objects.Add(obj);
            }
            return new JObject
            {
                ["ok"] = true,
                ["generation"] = _generation,
                ["ms"] = Math.Round(started.Elapsed.TotalMilliseconds, 3),
                ["results"] = results,
            };
        }

        // -- ops -------------------------------------------------------------

        private void Expect(JObject op, object target, ref string error)
        {
            // {"class": "ShortName"} and/or {"member": "X", "equals": scalar}; enums
            // match by name (string) or value (number).
            var type = target.GetType();
            string wantedClass = Text(op["class"]);
            if (wantedClass.Length > 0 && wantedClass != type.Name)
            {
                error = "expected " + wantedClass + ", found " + type.Name;
                return;
            }
            var wanted = op["equals"];
            if (wanted == null) return;
            string member = Text(op["member"]);
            var shallow = new Encoder(this, 1, 1, new HashSet<string>());
            JToken actual;
            var getter = FindMethod(type, "get_" + member, 0);
            if (getter != null)
            {
                actual = shallow.Object(Invoke(getter, target, new object[0], ref error), 1);
            }
            else
            {
                var field = FindField(type, member);
                if (field == null)
                {
                    error = "no member " + member + " on " + ClassFullName(type);
                    return;
                }
                actual = shallow.Object(field.GetValue(target), 1);
            }
            if (error.Length > 0) return;
            var compare = actual;
            if (actual is JObject node) compare = node[wanted.Type == JTokenType.String ? "e" : "v"];
            if (!Same(compare, wanted)) error = "identity mismatch: " + member + " is " + actual.ToString(Newtonsoft.Json.Formatting.None);
        }

        private void ExpectPending(object target, ref string error)
        {
            var pending = _findPending();
            if (!ReferenceEquals(pending.Request, target))
                error = "stale: the pending request changed (now " + (pending.Request != null ? pending.Request.GetType().Name : "none") + ")";
        }

        private object Get(JObject op, object target, Encoder encoder, int depth, ref JToken result, ref string error)
        {
            string member = Text(op["member"]);
            var type = target.GetType();
            var getter = FindMethod(type, "get_" + member, 0);
            object value;
            if (getter != null)
            {
                value = Invoke(getter, target, new object[0], ref error);
            }
            else
            {
                var field = FindField(type, member);
                if (field == null)
                {
                    error = "no member " + member + " on " + ClassFullName(type);
                    return null;
                }
                value = field.GetValue(field.IsStatic ? null : target);
            }
            if (error.Length == 0) result = encoder.Object(value, depth);
            return value;
        }

        private object Call(JObject op, object target, List<object> objects, Encoder encoder, int depth,
                            ref JToken result, ref string error)
        {
            var args = op["args"] as JArray;
            var method = SelectMethod(target.GetType(), Text(op["method"]), args, op["sig"] as JArray, objects, ref error);
            if (method == null) return null;
            var values = Coerce(method.GetParameters(), args, objects, ref error);
            if (values == null) return null;
            var value = Invoke(method, method.IsStatic ? null : target, values, ref error);
            if (error.Length == 0) result = encoder.Object(value, depth);
            return value;
        }

        private void Set(JObject op, object target, List<object> objects, ref string error)
        {
            var value = op["value"];
            if (value == null)
            {
                error = "set needs a value";
                return;
            }
            var args = new JArray { value };
            var setter = SelectMethod(target.GetType(), "set_" + Text(op["member"]), args, null, objects, ref error);
            if (setter == null) return;
            error = "";
            var values = Coerce(setter.GetParameters(), args, objects, ref error);
            if (values != null) Invoke(setter, target, values, ref error);
        }

        private object New(JObject op, List<object> objects, Encoder encoder, int depth, ref JToken result, ref string error)
        {
            string className = Text(op["class"]);
            var type = ClassByFullName(className);
            if (type == null)
            {
                error = "class not found: " + className;
                return null;
            }
            var args = op["args"] as JArray;
            object created;
            if (type.IsValueType && (args == null || args.Count == 0))
            {
                created = Activator.CreateInstance(type);
            }
            else
            {
                var constructor = SelectMethod(type, ".ctor", args, op["sig"] as JArray, objects, ref error) as ConstructorInfo;
                if (constructor == null) return null;
                var values = Coerce(constructor.GetParameters(), args, objects, ref error);
                if (values == null) return null;
                try
                {
                    created = constructor.Invoke(values);
                }
                catch (TargetInvocationException exc)
                {
                    error = Describe(exc.InnerException ?? exc);
                    return null;
                }
            }
            result = encoder.Object(created, Math.Max(depth, 0));
            return created;
        }

        // -- handles and targets ---------------------------------------------

        internal int HandleFor(object obj)
        {
            if (_handleIds.TryGetValue(obj, out int known))
            {
                _handles[known].Generation = _generation;
                return known;
            }
            int id = _nextHandle++;
            _handles[id] = new HandleEntry { Object = obj, Generation = _generation };
            _handleIds[obj] = id;
            return id;
        }

        private void BeginGeneration()
        {
            _generation++;
            foreach (var id in _handles.Keys.ToList())
            {
                var entry = _handles[id];
                if (entry.Generation + HandleGenerations < _generation)
                {
                    _handleIds.Remove(entry.Object);
                    _handles.Remove(id);
                }
            }
        }

        private object ResolveTarget(JToken target, List<object> objects, ref string error)
        {
            if (!(target is JObject node))
            {
                error = "target must be {\"h\": id} or {\"ref\": op}";
                return null;
            }
            if (node["h"] != null)
            {
                if (_handles.TryGetValue((int)Int(node["h"], 0), out var entry)) return entry.Object;
                error = "unknown or expired handle";
                return null;
            }
            long index = Int(node["ref"], -1);
            if (index < 0 || index >= objects.Count || objects[(int)index] == null)
            {
                error = "ref does not name an object result";
                return null;
            }
            return objects[(int)index];
        }

        // -- arguments -------------------------------------------------------

        private object[] Coerce(ParameterInfo[] parameters, JArray args, List<object> objects, ref string error)
        {
            var values = new object[parameters.Length];
            for (int i = 0; i < parameters.Length; i++)
            {
                if (!CoerceOne(args[i], parameters[i].ParameterType, objects, out values[i], ref error)) return null;
            }
            return values;
        }

        private bool CoerceOne(JToken arg, Type type, List<object> objects, out object value, ref string error)
        {
            value = null;
            if (!(arg is JObject node))
            {
                error = "argument must be an object like {\"uint\": 5}";
                return false;
            }
            if (node["h"] != null || node["ref"] != null)
            {
                value = ResolveTarget(node, objects, ref error);
                return value != null;
            }
            if (node["null"] != null)
            {
                if (type.IsValueType)
                {
                    error = "null passed for value parameter " + TypeName(type);
                    return false;
                }
                return true;
            }
            if (node["str"] != null)
            {
                value = Text(node["str"]);
                return true;
            }
            if (node["list"] is JArray items)
            {
                value = BuildCollection(items, type, objects, ref error);
                return value != null;
            }
            if (node["dict"] is JArray pairs)
            {
                // {"dict": [[key, value], ...]} -> new Dictionary<K, V>() + Add(key, value)
                var add = type.IsValueType ? null : FindMethod(type, "Add", 2);
                var constructor = type.IsValueType ? null : type.GetConstructor(Type.EmptyTypes);
                if (add == null || constructor == null)
                {
                    error = "cannot build a dictionary for " + TypeName(type);
                    return false;
                }
                var dictionary = constructor.Invoke(new object[0]);
                var addParams = add.GetParameters();
                foreach (var pair in pairs)
                {
                    if (!(pair is JArray entry) || entry.Count != 2)
                    {
                        error = "dict entries must be [key, value]";
                        return false;
                    }
                    var kv = new object[2];
                    for (int side = 0; side < 2; side++)
                        if (!CoerceOne(entry[side], addParams[side].ParameterType, objects, out kv[side], ref error)) return false;
                    Invoke(add, dictionary, kv, ref error);
                    if (error.Length > 0) return false;
                }
                value = dictionary;
                return true;
            }
            if (!type.IsValueType)
            {
                error = "scalar passed for reference parameter " + TypeName(type);
                return false;
            }
            if (type.IsEnum && node["enum"] != null)
            {
                string name = Text(node["enum"]);
                var constant = type.GetField(name, BindingFlags.Public | BindingFlags.Static);
                if (constant == null)
                {
                    error = "no constant " + name + " on " + ClassFullName(type);
                    return false;
                }
                value = constant.GetValue(null);
                return true;
            }
            JToken number = null;
            foreach (var key in new[] { "bool", "int", "uint", "long", "ulong", "float", "double" })
                if ((number = node[key]) != null) break;
            if (number == null)
            {
                error = "unsupported argument for " + TypeName(type);
                return false;
            }
            double real = number.Type == JTokenType.Boolean ? ((bool)number ? 1 : 0) : (double)number;
            long whole = (long)real;
            var storage = type.IsEnum ? Enum.GetUnderlyingType(type) : type;
            object scalar;
            switch (Type.GetTypeCode(storage))
            {
                case TypeCode.Boolean: scalar = (byte)whole != 0; break;
                case TypeCode.SByte: scalar = unchecked((sbyte)whole); break;
                case TypeCode.Byte: scalar = unchecked((byte)whole); break;
                case TypeCode.Char: scalar = unchecked((char)whole); break;
                case TypeCode.Int16: scalar = unchecked((short)whole); break;
                case TypeCode.UInt16: scalar = unchecked((ushort)whole); break;
                case TypeCode.Int32: scalar = unchecked((int)whole); break;
                case TypeCode.UInt32: scalar = unchecked((uint)whole); break;
                case TypeCode.Int64: scalar = whole; break;
                case TypeCode.UInt64: scalar = unchecked((ulong)whole); break;
                case TypeCode.Single: scalar = (float)real; break;
                case TypeCode.Double: scalar = real; break;
                default:
                    error = "cannot pass a number as " + TypeName(type);
                    return false;
            }
            value = type.IsEnum ? Enum.ToObject(type, scalar) : scalar;
            return true;
        }

        // A list argument becomes whatever collection the parameter wants: an array for
        // T[] and IEnumerable<T>-style interfaces, or new+Add for List<T>/RepeatedField<T>.
        private object BuildCollection(JArray items, Type type, List<object> objects, ref string error)
        {
            Type element = null;
            if (type.IsArray)
            {
                element = type.GetElementType();
            }
            else if (type.IsInterface)
            {
                var generic = type.IsGenericType ? type.GetGenericArguments() : new Type[0];
                if (generic.Length != 1)
                {
                    error = "unsupported collection parameter " + TypeName(type);
                    return null;
                }
                element = generic[0];
            }
            if (element != null)
            {
                var array = Array.CreateInstance(element, items.Count);
                for (int i = 0; i < items.Count; i++)
                {
                    if (!CoerceOne(items[i], element, objects, out var item, ref error)) return null;
                    array.SetValue(item, i);
                }
                return array;
            }
            var constructor = type.GetConstructor(Type.EmptyTypes);
            MethodInfo add = null;
            for (var k = type; k != null && add == null; k = k.BaseType)
                add = k.GetMethods(AllDeclared).FirstOrDefault(m =>
                    m.Name == "Add" && !m.IsStatic && m.GetParameters().Length == 1 &&
                    !m.GetParameters()[0].ParameterType.IsInterface);  // not Add(IEnumerable<T>)
            if (constructor == null || add == null)
            {
                error = "cannot construct " + ClassFullName(type);
                return null;
            }
            var collection = constructor.Invoke(new object[0]);
            var itemType = add.GetParameters()[0].ParameterType;
            foreach (var entry in items)
            {
                if (!CoerceOne(entry, itemType, objects, out var item, ref error)) return null;
                Invoke(add, collection, new[] { item }, ref error);
                if (error.Length > 0) return null;
            }
            return collection;
        }

        private bool ArgumentFits(JToken arg, Type type, List<object> objects)
        {
            if (!(arg is JObject node)) return false;
            bool isValue = type.IsValueType;
            if (node["h"] != null || node["ref"] != null)
            {
                // Object arguments must really be assignable: RepeatedField<T> has both
                // Add(T) and Add(IEnumerable<T>), and passing a T to the second crashes.
                string ignored = "";
                var obj = ResolveTarget(node, objects, ref ignored);
                return obj != null && type.IsInstanceOfType(obj);
            }
            if (node["list"] != null)
                return !isValue && (type.IsArray || type.IsInterface || IsListClass(type) || type.Name.StartsWith("HashSet`1", StringComparison.Ordinal));
            if (node["dict"] != null) return !isValue && FindMethod(type, "Add", 2) != null;
            if (node["str"] != null) return type == typeof(string);
            if (node["enum"] != null) return type.IsEnum;
            if (node["null"] != null) return !isValue;
            return isValue;  // numbers and bools
        }

        // Most-derived overload first; `signature` (parameter type names) pins an overload.
        private MethodBase SelectMethod(Type type, string name, JArray args, JArray signature, List<object> objects,
                                        ref string error)
        {
            int argc = args?.Count ?? 0;
            var candidates = new List<MethodBase>();
            for (var k = type; k != null; k = k.BaseType)
            {
                IEnumerable<MethodBase> members = name == ".ctor"
                    ? (k == type ? k.GetConstructors(AllDeclared & ~BindingFlags.Static) : new ConstructorInfo[0])
                    : k.GetMethods(AllDeclared).Where(m => m.Name == name && !m.IsGenericMethodDefinition);
                foreach (var method in members)
                {
                    var parameters = method.GetParameters();
                    if (parameters.Length != argc) continue;
                    if (signature != null)
                    {
                        bool same = signature.Count == argc;
                        for (int i = 0; same && i < argc; i++)
                            same = TypeName(parameters[i].ParameterType) == Text(signature[i]);
                        if (!same) continue;
                    }
                    candidates.Add(method);
                }
            }
            foreach (var method in candidates)
            {
                var parameters = method.GetParameters();
                bool fits = true;
                for (int i = 0; fits && i < argc; i++) fits = ArgumentFits(args[i], parameters[i].ParameterType, objects);
                if (fits) return method;
            }
            error = candidates.Count == 0
                ? "no method " + name + "/" + argc + " on " + ClassFullName(type)
                : "no overload of " + name + " accepts these arguments";
            return null;
        }

        // -- reflection helpers ----------------------------------------------

        /// Walks base classes; skips open generic methods (probe find_method).
        internal static MethodInfo FindMethod(Type type, string name, int argc)
        {
            for (var k = type; k != null; k = k.BaseType)
                foreach (var method in k.GetMethods(AllDeclared))
                    if (method.Name == name && !method.IsGenericMethodDefinition && (argc < 0 || method.GetParameters().Length == argc))
                        return method;
            return null;
        }

        internal static FieldInfo FindField(Type type, string name)
        {
            for (var k = type; k != null; k = k.BaseType)
            {
                var field = k.GetField(name, AllDeclared);
                if (field != null) return field;
            }
            return null;
        }

        private static object Invoke(MethodBase method, object target, object[] args, ref string error)
        {
            try
            {
                return method.Invoke(method.IsStatic ? null : target, args);
            }
            catch (TargetInvocationException exc)
            {
                error = Describe(exc.InnerException ?? exc);
                return null;
            }
        }

        private static string Describe(Exception exc) => exc.GetType().FullName + ": " + exc.Message;

        /// probe class_by_full_name: "Ns.Name", or a bare name in the global namespace.
        /// Top-level classes only, like il2cpp_class_from_name.
        internal Type ClassByFullName(string fullName)
        {
            if (_classes == null)
            {
                var classes = new Dictionary<string, Type>();
                foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
                {
                    Type[] types;
                    try
                    {
                        types = assembly.GetTypes();
                    }
                    catch (ReflectionTypeLoadException exc)
                    {
                        types = exc.Types;
                    }
                    catch (Exception)
                    {
                        continue;
                    }
                    foreach (var type in types)
                    {
                        if (type == null || type.IsNested) continue;
                        string key = string.IsNullOrEmpty(type.Namespace) ? type.Name : type.Namespace + "." + type.Name;
                        if (!classes.ContainsKey(key)) classes[key] = type;
                    }
                }
                _classes = classes;
            }
            return _classes.TryGetValue(fullName ?? "", out var found) ? found : null;
        }

        internal static string ClassFullName(Type type) =>
            type == null ? "null" : string.IsNullOrEmpty(type.Namespace) ? type.Name : type.Namespace + "." + type.Name;

        /// il2cpp_type_get_name (IL format): "Ns.Outer.Inner", "Ns.List<Ns.T>", "T[]".
        internal static string TypeName(Type type)
        {
            if (type == null) return "?";
            if (type.IsByRef) return TypeName(type.GetElementType()) + "&";
            if (type.IsArray)
            {
                int rank = type.GetArrayRank();
                return TypeName(type.GetElementType()) + "[" + new string(',', rank - 1) + "]";
            }
            if (type.IsGenericParameter) return type.Name;
            string name = type.Name;
            int tick = name.IndexOf('`');
            if (tick >= 0) name = name.Substring(0, tick);
            string prefix = type.IsNested ? TypeName(type.DeclaringType.IsGenericType
                                                ? type.DeclaringType.GetGenericTypeDefinition()
                                                : type.DeclaringType) + "."
                          : string.IsNullOrEmpty(type.Namespace) ? "" : type.Namespace + ".";
            if (!type.IsGenericType) return prefix + name;
            var arguments = type.GetGenericArguments();
            if (type.IsNested && type.DeclaringType.IsGenericType)
                arguments = arguments.Skip(type.DeclaringType.GetGenericArguments().Length).ToArray();
            if (arguments.Length == 0) return prefix + name;
            return prefix + name + "<" + string.Join(",", arguments.Select(TypeName)) + ">";
        }

        internal static bool IsListClass(Type type)
        {
            string name = type.Name;
            return name.StartsWith("List`1", StringComparison.Ordinal) || name.StartsWith("RepeatedField`1", StringComparison.Ordinal) ||
                   name.StartsWith("ReadOnlyCollection`1", StringComparison.Ordinal) || name.StartsWith("Collection`1", StringComparison.Ordinal);
        }

        internal bool IsUnityObject(object obj) => _isUnityObject != null && _isUnityObject(obj);

        private static bool Same(JToken actual, JToken wanted)
        {
            if (actual == null) return false;
            switch (wanted.Type)
            {
                case JTokenType.String: return actual.Type == JTokenType.String && (string)actual == (string)wanted;
                case JTokenType.Integer:
                case JTokenType.Float:
                    return (actual.Type == JTokenType.Integer || actual.Type == JTokenType.Float) && (double)actual == (double)wanted;
                case JTokenType.Boolean: return actual.Type == JTokenType.Boolean && (bool)actual == (bool)wanted;
                default: return actual.Type == wanted.Type;
            }
        }

        private static string Text(JToken token) => token == null || token.Type == JTokenType.Null ? "" : token.ToString();

        private static long Int(JToken token, long fallback) =>
            token != null && (token.Type == JTokenType.Integer || token.Type == JTokenType.Float) ? (long)(double)token : fallback;

        private static bool Flag(JToken token) => token != null && token.Type == JTokenType.Boolean && (bool)token;

        private sealed class ReferenceComparer : IEqualityComparer<object>
        {
            public static readonly ReferenceComparer Instance = new ReferenceComparer();
            public new bool Equals(object x, object y) => ReferenceEquals(x, y);
            public int GetHashCode(object obj) => RuntimeHelpers.GetHashCode(obj);
        }

        // -- encoder ---------------------------------------------------------

        /// Snapshot nodes: {"$c": type, "$h": handle, field: value, ...}; lists and
        /// arrays carry "$n" and "$items"; enums are {"e": name, "v": number}.
        private sealed class Encoder
        {
            private readonly ReflectBatch _owner;
            private readonly int _maxNodes;
            private readonly int _maxItems;
            private readonly HashSet<string> _skip;
            private readonly HashSet<object> _seen = new HashSet<object>(ReferenceComparer.Instance);
            private int _nodes;

            public Encoder(ReflectBatch owner, int maxNodes, int maxItems, HashSet<string> skip)
            {
                _owner = owner;
                _maxNodes = maxNodes;
                _maxItems = maxItems;
                _skip = skip;
            }

            public JToken Object(object obj, int depth)
            {
                if (obj == null) return JValue.CreateNull();
                var type = obj.GetType();
                if (obj is string text) return text;
                if (type.IsValueType) return Value(obj, depth);
                var node = new JObject { ["$c"] = TypeName(type), ["$h"] = _owner.HandleFor(obj) };
                if (!_seen.Add(obj))
                {
                    node["$ref"] = true;
                    return node;
                }
                if (depth <= 0 || ++_nodes > _maxNodes)
                {
                    node["$more"] = true;
                    return node;
                }
                if (_owner.IsUnityObject(obj))
                {
                    node["$unity"] = true;
                    return node;
                }
                if (type.IsArray) Items(node, (Array)obj, depth);
                else if (IsListClass(type)) ListItems(node, obj, type, depth);
                else Fields(node, obj, type, depth);
                return node;
            }

            private JToken Value(object value, int depth)
            {
                var type = value.GetType();
                if (type.IsEnum)
                    return new JObject { ["e"] = value.ToString(), ["v"] = Convert.ToInt64(value, CultureInfo.InvariantCulture) };
                switch (Type.GetTypeCode(type))
                {
                    case TypeCode.Boolean: return (bool)value;
                    case TypeCode.Char: return (int)(char)value;
                    case TypeCode.SByte: case TypeCode.Byte: case TypeCode.Int16: case TypeCode.UInt16:
                    case TypeCode.Int32: case TypeCode.UInt32: case TypeCode.Int64:
                        return Convert.ToInt64(value, CultureInfo.InvariantCulture);
                    case TypeCode.UInt64: return (ulong)value;
                    case TypeCode.Single: return (double)(float)value;
                    case TypeCode.Double: return (double)value;
                }
                if (type == typeof(IntPtr) || type == typeof(UIntPtr) || type.IsPointer) return JValue.CreateNull();
                var node = new JObject { ["$c"] = TypeName(type), ["$struct"] = true };
                if (depth <= 0)
                {
                    node["$more"] = true;
                    return node;
                }
                Fields(node, value, type, depth);
                return node;
            }

            private void Fields(JObject node, object obj, Type type, int depth)
            {
                for (var k = type; k != null && k != typeof(object) && k != typeof(ValueType) && k != typeof(Enum); k = k.BaseType)
                {
                    foreach (var field in k.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                    {
                        string name = CleanFieldName(field.Name);
                        if (name.Length == 0 || name[0] == '<' || name == "_parser" || name == "_unknownFields" || _skip.Contains(name))
                            continue;
                        if (typeof(Delegate).IsAssignableFrom(field.FieldType)) continue;
                        if (field.FieldType.IsPointer) continue;
                        JToken value;
                        try
                        {
                            value = Object(field.GetValue(obj), depth - 1);
                        }
                        catch (Exception)
                        {
                            value = JValue.CreateNull();
                        }
                        node[name] = value;
                    }
                }
            }

            private void ListItems(JObject node, object list, Type type, int depth)
            {
                int count = 0;
                var countGetter = FindMethod(type, "get_Count", 0);
                var itemGetter = type.GetMethod("get_Item", new[] { typeof(int) });
                try
                {
                    if (countGetter != null) count = Math.Max(0, (int)countGetter.Invoke(list, new object[0]));
                }
                catch (Exception)
                {
                    count = 0;
                }
                var items = new JArray();
                int shown = itemGetter != null ? Math.Min(count, _maxItems) : 0;
                for (int i = 0; i < shown; i++)
                {
                    object item;
                    try
                    {
                        item = itemGetter.Invoke(list, new object[] { i });
                    }
                    catch (Exception)
                    {
                        item = null;
                    }
                    items.Add(Object(item, depth - 1));
                }
                node["$n"] = count;
                node["$items"] = items;
            }

            private void Items(JObject node, Array array, int depth)
            {
                var items = new JArray();
                int shown = Math.Min(array.Length, _maxItems);
                for (int i = 0; i < shown; i++) items.Add(Object(array.GetValue(i), depth - 1));
                node["$n"] = array.Length;
                node["$items"] = items;
            }

            private static string CleanFieldName(string raw)
            {
                const string suffix = ">k__BackingField";
                if (raw.Length > 18 && raw[0] == '<' && raw.EndsWith(suffix, StringComparison.Ordinal))
                    return raw.Substring(1, raw.Length - 1 - suffix.Length);
                return raw;
            }
        }
    }
}
