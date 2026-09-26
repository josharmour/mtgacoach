// mtgacoach native-macOS IL2CPP probe: feasibility spike for autopilot4mac.md (G1/G2).
//
// Injected into the native Mac MTGA client with DYLD_INSERT_LIBRARIES; the app is
// not hardened-runtime signed, so dyld honours it. The probe talks to the game
// only through GameAssembly.dylib's exported il2cpp_* C API: no BepInEx, no
// Il2CppInterop, no Rosetta, and no dependency on the metadata file format.
//
// Main-thread access: PAPA, the game's root MonoBehaviour, has an Update() that
// Unity invokes every frame through il2cpp_runtime_invoke, which reads
// MethodInfo->methodPointer on each call. We swap that pointer for a trampoline
// that drains a job queue and then calls the original. No code pages are patched.
//
// Two sockets:
// - Bridge: a client of the coach's GRE bridge server (127.0.0.1:44222, override
//   with MTGACOACH_BRIDGE_PORT), speaking the same newline-delimited JSON commands
//   and field names as the Windows BepInEx plugin, so gre_bridge.py and the
//   autopilot drive either client unchanged. Unsupported commands answer
//   {"ok":false,"unsupported":true} instead of guessing.
// - Diagnostics: text commands on 127.0.0.1:44223 (MTGACOACH_PROBE_PORT).
// Game calls run on Unity's main thread with a deadline shorter than Python's
// 5 s read timeout; a job that misses it is dropped, never executed late.
// Log: ~/.arenamcp/il2cpp_probe.log (override with MTGACOACH_PROBE_LOG).

#include <arpa/inet.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#if defined(__APPLE__)
#include <mach-o/dyld.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#else
#include <link.h>
#endif
#if defined(__ANDROID__)
#include <android/log.h>
#endif
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pthread.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cinttypes>
#include <condition_variable>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#if defined(__arm64__) || defined(__aarch64__)
static const char* kArch = "arm64";
#else
static const char* kArch = "x86_64";
#endif

// Android port: the same probe, built with the NDK and loaded into the Android
// client (see spikes/android-il2cpp/). Only library lookup, the memory-protection
// check, process detection and log sinks differ.
#if defined(__ANDROID__)
static const char* kRuntime = "il2cpp-android";
static const char* kGameLibrary = "/libil2cpp.so";
#else
static const char* kRuntime = "il2cpp-macos";
static const char* kGameLibrary = "/GameAssembly.dylib";
#endif

// Linux has no SO_NOSIGPIPE; a write to a closed coach socket must not SIGPIPE the game.
#if defined(MSG_NOSIGNAL)
static const int kSendFlags = MSG_NOSIGNAL;
#else
static const int kSendFlags = 0;
#endif

// ---------------------------------------------------------------------------
// Logging and JSON helpers
// ---------------------------------------------------------------------------

static FILE* g_log = nullptr;
static std::mutex g_log_mutex;

static void plog(const char* fmt, ...) {
    std::lock_guard<std::mutex> lock(g_log_mutex);
#if defined(__ANDROID__)
    // Mirror to logcat: `adb logcat -s mtgacoach` works even when the file sink
    // could not be opened.
    va_list logcat_args;
    va_start(logcat_args, fmt);
    __android_log_vprint(ANDROID_LOG_INFO, "mtgacoach", fmt, logcat_args);
    va_end(logcat_args);
#endif
    if (!g_log) return;
    time_t now = time(nullptr);
    struct tm local;
    localtime_r(&now, &local);
    char stamp[32];
    strftime(stamp, sizeof stamp, "%Y-%m-%d %H:%M:%S", &local);
    fprintf(g_log, "%s [%d] ", stamp, getpid());
    va_list args;
    va_start(args, fmt);
    vfprintf(g_log, fmt, args);
    va_end(args);
    fputc('\n', g_log);
    fflush(g_log);
}

static std::string q(const std::string& text) {
    std::string out = "\"";
    for (unsigned char c : text) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char escaped[8];
                    snprintf(escaped, sizeof escaped, "\\u%04x", c);
                    out += escaped;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    return out + "\"";
}

static std::string error_json(const std::string& message) {
    return "{\"ok\":false,\"error\":" + q(message) + "}";
}

static std::string json_bool(bool value) { return value ? "true" : "false"; }

// Minimal parser for the coach's command objects (flat values, small arrays).
struct Json {
    enum Kind { Null, Bool, Number, String, Array, Object };
    Kind kind = Null;
    bool boolean = false;
    double number = 0;
    std::string text;
    std::vector<Json> items;
    std::vector<std::string> keys;  // Object: keys[i] names items[i]

    const Json* get(const char* key) const {
        if (kind != Object) return nullptr;
        for (size_t i = 0; i < keys.size(); i++)
            if (keys[i] == key) return &items[i];
        return nullptr;
    }
};

static long long json_int(const Json* value, long long fallback) {
    if (value && value->kind == Json::Number) return static_cast<long long>(value->number);
    if (value && value->kind == Json::Bool) return value->boolean;
    return fallback;
}

static bool json_flag(const Json* value, bool fallback) {
    if (value && value->kind == Json::Bool) return value->boolean;
    if (value && value->kind == Json::Number) return value->number != 0;
    return fallback;
}

static std::string json_text(const Json* value) {
    return value && value->kind == Json::String ? value->text : "";
}

class JsonParser {
  public:
    explicit JsonParser(const std::string& source) : s_(source) {}

    bool parse(Json* out) {
        if (!value(out, 0)) return false;
        skip_space();
        return pos_ == s_.size();
    }

  private:
    const std::string& s_;
    size_t pos_ = 0;

    void skip_space() {
        while (pos_ < s_.size() && isspace(static_cast<unsigned char>(s_[pos_]))) pos_++;
    }

    bool literal(const char* word) {
        size_t length = strlen(word);
        if (s_.compare(pos_, length, word) != 0) return false;
        pos_ += length;
        return true;
    }

    bool string(std::string* out) {
        if (pos_ >= s_.size() || s_[pos_] != '"') return false;
        pos_++;
        while (pos_ < s_.size()) {
            char c = s_[pos_++];
            if (c == '"') return true;
            if (c != '\\') {
                *out += c;
                continue;
            }
            if (pos_ >= s_.size()) return false;
            char escape = s_[pos_++];
            switch (escape) {
                case '"': *out += '"'; break;
                case '\\': *out += '\\'; break;
                case '/': *out += '/'; break;
                case 'b': *out += '\b'; break;
                case 'f': *out += '\f'; break;
                case 'n': *out += '\n'; break;
                case 'r': *out += '\r'; break;
                case 't': *out += '\t'; break;
                case 'u': {
                    if (pos_ + 4 > s_.size()) return false;
                    unsigned code = static_cast<unsigned>(strtoul(s_.substr(pos_, 4).c_str(), nullptr, 16));
                    pos_ += 4;
                    if (code < 0x80) {
                        *out += static_cast<char>(code);
                    } else if (code < 0x800) {
                        *out += static_cast<char>(0xC0 | (code >> 6));
                        *out += static_cast<char>(0x80 | (code & 0x3F));
                    } else {
                        *out += static_cast<char>(0xE0 | (code >> 12));
                        *out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
                        *out += static_cast<char>(0x80 | (code & 0x3F));
                    }
                    break;
                }
                default: return false;
            }
        }
        return false;
    }

    bool value(Json* out, int depth) {
        if (depth > 32) return false;
        skip_space();
        if (pos_ >= s_.size()) return false;
        char c = s_[pos_];
        if (c == '{' || c == '[') {
            bool object = c == '{';
            char close = object ? '}' : ']';
            out->kind = object ? Json::Object : Json::Array;
            pos_++;
            skip_space();
            if (pos_ < s_.size() && s_[pos_] == close) {
                pos_++;
                return true;
            }
            for (;;) {
                skip_space();
                if (object) {
                    std::string key;
                    if (!string(&key)) return false;
                    skip_space();
                    if (pos_ >= s_.size() || s_[pos_] != ':') return false;
                    pos_++;
                    out->keys.push_back(key);
                }
                Json item;
                if (!value(&item, depth + 1)) return false;
                out->items.push_back(std::move(item));
                skip_space();
                if (pos_ < s_.size() && s_[pos_] == ',') {
                    pos_++;
                    continue;
                }
                if (pos_ < s_.size() && s_[pos_] == close) {
                    pos_++;
                    return true;
                }
                return false;
            }
        }
        if (c == '"') {
            out->kind = Json::String;
            return string(&out->text);
        }
        if (literal("true")) {
            out->kind = Json::Bool;
            out->boolean = true;
            return true;
        }
        if (literal("false")) {
            out->kind = Json::Bool;
            return true;
        }
        if (literal("null")) return true;
        const char* start = s_.c_str() + pos_;
        char* end = nullptr;
        double number = strtod(start, &end);
        if (end == start) return false;
        pos_ += static_cast<size_t>(end - start);
        out->kind = Json::Number;
        out->number = number;
        return true;
    }
};

// ---------------------------------------------------------------------------
// il2cpp C API, resolved from GameAssembly.dylib at runtime
// ---------------------------------------------------------------------------

#define IL2CPP_FUNCS(X)                                                        \
    X(void*, il2cpp_get_corlib, ())                                            \
    X(void*, il2cpp_domain_get, ())                                            \
    X(void**, il2cpp_domain_get_assemblies, (void*, size_t*))                  \
    X(void*, il2cpp_assembly_get_image, (void*))                               \
    X(const char*, il2cpp_image_get_name, (void*))                             \
    X(void*, il2cpp_class_from_name, (void*, const char*, const char*))        \
    X(void*, il2cpp_class_get_methods, (void*, void**))                        \
    X(void*, il2cpp_class_get_fields, (void*, void**))                         \
    X(void*, il2cpp_class_get_properties, (void*, void**))                     \
    X(void*, il2cpp_class_get_parent, (void*))                                 \
    X(const char*, il2cpp_class_get_name, (void*))                             \
    X(const char*, il2cpp_class_get_namespace, (void*))                        \
    X(void*, il2cpp_class_get_type, (void*))                                   \
    X(const char*, il2cpp_method_get_name, (void*))                            \
    X(uint32_t, il2cpp_method_get_param_count, (void*))                        \
    X(void*, il2cpp_method_get_param, (void*, uint32_t))                       \
    X(void*, il2cpp_method_get_return_type, (void*))                           \
    X(bool, il2cpp_method_is_generic, (void*))                                 \
    X(bool, il2cpp_method_is_instance, (void*))                                \
    X(const char*, il2cpp_field_get_name, (void*))                             \
    X(void*, il2cpp_field_get_type, (void*))                                   \
    X(size_t, il2cpp_field_get_offset, (void*))                                \
    X(int, il2cpp_field_get_flags, (void*))                                    \
    X(void, il2cpp_field_get_value, (void*, void*, void*))                     \
    X(void, il2cpp_field_static_get_value, (void*, void*))                     \
    X(const char*, il2cpp_property_get_name, (void*))                          \
    X(char*, il2cpp_type_get_name, (void*))                                    \
    X(int, il2cpp_type_get_type, (void*))                                      \
    X(void*, il2cpp_type_get_object, (void*))                                  \
    X(void*, il2cpp_object_get_class, (void*))                                 \
    X(void*, il2cpp_object_unbox, (void*))                                     \
    X(void*, il2cpp_value_box, (void*, void*))                                 \
    X(void*, il2cpp_class_from_type, (void*))                                  \
    X(uintptr_t, il2cpp_gchandle_new, (void*, bool))                           \
    X(void*, il2cpp_gchandle_get_target, (uintptr_t))                          \
    X(void, il2cpp_gchandle_free, (uintptr_t))                                 \
    X(void*, il2cpp_array_new, (void*, size_t))                                \
    X(uint32_t, il2cpp_array_length, (void*))                                  \
    X(void*, il2cpp_class_get_element_class, (void*))                          \
    X(int, il2cpp_class_array_element_size, (void*))                           \
    X(int, il2cpp_class_get_rank, (void*))                                     \
    X(bool, il2cpp_class_is_enum, (void*))                                     \
    X(bool, il2cpp_class_is_valuetype, (void*))                                \
    X(bool, il2cpp_class_is_interface, (void*))                                \
    X(bool, il2cpp_class_is_subclass_of, (void*, void*, bool))                 \
    X(bool, il2cpp_class_is_assignable_from, (void*, void*))                   \
    X(void*, il2cpp_class_enum_basetype, (void*))                              \
    X(int32_t, il2cpp_class_value_size, (void*, uint32_t*))                    \
    X(void*, il2cpp_object_new, (void*))                                       \
    X(void*, il2cpp_string_new, (const char*))                                 \
    X(void, il2cpp_field_set_value, (void*, void*, void*))                     \
    X(void, il2cpp_field_static_set_value, (void*, void*))                     \
    X(void, il2cpp_runtime_class_init, (void*))                                \
    X(void, il2cpp_gc_wbarrier_set_field, (void*, void**, void*))              \
    X(void*, il2cpp_runtime_invoke, (void*, void*, void**, void**))            \
    X(void*, il2cpp_thread_attach, (void*))                                    \
    X(void, il2cpp_format_exception, (void*, char*, int))                      \
    X(void, il2cpp_free, (void*))                                              \
    X(uint16_t*, il2cpp_string_chars, (void*))                                 \
    X(int32_t, il2cpp_string_length, (void*))

