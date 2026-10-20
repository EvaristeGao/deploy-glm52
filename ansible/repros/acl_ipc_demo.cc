// ACL 跨进程 IPC 显存导出/导入最小复现（不依赖 HIXL / mooncake / vLLM）
//
// 目的：mooncake ascend transport 在跨进程/跨节点传 KV 时最终落到
//   aclrtIpcMemGetExportKey / aclrtIpcMemImportByKey（报错 return[107000] + invalid value）。
// 本 demo 把这个原语单独拎出来：进程 A 导出自己 device 显存的 key，进程 B 导入并读回校验。
//
// 编译（容器内，CANN 9.1.0）：
//   g++ -std=c++17 -O2 acl_ipc_demo.cc \
//       -I/usr/local/Ascend/cann-9.1.0/aarch64-linux/include \
//       -L/usr/local/Ascend/cann-9.1.0/aarch64-linux/lib64 \
//       -lascendcl -o acl_ipc_demo
//
// 用法：
//   ./acl_ipc_demo export --device 0 --size-mb 1 --hold 60 --key-file /tmp/k.txt [--no-pid-check]
//   ./acl_ipc_demo import --device 0 --size-mb 1 --key-file /tmp/k.txt [--peer-access] [--set-import-pid]
//
// 判据：import 打印 "RESULT PASS" = 原语可用；否则打印 acl 返回码（107000 = invalid value）。
#include <acl/acl.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

#include <unistd.h>

namespace {

constexpr unsigned char kPattern = 0xAB;
constexpr size_t kKeyLen = 256;

#define ACK(expr)                                                                      \
  do {                                                                                 \
    aclError _rc = (expr);                                                             \
    if (_rc != ACL_SUCCESS) {                                                          \
      std::printf("[acl] %s failed rc=%d (%s:%d)\n", #expr, static_cast<int>(_rc),      \
                  __FILE__, __LINE__);                                                 \
      return 2;                                                                        \
    }                                                                                  \
  } while (0)

void Usage() {
  std::printf(
      "usage:\n"
      "  export --device N [--size-mb M] [--hold S] [--key-file PATH] [--no-pid-check]\n"
      "  import --device N [--size-mb M] [--key-file PATH] [--peer-access] [--set-import-pid]\n");
}

std::string Arg(int argc, char** argv, const char* name, const char* def = nullptr) {
  for (int i = 2; i + 1 < argc; ++i) {
    if (std::strcmp(argv[i], name) == 0) return argv[i + 1];
  }
  return def ? std::string(def) : std::string();
}

bool HasFlag(int argc, char** argv, const char* name) {
  for (int i = 2; i < argc; ++i) {
    if (std::strcmp(argv[i], name) == 0) return true;
  }
  return false;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    Usage();
    return 1;
  }
  const std::string mode = argv[1];
  const int device = std::atoi(Arg(argc, argv, "--device", "0").c_str());
  const size_t size = static_cast<size_t>(std::atoll(Arg(argc, argv, "--size-mb", "1").c_str())) << 20;
  const int hold_sec = std::atoi(Arg(argc, argv, "--hold", "60").c_str());
  const std::string key_file = Arg(argc, argv, "--key-file", "/tmp/acl_ipc_key.txt");

  ACK(aclInit(nullptr));
  ACK(aclrtSetDevice(device));
  std::printf("[init] device=%d size=%zu mode=%s\n", device, size, mode.c_str());

  void* local = nullptr;
  ACK(aclrtMalloc(&local, size, ACL_MEM_MALLOC_HUGE_FIRST));

  std::vector<unsigned char> host(size, kPattern);
  ACK(aclrtMemcpy(local, size, host.data(), size, ACL_MEMCPY_HOST_TO_DEVICE));

  if (mode == "export") {
    std::vector<char> key(kKeyLen, 0);
    const uint64_t flags = HasFlag(argc, argv, "--no-pid-check")
                               ? ACL_RT_IPC_MEM_EXPORT_FLAG_DISABLE_PID_VALIDATION
                               : ACL_RT_IPC_MEM_EXPORT_FLAG_DEFAULT;
    ACK(aclrtIpcMemGetExportKey(local, size, key.data(), key.size(), flags));
    std::ofstream out(key_file);
    out << key.data() << "\n";
    out.close();
    std::printf("[export] ok flags=0x%lx key_len=%zu key_file=%s\n", static_cast<unsigned long>(flags),
                std::strlen(key.data()), key_file.c_str());
    std::printf("[export] pid=%d\n", static_cast<int>(getpid()));
    std::fflush(stdout);
    sleep(hold_sec);
    aclrtIpcMemClose(key.data());
    aclrtFree(local);
    aclrtResetDevice(device);
    aclFinalize();
    std::printf("RESULT EXPORT_DONE\n");
    return 0;
  }

  if (mode == "import") {
    std::ifstream in(key_file);
    std::string key;
    std::getline(in, key);
    if (key.empty()) {
      std::printf("[import] key file empty: %s\n", key_file.c_str());
      return 2;
    }
    const uint64_t flags = HasFlag(argc, argv, "--peer-access")
                               ? ACL_RT_IPC_MEM_IMPORT_FLAG_ENABLE_PEER_ACCESS
                               : ACL_RT_IPC_MEM_IMPORT_FLAG_DEFAULT;
    if (HasFlag(argc, argv, "--set-import-pid")) {
      int32_t pid = static_cast<int32_t>(getpid());
      aclError rc = aclrtIpcMemSetImportPid(key.c_str(), &pid, 1);
      std::printf("[import] SetImportPid(self=%d) rc=%d\n", pid, static_cast<int>(rc));
    }
    void* peer = nullptr;
    aclError rc = aclrtIpcMemImportByKey(&peer, key.c_str(), flags);
    std::printf("[import] ImportByKey flags=0x%lx rc=%d peer=%p\n", static_cast<unsigned long>(flags),
                static_cast<int>(rc), peer);
    if (rc != ACL_SUCCESS) {
      std::printf("RESULT FAIL (ImportByKey rc=%d)\n", static_cast<int>(rc));
      aclrtFree(local);
      aclrtResetDevice(device);
      aclFinalize();
      return 3;
    }
    std::vector<unsigned char> got(size, 0);
    aclError rc2 = aclrtMemcpy(got.data(), size, peer, size, ACL_MEMCPY_DEVICE_TO_HOST);
    const bool ok = (rc2 == ACL_SUCCESS) && (got[0] == kPattern) && (got[size - 1] == kPattern);
    std::printf("[import] Memcpy rc=%d first=%02x last=%02x\n", static_cast<int>(rc2), got[0],
                got[size - 1]);
    std::printf("RESULT %s\n", ok ? "PASS" : "FAIL (data mismatch)");
    aclrtFree(local);
    aclrtResetDevice(device);
    aclFinalize();
    return ok ? 0 : 4;
  }

  Usage();
  return 1;
}
