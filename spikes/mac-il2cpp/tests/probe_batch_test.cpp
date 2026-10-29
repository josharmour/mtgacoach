// Native regression tests: include the production translation unit so these
// execute its parser, run_batch, overload selection, coercion and GC retention.
// Only the IL2CPP API boundary is mocked. No sockets, game loading or hooks run.
// macOS: clang++ -std=c++17 -O1 -g -Wno-unused-function \
//   spikes/mac-il2cpp/tests/probe_batch_test.cpp -o /tmp/probe_batch_test
//   /tmp/probe_batch_test
#define MTGACOACH_PROBE_TEST 1
#include "../probe.cpp"

#include <array>
#include <iostream>
#include <stdexcept>

#define CHECK(condition) do { \
    if (!(condition)) throw std::runtime_error( \
        std::string(__FILE__) + ":" + std::to_string(__LINE__) + ": " #condition); \
} while (false)

namespace mock {
struct Field;
struct Method;
struct Class {
    const char* ns;
    const char* name;
    int kind;
    bool value = false;
    size_t size = sizeof(void*);
    Class* parent = nullptr;
    std::vector<Field*> fields{};
    std::vector<Method*> methods{};
};
struct Field {
    const char* name;
    Class* type;
    size_t offset;
    int flags = 0x0006;  // public instance
};
struct Method {
    const char* name;
    std::vector<Class*> params;
    std::function<void*(void*, void**, void**)> call;
    bool instance = true;
};
int monitor_token;
struct Object {
    Class* klass;
    void* monitor = &monitor_token;
    alignas(16) std::array<uint8_t, 64> data{};
    std::u16string text{};
};
static_assert(offsetof(Object, data) == 16, "installed IL2CPP has a 16-byte object header");

Class string_class{"System", "String", kTypeString};
Class int_class{"System", "Int32", kTypeI4, true, 4};
Class uint_class{"System", "UInt32", kTypeU4, true, 4};
Class guid_class{"System", "Guid", kTypeValueType, true, 16};
Class value_base_class{"System", "ValueType", kTypeClass};
Class card_class{"", "Client_DeckCard", kTypeValueType, true, 8, &value_base_class};
Class cards_class{"Test", "Cards", kTypeClass};
Class base_class{"Test", "SummaryBase", kTypeClass};
Class summary_class{"", "Client_DeckSummary", kTypeClass, false, sizeof(void*), &base_class};
Class deck_class{"Test", "Deck", kTypeClass};
Field summary_field{"Summary", &summary_class, 0};
Field name_field{"Name", &string_class, 0};
Field id_field{"DeckId", &guid_class, 16};
Field count_field{"Count", &int_class, 32};
Field inherited_field{"Inherited", &string_class, 40};
Field static_field{"StaticName", &string_class, 0, 0x0006 | kFieldStatic};
Field readonly_field{"ReadonlyName", &string_class, 0, 0x0006 | kFieldInitOnly};
Field literal_field{"ConstantName", &string_class, 0, 0x0006 | kFieldLiteral};
Field card_id_field{"Id", &uint_class, 0, 0x0006 | kFieldInitOnly};
Field card_quantity_field{"Quantity", &uint_class, 4, 0x0006 | kFieldInitOnly};

std::vector<Class*> classes{&string_class, &int_class, &uint_class, &guid_class,
                           &value_base_class, &card_class, &cards_class,
                           &base_class, &summary_class, &deck_class};

std::vector<std::unique_ptr<Object>> objects;
std::unordered_map<void*, Class*> allocated_classes;
std::unordered_map<uintptr_t, void*> roots;
uintptr_t next_root = 1;
int field_writes = 0;
int setter_calls = 0;
int guid_calls = 0;
Object* summary = nullptr;
uint32_t deck_handle = 0;
std::array<uint8_t, 16> expected_guid{};

Object* make(Class* klass) {
    objects.emplace_back(new Object{klass});
    allocated_classes[objects.back().get()] = klass;
    return objects.back().get();
}

bool is_object(void* ptr) {
    return std::any_of(objects.begin(), objects.end(),
                       [ptr](const auto& obj) { return obj.get() == ptr; });
}

Object* checked_object(void* ptr) {
    CHECK(is_object(ptr));
    auto* obj = static_cast<Object*>(ptr);
    // Read the real header and compare against allocation-time identity. Never
    // synthesize the class from the registry or silently unbox a ctor target.
    CHECK(obj->klass == allocated_classes.at(ptr));
    CHECK(obj->monitor == &monitor_token);
    return obj;
}

template <typename T>
void* next(const std::vector<T*>& entries, void** iter) {
    size_t index = reinterpret_cast<uintptr_t>(*iter);
    if (index >= entries.size()) return nullptr;
    *iter = reinterpret_cast<void*>(index + 1);
    return entries[index];
}

void install() {
    // An unexpected API call fails rather than quietly returning fake success.
#define UNEXPECTED_API(ret, name, params) \
    name = +[] params -> ret { throw std::runtime_error("unexpected API: " #name); };
    IL2CPP_FUNCS(UNEXPECTED_API)
#undef UNEXPECTED_API
    il2cpp_domain_get = +[]() -> void* { return &classes; };
    il2cpp_domain_get_assemblies = +[](void*, size_t* count) -> void** {
        static void* assembly = &classes;
        *count = 1;
        return &assembly;
    };
    il2cpp_assembly_get_image = +[](void* assembly) { return assembly; };
    il2cpp_class_from_name = +[](void*, const char* ns, const char* name) -> void* {
        for (Class* klass : classes)
            if (!strcmp(ns, klass->ns) && !strcmp(name, klass->name)) return klass;
        return nullptr;
    };
    il2cpp_class_get_name = +[](void* k) { return static_cast<Class*>(k)->name; };
    il2cpp_class_get_namespace = +[](void* k) { return static_cast<Class*>(k)->ns; };
    il2cpp_class_get_parent = +[](void* k) -> void* { return static_cast<Class*>(k)->parent; };
    il2cpp_class_from_type = il2cpp_class_get_type = +[](void* k) { return k; };
    il2cpp_class_is_valuetype = +[](void* k) { return static_cast<Class*>(k)->value; };
    il2cpp_class_is_enum = il2cpp_class_is_interface = +[](void*) { return false; };
    il2cpp_class_get_rank = +[](void*) { return 0; };
    il2cpp_class_value_size = +[](void* k, uint32_t*) { return static_cast<int32_t>(static_cast<Class*>(k)->size); };
    il2cpp_class_is_assignable_from = +[](void* want, void* actual) {
        for (auto* k = static_cast<Class*>(actual); k; k = k->parent)
            if (k == want) return true;
        return false;
    };
    il2cpp_class_get_fields = +[](void* k, void** it) { return next(static_cast<Class*>(k)->fields, it); };
    il2cpp_class_get_methods = +[](void* k, void** it) { return next(static_cast<Class*>(k)->methods, it); };
    il2cpp_field_get_name = +[](void* f) { return static_cast<Field*>(f)->name; };
    il2cpp_field_get_type = +[](void* f) -> void* { return static_cast<Field*>(f)->type; };
    il2cpp_field_get_flags = +[](void* f) { return static_cast<Field*>(f)->flags; };
    il2cpp_field_get_value = +[](void* obj, void* raw_field, void* out) {
        auto* field = static_cast<Field*>(raw_field);
        CHECK(!(field->flags & kFieldStatic));
        memcpy(out, checked_object(obj)->data.data() + field->offset, field->type->size);
    };
    il2cpp_field_set_value = +[](void* obj, void* raw_field, void* value) {
        auto* field = static_cast<Field*>(raw_field);
        CHECK(!(field->flags & (kFieldStatic | kFieldInitOnly | kFieldLiteral)));
        // Match installed IL2CPP SetValue -> SetValueRaw(deref=false):
        // references are passed directly (including null), values by address.
        if (!field->type->value) {
            void* ref = value;
            CHECK(!ref || is_object(ref));
            CHECK(!ref || il2cpp_class_is_assignable_from(field->type, checked_object(ref)->klass));
            memcpy(checked_object(obj)->data.data() + field->offset, &ref, sizeof ref);
        } else {
            CHECK(value != nullptr);
            CHECK(!is_object(value));
            memcpy(checked_object(obj)->data.data() + field->offset, value, field->type->size);
        }
        field_writes++;
    };
    il2cpp_type_get_type = +[](void* t) { return static_cast<Class*>(t)->kind; };
    il2cpp_type_get_name = +[](void* t) { return strdup(class_fullname(t).c_str()); };
    il2cpp_free = free;
    il2cpp_object_get_class = +[](void* obj) -> void* { return checked_object(obj)->klass; };
    il2cpp_object_new = +[](void* k) -> void* { return make(static_cast<Class*>(k)); };
    il2cpp_object_unbox = +[](void* obj) -> void* {
        CHECK(checked_object(obj)->klass->value);
        return checked_object(obj)->data.data();
    };
    il2cpp_value_box = +[](void* k, void* data) -> void* {
        auto* klass = static_cast<Class*>(k);
        CHECK(klass->value);
        auto* boxed = make(klass);
        memcpy(boxed->data.data(), data, klass->size);
        return boxed;
    };
    il2cpp_string_new = +[](const char* text) -> void* {
        auto* str = make(&string_class);
        while (*text) str->text.push_back(static_cast<unsigned char>(*text++));
        return str;
    };
    il2cpp_string_length = +[](void* str) { return static_cast<int32_t>(checked_object(str)->text.size()); };
    il2cpp_string_chars = +[](void* str) { return reinterpret_cast<uint16_t*>(checked_object(str)->text.data()); };
    il2cpp_gchandle_new = +[](void* obj, bool) -> uintptr_t {
        CHECK(is_object(obj));
        uintptr_t id = next_root++;
        roots[id] = obj;
        return id;
    };
    il2cpp_gchandle_get_target = +[](uintptr_t id) { return roots.at(id); };
    il2cpp_gchandle_free = +[](uintptr_t id) { CHECK(roots.erase(id) == 1); };
    il2cpp_gc_disable = il2cpp_gc_enable = +[] {};
    il2cpp_method_get_name = +[](void* m) { return static_cast<Method*>(m)->name; };
    il2cpp_method_get_class = +[](void* m) -> void* {
        for (Class* klass : classes)
            for (Method* method : klass->methods)
                if (method == m) return klass;
        throw std::runtime_error("method has no declaring class");
    };
    il2cpp_method_is_instance = +[](void* m) { return static_cast<Method*>(m)->instance; };
    il2cpp_method_get_param_count = +[](void* m) { return static_cast<uint32_t>(static_cast<Method*>(m)->params.size()); };
    il2cpp_method_get_param = +[](void* m, uint32_t i) -> void* { return static_cast<Method*>(m)->params.at(i); };
    il2cpp_method_is_generic = +[](void*) { return false; };
    il2cpp_runtime_invoke = +[](void* m, void* obj, void** args, void** exception) {
        return static_cast<Method*>(m)->call(obj, args, exception);
    };
    il2cpp_format_exception = +[](void*, char* out, int size) { snprintf(out, size, "setter rejected name"); };
    g_string_class = &string_class;
    g_handles_ok = true;
}

void reset() {
    g_handles.clear();
    g_handle_ids.clear();
    g_generation = 0;
    g_next_handle = 1;
    roots.clear();
    objects.clear();
    allocated_classes.clear();
    field_writes = setter_calls = guid_calls = 0;
    summary_class.methods.clear();
    card_class.methods.clear();
    cards_class.methods.clear();
    value_base_class.methods.clear();
    card_class.fields = {&card_id_field, &card_quantity_field};
    summary_class.fields = {&name_field, &id_field, &count_field, &static_field, &readonly_field, &literal_field};
    base_class.fields = {&inherited_field};
    deck_class.fields = {&summary_field};
    summary = make(&summary_class);
    for (size_t i = 0; i < expected_guid.size(); i++) expected_guid[i] = static_cast<uint8_t>(0xa0 + i);
    memcpy(summary->data.data() + id_field.offset, expected_guid.data(), expected_guid.size());
    auto* deck = make(&deck_class);
    memcpy(deck->data.data(), &summary, sizeof summary);
    deck_handle = handle_for(deck);
}

Json batch(const std::string& ops) {
    // Start with the actual get(Summary); subsequent writes use its batch ref.
    std::string source = "{\"ops\":[{\"op\":\"get\",\"target\":{\"h\":" + std::to_string(deck_handle) +
                         "},\"member\":\"Summary\",\"depth\":0}" + (ops.empty() ? "" : "," + ops) + "]}";
    Json request, response;
    CHECK(JsonParser(source).parse(&request));
    std::string output = run_batch(request);
    CHECK(JsonParser(output).parse(&response));
    CHECK(response.get("ok"));
    return response;
}

std::string set(const std::string& member, const std::string& value) {
    return "{\"op\":\"set\",\"target\":{\"ref\":0},\"member\":" + q(member) + ",\"value\":" + value + "}";
}
std::string get(const std::string& member, bool retain = false) {
    return "{\"op\":\"get\",\"target\":{\"ref\":0},\"member\":" + q(member) +
           ",\"depth\":0,\"retain_value\":" + json_bool(retain) + "}";
}
void success(const Json& response) {
    if (!response.get("ok")->boolean) throw std::runtime_error(json_text(response.get("error")));
}
void failure(const Json& response, const std::string& message, int failed_op = 1) {
    CHECK(!response.get("ok")->boolean);
    CHECK(json_text(response.get("error")).find(message) != std::string::npos);
    CHECK(json_int(response.get("failed_op"), -1) == failed_op);
    CHECK(response.get("results")->items.size() == static_cast<size_t>(failed_op));
    CHECK(field_writes == 0);
}

void string_field_round_trip() {
    reset();
    Json response = batch(set("Name", R"({"str":"Native regression deck"})") + "," + get("Name"));
    success(response);
    CHECK(json_text(&response.get("results")->items.at(2)) == "Native regression deck");
    CHECK(field_writes == 1);
    // A string reference from get, not only a freshly allocated string.
    response = batch(get("Name") + "," + set("Inherited", R"({"ref":1})") + "," + get("Inherited"));
    success(response);
    CHECK(json_text(&response.get("results")->items.at(3)) == "Native regression deck");
    response = batch(set("Name", R"({"null":true})") + "," + get("Name"));
    success(response);
    CHECK(response.get("results")->items.at(2).kind == Json::Null);
    CHECK(field_writes == 3);
}

void primitive_field_round_trip() {
    reset();
    Json response = batch(set("Count", R"({"int":1234567})") + "," + get("Count"));
    success(response);
    CHECK(json_int(&response.get("results")->items.at(2), -1) == 1234567);
    CHECK(field_writes == 1);
}

void rejected_fields_and_types() {
    for (const auto& member : {"StaticName", "ReadonlyName", "ConstantName", "Missing"}) {
        reset();
        failure(batch(set(member, R"({"str":"must not write"})")),
                std::string(member) == "StaticName" ? "static field" :
                std::string(member) == "Missing" ? "no writable member" : "readonly field");
    }
    for (const auto& entry : std::vector<std::pair<std::string, std::string>>{
             {"Name", R"({"int":7})"}, {"Name", R"({"ref":0})"},
             {"Name", R"({"h":999999})"}, {"Name", R"({"ref":99})"},
             {"Count", R"({"str":"wrong"})"}, {"Count", R"({"null":true})"},
             {"DeckId", R"({"ref":0})"}}) {
        reset();
        failure(batch(set(entry.first, entry.second)), "does not fit field");
    }
    reset();
    failure(batch(set("DeckId", R"({"int":7})")), "cannot pass a number");
    failure(batch(R"({"op":"set","target":{"ref":0},"member":"Name"})"), "set needs a value");
    failure(batch(get("DeckId", true) + "," + set("Count", R"({"ref":1})")), "does not fit field", 2);
    failure(batch(get("DeckId", true) + "," + set("Name", R"({"ref":1})")), "does not fit field", 2);
}

void setter_precedence_and_exceptions() {
    reset();
    Method setter{"set_Name", {&string_class}, [](void*, void** args, void**) -> void* {
        CHECK(managed_string(args[0]) == "through setter");  // invoke receives the object itself
        setter_calls++;
        return nullptr;
    }};
    summary_class.methods = {&setter};
    success(batch(set("Name", R"({"str":"through setter"})")));
    CHECK(setter_calls == 1);
    CHECK(field_writes == 0);
    failure(batch(set("Name", R"({"int":7})")), "no overload of set_Name");
    CHECK(setter_calls == 1);
    setter.call = [](void*, void**, void** exception) -> void* {
        setter_calls++;
        *exception = summary;
        return nullptr;
    };
    failure(batch(set("Name", R"({"str":"throws"})")), "setter rejected name");
    CHECK(setter_calls == 2);
    // Even a setter incompatible with a field-compatible value blocks fallback.
    setter.params = {&int_class};
    failure(batch(set("Name", R"({"str":"must not bypass"})")), "no overload of set_Name");
    CHECK(setter_calls == 2);
}

void retained_guid(bool getter) {
    reset();
    Method guid_getter{"get_DeckId", {}, [](void* obj, void**, void**) {
        return il2cpp_value_box(&guid_class, checked_object(obj)->data.data() + id_field.offset);
    }};
    Method mutate{"ChangeSourceGuid", {}, [](void*, void**, void**) -> void* {
        memset(summary->data.data() + id_field.offset, 0, expected_guid.size());
        return nullptr;
    }};
    Method consume{"UseGuid", {&guid_class}, [](void*, void** args, void**) -> void* {
        CHECK(memcmp(args[0], expected_guid.data(), expected_guid.size()) == 0);
        bool rooted = false;
        for (const auto& root : roots) {
            auto* obj = checked_object(root.second);
            if (obj->klass == &guid_class && obj->data.data() == args[0]) rooted = true;
        }
        CHECK(rooted);  // the exact box supplying the raw Guid is held by a GC handle
        guid_calls++;
        return nullptr;
    }};
    summary_class.methods = {&mutate, &consume};
    if (getter) summary_class.methods.push_back(&guid_getter);
    Json response = batch(get("DeckId", true) +
        R"(,{"op":"call","target":{"ref":0},"method":"ChangeSourceGuid","args":[]},)" +
        R"({"op":"call","target":{"ref":0},"method":"UseGuid","args":[{"ref":1}]},)" +
        set("DeckId", R"({"ref":1})") + "," + get("DeckId"));
    success(response);
    CHECK(response.get("results")->items.at(1).get("$struct")->boolean);
    CHECK(guid_calls == 1);
    CHECK(field_writes == 1);
    CHECK(memcmp(summary->data.data() + id_field.offset, expected_guid.data(), expected_guid.size()) == 0);

    // Without the opt-in, value results remain non-referenceable as before.
    field_writes = 0;
    failure(batch(get("DeckId") +
        R"(,{"op":"call","target":{"ref":0},"method":"UseGuid","args":[{"ref":1}]})"),
        "no overload of UseGuid", 2);
    CHECK(guid_calls == 1);
}

void retention_unavailable() {
    reset();
    g_handles_ok = false;
    failure(batch(get("DeckId", true)), "cannot root retained field value");
    Method getter{"get_DeckId", {}, [](void* obj, void**, void**) {
        return il2cpp_value_box(&guid_class, checked_object(obj)->data.data() + id_field.offset);
    }};
    summary_class.methods = {&getter};
    failure(batch(get("DeckId", true)), "cannot root retained getter value");
    g_handles_ok = true;
}

// Emulate the generated value-type constructor: write exactly where `this`
// points. In particular, do NOT call checked_object/unbox or compensate for a
// boxed receiver here. The old production path overwrites klass with these two
// uints and the independent header checks then fail.
void* construct_card(void* self, void** args, void**) {
    memcpy(self, args[0], sizeof(uint32_t));
    memcpy(static_cast<uint8_t*>(self) + sizeof(uint32_t), args[1], sizeof(uint32_t));
    return nullptr;
}

void new_struct_then_add_ref() {
    reset();
    Method ctor{".ctor", {&uint_class, &uint_class}, construct_card};
    card_class.methods = {&ctor};
    auto* cards = make(&cards_class);
    uint32_t cards_handle = handle_for(cards);
    int add_calls = 0;
    Object* constructed = nullptr;
    Method add{"Add", {&card_class}, [&](void* self, void** args, void**) -> void* {
        CHECK(self == cards);
        CHECK(checked_object(self)->klass == &cards_class);
        // Find the actual allocation from its payload address, independently
        // of the class/unbox API under test. REF must supply this exact box.
        for (const auto& obj : objects)
            if (obj->data.data() == args[0]) constructed = checked_object(obj.get());
        CHECK(constructed != nullptr);
        CHECK(constructed->klass == &card_class);
        CHECK(constructed->monitor == &monitor_token);
        uint32_t fields[2]{};
        memcpy(fields, args[0], sizeof fields);
        CHECK(fields[0] == 7347);
        CHECK(fields[1] == 24);
        CHECK(std::any_of(roots.begin(), roots.end(), [&](const auto& root) {
            return root.second == constructed;
        }));
        ++add_calls;
        return nullptr;
    }};
    Method wrong_add{"Add", {&cards_class}, [](void*, void**, void**) -> void* {
        throw std::runtime_error("selected Add(collection) instead of Add(Client_DeckCard)");
    }};
    cards_class.methods = {&wrong_add, &add};
    Json response = batch(
        R"({"op":"new","class":"Client_DeckCard","args":[{"uint":7347},{"uint":24}],"depth":1},)"
        R"({"op":"call","target":{"h":)" + std::to_string(cards_handle) +
        R"(},"method":"Add","args":[{"ref":1}]})");
    success(response);
    CHECK(add_calls == 1);
    const auto& encoded = response.get("results")->items.at(1);
    CHECK(encoded.get("$struct")->boolean);
    CHECK(json_int(encoded.get("Id"), -1) == 7347);
    CHECK(json_int(encoded.get("Quantity"), -1) == 24);
    CHECK(checked_object(constructed)->klass == &card_class);
    CHECK(field_writes == 0);  // readonly fields were initialized by the ctor
}

void value_receiver_boundaries() {
    reset();
    Method ctor{".ctor", {&uint_class, &uint_class}, construct_card};
    int calls = 0;
    Method instance{"CheckPayload", {}, [&](void* self, void**, void**) -> void* {
        CHECK(!is_object(self));
        uint32_t fields[2]{};
        memcpy(fields, self, sizeof fields);
        CHECK(fields[0] == 7347 && fields[1] == 24);
        ++calls;
        return nullptr;
    }};
    Method inherited{"CheckBox", {}, [&](void* self, void**, void**) -> void* {
        CHECK(checked_object(self)->klass == &card_class);
        ++calls;
        return nullptr;
    }};
    Method static_method{"CheckStatic", {}, [&](void* self, void**, void**) -> void* {
        // A supplied boxed target for a static method must not be unboxed.
        CHECK(checked_object(self)->klass == &card_class);
        ++calls;
        return nullptr;
    }, false};
    card_class.methods = {&ctor, &instance, &static_method};
    value_base_class.methods = {&inherited};
    success(batch(
        R"({"op":"new","class":"Client_DeckCard","args":[{"uint":7347},{"uint":24}],"depth":0},)"
        R"({"op":"call","target":{"ref":1},"method":"CheckPayload"},)"
        R"({"op":"call","target":{"ref":1},"method":"CheckBox"},)"
        R"({"op":"call","target":{"ref":1},"method":"CheckStatic"})"));
    CHECK(calls == 3);

    Method reference_ctor{".ctor", {}, [&](void* self, void**, void**) -> void* {
        CHECK(checked_object(self)->klass == &cards_class);
        ++calls;
        return nullptr;
    }};
    cards_class.methods = {&reference_ctor};
    success(batch(R"({"op":"new","class":"Test.Cards","args":[],"depth":0})"));
    CHECK(calls == 4);

    // Static invocation without a target still forwards null unchanged.
    static_method.call = [&](void* self, void**, void**) -> void* {
        CHECK(self == nullptr);
        ++calls;
        return nullptr;
    };
    std::string error;
    invoke(&static_method, nullptr, nullptr, &error);
    CHECK(error.empty());
    CHECK(calls == 5);
}

void new_struct_failures() {
    reset();
    int calls = 0;
    Method ctor{".ctor", {&uint_class, &uint_class}, [&](void* self, void** args, void** exception) -> void* {
        construct_card(self, args, exception);
        ++calls;
        *exception = summary;
        return nullptr;
    }};
    card_class.methods = {&ctor};
    const std::string create =
        R"({"op":"new","class":"Client_DeckCard","args":[{"uint":7347},{"uint":24}],"depth":0})";
    failure(batch(create), "setter rejected name");
    CHECK(calls == 1);
    for (const auto& obj : objects) checked_object(obj.get());
    g_handles_ok = false;
    failure(batch(create), "cannot root new value");
    CHECK(calls == 1);
    g_handles_ok = true;
    il2cpp_object_new = +[](void*) -> void* { return nullptr; };
    failure(batch(create), "could not allocate Client_DeckCard");
    CHECK(calls == 1);
    il2cpp_object_new = +[](void* k) -> void* { return make(static_cast<Class*>(k)); };
}
}  // namespace mock

int main() {
    try {
        mock::install();
        mock::string_field_round_trip();
        std::cout << "PASS string field set/get, reference, null and inherited field\n";
        mock::primitive_field_round_trip();
        std::cout << "PASS raw value field set/get\n";
        mock::rejected_fields_and_types();
        std::cout << "PASS static/readonly/literal, missing value/member and type rejection\n";
        mock::setter_precedence_and_exceptions();
        std::cout << "PASS setter precedence, incompatible overload and no fallback after exception\n";
        mock::retained_guid(false);
        std::cout << "PASS retained field Guid ref: rooted box, later invoke and raw field assignment\n";
        mock::retained_guid(true);
        std::cout << "PASS retained getter Guid ref and default non-retaining behavior\n";
        mock::retention_unavailable();
        std::cout << "PASS retention fails when GC handles unavailable\n";
        mock::new_struct_then_add_ref();
        std::cout << "PASS new readonly struct ctor then Add(REF): payload, header identity and rooting\n";
        mock::value_receiver_boundaries();
        std::cout << "PASS value instance, inherited boxed, static and reference ctor receivers\n";
        mock::new_struct_failures();
        std::cout << "PASS new struct constructor exception, unavailable rooting and allocation failure\n";
    } catch (const std::exception& error) {
        std::cerr << "FAIL " << error.what() << '\n';
        return 1;
    }
    return 0;
}