#define IL2CPP_DECLARE(ret, name, params) \
    using name##_fn = ret(*) params;      \
    static name##_fn name = nullptr;
IL2CPP_FUNCS(IL2CPP_DECLARE)
#undef IL2CPP_DECLARE

static void* g_game_assembly = nullptr;

#if defined(__APPLE__)
static bool open_game_library() {
    for (uint32_t i = 0, count = _dyld_image_count(); i < count; i++) {
        const char* image = _dyld_get_image_name(i);
        if (image && strstr(image, kGameLibrary)) {
            g_game_assembly = dlopen(image, RTLD_LAZY | RTLD_NOLOAD);
            break;
        }
    }
    return g_game_assembly != nullptr;
}

static void* game_symbol(const char* name) { return dlsym(g_game_assembly, name); }
#else
// A library loaded ahead of the game (LD_PRELOAD, or a DT_NEEDED of libmain.so)
// can land in a different linker namespace from libil2cpp.so, where dlopen
// refuses it even with RTLD_NOLOAD. The fallback reads the export table straight
// from the mapped image through its GNU hash table (libil2cpp.so has no DT_HASH).
struct LoadedImage {
    ElfW(Addr) base = 0;
    const ElfW(Sym)* symtab = nullptr;
    const char* strtab = nullptr;
    const uint32_t* gnu_hash = nullptr;
    std::string path;
};
static LoadedImage g_game_image;

static int find_game_image(struct dl_phdr_info* info, size_t, void* out) {
    if (!info->dlpi_name || !strstr(info->dlpi_name, kGameLibrary)) return 0;
    auto* image = static_cast<LoadedImage*>(out);
    image->base = info->dlpi_addr;
    image->path = info->dlpi_name;
    for (int i = 0; i < info->dlpi_phnum; i++) {
        if (info->dlpi_phdr[i].p_type != PT_DYNAMIC) continue;
        auto* dyn = reinterpret_cast<const ElfW(Dyn)*>(info->dlpi_addr + info->dlpi_phdr[i].p_vaddr);
        // Bionic leaves d_ptr unrelocated; glibc relocates it. Accept either.
        auto addr = [&](ElfW(Addr) ptr) { return ptr < info->dlpi_addr ? info->dlpi_addr + ptr : ptr; };
        for (; dyn->d_tag != DT_NULL; dyn++) {
            if (dyn->d_tag == DT_SYMTAB) image->symtab = reinterpret_cast<const ElfW(Sym)*>(addr(dyn->d_un.d_ptr));
            if (dyn->d_tag == DT_STRTAB) image->strtab = reinterpret_cast<const char*>(addr(dyn->d_un.d_ptr));
            if (dyn->d_tag == DT_GNU_HASH) image->gnu_hash = reinterpret_cast<const uint32_t*>(addr(dyn->d_un.d_ptr));
        }
    }
    return 1;
}

static void* gnu_hash_lookup(const LoadedImage& image, const char* name) {
    if (!image.symtab || !image.strtab || !image.gnu_hash) return nullptr;
    uint32_t hash = 5381;
    for (const unsigned char* c = reinterpret_cast<const unsigned char*>(name); *c; c++) hash = hash * 33 + *c;
    const uint32_t nbuckets = image.gnu_hash[0], symoffset = image.gnu_hash[1], bloom_size = image.gnu_hash[2];
    const uint32_t* buckets = image.gnu_hash + 4 + bloom_size * (sizeof(ElfW(Addr)) / 4);
    const uint32_t* chain = buckets + nbuckets;
    uint32_t index = buckets[hash % nbuckets];
    if (index < symoffset) return nullptr;
    for (;; index++) {
        const uint32_t entry = chain[index - symoffset];
        const ElfW(Sym)& sym = image.symtab[index];
        if ((entry | 1) == (hash | 1) && sym.st_shndx != SHN_UNDEF && strcmp(image.strtab + sym.st_name, name) == 0) {
            return reinterpret_cast<void*>(image.base + sym.st_value);
        }
        if (entry & 1) return nullptr;
    }
}

static bool open_game_library() {
    LoadedImage image;
    if (!dl_iterate_phdr(find_game_image, &image)) return false;
    g_game_image = image;
    g_game_assembly = dlopen(image.path.c_str(), RTLD_NOW | RTLD_NOLOAD);
    plog("game library %s at %p (%s)", image.path.c_str(), reinterpret_cast<void*>(image.base),
         g_game_assembly ? "dlopen" : "export table");
    return true;
}

static void* game_symbol(const char* name) {
    void* symbol = g_game_assembly ? dlsym(g_game_assembly, name) : nullptr;
    return symbol ? symbol : gnu_hash_lookup(g_game_image, name);
}
#endif

static bool g_library_open = false;

static bool resolve_api() {
    if (!g_library_open) g_library_open = open_game_library();
    if (!g_library_open) return false;
    bool complete = true;
#define IL2CPP_RESOLVE(ret, name, params)                       \
    name = reinterpret_cast<name##_fn>(game_symbol(#name));     \
    if (!name) {                                                \
        plog("missing export %s", #name);                       \
        complete = false;                                       \
    }
    IL2CPP_FUNCS(IL2CPP_RESOLVE)
#undef IL2CPP_RESOLVE
    return complete;
}

// Il2CppTypeEnum values used when reading scalars.
enum : int {
    kTypeBoolean = 0x02, kTypeChar = 0x03, kTypeI1 = 0x04, kTypeU1 = 0x05,
    kTypeI2 = 0x06, kTypeU2 = 0x07, kTypeI4 = 0x08, kTypeU4 = 0x09,
    kTypeI8 = 0x0a, kTypeU8 = 0x0b, kTypeValueType = 0x11,
};
static const int kFieldStatic = 0x0010;

// ---------------------------------------------------------------------------
// Reflection helpers (callable from the attached server thread or main thread)
// ---------------------------------------------------------------------------

static std::vector<void*> loaded_images() {
    std::vector<void*> images;
    size_t count = 0;
    void** assemblies = il2cpp_domain_get_assemblies(il2cpp_domain_get(), &count);
    for (size_t i = 0; assemblies && i < count; i++) {
        if (void* image = il2cpp_assembly_get_image(assemblies[i])) images.push_back(image);
    }
    return images;
}

static bool has_image(const char* name) {
    for (void* image : loaded_images()) {
        const char* image_name = il2cpp_image_get_name(image);
        if (image_name && strcmp(image_name, name) == 0) return true;
    }
    return false;
}

static void* find_class(const char* ns, const char* name) {
    for (void* image : loaded_images()) {
        if (void* klass = il2cpp_class_from_name(image, ns, name)) return klass;
    }
    return nullptr;
}

static std::string class_fullname(void* klass) {
    if (!klass) return "null";
    const char* ns = il2cpp_class_get_namespace(klass);
    const char* name = il2cpp_class_get_name(klass);
    return (ns && *ns) ? std::string(ns) + "." + name : std::string(name ? name : "?");
}

static std::string type_name(void* type) {
    char* name = type ? il2cpp_type_get_name(type) : nullptr;
    std::string result = name ? name : "?";
    if (name) il2cpp_free(name);
    return result;
}

// Walks base classes; skips open generic methods. param0_type filters overloads.
static void* find_method(void* klass, const char* name, int argc, const char* param0_type = nullptr) {
    for (void* k = klass; k; k = il2cpp_class_get_parent(k)) {
        void* iter = nullptr;
        while (void* method = il2cpp_class_get_methods(k, &iter)) {
            if (strcmp(il2cpp_method_get_name(method), name) != 0) continue;
            if (argc >= 0 && static_cast<int>(il2cpp_method_get_param_count(method)) != argc) continue;
            if (il2cpp_method_is_generic(method)) continue;
            if (param0_type && type_name(il2cpp_method_get_param(method, 0)) != param0_type) continue;
            return method;
        }
    }
    return nullptr;
}

static void* find_field(void* klass, const char* name) {
    for (void* k = klass; k; k = il2cpp_class_get_parent(k)) {
        void* iter = nullptr;
        while (void* field = il2cpp_class_get_fields(k, &iter)) {
            if (strcmp(il2cpp_field_get_name(field), name) == 0) return field;
        }
    }
    return nullptr;
}

// Returns the result object (boxed for value types). Managed exceptions are
// caught by il2cpp and reported through `error`; a void method returns null.
static void* invoke(void* method, void* instance, void** args, std::string* error) {
    void* exception = nullptr;
    void* result = il2cpp_runtime_invoke(method, instance, args, &exception);
    if (exception) {
        char message[2048] = {0};
        il2cpp_format_exception(exception, message, sizeof message);
        if (error) *error = message[0] ? message : "managed exception";
        return nullptr;
    }
    return result;
}

static std::string managed_string(void* str) {
    if (!str) return "";
    int32_t length = il2cpp_string_length(str);
    const uint16_t* chars = il2cpp_string_chars(str);
    std::string out;
    for (int32_t i = 0; i < length; i++) {
        uint32_t code = chars[i];
        // Combine surrogate pairs; a lone surrogate would be invalid UTF-8 and
        // make the coach's JSON decode fail (and drop the connection).
        if (code >= 0xD800 && code <= 0xDBFF && i + 1 < length && chars[i + 1] >= 0xDC00 && chars[i + 1] <= 0xDFFF) {
            code = 0x10000 + ((code - 0xD800) << 10) + (chars[i + 1] - 0xDC00);
            i++;
        } else if (code >= 0xD800 && code <= 0xDFFF) {
            code = 0xFFFD;
        }
        if (code < 0x80) {
            out += static_cast<char>(code);
        } else if (code < 0x800) {
            out += static_cast<char>(0xC0 | (code >> 6));
            out += static_cast<char>(0x80 | (code & 0x3F));
        } else if (code < 0x10000) {
            out += static_cast<char>(0xE0 | (code >> 12));
            out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (code & 0x3F));
        } else {
            out += static_cast<char>(0xF0 | (code >> 18));
            out += static_cast<char>(0x80 | ((code >> 12) & 0x3F));
            out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (code & 0x3F));
        }
    }
    return out;
}

static std::string object_to_string(void* obj) {
    if (!obj) return "null";
    void* method = find_method(il2cpp_object_get_class(obj), "ToString", 0);
    if (!method) return "?";
    std::string error;
    void* str = invoke(method, obj, nullptr, &error);
    return str ? managed_string(str) : "<" + error + ">";
}

// Reference-typed member: property getter first, then field.
static void* get_object(void* obj, const char* name, std::string* error) {
    if (!obj) {
        if (error) *error = std::string("null object while reading ") + name;
        return nullptr;
    }
    void* klass = il2cpp_object_get_class(obj);
    std::string getter = std::string("get_") + name;
    if (void* method = find_method(klass, getter.c_str(), 0)) return invoke(method, obj, nullptr, error);
    if (void* field = find_field(klass, name)) {
        void* value = nullptr;
        il2cpp_field_get_value(obj, field, &value);
        return value;
    }
    if (error) *error = std::string("no member ") + name + " on " + class_fullname(klass);
    return nullptr;
}

static bool read_scalar(const void* data, int type, int64_t* out) {
    switch (type) {
        case kTypeBoolean: case kTypeU1: *out = *static_cast<const uint8_t*>(data); return true;
        case kTypeI1: *out = *static_cast<const int8_t*>(data); return true;
        case kTypeChar: case kTypeU2: *out = *static_cast<const uint16_t*>(data); return true;
        case kTypeI2: *out = *static_cast<const int16_t*>(data); return true;
        case kTypeI4: *out = *static_cast<const int32_t*>(data); return true;
        case kTypeU4: *out = *static_cast<const uint32_t*>(data); return true;
        case kTypeI8: case kTypeU8: *out = *static_cast<const int64_t*>(data); return true;
        case kTypeValueType: *out = *static_cast<const int32_t*>(data); return true;  // int32-backed enums
        default: return false;
    }
}

static bool get_int(void* obj, const char* name, int64_t* out, std::string* error) {
    if (!obj) return false;
    void* klass = il2cpp_object_get_class(obj);
    std::string getter = std::string("get_") + name;
    if (void* method = find_method(klass, getter.c_str(), 0)) {
        void* boxed = invoke(method, obj, nullptr, error);
        return boxed && read_scalar(il2cpp_object_unbox(boxed),
                                    il2cpp_type_get_type(il2cpp_method_get_return_type(method)), out);
    }
    if (void* field = find_field(klass, name)) {
        uint8_t buffer[16] = {0};
        il2cpp_field_get_value(obj, field, buffer);
        return read_scalar(buffer, il2cpp_type_get_type(il2cpp_field_get_type(field)), out);
    }
    return false;
}

// Enum-valued property or field rendered through the managed ToString() (e.g. "Play").
static std::string get_enum_name(void* obj, const char* name) {
    if (!obj) return "";
    void* klass = il2cpp_object_get_class(obj);
    std::string getter = std::string("get_") + name;
    std::string error;
    if (void* method = find_method(klass, getter.c_str(), 0)) {
        void* boxed = invoke(method, obj, nullptr, &error);
        return boxed ? object_to_string(boxed) : "";
    }
    if (void* field = find_field(klass, name)) {
        uint8_t buffer[16] = {0};
        il2cpp_field_get_value(obj, field, buffer);
        void* enum_class = il2cpp_class_from_type(il2cpp_field_get_type(field));
        void* boxed = enum_class ? il2cpp_value_box(enum_class, buffer) : nullptr;
        return boxed ? object_to_string(boxed) : "";
    }
    return "";
}

// Value of a named constant on an enum class (e.g. OptionResponse.AllowYes).
static bool enum_constant(void* enum_class, const char* name, int32_t* out) {
    void* field = enum_class ? find_field(enum_class, name) : nullptr;
    if (!field || !(il2cpp_field_get_flags(field) & kFieldStatic)) return false;
    il2cpp_field_static_get_value(field, out);
    return true;
}

static int list_count(void* list, std::string* error) {
    void* method = find_method(il2cpp_object_get_class(list), "get_Count", 0);
    if (!method) return -1;
    void* boxed = invoke(method, list, nullptr, error);
    return boxed ? *static_cast<int32_t*>(il2cpp_object_unbox(boxed)) : -1;
}

static void* list_item(void* list, int index, std::string* error) {
    void* method = find_method(il2cpp_object_get_class(list), "get_Item", 1);
    if (!method) return nullptr;
    int32_t position = index;
    void* args[1] = {&position};
    return invoke(method, list, args, error);
}

// ---------------------------------------------------------------------------
// Main-thread dispatch: PAPA.Update method-pointer swap + job queue
// ---------------------------------------------------------------------------

using UpdateFn = void (*)(void*, const void*);
static UpdateFn g_original_update = nullptr;
static std::atomic<uint64_t> g_ticks{0};
static std::atomic<bool> g_hooked{false};
static std::string g_hook_error = "not attempted";

struct Job {
    std::function<std::string()> run;
    std::chrono::steady_clock::time_point deadline;
    std::string result;
    bool done = false;
    std::mutex mutex;
    std::condition_variable finished;
};
static std::mutex g_jobs_mutex;
static std::deque<std::shared_ptr<Job>> g_jobs;

static void drain_jobs() {
    std::deque<std::shared_ptr<Job>> batch;
    {
        std::lock_guard<std::mutex> lock(g_jobs_mutex);
        if (g_jobs.empty()) return;
        batch.swap(g_jobs);
    }
    for (auto& job : batch) {
        std::string result;
        // Expired work is dropped, never executed late.
        if (std::chrono::steady_clock::now() > job->deadline) {
            result = error_json("expired before the main thread ran it");
        } else {
            try {
                result = job->run();
            } catch (...) {
                result = error_json("exception while running on the main thread");
            }
        }
        {
            std::lock_guard<std::mutex> lock(job->mutex);
            job->result = result;
            job->done = true;
        }
        job->finished.notify_all();
    }
}

static void hooked_update(void* self, const void* method) {
    g_ticks.fetch_add(1, std::memory_order_relaxed);
    drain_jobs();
    g_original_update(self, method);
}

#if !defined(__APPLE__)
static bool is_writable(void* address, size_t length) {
    FILE* maps = fopen("/proc/self/maps", "r");
    if (!maps) return false;
    const uintptr_t start = reinterpret_cast<uintptr_t>(address);
    bool writable = false;
    char line[512];
    while (fgets(line, sizeof line, maps)) {
        uintptr_t lo = 0, hi = 0;
        char perms[5] = {};
        if (sscanf(line, "%" SCNxPTR "-%" SCNxPTR " %4s", &lo, &hi, perms) != 3) continue;
        if (start >= lo && start < hi) {
            writable = perms[1] == 'w' && start + length <= hi;
            break;
        }
    }
    fclose(maps);
    return writable;
}
#else
static bool is_writable(void* address, size_t length) {
    mach_vm_address_t region = reinterpret_cast<mach_vm_address_t>(address);
    mach_vm_size_t size = 0;
    vm_region_basic_info_data_64_t info;
    mach_msg_type_number_t count = VM_REGION_BASIC_INFO_COUNT_64;
    mach_port_t object = MACH_PORT_NULL;
    if (mach_vm_region(mach_task_self(), &region, &size, VM_REGION_BASIC_INFO_64,
                       reinterpret_cast<vm_region_info_t>(&info), &count, &object) != KERN_SUCCESS) {
        return false;
    }
    mach_vm_address_t start = reinterpret_cast<mach_vm_address_t>(address);
    return (info.protection & VM_PROT_WRITE) && start >= region && start + length <= region + size;
}
#endif

static bool install_update_hook(void* papa_class) {
    void* method = find_method(papa_class, "Update", 0);
    if (!method) {
        g_hook_error = "PAPA.Update not found";
        return false;
    }
    void** slots = static_cast<void**>(method);
    const void* name = il2cpp_method_get_name(method);
    // MethodInfo begins {methodPointer, [virtualMethodPointer,] invoker_method, name, ...};
    // locating `name` confirms which layout this build uses before we write.
    int name_slot = -1;
    for (int i = 1; i < 8; i++) {
        if (slots[i] == name) {
            name_slot = i;
            break;
        }
    }
    if (name_slot < 2) {
        g_hook_error = "unexpected MethodInfo layout (name slot " + std::to_string(name_slot) + ")";
        return false;
    }
    if (!slots[0]) {
        g_hook_error = "PAPA.Update has no method pointer";
        return false;
    }
    if (!is_writable(slots, sizeof(void*) * 2)) {
        g_hook_error = "PAPA.Update MethodInfo is not writable";
        return false;
    }
    g_original_update = reinterpret_cast<UpdateFn>(slots[0]);
    bool swap_virtual = name_slot >= 3 && slots[1] == slots[0];
    slots[0] = reinterpret_cast<void*>(&hooked_update);
    if (swap_virtual) slots[1] = reinterpret_cast<void*>(&hooked_update);
    g_hook_error.clear();
    g_hooked = true;
    plog("hooked PAPA.Update: MethodInfo=%p original=%p name_slot=%d virtual_swapped=%d", method,
         reinterpret_cast<void*>(g_original_update), name_slot, swap_virtual);
    return true;
}

static std::string run_on_main(std::function<std::string()> run, int timeout_ms = 3000) {
    if (!g_hooked) return error_json("main-thread hook not installed: " + g_hook_error);
    auto job = std::make_shared<Job>();
    job->run = std::move(run);
    job->deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    {
        std::lock_guard<std::mutex> lock(g_jobs_mutex);
        g_jobs.push_back(job);
    }
    std::unique_lock<std::mutex> lock(job->mutex);
    if (!job->finished.wait_until(lock, job->deadline + std::chrono::milliseconds(500), [&] { return job->done; })) {
        return error_json("main thread did not answer in time (ticks=" + std::to_string(g_ticks.load()) +
                          "); outcome unknown");
    }
    return job->result;
}

// ---------------------------------------------------------------------------
// Game access (main thread only): GameManager -> WorkflowController -> request
// ---------------------------------------------------------------------------

struct Pending {
    void* game_manager = nullptr;
    void* workflow = nullptr;
    void* request = nullptr;
    std::string workflow_source;
    std::string error;
};

static void* find_scene_object(void* klass, std::string* error) {
    static void* unity_object = find_class("UnityEngine", "Object");
    if (!unity_object) {
        *error = "UnityEngine.Object not found";
        return nullptr;
    }
    void* type_object = il2cpp_type_get_object(il2cpp_class_get_type(klass));
    for (const char* name : {"FindAnyObjectByType", "FindFirstObjectByType", "FindObjectOfType"}) {
        void* method = find_method(unity_object, name, 1, "System.Type");
        if (!method) continue;
        void* args[1] = {type_object};
        std::string call_error;
        if (void* found = invoke(method, nullptr, args, &call_error)) return found;
        if (!call_error.empty()) *error = std::string(name) + ": " + call_error;
    }
    return nullptr;
}

static Pending find_pending() {
    Pending pending;
    static void* game_manager_class = find_class("", "GameManager");
    if (!game_manager_class) {
        pending.error = "GameManager class not found";
        return pending;
    }
    pending.game_manager = find_scene_object(game_manager_class, &pending.error);
    if (!pending.game_manager) {
        if (pending.error.empty()) pending.error = "no GameManager in the scene (not in a match)";
        return pending;
    }
    void* controller = get_object(pending.game_manager, "WorkflowController", &pending.error);
    if (!controller) {
        if (pending.error.empty()) pending.error = "WorkflowController is null";
        return pending;
    }
    std::string ignored;
    pending.workflow = get_object(controller, "CurrentWorkflow", &ignored);
    pending.workflow_source = "CurrentWorkflow";
    if (!pending.workflow) {
        pending.workflow = get_object(controller, "PendingWorkflow", &ignored);
        pending.workflow_source = "PendingWorkflow";
    }
    if (!pending.workflow) {
        pending.error = "no current or pending workflow";
        return pending;
    }
    pending.request = get_object(pending.workflow, "BaseRequest", &ignored);
    if (!pending.request) pending.request = get_object(pending.workflow, "Request", &ignored);
    pending.error = pending.request ? "" : "workflow has no request";
    return pending;
}

static const char* request_name(void* request) {
    return il2cpp_class_get_name(il2cpp_object_get_class(request));
}

static std::string action_json(void* action, int index) {
    std::string error;
    int64_t grp_id = 0, instance_id = 0, ability_grp_id = 0;
    get_int(action, "GrpId", &grp_id, &error);
    get_int(action, "InstanceId", &instance_id, &error);
    get_int(action, "AbilityGrpId", &ability_grp_id, &error);
    return "{\"index\":" + std::to_string(index) + ",\"type\":" + q(get_enum_name(action, "ActionType")) +
           ",\"grp_id\":" + std::to_string(grp_id) + ",\"instance_id\":" + std::to_string(instance_id) +
           ",\"ability_grp_id\":" + std::to_string(ability_grp_id) + "}";
}

static std::string command_pending() {
    Pending pending = find_pending();
    if (!pending.request) {
        std::string out = "{\"ok\":true,\"has_pending\":false,\"reason\":" + q(pending.error);
        if (pending.workflow)
            out += ",\"workflow\":" + q(class_fullname(il2cpp_object_get_class(pending.workflow)));
        return out + "}";
    }
    void* request_class = il2cpp_object_get_class(pending.request);
    std::string out = "{\"ok\":true,\"has_pending\":true,\"workflow\":" +
                      q(class_fullname(il2cpp_object_get_class(pending.workflow))) +
                      ",\"workflow_source\":" + q(pending.workflow_source) +
                      ",\"request\":" + q(class_fullname(request_class));
    if (strcmp(request_name(pending.request), "ActionsAvailableRequest") == 0) {
        std::string error;
        void* actions = get_object(pending.request, "Actions", &error);
        int count = actions ? list_count(actions, &error) : -1;
        out += ",\"actions\":[";
        for (int i = 0; i < count; i++) {
            void* action = list_item(actions, i, &error);
            out += (i ? "," : "") + (action ? action_json(action, i) : std::string("null"));
        }
        out += "]";
        int64_t can_pass = 0;
        if (get_int(pending.request, "CanPass", &can_pass, &error))
            out += std::string(",\"can_pass\":") + (can_pass ? "true" : "false");
        if (count < 0) out += ",\"actions_error\":" + q(error);
    }
    return out + "}";
}

// Submits the action at `index` only if it still refers to `expected_instance`
// (pass -1 to skip the identity check).
static std::string command_submit_action(int index, long long expected_instance) {
    Pending pending = find_pending();
    if (!pending.request) return error_json("no pending request: " + pending.error);
    void* request_class = il2cpp_object_get_class(pending.request);
    if (strcmp(request_name(pending.request), "ActionsAvailableRequest") != 0)
        return error_json("pending request is " + class_fullname(request_class) + ", not ActionsAvailableRequest");
    std::string error;
    void* actions = get_object(pending.request, "Actions", &error);
    if (!actions) return error_json("no Actions list: " + error);
    int count = list_count(actions, &error);
    if (index < 0 || index >= count)
        return error_json("index " + std::to_string(index) + " out of range (" + std::to_string(count) + " actions)");
    void* action = list_item(actions, index, &error);
    if (!action) return error_json("action " + std::to_string(index) + " is null: " + error);
    int64_t instance_id = 0;
    get_int(action, "InstanceId", &instance_id, &error);
    if (expected_instance >= 0 && instance_id != expected_instance)
        return error_json("identity mismatch: action " + std::to_string(index) + " is instance " +
                          std::to_string(instance_id) + ", expected " + std::to_string(expected_instance));
    void* submit = find_method(request_class, "SubmitAction", 2);
    if (!submit) return error_json("SubmitAction(Action, bool) not found");
    bool auto_pass = false;
    void* args[2] = {action, &auto_pass};
    std::string call_error;
    invoke(submit, pending.request, args, &call_error);
    if (!call_error.empty()) return error_json("SubmitAction threw: " + call_error);
    plog("submitted action %d: %s", index, action_json(action, index).c_str());
    return "{\"ok\":true,\"submitted\":" + action_json(action, index) + "}";
}

// Calls a no-argument or single-uint method on the pending request after
// checking its class name.
static std::string command_call_request(const char* expected_class, const char* method_name,
                                        long long uint_arg = -1) {
    Pending pending = find_pending();
    if (!pending.request) return error_json("no pending request: " + pending.error);
    void* request_class = il2cpp_object_get_class(pending.request);
    if (strcmp(request_name(pending.request), expected_class) != 0)
        return error_json(std::string("pending request is ") + class_fullname(request_class) + ", not " +
                          expected_class);
    int argc = uint_arg >= 0 ? 1 : 0;
    void* method = find_method(request_class, method_name, argc);
    if (!method) return error_json(std::string(method_name) + " not found");
    uint32_t value = static_cast<uint32_t>(uint_arg);
    void* args[1] = {&value};
    std::string call_error;
    invoke(method, pending.request, argc ? args : nullptr, &call_error);
    if (!call_error.empty()) return error_json(std::string(method_name) + " threw: " + call_error);
    plog("called %s.%s", expected_class, method_name);
    return "{\"ok\":true,\"called\":" + q(std::string(expected_class) + "." + method_name) + "}";
}

// ---------------------------------------------------------------------------
// Bridge protocol (same commands and fields as the BepInEx plugin; main thread)
// ---------------------------------------------------------------------------

static const char* kBridgeVersion = "mac-il2cpp-0.2.0";
static const int kMainThreadBudgetMs = 3500;  // under gre_bridge.py's 5 s default read timeout
static std::atomic<bool> g_bridge_connected{false};

static void append_int_member(std::string* out, void* obj, const char* member, const char* key, bool skip_zero) {
    int64_t value = 0;
    std::string error;
    if (!get_int(obj, member, &value, &error) || (skip_zero && value == 0)) return;
    *out += std::string(",\"") + key + "\":" + std::to_string(value);
}

static std::string serialize_action(void* action) {
    std::string out = "{\"actionType\":" + q(get_enum_name(action, "ActionType"));
    append_int_member(&out, action, "GrpId", "grpId", false);
    append_int_member(&out, action, "InstanceId", "instanceId", false);
    append_int_member(&out, action, "AbilityGrpId", "abilityGrpId", true);
    append_int_member(&out, action, "SourceId", "sourceId", true);
    append_int_member(&out, action, "AlternativeGrpId", "alternativeGrpId", true);
    append_int_member(&out, action, "FacetId", "facetId", true);
    append_int_member(&out, action, "UniqueAbilityId", "uniqueAbilityId", true);
    std::string error;
    int64_t payable = 0;
    if (get_int(action, "AssumeCanBePaidFor", &payable, &error)) out += ",\"assumeCanBePaidFor\":" + json_bool(payable);
    if (void* costs = get_object(action, "ManaCost", &error)) {
        int count = list_count(costs, &error);
        if (count > 0) {
            out += ",\"manaCost\":[";
            for (int i = 0; i < count; i++) {
                void* cost = list_item(costs, i, &error);
                int64_t amount = 0;
                get_int(cost, "Count", &amount, &error);
                out += std::string(i ? "," : "") + "{\"color\":" +
                       q(object_to_string(get_object(cost, "Color", &error))) +
                       ",\"count\":" + std::to_string(amount) + "}";
            }
            out += "]";
        }
    }
    if (void* solution = get_object(action, "AutoTapSolution", &error)) {
        out += ",\"hasAutoTap\":true";
        void* taps = get_object(solution, "AutoTapActions", &error);
        int count = taps ? list_count(taps, &error) : 0;
        if (count > 0) {
            out += ",\"autoTapActions\":[";
            for (int i = 0; i < count; i++) {
                void* tap = list_item(taps, i, &error);
                int64_t instance_id = 0, mana_id = 0;
                get_int(tap, "InstanceId", &instance_id, &error);
                get_int(tap, "ManaId", &mana_id, &error);
                out += std::string(i ? "," : "") + "{\"instanceId\":" + std::to_string(instance_id) +
                       ",\"manaId\":" + std::to_string(mana_id) + "}";
            }
            out += "]";
        }
    }
    return out + "}";
}

static std::string serialize_actions(void* list) {
    if (!list) return "[]";
    std::string error;
    int count = list_count(list, &error);
    std::string out = "[";
    for (int i = 0; i < count; i++) {
        void* action = list_item(list, i, &error);
        out += std::string(i ? "," : "") + (action ? serialize_action(action) : "null");
    }
    return out + "]";
}

// PayCostsRequest wraps the "Auto Pay" solution in an AutoTapActionsRequest child.
static void* auto_tap_request(void* request) {
    const char* name = request_name(request);
    if (strcmp(name, "AutoTapActionsRequest") == 0) return request;
    if (strcmp(name, "PayCostsRequest") != 0) return nullptr;
    std::string error;
    if (void* child = get_object(request, "AutoTapActions", &error)) return child;
    void* children = get_object(request, "ChildRequests", &error);
    int count = children ? list_count(children, &error) : 0;
    for (int i = 0; i < count; i++) {
        void* child = list_item(children, i, &error);
        if (child && strcmp(request_name(child), "AutoTapActionsRequest") == 0) return child;
    }
    return nullptr;
}

static std::string bridge_get_pending() {
    Pending pending = find_pending();
    if (!pending.request) return "{\"ok\":true,\"has_pending\":false,\"request_type\":null}";
    void* request = pending.request;
    std::string name = request_name(request);
    std::string error;
    int64_t value = 0;
    std::string out = "{\"ok\":true,\"has_pending\":true,\"request_type\":" + q(get_enum_name(request, "Type")) +
                      ",\"request_class\":" + q(name);
    if (get_int(request, "CanCancel", &value, &error)) out += ",\"can_cancel\":" + json_bool(value);
    if (get_int(request, "AllowUndo", &value, &error)) out += ",\"allow_undo\":" + json_bool(value);
    if (void* message = get_object(request, "OriginalMessage", &error)) {
        if (get_int(message, "GameStateId", &value, &error)) out += ",\"game_state_id\":" + std::to_string(value);
        if (get_int(message, "MsgId", &value, &error)) out += ",\"msg_id\":" + std::to_string(value);
    }
    if (name == "ActionsAvailableRequest") {
        out += ",\"actions\":" + serialize_actions(get_object(request, "Actions", &error));
        if (void* inactive = get_object(request, "InactiveActions", &error))
            out += ",\"inactive_actions\":" + serialize_actions(inactive);
        if (get_int(request, "CanPass", &value, &error)) out += ",\"can_pass\":" + json_bool(value);
    } else if (name == "PayCostsRequest" || name == "AutoTapActionsRequest") {
        void* auto_tap = auto_tap_request(request);
        void* solutions = auto_tap ? get_object(auto_tap, "Solutions", &error) : nullptr;
        int count = solutions ? std::max(0, list_count(solutions, &error)) : 0;
        out += ",\"auto_tap_solution_count\":" + std::to_string(count) + ",\"can_pass\":false";
        if (void* requirements = get_object(request, "ManaRequirements", &error))
            out += ",\"mana_requirement_count\":" + std::to_string(std::max(0, list_count(requirements, &error)));
    } else if (name == "MulliganRequest" || name == "ChooseStartingPlayerRequest" ||
               name == "OptionalActionMessageRequest") {
        out += ",\"can_pass\":false";
    } else {
        // Reported, never guessed at: Python sees the class and no actions.
        out += ",\"can_pass\":false,\"bridge_unsupported\":true";
    }
    return out + ",\"bridge_runtime\":" + q(kRuntime) + "}";
}

// Returns an error message, or "" on success.
static std::string call_method(void* target, const char* method_name, void** args, int argc) {
    void* method = find_method(il2cpp_object_get_class(target), method_name, argc);
    if (!method) return std::string(method_name) + " not found";
    std::string error;
    invoke(method, target, args, &error);
    return error.empty() ? "" : std::string(method_name) + " threw: " + error;
}

static std::string pending_is_not(const Pending& pending, const char* wanted) {
    return error_json(std::string("Pending is ") + (pending.request ? request_name(pending.request) : "null") +
                      ", not " + wanted);
}

struct Expected {
    long long instance_id = -1;
    long long grp_id = -1;
    long long game_state_id = -1;
    std::string action_type;
};

static std::string bridge_submit_action(int index, bool auto_pass, const Expected& expected) {
    Pending pending = find_pending();
    if (!pending.request) return error_json("No pending interaction");
    if (strcmp(request_name(pending.request), "ActionsAvailableRequest") != 0)
        return pending_is_not(pending, "ActionsAvailableRequest");
    std::string error;
    if (expected.game_state_id >= 0) {
        int64_t current = -1;
        void* message = get_object(pending.request, "OriginalMessage", &error);
        if (!message || !get_int(message, "GameStateId", &current, &error) || current != expected.game_state_id)
            return error_json("stale command: request game_state_id " + std::to_string(current) + " != expected " +
                              std::to_string(expected.game_state_id));
    }
    void* actions = get_object(pending.request, "Actions", &error);
    int count = actions ? list_count(actions, &error) : -1;
    if (index < 0 || index >= count)
        return error_json("Action index " + std::to_string(index) + " out of range (" + std::to_string(count) +
                          " actions)");
    void* action = list_item(actions, index, &error);
    if (!action) return error_json("Action is null: " + error);
    int64_t grp_id = 0, instance_id = 0;
    get_int(action, "GrpId", &grp_id, &error);
    get_int(action, "InstanceId", &instance_id, &error);
    std::string type = get_enum_name(action, "ActionType");
    if ((expected.instance_id >= 0 && instance_id != expected.instance_id) ||
        (expected.grp_id >= 0 && grp_id != expected.grp_id) ||
        (!expected.action_type.empty() && type != expected.action_type))
        return error_json("identity mismatch at index " + std::to_string(index) + ": " + type + " grp " +
                          std::to_string(grp_id) + " instance " + std::to_string(instance_id));
    void* args[2] = {action, &auto_pass};
    std::string failure = call_method(pending.request, "SubmitAction", args, 2);
    if (!failure.empty()) return error_json(failure);
    plog("bridge submit_action %d: %s grp=%lld instance=%lld", index, type.c_str(), static_cast<long long>(grp_id),
         static_cast<long long>(instance_id));
    return "{\"ok\":true,\"submitted_type\":" + q(type) + ",\"submitted_grp_id\":" + std::to_string(grp_id) +
           ",\"submitted_instance_id\":" + std::to_string(instance_id) + "}";
}

static std::string bridge_submit_pass() {
    Pending pending = find_pending();
    if (!pending.request) return error_json("No pending interaction");
    if (strcmp(request_name(pending.request), "ActionsAvailableRequest") != 0)
        return pending_is_not(pending, "ActionsAvailableRequest");
    int64_t can_pass = 0;
    std::string error;
    if (get_int(pending.request, "CanPass", &can_pass, &error) && !can_pass)
        return error_json("Pass is not available in this request");
    std::string failure = call_method(pending.request, "SubmitPass", nullptr, 0);
    if (!failure.empty()) return error_json(failure);
    plog("bridge submit_pass");
    return "{\"ok\":true,\"submitted_type\":\"Pass\"}";
}

static std::string bridge_submit_mulligan(bool keep) {
    Pending pending = find_pending();
    if (!pending.request || strcmp(request_name(pending.request), "MulliganRequest") != 0)
        return pending_is_not(pending, "MulliganRequest");
    std::string failure = call_method(pending.request, keep ? "KeepHand" : "MulliganHand", nullptr, 0);
    if (!failure.empty()) return error_json(failure);
    plog("bridge submit_mulligan keep=%d", keep);
    return std::string("{\"ok\":true,\"submitted_type\":") + (keep ? "\"Keep\"" : "\"Mulligan\"") + "}";
}

static std::string bridge_submit_choose_starting_player(long long seat_id) {
    Pending pending = find_pending();
    if (!pending.request || strcmp(request_name(pending.request), "ChooseStartingPlayerRequest") != 0)
        return pending_is_not(pending, "ChooseStartingPlayerRequest");
    if (seat_id < 0) return error_json("seat_id is required");
    uint32_t seat = static_cast<uint32_t>(seat_id);
    void* args[1] = {&seat};
    std::string failure = call_method(pending.request, "ChooseStartingPlayer", args, 1);
    if (!failure.empty()) return error_json(failure);
    plog("bridge submit_choose_starting_player seat=%lld", seat_id);
    return "{\"ok\":true,\"submitted_type\":\"ChooseStartingPlayer\",\"seat_id\":" + std::to_string(seat_id) + "}";
}

static std::string bridge_submit_auto_tap(int solution_index) {
    Pending pending = find_pending();
    if (!pending.request) return error_json("No pending interaction");
    void* auto_tap = auto_tap_request(pending.request);
    if (!auto_tap)
        return error_json(std::string("Pending is ") + request_name(pending.request) +
                          ", no AutoTapActionsRequest available");
    std::string error;
    void* solutions = get_object(auto_tap, "Solutions", &error);
    int count = solutions ? list_count(solutions, &error) : 0;
    if (count <= 0) return error_json("AutoTap Solutions list is empty");
    if (solution_index < 0 || solution_index >= count)
        return error_json("AutoTap solution index " + std::to_string(solution_index) + " out of range (0-" +
                          std::to_string(count - 1) + ")");
    void* args[1] = {list_item(solutions, solution_index, &error)};
    if (!args[0]) return error_json("AutoTap solution is null: " + error);
    std::string failure = call_method(auto_tap, "SubmitSolution", args, 1);
    if (!failure.empty()) return error_json(failure);
    plog("bridge submit_auto_tap %d of %d", solution_index, count);
    return "{\"ok\":true,\"submitted_type\":\"AutoTap\",\"solution_index\":" + std::to_string(solution_index) +
           ",\"solution_count\":" + std::to_string(count) + "}";
}

static std::string bridge_submit_optional(bool accept) {
    Pending pending = find_pending();
    if (!pending.request || strcmp(request_name(pending.request), "OptionalActionMessageRequest") != 0)
        return pending_is_not(pending, "OptionalActionMessageRequest");
    void* method = find_method(il2cpp_object_get_class(pending.request), "SubmitResponse", 1);
    if (!method) return error_json("SubmitResponse not found");
    void* enum_class = il2cpp_class_from_type(il2cpp_method_get_param(method, 0));
    int32_t response = 0;
    bool found = accept ? (enum_constant(enum_class, "AllowYes", &response) ||
                           enum_constant(enum_class, "Allow_Yes", &response))
                        : (enum_constant(enum_class, "CancelNo", &response) ||
                           enum_constant(enum_class, "Cancel_No", &response));
    if (!found) return error_json("OptionResponse constant not found on " + class_fullname(enum_class));
    void* args[1] = {&response};
    std::string error;
    invoke(method, pending.request, args, &error);
    if (!error.empty()) return error_json("SubmitResponse threw: " + error);
    plog("bridge submit_optional accept=%d", accept);
    return std::string("{\"ok\":true,\"submitted_type\":\"Optional\",\"response\":") +
           (accept ? "\"AllowYes\"" : "\"CancelNo\"") + "}";
}

// ---------------------------------------------------------------------------
// Remote reflection ("reflect-1"). arenamcp.mac_bridge_adapter implements the
// BepInEx plugin's protocol on top of these generic operations, so MTGA-specific
// logic lives in Python and can change without restarting the game. A batch runs
// on Unity's main thread in one frame; `expect_pending` makes identity checks and
// submissions atomic.
// ---------------------------------------------------------------------------

enum : int {
    kTypeR4 = 0x0c, kTypeR8 = 0x0d, kTypeString = 0x0e, kTypeClass = 0x12, kTypeArray = 0x14,
    kTypeGenericInst = 0x15, kTypeI = 0x18, kTypeU = 0x19, kTypeObject = 0x1c, kTypeSzArray = 0x1d,
};

static void* g_string_class = nullptr;
static void* g_system_object_class = nullptr;
static void* g_value_type_class = nullptr;
static void* g_enum_class = nullptr;
static void* g_delegate_class = nullptr;
static void* g_unity_object_class = nullptr;
static size_t g_array_header = 0;  // verified offset of element 0 in an Il2CppArray, 0 = unknown
static bool g_handles_ok = false;  // GC handle round trip verified at startup

static void init_reflection() {
    g_string_class = find_class("System", "String");
    g_system_object_class = find_class("System", "Object");
    g_value_type_class = find_class("System", "ValueType");
    g_enum_class = find_class("System", "Enum");
    g_delegate_class = find_class("System", "Delegate");
    g_unity_object_class = find_class("UnityEngine", "Object");
    // Il2CppArray = {klass, monitor, bounds, max_length} then elements; confirm
    // max_length sits in the fourth word before trusting that layout.
    void* uint_class = find_class("System", "UInt32");
    void* probe = uint_class ? il2cpp_array_new(uint_class, 5) : nullptr;
    if (probe && il2cpp_array_length(probe) == 5 &&
        *reinterpret_cast<uintptr_t*>(static_cast<uint8_t*>(probe) + 3 * sizeof(void*)) == 5)
        g_array_header = 4 * sizeof(void*);
    // GC handles changed width across IL2CPP versions; a mismatched ABI crashes
    // the game on the first free, so prove a round trip before handing any out.
    void* sample = il2cpp_string_new("mtgacoach");
    uintptr_t gc = sample ? il2cpp_gchandle_new(sample, false) : 0;
    g_handles_ok = gc != 0 && il2cpp_gchandle_get_target(gc) == sample;
    if (gc) il2cpp_gchandle_free(gc);
    plog("reflection ready: array_header=%zu handles=%d delegate=%d unity_object=%d", g_array_header, g_handles_ok,
         g_delegate_class != nullptr, g_unity_object_class != nullptr);
}

static void* class_by_full_name(const std::string& full_name) {
    size_t dot = full_name.rfind('.');
    if (dot == std::string::npos) return find_class("", full_name.c_str());
    return find_class(full_name.substr(0, dot).c_str(), full_name.substr(dot + 1).c_str());
}

// Handles: strong GC handles, one stable id per live object, expired after
// kHandleGenerations batches without use. Main thread only.
struct HandleEntry {
    uintptr_t gc_handle;  // pointer-sized since IL2CPP 2021.2; truncating it crashed the game
    uint64_t generation;
    void* object;
};
static std::unordered_map<uint32_t, HandleEntry> g_handles;
static std::unordered_map<void*, uint32_t> g_handle_ids;
static uint32_t g_next_handle = 1;
static uint64_t g_generation = 0;
static const uint64_t kHandleGenerations = 64;

static uint32_t handle_for(void* obj) {
    if (!g_handles_ok) return 0;  // 0 = no handle; {"h": 0} never resolves
    auto known = g_handle_ids.find(obj);
    if (known != g_handle_ids.end()) {
        g_handles[known->second].generation = g_generation;
        return known->second;
    }
    uint32_t id = g_next_handle++;
    g_handles[id] = {il2cpp_gchandle_new(obj, false), g_generation, obj};
    g_handle_ids[obj] = id;
    return id;
}

static void* handle_object(uint32_t id) {
    auto entry = g_handles.find(id);
    return entry == g_handles.end() ? nullptr : il2cpp_gchandle_get_target(entry->second.gc_handle);
}

static void begin_generation() {
    g_generation++;
    for (auto it = g_handles.begin(); it != g_handles.end();) {
        if (it->second.generation + kHandleGenerations < g_generation) {
            il2cpp_gchandle_free(it->second.gc_handle);
            g_handle_ids.erase(it->second.object);
            it = g_handles.erase(it);
        } else {
            ++it;
        }
    }
}

static bool is_list_class(void* klass) {
    const char* name = il2cpp_class_get_name(klass);
    for (const char* prefix : {"List`1", "RepeatedField`1", "ReadOnlyCollection`1", "Collection`1"})
        if (strncmp(name, prefix, strlen(prefix)) == 0) return true;
    return false;
}

static size_t value_size(void* type) {
    void* klass = il2cpp_class_from_type(type);
    if (!klass || !il2cpp_class_is_valuetype(klass)) return sizeof(void*);
    uint32_t align = 0;
    int32_t size = il2cpp_class_value_size(klass, &align);
    return size > 0 ? static_cast<size_t>(size) : sizeof(void*);
}

static std::string clean_field_name(const char* raw) {
    std::string name = raw ? raw : "";
    if (name.size() > 18 && name[0] == '<' && name.compare(name.size() - 16, 16, ">k__BackingField") == 0)
        return name.substr(1, name.size() - 17);
    return name;
}

static std::string format_number(double value) {
    char buffer[32];
    snprintf(buffer, sizeof buffer, "%.9g", value);
    return buffer;
}

class Encoder {
  public:
    Encoder(int max_nodes, int max_items, std::unordered_set<std::string> skip)
        : max_nodes_(max_nodes), max_items_(max_items), skip_(std::move(skip)) {}

    std::string object(void* obj, int depth) {
        if (!obj) return "null";
        void* klass = il2cpp_object_get_class(obj);
        if (klass == g_string_class) return q(managed_string(obj));
        if (il2cpp_class_is_valuetype(klass))
            return value(static_cast<const uint8_t*>(il2cpp_object_unbox(obj)), il2cpp_class_get_type(klass), depth);
        std::string out = "{\"$c\":" + q(type_name(il2cpp_class_get_type(klass))) +
                          ",\"$h\":" + std::to_string(handle_for(obj));
        if (!seen_.insert(obj).second) return out + ",\"$ref\":true}";
        if (depth <= 0 || ++nodes_ > max_nodes_) return out + ",\"$more\":true}";
        if (g_unity_object_class && il2cpp_class_is_subclass_of(klass, g_unity_object_class, false))
            return out + ",\"$unity\":true}";
        if (il2cpp_class_get_rank(klass) > 0) return out + array_items(obj, klass, depth) + "}";
        if (is_list_class(klass)) return out + list_items(obj, klass, depth) + "}";
        return out + fields(obj, klass, depth) + "}";
    }

    std::string value(const uint8_t* data, void* type, int depth) {
        switch (il2cpp_type_get_type(type)) {
            case kTypeBoolean: return *data ? "true" : "false";
            case kTypeChar: case kTypeU2: return std::to_string(*reinterpret_cast<const uint16_t*>(data));
            case kTypeI1: return std::to_string(*reinterpret_cast<const int8_t*>(data));
            case kTypeU1: return std::to_string(*data);
            case kTypeI2: return std::to_string(*reinterpret_cast<const int16_t*>(data));
            case kTypeI4: return std::to_string(*reinterpret_cast<const int32_t*>(data));
            case kTypeU4: return std::to_string(*reinterpret_cast<const uint32_t*>(data));
            case kTypeI8: case kTypeI: return std::to_string(*reinterpret_cast<const int64_t*>(data));
            case kTypeU8: case kTypeU: return std::to_string(*reinterpret_cast<const uint64_t*>(data));
            case kTypeR4: return format_number(*reinterpret_cast<const float*>(data));
            case kTypeR8: return format_number(*reinterpret_cast<const double*>(data));
            case kTypeString: case kTypeClass: case kTypeObject: case kTypeSzArray: case kTypeArray:
                return object(*reinterpret_cast<void* const*>(data), depth);
            case kTypeValueType: case kTypeGenericInst: {
                void* klass = il2cpp_class_from_type(type);
                if (!klass) return "null";
                if (!il2cpp_class_is_valuetype(klass)) return object(*reinterpret_cast<void* const*>(data), depth);
                if (il2cpp_class_is_enum(klass)) return enum_value(data, klass);
                return struct_value(data, klass, depth);
            }
            default: return "null";  // pointers and unresolved generic parameters
        }
    }

  private:
    int max_nodes_;
    int max_items_;
    int nodes_ = 0;
    std::unordered_set<std::string> skip_;
    std::unordered_set<void*> seen_;

    std::string enum_value(const uint8_t* data, void* klass) {
        int64_t number = 0;
        read_scalar(data, il2cpp_type_get_type(il2cpp_class_enum_basetype(klass)), &number);
        static std::unordered_map<void*, std::unordered_map<int64_t, std::string>> names;
        auto& cache = names[klass];
        auto cached = cache.find(number);
        if (cached == cache.end()) {
            void* boxed = il2cpp_value_box(klass, const_cast<uint8_t*>(data));
            cached = cache.emplace(number, boxed ? object_to_string(boxed) : "").first;
        }
        return "{\"e\":" + q(cached->second) + ",\"v\":" + std::to_string(number) + "}";
    }

    std::string struct_value(const uint8_t* data, void* klass, int depth) {
        std::string out = "{\"$c\":" + q(type_name(il2cpp_class_get_type(klass))) + ",\"$struct\":true";
        if (depth <= 0) return out + ",\"$more\":true}";
        void* boxed = il2cpp_value_box(klass, const_cast<uint8_t*>(data));
        return out + (boxed ? fields(boxed, klass, depth) : "") + "}";
    }

    std::string fields(void* obj, void* klass, int depth) {
        std::string out;
        for (void* k = klass; k && k != g_system_object_class && k != g_value_type_class && k != g_enum_class;
             k = il2cpp_class_get_parent(k)) {
            void* iter = nullptr;
            while (void* field = il2cpp_class_get_fields(k, &iter)) {
                if (il2cpp_field_get_flags(field) & kFieldStatic) continue;
                std::string name = clean_field_name(il2cpp_field_get_name(field));
                if (name.empty() || name[0] == '<' || name == "_parser" || name == "_unknownFields" ||
                    skip_.count(name))
                    continue;
                void* type = il2cpp_field_get_type(field);
                void* field_class = il2cpp_class_from_type(type);
                if (field_class && g_delegate_class && il2cpp_class_is_subclass_of(field_class, g_delegate_class, false))
                    continue;
                std::vector<uint8_t> buffer(std::max<size_t>(16, value_size(type)), 0);
                il2cpp_field_get_value(obj, field, buffer.data());
                out += "," + q(name) + ":" + value(buffer.data(), type, depth - 1);
            }
        }
        return out;
    }

    std::string list_items(void* list, void* klass, int depth) {
        std::string error;
        int count = std::max(0, list_count(list, &error));
        void* item_getter = find_method(klass, "get_Item", 1, "System.Int32");
        std::string out = ",\"$n\":" + std::to_string(count) + ",\"$items\":[";
        int shown = item_getter ? std::min(count, max_items_) : 0;
        for (int i = 0; i < shown; i++) {
            int32_t position = i;
            void* args[1] = {&position};
            out += (i ? "," : "") + object(invoke(item_getter, list, args, &error), depth - 1);
        }
        return out + "]";
    }

    std::string array_items(void* array, void* klass, int depth) {
        uint32_t length = il2cpp_array_length(array);
        std::string out = ",\"$n\":" + std::to_string(length) + ",\"$items\":[";
        if (!g_array_header) return out + "],\"$more\":true";
        void* element_type = il2cpp_class_get_type(il2cpp_class_get_element_class(klass));
        size_t element_size = static_cast<size_t>(il2cpp_class_array_element_size(klass));
        const uint8_t* data = static_cast<const uint8_t*>(array) + g_array_header;
        uint32_t shown = std::min<uint32_t>(length, static_cast<uint32_t>(max_items_));
        for (uint32_t i = 0; i < shown; i++)
            out += (i ? "," : "") + value(data + i * element_size, element_type, depth - 1);
        return out + "]";
    }
};

struct Batch {
    std::vector<void*> objects;                 // op results usable as {"ref": i}
    std::deque<std::vector<uint8_t>> storage;   // argument buffers kept alive until the batch ends
};

static void* resolve_target(const Json* target, Batch& batch, std::string* error) {
    if (!target || target->kind != Json::Object) {
        *error = "target must be {\"h\": id} or {\"ref\": op}";
        return nullptr;
    }
    if (const Json* handle = target->get("h")) {
        void* obj = handle_object(static_cast<uint32_t>(json_int(handle, 0)));
        if (!obj) *error = "unknown or expired handle";
        return obj;
    }
    long long index = json_int(target->get("ref"), -1);
    if (index < 0 || index >= static_cast<long long>(batch.objects.size()) || !batch.objects[index]) {
        *error = "ref does not name an object result";
        return nullptr;
    }
    return batch.objects[index];
}

static bool coerce(const Json& arg, void* type, Batch& batch, void** slot, std::string* error);

// A list argument becomes whatever collection the parameter wants: an array for
// T[] and IEnumerable<T>-style interfaces, or new+Add for List<T>/RepeatedField<T>.
static void* build_collection(const Json& items, void* type, Batch& batch, std::string* error) {
    void* klass = il2cpp_class_from_type(type);
    if (!klass) {
        *error = "cannot resolve collection type";
        return nullptr;
    }
    void* element_class = nullptr;
    if (il2cpp_class_get_rank(klass) > 0) {
        element_class = il2cpp_class_get_element_class(klass);
    } else if (il2cpp_class_is_interface(klass)) {
        std::string name = type_name(type);
        size_t open = name.find('<'), close = name.rfind('>');
        std::string element = open != std::string::npos && close > open ? name.substr(open + 1, close - open - 1) : "";
        if (element.empty() || element.find_first_of("<,") != std::string::npos) {
            *error = "unsupported collection parameter " + name;
            return nullptr;
        }
        element_class = class_by_full_name(element);
        if (!element_class) {
            *error = "element class not found: " + element;
            return nullptr;
        }
    }
    if (element_class) {
        if (!g_array_header) {
            *error = "array layout unverified";
            return nullptr;
        }
        void* array = il2cpp_array_new(element_class, items.items.size());
        void* element_type = il2cpp_class_get_type(element_class);
        size_t element_size = static_cast<size_t>(il2cpp_class_array_element_size(il2cpp_object_get_class(array)));
        bool inline_values = il2cpp_class_is_valuetype(element_class);
        for (size_t i = 0; i < items.items.size(); i++) {
            void* slot = nullptr;
            if (!coerce(items.items[i], element_type, batch, &slot, error)) return nullptr;
            uint8_t* target = static_cast<uint8_t*>(array) + g_array_header + i * element_size;
            if (inline_values)
                memcpy(target, slot, element_size);
            else
                il2cpp_gc_wbarrier_set_field(array, reinterpret_cast<void**>(target), slot);
        }
        return array;
    }
    void* collection = il2cpp_object_new(klass);
    void* constructor = find_method(klass, ".ctor", 0);
    void* add = nullptr;
    void* iter = nullptr;
    while (void* method = il2cpp_class_get_methods(klass, &iter)) {
        if (strcmp(il2cpp_method_get_name(method), "Add") != 0 || il2cpp_method_get_param_count(method) != 1) continue;
        void* param_class = il2cpp_class_from_type(il2cpp_method_get_param(method, 0));
        if (param_class && il2cpp_class_is_interface(param_class)) continue;  // Add(IEnumerable<T>)
        add = method;
        break;
    }
    if (!constructor || !add) {
        *error = "cannot construct " + class_fullname(klass);
        return nullptr;
    }
    invoke(constructor, collection, nullptr, error);
    if (!error->empty()) return nullptr;
    for (const Json& item : items.items) {
        void* slot = nullptr;
        if (!coerce(item, il2cpp_method_get_param(add, 0), batch, &slot, error)) return nullptr;
        void* args[1] = {slot};
        invoke(add, collection, args, error);
        if (!error->empty()) return nullptr;
    }
    return collection;
}

// Produces the pointer il2cpp_runtime_invoke expects for one parameter: the object
// itself for reference types, a pointer to the value for value types.
static bool coerce(const Json& arg, void* type, Batch& batch, void** slot, std::string* error) {
    void* klass = il2cpp_class_from_type(type);
    int kind = il2cpp_type_get_type(type);
    bool is_value = klass && il2cpp_class_is_valuetype(klass);
    if (arg.kind != Json::Object) {
        *error = "argument must be an object like {\"uint\": 5}";
        return false;
    }
    if (arg.get("h") || arg.get("ref")) {
        void* obj = resolve_target(&arg, batch, error);
        if (!obj) return false;
        *slot = is_value ? il2cpp_object_unbox(obj) : obj;
        return true;
    }
    if (arg.get("null")) {
        if (is_value) {
            *error = "null passed for value parameter " + type_name(type);
            return false;
        }
        *slot = nullptr;
        return true;
    }
    if (const Json* text = arg.get("str")) {
        *slot = il2cpp_string_new(json_text(text).c_str());
        return true;
    }
    if (const Json* items = arg.get("list")) {
        *slot = build_collection(*items, type, batch, error);
        return *slot != nullptr;
    }
    if (const Json* pairs = arg.get("dict")) {
        // {"dict": [[key, value], ...]} -> new Dictionary<K, V>() + Add(key, value)
        void* add = klass ? find_method(klass, "Add", 2) : nullptr;
        void* constructor = klass ? find_method(klass, ".ctor", 0) : nullptr;
        if (!add || !constructor) {
            *error = "cannot build a dictionary for " + type_name(type);
            return false;
        }
        void* dictionary = il2cpp_object_new(klass);
        invoke(constructor, dictionary, nullptr, error);
        for (const Json& pair : pairs->items) {
            if (!error->empty()) return false;
            if (pair.items.size() != 2) {
                *error = "dict entries must be [key, value]";
                return false;
            }
            void* entry[2] = {nullptr, nullptr};
            for (uint32_t side = 0; side < 2; side++)
                if (!coerce(pair.items[side], il2cpp_method_get_param(add, side), batch, &entry[side], error)) return false;
            invoke(add, dictionary, entry, error);
        }
        *slot = dictionary;
        return error->empty();
    }
    if (!is_value) {
        *error = "scalar passed for reference parameter " + type_name(type);
        return false;
    }
    batch.storage.emplace_back(std::max<size_t>(16, value_size(type)), 0);
    uint8_t* buffer = batch.storage.back().data();
    *slot = buffer;
    int storage_kind = kind;
    if (klass && il2cpp_class_is_enum(klass)) {
        storage_kind = il2cpp_type_get_type(il2cpp_class_enum_basetype(klass));
        if (const Json* name = arg.get("enum")) {
            int32_t constant = 0;
            if (!enum_constant(klass, json_text(name).c_str(), &constant)) {
                *error = "no constant " + json_text(name) + " on " + class_fullname(klass);
                return false;
            }
            memcpy(buffer, &constant, sizeof constant);
            return true;
        }
    }
    const Json* number = nullptr;
    for (const char* key : {"bool", "int", "uint", "long", "ulong", "float", "double"})
        if ((number = arg.get(key))) break;
    if (!number) {
        *error = "unsupported argument for " + type_name(type);
        return false;
    }
    double real = number->kind == Json::Bool ? (number->boolean ? 1 : 0) : number->number;
    long long whole = static_cast<long long>(real);
    switch (storage_kind) {
        case kTypeBoolean: case kTypeI1: case kTypeU1: *buffer = static_cast<uint8_t>(whole); break;
        case kTypeChar: case kTypeI2: case kTypeU2: { uint16_t v = static_cast<uint16_t>(whole); memcpy(buffer, &v, 2); break; }
        case kTypeI4: case kTypeU4: { uint32_t v = static_cast<uint32_t>(whole); memcpy(buffer, &v, 4); break; }
        case kTypeI8: case kTypeU8: case kTypeI: case kTypeU: memcpy(buffer, &whole, 8); break;
        case kTypeR4: { float v = static_cast<float>(real); memcpy(buffer, &v, 4); break; }
        case kTypeR8: memcpy(buffer, &real, 8); break;
        default:
            *error = "cannot pass a number as " + type_name(type);
            return false;
    }
    return true;
}

static bool argument_fits(const Json& arg, void* type, Batch& batch) {
    void* klass = il2cpp_class_from_type(type);
    if (!klass) return false;
    bool is_value = il2cpp_class_is_valuetype(klass);
    if (arg.get("h") || arg.get("ref")) {
        // Object arguments must really be assignable: RepeatedField<T> has both
        // Add(T) and Add(IEnumerable<T>), and passing a T to the second crashes.
        std::string ignored;
        void* obj = resolve_target(&arg, batch, &ignored);
        return obj && il2cpp_class_is_assignable_from(klass, il2cpp_object_get_class(obj));
    }
    if (arg.get("list")) return !is_value && (il2cpp_class_get_rank(klass) > 0 || il2cpp_class_is_interface(klass) ||
                                              is_list_class(klass) || strncmp(il2cpp_class_get_name(klass), "HashSet`1", 9) == 0);
    if (arg.get("dict")) return !is_value && find_method(klass, "Add", 2) != nullptr;
    if (arg.get("str")) return il2cpp_type_get_type(type) == kTypeString;
    if (arg.get("enum")) return il2cpp_class_is_enum(klass);
    if (arg.get("null")) return !is_value;
    return is_value;  // numbers and bools
}

// Most-derived overload first; `signature` (parameter type names) pins an overload.
static void* select_method(void* klass, const std::string& name, const Json* args, const Json* signature,
                           Batch& batch, std::string* error) {
    size_t argc = args ? args->items.size() : 0;
    std::vector<void*> candidates;
    for (void* k = klass; k; k = il2cpp_class_get_parent(k)) {
        void* iter = nullptr;
        while (void* method = il2cpp_class_get_methods(k, &iter)) {
            if (name != il2cpp_method_get_name(method) || il2cpp_method_get_param_count(method) != argc ||
                il2cpp_method_is_generic(method))
                continue;
            if (signature) {
                bool same = signature->items.size() == argc;
                for (size_t i = 0; same && i < argc; i++)
                    same = type_name(il2cpp_method_get_param(method, static_cast<uint32_t>(i))) ==
                           json_text(&signature->items[i]);
                if (!same) continue;
            }
            candidates.push_back(method);
        }
    }
    for (void* method : candidates) {
        bool fits = true;
        for (size_t i = 0; fits && i < argc; i++)
            fits = argument_fits(args->items[i], il2cpp_method_get_param(method, static_cast<uint32_t>(i)), batch);
        if (fits) return method;
    }
    *error = candidates.empty() ? "no method " + name + "/" + std::to_string(argc) + " on " + class_fullname(klass)
                                : "no overload of " + name + " accepts these arguments";
    return nullptr;
}

static void* call_with_args(void* method, void* target, const Json* args, Batch& batch, std::string* error) {
    std::vector<void*> slots(args ? args->items.size() : 0, nullptr);
    for (size_t i = 0; i < slots.size(); i++)
        if (!coerce(args->items[i], il2cpp_method_get_param(method, static_cast<uint32_t>(i)), batch, &slots[i], error))
            return nullptr;
    return invoke(method, target, slots.empty() ? nullptr : slots.data(), error);
}

static std::string run_batch(const Json& request) {
    auto started = std::chrono::steady_clock::now();
    begin_generation();
    const Json* ops = request.get("ops");
    if (!ops || ops->kind != Json::Array) return error_json("reflect_batch needs an ops array");
    Batch batch;
    std::string results;
    for (size_t index = 0; index < ops->items.size(); index++) {
        const Json& op = ops->items[index];
        std::string kind = json_text(op.get("op"));
        std::string error;
        std::unordered_set<std::string> skip;
        if (const Json* names = op.get("skip"))
            for (const Json& name : names->items) skip.insert(json_text(&name));
        Encoder encoder(static_cast<int>(json_int(op.get("max_nodes"), 4000)),
                        static_cast<int>(json_int(op.get("max_items"), 100)), std::move(skip));
        int depth = static_cast<int>(json_int(op.get("depth"), 2));
        std::string result = "null";
        void* object = nullptr;
        if (kind == "pending") {
            Pending pending = find_pending();
            object = pending.request;
            result = object ? encoder.object(object, depth) : "{\"$none\":" + q(pending.error) + "}";
        } else if (kind == "find" || kind == "static") {
            void* klass = class_by_full_name(json_text(op.get("class")));
            if (!klass) {
                error = "class not found: " + json_text(op.get("class"));
            } else if (kind == "find") {
                object = find_scene_object(klass, &error);
                result = encoder.object(object, depth);
            } else {
                std::string member = json_text(op.get("member"));
                void* getter = find_method(klass, ("get_" + member).c_str(), 0);
                void* field = find_field(klass, member.c_str());
                if (getter && !il2cpp_method_is_instance(getter)) {
                    object = invoke(getter, nullptr, nullptr, &error);
                    result = encoder.object(object, depth);
                } else if (field && (il2cpp_field_get_flags(field) & kFieldStatic)) {
                    std::vector<uint8_t> buffer(std::max<size_t>(16, value_size(il2cpp_field_get_type(field))), 0);
                    il2cpp_field_static_get_value(field, buffer.data());
                    result = encoder.value(buffer.data(), il2cpp_field_get_type(field), depth);
                    if (!il2cpp_class_is_valuetype(il2cpp_class_from_type(il2cpp_field_get_type(field))))
                        object = *reinterpret_cast<void**>(buffer.data());
                } else {
                    error = "no static member " + member;
                }
            }
        } else if (kind == "get" || kind == "call" || kind == "set" || kind == "expect_pending" || kind == "expect") {
            void* target = resolve_target(op.get("target"), batch, &error);
            if (target && kind == "expect") {
                // {"class": "ShortName"} and/or {"member": "X", "equals": scalar}; enums
                // match by name (string) or value (number).
                void* klass = il2cpp_object_get_class(target);
                std::string wanted_class = json_text(op.get("class"));
                if (!wanted_class.empty() && wanted_class != il2cpp_class_get_name(klass)) {
                    error = "expected " + wanted_class + ", found " + il2cpp_class_get_name(klass);
                } else if (const Json* wanted = op.get("equals")) {
                    std::string member = json_text(op.get("member"));
                    std::string encoded;
                    Encoder shallow(1, 1, {});
                    if (void* getter = find_method(klass, ("get_" + member).c_str(), 0)) {
                        encoded = shallow.object(invoke(getter, target, nullptr, &error), 1);
                    } else if (void* field = find_field(klass, member.c_str())) {
                        void* type = il2cpp_field_get_type(field);
                        std::vector<uint8_t> buffer(std::max<size_t>(16, value_size(type)), 0);
                        il2cpp_field_get_value(target, field, buffer.data());
                        encoded = shallow.value(buffer.data(), type, 1);
                    } else {
                        error = "no member " + member + " on " + class_fullname(klass);
                    }
                    Json actual;
                    if (error.empty() && JsonParser(encoded).parse(&actual)) {
                        const Json* compare = &actual;
                        if (actual.kind == Json::Object)
                            compare = actual.get(wanted->kind == Json::String ? "e" : "v");
                        bool same = compare && compare->kind == wanted->kind &&
                                    (compare->kind == Json::String   ? compare->text == wanted->text
                                     : compare->kind == Json::Number ? compare->number == wanted->number
                                     : compare->kind == Json::Bool   ? compare->boolean == wanted->boolean
                                                                     : true);
                        if (!same) error = "identity mismatch: " + member + " is " + encoded;
                    } else if (error.empty()) {
                        error = "cannot compare " + member;
                    }
                }
            } else if (target && kind == "expect_pending") {
                Pending pending = find_pending();
                if (pending.request != target)
                    error = std::string("stale: the pending request changed (now ") +
                            (pending.request ? request_name(pending.request) : "none") + ")";
            } else if (target && kind == "get") {
                std::string member = json_text(op.get("member"));
                void* klass = il2cpp_object_get_class(target);
                if (void* getter = find_method(klass, ("get_" + member).c_str(), 0)) {
                    object = invoke(getter, target, nullptr, &error);
                    result = encoder.object(object, depth);
                    if (object && il2cpp_class_is_valuetype(il2cpp_object_get_class(object))) object = nullptr;
                } else if (void* field = find_field(klass, member.c_str())) {
                    void* type = il2cpp_field_get_type(field);
                    std::vector<uint8_t> buffer(std::max<size_t>(16, value_size(type)), 0);
                    il2cpp_field_get_value(target, field, buffer.data());
                    result = encoder.value(buffer.data(), type, depth);
                    if (!il2cpp_class_is_valuetype(il2cpp_class_from_type(type)))
                        object = *reinterpret_cast<void**>(buffer.data());
                } else {
                    error = "no member " + member + " on " + class_fullname(klass);
                }
            } else if (target && kind == "call") {
                void* method = select_method(il2cpp_object_get_class(target), json_text(op.get("method")),
                                             op.get("args"), op.get("sig"), batch, &error);
                if (method) {
                    object = call_with_args(method, target, op.get("args"), batch, &error);
                    if (error.empty()) result = encoder.object(object, depth);
                    if (object && il2cpp_class_is_valuetype(il2cpp_object_get_class(object))) object = nullptr;
                }
            } else if (target && kind == "set") {
                std::string member = json_text(op.get("member"));
                const Json* value = op.get("value");
                Json args;
                args.kind = Json::Array;
                if (value) args.items.push_back(*value);
                void* setter = value ? select_method(il2cpp_object_get_class(target), "set_" + member, &args,
                                                     nullptr, batch, &error)
                                     : nullptr;
                if (setter) {
                    error.clear();
                    call_with_args(setter, target, &args, batch, &error);
                } else if (error.empty()) {
                    error = "set needs a value";
                }
            }
        } else if (kind == "new") {
            void* klass = class_by_full_name(json_text(op.get("class")));
            if (!klass) {
                error = "class not found: " + json_text(op.get("class"));
            } else {
                object = il2cpp_object_new(klass);
                void* constructor = select_method(klass, ".ctor", op.get("args"), op.get("sig"), batch, &error);
                if (constructor) call_with_args(constructor, object, op.get("args"), batch, &error);
                if (error.empty()) result = encoder.object(object, std::max(depth, 0));
            }
        } else {
            error = "unknown op " + kind;
        }
        if (!error.empty() && json_flag(op.get("optional"), false)) {
            error.clear();  // speculative read: absent member or null ref yields null
            result = "null";
            object = nullptr;
        }
        if (!error.empty()) {
            return "{\"ok\":false,\"error\":" + q(error) + ",\"failed_op\":" + std::to_string(index) +
                   ",\"results\":[" + results + "]}";
        }
        results += (index ? "," : "") + result;
        batch.objects.push_back(object);
    }
    double elapsed = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
    return "{\"ok\":true,\"generation\":" + std::to_string(g_generation) + ",\"ms\":" + format_number(elapsed) +
           ",\"results\":[" + results + "]}";
}

static std::string dispatch_bridge(const std::string& line) {
    Json command;
    if (!JsonParser(line).parse(&command) || command.kind != Json::Object) return error_json("invalid JSON command");
    std::string action = json_text(command.get("action"));
    if (action == "ping") {
        if (!g_hooked) return error_json("main-thread hook not installed: " + g_hook_error);
        return "{\"ok\":true,\"version\":" + q(kBridgeVersion) + ",\"runtime\":" + q(kRuntime) + ",\"protocols\":[\"reflect-1\"]" +
               ",\"arch\":" + q(kArch) + ",\"ticks\":" + std::to_string(g_ticks.load()) + "}";
    }
    if (action == "reflect_batch") return run_on_main([command] { return run_batch(command); }, kMainThreadBudgetMs);
    if (action == "get_pending_actions") return run_on_main(bridge_get_pending, kMainThreadBudgetMs);
    if (action == "submit_action") {
        int index = static_cast<int>(json_int(command.get("action_index"), -1));
        bool auto_pass = json_flag(command.get("auto_pass"), false);
        Expected expected;
        expected.instance_id = json_int(command.get("expected_instance_id"), -1);
        expected.grp_id = json_int(command.get("expected_grp_id"), -1);
        expected.game_state_id = json_int(command.get("expected_game_state_id"), -1);
        expected.action_type = json_text(command.get("expected_action_type"));
        return run_on_main([=] { return bridge_submit_action(index, auto_pass, expected); }, kMainThreadBudgetMs);
    }
    if (action == "submit_pass") return run_on_main(bridge_submit_pass, kMainThreadBudgetMs);
    if (action == "submit_mulligan") {
        bool keep = json_flag(command.get("keep"), true);
        return run_on_main([=] { return bridge_submit_mulligan(keep); }, kMainThreadBudgetMs);
    }
    if (action == "submit_choose_starting_player") {
        long long seat = json_int(command.get("seat_id"), -1);
        return run_on_main([=] { return bridge_submit_choose_starting_player(seat); }, kMainThreadBudgetMs);
    }
    if (action == "submit_auto_tap") {
        int index = static_cast<int>(json_int(command.get("solution_index"), 0));
        return run_on_main([=] { return bridge_submit_auto_tap(index); }, kMainThreadBudgetMs);
    }
    if (action == "submit_optional") {
        bool accept = json_flag(command.get("accept"), true);
        return run_on_main([=] { return bridge_submit_optional(accept); }, kMainThreadBudgetMs);
    }
    return "{\"ok\":false,\"unsupported\":true,\"error\":" +
           q("not supported by the macOS IL2CPP bridge yet: " + action) + "}";
}

// ---------------------------------------------------------------------------
// Background-thread diagnostics
// ---------------------------------------------------------------------------

static std::string command_describe(const std::string& ns, const std::string& name) {
    void* klass = find_class(ns.c_str(), name.c_str());
    if (!klass) return error_json("class not found: " + ns + "." + name);
    std::string out = "{\"ok\":true,\"class\":" + q(class_fullname(klass)) + ",\"parents\":[";
    int parent_index = 0;
    for (void* parent = il2cpp_class_get_parent(klass); parent; parent = il2cpp_class_get_parent(parent))
        out += (parent_index++ ? "," : "") + q(class_fullname(parent));
    out += "],\"fields\":[";
    void* iter = nullptr;
    int index = 0;
    while (void* field = il2cpp_class_get_fields(klass, &iter)) {
        bool is_static = il2cpp_field_get_flags(field) & kFieldStatic;
        out += std::string(index++ ? "," : "") + "{\"name\":" + q(il2cpp_field_get_name(field)) +
               ",\"type\":" + q(type_name(il2cpp_field_get_type(field))) +
               ",\"static\":" + (is_static ? "true" : "false") +
               ",\"offset\":" + std::to_string(is_static ? 0 : il2cpp_field_get_offset(field)) + "}";
    }
    out += "],\"properties\":[";
    iter = nullptr;
    index = 0;
    while (void* property = il2cpp_class_get_properties(klass, &iter))
        out += (index++ ? "," : "") + q(il2cpp_property_get_name(property));
    out += "],\"methods\":[";
    iter = nullptr;
    index = 0;
    while (void* method = il2cpp_class_get_methods(klass, &iter))
        out += (index++ ? "," : "") + q(std::string(il2cpp_method_get_name(method)) + "/" +
                                        std::to_string(il2cpp_method_get_param_count(method)));
    return out + "]}";
}

static std::string command_static(const std::string& ns, const std::string& name, const std::string& field_name) {
    void* klass = find_class(ns.c_str(), name.c_str());
    if (!klass) return error_json("class not found");
    void* field = find_field(klass, field_name.c_str());
    if (!field) return error_json("field not found");
    if (!(il2cpp_field_get_flags(field) & kFieldStatic)) return error_json("field is not static");
    void* value = nullptr;
    il2cpp_field_static_get_value(field, &value);
    return "{\"ok\":true,\"non_null\":" + std::string(value ? "true" : "false") +
           ",\"value_class\":" + q(value ? class_fullname(il2cpp_object_get_class(value)) : "null") + "}";
}

static std::string command_ping() {
    return "{\"ok\":true,\"pid\":" + std::to_string(getpid()) + ",\"arch\":" + q(kArch) +
           ",\"hooked\":" + (g_hooked ? "true" : "false") + ",\"hook_error\":" + q(g_hook_error) +
           ",\"ticks\":" + std::to_string(g_ticks.load()) +
           ",\"bridge_connected\":" + json_bool(g_bridge_connected) +
           ",\"images\":" + std::to_string(loaded_images().size()) + "}";
}

static std::vector<std::string> split_words(const std::string& line) {
    std::vector<std::string> words;
    size_t start = 0;
    while (start < line.size()) {
        size_t end = line.find(' ', start);
        if (end == std::string::npos) end = line.size();
        if (end > start) words.push_back(line.substr(start, end - start));
        start = end + 1;
    }
    return words;
}

static std::string dispatch(const std::string& line) {
    std::vector<std::string> words = split_words(line);
    if (words.empty()) return error_json("empty command");
    const std::string& command = words[0];
    auto ns_arg = [](const std::string& value) { return value == "-" ? std::string() : value; };
    if (command == "ping") return command_ping();
    if (command == "class" && words.size() == 3) return command_describe(ns_arg(words[1]), words[2]);
    if (command == "static" && words.size() == 4) return command_static(ns_arg(words[1]), words[2], words[3]);
    if (command == "pending") return run_on_main(command_pending);
    if (command == "submit_action" && words.size() >= 2) {
        int index = atoi(words[1].c_str());
        long long expected = words.size() >= 3 ? atoll(words[2].c_str()) : -1;
        return run_on_main([=] { return command_submit_action(index, expected); });
    }
    if (command == "submit_pass")
        return run_on_main([] { return command_call_request("ActionsAvailableRequest", "SubmitPass"); });
    if (command == "keep")
        return run_on_main([] { return command_call_request("MulliganRequest", "KeepHand"); });
    if (command == "choose_start" && words.size() == 2) {
        long long seat = atoll(words[1].c_str());
        return run_on_main([=] {
            return command_call_request("ChooseStartingPlayerRequest", "ChooseStartingPlayer", seat);
        });
    }
    return error_json("unknown command: " + line);
}

// ---------------------------------------------------------------------------
// Loopback server and startup
// ---------------------------------------------------------------------------

// Returns how many requests were answered.
static size_t serve_lines(int client, std::string (*handler)(const std::string&)) {
#ifdef SO_NOSIGPIPE
    int one = 1;
    setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);  // never SIGPIPE the game
#endif
    std::string buffer;
    char chunk[4096];
    size_t answered = 0;
    for (;;) {
        ssize_t received = recv(client, chunk, sizeof chunk, 0);
        if (received <= 0) return answered;
        buffer.append(chunk, static_cast<size_t>(received));
        size_t newline;
        while ((newline = buffer.find('\n')) != std::string::npos) {
            std::string line = buffer.substr(0, newline);
            buffer.erase(0, newline + 1);
            if (!line.empty() && line.back() == '\r') line.pop_back();
            if (line.empty()) continue;
            std::string response = handler(line) + "\n";
            const char* data = response.data();
            size_t remaining = response.size();
            while (remaining > 0) {
                ssize_t sent = send(client, data, remaining, kSendFlags);
                if (sent <= 0) return answered;
                data += sent;
                remaining -= static_cast<size_t>(sent);
            }
            answered++;
        }
    }
}

static void serve() {
    const char* port_env = getenv("MTGACOACH_PROBE_PORT");
    int port = port_env ? atoi(port_env) : 44223;
    int server = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<uint16_t>(port));
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(server, reinterpret_cast<sockaddr*>(&address), sizeof address) != 0 || listen(server, 2) != 0) {
        plog("cannot listen on 127.0.0.1:%d", port);
        return;
    }
    plog("listening on 127.0.0.1:%d", port);
    for (;;) {
        int client = accept(server, nullptr, nullptr);
        if (client < 0) {
            usleep(100000);
            continue;
        }
        serve_lines(client, dispatch);
        close(client);
    }
}

// Connects to the coach's GRE bridge server, like the BepInEx plugin's
// PipeClientLoop: 1 s connect timeout, 200 ms-2 s backoff, self-connect guard.
static int connect_to_coach(int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    int one = 1;
#ifdef SO_NOSIGPIPE
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<uint16_t>(port));
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(fd, reinterpret_cast<sockaddr*>(&address), sizeof address) != 0) {
        pollfd waiter{fd, POLLOUT, 0};
        int error = 0;
        socklen_t length = sizeof error;
        if (errno != EINPROGRESS || poll(&waiter, 1, 1000) != 1 ||
            getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &length) != 0 || error != 0) {
            close(fd);
            return -1;
        }
    }
    fcntl(fd, F_SETFL, flags);
    sockaddr_in local{};
    socklen_t local_length = sizeof local;
    if (getsockname(fd, reinterpret_cast<sockaddr*>(&local), &local_length) == 0 &&
        ntohs(local.sin_port) == port) {
        close(fd);
        return -1;
    }
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    return fd;
}

static void* bridge_client(void*) {
    const char* port_env = getenv("MTGACOACH_BRIDGE_PORT");
    int port = port_env ? atoi(port_env) : 44222;
    int retry_ms = 200;
    for (int failures = 0;; ) {
        int fd = connect_to_coach(port);
        if (fd < 0) {
            if (failures++ % 60 == 0) plog("bridge: coach not listening on 127.0.0.1:%d (attempt %d)", port, failures);
            usleep(static_cast<useconds_t>(retry_ms) * 1000);
            retry_ms = std::min(2000, retry_ms * 2);
            continue;
        }
        g_bridge_connected = true;
        size_t answered = serve_lines(fd, dispatch_bridge);
        close(fd);
        g_bridge_connected = false;
        if (answered == 0) {
            // adb reverse (Android) accepts even with nothing listening behind it,
            // so a session that ends without a request is a failed connect.
            if (failures++ % 60 == 0) plog("bridge: no coach behind 127.0.0.1:%d (attempt %d)", port, failures);
            usleep(static_cast<useconds_t>(retry_ms) * 1000);
            retry_ms = std::min(2000, retry_ms * 2);
            continue;
        }
        failures = 0;
        retry_ms = 200;
        plog("bridge: coach session ended after %zu requests; reconnecting", answered);
        usleep(200000);
    }
    return nullptr;
}

#if defined(__ANDROID__)
// On phones MTGA's only log sink is LogToFile (UTC_Log), which it flushes every
// 30 s; the coach mirrors that file over adb and needs it close to real time.
static void speed_up_log_file() {
    void* klass = find_class("", "LogToFile");
    void* field = klass ? find_field(klass, "_timeBetweenWrites") : nullptr;
    if (!field || !(il2cpp_field_get_flags(field) & kFieldStatic)) {
        plog("LogToFile._timeBetweenWrites not found; UTC_Log stays on its 30 s flush");
        return;
    }
    il2cpp_runtime_class_init(klass);  // its static initializer must not overwrite us later
    float interval = 0.25f;
    il2cpp_field_static_set_value(field, &interval);
    plog("LogToFile flush interval set to %.2f s", interval);
}
#endif

static void* startup(void*) {
    plog("probe loaded (arch=%s)", kArch);
    for (int attempt = 0; !resolve_api(); attempt++) {
        if (attempt == 1200) {
            plog("%s exports never resolved; probe idle", kGameLibrary + 1);
            return nullptr;
        }
        usleep(250000);
    }
    plog("il2cpp API resolved");
    // The library is mapped long before il2cpp_init runs (seconds on Android,
    // where libunity dlopens it at startup), and the domain calls below
    // null-deref until then (crashed MTGA 2026-09-24 21:23). il2cpp_get_corlib
    // only returns a global that init sets, so it is safe to poll.
    // No give-up: on Android a relaunched MTGA can sit in the background (screen
    // off, lock screen up) for any length of time before Unity starts.
    for (int attempt = 0; !il2cpp_get_corlib(); attempt++) {
        if (attempt == 240) plog("il2cpp runtime not initialized after 60 s; still waiting");
        usleep(attempt < 240 ? 250000 : 1000000);
    }
    plog("il2cpp runtime initialized");
    sleep(3);  // let il2cpp_init finish registering assemblies before touching the domain
    void* papa = nullptr;
    bool attached = false;
    for (int attempt = 0; attempt < 600 && !papa; attempt++) {
        size_t count = 0;
        il2cpp_domain_get_assemblies(il2cpp_domain_get(), &count);
        if (count > 0 && has_image("Core.dll")) {
            if (!attached) {
                il2cpp_thread_attach(il2cpp_domain_get());
                attached = true;
            }
            papa = find_class("", "PAPA");
        }
        if (!papa) sleep(1);
    }
    if (papa) init_reflection();
#if defined(__ANDROID__)
    if (papa) speed_up_log_file();
#endif
    if (!papa) {
        g_hook_error = "PAPA class not found";
        plog("PAPA class not found; serving diagnostics only");
    } else if (!install_update_hook(papa)) {
        plog("main-thread hook failed: %s", g_hook_error.c_str());
    }
    pthread_t bridge_thread;
    if (pthread_create(&bridge_thread, nullptr, bridge_client, nullptr) == 0) pthread_detach(bridge_thread);
    serve();
    return nullptr;
}

__attribute__((constructor)) static void probe_constructor() {
#if defined(__ANDROID__)
    unsetenv("LD_PRELOAD");
    char name[256] = {};
    if (FILE* cmdline = fopen("/proc/self/cmdline", "r")) {
        fread(name, 1, sizeof name - 1, cmdline);
        fclose(cmdline);
    }
    // A wrap.<package> LD_PRELOAD runs before the process is renamed, so only
    // named secondary processes (com.wizards.mtga:<service>) are excluded.
    if (strchr(name, ':') && !getenv("MTGACOACH_PROBE_ANY_PROCESS")) return;
    std::string log_path = "/storage/emulated/0/Android/data/com.wizards.mtga/files/il2cpp_probe.log";
    if (const char* custom = getenv("MTGACOACH_PROBE_LOG")) log_path = custom;
#else
    // Children (browser helpers, crash reporters) must not inherit the probe.
    unsetenv("DYLD_INSERT_LIBRARIES");
    char path[PATH_MAX];
    uint32_t size = sizeof path;
    if (_NSGetExecutablePath(path, &size) != 0) return;
    if (!strstr(path, "/MTGA.app/Contents/MacOS/MTGA") && !getenv("MTGACOACH_PROBE_ANY_PROCESS")) return;
    std::string log_path;
    if (const char* custom = getenv("MTGACOACH_PROBE_LOG")) {
        log_path = custom;
    } else if (const char* home = getenv("HOME")) {
        log_path = std::string(home) + "/.arenamcp/il2cpp_probe.log";
    }
#endif
    if (!log_path.empty()) g_log = fopen(log_path.c_str(), "a");
    pthread_t thread;
    if (pthread_create(&thread, nullptr, startup, nullptr) == 0) pthread_detach(thread);
}
